# Design decisions

Architecture decision records for atlas-dispatch. Each states the context, the decision, what it
cost, and what I would revisit. Where a decision came out of something that went wrong in use, the
entry says what went wrong in general terms; the tests named alongside pin the behaviour so it cannot
quietly regress.

## Background

atlas-dispatch was built to let one orchestrating agent hand bounded implementation work to several
frontier coding CLIs (Codex, Claude Code, Gemini, Kimi, Grok, Hermes) and then decide, from a report,
what to do with the result. The orchestrator's context window is the scarce resource; the harness
exists so that it can delegate without having to re-read every diff to find out whether anything
happened at all.

Three observations shaped almost everything below:

1. **An agent's own account of its work is not evidence.** Exit codes, "done!" summaries and green
   output from a test run in the wrong environment all turned out to be wrong in practice, in both
   directions.
2. **Agents are capable and literal.** Given full access they will do what it takes to finish,
   including things nobody asked for, like merging their own branch.
3. **The expensive mistake is the reader's.** A report that makes a good change look failed gets the
   change thrown away; one that makes a bad change look verified gets it merged.

---

## ADR-001: The worktree is the boundary; the CLI runs with full access inside it

**Context.** Coding CLIs offer permission modes and sandboxes. In unattended mode those modes either
block the agent from running tests (it cannot ask anyone for approval) or they are bypassed anyway.
A reviewer that cannot execute code cannot verify anything by effect. One CLI's "accept edits" mode
still gated shell commands, so a review agent on it could not create a virtualenv or run a test, and
could only report that it had executed nothing.

**Decision.** Every registered CLI runs non-interactively with its full-access flag
(`--dangerously-bypass-approvals-and-sandbox`, `--dangerously-skip-permissions` and equivalents). The
safety boundary is instead:

- a fresh git worktree and branch per task, created from an explicit `base_ref`;
- an optional per-worktree virtualenv, so editable installs cannot leak between the parent checkout
  and the worktree;
- an **environment allowlist**: the CLI subprocess receives only locale, proxy, TLS and identity
  variables, its own declared keys, a rebuilt `PATH`, and isolation settings
  (`GIT_CONFIG_GLOBAL=/dev/null`, no terminal prompts). Secret values are redacted from captured
  output, and `run-identity.json` records the key names that were passed, never their values;
- verification after the fact (ADR-003, ADR-004, ADR-007).

A test pins the set of CLIs with full-access flags, so adding a restricted one is a deliberate act
(`tests/test_adapter.py`).

**Consequences.** Agents can install, build and test, which is what makes their output checkable.

**Trade-off, stated plainly.** This is not a sandbox. An agent with full access can read anything the
user can read and can make network calls. The harness contains the *diff*, not the process. Run it as
a user and on a machine whose blast radius you accept, or inside a container you provide.

---

## ADR-002: The harness never merges

**Context.** The point of the report is that a decision gets made on evidence. If the harness merged
on green, the evidence would be consumed by the same component that produced it.

**Decision.** No code path in the harness merges task work or moves the target repository's `main`
to include it. The branch and worktree are left in place whatever the outcome; merging, re-dispatching
or discarding is the orchestrator's call. Two related choices:

- **Base sync is a fast-forward only.** Before creating the worktree, the harness fast-forwards the
  local base branch to its remote (`merge --ff-only`) so work starts from current code. If local and
  remote have diverged, dispatch refuses rather than choosing a side.
- **Publication is preservation, not approval.** With `push_branch`, the branch is pushed *before*
  verification runs, and pushed even if verification later fails. Gating the push on a green run
  inverts the risk: the work most likely to be lost (red, partial) is exactly the work that would not
  be preserved, and a cross-model reviewer, which starts from `origin/<branch>`, could not start at
  all. Publication never overwrites: a rejected push goes to the next free `<branch>-runN`, and the
  harness proves by fetching into a disposable repository that the remote ref really contains the
  built commit.

**Trade-off.** An orchestrator that wants auto-merge has to build it, deliberately, on top of the
report. That is the intended friction.

---

## ADR-003: Worktrees isolate files, not refs, so there is a ref guard

