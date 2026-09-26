"""CLI + Model registries and the run/classify pipeline.

Two registries:

- `CLIS` — one entry per coding-agent CLI (codex, claude, gemini, kimi, grok,
  hermes).
  A `CLIDefinition` knows how to invoke the CLI in **full-access write mode**
  (so the dispatched task can edit files without permission prompts), and
  carries the regex patterns this engine uses to classify common failure
  modes (auth required, rate limited, overloaded, model refusal, etc.).

- `MODELS` — one entry per (CLI, model, thinking-level) tuple. A
  `ModelDefinition` tells the dispatcher which CLI to call and what
  model + reasoning args to pass. Task specs can either reference a
  model name (like `codex/gpt-6-sol-medium`) and let the engine resolve
  everything, or specify `cli` + `model` + `reasoning_effort` directly.

Both registries are plain dicts and are extensible from outside the package.
Users can override an individual CLI's invocation via `ATLAS_DISPATCH_<CLI>_CMD`
without touching code. CLI names are uppercased and non-alphanumerics are
converted to underscores for this variable (for example,
`kimi-code` uses `ATLAS_DISPATCH_KIMI_CODE_CMD`).

Kimi has multiple invocation modes. The legacy `kimi` and `kimi-streaming`
CLI definitions remain for direct adapter compatibility, but selectable
`kimi/k2.7-*` and `kimi/k3-*` models target Moonshot's Node `kimi-code` 0.x
CLI. No additional environment variables are required.

After every run, `classify_result()` reads the AdapterResult and
attaches a `DispatchErrorKind` plus a concrete `suggested_action` so
the orchestrator (an agent or a person) sees a clear "do this to fix it"
line in the report rather than a raw stack trace.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TextIO, runtime_checkable

from atlas_dispatch.codex_lifecycle import (
    CodexLifecycleState,
    ItemCompleted,
    apply_codex_event,
    classify_terminal_state,
    compact_lifecycle_record,
    decode_jsonl_line,
    utc_timestamp,
)

DEFAULT_TIMEOUT_SECONDS = 1800  # 30 min cap per task
# Codex emits JSONL lifecycle events between turn items but can think for many
# minutes between events on complex high-effort tasks. A 60s idle window
# declared healthy runs stalled while they were still working, so the default
# is 15 minutes. Override per spec via task.idle_timeout_seconds.
DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS = 900
CODEX_RUNTIME_MODE_ENV = "CODEX_RUNTIME_MODE"
BEAT_INTERVAL_SECONDS = 30.0
KIMI_WIRE_MODE = "kimi_wire"
KIMI_WIRE_INIT_ID = "1"
KIMI_WIRE_PROMPT_ID = "2"
KIMI_WIRE_CANCEL_ID = "3"
KIMI_WIRE_PROTOCOL_VERSION = "1.10"
KIMI_WIRE_QUEUE_POLL_SECONDS = 0.1
KIMI_WIRE_EXIT_DRAIN_SECONDS = 2.0
CLI_ENV_POLICY_VERSION = "1"

# These are the only ambient values common to every dispatched CLI. They are
# limited to OS identity/config discovery, locale, TLS/proxy configuration,
# and the SSH agent socket. PATH is rebuilt separately below so an ambient
# path entry cannot poison child tool discovery.
CLI_COMMON_ENV_ALLOWLIST: tuple[str, ...] = (
    "ALL_PROXY",
    "COLORTERM",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LANGUAGE",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_COLOR",
    "NO_PROXY",
    "SHELL",
    "SSH_AUTH_SOCK",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "all_proxy",
    "https_proxy",
    "http_proxy",
    "no_proxy",
)
CLI_ISOLATION_ENV: Mapping[str, str] = {
    "GCM_INTERACTIVE": "Never",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "PAGER": "cat",
    "PYTHONNOUSERSITE": "1",
}


# --------------------------------------------------------------------------- #
# Error classification                                                        #
# --------------------------------------------------------------------------- #


class DispatchErrorKind(StrEnum):
    SUCCESS = "success"
    NO_CHANGE = "no_change"
    FAILED = "failed"
    STALLED = "stalled"
    INTERRUPTED = "interrupted"
    APPROVAL_BLOCKED = "approval_blocked"
    EXIT_NONZERO = "exit_nonzero"
    TIMEOUT = "timeout"
    EXECUTABLE_NOT_FOUND = "executable_not_found"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    OVERLOADED = "overloaded"
    REFUSED = "refused"
    REFUSED_OUT_OF_SCOPE = "refused_out_of_scope"
    MODEL_SELECTION_ERROR = "model_selection_error"
    NO_OUTPUT = "no_output"
    UNKNOWN_FAILURE = "unknown_failure"


class QuotaResetWindowProvenance(StrEnum):
    """Authority for a concrete quota-reset window, never for an unknown one."""

    VENDOR_DECLARED = "vendor-declared"
    LOCALLY_INFERRED = "locally-inferred"


class CodexRuntimeMode(StrEnum):
    SUBPROCESS = "subprocess"
    APP_SERVER = "app_server"


@dataclass(frozen=True, kw_only=True)
class Classification:
    kind: DispatchErrorKind
    suggested_action: str
    matched_pattern: str | None = None
    # Set when a CLI-specific, positively evidenced completion rule accepts a
    # non-zero process exit. The dispatcher may normalize AdapterResult.exit_code
    # so its existing success gate can run acceptance, but the raw fact remains
    # visible here and in AdapterResult.process_exit_code.
    observed_exit_code: int | None = None
    quota_reset_window: str | None = None
    quota_reset_window_provenance: QuotaResetWindowProvenance | None = None

    def __post_init__(self) -> None:
        if (self.quota_reset_window is None) != (
            self.quota_reset_window_provenance is None
        ):
            raise ValueError(
                "quota reset window and provenance must be recorded together"
            )


# Default patterns. CLIDefinition entries can extend these per-CLI.
GENERIC_AUTH_PATTERNS: tuple[str, ...] = (
    r"\b401\b",
    r"unauthori[sz]ed",
    r"authentication\s+failed",
    r"not\s+logged\s+in",
    r"please\s+log\s*in",
    r"please\s+login",
    r"missing\s+credentials",
    r"invalid\s+api[_\s-]?key",
    r"expired\s+token",
    r"refresh\s+your\s+token",
)
GENERIC_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    r"\b429\b",
    r"rate[\s_-]?limit",
    r"too\s+many\s+requests",
    r"throttled",
    r"quota\s+exceeded",
    # codex/claude phrase a hard quota stop as "You've hit your usage limit."
    r"usage\s+limit",
)
GENERIC_OVERLOADED_PATTERNS: tuple[str, ...] = (
    r"\b503\b",
    r"service\s+unavailable",
    r"overloaded",
    r"server\s+busy",
)

# A completed turn may intentionally stop without editing because the required
# fix crosses the dispatch contract. Require evidence for all three parts of
# that meaning: inability to complete the requested work, a required change,
# and a causal crossing of the explicit scope/allowlist boundary. In
# particular, "I will not modify files outside scope" is a compliance promise,
# not evidence that required work could not be completed.
_OUT_OF_SCOPE_REFUSAL = (
    r"(?:can(?:not|'t)|unable\s+to|won't|will\s+not|must\s+not|must\s+stop|"
    r"declin(?:e|ed|ing)|refus(?:e|ed|ing)|stopp(?:ed|ing))"
)
_OUT_OF_SCOPE_COMPLETION = (
    r"(?:implement(?:ed|ing)?|complet(?:e|ed|ing)|finish(?:ed|ing)?|"
    r"deliver(?:ed|ing)?|perform(?:ed|ing)?)"
)
_OUT_OF_SCOPE_REQUIRED_CHANGE = (
    r"(?:required|necessary|needed|requires?|would\s+require|needs?|must)"
    r"[^.!?\n]{0,160}(?:chang(?:e|es|ing)|modif(?:y|ication|ications|ying)|"
    r"edit(?:s|ing)?|files?|paths?|work|config(?:uration)?(?:\s+file)?)"
)
_OUT_OF_SCOPE_BOUNDARY = (
    r"(?:outside|out\s+of|beyond)\s+(?:the\s+)?"
    r"(?:allowed[_\s-]*paths?|allowlist|allowed\s+scope|task\s+scope|scope)"
)
_OUT_OF_SCOPE_CAUSE = r"(?:because|since|due\s+to|as\s+a\s+result|:|;)"
# Permit dots inside paths such as ``settings_service.py`` while refusing to cross
# sentence boundaries (a period followed by whitespace).
_OUT_OF_SCOPE_SPAN = r"(?:[^.!?\n]|\.(?=\S)){0,240}"
OUT_OF_SCOPE_REFUSAL_PATTERNS: tuple[str, ...] = (
    (
        _OUT_OF_SCOPE_REFUSAL
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_COMPLETION
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_CAUSE
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_REQUIRED_CHANGE
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_BOUNDARY
    ),
    (
        _OUT_OF_SCOPE_REFUSAL
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_COMPLETION
        + _OUT_OF_SCOPE_SPAN
        + r"(?:required|necessary|requested)"
        + _OUT_OF_SCOPE_SPAN
        + r"(?:without|unless)"
        + _OUT_OF_SCOPE_SPAN
        + r"(?:chang(?:e|ing)|modif(?:y|ying)|edit(?:ing)?)"
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_BOUNDARY
    ),
    (
        _OUT_OF_SCOPE_REQUIRED_CHANGE
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_BOUNDARY
        + _OUT_OF_SCOPE_SPAN
        + r"(?:so|therefore|thus|which\s+means)"
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_REFUSAL
        + _OUT_OF_SCOPE_SPAN
        + _OUT_OF_SCOPE_COMPLETION
    ),
)
GENERIC_REFUSAL_PATTERNS: tuple[str, ...] = (
    r"i\s+can'?t\s+(help|assist)",
    r"i\s+(cannot|will\s+not)\s+(help|assist|comply)",
    r"i\s+won'?t\s+(help|assist)",
    r"this\s+request\s+(violates|is\s+against)",
)
BENIGN_MACOS_MALLOC_WARNING_RE = re.compile(
    r"(?m)^.*\bMallocStackLogging: can't turn off malloc stack logging "
    r"because it was not enabled\.\n?"
)

# Classification is searched against a bounded slice so a multi-MB stdout
# does not cause a temporary ~2x spike during .lower() concatenation.
MAX_CLASSIFY_HAYSTACK = 256_000  # 256 KB — tail-biased because errors tend
                                 # to appear near the end of CLI output.


# --------------------------------------------------------------------------- #
# CLI registry                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class CLIDefinition:
    """How to invoke a coding-agent CLI in full-access write mode."""

    name: str
    executable: str

    # argv with placeholders. Substituted: {cwd}, {model}, {reasoning_effort}.
    # The CLI must read its prompt from stdin (most agent CLIs do; the
    # template should end in `-` or its CLI-specific stdin marker).
    argv_template: list[str]

    # Hint shown in the report when a run is classified auth_required.
    auth_setup_hint: str

    # Per-CLI patterns layered on top of the GENERIC_* patterns.
    extra_auth_patterns: tuple[str, ...] = ()
    extra_rate_limit_patterns: tuple[str, ...] = ()
    extra_quota_exhausted_patterns: tuple[str, ...] = ()
    extra_overloaded_patterns: tuple[str, ...] = ()
    extra_refusal_patterns: tuple[str, ...] = ()
    extra_post_completion_timeout_patterns: tuple[str, ...] = ()

    # Exact exit-zero output that means the CLI did not complete the task.
    model_selection_error_patterns: tuple[str, ...] = ()

    # Whether this CLI's argv reads the prompt from stdin (vs. as an arg).
    reads_prompt_from_stdin: bool = True

    # Runtime implementation used by SubprocessAdapter/run_cli.
    adapter_mode: str = "subprocess"

    # CLI-specific ambient auth/config keys. Values reach only the selected
    # CLI subprocess and are never recorded in run artifacts.
    env_allowlist: tuple[str, ...] = ()

    # Optional dotenv file holding this CLI's provider credentials, and the
    # (file key -> subprocess env var) pairs to lift out of it.
    #
    # Why a file rather than the ambient environment: a CLI pointed at a
    # different provider endpoint needs variables (a base URL, a key) that
    # would also redirect every other CLI reading the same names if they were
    # set globally. Reading them from a per-CLI file at invocation time scopes
    # them to exactly one CLI and works the same under any process launcher.
    # Every target here MUST also appear in env_allowlist -- the allowlist
    # stays the single statement of what may reach a CLI.
    credentials_file: str = ""
    credentials_env_map: tuple[tuple[str, str], ...] = ()


CLIS: dict[str, CLIDefinition] = {
    # ------------------------------------------------------------------ codex
    "codex": CLIDefinition(
        name="codex",
        executable="codex",
        argv_template=[
            "codex",
            "exec",
            "--json",
            "-m",
            "{model}",
            "-c",
            'model_reasoning_effort="{reasoning_effort}"',
            # Full access. The task runs in a per-task git worktree, so the
            # blast radius is the worktree, not whatever sandbox codex would
            # otherwise impose. No permission prompts.
            "--dangerously-bypass-approvals-and-sandbox",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-C",
            "{cwd}",
            "-",
        ],
        auth_setup_hint="Run `codex login` to refresh credentials.",
        extra_auth_patterns=(r"codex\s+login",),
        extra_overloaded_patterns=(
            r"selected\s+model\s+is\s+at\s+capacity",
            r"model\s+is\s+at\s+capacity",
        ),
        env_allowlist=(
            "AZURE_OPENAI_API_KEY",
            "AZURE_OPENAI_ENDPOINT",
            "CODEX_API_KEY",
            "CODEX_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORGANIZATION",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT",
        ),
    ),
    # ------------------------------------------------------------------ claude
    "claude": CLIDefinition(
        name="claude",
        executable="claude",
        argv_template=[
            "claude",
            "--print",
            "--input-format",
            "text",
            "--output-format",
            "text",
            "--model",
            "{model}",
            # Full-access inside the worktree, matching codex
            # (--dangerously-bypass-approvals-and-sandbox) and gemini
            # (--dangerously-skip-permissions). The task-authored path lists
            # predict the diff surface; they are not a sandbox or a safety
            # gate. Repository isolation and post-run verification remain
            # outside this CLI permission setting.
            #
            # `--permission-mode acceptEdits` is not enough: it auto-accepts
            # EDITS but still gates Bash, and in --print mode nobody is there
            # to grant it, so every test invocation is denied. A reviewer that
            # cannot execute cannot verify by effect, which is the one thing a
            # review is for.
            "--dangerously-skip-permissions",
            "--add-dir",
            "{cwd}",
            "--append-system-prompt",
            "The target repository/worktree is {cwd}. Operate in that directory.",
            # Claude Code reads the prompt from stdin in --print/text pipe mode.
        ],
        auth_setup_hint="Run `claude auth login` (or refresh your Anthropic credentials).",
        extra_auth_patterns=(
            r"could\s+not\s+resolve\s+authentication\s+method",
            r"expected\s+either\s+apikey\s+or\s+authtoken\s+to\s+be\s+set",
            r"authentication_(?:error|failed)",
            r"oauth\s+token\s+(?:has\s+)?expired",
            r"oauth\s+session\s+(?:has\s+)?expired",
            r"invalid\s+api\s+key.*please\s+run\s+/login",
            r"please\s+run\s+/login",
            r"run\s+`?claude\s+/login`?",
        ),
        extra_rate_limit_patterns=(
            r"api\s+error(?:\s*[:(])\s*429",
            r"api\s+error:?\s+rate\s+limit\s+reached",
            r"rate\s+limited\s+\(429\)",
            r"rate_limit_error",
            r"you'?ve\s+been\s+rate\s+limited",
        ),
        extra_quota_exhausted_patterns=(
            (
                r"you(?:'|\u2019)?ve\s+hit\s+your\s+session\s+limit\s*"
                r"(?:[\u00b7\u2022]\s*)?resets\s+"
                r"(?P<reset_window>"
                r"\d{1,2}(?::[0-5]\d)?\s*(?:am|pm)\s*"
                r"\([a-z][a-z0-9._+-]*(?:/[a-z0-9._+-]+)+\)"
                r")"
            ),
        ),
        extra_overloaded_patterns=(
            r"api\s+error(?:\s*[:(])\s*529",
            r"overloaded_error",
        ),
        extra_refusal_patterns=(
            r"i\s+can'?t\s+provide\s+instructions\s+that\s+would\s+facilitate",
            r"i\s+can'?t\s+assist\s+with\s+that\s+request",
        ),
        model_selection_error_patterns=(
            r"\A\s*there'?s\s+an\s+issue\s+with\s+the\s+selected\s+model\s+"
            r"\([^\r\n)]+\)\.\s+it\s+may\s+not\s+exist\s+or\s+you\s+may\s+not\s+"
            r"have\s+access\s+to\s+it\.\s+run\s+--model\s+to\s+pick\s+a\s+"
            r"different\s+model\.\s*\Z",
        ),
        env_allowlist=(
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "AWS_ACCESS_KEY_ID",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_CONFIG_FILE",
            "AWS_DEFAULT_REGION",
            "AWS_PROFILE",
            "AWS_REGION",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_SHARED_CREDENTIALS_FILE",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CONFIG_DIR",
            "CLOUD_ML_REGION",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_PROJECT",
        ),
    ),
    # ------------------------------------------------------------------ gemini
    "gemini": CLIDefinition(
        name="gemini",
        # The legacy `gemini` CLI's individual tier now fails with
        # IneligibleTierError and points users at Antigravity; upgrading the
        # client or setting GEMINI_API_KEY does not route around it. The
        # successor binary is `agy` (Antigravity CLI).
        # This entry keeps the NAME "gemini" deliberately: specs and reviewer
        # routing key off the CLI name, and the model really is Gemini. Only
        # the delivery binary changed, so one definition is kept rather than a
        # parallel `agy` entry that would drift from this one.
        executable="agy",
        # Gemini 0.39+: --prompt forces headless mode; an empty prompt lets
        # the dispatcher supply the real task on stdin.
        # Gemini 0.41+: --skip-trust is required for headless workspaces.
        # Without it, the CLI silently overrides --approval-mode yolo to
        # default and waits forever for an interactive trust confirmation
        # that never arrives in unattended contexts. Symptom: gemini sits at
        # 0% CPU on kevent with no stdout and no TCP connections.
        # DO NOT ADD `--model`. Verified live against agy 1.1.7: EVERY
        # explicit --model value tried -- slug ("gemini-3.6-pro-preview") and
        # human-readable ("Gemini 3.6 Flash", "Gemini 3 Pro") alike -- SILENTLY
        # FALLS BACK TO "GPT-OSS 120B", a GPT-FAMILY model. Some names error
        # loudly; most degrade silently. This CLI exists to be the INDEPENDENT
        # non-Codex review leg, so a silent GPT-family fallback would make the
        # reviewer correlated with the Codex builder while every artifact still
        # looked healthy. The DEFAULT resolves to "Gemini 3.6 Flash (High)".
        # Non-TTY hang (upstream #318, seen on 1.0.6) is FIXED at 1.1.7:
        # verified with stdin+stdout both piped, 6s, exit 0, output captured.
        argv_template=[
            "agy",
            "--dangerously-skip-permissions",
            "-p",
            "{prompt}",
        ],
        # agy takes the prompt as a `-p` ARGUMENT, not on stdin -- same shape as
        # kimi-code. Leaving the default (True) sends the prompt BOTH ways and
        # agy exits 2 with its usage text in 0.04s. Verified by-effect.
        reads_prompt_from_stdin=False,
        auth_setup_hint=(
            "Antigravity CLI (`agy`). Install/update: curl -fsSL "
            "https://antigravity.google/cli/install.sh | bash -- it also "
            "self-updates in the background on normal runs. Auth is "
            "INTERACTIVE and per-machine: a machine printing 'Waiting for "
            "authentication' needs a human sign-in there. The legacy "
            "`gemini` binary is retired (IneligibleTierError)."
        ),
        extra_auth_patterns=(
            r"(?m)^\s*(?:[\u2715\u2139]\s*)?code\s+assist\s+login\s+required\.?\s*$",
            r"failed\s+to\s+login\.\s+message:",
            r"api\s+error:\s+api\s+key\s+(?:not\s+found|not\s+valid)\.\s+please\s+pass\s+a\s+valid\s+api\s+key",
            r"gemini\s+api\s+key\s+is\s+missing\s+or\s+not\s+configured",
            r"(?i)waiting\s+for\s+authentication",
            r"(?i)ineligibletiererror",
        ),
        extra_rate_limit_patterns=(
            r"rate\s+limit\s+exceeded\.\s+try\s+again\s+later",
            r"resource[_\s]+exhausted",
            r"quota\s+exceeded\s+for\s+quota\s+metric",
            r"you\s+have\s+reached\s+your\s+daily\s+gemini-[\w.-]+\s+quota\s+limit",
        ),
        extra_quota_exhausted_patterns=(
            (
                r"individual\s+quota\s+reached\.\s+please\s+upgrade\s+your\s+"
                r"subscription\s+to\s+increase\s+your\s+limits\."
                r"(?:\s+resets\s+in\s+"
                r"(?P<reset_window>\d+\s*h\s*\d+\s*m\s*\d+\s*s)\.)?"
            ),
        ),
        extra_overloaded_patterns=(
            r"the\s+model\s+is\s+overloaded\.\s+please\s+try\s+again\s+later",
            r"(?m)[\"']status[\"']\s*:\s*[\"']unavailable[\"']",
            r"service\s+may\s+be\s+temporarily\s+overloaded\s+or\s+down",
            r"service\s+is\s+temporarily\s+running\s+out\s+of\s+capacity",
        ),
        extra_refusal_patterns=(
            r"i'?m\s+not\s+okay\s+with\s+this\s+conversation,?\s+so\s+i'?ll\s+stop\s+it\s+here",
        ),
        # agy can finish a headless task, write its requested artifact, and
        # only then fail its print-mode response wait. This pattern is not
        # sufficient on its own: classify_result also requires independently
        # captured evidence that this run produced the expected deliverable.
        extra_post_completion_timeout_patterns=(
            r"(?m)^error:\s+timeout\s+waiting\s+for\s+response\s*$",
        ),
        env_allowlist=(
            "CLOUDSDK_CONFIG",
            "GCLOUD_PROJECT",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_GENAI_USE_VERTEXAI",
        ),
    ),
    # ------------------------------------------------------------------ kimi
    "kimi": CLIDefinition(
        name="kimi",
        executable="kimi",
        # --final-message-only is required to suppress the structured event
        # stream and surface only the assistant's final text on stdout. Without
        # it, the agent loop can exit non-zero on otherwise-successful runs.
        # Live-verified against kimi 1.37.0.
        argv_template=[
            "kimi",
            "--print",
            "--input-format",
            "text",
            "--output-format",
            "text",
            "--final-message-only",
            "--model",
            "{model}",
            "{reasoning_effort}",
            "--work-dir",
            "{cwd}",
        ],
        auth_setup_hint=(
            "Run `kimi login`, or configure KIMI_API_KEY / KIMI_BASE_URL / "
            "KIMI_MODEL_NAME for headless Kimi provider use."
        ),
        extra_auth_patterns=(
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"(?:invalid_authentication_error|incorrect_api_key_error|"
                r"invalid\s+authentication|incorrect\s+api\s+key\s+provided)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"run\s+the\s+`?kimi\s+login`?\s+command"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"kimi_api_key\s+(?:is\s+)?(?:not\s+set|missing)"
            ),
            r"auth_required[^\n]+run\s+the\s+`?kimi\s+login`?\s+command",
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"\bauth_(?:required|expired)\b.*"
                r"(?:kimi\s+login|sign\s+in|log\s+in)"
            ),
        ),
        extra_rate_limit_patterns=(
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"request\s+reached\s+organization\s+max\s+(?:concurrency|rpm)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"request\s+reached\s+organization\s+(?:tpm|tpd)\s+rate\s+limit"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"you\s+exceeded\s+your\s+current\s+token\s+quota"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"exceeded_current_quota_error"
            ),
        ),
        extra_overloaded_patterns=(
            r"(?m)^server_error:\s+failed\s+to\s+extract\s+file",
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"(?:server_error|unexpected_output)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error)\b.*"
                r"engine_overloaded_error"
            ),
            r"the\s+engine\s+is\s+currently\s+overloaded,\s+please\s+try\s+again\s+later",
        ),
        extra_refusal_patterns=(
            r"the\s+request\s+was\s+rejected\s+because\s+it\s+was\s+considered\s+high\s+risk",
            r"i\s+can'?t\s+help\s+with\s+this\s+request",
        ),
        reads_prompt_from_stdin=True,
        env_allowlist=(
            "KIMI_API_KEY",
            "KIMI_BASE_URL",
            "KIMI_CONFIG_FILE",
            "KIMI_MODEL_NAME",
            "MOONSHOT_API_KEY",
        ),
    ),
    # ------------------------------------------------------------- kimi-code
    "kimi-code": CLIDefinition(
        name="kimi-code",
        executable="kimi",
        # Kimi Code 0.x removed `--print`, stdin pipe mode, and
        # `--final-message-only`; the prompt is a `-p` argument. Use text
        # output until atlas-dispatch has a stream-json final-message parser,
        # because raw stream-json would persist event envelopes as stdout.
        #
        # `-p`/`--prompt` is already a non-interactive one-shot mode that
        # auto-executes tools (validated live against 0.6.0). The interactive
        # approval flags `-y`/`--yolo` and `--auto` CANNOT be combined with
        # `-p` — 0.6.0 hard-errors "Cannot combine --prompt with --yolo/--auto"
        # and exits non-zero, so they must NOT appear here.
        argv_template=[
            "kimi",
            "-p",
            "{prompt}",
            "-m",
            "{model}",
            "--output-format",
            "text",
        ],
        auth_setup_hint=(
            "Run `kimi migrate` from the legacy Kimi config if needed, then "
            "verify ~/.kimi-code/config.toml and ~/.kimi-code/credentials/."
        ),
        extra_auth_patterns=(
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*(?:invalid_authentication_error|"
                r"incorrect_api_key_error|invalid\s+authentication|"
                r"incorrect\s+api\s+key\s+provided)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*(?:missing|not\s+found|invalid)\s+"
                r"(?:credentials|credential|token)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*(?:run\s+`?kimi\s+migrate`?|"
                r"migrate\s+from\s+legacy\s+config|"
                r"~/?\.kimi-code/(?:config\.toml|credentials))"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*\bauth_(?:required|expired)\b.*"
                r"(?:sign\s+in|log\s+in|migrate|credentials)"
            ),
        ),
        extra_rate_limit_patterns=(
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*request\s+reached\s+organization\s+"
                r"max\s+(?:concurrency|rpm)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*request\s+reached\s+organization\s+"
                r"(?:tpm|tpd)\s+rate\s+limit"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*"
                r"you\s+exceeded\s+your\s+current\s+token\s+quota"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*(?:exceeded_current_quota_error|"
                r"quota_exceeded|insufficient_quota)"
            ),
        ),
        extra_overloaded_patterns=(
            r"(?m)^server_error:\s+failed\s+to\s+extract\s+file",
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*(?:server_error|unexpected_output)"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*engine_overloaded_error"
            ),
            r"the\s+engine\s+is\s+currently\s+overloaded,\s+please\s+try\s+again\s+later",
        ),
        extra_refusal_patterns=(
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*(?:\b400\b|bad\s+request).*"
                r"content[_\s-]?filter"
            ),
            (
                r"(?m)^(?:error|api error|llm provider error|provider error|"
                r"kimi(?:[-\s]code)? error)\b.*"
                r"(?:content[_\s-]?filter|high\s+risk|risk_control)"
            ),
            r"the\s+request\s+was\s+rejected\s+because\s+it\s+was\s+considered\s+high\s+risk",
            r"i\s+can'?t\s+help\s+with\s+this\s+request",
        ),
        reads_prompt_from_stdin=False,
        env_allowlist=(
            "KIMI_API_KEY",
            "KIMI_BASE_URL",
            "KIMI_CONFIG_FILE",
            "KIMI_MODEL_NAME",
            "MOONSHOT_API_KEY",
        ),
    ),
    # ------------------------------------------------------------------ hermes
    "hermes": CLIDefinition(
        name="hermes",
        executable="hermes",
        # Hermes selects model/reasoning via its own config, not argv flags.
        argv_template=[
            "hermes",
            "--ignore-rules",
            "--oneshot",
            "{prompt}",
        ],
        auth_setup_hint=(
            "Configure Hermes per its setup docs; the CLI inherits Codex "
            "authentication."
        ),
        extra_auth_patterns=(
            # Hermes is Codex-backed; surface the same auth wording.
            r"codex\s+login",
            r"please\s+log\s+in",
            r"authentication\s+(?:required|failed|error)",
        ),
        extra_rate_limit_patterns=(
            r"rate\s+limit",
            r"too\s+many\s+requests",
            r"\b429\b",
        ),
        extra_overloaded_patterns=(
            r"overloaded",
            r"capacity\s+(?:exceeded|exhausted)",
            r"\b503\b",
        ),
        reads_prompt_from_stdin=False,
        env_allowlist=(
            "CODEX_API_KEY",
            "CODEX_HOME",
            "HERMES_CONFIG",
            "HERMES_HOME",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORGANIZATION",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT",
        ),
    ),
}

CLIS["grok"] = CLIDefinition(
    name="grok",
    executable="grok",
    # Grok Build's `-p/--single` is one-shot headless: it prints the response to
    # stdout and exits, which is the shape this harness wants. The prompt is an
    # ARGV ARGUMENT, not stdin -- same as hermes, unusual among the others.
    # `--effort` is the documented alias for --reasoning-effort.
    argv_template=[
        "grok",
        "-p",
        "{prompt}",
        "--model",
        "{model}",
        "--effort",
        "{reasoning_effort}",
        "--cwd",
        "{cwd}",
    ],
    auth_setup_hint=(
        "Run `grok login` (SuperGrok account). `grok models` lists the models "
        "the current login can reach."
    ),
    extra_auth_patterns=(
        r"grok\s+login",
        r"not\s+logged\s+in",
        r"authentication\s+(?:required|failed|error)",
    ),
    extra_rate_limit_patterns=(
        r"rate\s+limit",
        r"too\s+many\s+requests",
        r"\b429\b",
    ),
    extra_quota_exhausted_patterns=(
        r"out\s+of\s+credits",
        r"allowance\s+exhausted",
        r"usage\s+limit\s+reached",
    ),
    extra_overloaded_patterns=(
        r"overloaded",
        r"capacity\s+(?:exceeded|exhausted)",
        r"\b503\b",
        r"\b529\b",
    ),
    reads_prompt_from_stdin=False,
)

CLIS["fake"] = CLIDefinition(
    name="fake",
    # A deterministic stand-in agent shipped with the package, for the
    # quickstart and end-to-end tests. It needs no account and makes no network
    # calls; see atlas_dispatch/fake_agent.py for the prompt directives it obeys.
    # The interpreter is the one running atlas-dispatch, by absolute path, so
    # the sanitised CLI PATH cannot select a Python without this package.
    executable=sys.executable,
    argv_template=[sys.executable, "-m", "atlas_dispatch.fake_agent"],
    auth_setup_hint="No authentication: `fake` is a local stand-in agent.",
    reads_prompt_from_stdin=True,
)

CLIS["kimi-streaming"] = CLIDefinition(
    name="kimi-streaming",
    executable="kimi",
    # Kimi 1.43.0 exposes true streaming through Wire JSON-RPC over stdio.
    # `--afk` preserves the non-interactive behavior of `--print`: no user
    # is present, AskUserQuestion is auto-dismissed, and tool calls are
    # auto-approved. The named session makes retries land in the same Kimi
    # session for this dispatch worktree.
    argv_template=[
        "kimi",
        "--wire",
        "--afk",
        "--model",
        "{model}",
        "{reasoning_effort}",
        "--work-dir",
        "{cwd}",
        "--session",
        "{kimi_session}",
    ],
    auth_setup_hint=CLIS["kimi"].auth_setup_hint,
    extra_auth_patterns=CLIS["kimi"].extra_auth_patterns,
    extra_rate_limit_patterns=CLIS["kimi"].extra_rate_limit_patterns,
    extra_overloaded_patterns=CLIS["kimi"].extra_overloaded_patterns,
    extra_refusal_patterns=CLIS["kimi"].extra_refusal_patterns,
    reads_prompt_from_stdin=False,
    adapter_mode=KIMI_WIRE_MODE,
    env_allowlist=CLIS["kimi"].env_allowlist,
)


# --------------------------------------------------------------------------- #
# Model registry                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class ModelDefinition:
    """A named (CLI, model, thinking-level) triple."""

    name: str  # e.g. "codex/gpt-6-sol-medium"
    cli: str  # key into CLIS
    model_id: str  # passed as -m
    reasoning_effort: str | None = None
    description: str = ""


REVIEW_MODEL_EVIDENCE_SCHEMA = "atlas-dispatch.review-model-evidence"
REVIEW_MODEL_EVIDENCE_SCHEMA_VERSION = 1
UNKNOWN_EFFECTIVE_MODEL = "unknown"
ANTIGRAVITY_EFFECTIVE_MODEL = "gemini/3.6-flash-agy"


@dataclass(frozen=True, kw_only=True)
class ReviewModelEvidence:
    """Durable, versioned identity for the runtime that performed a review."""

    schema: str
    schema_version: int
    effective_cli: str
    effective_model: str
    model_resolution_source: str
    requested_model: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema": self.schema,
            "schema_version": self.schema_version,
            "effective_cli": self.effective_cli,
            "effective_model": self.effective_model,
            "model_resolution_source": self.model_resolution_source,
        }
        if self.requested_model is not None:
            payload["requested_model"] = self.requested_model
        return payload


@dataclass(frozen=True, kw_only=True)
class ResolvedInvocation:
    """The one resolved command and model identity used for an adapter run."""

    command: list[str]
    review_model_evidence: ReviewModelEvidence


MODELS: dict[str, ModelDefinition] = {
    # ------------------------------------------------------------------ codex
    "codex/gpt-6-sol-medium": ModelDefinition(
        name="codex/gpt-6-sol-medium",
        cli="codex",
        model_id="gpt-6-sol",
        reasoning_effort='medium',
        description=(
            'GPT-6 Sol, medium effort. A good default for ordinary implementation work.'
        ),
    ),
    "codex/gpt-6-sol-high": ModelDefinition(
        name="codex/gpt-6-sol-high",
        cli="codex",
        model_id="gpt-6-sol",
        reasoning_effort='high',
        description=(
            'GPT-6 Sol, high effort. Multi-file changes and harder bugs.'
        ),
    ),
    "codex/gpt-6-sol-xhigh": ModelDefinition(
        name="codex/gpt-6-sol-xhigh",
        cli="codex",
        model_id="gpt-6-sol",
        reasoning_effort='xhigh',
        description=(
            'GPT-6 Sol, extra-high effort. Large or ambiguous builds; noticeably more expensive than medium.'
        ),
    ),
    "codex/gpt-6-astra-medium": ModelDefinition(
        name="codex/gpt-6-astra-medium",
        cli="codex",
        model_id="gpt-6-astra",
        reasoning_effort='medium',
        description=(
            'GPT-6 Astra, medium effort. Requires a recent codex-cli; an older client refuses with an error that reads like a bad model name but is a version floor.'
        ),
    ),
    "codex/gpt-6-astra-high": ModelDefinition(
        name="codex/gpt-6-astra-high",
        cli="codex",
        model_id="gpt-6-astra",
        reasoning_effort='high',
        description=(
            'GPT-6 Astra, high effort.'
        ),
    ),
    "codex/gpt-6-luna-low": ModelDefinition(
        name="codex/gpt-6-luna-low",
        cli="codex",
        model_id="gpt-6-luna",
        reasoning_effort='low',
        description=(
            'GPT-6 Luna, low effort. Cheap, high-volume clerical work (summaries, extraction). Not suited to building or reviewing.'
        ),
    ),
    "codex/gpt-6-luna-medium": ModelDefinition(
        name="codex/gpt-6-luna-medium",
        cli="codex",
        model_id="gpt-6-luna",
        reasoning_effort='medium',
        description=(
            'GPT-6 Luna, medium effort. Small, clearly specified edits.'
        ),
    ),
    "codex/gpt-5.6-terra-medium": ModelDefinition(
        name="codex/gpt-5.6-terra-medium",
        cli="codex",
        model_id="gpt-5.6-terra",
        reasoning_effort='medium',
        description=(
            'GPT-5.6 Terra, medium effort. Tasks that need exploring several parts of a repository.'
        ),
    ),
    # ----------------------------------------------------------------- claude
    "claude/opus-5.5": ModelDefinition(
        name="claude/opus-5.5",
        cli="claude",
        model_id="claude-opus-5-5",
        reasoning_effort=None,
        description=(
            'Claude Code on Opus 5.5. Highest-capability Claude; long, hard, autonomous builds and deep reviews.'
        ),
    ),
    "claude/sonnet-5": ModelDefinition(
        name="claude/sonnet-5",
        cli="claude",
        model_id="claude-sonnet-5",
        reasoning_effort=None,
        description=(
            'Claude Code on Sonnet 5. Cheaper Claude for routine builds and reviews.'
        ),
    ),
    "claude/haiku-4.5": ModelDefinition(
        name="claude/haiku-4.5",
        cli="claude",
        model_id="claude-haiku-4-5-20251001",
        reasoning_effort=None,
        description=(
            'Claude Haiku 4.5. Fast and cheap; triage and classification rather than building.'
        ),
    ),
    "claude/fable-5": ModelDefinition(
        name="claude/fable-5",
        cli="claude",
        model_id="claude-fable-5[1m]",
        reasoning_effort=None,
        description=(
            'Claude Fable 5 (1M context). A strong finder for review work. Note it is Claude-family, so it is not independent of a Claude builder.'
        ),
    ),
    # ------------------------------------------------------------------- kimi
    # Kimi via Moonshot's Node kimi-code 0.x CLI. One-shot mode is
    # `kimi -p {prompt} -m {alias} --output-format text`; the CLI exposes no
    # per-invocation thinking or effort flag, so the registry selects the MODEL
    # and effort follows the CLI's own config default.
    "kimi/k2.7-coding": ModelDefinition(
        name="kimi/k2.7-coding",
        cli="kimi-code",
        model_id="kimi-code/kimi-for-coding",
        reasoning_effort=None,
        description=(
            'Kimi K2.7 Code (managed alias kimi-for-coding, 256K context, always thinking) via Node kimi-code. A good independent reviewer model.'
        ),
    ),
    "kimi/k2.7-coding-highspeed": ModelDefinition(
        name="kimi/k2.7-coding-highspeed",
        cli="kimi-code",
        model_id="kimi-code/kimi-for-coding-highspeed",
        reasoning_effort=None,
        description=(
            'Kimi K2.7 Code Highspeed via Node kimi-code; faster, same model family.'
        ),
    ),
    "kimi/k3-max": ModelDefinition(
        name="kimi/k3-max",
        cli="kimi-code",
        model_id="kimi-code/k3",
        reasoning_effort=None,
        description=(
            "Kimi K3 (1M context) via Node kimi-code. The name is historical: no effort flag is passed, so it runs at the CLI's configured default effort."
        ),
    ),
    "kimi/k3-256k": ModelDefinition(
        name="kimi/k3-256k",
        cli="kimi-code",
        model_id="kimi-code/k3-256k",
        reasoning_effort=None,
        description=(
            'Kimi K3 in 256K-context mode via Node kimi-code; effort follows the config default.'
        ),
    ),
    # ----------------------------------------------------------------- gemini
    # model_id is INTENTIONALLY EMPTY. Passing --model to the Antigravity CLI
    # silently falls back to a different model family (see the CLI definition),
    # which would make a "Gemini" review correlated with a GPT-family builder.
    "gemini/3.6-flash-agy": ModelDefinition(
        name="gemini/3.6-flash-agy",
        cli="gemini",
        model_id="",
        reasoning_effort=None,
        description=(
            'Antigravity CLI default model, which resolves to Gemini 3.6 Flash (High). An independent non-GPT, non-Claude reviewer model. Never populate model_id.'
        ),
    ),
    # ------------------------------------------------------------------- grok
    "grok/4.7-high": ModelDefinition(
        name="grok/4.7-high",
        cli="grok",
        model_id="grok-4.7",
        reasoning_effort='high',
        description=(
            'Grok 4.7 at high effort. The default Grok reviewer model. The prompt is passed as an argv argument, not on stdin.'
        ),
    ),
    "grok/4.7": ModelDefinition(
        name="grok/4.7",
        cli="grok",
        model_id="grok-4.7",
        reasoning_effort='medium',
        description=(
            'Grok 4.7 at medium effort, for smaller reviews and well-scoped work.'
        ),
    ),
    "grok/4.7-xhigh": ModelDefinition(
        name="grok/4.7-xhigh",
        cli="grok",
        model_id="grok-4.7",
        reasoning_effort='xhigh',
        description=(
            'Grok 4.7 at extra-high effort. Reserve for security-sensitive diffs or a second pass after high found something; it costs noticeably more for a modest gain.'
        ),
    ),
    "grok/4.6-high": ModelDefinition(
        name="grok/4.6-high",
        cli="grok",
        model_id="grok-4.6",
        reasoning_effort='high',
        description=(
            'Grok 4.6 at high effort. Prior generation; a fallback when 4.7 is capacity-limited.'
        ),
    ),
    # ------------------------------------------------------------------- fake
    "fake/scripted": ModelDefinition(
        name="fake/scripted",
        cli="fake",
        model_id="scripted",
        reasoning_effort=None,
        description="The bundled stand-in agent. Follows FAKE-AGENT directives in the prompt; no API key needed.",
    ),
    # ----------------------------------------------------------------- hermes
    # Hermes is a Codex-backed agent harness. Model and reasoning are recorded
    # here for documentation, but Hermes selects them from its own config and
    # does not accept them as argv flags.
    "hermes/gpt-5.5-high": ModelDefinition(
        name="hermes/gpt-5.5-high",
        cli="hermes",
        model_id="gpt-5.5",
        reasoning_effort='high',
        description=(
            "Hermes agent harness (Codex-backed) used as a reviewer. Model and effort come from Hermes' own config, not from argv."
        ),
    ),
}


def list_supported_clis() -> list[str]:
    return sorted(CLIS)


def list_supported_models() -> list[str]:
    return sorted(MODELS)


def resolve_model(model_name: str) -> ModelDefinition:
    if model_name not in MODELS:
        raise ValueError(
            f"unknown model: {model_name!r}. "
            f"Supported: {list_supported_models()}. "
            "Add an entry to MODELS to support a new (CLI, model, thinking) triple."
        )
    return MODELS[model_name]


# --------------------------------------------------------------------------- #
# Adapter                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, kw_only=True)
class AdapterResult:
    cli: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: list[str]
    timed_out: bool = False
    executable_not_found: bool = False
    classification: Classification | None = None
    turn_lifecycle: list[dict[str, Any]] | None = None
    final_turn_status: str | None = None
    token_usage: dict[str, int] | None = None
    error_info: dict[str, Any] | None = None
    idle_classification: str | None = None
    run_identity: dict[str, Any] | None = None
    # The subprocess adapter normalizes exit_code to zero only for a tightly
    # bounded, CLI-specific post-completion failure. Preserve the raw process
    # status separately so the non-zero exit is never erased.
    process_exit_code: int | None = None
    expected_deliverable_path: str | None = None
    produced_expected_deliverable: bool = False


@runtime_checkable
class Adapter(Protocol):
    """Run a one-shot agent invocation.

    The implementation chooses the runtime (subprocess, ACP client,
    in-process SDK call, etc.); the dispatcher only sees AdapterResult.
    Implementations MUST handle their own timeouts and translate
    runtime-specific failures into AdapterResult fields rather than raising.
    """

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        expected_deliverable_path: Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        idle_timeout_seconds: int = DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS,
        extra_env: dict[str, str] | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> AdapterResult: ...


@runtime_checkable
class Heartbeat(Protocol):
    def beat(self) -> None: ...


@runtime_checkable
class CodexClient(Protocol):
    """Codex-only runtime seam for subprocess and app-server implementations."""

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        command: list[str],
        timeout_seconds: int,
        idle_timeout_seconds: int,
        extra_env: dict[str, str] | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> AdapterResult: ...


class SubprocessCodexClient:
    """Current P1 `codex exec --json` implementation behind CodexClient."""

    def __init__(self, *, cli: str) -> None:
        self.cli = cli

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        command: list[str],
        timeout_seconds: int,
        idle_timeout_seconds: int,
        extra_env: dict[str, str] | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> AdapterResult:
        return _run_codex_json_command(
            cli=self.cli,
            prompt=prompt,
            cwd=cwd,
            command=command,
            timeout_seconds=timeout_seconds,
            idle_timeout_seconds=idle_timeout_seconds,
            extra_env=extra_env,
            heartbeat=heartbeat,
        )


class AppServerCodexClient:
    """Phase-A seam for the future `codex app-server` runtime."""

    def __init__(self, *, cli: str) -> None:
        self.cli = cli

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        command: list[str],
        timeout_seconds: int,
        idle_timeout_seconds: int,
        extra_env: dict[str, str] | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> AdapterResult:
        del prompt, cwd, command, timeout_seconds, idle_timeout_seconds, extra_env, heartbeat
        started = time.monotonic()
        return _with_classification(
            AdapterResult(
                cli=self.cli,
                exit_code=1,
                stdout="",
                stderr=(
                    "CODEX_RUNTIME_MODE=app_server selected, but the codex "
                    "app-server runtime is a Phase A adapter seam only. "
                    "Implement daemon supervision and session lifecycle in Phase B/C.\n"
                ),
                duration_seconds=time.monotonic() - started,
                command=["codex", "app-server"],
            )
        )


_CURRENT_HEARTBEAT: contextvars.ContextVar[Heartbeat | None] = contextvars.ContextVar(
    "atlas_dispatch_adapter_heartbeat",
    default=None,
)


@contextlib.contextmanager
def heartbeat_context(heartbeat: Heartbeat | None):
    token = _CURRENT_HEARTBEAT.set(heartbeat)
    try:
        yield
    finally:
        _CURRENT_HEARTBEAT.reset(token)


def _effective_heartbeat(heartbeat: Heartbeat | None) -> Heartbeat | None:
    return heartbeat if heartbeat is not None else _CURRENT_HEARTBEAT.get()


def _load_cli_credentials_env(
    definition: CLIDefinition | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Lift a CLI's provider credentials out of its dotenv file.

    Returns the (env var -> value) mapping plus a provenance record that names
    the file and the KEYS ONLY. Values are never recorded, and a missing file
    is not an error here: it surfaces later as the CLI's own auth_required
    classification, which carries auth_setup_hint and is the actionable form.
    """
    provenance: dict[str, Any] = {"configured": False}
    if definition is None or not definition.credentials_file:
        return {}, provenance

    path = Path(definition.credentials_file).expanduser()
    provenance = {
        "configured": True,
        "path": str(path),
        "exists": path.is_file(),
        "loaded_env_keys": [],
        "missing_source_keys": [],
    }
    if not path.is_file():
        return {}, provenance

    parsed: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip().strip('"').strip("'")

    allowed = set(definition.env_allowlist)
    resolved: dict[str, str] = {}
    for source_key, target_key in definition.credentials_env_map:
        if target_key not in allowed:
            # The allowlist is the single statement of what may reach a CLI.
            # A mapping that writes outside it would smuggle an env var past
            # the policy the identity record claims to describe.
            raise ValueError(
                f"CLI {definition.name!r} maps credential {source_key!r} to "
                f"{target_key!r}, which is not in its env_allowlist"
            )
        value = parsed.get(source_key)
        if value:
            resolved[target_key] = value
        else:
            provenance["missing_source_keys"].append(source_key)

    provenance["loaded_env_keys"] = sorted(resolved)
    return resolved, provenance


