"""Console entry point for the ``atlas-dispatch`` command."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from atlas_dispatch.adapter import (
    CLIS,
    MODELS,
    list_supported_clis,
    list_supported_models,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    if args.cmd == "run":
        # Imported here: the dispatcher re-exports ``main`` from this module.
        from atlas_dispatch.dispatcher import dispatch

        return int(dispatch(args.spec, no_reuse=args.no_reuse))
    if args.cmd == "doctor":
        from atlas_dispatch.doctor import run_doctor

        return run_doctor()
    if args.cmd == "models":
        _print_models()
        return 0
    if args.cmd == "clis":
        _print_clis()
        return 0
    if args.cmd == "capabilities":
        return _print_capabilities(args.cli)
    parser.error("unknown command")
    return 2  # unreachable: parser.error exits


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlas-dispatch",
        description=(
            "Dispatch a task spec to a coding-agent CLI in an isolated worktree. "
            "Any orchestrator (an AI agent, a script, or a person at a shell) "
            "can invoke this dispatcher; the dispatched CLI runs in a fresh "
            "session with full-access flags inside a per-task git worktree, and "
            "the harness never merges."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="Run a single task spec")
    run_parser.add_argument(
        "spec",
        type=Path,
        help="Path to a task spec JSON file.",
    )
    run_parser.add_argument(
        "--no-reuse",
        action="store_true",
        help="Require a fresh CLI invocation even when an identical run exists.",
    )

    sub.add_parser("doctor", help="Show CLI availability, git, and protected paths")
    sub.add_parser("clis", help="List supported CLIs (one per line)")
    sub.add_parser("models", help="List registered model identifiers with their CLIs")
    capabilities_parser = sub.add_parser(
        "capabilities",
        help="Show what atlas-dispatch knows about a CLI (argv template, patterns, models)",
    )
    capabilities_parser.add_argument(
        "cli",
        type=str,
        help="CLI name (e.g. codex, claude, gemini, kimi-code, grok, hermes)",
    )
    return parser


def _print_models() -> None:
    for name in list_supported_models():
        definition = MODELS[name]
        print(f"- {name}")
        print(f"    cli: {definition.cli}")
        print(f"    model_id: {definition.model_id}")
        print(f"    reasoning_effort: {definition.reasoning_effort or '(default)'}")
        if definition.description:
            print(f"    description: {definition.description}")


def _print_clis() -> None:
    for name in list_supported_clis():
        definition = CLIS[name]
        print(f"- {name} (executable: {definition.executable})")


def _print_capabilities(cli: str) -> int:
    cli = cli.lower()
    if cli not in CLIS:
        print(f"unknown cli: {cli}", file=sys.stderr)
        return 1

    definition = CLIS[cli]
    models = sorted(m.name for m in MODELS.values() if m.cli == cli)
    models_str = ", ".join(models) if models else "(none)"

    print(f"- cli: {definition.name}")
    print(f"    executable: {definition.executable}")
    print(f"    argv_template: {' '.join(definition.argv_template)}")
    print(f"    auth_setup_hint: {definition.auth_setup_hint}")
    print(
        f"    extra_pattern_counts: "
        f"auth={len(definition.extra_auth_patterns)} "
        f"rate_limit={len(definition.extra_rate_limit_patterns)} "
        f"overloaded={len(definition.extra_overloaded_patterns)} "
        f"refusal={len(definition.extra_refusal_patterns)}"
    )
    print(f"    reads_prompt_from_stdin: {definition.reads_prompt_from_stdin}")
    print(f"    models: {models_str}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
