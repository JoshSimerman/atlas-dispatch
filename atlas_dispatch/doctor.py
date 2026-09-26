"""Environment health checks for ``atlas-dispatch doctor``.

The doctor is read-only: it reports which registered CLIs are on ``PATH``,
which ``ATLAS_DISPATCH_<CLI>_CMD`` overrides are active, the models each CLI
serves, the login hint for each CLI, whether ``git`` is usable, and which
protected-path patterns the verifier will enforce. It never invokes a coding
CLI and never contacts a provider.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping
from typing import TextIO

from atlas_dispatch.adapter import cli_doctor
from atlas_dispatch.verify import protected_patterns


def check_git() -> tuple[bool, str]:
    """Return ``(ok, detail)`` for the ``git`` executable the engine will use."""

    path = shutil.which("git")
    if path is None:
        return False, "git was not found on PATH"
    try:
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"could not run git --version: {exc}"
    if result.returncode != 0:
        return False, f"git --version exited {result.returncode}"
    return True, f"{result.stdout.strip()} ({path})"


def run_doctor(
    *,
    stdout: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Print the doctor report. Returns 1 only when git itself is unusable.

    A missing coding CLI is reported but is not a failure: most installations
    use one or two CLIs, and the report says which ones are available.
    """

    out = stdout or sys.stdout

    print("CLI availability:", file=out)
    for row in cli_doctor(env=environ):
        print(f"- {row['cli']}", file=out)
        for key, value in row.items():
            if key == "cli":
                continue
            print(f"    {key}: {value}", file=out)

    print("", file=out)
    git_ok, git_detail = check_git()
    print(f"git: {'ok' if git_ok else 'MISSING'} - {git_detail}", file=out)

    print("", file=out)
    patterns = protected_patterns(environ)
    print("Protected paths (a change to any of these fails verification):", file=out)
    if patterns:
        for pattern in patterns:
            print(f"- {pattern}", file=out)
    else:
        print("- (none configured)", file=out)

    return 0 if git_ok else 1
