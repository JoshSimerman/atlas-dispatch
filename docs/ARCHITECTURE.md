# Architecture

This is a design overview. For how to run it, see the [README](../README.md) and
[USAGE.md](USAGE.md); for every spec field,
see [SPEC_REFERENCE.md](SPEC_REFERENCE.md); for the reasoning behind the shape, see
[DESIGN_DECISIONS.md](DESIGN_DECISIONS.md).

## Roles

- **Orchestrator** (the caller): an AI agent, a script or a person. It writes the task spec, calls
  `atlas-dispatch run`, reads the report, and decides what happens next: merge, re-dispatch, send a
  review task, or throw the branch away.
- **Implementer**: the coding-agent CLI session atlas-dispatch starts inside the task's worktree. It
  sees only the rendered prompt and the worktree.
- **The harness** (atlas-dispatch): everything in between. It never merges and never decides.

## Components

```
atlas_dispatch/
├── cli.py               console entry point: argument parsing, `clis`, `models`, `capabilities`
├── dispatcher.py        dispatch(): sequences the pipeline below, runaway caps, duplicate reuse
│
│   inputs
├── task_spec.py         TaskSpec, load_task: validation, path and model resolution
├── prompt.py            render_prompt: placeholders, bounded context-file reads
├── mcp_config.py        per-CLI MCP server configuration written into the worktree
│
│   git and the worktree
├── base_sync.py         fast-forward the base branch before the worktree is cut
├── worktree.py          create/reuse worktrees, branch rescue, auto-commit, changed files,
│                        per-worktree venv, branch publication with remote containment proof
├── worktree_state.py    worktree HEAD facts: no-change detection, verified HEAD
├── ref_guard.py         snapshot and compare the orchestrator's refs (ADR-003)
├── git_exec.py          every git call: watchdog timeout, non-interactive environment
│
│   running the CLI and checking the result
├── adapter.py           CLIS and MODELS registries, command rendering, the subprocess
│                        adapter (plain, Codex JSONL, Kimi Wire), env sanitising, classify_result
├── codex_lifecycle.py   parser and state machine for `codex exec --json` events
├── verify.py            allowlist measurement, protected paths, acceptance runner,
│                        pytest denominator parsing, collection floors
├── import_provenance.py probe that acceptance imports the worktree's packages, not a stale install
│
│   run records
├── run_history.py       run directories, the runaway caps' inputs, duplicate-run lookup
├── run_records.py       cli.summary.json phases, post-run heartbeat, combined diff
├── run_outcomes.py      complete records for refused, reused and failed attempts
├── reporting.py         report.md and the three-state verdict
├── artifacts.py         atomic writes and the changed-files evidence format
│
├── doctor.py            read-only environment report
└── fake_agent.py        deterministic stand-in agent behind the `fake` CLI (quickstart, tests)
```

Roughly 12,500 lines of Python in the package and 13,000 in the tests. `dispatcher` only sequences
the pipeline; each step lives in its own module. `adapter` is the CLI abstraction; `worktree`,
`verify` and `git_exec` are leaves. There are no runtime dependencies.

How the modules, the agent CLI, the target repository and the run directory fit together:

```mermaid
flowchart TB
    O(["Orchestrator: agent, script or person"])

    subgraph PKG["atlas_dispatch package"]
        direction LR
        CLI["cli.py"] --> D["dispatcher.py<br/>sequences the pipeline"]
        subgraph IN["Inputs"]
            TS["task_spec.py<br/>prompt.py<br/>mcp_config.py"]
        end
        subgraph RT["CLI runtime"]
            direction TB
            A["adapter.py<br/>registries, run_cli,<br/>classify_result"] --> CL["codex_lifecycle.py"]
        end
        subgraph CHK["Checks"]
            direction TB
            V["verify.py<br/>paths + acceptance"] --> IP["import_provenance.py"]
        end
        subgraph GT["Git"]
            direction TB
            W["base_sync.py<br/>worktree.py<br/>worktree_state.py<br/>ref_guard.py"] --> GX["git_exec.py<br/>git watchdog"]
        end
        subgraph REC["Run records"]
            RR["run_history.py<br/>run_records.py<br/>run_outcomes.py<br/>reporting.py<br/>artifacts.py"]
        end
        D --> TS
        D --> A
        D --> V
        D --> W
        D --> RR
    end

    AG["Agent CLI<br/>codex, claude, agy,<br/>kimi, grok, hermes, fake"]
    REPO[("Target repo + worktrees<br/>shared .git")]
    RUNS[/"run dir<br/>report.md, cli.summary.json"/]

    O -->|"run spec.json"| CLI
    A -->|"spawn, scrubbed env"| AG
    AG -->|"edits worktree"| REPO
    GX --> REPO
    RR -->|writes| RUNS
    RUNS -.->|"reads verdict"| O

    classDef ext fill:#fff4d6,stroke:#b8860b,color:#1a1a1a
    classDef core fill:#e3eefc,stroke:#2f6fba,color:#1a1a1a
    class O,AG,REPO,RUNS ext
    class D,A core
```