def _prepare_cli_subprocess_environment(
    *,
    cli: str,
    cwd: Path,
    command: list[str],
    extra_env: Mapping[str, str] | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Build the explicit environment for one dispatched CLI subprocess.

    This is deliberately not used by acceptance, verification, provenance,
    or dispatcher git subprocesses. ``extra_env`` is a narrow internal seam,
    not a way for callers to bypass the supported CLI policy.
    """
    definition = CLIS.get(cli.lower())
    cli_keys = definition.env_allowlist if definition is not None else ()
    allowed_overrides = {*cli_keys, "UV_CACHE_DIR"}
    unexpected = set(extra_env or ()) - allowed_overrides
    if unexpected:
        names = ", ".join(sorted(unexpected))
        raise ValueError(f"unsupported CLI environment override(s): {names}")

    selected_keys = (*CLI_COMMON_ENV_ALLOWLIST, *cli_keys)
    process_env = {
        key: os.environ[key]
        for key in selected_keys
        if key in os.environ
    }
    credentials_env, credentials_provenance = _load_cli_credentials_env(definition)
    process_env.update(credentials_env)
    if extra_env:
        # An explicit caller override still wins over the credentials file.
        process_env.update(extra_env)

    executable_path = _resolve_cli_executable(command, cwd=cwd)
    process_env["PATH"] = _sanitized_cli_path(executable_path)
    process_env.update(CLI_ISOLATION_ENV)
    process_env["UV_CACHE_DIR"] = str(
        (extra_env or {}).get(
            "UV_CACHE_DIR",
            (cwd / ".atlas-dispatch" / "uv-cache").resolve(),
        )
    )

    forwarded_cli_keys = sorted(key for key in cli_keys if key in process_env)
    identity = {
        "schema_version": 1,
        "policy_version": CLI_ENV_POLICY_VERSION,
        "cli": cli,
        "selected_env_keys": sorted(process_env),
        "forwarded_cli_env_keys": forwarded_cli_keys,
        "executable": _executable_identity(executable_path),
        "isolation": dict(CLI_ISOLATION_ENV),
        "uv_cache_path": process_env["UV_CACHE_DIR"],
        "credentials_file": credentials_provenance,
    }
    return process_env, identity


def _resolve_cli_executable(command: list[str], *, cwd: Path) -> Path | None:
    if not command:
        return None
    candidate = Path(command[0]).expanduser()
    if candidate.is_absolute():
        return candidate.absolute()
    if candidate.parent != Path("."):
        return (cwd / candidate).absolute()
    resolved = shutil.which(command[0], path=_sanitized_cli_path(None))
    return Path(resolved).absolute() if resolved else None


def _sanitized_cli_path(executable_path: Path | None) -> str:
    """Return a stable PATH without copying arbitrary ambient entries."""
    candidates: list[Path] = []
    if executable_path is not None:
        candidates.extend(
            [executable_path.parent, executable_path.resolve(strict=False).parent]
        )
    home = Path(os.environ.get("HOME", "")).expanduser()
    if str(home) not in {"", "."}:
        candidates.extend(
            [
                home / ".local" / "bin",
                home / ".npm-global" / "bin",
                home / ".cargo" / "bin",
                home / ".bun" / "bin",
                home / "Library" / "pnpm",
            ]
        )
    candidates.extend(
        [
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
        ]
    )
    unique: list[str] = []
    for candidate in candidates:
        value = str(candidate)
        if value not in unique:
            unique.append(value)
    return os.pathsep.join(unique)


def _executable_identity(executable_path: Path | None) -> dict[str, str | None]:
    if executable_path is None:
        return {"resolved_path": None, "sha256": None}
    resolved = executable_path.resolve(strict=False)
    digest: str | None = None
    try:
        hasher = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    except OSError:
        pass
    return {"resolved_path": str(resolved), "sha256": digest}


@dataclass(frozen=True)
class _ExpectedDeliverable:
    workspace: Path
    path: Path
    before_fingerprint: str | None


def _regular_file_fingerprint(path: Path, *, workspace: Path) -> str | None:
    """Hash a regular file only when its resolved target stays in the workspace."""
    try:
        resolved_path = path.resolve(strict=True)
        resolved_workspace = workspace.resolve(strict=True)
        if not resolved_path.is_relative_to(resolved_workspace) or not path.is_file():
            return None
        hasher = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        return hasher.hexdigest()
    except OSError:
        return None


def _capture_expected_deliverable(
    expected_deliverable_path: Path | None,
    *,
    cwd: Path,
) -> _ExpectedDeliverable | None:
    """Snapshot a structurally supplied deliverable within the run workspace."""
    if expected_deliverable_path is None:
        return None

    workspace = cwd.resolve()
    declared_path = expected_deliverable_path.expanduser()
    if not declared_path.is_absolute():
        return None

    expected_path = declared_path.resolve()
    if not expected_path.is_relative_to(workspace):
        return None

    return _ExpectedDeliverable(
        workspace=workspace,
        path=expected_path,
        before_fingerprint=_regular_file_fingerprint(
            expected_path,
            workspace=workspace,
        ),
    )


def _deliverable_was_produced(expected: _ExpectedDeliverable | None) -> bool:
    if expected is None:
        return False
    after_fingerprint = _regular_file_fingerprint(
        expected.path,
        workspace=expected.workspace,
    )
    return (
        after_fingerprint is not None
        and after_fingerprint != expected.before_fingerprint
    )


def _finalize_cli_subprocess_result(
    result: AdapterResult,
    run_identity: dict[str, Any],
    process_env: Mapping[str, str],
    *,
    expected_deliverable: _ExpectedDeliverable | None = None,
) -> AdapterResult:
    result = replace(
        result,
        expected_deliverable_path=(
            str(expected_deliverable.path)
            if expected_deliverable is not None
            else None
        ),
        produced_expected_deliverable=_deliverable_was_produced(
            expected_deliverable
        ),
    )
    definition = CLIS.get(result.cli.lower())
    cli_keys = definition.env_allowlist if definition is not None else ()
    secret_key = re.compile(
        r"(?i)(?:api[-_]?key|authorization|bearer|password|secret|token)"
    )
    secret_values = [
        (key, process_env[key])
        for key in cli_keys
        if key in process_env and process_env[key] and secret_key.search(key)
    ]
    sanitized = result
    for key, value in sorted(secret_values, key=lambda item: len(item[1]), reverse=True):
        marker = f"<redacted-env:{key}>"
        sanitized = replace(
            sanitized,
            stdout=sanitized.stdout.replace(value, marker),
            stderr=sanitized.stderr.replace(value, marker),
            command=[token.replace(value, marker) for token in sanitized.command],
        )
    classified = _with_classification(
        replace(sanitized, run_identity=run_identity)
    )
    classification = classified.classification
    if (
        classified.exit_code != 0
        and classification is not None
        and classification.kind is DispatchErrorKind.SUCCESS
        and classification.observed_exit_code == classified.exit_code
    ):
        raw_exit_code = classified.exit_code
        recorded_identity = dict(classified.run_identity or {})
        recorded_identity["process_exit_code"] = raw_exit_code
        return replace(
            classified,
            exit_code=0,
            process_exit_code=raw_exit_code,
            run_identity=recorded_identity,
        )
    return classified


def render_command(
    *,
    cli: str,
    cwd: Path,
    model: str | None = None,
    reasoning_effort: str | None = None,
    prompt: str | None = None,
) -> list[str]:
    """Resolve argv for `cli` with supported placeholders filled in.

    `ATLAS_DISPATCH_<CLI>_CMD` env var, if set, overrides the builtin
    template entirely. Its value is parsed with shlex and undergoes the
    same placeholder substitution.
    """
    cli_lower = cli.lower()
    definition = CLIS.get(cli_lower)

    env_override = _cli_env_override_value(cli_lower)
    if env_override:
        argv = shlex.split(env_override)
    elif definition is not None:
        argv = list(definition.argv_template)
    else:
        argv = [cli, "-"]

    rendered: list[str] = []
    for token in argv:
        rendered_token = (
            token.replace("{cwd}", str(cwd))
            .replace("{model}", model or "")
            .replace("{reasoning_effort}", reasoning_effort or "")
            .replace("{prompt}", prompt or "")
            .replace("{kimi_session}", _default_kimi_session_id(cwd))
        )
        if rendered_token:
            rendered.append(rendered_token)

    if rendered:
        executable = shutil.which(rendered[0], path=_sanitized_cli_path(None))
        if executable:
            rendered[0] = executable
    return rendered


def _command_declares_model(command: Sequence[str]) -> bool:
    """True if the rendered argv carries an explicit model selection flag.

    TOKEN-EXACT, not substring: a whole argv element must equal `-m` or
    `--model`, or start with `--model=`. That matters because the prompt is a
    SINGLE argv element -- a review prompt that merely discusses `--model` (this
    docstring, for instance) must not trip the guard. A substring scan would
    misfire on exactly the reviews this branch exists to protect.

    Within that, it is deliberately permissive about flag SHAPE: it guards a
    fail-closed path, so a FALSE POSITIVE costs one honest
    `unknown-effective-model` gate failure, while a FALSE NEGATIVE lets a
    GPT-family fallback be recorded as a Gemini review. Those costs are not
    symmetric.
    """

    for arg in command or ():
        token = str(arg)
        if token == "-m" or token == "--model" or token.startswith("--model="):
            return True
    return False


def resolve_invocation(
    *,
    cli: str,
    cwd: Path,
    model: str | None,
    reasoning_effort: str | None,
    requested_model: str | None,
    resolved_via: str,
    prompt: str | None = None,
) -> ResolvedInvocation:
    """Resolve argv and durable review-model evidence from the same snapshot.

    Registry contents are consulted only while resolving the invocation.  The
    returned evidence must travel with the run artifact; historical consumers
    must never re-resolve it from today's mutable registry.
    """

    command = render_command(
        cli=cli,
        cwd=cwd,
        model=model,
        reasoning_effort=reasoning_effort,
        prompt=prompt,
    )
    effective_cli = Path(command[0]).name if command else cli.lower()

    if cli.lower() == "gemini" and effective_cli == "agy":
        # The constant is only true while the invocation carries NO model flag.
        # agy 1.1.7 SILENTLY falls back to GPT-OSS 120B for every explicit
        # --model value (see the argv_template comment above), so a model flag
        # means a GPT-FAMILY model ran -- the BUILDER's own family -- while this
        # record would still read "gemini/3.6-flash-agy".
        #
        # An invariant carried only by a COMMENT and the template's shape fails
        # silently: if this branch merely DECLARED the model without OBSERVING
        # the command, a model flag added later would be recorded as a Gemini
        # review while a GPT-family model ran. That failure points toward "good"
        # and is correlated with the property being measured (independence
        # from the builder). Observing the command turns the comment into a
        # branch, and a downstream consumer can refuse unknown-effective-model
        # loudly instead of recording a confident wrong answer.
        if _command_declares_model(command):
            effective_model = UNKNOWN_EFFECTIVE_MODEL
            resolution_source = "agy-model-flag-present-runtime-unverifiable"
        else:
            effective_model = ANTIGRAVITY_EFFECTIVE_MODEL
            resolution_source = "fixed-runtime-declaration"
    elif model and model in command:
        if resolved_via.startswith("model_registry:") and requested_model:
            effective_model = requested_model
            resolution_source = "resolved-registry-argv"
        else:
            effective_model = f"{cli.lower()}/{model}"
            if reasoning_effort:
                effective_model = f"{effective_model}-{reasoning_effort}"
            resolution_source = "explicit-argv"
    else:
        effective_model = UNKNOWN_EFFECTIVE_MODEL
        resolution_source = (
            "unread-runtime-model-not-invocation"
            if model
            else "unread-runtime-model"
        )

    return ResolvedInvocation(
        command=command,
        review_model_evidence=ReviewModelEvidence(
            schema=REVIEW_MODEL_EVIDENCE_SCHEMA,
            schema_version=REVIEW_MODEL_EVIDENCE_SCHEMA_VERSION,
            effective_cli=effective_cli,
            effective_model=effective_model,
            model_resolution_source=resolution_source,
            requested_model=(
                requested_model
                if requested_model and requested_model != effective_model
                else None
            ),
        ),
    )


def _cli_env_override_name(cli: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", cli).strip("_").upper()
    return f"ATLAS_DISPATCH_{normalized}_CMD"


def _legacy_cli_env_override_name(cli: str) -> str:
    return f"ATLAS_DISPATCH_{cli.upper()}_CMD"


def _cli_env_override_value(
    cli: str, *, environ: Mapping[str, str] | None = None
) -> str | None:
    env = os.environ if environ is None else environ
    primary = _cli_env_override_name(cli)
    value = env.get(primary)
    if value is not None:
        return value
    legacy = _legacy_cli_env_override_name(cli)
    if legacy != primary:
        return env.get(legacy)
    return None


def inject_mcp_config(cli: str, command: list[str], config_path: Path) -> list[str]:
    """Return argv with a per-invocation MCP config flag for flag-based CLIs."""
    cli_lower = cli.lower()
    command_copy = list(command)

    if _is_kimi_cli(cli_lower):
        return _insert_after_executable(
            command_copy, ["--mcp-config-file", str(config_path)]
        )
    if cli_lower == "claude":
        return _insert_after_executable(command_copy, ["--mcp-config", str(config_path)])
    if cli_lower == "codex":
        # Codex reads MCP servers from CODEX_HOME/config.toml; dispatcher.py
        # scopes CODEX_HOME per worktree via run_cli(extra_env=...).
        return command_copy
    if cli_lower == "gemini":
        # Gemini reads workspace-scoped .gemini/settings.json automatically;
        # there is no argv flag to inject for the per-task config.
        return command_copy
    return command_copy


def _insert_after_executable(command: list[str], tokens: list[str]) -> list[str]:
    insert_at = 1 if command else 0
    return command[:insert_at] + tokens + command[insert_at:]


def _is_kimi_cli(cli_lower: str) -> bool:
    return cli_lower in {"kimi", "kimi-streaming"}


def _default_kimi_session_id(cwd: Path) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", cwd.name).strip("-")
    resolved = str(cwd.resolve(strict=False))
    path_hash = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    return f"atlas-dispatch-{slug or 'worktree'}-{path_hash}"


def _is_codex_json_command(cli: str, command: list[str]) -> bool:
    if cli.lower() != "codex":
        return False
    return "--json" in command or "--experimental-json" in command


def _resolve_codex_runtime_mode(
    env: Mapping[str, str] | None = None,
) -> CodexRuntimeMode:
    source = env if env is not None else os.environ
    raw = source.get(CODEX_RUNTIME_MODE_ENV, CodexRuntimeMode.SUBPROCESS.value)
    normalized = raw.strip().lower().replace("-", "_")
    if not normalized:
        normalized = CodexRuntimeMode.SUBPROCESS.value
    try:
        return CodexRuntimeMode(normalized)
    except ValueError as exc:
        allowed = ", ".join(mode.value for mode in CodexRuntimeMode)
        raise ValueError(
            f"invalid {CODEX_RUNTIME_MODE_ENV}={raw!r}; expected one of: {allowed}"
        ) from exc


def _codex_client_for_mode(mode: CodexRuntimeMode, *, cli: str) -> CodexClient:
    if mode is CodexRuntimeMode.SUBPROCESS:
        return SubprocessCodexClient(cli=cli)
    if mode is CodexRuntimeMode.APP_SERVER:
        return AppServerCodexClient(cli=cli)
    raise AssertionError(f"unhandled Codex runtime mode: {mode}")


def _invalid_codex_runtime_mode_result(
    *,
    cli: str,
    command: list[str],
    started: float,
    error: ValueError,
) -> AdapterResult:
    return _with_classification(
        AdapterResult(
            cli=cli,
            exit_code=1,
            stdout="",
            stderr=str(error) + "\n",
            duration_seconds=time.monotonic() - started,
            command=command,
        )
    )


def _run_codex_client(
    *,
    cli: str,
    prompt: str,
    cwd: Path,
    command: list[str],
    timeout_seconds: int,
    idle_timeout_seconds: int,
    extra_env: dict[str, str] | None = None,
    heartbeat: Heartbeat | None = None,
) -> AdapterResult:
    started = time.monotonic()
    try:
        mode = _resolve_codex_runtime_mode()
    except ValueError as exc:
        return _invalid_codex_runtime_mode_result(
            cli=cli,
            command=command,
            started=started,
            error=exc,
        )
    client = _codex_client_for_mode(mode, cli=cli)
    return client.run(
        prompt=prompt,
        cwd=cwd,
        command=command,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        extra_env=extra_env,
        heartbeat=heartbeat,
    )


class SubprocessAdapter:
    """Adapter implementation backed by a local CLI subprocess."""

    def __init__(self, *, cli: str, definition: CLIDefinition | None = None) -> None:
        self.cli = cli
        self.definition = definition if definition is not None else CLIS.get(cli.lower())

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        expected_deliverable_path: Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        idle_timeout_seconds: int = DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS,
        extra_env: dict[str, str] | None = None,
        heartbeat: Heartbeat | None = None,
    ) -> AdapterResult:
        heartbeat = _effective_heartbeat(heartbeat)
        reads_stdin = self.definition.reads_prompt_from_stdin if self.definition else True
        command = render_command(
            cli=self.cli,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            prompt="" if reads_stdin else prompt,
        )
        if self.definition and self.definition.adapter_mode == KIMI_WIRE_MODE:
            return _run_kimi_wire_command(
                cli=self.cli,
                prompt=prompt,
                cwd=cwd,
                command=command,
                timeout_seconds=timeout_seconds,
                extra_env=extra_env,
            )
        if _is_codex_json_command(self.cli, command):
            return _run_codex_client(
                cli=self.cli,
                prompt=prompt if reads_stdin else "",
                cwd=cwd,
                command=command,
                timeout_seconds=timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                extra_env=extra_env,
                heartbeat=heartbeat,
            )
        expected_deliverable = _capture_expected_deliverable(
            expected_deliverable_path,
            cwd=cwd,
        )
        return _run_subprocess_command(
            cli=self.cli,
            prompt=prompt if reads_stdin else "",
            cwd=cwd,
            command=command,
            timeout_seconds=timeout_seconds,
            extra_env=extra_env,
            heartbeat=heartbeat,
            expected_deliverable=expected_deliverable,
        )


def _run_kimi_wire_command(
    *,
    cli: str,
    prompt: str,
    cwd: Path,
    command: list[str],
    timeout_seconds: int,
    extra_env: dict[str, str] | None = None,
) -> AdapterResult:
    command = list(command)
    process_env, run_identity = _prepare_cli_subprocess_environment(
        cli=cli,
        cwd=cwd,
        command=command,
        extra_env=extra_env,
    )

    started = time.monotonic()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    prompt_sent = False
    prompt_done = False
    prompt_status: str | None = None
    exit_code = 1

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=process_env,
        )
    except FileNotFoundError as exc:
        result = AdapterResult(
            cli=cli,
            exit_code=-2,
            stdout="",
            stderr=f"executable not found: {exc}",
            duration_seconds=time.monotonic() - started,
            command=command,
            executable_not_found=True,
        )
        return _finalize_cli_subprocess_result(result, run_identity, process_env)

    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    stdout_reader = _start_stream_reader("stdout", process.stdout, events)
    stderr_reader = _start_stream_reader("stderr", process.stderr, events)

    _write_kimi_wire_json(process, _kimi_wire_initialize_message())

    try:
        process_exited_before_protocol_done = False
        while True:
            if time.monotonic() - started > timeout_seconds:
                _write_kimi_wire_json(process, _kimi_wire_cancel_message())
                _terminate_process(process)
                _drain_kimi_wire_events(events, stdout_parts, stderr_parts)
                result = AdapterResult(
                    cli=cli,
                    exit_code=-1,
                    stdout=_strip_benign_process_noise("".join(stdout_parts)),
                    stderr=_strip_benign_process_noise("".join(stderr_parts)),
                    duration_seconds=time.monotonic() - started,
                    command=command,
                    timed_out=True,
                )
                return _finalize_cli_subprocess_result(result, run_identity, process_env)

            try:
                stream_name, line = events.get(timeout=KIMI_WIRE_QUEUE_POLL_SECONDS)
            except queue.Empty:
                if process.poll() is not None:
                    process_exited_before_protocol_done = True
                    break
                continue

            if line is None:
                if process.poll() is not None and events.empty():
                    process_exited_before_protocol_done = True
                    break
                continue

            if stream_name == "stderr":
                stderr_parts.append(line)
                continue

            msg = _parse_kimi_wire_line(line)
            if msg is None:
                stderr_parts.append(f"invalid kimi wire json: {line}")
                continue

            if _is_kimi_wire_init_response(msg):
                if "error" in msg:
                    stderr_parts.append(_kimi_wire_error_text(msg))
                    break
                _write_kimi_wire_json(process, _kimi_wire_prompt_message(prompt))
                prompt_sent = True
                continue

            if _is_kimi_wire_prompt_response(msg):
                if "error" in msg:
                    stderr_parts.append(_kimi_wire_error_text(msg))
                    prompt_status = "error"
                else:
                    result = msg.get("result")
                    if isinstance(result, dict):
                        prompt_status = str(result.get("status") or "")
                    else:
                        prompt_status = ""
                prompt_done = True
                break

            if msg.get("method") == "event":
                stdout_parts.append(_kimi_wire_event_text(msg))
                continue

            if msg.get("method") == "request":
                _answer_kimi_wire_request(process, msg)

        if process_exited_before_protocol_done:
            # Option (b): on the early process-exit path, wait briefly for both
            # reader threads to publish EOF and any trailing stderr before
            # checking unsupported --wire diagnostics. Healthy streaming runs
            # finish through protocol responses and skip this drain.
            _drain_kimi_wire_exit_events(
                events,
                stdout_parts,
                stderr_parts,
                (stdout_reader, stderr_reader),
            )

        if prompt_done and prompt_status == "finished":
            exit_code = 0
        elif prompt_done:
            exit_code = 1
            if prompt_status:
                stderr_parts.append(f"kimi wire prompt status: {prompt_status}\n")
        elif not prompt_sent:
            exit_code = process.poll()
            if exit_code is None:
                exit_code = 1
            stderr_text = "".join(stderr_parts)
            if _kimi_wire_unsupported(stderr_text):
                stderr_parts.append(
                    "requested Kimi streaming mode, but this Kimi CLI does not "
                    "support `--wire`; upgrade Kimi CLI or use a non-streaming "
                    "Kimi model entry.\n"
                )
            stderr_parts.append("kimi wire server exited before initialize completed\n")
        else:
            exit_code = process.poll()
            if exit_code is None:
                exit_code = 1
            stderr_parts.append("kimi wire server exited before prompt completed\n")

        _close_process_stdin(process)
        waited_code = _wait_process(process)
        if waited_code is not None and exit_code == 0:
            exit_code = waited_code

        result = AdapterResult(
            cli=cli,
            exit_code=exit_code,
            stdout=_strip_benign_process_noise("".join(stdout_parts)),
            stderr=_strip_benign_process_noise("".join(stderr_parts)),
            duration_seconds=time.monotonic() - started,
            command=command,
        )
        return _finalize_cli_subprocess_result(result, run_identity, process_env)
    finally:
        if process.poll() is None:
            _terminate_process(process)


class _CrossProcessRunMarker:
    """A file in the worktree that says "this run is still alive", for OTHER processes.

    The in-process `Heartbeat` is invisible to a supervisor running in a
    different process, which can only see the filesystem. The tmp files that
    `_run_subprocess_command` streams CLI output into double as that signal
    (a supervisor can glob for `cli.stdout.*.tmp`).

    `codex exec --json` does NOT take that path: it streams events over pipes and
    writes no tmp files, so a healthy codex build would produce no liveness
    evidence at all, and a supervisor would read the absence of evidence as
    evidence of a stall. This marker gives that path the same signal the others
    have.
    """

    #: Touching on every stream event would be thousands of syscalls a minute for no
    #: extra signal; the consumer's freshness window is minutes wide.
    touch_interval_seconds: float = 15.0

    def __init__(self, cwd: Path) -> None:
        self.path = cwd / ".atlas-dispatch" / f"cli.stream.{os.getpid()}.{time.monotonic_ns()}.tmp"
        self._last_touch = 0.0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch()
        except OSError:
            self.path = None  # type: ignore[assignment]

    def beat(self) -> None:
        if self.path is None:
            return
        now = time.monotonic()
        if now - self._last_touch < self.touch_interval_seconds:
            return
        self._last_touch = now
        try:
            self.path.touch()
        except OSError:
            self.path = None  # type: ignore[assignment]

    def close(self) -> None:
        if self.path is None:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
        self.path = None  # type: ignore[assignment]


def _run_codex_json_command(
    *,
    cli: str,
    prompt: str,
    cwd: Path,
    command: list[str],
    timeout_seconds: int,
    idle_timeout_seconds: int,
    extra_env: dict[str, str] | None = None,
    heartbeat: Heartbeat | None = None,
) -> AdapterResult:
    command = list(command)
    process_env, run_identity = _prepare_cli_subprocess_environment(
        cli=cli,
        cwd=cwd,
        command=command,
        extra_env=extra_env,
    )

    started = time.monotonic()
    state = CodexLifecycleState()
    state.last_event_at = started
    lifecycle: list[dict[str, Any]] = []
    final_messages: list[str] = []
    stderr_parts: list[str] = []
    termination_reason: str | None = None
    timed_out = False

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=process_env,
        )
    except FileNotFoundError as exc:
        result = AdapterResult(
            cli=cli,
            exit_code=-2,
            stdout="",
            stderr=f"executable not found: {exc}",
            duration_seconds=time.monotonic() - started,
            command=command,
            executable_not_found=True,
        )
        return _finalize_cli_subprocess_result(result, run_identity, process_env)

    events: queue.Queue[tuple[str, str | None]] = queue.Queue()
    _start_stream_reader("stdout", process.stdout, events)
    _start_stream_reader("stderr", process.stderr, events)

    stdin = process.stdin
    if stdin is not None:
        try:
            stdin.write(prompt)
            stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
    _close_process_stdin(process)

    run_marker = _CrossProcessRunMarker(cwd)

    open_streams = {"stdout", "stderr"}
    while open_streams:
        if heartbeat is not None:
            heartbeat.beat()
        run_marker.beat()
        now = time.monotonic()
        if termination_reason is None and now - started > timeout_seconds:
            termination_reason = "timeout"
            timed_out = True
            _terminate_process(process)
        if termination_reason is None:
            terminal_state = classify_terminal_state(
                state,
                now=now,
                idle_timeout_seconds=idle_timeout_seconds,
            )
            if terminal_state == "stalled":
                termination_reason = "stalled"
                timed_out = True
                _terminate_process(process)
            elif terminal_state == "approval_blocked":
                termination_reason = "approval_blocked"
                stderr_parts.append(
                    "warning: codex emitted an approval-required event; "
                    "atlas-dispatch does not auto-approve Codex requests in "
                    "exec-json mode.\n"
                )
                _terminate_process(process)

        try:
            stream_name, line = events.get(timeout=0.1)
        except queue.Empty:
            continue

        if line is None:
            open_streams.discard(stream_name)
            continue

        if stream_name == "stderr":
            stderr_parts.append(line)
            continue

        payload = decode_jsonl_line(line)
        if payload is None:
            stderr_parts.append(f"invalid codex json line: {line}")
            continue

        timestamp_utc = utc_timestamp()
        lifecycle.append(
            compact_lifecycle_record(payload, timestamp_utc=timestamp_utc)
        )
        typed_events = apply_codex_event(
            payload,
            state,
            received_at=time.monotonic(),
            timestamp_utc=timestamp_utc,
        )
        for typed_event in typed_events:
            if isinstance(typed_event, ItemCompleted):
                item = typed_event.item
                if item.get("type") == "agent_message":
                    text = str(item.get("text") or "")
                    if text:
                        final_messages.append(text)

    run_marker.close()
    waited_code = _wait_process(process)
    if termination_reason in {"timeout", "stalled", "approval_blocked"}:
        exit_code = -1
    elif waited_code is not None:
        exit_code = waited_code
    else:
        polled = process.poll()
        exit_code = int(polled) if polled is not None else 1

    final_turn_status = (
        state.current_turn_status
        if state.current_turn_status in {"completed", "failed", "interrupted"}
        else None
    )
    idle_classification = (
        termination_reason
        if termination_reason in {"stalled", "approval_blocked"}
        else None
    )
    result = AdapterResult(
        cli=cli,
        exit_code=exit_code,
        stdout=_strip_benign_process_noise("\n".join(final_messages)),
        stderr=_strip_benign_process_noise("".join(stderr_parts)),
        duration_seconds=time.monotonic() - started,
        command=command,
        timed_out=timed_out,
        turn_lifecycle=lifecycle or None,
        final_turn_status=final_turn_status,
        token_usage=state.accumulated_token_usage or None,
        error_info=state.error_info,
        idle_classification=idle_classification,
    )
    return _finalize_cli_subprocess_result(result, run_identity, process_env)


def _start_stream_reader(
    stream_name: str,
    stream: TextIO | None,
    events: queue.Queue[tuple[str, str | None]],
) -> threading.Thread:
    def _read() -> None:
        try:
            if stream is not None:
                for line in stream:
                    events.put((stream_name, line))
        finally:
            events.put((stream_name, None))

    thread = threading.Thread(target=_read, daemon=True)
    thread.start()
    return thread


def _write_kimi_wire_json(process: Any, payload: dict[str, Any]) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _kimi_wire_initialize_message() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": KIMI_WIRE_INIT_ID,
        "method": "initialize",
        "params": {
            "protocol_version": KIMI_WIRE_PROTOCOL_VERSION,
            "client": {"name": "atlas-dispatch"},
            "capabilities": {
                "supports_question": False,
                "supports_plan_mode": False,
            },
        },
    }


def _kimi_wire_prompt_message(prompt: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": KIMI_WIRE_PROMPT_ID,
        "method": "prompt",
        "params": {"user_input": prompt},
    }


def _kimi_wire_cancel_message() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": KIMI_WIRE_CANCEL_ID,
        "method": "cancel",
    }


def _parse_kimi_wire_line(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_kimi_wire_init_response(msg: dict[str, Any]) -> bool:
    return msg.get("id") == KIMI_WIRE_INIT_ID and "method" not in msg


def _is_kimi_wire_prompt_response(msg: dict[str, Any]) -> bool:
    return msg.get("id") == KIMI_WIRE_PROMPT_ID and "method" not in msg


def _kimi_wire_error_text(msg: dict[str, Any]) -> str:
    error = msg.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message") or "unknown kimi wire error"
        return f"kimi wire error {code}: {message}\n"
    return "kimi wire error: unknown error\n"


def _kimi_wire_unsupported(stderr_text: str) -> bool:
    lowered = stderr_text.lower()
    return "--wire" in lowered and (
        "no such option" in lowered
        or "unknown option" in lowered
        or "unrecognized option" in lowered
        or "unexpected argument" in lowered
    )


def _kimi_wire_event_text(msg: dict[str, Any]) -> str:
    params = msg.get("params")
    if not isinstance(params, dict):
        return ""

    event_type = params.get("type")
    payload = params.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if event_type == "TextPart":
        return str(payload.get("text") or "")
    if event_type == "ContentPart" and payload.get("type") == "text":
        return str(payload.get("text") or "")
    return ""


def _drain_kimi_wire_events(
    events: queue.Queue[tuple[str, str | None]],
    stdout_parts: list[str],
    stderr_parts: list[str],
) -> None:
    while True:
        try:
            stream_name, line = events.get_nowait()
        except queue.Empty:
            return

        if line is None:
            continue
        if stream_name == "stderr":
            stderr_parts.append(line)
            continue

        msg = _parse_kimi_wire_line(line)
        if msg is None:
            stderr_parts.append(f"invalid kimi wire json: {line}")
        elif msg.get("method") == "event":
            stdout_parts.append(_kimi_wire_event_text(msg))


def _drain_kimi_wire_exit_events(
    events: queue.Queue[tuple[str, str | None]],
    stdout_parts: list[str],
    stderr_parts: list[str],
    reader_threads: tuple[threading.Thread, threading.Thread],
) -> None:
    deadline = time.monotonic() + KIMI_WIRE_EXIT_DRAIN_SECONDS
    for thread in reader_threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    _drain_kimi_wire_events(events, stdout_parts, stderr_parts)


def _answer_kimi_wire_request(process: Any, msg: dict[str, Any]) -> None:
    request_id = str(msg.get("id") or "")
    params = msg.get("params")
    if not request_id or not isinstance(params, dict):
        return

    request_type = params.get("type")
    payload = params.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    nested_request_id = str(payload.get("id") or request_id)
    if request_type == "ApprovalRequest":
        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "request_id": nested_request_id,
                "response": "approve_for_session",
                "feedback": "",
            },
        }
    elif request_type == "QuestionRequest":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {"request_id": nested_request_id, "answers": {}},
        }
    elif request_type == "HookRequest":
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "request_id": nested_request_id,
                "action": "allow",
                "reason": "",
            },
        }
    else:
        response = {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32603,
                "message": (
                    "atlas-dispatch Kimi wire adapter cannot service this "
                    f"request type: {request_type}"
                ),
            },
        }
    _write_kimi_wire_json(process, response)


def _close_process_stdin(process: Any) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _wait_process(process: Any) -> int | None:
    try:
        return int(process.wait(timeout=5))
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        return None


def _terminate_process(process: Any) -> None:
    try:
        if process.poll() is None:
            process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
        except OSError:
            pass


def _run_subprocess_command(
    *,
    cli: str,
    prompt: str,
    cwd: Path,
    command: list[str],
    timeout_seconds: int,
    extra_env: dict[str, str] | None = None,
    heartbeat: Heartbeat | None = None,
    expected_deliverable: _ExpectedDeliverable | None = None,
) -> AdapterResult:
    command = list(command)
    process_env, run_identity = _prepare_cli_subprocess_environment(
        cli=cli,
        cwd=cwd,
        command=command,
        extra_env=extra_env,
    )

    started = time.monotonic()

    # Stream stdout/stderr to temp files instead of capture_output=True
    # so a long-running caller does not retain the full CLI output in RAM
    # for the entire duration of the subprocess.  After the process exits we
    # read the files back into strings (needed for persistence/classification),
    # but the peak RSS during the run is bounded to the OS pipe buffer.
    tmp_dir = cwd / ".atlas-dispatch"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = tmp_dir / f"cli.stdout.{os.getpid()}.{time.monotonic_ns()}.tmp"
    stderr_path = tmp_dir / f"cli.stderr.{os.getpid()}.{time.monotonic_ns()}.tmp"

    try:
        with stdout_path.open("w", encoding="utf-8") as out_f, \
             stderr_path.open("w", encoding="utf-8") as err_f:
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    text=True,
                    stdout=out_f,
                    stderr=err_f,
                    cwd=cwd,
                    env=process_env,
                )
                stdin = process.stdin
                if stdin is not None:
                    try:
                        stdin.write(prompt)
                        stdin.flush()
                    except (BrokenPipeError, OSError, ValueError):
                        pass
                _close_process_stdin(process)

                deadline = started + timeout_seconds
                while True:
                    remaining_seconds = deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        _terminate_process(process)
                        raise subprocess.TimeoutExpired(
                            cmd=command,
                            timeout=timeout_seconds,
                        )
                    try:
                        returncode = process.wait(
                            timeout=min(BEAT_INTERVAL_SECONDS, remaining_seconds)
                        )
                        break
                    except subprocess.TimeoutExpired:
                        if heartbeat is not None:
                            heartbeat.beat()
            except subprocess.TimeoutExpired:
                # The process was killed by _terminate_process on timeout; read
                # whatever was flushed to disk rather than in-memory buffers.
                out_f.flush()
                err_f.flush()
                stdout_text = _strip_benign_process_noise(
                    stdout_path.read_text(encoding="utf-8")
                    if stdout_path.exists()
                    else ""
                )
                stderr_text = _strip_benign_process_noise(
                    stderr_path.read_text(encoding="utf-8")
                    if stderr_path.exists()
                    else ""
                )
                result = AdapterResult(
                    cli=cli,
                    exit_code=-1,
                    stdout=stdout_text,
                    stderr=stderr_text,
                    duration_seconds=time.monotonic() - started,
                    command=command,
                    timed_out=True,
                )
                return _finalize_cli_subprocess_result(
                    result,
                    run_identity,
                    process_env,
                    expected_deliverable=expected_deliverable,
                )
            except FileNotFoundError as exc:
                result = AdapterResult(
                    cli=cli,
                    exit_code=-2,
                    stdout="",
                    stderr=f"executable not found: {exc}",
                    duration_seconds=time.monotonic() - started,
                    command=command,
                    executable_not_found=True,
                )
                return _finalize_cli_subprocess_result(
                    result,
                    run_identity,
                    process_env,
                    expected_deliverable=expected_deliverable,
                )

            out_f.flush()
            err_f.flush()
            stdout_text = _strip_benign_process_noise(stdout_path.read_text(encoding="utf-8"))
            stderr_text = _strip_benign_process_noise(stderr_path.read_text(encoding="utf-8"))
            result = AdapterResult(
                cli=cli,
                exit_code=returncode,
                stdout=stdout_text,
                stderr=stderr_text,
                duration_seconds=time.monotonic() - started,
                command=command,
            )
            return _finalize_cli_subprocess_result(
                result,
                run_identity,
                process_env,
                expected_deliverable=expected_deliverable,
            )
    finally:
        # Clean up temp files so disk usage does not grow unbounded in
        # long-lived callers.
        for p in (stdout_path, stderr_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _strip_benign_process_noise(text: str) -> str:
    """Remove known OS/runtime noise that obscures real CLI output."""
    return BENIGN_MACOS_MALLOC_WARNING_RE.sub("", text)


def run_cli(
    *,
    cli: str,
    prompt: str,
    cwd: Path,
    expected_deliverable_path: Path | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    idle_timeout_seconds: int = DEFAULT_CODEX_IDLE_TIMEOUT_SECONDS,
    extra_env: dict[str, str] | None = None,
    command: list[str] | None = None,
    heartbeat: Heartbeat | None = None,
) -> AdapterResult:
    """Backwards-compatible wrapper kept for v0.1 callers.

    New code should construct an Adapter directly.
    """
    heartbeat = _effective_heartbeat(heartbeat)
    if command is not None:
        definition = CLIS.get(cli.lower())
        reads_stdin = definition.reads_prompt_from_stdin if definition else True
        rendered_command = [
            rendered_token
            for token in command
            if (
                rendered_token := token.replace("{cwd}", str(cwd))
                .replace("{model}", model or "")
                .replace("{reasoning_effort}", reasoning_effort or "")
                .replace("{prompt}", "" if reads_stdin else prompt)
                .replace("{kimi_session}", _default_kimi_session_id(cwd))
            )
        ]
        if definition and definition.adapter_mode == KIMI_WIRE_MODE:
            return _run_kimi_wire_command(
                cli=cli,
                prompt=prompt,
                cwd=cwd,
                command=rendered_command,
                timeout_seconds=timeout_seconds,
                extra_env=extra_env,
            )
        if _is_codex_json_command(cli, rendered_command):
            return _run_codex_client(
                cli=cli,
                prompt=prompt if reads_stdin else "",
                cwd=cwd,
                command=rendered_command,
                timeout_seconds=timeout_seconds,
                idle_timeout_seconds=idle_timeout_seconds,
                extra_env=extra_env,
                heartbeat=heartbeat,
            )
        expected_deliverable = _capture_expected_deliverable(
            expected_deliverable_path,
            cwd=cwd,
        )
        return _run_subprocess_command(
            cli=cli,
            prompt=prompt if reads_stdin else "",
            cwd=cwd,
            command=rendered_command,
            timeout_seconds=timeout_seconds,
            extra_env=extra_env,
            heartbeat=heartbeat,
            expected_deliverable=expected_deliverable,
        )

    return SubprocessAdapter(cli=cli).run(
        prompt=prompt,
        cwd=cwd,
        expected_deliverable_path=expected_deliverable_path,
        model=model,
        reasoning_effort=reasoning_effort,
        timeout_seconds=timeout_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
        extra_env=extra_env,
        heartbeat=heartbeat,
    )


def _with_classification(result: AdapterResult) -> AdapterResult:
    classification = classify_result(result)
    return replace(result, classification=classification)


# --------------------------------------------------------------------------- #
# Result classification                                                       #
# --------------------------------------------------------------------------- #


def classify_result(result: AdapterResult) -> Classification:
    """Inspect the result and return a Classification.

    Order matters. Codex lifecycle states are surfaced before generic
    subprocess signals, followed by timeout > executable not found >
    auth required > quota exhausted > rate limited > overloaded > scoped
    refusal > generic refusal > exit nonzero > model selection error > no
    output > success.
    """
    cli = result.cli.lower()
    definition = CLIS.get(cli)

    rate_patterns = list(GENERIC_RATE_LIMIT_PATTERNS)
    quota_patterns: tuple[str, ...] = ()
    if definition is not None:
        rate_patterns.extend(definition.extra_rate_limit_patterns)
        quota_patterns = definition.extra_quota_exhausted_patterns

    # Avoid concatenating and lowering multi-MB strings in one shot.
    # Error signals (auth, rate-limit, overload) are usually near the end
    # of output, so tail-biased sampling is safe.
    _haystack_raw = result.stderr + "\n" + result.stdout
    if len(_haystack_raw) > MAX_CLASSIFY_HAYSTACK:
        _haystack_raw = _haystack_raw[-MAX_CLASSIFY_HAYSTACK:]
    quota_haystack = _haystack_raw
    haystack = _haystack_raw.lower()
    del _haystack_raw

    _refusal_raw = result.stdout
    if len(_refusal_raw) > MAX_CLASSIFY_HAYSTACK:
        _refusal_raw = _refusal_raw[-MAX_CLASSIFY_HAYSTACK:]
    refusal_haystack = _refusal_raw.lower()
    del _refusal_raw

    if result.idle_classification == "approval_blocked":
        return Classification(
            kind=DispatchErrorKind.APPROVAL_BLOCKED,
            suggested_action=(
                "`codex` emitted an approval-required lifecycle event. Inspect "
                "cli.summary.json turn_lifecycle and rerun after adjusting "
                "Codex approval/sandbox settings; atlas-dispatch did not "
                "auto-approve it."
            ),
        )

    if result.idle_classification == "stalled":
        return Classification(
            kind=DispatchErrorKind.STALLED,
            suggested_action=(
                "`codex` turn stayed in progress without lifecycle events past "
                "idle_timeout_seconds. Retry the dispatch or split the task if "
                "it repeatedly stalls."
            ),
        )

    clean_completed_turn = (
        result.exit_code == 0 and result.final_turn_status == "completed"
    )
    if not clean_completed_turn and _codex_error_code(result.error_info) == "-32001":
        return Classification(
            kind=DispatchErrorKind.OVERLOADED,
            suggested_action=(
                "`codex` reported server overload (-32001). Wait and retry; "
                "attempt-aware backoff is handled by a follow-up task."
            ),
        )

    # A hard usage/quota stop surfaces as a *failed turn* whose only evidence is
    # error_info.message -- stdout and stderr are empty, so the pattern scan over
    # `haystack` below never sees it. Classify it here, ahead of the generic
    # failed-turn catch-all, or the reader is told to "retry after the
    # underlying model/runtime issue is resolved" when the correct action is to
    # wait for the quota reset or hand the task to a different CLI.
    error_message = _error_info_message(result.error_info)
    if error_message and not clean_completed_turn:
        quota_evidence = _quota_exhaustion_evidence(
            error_message,
            quota_patterns,
        )
        if quota_evidence is not None:
            return _quota_exhausted_classification(
                cli=cli,
                evidence=quota_evidence,
            )
        matched = _first_match(error_message.lower(), rate_patterns)
        if matched:
            return Classification(
                kind=DispatchErrorKind.RATE_LIMITED,
                suggested_action=(
                    f"`{cli}` reported a usage/rate limit. Wait for the quota to "
                    "reset, switch to a different CLI/model for this task, or "
                    "split the work."
                ),
                matched_pattern=matched,
            )

    # A non-zero run may include explanatory text around its quota failure, so
    # retain the broad scan used for failure output. An exit-zero response must
    # be more discriminating: successful work about this classifier can quote
    # the exact vendor text in prose. Only treat exit-zero output as exhausted
    # when the banner occupies the complete final non-empty line.
    quota_evidence = (
        _terminal_quota_exhaustion_evidence(quota_haystack, quota_patterns)
        if result.exit_code == 0
        else _quota_exhaustion_evidence(quota_haystack, quota_patterns)
    )
    if quota_evidence is not None:
        return _quota_exhausted_classification(
            cli=cli,
            evidence=quota_evidence,
        )

    if result.final_turn_status == "failed":
        return Classification(
            kind=DispatchErrorKind.FAILED,
            suggested_action=(
                "`codex` completed the turn with status=failed. Inspect "
                "cli.summary.json error_info and retry after the underlying "
                "model/runtime issue is resolved."
            ),
        )

    if result.final_turn_status == "interrupted":
        return Classification(
            kind=DispatchErrorKind.INTERRUPTED,
            suggested_action=(
                "`codex` reported the turn was interrupted. Retry the dispatch "
                "unless someone intentionally interrupted it."
            ),
        )

    if result.timed_out:
        return Classification(
            kind=DispatchErrorKind.TIMEOUT,
            suggested_action=(
                f"The {cli} run exceeded the task's timeout. Consider raising "
                "`timeout_seconds` in the task spec, or splitting the task "
                "into smaller pieces."
            ),
        )

    if result.executable_not_found:
        return Classification(
            kind=DispatchErrorKind.EXECUTABLE_NOT_FOUND,
            suggested_action=(
                f"`{definition.executable if definition else cli}` is not on PATH. "
                "Install the CLI or set "
                f"{_cli_env_override_name(cli)} to a working invocation."
            ),
        )

    auth_patterns = list(GENERIC_AUTH_PATTERNS)
    overload_patterns = list(GENERIC_OVERLOADED_PATTERNS)
    refusal_patterns = list(GENERIC_REFUSAL_PATTERNS)
    if definition is not None:
        auth_patterns.extend(definition.extra_auth_patterns)
        overload_patterns.extend(definition.extra_overloaded_patterns)
        refusal_patterns.extend(definition.extra_refusal_patterns)

    if result.exit_code != 0:
        matched = _first_match(haystack, auth_patterns)
        if matched:
            hint = (
                definition.auth_setup_hint
                if definition is not None
                else f"Refresh authentication for `{cli}` and try again."
            )
            return Classification(
                kind=DispatchErrorKind.AUTH_REQUIRED,
                suggested_action=hint,
                matched_pattern=matched,
            )

        matched = _first_match(haystack, rate_patterns)
        if matched:
            return Classification(
                kind=DispatchErrorKind.RATE_LIMITED,
                suggested_action=(
                    f"`{cli}` returned a rate-limit signal. Wait and retry, "
                    "switch to a different CLI/model for this task, or split "
                    "the work."
                ),
                matched_pattern=matched,
            )

        matched = _first_match(haystack, overload_patterns)
        if matched:
            return Classification(
                kind=DispatchErrorKind.OVERLOADED,
                suggested_action=(
                    f"`{cli}` reported a server-side overload (5xx). Wait a few "
                    "minutes and retry; if persistent, try a different CLI."
                ),
                matched_pattern=matched,
            )

    scope_refusal_reason = out_of_scope_refusal_reason(result.stdout)
    if scope_refusal_reason:
        return Classification(
            kind=DispatchErrorKind.REFUSED_OUT_OF_SCOPE,
            suggested_action=(
                "The builder intentionally stopped at the task scope boundary. "
                f"Builder reason: {scope_refusal_reason}"
            ),
            matched_pattern=_first_match(
                result.stdout.casefold(), OUT_OF_SCOPE_REFUSAL_PATTERNS
            ),
        )

    if result.exit_code != 0 or result.final_turn_status != "completed":
        refusal_source = haystack if result.exit_code != 0 else refusal_haystack
        matched = _first_match(refusal_source, refusal_patterns)
        if matched:
            return Classification(
                kind=DispatchErrorKind.REFUSED,
                suggested_action=(
                    f"`{cli}` refused the task. Reword the prompt to be more "
                    "concrete, narrow the scope, or hand the task to a different "
                    "CLI/model."
                ),
                matched_pattern=matched,
            )

    if (
        result.exit_code != 0
        and result.produced_expected_deliverable
        and definition is not None
    ):
        matched = _first_match(
            haystack,
            definition.extra_post_completion_timeout_patterns,
        )
        if matched:
            return Classification(
                kind=DispatchErrorKind.SUCCESS,
                suggested_action=(
                    f"`{definition.executable}` exited with code "
                    f"{result.exit_code} after producing the expected "
                    "deliverable, then reported its post-completion response "
                    "timeout. The raw non-zero process exit is retained in "
                    "classification and run-identity evidence."
                ),
                matched_pattern=matched,
                observed_exit_code=result.exit_code,
            )

    if result.exit_code != 0:
        return Classification(
            kind=DispatchErrorKind.EXIT_NONZERO,
            suggested_action=(
                f"`{cli}` exited with code {result.exit_code}. Inspect "
                "cli.stderr.txt in the run directory for the exact error."
            ),
        )

    if definition is not None and len(result.stdout) <= MAX_CLASSIFY_HAYSTACK:
        matched = _first_match(
            result.stdout,
            definition.model_selection_error_patterns,
        )
        if matched:
            return Classification(
                kind=DispatchErrorKind.MODEL_SELECTION_ERROR,
                suggested_action=(
                    f"`{cli}` did not run the task because the selected model "
                    "does not exist or is not accessible. Correct the model id "
                    "or choose a different model, then retry."
                ),
                matched_pattern=matched,
            )

    if result.final_turn_status == "completed":
        return Classification(
            kind=DispatchErrorKind.SUCCESS,
            suggested_action="",
        )

    if not result.stdout.strip():
        return Classification(
            kind=DispatchErrorKind.NO_OUTPUT,
            suggested_action=(
                f"`{cli}` exited 0 but produced no stdout. Many CLIs emit a "
                "summary to stdout; check that the task prompt was actually "
                "received and that the CLI's quiet/print flags are right."
            ),
        )

    return Classification(
        kind=DispatchErrorKind.SUCCESS,
        suggested_action="",
    )


def _first_match(haystack: str, patterns: list[str] | tuple[str, ...]) -> str | None:
    for pattern in patterns:
        if re.search(pattern, haystack, re.IGNORECASE):
            return pattern
    return None


def out_of_scope_refusal_reason(stdout: str) -> str:
    """Return the builder's explicit scope-boundary reason, if present."""

    for raw_line in reversed(stdout.splitlines()):
        line = " ".join(raw_line.strip().split())
        if line and _first_match(line.casefold(), OUT_OF_SCOPE_REFUSAL_PATTERNS):
            return line[:1000]
    normalized = " ".join(stdout.strip().split())
    if normalized and _first_match(
        normalized.casefold(), OUT_OF_SCOPE_REFUSAL_PATTERNS
    ):
        return normalized[-1000:]
    return ""


@dataclass(frozen=True)
class _QuotaExhaustionEvidence:
    matched_pattern: str
    reset_window: str | None


def _quota_exhaustion_evidence(
    haystack: str,
    patterns: tuple[str, ...],
) -> _QuotaExhaustionEvidence | None:
    for pattern in patterns:
        match = re.search(pattern, haystack, re.IGNORECASE)
        if match is None:
            continue
        reset_window = match.groupdict().get("reset_window")
        return _QuotaExhaustionEvidence(
            matched_pattern=pattern,
            reset_window=reset_window.strip() if reset_window else None,
        )
    return None


def _terminal_quota_exhaustion_evidence(
    haystack: str,
    patterns: tuple[str, ...],
) -> _QuotaExhaustionEvidence | None:
    for pattern in patterns:
        match = re.search(
            (
                rf"(?:\A|(?<=\n))[^\S\r\n]*(?:{pattern})"
                r"[^\S\r\n]*(?:\r?\n[^\S\r\n]*)*\Z"
            ),
            haystack,
            re.IGNORECASE,
        )
        if match is None:
            continue
        reset_window = match.groupdict().get("reset_window")
        return _QuotaExhaustionEvidence(
            matched_pattern=pattern,
            reset_window=reset_window.strip() if reset_window else None,
        )
    return None


def _quota_exhausted_classification(
    *,
    cli: str,
    evidence: _QuotaExhaustionEvidence,
) -> Classification:
    reset_detail = (
        f" The vendor-declared reset window is {evidence.reset_window}."
        if evidence.reset_window is not None
        else ""
    )
    return Classification(
        kind=DispatchErrorKind.QUOTA_EXHAUSTED,
        suggested_action=(
            f"`{cli}` reported that its individual quota is exhausted."
            f"{reset_detail} Wait for the quota reset or route the task to a "
            "CLI backed by a different quota pool."
        ),
        matched_pattern=evidence.matched_pattern,
        quota_reset_window=evidence.reset_window,
        quota_reset_window_provenance=(
            QuotaResetWindowProvenance.VENDOR_DECLARED
            if evidence.reset_window is not None
            else None
        ),
    )


def _error_info_message(error_info: dict[str, Any] | None) -> str:
    """Return the human-readable message a CLI attached to a failed turn.

    Codex reports a hard quota stop only here; stdout/stderr stay empty.
    """
    if not error_info:
        return ""
    message = error_info.get("message")
    return "" if message is None else str(message)


def _codex_error_code(error_info: dict[str, Any] | None) -> str | None:
    if not error_info:
        return None
    error_code = error_info.get("errorCode")
    return None if error_code is None else str(error_code)


# --------------------------------------------------------------------------- #
# Doctor                                                                      #
# --------------------------------------------------------------------------- #


def cli_doctor(
    *, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Return per-CLI availability info for the `doctor` subcommand."""
    process_env = os.environ if env is None else env
    rows: list[dict[str, Any]] = []
    for name in list_supported_clis():
        definition = CLIS[name]
        path = shutil.which(definition.executable)
        env_override = _cli_env_override_value(name, environ=process_env) or ""
        models_for_cli = [m.name for m in MODELS.values() if m.cli == name]
        row = {
            "cli": name,
            "executable": definition.executable,
            "found_at": path or "(not on PATH)",
            "env_override": env_override,
            "models": models_for_cli,
            "auth_setup_hint": definition.auth_setup_hint,
        }
        rows.append(row)
    return rows
