"""Task specs: the JSON contract between an orchestrator and the harness.

``load_task`` validates a spec file and resolves it into a frozen ``TaskSpec``:
paths relative to the spec, the (cli, model, reasoning effort) triple from the
model registry or from explicit fields, and the per-attempt reuse policy.
"""

from __future__ import annotations

import json
import os
import shlex
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, cast

from atlas_dispatch.adapter import (
    DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    list_supported_models,
    resolve_model,
)
from atlas_dispatch.verify import DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS
from atlas_dispatch.worktree import is_git_repo

REUSE_POLICY_ALLOW = "allow"
REUSE_POLICY_NEVER = "never"

# A single-segment ``target_repo`` such as ``"my-service"`` is a repo NAME,
# resolved against code roots, so one spec works unchanged on any machine that
# follows the same layout. The roots default to ``~/code`` and can be set with
# ATLAS_DISPATCH_CODE_ROOTS (os.pathsep-separated, searched in order).
CODE_ROOTS_ENV = "ATLAS_DISPATCH_CODE_ROOTS"


@dataclass(frozen=True, kw_only=True)
class TaskSpec:
    id: str
    ticket_ref: str | None = None
    title: str
    target_repo: Path
    cli: str  # always populated; resolved from `model` if only that was given
    prompt_template: Path
    worktree_branch: str
    allowed_paths: list[str]
    forbidden_paths: list[str] = field(default_factory=list)
    mcp_servers: list[dict[str, object]] = field(default_factory=list)
    context_files: list[Path] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)
    acceptance_timeout_seconds: int = DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS
    acceptance_min_collected: int | dict[str, int] | None = None
    # Probe that pytest-bearing acceptance commands import this worktree's
    # packages (not a stale editable install). Default on; see
    # import_provenance.py for when to turn it off.
    check_import_provenance: bool = True
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    idle_timeout_seconds: int = DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS
    base_ref: str = "main"
    model: str | None = None
    reasoning_effort: str | None = None
    extra_prompt_vars: dict[str, str] = field(default_factory=dict)
    push_branch: bool = False
    detached_head: bool = False
    allow_destructive_branch_reset: bool = False
    protected_head_sha: str | None = None
    remote_name: str = "origin"
    runs_dir: Path | None = None
    spec_path: Path = Path()
    resolved_via: str = ""

    @property
    def reuse_policy(self) -> str:
        """One-attempt dispatch control, intentionally outside task identity."""

        return str(getattr(self, "_reuse_policy", REUSE_POLICY_ALLOW))


def _is_single_segment_repo_name(value: str) -> bool:
    return (
        bool(value)
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
        and not value.startswith(".")
    )


def _code_roots() -> list[Path]:
    raw = os.environ.get(CODE_ROOTS_ENV, "").strip()
    if not raw:
        return [Path.home() / "code"]
    return [Path(item).expanduser() for item in raw.split(os.pathsep) if item.strip()]


def _conventional_repo_candidates(name: str) -> list[tuple[Path, str]]:
    return [(root / name, str(root / name)) for root in _code_roots()]


def _resolve_target_repo(value: str, resolve_relative: Callable[[str], Path]) -> Path:
    """Resolve ``target_repo``, honouring the single-segment repo-name form.

    Every other path-like field in a spec is relative to the spec file. A
    single-segment value (``"my-service"``) is instead treated as a repo NAME
    and resolved against the code roots (``~/code/<name>`` by default, or each
    root in ATLAS_DISPATCH_CODE_ROOTS in order), which is what makes a spec
    portable across machines.

    A relative path that actually exists still wins, so ``".."`` and
    ``"../my-service"`` keep their meaning.
    """

    relative = resolve_relative(value)
    if is_git_repo(relative):
        return relative
    if _is_single_segment_repo_name(value):
        for candidate, _ in _conventional_repo_candidates(value):
            if is_git_repo(candidate):
                return candidate.resolve()
    return relative


