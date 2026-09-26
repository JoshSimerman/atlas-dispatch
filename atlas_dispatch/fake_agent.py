"""A deterministic stand-in for a coding-agent CLI, for demos and tests.

atlas-dispatch pipes the rendered prompt to a CLI's stdin and runs it inside
the task's worktree. This script does the same job a real agent would do, but
it takes its instructions literally from directives embedded in the prompt, so
the whole pipeline can be exercised without an API key or a network.

Directives (each on its own line in the prompt):

    FAKE-AGENT WRITE <relative/path>    write the following lines to <path>,
    ...file content...                  up to the matching END line
    FAKE-AGENT END

    FAKE-AGENT MOVE-MAIN                point the repository's `main` at this
                                        worktree's HEAD (what a misbehaving
                                        agent does); the ref guard catches it
    FAKE-AGENT RATE-LIMIT               print a 429 and exit 1
    FAKE-AGENT REFUSE                   print a refusal and exit 0

It is registered as the `fake` CLI (model `fake/scripted`), so a spec only
needs `"model": "fake/scripted"`. It can also be run directly:

    python -m atlas_dispatch.fake_agent < prompt.md
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    prompt = sys.stdin.read()
    lines = prompt.splitlines()
    written: list[str] = []
    move_main = False

    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line == "FAKE-AGENT RATE-LIMIT":
            print("Error: 429 Too Many Requests: rate limit reached", file=sys.stderr)
            return 1
        if line == "FAKE-AGENT REFUSE":
            print("I can't help with that request.")
            return 0
        if line == "FAKE-AGENT MOVE-MAIN":
            move_main = True
        if line.startswith("FAKE-AGENT WRITE "):
            relative = line.removeprefix("FAKE-AGENT WRITE ").strip()
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() != "FAKE-AGENT END":
                body.append(lines[index])
                index += 1
            target = Path.cwd() / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(body) + "\n", encoding="utf-8")
            written.append(relative)
        index += 1

    if move_main:
        # Commit first so HEAD differs from main, then move main to it. A real
        # agent that "helpfully" merges its own branch has the same effect.
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fake-agent: self-merged work"],
            check=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "update-ref", "refs/heads/main", head], check=True)

    print("fake-agent summary:")
    for path in written:
        print(f"- wrote {path}")
    if move_main:
        print("- moved main to this worktree's HEAD")
    if not written and not move_main:
        print("- no changes made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