**Context.** Every worktree shares the repository's `.git`. An agent working in a worktree can run
`git checkout main && git merge` or `git update-ref refs/heads/main ...`, and the orchestrator's
`main` moves. No file in the orchestrator's checkout changes, so `allowed_paths` and every other
file-based check see nothing. An implementer with full-access flags can merge and push from inside
its worktree, because worktrees share refs; if merges auto-deploy, that ships unreviewed code. This
is not hypothetical: capable agents do it when a prompt so much as suggests finishing the job end
to end.

It also breaks the diff. After the agent merges its branch into `main`, `main...HEAD` is empty, so
the run looks like it changed nothing.

**Decision.** Snapshot `main`, `HEAD` and the remote's advertised `refs/heads/main` immediately before
and after the CLI runs, in the orchestrator's checkout. Any movement, or a local ref that cannot be
read, is a finding and fails verification even when acceptance passed. Findings are printed to stdout
as well as written to `orchestrator_refs.json` and the report, because a detector whose output lands
only in a file nobody opens is not detection.

The finding says **what moved, not who moved it**. The guard compares two snapshots; it cannot tell
the agent from an orchestrator that committed to `main` during the run, and both happen. An
accusation the evidence does not support is how a correct alarm gets dismissed.

A related rule: `no_change` is decided by whether the worktree's `HEAD` moved during the run, not by
`base_ref..HEAD`. An empty range next to a moved ref means "possibly merged elsewhere", and the
report says exactly that instead of "nothing was built".

Tests: `tests/test_orchestrator_ref_guard.py`, `tests/test_no_change_head_moved.py`, and the
quickstart's `T-002`, where the fake agent moves `main` and the guard catches it.

**Limits.**

- It **detects**; it does not prevent and does not roll back. Restoring refs automatically would hide
  what happened before anyone had seen it. Prevention would need the agent to run without write
  access to the shared `.git` (a separate clone, or a container), which costs the cheapness that makes
  worktrees attractive.
- It watches `main`, `HEAD` and `origin/main` by name. In a repository with no `main` branch,
  `main` is recorded as absent (a readable fact, unlike a ref git cannot read, which fails closed),
  so protection comes from `HEAD` alone. Making the watched refs follow `base_ref` and
  `remote_name` is the obvious next change.
- The remote observation needs network access; if the remote is unreachable, that half of the guard
  is recorded as unavailable rather than failing the run.

---

## ADR-004: Acceptance must exist and pass; "not checked" is not "failed"

**Context.** "Exit 0" was the first thing that proved not to mean success: CLIs exit 0 after
refusing, after hitting a quota they reported only in a JSON event, after printing nothing, and
after changing nothing. The only evidence that counts is a command the orchestrator chose, run by
the harness, on the committed result.

**Decision.**

- A run verifies only if files changed, **at least one** acceptance command ran, and **every** one
  passed. An empty `acceptance` list is not a pass; it is reported as `no_acceptance_configured`, and
  `verify_passed` stays false. Not gated is not passed.
- Acceptance runs only if the CLI succeeded and something changed, inside the worktree, with a
  per-command timeout that kills the whole process group.
- **Collection floors.** `acceptance_min_collected` fails a pytest command that collected fewer tests
  than declared, or whose output does not establish a count. A suite that silently shrinks still
  exits 0.
- **Import provenance.** Before a pytest-bearing command runs, a probe imports each first-party
  package with the same interpreter and environment the command will use, and refuses if any resolves
  outside the worktree. A stale editable install otherwise lets acceptance test the parent checkout
  while `cwd` and `git HEAD` both say you are on the branch.
- **Three verdicts, not a boolean.** `verify_passed=False` used to mean both "the code failed" and
  "nobody checked". The report now always says which: `VERIFIED_PASS`, `VERIFIED_FAIL` or
  `NOT_ATTEMPTED`, and `NOT_ATTEMPTED` is labelled "not a verdict". A build killed at its timeout
  halfway through a long suite has produced no evidence that it is wrong, and should not be read as
  "failed, start over". The boolean is unchanged for machine consumers.

**Trade-off.** The provenance probe fails closed, including for repositories it cannot understand
(a flat layout with no installable package). That is correct by default and annoying in practice, so
a spec can set `check_import_provenance: false`. Collection floors only understand pytest output.