def load_task(spec_path: Path) -> TaskSpec:
    """Load a task spec from a JSON file.

    Path-like fields (`target_repo`, `prompt_template`, `context_files`,
    `runs_dir`) are resolved relative to the spec file's directory unless
    absolute. The (cli, model, reasoning_effort) triple may come from a
    registry entry under `model` (e.g. "codex/gpt-6-sol-high"), or be
    specified explicitly via `cli`, `model_id`, and `reasoning_effort`.
    """
    spec_path = spec_path.resolve()
    raw = json.loads(spec_path.read_text(encoding="utf-8"))

    base_dir = spec_path.parent

    def _resolve(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (base_dir / path).resolve()

    # Same evaluation order as the fields below, so a spec with several
    # problems always reports the same one first.
    target_repo = _resolve_target_repo(raw["target_repo"], _resolve)
    prompt_template = _resolve(raw["prompt_template"])
    context_files = [_resolve(p) for p in raw.get("context_files", [])]
    runs_dir_raw = raw.get("runs_dir")
    runs_dir = _resolve(runs_dir_raw) if runs_dir_raw else None
    forbidden_paths = list(raw.get("forbidden_paths", []))
    reuse_policy = _parse_reuse_policy(raw, spec_path=spec_path)
    cli, model_id, reasoning_effort, resolved_via = _resolve_cli_and_model(
        raw, spec_path=spec_path
    )
    raw_ticket_ref = raw.get("ticket_ref")
    ticket_ref = (
        str(raw_ticket_ref).strip() if raw_ticket_ref is not None else None
    ) or None
    acceptance = list(raw.get("acceptance", []))
    raw_check_import_provenance = raw.get("check_import_provenance", True)
    if type(raw_check_import_provenance) is not bool:
        raise ValueError(
            f"task spec {spec_path.name}: check_import_provenance must be a "
            "JSON boolean"
        )

    task = TaskSpec(
        id=str(raw["id"]),
        ticket_ref=ticket_ref,
        title=str(raw["title"]),
        target_repo=target_repo,
        cli=str(cli).lower(),
        prompt_template=prompt_template,
        worktree_branch=str(raw["worktree_branch"]),
        allowed_paths=list(raw["allowed_paths"]),
        forbidden_paths=forbidden_paths,
        mcp_servers=list(raw.get("mcp_servers") or []),
        context_files=context_files,
        acceptance=acceptance,
        acceptance_timeout_seconds=int(
            raw.get("acceptance_timeout_seconds", DEFAULT_ACCEPTANCE_TIMEOUT_SECONDS)
        ),
        acceptance_min_collected=_parse_acceptance_min_collected(
            raw,
            spec_path=spec_path,
            acceptance=acceptance,
        ),
        check_import_provenance=raw_check_import_provenance,
        timeout_seconds=int(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
        idle_timeout_seconds=int(
            raw.get("idle_timeout_seconds", DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS)
        ),
        base_ref=str(raw.get("base_ref", "main")),
        model=model_id,
        reasoning_effort=reasoning_effort,
        extra_prompt_vars=dict(raw.get("extra_prompt_vars") or {}),
        push_branch=bool(raw.get("push_branch") or False),
        detached_head=bool(raw.get("detached_head") or False),
        allow_destructive_branch_reset=(
            raw.get("allow_destructive_branch_reset") is True
        ),
        protected_head_sha=(
            str(raw.get("protected_head_sha") or "").strip() or None
        ),
        remote_name=str(raw.get("remote_name") or "origin"),
        runs_dir=runs_dir,
        spec_path=spec_path,
        resolved_via=resolved_via,
    )
    object.__setattr__(task, "_reuse_policy", reuse_policy)
    return task


def _parse_reuse_policy(raw: Mapping[str, Any], *, spec_path: Path) -> str:
    """Read `reuse_policy` and the `no_reuse` shorthand; they must agree."""

    raw_reuse_policy = raw.get("reuse_policy")
    raw_no_reuse = raw.get("no_reuse")
    if raw_reuse_policy is not None and raw_reuse_policy not in {
        REUSE_POLICY_ALLOW,
        REUSE_POLICY_NEVER,
    }:
        raise ValueError(
            f"task spec {spec_path.name}: reuse_policy must be "
            f"{REUSE_POLICY_ALLOW!r} or {REUSE_POLICY_NEVER!r}"
        )
    if raw_no_reuse is not None and type(raw_no_reuse) is not bool:
        raise ValueError(
            f"task spec {spec_path.name}: no_reuse must be a JSON boolean"
        )
    requested_policies = {
        str(policy)
        for present, policy in (
            (raw_reuse_policy is not None, raw_reuse_policy),
            (
                raw_no_reuse is not None,
                REUSE_POLICY_NEVER if raw_no_reuse else REUSE_POLICY_ALLOW,
            ),
        )
        if present
    }
    if len(requested_policies) > 1:
        raise ValueError(
            f"task spec {spec_path.name}: conflicting reuse controls "
            f"{sorted(requested_policies)}"
        )
    return next(iter(requested_policies), REUSE_POLICY_ALLOW)


def _resolve_cli_and_model(
    raw: Mapping[str, Any], *, spec_path: Path
) -> tuple[str, Any, Any, str]:
    """Return (cli, model_id, reasoning_effort, resolved_via) for a spec."""

    cli = raw.get("cli")
    model_name = raw.get("model")
    model_id = raw.get("model_id")
    reasoning_effort = raw.get("reasoning_effort")

    if model_name and model_name in list_supported_models():
        # Registry-style: `model` names a registry entry.
        definition = resolve_model(model_name)
        if reasoning_effort is None:
            reasoning_effort = definition.reasoning_effort
        return (
            definition.cli,
            definition.model_id,
            reasoning_effort,
            f"model_registry:{model_name}",
        )
    if model_name and cli is None:
        raise ValueError(
            f"task spec {spec_path.name}: `model={model_name!r}` is not a "
            f"registry entry and no `cli` was specified. "
            f"Supported registry models: {list_supported_models()}. "
            "Either pick a registry entry, or specify `cli` + `model_id` + "
            "`reasoning_effort` directly."
        )
    # Explicit shape: cli + model_id + reasoning_effort.
    return (cli or "codex").lower(), model_id, reasoning_effort, "explicit"


def _parse_acceptance_min_collected(
    raw: Mapping[str, object],
    *,
    spec_path: Path,
    acceptance: list[str],
) -> int | dict[str, int] | None:
    """Validate the optional global or exact-command collection floor."""

    value = raw.get("acceptance_min_collected")
    if value is None:
        return None
    if type(value) is int:
        if value < 0:
            raise ValueError(
                f"task spec {spec_path.name}: acceptance_min_collected must be >= 0"
            )
        return value
    if not isinstance(value, Mapping):
        raise ValueError(
            f"task spec {spec_path.name}: acceptance_min_collected must be an "
            "integer or a mapping from acceptance command to integer"
        )

    parsed: dict[str, int] = {}
    for command, floor in value.items():
        if not isinstance(command, str) or not command.strip():
            raise ValueError(
                f"task spec {spec_path.name}: acceptance_min_collected mapping "
                "keys must be non-empty command strings"
            )
        if type(floor) is not int or floor < 0:
            raise ValueError(
                f"task spec {spec_path.name}: acceptance_min_collected for "
                f"{command!r} must be an integer >= 0"
            )
        parsed[command] = floor

    unknown_commands = sorted(set(parsed) - set(acceptance))
    if unknown_commands:
        raise ValueError(
            f"task spec {spec_path.name}: acceptance_min_collected names command(s) "
            f"not present in acceptance: {unknown_commands}"
        )
    return parsed


def _serialized_task(task: TaskSpec) -> dict[str, Any]:
    """Return the exact JSON-compatible task identity persisted in task.json."""

    serialized = json.loads(json.dumps(asdict(task), default=str))
    if not isinstance(serialized, dict):  # pragma: no cover - dataclass invariant
        raise TypeError("serialized TaskSpec was not an object")
    return cast(dict[str, Any], serialized)


def _default_runs_dir(task: TaskSpec) -> Path:
    return task.target_repo / ".atlas-dispatch" / "runs" / task.id


def _expected_review_deliverable_path(
    task: TaskSpec,
    *,
    workspace: Path,
) -> Path | None:
    """Resolve the Gemini reviewer's expected review file from agreeing spec declarations."""
    if task.cli != "gemini":
        return None

    raw_output_path = task.extra_prompt_vars.get("review_output_path")
    if not raw_output_path:
        return None

    declared_output_path = Path(raw_output_path).expanduser()
    if declared_output_path.is_absolute():
        return None

    resolved_workspace = workspace.resolve()
    expected_path = (resolved_workspace / declared_output_path).resolve()
    if not expected_path.is_relative_to(resolved_workspace):
        return None

    acceptance_paths: list[Path] = []
    for acceptance_command in task.acceptance:
        try:
            command = shlex.split(acceptance_command)
        except ValueError:
            continue
        if len(command) != 3 or command[:2] != ["test", "-f"]:
            continue
        accepted_path = Path(command[2]).expanduser()
        if accepted_path.is_absolute():
            return None
        resolved_accepted_path = (resolved_workspace / accepted_path).resolve()
        if not resolved_accepted_path.is_relative_to(resolved_workspace):
            return None
        acceptance_paths.append(resolved_accepted_path)

    if not acceptance_paths or any(
        accepted_path != expected_path for accepted_path in acceptance_paths
    ):
        return None

    return expected_path
