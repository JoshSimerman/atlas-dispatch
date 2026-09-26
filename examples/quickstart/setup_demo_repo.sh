#!/usr/bin/env bash
# Build a throwaway git repository for the atlas-dispatch quickstart.
#
#   examples/quickstart/setup_demo_repo.sh [DEMO_DIR]
#
# DEMO_DIR defaults to ${TMPDIR:-/tmp}/atlas-dispatch-demo. The repository is
# created at DEMO_DIR/calc, and atlas-dispatch will create its per-task
# worktrees next to it (DEMO_DIR/calc-wt-...). Nothing outside DEMO_DIR is
# touched. Re-running the script deletes and recreates DEMO_DIR.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
examples="$(dirname "$here")"
demo_dir="${1:-${TMPDIR:-/tmp}/atlas-dispatch-demo}"
demo_dir="${demo_dir%/}"
repo="$demo_dir/calc"

case "$demo_dir" in
  ""|"/"|"$HOME") echo "refusing to use '$demo_dir' as the demo directory" >&2; exit 2 ;;
esac

rm -rf "$demo_dir"
mkdir -p "$repo/dispatch_tasks" "$repo/dispatch_prompts"
cd "$repo"

git init -q -b main
git config user.name "atlas-dispatch demo"
git config user.email "demo@example.invalid"

cat > calc.py <<'PY'
def add(a, b):
    return a + b
PY

cat > test_calc.py <<'PY'
import unittest

from calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


if __name__ == "__main__":
    unittest.main()
PY

printf '.atlas-dispatch/\n__pycache__/\n' > .gitignore
cp "$examples/prompts/implement.md" dispatch_prompts/implement.md

# T-001: a well-behaved run. The fake agent adds subtract() and a test for it.
cat > dispatch_tasks/T-001-add-subtract.json <<'JSON'
{
  "id": "T-001",
  "title": "Add a subtract function",
  "target_repo": "..",
  "model": "fake/scripted",
  "prompt_template": "../dispatch_prompts/implement.md",
  "worktree_branch": "fake/t-001-subtract",
  "base_ref": "main",
  "allowed_paths": ["calc.py", "test_calc.py"],
  "forbidden_paths": ["dispatch_tasks/**"],
  "acceptance": ["python3 -m unittest -q"],
  "timeout_seconds": 120,
  "extra_prompt_vars": {
    "task_summary": "Add subtract(a, b) to calc.py with a unit test.\n\nFAKE-AGENT WRITE calc.py\ndef add(a, b):\n    return a + b\n\n\ndef subtract(a, b):\n    return a - b\nFAKE-AGENT END\nFAKE-AGENT WRITE test_calc.py\nimport unittest\n\nfrom calc import add, subtract\n\n\nclass CalcTest(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n    def test_subtract(self):\n        self.assertEqual(subtract(5, 3), 2)\n\n\nif __name__ == \"__main__\":\n    unittest.main()\nFAKE-AGENT END",
    "shape_guidance": "Keep add() unchanged."
  }
}
JSON

# T-002: the agent writes the code AND moves main. The worktree isolated the
# files, but not the ref; the ref guard fails the run.
cat > dispatch_tasks/T-002-agent-moves-main.json <<'JSON'
{
  "id": "T-002",
  "title": "Add multiply, then (wrongly) merge it into main",
  "target_repo": "..",
  "model": "fake/scripted",
  "prompt_template": "../dispatch_prompts/implement.md",
  "worktree_branch": "fake/t-002-multiply",
  "base_ref": "main",
  "allowed_paths": ["calc.py"],
  "acceptance": ["python3 -m unittest -q"],
  "timeout_seconds": 120,
  "extra_prompt_vars": {
    "task_summary": "Add multiply(a, b).\n\nFAKE-AGENT WRITE calc.py\ndef add(a, b):\n    return a + b\n\n\ndef multiply(a, b):\n    return a * b\nFAKE-AGENT END\nFAKE-AGENT MOVE-MAIN",
    "shape_guidance": "None."
  }
}
JSON

# T-003: a provider rate limit. Nothing is built; the classifier says why.
cat > dispatch_tasks/T-003-rate-limited.json <<'JSON'
{
  "id": "T-003",
  "title": "A run that hits a provider rate limit",
  "target_repo": "..",
  "model": "fake/scripted",
  "prompt_template": "../dispatch_prompts/implement.md",
  "worktree_branch": "fake/t-003-rate-limited",
  "base_ref": "main",
  "allowed_paths": ["calc.py"],
  "acceptance": ["python3 -m unittest -q"],
  "timeout_seconds": 120,
  "extra_prompt_vars": {
    "task_summary": "FAKE-AGENT RATE-LIMIT",
    "shape_guidance": "None."
  }
}
JSON

# T-004: the agent edits a protected path. Acceptance passes; verify still fails.
cat > dispatch_tasks/T-004-touches-protected-path.json <<'JSON'
{
  "id": "T-004",
  "title": "Add a migration (a protected path)",
  "target_repo": "..",
  "model": "fake/scripted",
  "prompt_template": "../dispatch_prompts/implement.md",
  "worktree_branch": "fake/t-004-migration",
  "base_ref": "main",
  "allowed_paths": ["migrations/**"],
  "acceptance": ["python3 -m unittest -q"],
  "timeout_seconds": 120,
  "extra_prompt_vars": {
    "task_summary": "FAKE-AGENT WRITE migrations/0001_add_index.sql\nCREATE INDEX idx_totals ON totals (created_at);\nFAKE-AGENT END",
    "shape_guidance": "None."
  }
}
JSON

git add -A
git commit -q -m "demo: calc with add()"

cat <<EOF
Demo repository ready: $repo

Dispatch the first task with the bundled stand-in agent (the "fake" CLI):

  atlas-dispatch run $repo/dispatch_tasks/T-001-add-subtract.json

Run reports land in $repo/.atlas-dispatch/runs/<task-id>/<timestamp>/report.md
EOF