## Core types

The public API (`from atlas_dispatch import ...`) is built around a handful of frozen dataclasses. A
new runtime only has to implement the `Adapter` protocol and return an `AdapterResult`; classification
and everything downstream are unchanged.

```mermaid
classDiagram
    direction LR
    class TaskSpec {
        +str id
        +str title
        +Path target_repo
        +str cli
        +str worktree_branch
        +str base_ref
        +list allowed_paths
        +list acceptance
        +int timeout_seconds
    }
    class ModelDefinition {
        +str name
        +str cli
        +str model_id
        +str reasoning_effort
    }
    class CLIDefinition {
        +str executable
        +list argv_template
        +bool reads_prompt_from_stdin
        +str adapter_mode
        +tuple env_allowlist
        +str auth_setup_hint
    }
    class Adapter {
        <<Protocol>>
        +run(prompt, cwd, ...) AdapterResult
    }
    class SubprocessAdapter
    class AdapterResult {
        +int exit_code
        +str stdout
        +str stderr
        +bool timed_out
        +str final_turn_status
    }
    class Classification {
        +DispatchErrorKind kind
        +str suggested_action
        +str matched_pattern
    }
    class Worktree {
        +Path repo_root
        +Path worktree_path
        +str branch
    }
    class VerifyReport {
        +bool changed_files_present
        +list protected_path_violations
        +list command_results
        +acceptance_outcome
        +passed
    }
    class CheckResult {
        +str name
        +bool passed
        +str classification
    }
    ModelDefinition --> CLIDefinition : cli names
    TaskSpec ..> ModelDefinition : model resolves via MODELS
    Adapter <|.. SubprocessAdapter
    SubprocessAdapter ..> CLIDefinition : renders argv
    SubprocessAdapter --> AdapterResult : returns
    AdapterResult *-- Classification
    TaskSpec ..> Worktree : create_worktree
    VerifyReport *-- "0..*" CheckResult
```

## Guards