---

## ADR-005: Auto-commit with a deterministic message

**Context.** Some CLIs commit, some do not, some commit part of their work. Verification must run
against something immutable and nameable, and the orchestrator must be able to find the result.

**Decision.** After the CLI exits, the harness runs `git add -A` and commits anything uncommitted as
`<cli>(<task-id>): <title>`. The commit it verifies is resolved to a full SHA (`verified_head_sha`),
and the combined diff is written as `combined-<short-sha>.diff`. Harness-owned files (MCP config,
Codex home) are added to the worktree's git exclude list so they never enter the diff.

**Consequences.** Every run leaves a branch whose last commit is attributable to one task and one
CLI, and `git log --grep '(EX-001)'` finds it.

**Trade-off.** `git add -A` commits everything the agent left behind, including scratch files. That
is deliberate (the reviewer should see what the agent did), and it is why the surface report
(ADR-007) exists.

---

## ADR-006: Cross-model review is a spec, not a feature

**Context.** A second model reading the diff catches defects the builder cannot see, mostly because
it does not share the builder's assumptions. That only holds if it is genuinely a different model.

**Decision.** Review is the same pipeline pointed the other way: a spec whose `base_ref` is the
implementer's branch, whose model is from a different family, whose prompt asks for findings only,
and whose only allowed output (and acceptance check) is a review file. See
`examples/tasks/EX-002-review.kimi.json` and `examples/prompts/review.md`. The prompt asks the reviewer
to record the exact commit it read, to reject tests that mock the function under test, and to say
what it did **not** check.

Every run records **review-model evidence**: which CLI and model actually ran, derived from the
rendered command rather than the requested name. This exists because one CLI silently substitutes a
model from a different family whenever a `--model` flag is passed. A "Gemini" review would then be
produced by the builder's own family, and every artifact would still look healthy. The adapter never
passes that flag, and if one appears in the command, the evidence reads `unknown` instead of the
requested name. A test forces the flag in to prove the guard can go red
(`tests/test_adapter.py`).

**Trade-off.** Independence is only as good as the registry's knowledge of model families; the
harness records what ran but does not judge whether two models are "different enough".

---

## ADR-007: A protected-paths floor; the allowlist is informational

**Context.** Specs predict which files a task will touch (`allowed_paths`, `forbidden_paths`).
Originally a miss failed the run. In practice the prediction was wrong far more often than the agent
was: a correct fix needed a test helper, an `__init__.py`, a fixture. Gating on the prediction blocked
correct work and taught everyone to write `**`, at which point the check protected nothing.

Some paths, though, must never be changed by a dispatched agent, whatever the spec says: production
configuration, migrations, secrets, and the CI definitions that decide what "green" means.

**Decision.** Two separate mechanisms:

- `allowed_paths` and `forbidden_paths` are **measured and reported**, not enforced: the report shows
  every out-of-scope and forbidden file, and a "surface" section summarising unpredicted directories.
  The prompt also shows them to the agent as the expected surface. A reviewer reads them; the harness
  does not block on them.
- **Protected paths** are a hard floor. Any changed file matching a protected glob fails verification
  regardless of acceptance. The list is configured in the harness environment
  (`ATLAS_DISPATCH_PROTECTED_PATTERNS`), never in the spec, so the author of a spec, possibly another
  agent, cannot lower it. Defaults are illustrative: `configs/prod.*`, `migrations/**`, `secrets/**`,
  `.github/workflows/**`.
- Changed files are listed with `git diff --no-renames`. Git's default rename detection records only
  the new path, so renaming a protected file with a small edit would otherwise escape the floor. A
  test builds a real high-similarity rename and guards its own fixture (`tests/test_worktree.py`).
- Globs are matched with `*` not crossing `/`, `**` crossing it, and everything else literal, so
  `src/app/[id]/route.ts` (a Next.js route) matches itself instead of being read as a character class.

**Trade-off.** There is no per-task hard deny list; if you need one, it has to be a protected path
for every task. The floor is a path check, not a content check: an agent can still do damage through
a path that is not on the list.

---

## ADR-008: Failure classification is ordered, pattern-based, and measured

