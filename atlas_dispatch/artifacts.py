"""Low-level run-directory I/O shared by the run records and the reports.

Critical files are written atomically (temp file, fsync, rename). The
changed-files artifact distinguishes an observed empty diff from a diff that
was never observed, so a crash cannot read as "the agent changed nothing".
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

CHANGED_FILES_UNKNOWN = "UNKNOWN"
CHANGED_FILES_UNKNOWN_PREFIX = f"{CHANGED_FILES_UNKNOWN}: "
POST_RUN_INCOMPLETE_MARKER = "post_run_incomplete"
POST_RUN_INCOMPLETE_CLASSIFICATION = "post_run_incomplete"
POST_RUN_INCOMPLETE_VERIFICATION_STATE = "POST_RUN_INCOMPLETE"
POST_RUN_HEARTBEAT_INTERVAL_SECONDS = 15.0
POST_RUN_HEARTBEAT_ARTIFACT = "post_run.json"


@dataclass(frozen=True, kw_only=True)
class ChangedFilesEvidence:
    """What this run actually learned about its changed-file set."""

    state: str
    files: tuple[str, ...] = ()
    reason: str = ""


def _write_text_atomic(path: Path, content: str) -> None:
    """Replace one critical artifact atomically and flush it before returning."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_text_atomic(path, json.dumps(dict(payload), indent=2))


def _write_text_artifact(path: Path, content: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("could not write artifact %s: %s", path, exc)


def _read_text_artifact(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object artifact; a missing, unreadable or non-object file is ``{}``."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _normalized_changed_files_unknown_reason(reason: str) -> str:
    normalized = " ".join(str(reason).split())
    return normalized[:1000] or "changed-file discovery did not run"


def _unknown_changed_files_evidence(reason: str) -> ChangedFilesEvidence:
    return ChangedFilesEvidence(
        state="unknown",
        reason=_normalized_changed_files_unknown_reason(reason),
    )


def _write_changed_files_evidence(
    run_dir: Path,
    evidence: ChangedFilesEvidence,
) -> None:
    if evidence.state == "unknown":
        content = CHANGED_FILES_UNKNOWN_PREFIX + evidence.reason
    elif evidence.state in {"empty", "observed"}:
        content = "\n".join(evidence.files)
    else:  # pragma: no cover - internal invariant
        raise ValueError(f"unsupported changed-files state: {evidence.state}")
    (run_dir / "changed_files.txt").write_text(content, encoding="utf-8")


def _known_changed_files_evidence(changed: list[str]) -> ChangedFilesEvidence:
    return ChangedFilesEvidence(
        state="observed" if changed else "empty",
        files=tuple(changed),
    )


def _read_changed_files_evidence(path: Path) -> ChangedFilesEvidence:
    if not path.is_file():
        return _unknown_changed_files_evidence(
            f"changed_files_artifact_missing: {path}"
        )
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _unknown_changed_files_evidence(
            "changed_files_artifact_unreadable: "
            f"{type(exc).__name__}: {exc}"
        )
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        return ChangedFilesEvidence(state="empty")
    if len(lines) == 1 and lines[0] == CHANGED_FILES_UNKNOWN:
        return _unknown_changed_files_evidence(
            "unknown marker did not include a reason"
        )
    if len(lines) == 1 and lines[0].startswith(CHANGED_FILES_UNKNOWN_PREFIX):
        return _unknown_changed_files_evidence(
            lines[0].removeprefix(CHANGED_FILES_UNKNOWN_PREFIX)
        )
    if any(
        line == CHANGED_FILES_UNKNOWN
        or line.startswith(CHANGED_FILES_UNKNOWN_PREFIX)
        for line in lines
    ):
        return _unknown_changed_files_evidence(
            "changed_files_artifact_mixed_unknown_marker_with_paths"
        )
    return ChangedFilesEvidence(state="observed", files=tuple(lines))


def _set_changed_files_summary(
    summary: dict[str, Any],
    evidence: ChangedFilesEvidence,
) -> None:
    summary["changed_files_state"] = evidence.state
    if evidence.state == "unknown":
        summary["changed_files"] = CHANGED_FILES_UNKNOWN
        summary["changed_files_unknown_reason"] = evidence.reason
    else:
        summary["changed_files"] = list(evidence.files)
        summary.pop("changed_files_unknown_reason", None)


def _changed_files_report_lines(evidence: ChangedFilesEvidence) -> list[str]:
    if evidence.state == "unknown":
        return [f"**UNKNOWN** — {evidence.reason}"]
    return [f"- `{path}`" for path in evidence.files] or ["(none)"]
