"""Safe git subprocess helpers for unattended runs.

Every git call gets a watchdog timeout and an environment that can never
block on an interactive credential prompt.
"""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

DEFAULT_GIT_TIMEOUT_SECONDS = 45
GIT_BIN_ENV = "ATLAS_GIT_BIN"
GIT_TIMEOUT_ENV = "ATLAS_DISPATCH_GIT_TIMEOUT_SECONDS"
PREDISPATCH_GIT_INDEX_LOCK_RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5)
PREDISPATCH_GIT_LOCK_CONTENTION_KIND = "git_lock_contention"
PREDISPATCH_GIT_LOCK_CONTENTION_SUGGESTED_ACTION = (
    "Transient pre-dispatch git index lock contention; retry or redispatch."
)
_SUBPROCESS_RUN = subprocess.run
_INDEX_LOCK_RE = re.compile(
    r"(?:^|[/\\])index\.lock\b|another git process seems to be running|could not lock index",
    re.IGNORECASE,
)
_PREDISPATCH_REPO_LOCKS_GUARD = threading.Lock()
_PREDISPATCH_REPO_LOCKS: dict[Path, threading.Lock] = {}


def git_timeout_seconds() -> float:
    """Return the git subprocess watchdog timeout in seconds."""

    raw = os.environ.get(GIT_TIMEOUT_ENV, "").strip()
    if not raw:
        return float(DEFAULT_GIT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_GIT_TIMEOUT_SECONDS)
    if value <= 0:
        return float(DEFAULT_GIT_TIMEOUT_SECONDS)
    return value


def noninteractive_git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for git calls that must never prompt (no TTY, no GUI)."""

    env = dict(os.environ if base is None else base)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "/usr/bin/true"
    env["SSH_ASKPASS"] = "/usr/bin/true"
    env["GCM_INTERACTIVE"] = "never"
    return env


def run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = False,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git with noninteractive credential behavior and a timeout watchdog."""

    run = runner or subprocess.run
    effective_env = noninteractive_git_env(env)
    git_binary = _resolve_git_binary(effective_env)
    # Credential helpers are left as the user configured them; the environment
    # above only guarantees that no helper or askpass program can block on an
    # interactive prompt in an unattended run.
    command = ["git", *args]
    effective_timeout = git_timeout_seconds() if timeout is None else timeout
    kwargs = {
        "cwd": cwd,
        "check": check,
        "capture_output": capture_output,
        "text": text,
        "timeout": effective_timeout,
        "env": effective_env,
        "executable": git_binary,
    }
    try:
        return run(command, **kwargs)
    except subprocess.TimeoutExpired as exc:
        stdout = _timeout_stream_to_text(exc.stdout)
        stderr = _timeout_stream_to_text(exc.stderr)
        timeout_stderr = (
            f"git command timed out after {effective_timeout} seconds: "
            f"{' '.join(command)}"
        )
        if stderr:
            timeout_stderr = f"{stderr}\n{timeout_stderr}"
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=stdout,
            stderr=timeout_stderr,
        )
    except TypeError as exc:
        # Some test doubles for subprocess.run accept only a narrow set of
        # kwargs. Retry only those patched runners; real git subprocesses
        # always receive the hardened env and timeout.
        if run is _SUBPROCESS_RUN or "unexpected keyword argument" not in str(exc):
            raise
        legacy_kwargs = dict(kwargs)
        legacy_kwargs.pop("timeout", None)
        legacy_kwargs.pop("env", None)
        legacy_kwargs.pop("executable", None)
        try:
            return run(command, **legacy_kwargs)
        except subprocess.TimeoutExpired as timeout_exc:
            stdout = _timeout_stream_to_text(timeout_exc.stdout)
            stderr = _timeout_stream_to_text(timeout_exc.stderr)
            timeout_stderr = (
                f"git command timed out after {effective_timeout} seconds: "
                f"{' '.join(command)}"
            )
            if stderr:
                timeout_stderr = f"{stderr}\n{timeout_stderr}"
            return subprocess.CompletedProcess(
                command,
                124,
                stdout=stdout,
                stderr=timeout_stderr,
            )


def _resolve_git_binary(env: Mapping[str, str]) -> str:
    if GIT_BIN_ENV in env:
        override = env[GIT_BIN_ENV]
        override_path = Path(override)
        if not override_path.is_file():
            raise RuntimeError(
                f"{GIT_BIN_ENV} is set to {override!r}, but that file does not exist"
            )
        if not os.access(override_path, os.X_OK):
            raise RuntimeError(
                f"{GIT_BIN_ENV} is set to {override!r}, but that file is not executable"
            )
        return override
    if sys.platform == "darwin" and Path("/usr/bin/git").is_file():
        return "/usr/bin/git"
    return "git"


def git_result_is_index_lock_contention(
    result: subprocess.CompletedProcess[str],
) -> bool:
    """Return True when git failed on the repository index lock."""

    if result.returncode == 0:
        return False
    output = "\n".join(
        str(part)
        for part in (result.stderr, result.stdout)
        if part is not None
    )
    return bool(_INDEX_LOCK_RE.search(output))


@contextmanager
def pre_dispatch_repo_lock(target_repo: Path) -> Iterator[Path]:
    lock_path = pre_dispatch_repo_lock_path(target_repo)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    process_lock = _pre_dispatch_in_process_lock(lock_path)
    with process_lock:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield lock_path
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def pre_dispatch_repo_lock_path(target_repo: Path) -> Path:
    repo_root = target_repo.resolve()
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        return dot_git / "atlas-dispatch-pre-dispatch-sync.lock"
    return repo_root.parent / f".{repo_root.name}.atlas-dispatch-pre-dispatch-sync.lock"


def run_pre_dispatch_git_with_index_lock_retry(
    git: Callable[[list[str]], subprocess.CompletedProcess[str]],
    args: list[str],
    *,
    sleep_fn: Callable[[float], None],
    retry_delays: tuple[float, ...] = PREDISPATCH_GIT_INDEX_LOCK_RETRY_DELAYS_SECONDS,
) -> subprocess.CompletedProcess[str]:
    for attempt_index in range(len(retry_delays) + 1):
        result = git(args)
        if not git_result_is_index_lock_contention(result):
            return result
        if attempt_index >= len(retry_delays):
            return result
        sleep_fn(retry_delays[attempt_index])
    return result


def _pre_dispatch_in_process_lock(lock_path: Path) -> threading.Lock:
    with _PREDISPATCH_REPO_LOCKS_GUARD:
        lock = _PREDISPATCH_REPO_LOCKS.get(lock_path)
        if lock is None:
            lock = threading.Lock()
            _PREDISPATCH_REPO_LOCKS[lock_path] = lock
        return lock


def _timeout_stream_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)
