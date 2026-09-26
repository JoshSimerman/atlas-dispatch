"""Run directories and what past runs of a task say about the next one.

Two runaway caps (ADR-010) and duplicate-run reuse both read the task's run
history under ``.atlas-dispatch/runs/<task-id>/``. A per-task file lock
serialises attempts so each one sees the runs that finished before it.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from atlas_dispatch.adapter import DispatchErrorKind
from atlas_dispatch.git_exec import run_git_in
from atlas_dispatch.task_spec import TaskSpec, _serialized_task

ALLOW_DUPLICATE_RUN_ENV = "ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN"
ALLOW_CONSECUTIVE_DISPATCH_ENV = "ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH"
CONSECUTIVE_DISPATCH_CAP = 3
CONSECUTIVE_DISPATCH_CAP_REFUSAL_MARKER = "consecutive_dispatch_cap_refusal.json"
# A retry loop that redispatches the same task every few seconds is a runaway.
# Three attempts in five minutes is already that signal, while ordinary
# redispatches spread over hours or days never approach it.
CONSECUTIVE_DISPATCH_WINDOW = dt.timedelta(minutes=5)
# ---------------------------------------------------------------------------
# ABSOLUTE, CLOCK-INDEPENDENT RUNAWAY BACKSTOP
#
# The window cap above is time-based, and a time-based brake can be defeated by
# the very runaway it exists to stop. If run-directory names ever drift ahead of
# the wall clock (for example, a collision strategy that bumps the timestamp in
# the name), a loop faster than one dispatch per second pushes its own history
# "into the future", the window check stops seeing it, and the loop runs
# unbounded.
#
# This cap shares no mechanism with the window cap: it counts directories and
# never parses a timestamp, so no clock, drift or naming anomaly can disable it.
# A single dispatch id legitimately reaching this many runs is itself
# pathological.
ABSOLUTE_DISPATCH_RUN_CAP = int(
    os.environ.get("ATLAS_DISPATCH_ABSOLUTE_RUN_CAP", "100") or "100"
)
LOGGER = logging.getLogger(__name__)


@contextmanager
def _dispatch_runs_lock(runs_root: Path) -> Iterator[None]:
    """Serialize one dispatch id so concurrent attempts observe completed runs."""

    runs_root.mkdir(parents=True, exist_ok=True)
    lock_path = runs_root.parent / f".{runs_root.name}.dispatch.lock"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _total_run_dir_count(runs_root: Path, *, cap: int) -> int:
    """Count every run dir under one dispatch id. No clock, no name parsing.

    Deliberately independent of `_recent_consecutive_dispatch_runs`: a runaway
    can defeat that control through its timestamp inputs, so the backstop must
    not share them. Counts, unlike clocks, cannot drift.
    An unreadable runs_root returns ``cap`` itself, so blindness refuses.
    """

    if not runs_root.exists():
        return 0
    try:
        return sum(1 for path in runs_root.iterdir() if path.is_dir())
    except OSError as exc:
        LOGGER.warning(
            "absolute run cap could not read runs_root %s: %s; treating as "
            "at-cap (fail-closed)",
            runs_root,
            exc,
        )
        return cap


def _recent_consecutive_dispatch_runs(
    *,
    runs_root: Path,
    task: TaskSpec,
    window: dt.timedelta,
    now: dt.datetime | None = None,
) -> list[Path] | None:
    """Return recent consecutive run dirs for this task/base_ref, or None on doubt."""

    if not runs_root.exists():
        # A first dispatch has no history. This is the ONE safe kind of "nothing
        # to see": absence of the directory, not inability to read it.
        return []
    try:
        candidates = sorted(
            (path for path in runs_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError as exc:
        # Genuinely blind: the directory is there and cannot be read, so no
        # statement about runaway history can be made. The caller REFUSES.
        LOGGER.warning(
            "consecutive-dispatch cap could not read runs_root %s: %s",
            runs_root,
            exc,
        )
        return None

    reference_time = now or dt.datetime.now(dt.UTC)
    matches: list[Path] = []
    for candidate in candidates:
        timestamp = _parse_run_dir_timestamp(candidate.name)
        if timestamp is None:
            # SKIP the unreadable directory; do NOT abandon the whole
            # evaluation. Returning None here would let one junk directory
            # switch the cap off entirely.
            LOGGER.warning(
                "consecutive-dispatch cap skipping malformed run directory %s",
                candidate,
            )
            continue
        age = reference_time - timestamp
        if age < -window:
            # A future-dated directory is EVIDENCE OF A RUNAWAY (or a clock
            # fault), never a reason to stop counting. It counts toward the cap.
            LOGGER.warning(
                "consecutive-dispatch cap counting implausibly future run "
                "directory %s as runaway evidence "
                "(reference_time=%s window_seconds=%s)",
                candidate,
                reference_time.isoformat(),
                int(window.total_seconds()),
            )
            matches.append(candidate)
            continue
        if age > window:
            break

        try:
            prior_task = json.loads(
                (candidate / "task.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            LOGGER.warning(
                "consecutive-dispatch cap skipping unreadable task evidence "
                "in %s: %s",
                candidate,
                exc,
            )
            continue
        if not isinstance(prior_task, dict):
            LOGGER.warning(
                "consecutive-dispatch cap skipping non-object task evidence in %s",
                candidate,
            )
            continue

        prior_id = prior_task.get("id")
        prior_base_ref = prior_task.get("base_ref", "main")
        if str(prior_id) == task.id and str(prior_base_ref) == task.base_ref:
            matches.append(candidate)
            continue
        break
    return matches


def _parse_run_dir_timestamp(name: str) -> dt.datetime | None:
    # `-NNN` is the same-second collision suffix written by _new_run_dir; the
    # instant is carried by the part before it.
    base = name.split("-", 1)[0]
    try:
        return dt.datetime.strptime(base, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def _prior_successful_run(
    *,
    runs_root: Path,
    task: TaskSpec,
    source_base_sha: str,
    rendered_prompt: str,
) -> tuple[Path, dict[str, Any]] | None:
    """Return the newest verified run with identical immutable work evidence."""

    try:
        candidates = sorted(
            (path for path in runs_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        return None

    for candidate in candidates:
        try:
            summary = json.loads(
                (candidate / "cli.summary.json").read_text(encoding="utf-8")
            )
            prior_task = json.loads(
                (candidate / "task.json").read_text(encoding="utf-8")
            )
            prior_prompt = (candidate / "prompt.md").read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(summary, dict) or not isinstance(prior_task, dict):
            continue
        classification = summary.get("classification")
        if not isinstance(classification, dict):
            continue
        if classification.get("kind") != DispatchErrorKind.SUCCESS.value:
            continue
        if summary.get("verify_passed") is not True:
            continue
        if summary.get("source_base_sha") != source_base_sha:
            continue
        if prior_task != _serialized_task(task):
            continue
        if prior_prompt != rendered_prompt:
            continue
        return candidate, summary
    return None


def _resolve_source_base_sha(target_repo: Path, base_ref: str) -> str | None:
    """Resolve a source ref to a full commit SHA, or fail open for dispatch."""

    result = run_git_in(
        target_repo,
        ["rev-parse", "--verify", f"{base_ref}^{{commit}}"],
    )
    source_base_sha = result.stdout.strip()
    if (
        result.returncode != 0
        or re.fullmatch(r"[0-9a-fA-F]{40}", source_base_sha) is None
    ):
        return None
    return source_base_sha.lower()


def _new_run_dir(runs_root: Path) -> Path:
    """Create a timestamped run dir without overwriting a same-second attempt.

    A same-second collision adds a `-NNN` SUFFIX; it must never advance the
    timestamp in the NAME. Advancing it would make the directory name a lie
    about when the run happened, and a loop faster than 1/s would drift those
    names arbitrarily far into the future, blinding the time-window cap. The
    name must stay bound to real time.
    """

    timestamp = dt.datetime.now(dt.UTC).replace(microsecond=0)
    base = timestamp.strftime("%Y%m%dT%H%M%SZ")
    attempt = 0
    while True:
        name = base if attempt == 0 else f"{base}-{attempt:03d}"
        run_dir = runs_root / name
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            attempt += 1
            continue
        return run_dir
