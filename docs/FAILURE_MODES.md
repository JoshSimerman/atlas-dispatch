# Failure modes

Every run ends with a classification and, when it is not `success`, a suggested next action. This
page lists every value the code can produce, where it comes from, and what triggers it. The source of
truth is `classify_result()` in `atlas_dispatch/adapter.py`, `DispatchErrorKind` in the same file,
and the dispatcher paths named below.

There are three layers, and a run reports all three:

1. **CLI classification** (`classification.kind` in `cli.summary.json`): what the CLI process did.
2. **Acceptance classification** (per command, `acceptance_commands[].classification`): what each
   acceptance command did.
3. **Verification verdict** (`Verification:` in `report.md`): whether the run produced evidence
   about the code, and what that evidence says.

## 1. CLI classification

`classify_result()` checks conditions **in this order** and returns the first match. The order is
the design: a timeout that also printed "rate limit" is a timeout, and a non-zero exit that mentions
a 401 is an auth problem, not a generic failure.

"Output" below means stderr and stdout together, searched case-insensitively and tail-biased to the
last 256 KB (errors cluster at the end, and lower-casing a multi-megabyte string doubles memory).

| # | Kind | Trigger | Suggested action (from the code) |
|---|---|---|---|
| 1 | `approval_blocked` | Codex JSONL: the turn emitted an approval-required event. The harness terminates the run; nobody is there to approve. | Inspect `turn_lifecycle` in `cli.summary.json`; adjust Codex approval/sandbox settings and rerun. |
| 2 | `stalled` | Codex JSONL: the turn was in progress with no lifecycle event for `idle_timeout_seconds` (default 900). | Retry, or split the task if it repeatedly stalls. |
| 3 | `overloaded` | Codex reported error code `-32001` on a turn that did not complete cleanly. | Wait and retry. |
| 4 | `quota_exhausted` | A failed turn's `error_info.message` matches the CLI's quota patterns. Records the vendor's reset window when the message states one. | Wait for the quota reset, or route to a CLI backed by a different quota pool. |
| 5 | `rate_limited` | A failed turn's `error_info.message` matches a rate-limit pattern. (Codex reports a hard usage stop only here; stdout and stderr are empty.) | Wait for the reset, switch CLI/model, or split the work. |
| 6 | `quota_exhausted` | Output matches a quota pattern. For exit 0 the banner must be the whole final line, so a successful run that merely *quotes* the message is not misread. | As above. |
| 7 | `failed` | Codex final turn status `failed`. | Inspect `error_info` and retry once the model/runtime issue is resolved. |
| 8 | `interrupted` | Codex final turn status `interrupted`. | Retry unless someone intentionally interrupted it. |
| 9 | `timeout` | The run exceeded `timeout_seconds`; the process was terminated. | Raise `timeout_seconds` or split the task. |
| 10 | `executable_not_found` | The executable could not be started (exit code `-2`). | Install the CLI or set `ATLAS_DISPATCH_<CLI>_CMD`. |
| 11 | `auth_required` | Non-zero exit and output matches an auth pattern (`401`, `not logged in`, `invalid api key`, plus per-CLI wording). | The CLI's own `auth_setup_hint`, e.g. "Run `codex login`". |
| 12 | `rate_limited` | Non-zero exit and output matches `429`, `rate limit`, `too many requests`, `throttled`, `quota exceeded`, `usage limit`, or per-CLI wording. | Wait and retry, switch CLI/model, or split the work. |
| 13 | `overloaded` | Non-zero exit and output matches `503`, `service unavailable`, `overloaded`, `server busy`, or per-CLI wording. | Wait a few minutes and retry; if persistent, try another CLI. |
| 14 | `refused_out_of_scope` | Stdout (any exit code) states that the required change is outside the allowed scope. The pattern needs an inability to complete, a required change, and a causal link to the scope boundary; "I won't touch files outside scope" alone does not match. | The builder stopped at the boundary; its reason is quoted. Widen the spec or split the task. |
| 15 | `refused` | Refusal phrasing ("I can't help with that") in the output; for exit 0, in stdout only. Not checked for a Codex turn that completed. | Reword, narrow the scope, or hand to another CLI/model. |
| 16 | `success` | Non-zero exit, but the output matches the CLI's post-completion-timeout pattern **and** the run demonstrably produced its declared deliverable (the file's hash changed). The raw exit code is kept as `process_exit_code`. | None. |
| 17 | `exit_nonzero` | Any other non-zero exit. | Read `cli.stderr.txt`. |
| 18 | `model_selection_error` | Exit 0, and stdout is exactly the CLI's "this model does not exist or you lack access" message. | Correct the model id or choose another model. |
| 19 | `success` | Codex final turn status `completed`. | None. |
| 20 | `no_output` | Exit 0 with empty stdout. | Check that the prompt was received and the CLI's print/quiet flags. |
| 21 | `success` | Anything else with exit 0. | None. |

