"""Prompt rendering: fill a Markdown template and inline context files.

An unfilled ``{{placeholder}}`` in the template stops the dispatch before any
CLI runs (ADR-009). Context files are read with a timeout so one unreadable
file cannot hang a dispatch.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from atlas_dispatch.task_spec import TaskSpec
from atlas_dispatch.worktree import worktree_path_for

CONTEXT_FILE_READ_TIMEOUT_SECONDS = 5.0


def render_prompt(task: TaskSpec) -> str:
    """Render the prompt template with task variables and inlined context.

    Substitutes `{{name}}` placeholders. Refuses to dispatch a prompt
    with any unfilled placeholder.
    """
    if not task.prompt_template.is_file():
        raise FileNotFoundError(f"prompt template not found: {task.prompt_template}")
    template_text = task.prompt_template.read_text(encoding="utf-8")
    context_block = _build_context_block(task.context_files)

    _worktree_path = worktree_path_for(
        task.target_repo, task.worktree_branch
    ).resolve()

    variables: dict[str, str] = {
        "task_id": task.id,
        "task_title": task.title,
        "worktree_branch": task.worktree_branch,
        "worktree_path": str(_worktree_path),
        "target_repo": str(task.target_repo),
        "cli": task.cli,
        "model": task.model or "",
        "reasoning_effort": task.reasoning_effort or "",
        "base_ref": task.base_ref,
        "allowed_paths_block": "\n".join(f"- {p}" for p in task.allowed_paths)
        or "- (none — task should not modify any file)",
        "forbidden_paths_block": "\n".join(f"- {p}" for p in task.forbidden_paths)
        or "- (no per-task forbidden paths beyond your project rules)",
        "acceptance_block": "\n".join(f"- `{c}`" for c in task.acceptance)
        or "- (no acceptance commands defined; verify will refuse to pass)",
        "context_block": context_block,
    }
    variables.update(task.extra_prompt_vars or {})

    rendered = template_text
    for key, value in variables.items():
        rendered = rendered.replace("{{" + key + "}}", _prompt_variable_text(value))

    if "read timed out after" in context_block and "{{context_block}}" not in template_text:
        rendered += ("\n\n" if rendered else "") + context_block

    # Scan the TEMPLATE, not the rendered prompt.
    #
    # This check exists to catch a half-rendered TEMPLATE: a slot the spec
    # never filled. Scanning `rendered` would also see every substituted VALUE,
    # and a value is data. `context_files` inline whole source files, and a
    # source file (a test fixture, a template engine) may legitimately contain
    # a literal `{{...}}`. Failing on that blames a template that is clean.
    #
    # Exempting fenced code is not enough on its own: an inlined file with an
    # odd number of fences desynchronises fence tracking for everything after
    # it. Not scanning values at all removes that whole class of false alarm.
    declared = set(variables)
    unfilled = [
        placeholder
        for placeholder in _find_unfilled_prompt_placeholders(template_text)
        if placeholder[2:-2] not in declared
    ]
    if unfilled:
        raise ValueError(
            f"prompt template {task.prompt_template} has unfilled placeholders: "
            f"{sorted(set(unfilled))}. Provide them in the task spec's "
            "extra_prompt_vars or remove them from the template."
        )

    return rendered


def _prompt_variable_text(value: object) -> str:
    """Render one prompt variable as substitution text.

    `extra_prompt_vars` comes from task-spec JSON, so a value can be any JSON type
    even though the built-in variables are all strings. Passing a non-str straight
    to `str.replace()` raises `TypeError`, which would abort the dispatch before
    the CLI is ever invoked.

    Numbers are legitimate prompt variables (a retry round, a line budget), so
    coerce rather than reject; containers are JSON-encoded because a Python repr
    is not what a prompt should carry.
    """

    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    return str(value)


def _find_unfilled_prompt_placeholders(rendered: str) -> list[str]:
    unfilled: list[str] = []
    opening_fence_char: str | None = None
    opening_fence_length = 0

    for line in rendered.splitlines():
        stripped_line = line.lstrip()
        fence_match = re.match(r"(`{3,}|~{3,})", stripped_line)
        if opening_fence_char is None and fence_match:
            opening_fence_char = fence_match.group(0)[0]
            opening_fence_length = len(fence_match.group(0))
            continue
        if opening_fence_char is not None:
            if (
                fence_match
                and fence_match.group(0)[0] == opening_fence_char
                and len(fence_match.group(0)) >= opening_fence_length
                and not stripped_line[fence_match.end() :].strip()
            ):
                opening_fence_char = None
                opening_fence_length = 0
            continue

        line_without_inline_code = re.sub(r"`.*?`", "", line)
        unfilled.extend(
            re.findall(
                r"\{\{[a-zA-Z_][a-zA-Z0-9_]*\}\}",
                line_without_inline_code,
            )
        )

    return unfilled


def _build_context_block(context_files: list[Path]) -> str:
    chunks: list[str] = []
    for path in context_files:
        label = str(path)
        try:
            body = _read_context_file_bounded(path)
        except FileNotFoundError:
            chunks.append(f"## {label}\n\n(file not found at {path})")
            continue
        if body is None:
            chunks.append(
                f"## {label}\n\n(context file skipped: read timed out after "
                f"{CONTEXT_FILE_READ_TIMEOUT_SECONDS:g} seconds at {path})"
            )
            continue
        longest_backtick_run = max(
            (len(match.group(0)) for match in re.finditer(r"`+", body)),
            default=0,
        )
        fence = "`" * max(3, longest_backtick_run + 1)
        chunks.append(f"## {label}\n\n{fence}\n{body}\n{fence}")
    return "\n\n".join(chunks)


def _read_context_file_bounded(path: Path) -> str | None:
    outcome: list[str | Exception] = []

    def read() -> None:
        try:
            if not path.is_file() and not path.is_fifo():
                raise FileNotFoundError(path)
            outcome.append(path.read_text(encoding="utf-8"))
        except Exception as exc:
            outcome.append(exc)

    reader = threading.Thread(
        target=read,
        name=f"atlas-context-read:{path.name}",
        daemon=True,
    )
    reader.start()
    reader.join(CONTEXT_FILE_READ_TIMEOUT_SECONDS)
    if reader.is_alive():
        # A read blocked in the kernel (for example on a wedged network
        # filesystem) cannot be cancelled, so this daemon thread may leak.
        # Proceeding is intentional: one bad context file must not hang dispatch.
        return None
    result = outcome[0]
    if isinstance(result, Exception):
        raise result
    return result
