# Task spec reference

A task spec is one JSON object in a file. `load_task()` in `atlas_dispatch/dispatcher.py` is the
source of truth for everything on this page; `atlas-dispatch run <spec.json>` loads it.

Path-valued fields (`target_repo`, `prompt_template`, `context_files`, `runs_dir`) are resolved
**relative to the directory containing the spec file** unless absolute. The convention is to keep
specs in `<repo>/dispatch_tasks/` and prompts in `<repo>/dispatch_prompts/`, so `target_repo` is
`".."`.

## Required fields

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Task identity. Names the run directory (`.atlas-dispatch/runs/<id>/`), appears in the auto-commit message, and scopes the runaway caps and duplicate-run check. |
| `title` | string | One line. Used in the report heading and the commit message `<cli>(<id>): <title>`. |
| `target_repo` | string | The repository to work on; must be the root of a git repository. A relative path that exists wins; otherwise a single-segment value such as `"my-service"` is looked up in each directory of `ATLAS_DISPATCH_CODE_ROOTS` in order (default: `~/code/my-service`). |
| `prompt_template` | string | Markdown template file with `{{placeholders}}` (see below). |
| `worktree_branch` | string | The task branch. Created from `base_ref` if missing; reset to `base_ref` if it exists (see `allow_destructive_branch_reset`). The worktree is created at `<repo>/../<repo>-wt-<slug>-<hash>`, where `<slug>` is the branch name with every `/` replaced by `-` and `<hash>` is the first 8 hex characters of the SHA-256 of the branch name (for example `fake/t-001-subtract` gives `calc-wt-fake-t-001-subtract-7eba2c4f`). |
| `allowed_paths` | list of globs | The change surface the spec predicts. Reported, not enforced (see below). |

Plus one way of choosing the model, below.

## Choosing the CLI and model

**Registry style (recommended).** `model` names an entry in `atlas_dispatch.adapter.MODELS`
(`atlas-dispatch models` lists them). It sets `cli`, the model id and, unless you also give
`reasoning_effort`, the effort.

```json
{ "model": "codex/gpt-6-sol-high" }
```

**Explicit style.** For a combination the registry does not have:

```json
{ "cli": "claude", "model_id": "claude-sonnet-5", "reasoning_effort": null }
```

| Field | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | none | A registry name. If it is not in the registry and `cli` is absent, loading fails and lists the valid names. |
| `cli` | string | `"codex"` | CLI name (lower-cased). An unregistered name is allowed: it runs `ATLAS_DISPATCH_<CLI>_CMD` if set, otherwise `<cli> -`, with the generic failure patterns. |
| `model_id` | string | none | Substituted for `{model}` in the CLI's argv. |
| `reasoning_effort` | string or null | the registry's, else none | Substituted for `{reasoning_effort}`. Overrides the registry value when given with `model`. |

`resolved_via` in `task.json` records which path was taken (`model_registry:<name>` or `explicit`).

## Scope and verification

| Field | Type | Default | Meaning |
|---|---|---|---|
| `allowed_paths` | list of globs | required | Predicted change surface. Every changed file outside it is listed as out of scope. **Informational: does not fail the run.** |
| `forbidden_paths` | list of globs | `[]` | Files the spec expects not to change. Matches are listed as forbidden violations. **Informational: does not fail the run.** |
| `acceptance` | list of shell commands | `[]` | Run with `sh -c` in the worktree after the CLI, in order. Every one must exit 0. **An empty list can never verify.** |
| `acceptance_timeout_seconds` | integer | `3600` | Per-command timeout. On expiry the whole process group is killed and the command is classified `acceptance_timeout`. |
| `acceptance_min_collected` | integer, or object of command → integer | none | Pytest collection floor. An integer applies to every command; an object applies per command and its keys must be commands present in `acceptance`. A command that collects fewer tests, or whose output does not establish a count, fails as `acceptance_collection_shrank`. Values must be `>= 0`. |
| `check_import_provenance` | boolean | `true` | Before each pytest-bearing command, probe that the first-party packages import from this worktree. Set `false` for repositories without an installable package layout, where the probe cannot decide and would refuse. Must be a JSON boolean. |

Glob rules for `allowed_paths`, `forbidden_paths` and protected patterns: paths are POSIX-style and
relative to the repository root; `*` and `?` do not cross `/`; `**` matches across directories;
every other character, including `[` and `]`, is literal.

**Protected paths** are not a spec field. They are a harness-level floor configured with the
`ATLAS_DISPATCH_PROTECTED_PATTERNS` environment variable (comma- or newline-separated globs; the
defaults are `configs/prod.*`, `migrations/**`, `secrets/**`, `.github/workflows/**`). A change to any
matching file fails verification whatever the spec or the acceptance commands say. Keeping them out of
the spec is the point: the author of a spec cannot lower the floor.

## Timing

| Field | Type | Default | Meaning |
|---|---|---|---|
| `timeout_seconds` | integer | `1800` | Hard limit on the CLI run. On expiry the CLI is terminated and the run is classified `timeout`. |
| `idle_timeout_seconds` | integer | `900` | Codex JSONL mode only: a turn with no lifecycle event for this long is classified `stalled`. |

## Git and branches