`DispatchErrorKind` also defines `unknown_failure`; no current code path returns it.

### Reclassified by the dispatcher

| Kind | Where | Trigger | Suggested action |
|---|---|---|---|
| `no_change` | `_should_classify_no_change` | The CLI classified `success`, exited 0, left no uncommitted work, and the worktree's `HEAD` is the same commit it was before the CLI ran. | Verify auth, read `cli.stdout.txt` for the model's own explanation, then redispatch. |

`no_change` compares `HEAD` before and after the run, not `base_ref..HEAD`. An agent that merges its
own branch (or a base that moves under a long run) leaves `base_ref..HEAD` at zero with real work
done; calling that `no_change` would suggest a retry, and a retry resets the branch.

### Runs that never reach the CLI

| Kind | Where | Trigger | Suggested action |
|---|---|---|---|
| `consecutive_dispatch_cap_reached` | `_dispatch_loaded_task` | The task id already has `ATLAS_DISPATCH_ABSOLUTE_RUN_CAP` run directories (default 100), or 3 runs of the same task and base in 5 minutes, or the run history cannot be read at all. | Find out why the caller keeps redispatching; set `ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH=1` for one intentional run. |
| `success` with `run_reuse.reused: true` | `_write_duplicate_satisfied_run` | A prior run already verified with the identical serialised task, rendered prompt and base commit. | None; the prior run's verdict is copied. Use `--no-reuse`, `reuse_policy: "never"` or `ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN=1` to force a fresh run. |
| `git_lock_contention` | `_pre_dispatch_sync_base_ref` | Git's index lock stayed held through three short retries while syncing the base. Marked `transient`. | Retry or redispatch. |
| `dispatch_exception` | `_write_dispatch_exception_run` | Anything else that raised: a base that diverged from its remote, a dirty stale worktree, a branch with unrescued commits, a bad placeholder. The run record says whether the CLI had been invoked, and checks the remote for a published branch so work is not reported as lost when it exists. | Fix the error in `dispatch_error` and redispatch. |
| `post_run_incomplete` | `_persist_cli_result`, `_persist_post_run_incomplete` | The CLI finished but the verification phase did not complete (crash, kill). This is the provisional state written before verification begins. | The branch was preserved; re-run acceptance on that head. Do not read it as a failed verification. |

## 2. Acceptance classification

Per command, from `verify.py`. Precedence, highest first: timeout, stale import binding, collection
floor, then the exit status.

| Classification | Trigger |
|---|---|
| `success` | Exit 0 (and the collection floor, if declared, is met). |
| `acceptance_failed` | Non-zero exit. |
| `acceptance_timeout` | Exceeded `acceptance_timeout_seconds` (default 3600). The whole process group is killed. |
| `acceptance_collection_shrank` | `acceptance_min_collected` is declared and pytest collected fewer tests, or the output did not let the harness establish a count at all. A suite that silently shrank to three tests passes, and this is what catches it. |
| `stale_import_binding` | The import-provenance probe found a package importing from outside the worktree (a stale editable install). The command is not run. |
| `unknown` | The provenance probe could not decide (no packages discovered, interpreter unresolvable). Unknown is not a pass, so the command is not run. Set `check_import_provenance: false` for repositories without an installable package layout. |

## 3. Verification verdict

`verification_verdict()` in `dispatcher.py`. `verify_passed` in `cli.summary.json` stays a boolean
for machines; the report always says which of these it means.

| Verdict | When | `verify_passed` |
|---|---|---|
| `VERIFIED_PASS` | Acceptance ran and every command passed, files changed, no protected path touched, ref guard clean. | `true` |
| `VERIFIED_FAIL` | Acceptance ran and a command failed; or it passed but a protected path was touched; or it passed but a ref outside the worktree moved. | `false` |
| `NOT_ATTEMPTED` | Acceptance never ran (CLI failed, nothing changed, no acceptance configured, or the provenance probe refused). The report says **"not a verdict"**. | `false` |

`NOT_ATTEMPTED` is deliberately distinct from `VERIFIED_FAIL`. A run killed at its timeout halfway
through a test suite has produced no evidence that the code is wrong, and the report must not invite
anyone to throw it away. Within `NOT_ATTEMPTED`, the reason also separates "nothing was built" from
"something was built and nobody checked it", and from "the diff is empty because a ref outside the
worktree moved", where the work may have been merged elsewhere.

## Adding a pattern

Patterns are added from real output, one failure at a time:

1. Capture the CLI's actual stdout and stderr from the failing run (they are in the run directory).
2. Add the narrowest regex that matches to that CLI's `extra_*_patterns`. Prefer anchoring on the
   vendor's error prefix over a bare keyword; `quota` alone matches too much prose.
3. Add a test in `tests/test_classifier.py` that feeds the captured text verbatim, plus a
   must-not-fire case where a successful run quotes the same words.
