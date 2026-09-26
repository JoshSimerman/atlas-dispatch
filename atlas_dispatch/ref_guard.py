"""The orchestrator ref guard (ADR-003).

Every worktree shares the repository's ``.git``, so an agent in a worktree can
move the orchestrator's ``main`` without changing a single file the path
checks can see. The guard snapshots the protected refs before and after the
CLI runs and reports any movement, or any local ref it could not read.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atlas_dispatch.artifacts import _write_json_atomic
from atlas_dispatch.git_exec import run_git_in

ORCHESTRATOR_PROTECTED_REFS: tuple[str, ...] = ("main", "HEAD")

_ORCHESTRATOR_REF_REVISIONS = {
    "main": "refs/heads/main",
    "HEAD": "HEAD",
}

_ORCHESTRATOR_REMOTE_REF_REVISIONS = {
    "origin/main": "refs/heads/main",
}

ORCHESTRATOR_OBSERVED_REMOTE_REFS: tuple[str, ...] = tuple(
    _ORCHESTRATOR_REMOTE_REF_REVISIONS
)


def _capture_orchestrator_refs(repo: Path) -> dict[str, Any]:
    """Snapshot the refs a dispatched agent must never move.

    A worktree is a FILESYSTEM boundary, not a git-ref boundary: every worktree
    shares one ``.git``, so an agent running with full-access flags can move the
    orchestrator's ``main`` -- and ``allowed_paths`` cannot see it, because no
    file in the orchestrator's checkout changed. See ADR-003 in
    docs/DESIGN_DECISIONS.md.

    Capture failures are recorded in the snapshots. Unreadable local protected
    refs fail closed; an unavailable remote observation is surfaced without
    making network availability a prerequisite for local dispatches. A change
    from resolved to unresolved (or the reverse) is still a mismatch.
    """
    refs: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for ref in ORCHESTRATOR_PROTECTED_REFS:
        revision = _ORCHESTRATOR_REF_REVISIONS[ref]
        result = run_git_in(
            repo,
            ["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            refs[ref] = sha
        elif (
            ref != "HEAD"
            and result.returncode == 1
            and not result.stdout.strip()
            and not result.stderr.strip()
        ):
            # `rev-parse --verify --quiet` exits 1 with no output when the ref
            # does not exist. That is a readable fact (a repository whose base
            # branch is not called `main`), not an unreadable ref. It is still
            # compared across snapshots, so the branch appearing or
            # disappearing during the run is a finding.
            refs[ref] = None
        else:
            refs[ref] = None
            errors[ref] = " ".join(
                (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"git rev-parse exited {result.returncode}"
                ).split()
            )[:200]

    for remote_ref, advertised_ref in _ORCHESTRATOR_REMOTE_REF_REVISIONS.items():
        result = run_git_in(repo, ["ls-remote", "--refs", "origin", advertised_ref])
        if result.returncode == 0:
            advertised = [line.split() for line in result.stdout.splitlines()]
            refs[remote_ref] = next(
                (
                    parts[0]
                    for parts in advertised
                    if len(parts) == 2 and parts[1] == advertised_ref
                ),
                None,
            )
        else:
            refs[remote_ref] = None
            errors[remote_ref] = " ".join(
                (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or f"git ls-remote exited {result.returncode}"
                ).split()
            )[:200]

    return {"refs": refs, "errors": errors}


def _diff_orchestrator_refs(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[str]:
    """Name every moved ref and every unreadable local protected ref."""
    findings: list[str] = []
    before_refs = dict(before.get("refs") or {})
    after_refs = dict(after.get("refs") or {})
    for ref in (*ORCHESTRATOR_PROTECTED_REFS, *ORCHESTRATOR_OBSERVED_REMOTE_REFS):
        was, now = before_refs.get(ref), after_refs.get(ref)
        if was != now:
            # SAY WHAT IS OBSERVED, NOT WHO DID IT. This guard compares two
            # snapshots; it cannot distinguish the dispatched agent from the
            # orchestrator committing on main during the run, and in practice
            # both happen. A finding that names the wrong culprit is how a
            # correct alarm gets dismissed.
            findings.append(
                f"{ref} moved during this run: {was or 'unresolved'} "
                f"-> {now or 'unresolved'} (the dispatched agent or the "
                f"orchestrator; a worktree isolates files, not refs)"
            )
    for label, snapshot in (("before", before), ("after", after)):
        errors = dict(snapshot.get("errors") or {})
        for ref in ORCHESTRATOR_PROTECTED_REFS:
            if error := errors.get(ref):
                findings.append(f"could not record {ref} {label} dispatch: {error}")
    return findings


def _orchestrator_ref_report_lines(
    guard: Mapping[str, object] | None,
) -> list[str]:
    """Render before/after ref evidence even when the values are equal."""
    lines = ["", "## Orchestrator refs", ""]
    if guard is None:
        lines.extend(
            [
                "- Ref guard passed: **False**",
                "- Before/after state was not recorded.",
                "",
            ]
        )
        return lines

    before = guard.get("before")
    after = guard.get("after")
    before_refs = _reported_orchestrator_ref_values(before)
    after_refs = _reported_orchestrator_ref_values(after)
    passed_value = guard.get("passed", guard.get("clean"))
    passed = passed_value is True
    lines.append(f"- Ref guard passed: **{passed}**")
    for ref in (*ORCHESTRATOR_PROTECTED_REFS, *ORCHESTRATOR_OBSERVED_REMOTE_REFS):
        lines.append(f"- {ref} before: `{before_refs.get(ref) or 'unresolved'}`")
        lines.append(f"- {ref} after: `{after_refs.get(ref) or 'unresolved'}`")

    for label, snapshot in (("before", before), ("after", after)):
        if not isinstance(snapshot, Mapping):
            continue
        for ref, error in dict(snapshot.get("errors") or {}).items():
            lines.append(f"- {ref} {label} error: `{error}`")

    findings = [str(item) for item in list(guard.get("findings") or [])]
    if findings:
        lines.append("- **A REF OUTSIDE THE WORKTREE MOVED OR COULD NOT BE READ:**")
        lines.extend(f"  - {finding}" for finding in findings)
        lines.append(
            "  - A worktree isolates files, not git refs. Review before "
            "trusting this run."
        )
    else:
        lines.append("- Findings: none")
    lines.append("")
    return lines


def _reported_orchestrator_ref_values(snapshot: object) -> dict[str, object]:
    """Read the ``refs`` map out of one ref-guard snapshot."""
    if not isinstance(snapshot, Mapping):
        return {}
    refs = snapshot.get("refs")
    if isinstance(refs, Mapping):
        return dict(refs)
    return {}


def _record_orchestrator_ref_guard(
    *,
    run_dir: Path,
    repo: Path,
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, object]:
    """Compare two snapshots, write ``orchestrator_refs.json`` and announce the result."""

    findings = _diff_orchestrator_refs(before, after)
    guard: dict[str, object] = {
        "schema_version": 2,
        "repo": str(repo),
        "protected_refs": list(ORCHESTRATOR_PROTECTED_REFS),
        "observed_remote_refs": list(ORCHESTRATOR_OBSERVED_REMOTE_REFS),
        "before": before,
        "after": after,
        "findings": findings,
        "passed": not findings,
        # Same value as `passed`. Schema-version-1 records carry only `clean`,
        # so readers accept either.
        "clean": not findings,
    }
    _write_json_atomic(run_dir / "orchestrator_refs.json", guard)
    if findings:
        # Loud on stdout: a detector whose output only lands in a file nobody
        # opens is not detection.
        print(
            "[atlas-dispatch] ORCHESTRATOR REF GUARD FAILED -- "
            "a protected ref moved or could not be read:"
        )
        for finding in findings:
            print(f"[atlas-dispatch]     {finding}")
        print(
            "[atlas-dispatch] Review before trusting this run; see "
            "orchestrator_refs.json."
        )
    else:
        print("[atlas-dispatch] orchestrator refs unchanged during dispatch")
    return guard