| Field | Type | Default | Meaning |
|---|---|---|---|
| `base_ref` | string | `"main"` | What the worktree starts from. `main` is checked out and fast-forwarded from `<remote_name>/main` first (refusing on divergence). Any other branch name uses the local branch if it exists, else fetches `<remote_name>/<base_ref>`. `refs/...`, `<remote>/...` and SHAs are used as given. For a review task, set it to the implementer's branch. |
| `remote_name` | string | `"origin"` | Remote used for base sync and publication. |
| `push_branch` | boolean | `false` | Push the task branch after the CLI run and before verification, whether or not verification later passes. Never force-pushes: a rejected push goes to `<branch>-run2`, `-run3`, ... The push counts only if a fresh fetch proves the remote ref contains the built commit. |
| `detached_head` | boolean | `false` | Create the worktree on a detached `HEAD` at `base_ref`; `worktree_branch` then only names the worktree directory. |
| `allow_destructive_branch_reset` | boolean | `false` | Reusing an existing `worktree_branch` resets it to `base_ref`. If the branch has commits that exist neither on `base_ref` nor on any remote, they are first saved under `refs/rescue/<branch-with-slashes-as-dashes>/<timestamp>` and the reset is **refused** unless this is exactly `true`. |
| `protected_head_sha` | string | none | If the existing branch's head equals this SHA, reuse is always refused, even with `allow_destructive_branch_reset`. For callers that have recorded a review of that exact commit. |

## Prompt

| Field | Type | Default | Meaning |
|---|---|---|---|
| `extra_prompt_vars` | object | `{}` | Extra `{{name}}` substitutions. Strings are inserted as-is; numbers and booleans are converted; lists and objects are inserted as indented JSON. Values are never scanned for placeholders. |
| `context_files` | list of paths | `[]` | Inlined into `{{context_block}}`, each under a `## <path>` heading in a fence long enough that the file's own backticks cannot close it. A missing file is noted, not fatal. A read that blocks for 5 seconds is skipped and the skip is recorded in the report. |

Built-in template variables:

| Variable | Value |
|---|---|
| `task_id`, `task_title` | `id`, `title` |
| `worktree_branch`, `worktree_path` | The branch and the absolute worktree path |
| `target_repo`, `base_ref` | As resolved |
| `cli`, `model`, `reasoning_effort` | As resolved (`model` is the model id) |
| `allowed_paths_block`, `forbidden_paths_block` | Bulleted lists, with a placeholder line when empty |
| `acceptance_block` | Bulleted list of commands in backticks |
| `context_block` | The inlined `context_files` |

A `{{name}}` in the template that is neither built in nor in `extra_prompt_vars` stops the dispatch
before anything runs. Placeholders inside fenced code blocks or inline code spans in the template are
ignored, so a template can show the syntax.

## MCP servers

| Field | Type | Default | Meaning |
|---|---|---|---|
| `mcp_servers` | list of objects | `[]` | Per-task MCP servers: `name`, `command`, `args`, optional `env`, optional `transport` (default `stdio`). |

How they reach the CLI depends on the CLI:

| CLI | Mechanism |
|---|---|
| `codex` | Written to `<worktree>/.atlas-dispatch/codex-home/config.toml`; the CLI runs with `CODEX_HOME` pointing there. |
| `gemini` | Written to `<worktree>/.gemini/settings.json`. |
| `claude` | Written to `<worktree>/.atlas-dispatch-mcp.json`, passed with `--mcp-config`. |
| `kimi`, `kimi-streaming` | Same file, passed with `--mcp-config-file`. |
| `kimi-code`, `grok`, `hermes`, unregistered CLIs | The file is written but no flag is injected; the CLI will not see it. |

The generated files are added to the worktree's git exclude list and ignored by the path checks, so
they never appear in the diff.

## Runs and reuse

| Field | Type | Default | Meaning |
|---|---|---|---|
| `runs_dir` | path | `<target_repo>/.atlas-dispatch/runs/<id>` | Where this task's run directories go. |
| `reuse_policy` | `"allow"` or `"never"` | `"allow"` | `allow`: if a prior run of this task verified with the identical resolved task, identical rendered prompt and the same base commit, record a new attempt satisfied by it and do not invoke the CLI. `never`: always run the CLI. |
| `no_reuse` | boolean | none | Shorthand: `true` means `reuse_policy: "never"`. Conflicting reuse settings are an error. |
| `ticket_ref` | string | none | Free-form external reference (an issue id, a URL) carried into `task.json`. |

`atlas-dispatch run --no-reuse` has the same effect as `reuse_policy: "never"` for one invocation.

## A complete example

```json
{
  "id": "EX-002",
  "title": "Independent review of EX-001 (retry with backoff)",
  "target_repo": "my-service",
  "model": "kimi/k2.7-coding",
  "prompt_template": "../prompts/review.md",
  "worktree_branch": "kimi/ex-002-review-of-ex-001",
  "base_ref": "codex/ex-001-http-retry",
  "reuse_policy": "never",
  "allowed_paths": ["reviews/EX-001-kimi.md"],
  "forbidden_paths": ["src/**", "tests/**"],
  "acceptance": ["test -f reviews/EX-001-kimi.md"],
  "timeout_seconds": 1500,
  "extra_prompt_vars": {
    "review_output_path": "reviews/EX-001-kimi.md",
    "source_task_id": "EX-001",
    "source_branch": "codex/ex-001-http-retry",
    "source_changed_files": "- `src/my_service/http_client.py`\n- `tests/test_http_client.py`"
  }
}
```

More in [examples/tasks/](../examples/tasks/).