**Context.** The CLIs do not agree on an error format. Some print JSON, most print prose, and the same
condition (a quota stop) arrives on stderr from one CLI and only inside a JSON lifecycle event from
another.

**Decision.** `classify_result()` reads the text (plus Codex's lifecycle events where they exist) and
checks conditions in a fixed order: lifecycle states, quota and rate evidence, timeout, missing
executable, auth, rate limit, overload, scope refusal, refusal, non-zero exit, model selection, no
output, success. Each classification carries a concrete suggested action. The full table is in
[FAILURE_MODES.md](FAILURE_MODES.md).

"Measured" means every CLI-specific pattern was added from a real failure's captured output, and the
test that pins it uses that text verbatim, alongside a must-not-fire case (for example, a successful
run that quotes the vendor's quota banner in prose must not be classified as exhausted). Some rules
exist because a wrong classification caused a wrong action:

- A hard usage stop that surfaced only in `error_info` was once classified as a generic failure, with
  the advice "retry after the model issue is resolved". The right advice is "wait for the quota reset
  or use a different CLI".
- `no_change` once meant `base_ref..HEAD == 0`, which is also what a self-merged success looks like.
  The suggested retry would have reset the branch. It now means "HEAD did not move" (ADR-003).
- One CLI can finish its work, write the requested file, and then time out waiting to print. That is
  `success` only if the harness independently observed the deliverable change; the raw exit code is
  kept.

**Trade-off.** Patterns lag vendors. New wording lands as `exit_nonzero` (with the stderr to read)
until someone adds a pattern. I prefer that to a classifier that guesses: an honest `exit_nonzero` costs
a read; a confident wrong class costs a wrong action.

---

## ADR-009: Unfilled template placeholders fail loudly

**Context.** The prompt is the whole interface to the implementer. A template slot the spec forgot to
fill (`{{task_summary}}` left literally in the prompt) produces an agent that improvises.

**Decision.** `render_prompt` refuses to dispatch if the **template** contains a `{{name}}` that no
built-in variable or `extra_prompt_vars` entry fills. It deliberately scans the template, not the
rendered prompt: context files are inlined into the prompt, and a source file may legitimately contain
`{{...}}` (a test fixture, a template engine). Scanning values produced hard failures that blamed a
template that was clean. Placeholders inside fenced or inline code in the template are exempt, so a
template can document the syntax.

Values are data: numbers and booleans are coerced, lists and dicts are JSON-encoded, and none of them
is scanned.

---

## ADR-010: Runaway caps that cannot be defeated by the runaway

**Context.** An orchestrator loop that redispatches a failing task will do so forever. A time-window
cap ("no more than three runs in five minutes") is the obvious brake, and it has a subtle weakness: if
run-directory names ever drift ahead of the wall clock (for example, a same-second collision strategy
that bumps the timestamp), a loop faster than one run per second pushes its own history "into the
future", out of the window, and the brake switches itself off.

**Decision.**

- Run directories are named by real UTC time; a same-second collision adds a `-NNN` suffix and never
  changes the timestamp.
- The window cap counts future-dated directories as evidence of a runaway, not as noise.
- An **absolute cap** (default 100 run directories per task id) shares no mechanism with the window
  cap: it counts directories and never parses a timestamp.
- Both fail closed: if the run history cannot be read, dispatch refuses. A refused attempt still gets
  a complete run record, and the refusal is overridable for one intentional run.
- A per-task `fcntl` lock serialises attempts, so a concurrent retry sees the completed run instead
  of racing it, and if that run verified, the duplicate-run check reuses its result without invoking
  the CLI again.

---

## What I would change next

- **Ref guard that follows the spec**: watch `base_ref` and `remote_name` rather than `main` and
  `origin`, and optionally run the agent against a separate clone for real prevention.
- **Structured output where CLIs offer it.** Codex's JSONL lifecycle made its classifications far more
  precise than the text patterns. Adopting stream-JSON modes for the other CLIs would move more of
  the table from patterns to facts.
- **A per-task hard deny list** that composes with the harness floor, for teams that want enforcement
  without making every path a protected path.
- **Windows support.** The locks and process-group handling are POSIX-only.