Every guard below exists in the code and has tests. The [README](../README.md#what-it-checks) shows
them as a pipeline: the first seven run before or around the CLI, the rest on the committed result.

| Guard | What it does | Where |
|---|---|---|
| Env allowlist | The CLI gets locale, proxy, TLS and identity variables, its own declared keys, a rebuilt `PATH`, and isolation settings (`GIT_CONFIG_GLOBAL=/dev/null`, no terminal prompts, `PYTHONNOUSERSITE=1`). Nothing else. | `adapter._prepare_cli_subprocess_environment` |
| Secret redaction | Values of secret-looking allowlisted keys are replaced with `<redacted-env:KEY>` in captured output; credential-bearing argv values are redacted before they are written. `run-identity.json` records key names, never values. | `adapter._finalize_cli_subprocess_result`, `run_records._sanitize_command_for_artifact` |
| Ref guard | Snapshots `main`, `HEAD` and the remote's `main` before and after the CLI. Any movement, or an unreadable local ref, fails verification even when acceptance passed. | `ref_guard._capture_orchestrator_refs` |
| Protected paths | Changed files matching `configs/prod.*`, `migrations/**`, `secrets/**` or `.github/workflows/**` (configurable only from the harness environment, never from a spec) fail verification. | `verify.check_protected_paths` |
| Allowlist | `allowed_paths` / `forbidden_paths` are measured and reported, deliberately **not** enforced. | `verify.check_allowlist` |
| Import provenance | Before a pytest-bearing command, probes that first-party packages import from the worktree, not a stale editable install. Fails closed. | `import_provenance.py` |
| Acceptance | At least one command must run and every one must pass; a per-command timeout kills the whole process group. | `verify.run_acceptance_commands` |
| Collection floor | `acceptance_min_collected` fails a pytest run that collected fewer tests than declared. | `verify._parse_pytest_counts` |
| Runaway caps | Refuses a task id with 100 run directories, or 3 runs of the same task and base in 5 minutes. Fails closed if history is unreadable. | `dispatcher._check_runaway_caps`, `run_history` |
| Branch rescue | Reusing a branch with commits that exist nowhere else saves them under `refs/rescue/...` and refuses the reset unless the spec sets `allow_destructive_branch_reset`. | `worktree._reset_existing_branch_for_reuse` |

## The pipeline, in code order

`dispatch(spec_path)` in `dispatcher.py`, with the module that does each step:

1. **Load** the spec (`task_spec.load_task`). Path fields resolve relative to the spec file; a single-segment
   `target_repo` resolves against the code roots (`ATLAS_DISPATCH_CODE_ROOTS`, default `~/code`). `model` resolves through `MODELS` to a CLI, a
   model id and an effort. Malformed fields raise before anything else happens.
2. **Render the prompt** (`prompt.render_prompt`). Built-in variables plus `extra_prompt_vars` are
   substituted; context files are inlined in fences sized so their own backticks cannot close them.
   A placeholder left in the *template* is a hard error.
3. **Take the per-task lock** (an `fcntl` lock beside the runs directory) and apply the
   **runaway caps**: an absolute count of run directories, then a time-window count of consecutive
   runs (`run_history`). Either refuses with a complete run record and no CLI invocation
   (`run_outcomes`).
4. **Duplicate-run check.** If a prior run for the same task already verified, with an identical
   serialised task, identical rendered prompt and the same base commit, the harness records a new
   attempt satisfied by that run and does not invoke the CLI (`run_history`, `run_outcomes`).
5. **Sync the base.** For `base_ref: main`, check out and fast-forward local `main` from its remote
   (`--ff-only`; divergence is an error). Other refs resolve to a local or remote-tracking ref
   (`base_sync`).
6. **Create the worktree** (`worktree.create_worktree`) at `<repo>/../<repo>-wt-<branch-slug>-<hash>`. A
   stale clean worktree is removed first; a dirty one is refused. A reused branch is reset to
   `base_ref`; if it holds commits that exist nowhere else, they are first saved under
   `refs/rescue/...` and the reset is refused unless the spec sets
   `allow_destructive_branch_reset`. If the repository has a `.venv`, the worktree gets its own
   isolated venv (built with `uv`) with the worktree's packages installed editable.
7. **Prepare MCP config** for the CLI, if the spec declares servers (`mcp_config`).
8. **Record `HEAD` of the worktree and snapshot the protected refs** (`main`, `HEAD` and the
   advertised `origin/main`) (`worktree_state`, `ref_guard`).
9. **Run the CLI** (`adapter.run_cli`) in the worktree with a sanitised environment and the task timeout.
10. **Snapshot the refs again and diff.** Any movement, or an unreadable local ref, is a finding
    (`ref_guard`).
11. **Reclassify no-change.** A CLI "success" whose worktree `HEAD` did not move and which left no
    uncommitted work becomes `no_change` (`worktree_state`).
12. **Persist the CLI result** as a provisional record (`post_run_incomplete`) so a crash from here
    on cannot be mistaken for a verdict (`run_records`).
13. **Auto-commit** everything left uncommitted: `git add -A` and `<cli>(<id>): <title>`.
14. **List changed files** with `git diff --name-only --no-renames base...HEAD`.
15. **Publish** the branch if `push_branch` is set, before verification, so a crash during a long
    test suite cannot lose the work.
16. **Verify** under a post-run heartbeat: measure the allowlist, check protected paths, and, if the
    CLI succeeded and files changed, run acceptance (with the import-provenance probe for
    pytest-bearing commands).
17. **Write** `combined-<sha>.diff`, the final `cli.summary.json` (`run_records`) and `report.md`
    (`reporting`).
18. **Return** 0 only if the CLI succeeded and verification passed, including the ref guard.

The worktree and branch are left in place whatever the outcome. If any step raises, `run_outcomes`
still writes a complete record for the attempt (`dispatch_exception`, or `post_run_incomplete` if
verification had started).

```mermaid
sequenceDiagram
    autonumber
    participant O as Orchestrator
    participant D as dispatcher
    participant W as worktree / git
    participant A as adapter
    participant C as coding CLI
    participant V as verify

    O->>D: dispatch(spec.json)
    D->>D: load_task, render_prompt
    D->>D: runaway caps, duplicate-run check
    D->>W: fast-forward base, create_worktree
    D->>W: snapshot refs (main, HEAD, origin/main)
    D->>A: run_cli(prompt, cwd=worktree, timeout)
    A->>C: spawn with sanitised env, prompt on stdin or argv
    C-->>A: stdout, stderr, exit code (or JSONL events)
    A->>A: classify_result
    A-->>D: AdapterResult + Classification
    D->>W: snapshot refs again, diff them
    D->>W: commit_all, list_changed_files
    D->>V: check_allowlist, check_protected_paths
    D->>V: run_acceptance_commands (if CLI ok and files changed)
    V-->>D: VerifyReport
    D->>D: write cli.summary.json, report.md
    D-->>O: exit code 0 / 1
```

### The branch model

Each task gets its own branch and worktree, cut from `base_ref`. The harness auto-commits what the
agent left as `<cli>(<task-id>): <title>`. A cross-model review is a second spec whose `base_ref` is
the implementer's branch. Merging is always the orchestrator's move, outside the harness.

```mermaid
gitGraph
    commit id: "base"
    branch codex/ex-001-http-retry
    checkout codex/ex-001-http-retry
    commit id: "agent commit (optional)"
    commit id: "codex(EX-001) auto-commit"
    branch kimi/ex-002-review
    checkout kimi/ex-002-review
    commit id: "kimi(EX-002) review file"
    checkout main
    commit id: "other work on main"
    merge codex/ex-001-http-retry id: "orchestrator merges" type: HIGHLIGHT
```

## The CLI registry

`adapter.CLIS` maps a short name to a `CLIDefinition`:

| Field | Purpose |
|---|---|
| `executable`, `argv_template` | How to invoke the CLI non-interactively with full access. Placeholders: `{cwd}`, `{model}`, `{reasoning_effort}`, `{prompt}`. |
| `reads_prompt_from_stdin` | Whether the prompt goes on stdin or into argv (`{prompt}`). Sending it both ways breaks some CLIs, so it is one or the other. |
| `extra_auth_patterns`, `extra_rate_limit_patterns`, `extra_quota_exhausted_patterns`, `extra_overloaded_patterns`, `extra_refusal_patterns` | Regexes layered on the generic lists in `classify_result`. |
| `model_selection_error_patterns` | Exact exit-0 output meaning "that model does not exist for you". |
| `extra_post_completion_timeout_patterns` | Output that counts as success *only* if the run also produced its declared deliverable. |
| `auth_setup_hint` | The suggested action for `auth_required`. |
| `env_allowlist`, `credentials_file`, `credentials_env_map` | Exactly which environment variables may reach this CLI, and an optional dotenv file to lift credentials from. |
| `adapter_mode` | `subprocess` or `kimi_wire` (JSON-RPC over stdio). |

Registered: `codex`, `claude`, `gemini` (the Antigravity `agy` binary), `kimi-code`, `grok`,
`hermes`, plus the legacy `kimi` and streaming `kimi-streaming` definitions, and `fake`, the
bundled stand-in agent that needs no account. Run
`atlas-dispatch capabilities <cli>` to see any one of them.

**Three runtime paths.** Most CLIs run through `_run_subprocess_command`, which streams output to
temporary files (so a long-running caller does not hold megabytes in memory) and enforces the
timeout. `codex exec --json` runs through `_run_codex_json_command`, which parses lifecycle events
as they arrive: it can tell a stalled turn (no events for `idle_timeout_seconds`) from a slow one,
and it terminates a run that asks for approval, since nobody is there to give it. Kimi's Wire mode
speaks JSON-RPC and auto-answers approval requests.

The Codex turn state kept by `codex_lifecycle.apply_codex_event` as JSONL events arrive. The
adapter terminates the process itself on a stall or an approval request; the final status then
feeds `classify_result`, where an approval request or an overload error (`-32001`) takes
precedence over the turn status.

```mermaid
stateDiagram-v2
    direction LR
    [*] --> NoTurn
    NoTurn --> inProgress: turn.started
    inProgress --> inProgress: item.started / item.completed
    inProgress --> completed: turn.completed
    inProgress --> failed: turn.failed or error event
    inProgress --> interrupted: turn.interrupted
    inProgress --> stalled: no event for idle_timeout_seconds
    completed --> [*]: success
    failed --> [*]: failed
    interrupted --> [*]: interrupted
    stalled --> [*]: stalled, process terminated
```

**The environment is an allowlist.** The CLI subprocess gets locale, proxy, TLS and identity
variables, its own `env_allowlist`, a rebuilt `PATH`, and a fixed set of isolation variables
(`GIT_CONFIG_GLOBAL=/dev/null`, no terminal prompts, no pager, `PYTHONNOUSERSITE=1`). Values of
secret-looking allowlisted keys are redacted from captured output. `run-identity.json` records which keys were
passed, never their values.

## The model registry

`adapter.MODELS` maps a logical name such as `codex/gpt-6-sol-high` to a `ModelDefinition(cli,
model_id, reasoning_effort, description)`. A spec that says `"model": "codex/gpt-6-sol-high"` gets
the CLI, the `-m` value and the effort without the author knowing any flags.

Every run records **review-model evidence**: the model that actually ran, derived from the rendered
command rather than from the name that was asked for. If the command carries no observable model
selection, the evidence says `unknown` instead of trusting the request. That matters for
cross-model review, where the whole point is that the reviewer is a different model family from the
builder (see DESIGN_DECISIONS, ADR-006).

## Extension points

**Add a model.** Add a `ModelDefinition` to `MODELS`. Run one real task through it and read the
report: an unregistered or inaccessible model id usually surfaces as `model_selection_error` or
`exit_nonzero`, not silently.

**Add a CLI.**

1. Try it without code first: `ATLAS_DISPATCH_MYCLI_CMD="mycli --yes --model {model} -"` plus
   `"cli": "mycli"` in a spec. An unregistered CLI runs through the plain subprocess path with the
   generic failure patterns and no CLI-specific environment keys.
2. When it earns a place, add a `CLIDefinition` to `CLIS`: its non-interactive, full-access argv;
   whether it reads the prompt from stdin; the environment keys it needs; and an auth hint.
3. Add failure patterns only from real output. Capture the CLI's actual stderr for a rate limit, an
   expired login and a refusal, add the narrowest regex that matches, and pin it with a test that
   uses the captured text verbatim (see `tests/test_classifier.py`).

**Add a runtime.** `adapter.Adapter` is a `Protocol` with one method,
`run(*, prompt, cwd, ...) -> AdapterResult`. A non-subprocess runtime (an SDK call, an app-server)
only has to return an `AdapterResult`; classification and everything downstream are unchanged.

**Add a task source.** `dispatch()` takes a path to a spec. A queue consumer, a CI job or another
agent can write a spec and call it; nothing in the harness assumes who the caller is.

## Run artifacts

`<target_repo>/.atlas-dispatch/runs/<task-id>/<YYYYMMDDTHHMMSSZ>[-NNN]/`

| File | Contents |
|---|---|
| `prompt.md` | The exact prompt the CLI received |
| `task.json` | The resolved task spec |
| `cli.stdout.txt`, `cli.stderr.txt` | Raw CLI output (secret values redacted) |
| `cli.summary.json` | The machine-readable record: classification, changed files, acceptance results, ref guard, post-run state, verdict |
| `report.md` | The human-readable report |
| `changed_files.txt` | One path per line, or an explicit `UNKNOWN: <reason>` |
| `combined-<sha>.diff` | `git diff base...HEAD` of the verified head |
| `orchestrator_refs.json` | Ref snapshots before and after, and findings |
| `post_run.json` | Heartbeat for the verification phase, so a killed run is distinguishable from a slow one |
| `run-identity.json` | Environment policy applied to the CLI: keys, executable path and hash |
| `branch_publish.json` | Only when `push_branch` is set |
| `import_provenance.json` | Only when the provenance probe ran |
| `rescue_refs.json`, `consecutive_dispatch_cap_refusal.json` | Only on those paths |

`report.md` is for people; `cli.summary.json` carries the same facts for machines. The diagram shows
how a spec relates to its runs and the main records inside `cli.summary.json` (field names as written
by the code; not every field is shown).

```mermaid
erDiagram
    TASK_SPEC ||--o{ RUN_DIR : "one per attempt"
    RUN_DIR ||--|| CLI_SUMMARY : "cli.summary.json"
    RUN_DIR ||--|| REPORT : "report.md"
    RUN_DIR ||--o| POST_RUN : "post_run.json"
    CLI_SUMMARY ||--|| CLASSIFICATION : classification
    CLI_SUMMARY ||--|| REF_GUARD : orchestrator_ref_guard
    CLI_SUMMARY ||--|| RUN_IDENTITY : run_identity
    CLI_SUMMARY ||--|| MODEL_EVIDENCE : review_model_evidence
    CLI_SUMMARY ||--o{ ACCEPTANCE_CMD : acceptance_commands

    TASK_SPEC {
        string id PK
        string title
        string model
        string worktree_branch
        string base_ref
    }
    RUN_DIR {
        string timestamp PK "YYYYMMDDTHHMMSSZ"
    }
    CLI_SUMMARY {
        int exit_code
        bool timed_out
        bool verify_passed
        string acceptance_outcome
        string verified_head_sha
        string source_base_sha
    }
    CLASSIFICATION {
        string kind
        string suggested_action
        string matched_pattern
    }
    REF_GUARD {
        json before
        json after
        list findings
        bool passed
    }
    RUN_IDENTITY {
        list selected_env_keys
        json executable
        json isolation
    }
    MODEL_EVIDENCE {
        string effective_cli
        string effective_model
        string requested_model
    }
    ACCEPTANCE_CMD {
        string name
        bool passed
        string classification
        json pytest_counts
    }
    POST_RUN {
        string state
        string last_heartbeat_at
    }
```

When the ref guard fires (quickstart `T-002`), the report says what moved and does not guess who moved
it:

```markdown
- Ref guard passed: **False**
- **A REF OUTSIDE THE WORKTREE MOVED OR COULD NOT BE READ:**
  - main moved during this run: <sha before> -> <sha after> (the dispatched agent or the
    orchestrator; a worktree isolates files, not refs)
```

Critical files are written atomically (temp file, `fsync`, rename). Artifacts are
machine-specific (absolute paths; the error recorded for a refused branch reset names the
hostname), so `.atlas-dispatch/` belongs in the target repository's `.gitignore`.

## Concurrency

- One `fcntl` lock per task id serialises attempts at the same task, so the runaway caps and
  duplicate-run check see completed runs.
- One lock per repository serialises the base-branch sync.
- One lock per repository (in the shared `.git`) covers worktree creation.
- Different tasks on the same repository run in parallel, each in its own worktree. Two dispatches
  of the *same branch* at the same time are the caller's responsibility to avoid.

## What is deliberately not here

- **Merging.** See ADR-002.
- **Scheduling, queues, retries.** The orchestrator decides; the report tells it what happened and
  suggests what to do.
- **Remote execution.** Everything runs on the machine that calls `dispatch()`.
- **Structured-output parsing across CLIs.** CLIs do not agree on an error format, so classification
  reads text (plus Codex's lifecycle events where they exist).
