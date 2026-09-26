"""atlas-dispatch — repo-agnostic dispatch engine for coding-agent CLIs.

Public API for in-process callers (orchestrating Claude Code or Codex
sessions, plain Python scripts, etc.):

    from pathlib import Path
    from atlas_dispatch import dispatch

    exit_code = dispatch(Path("path/to/task.json"))

CLI surface:

    atlas-dispatch run path/to/task.json
    atlas-dispatch doctor
    atlas-dispatch clis
    atlas-dispatch models
    atlas-dispatch capabilities <cli>

See README.md for the pipeline and a runnable quickstart, and
docs/SPEC_REFERENCE.md for every task-spec field.
"""

__version__ = "1.0.0"

from atlas_dispatch.adapter import (
    CLIS,
    MODELS,
    Adapter,
    AdapterResult,
    Classification,
    CLIDefinition,
    DispatchErrorKind,
    ModelDefinition,
    SubprocessAdapter,
    classify_result,
    cli_doctor,
    inject_mcp_config,
    list_supported_clis,
    list_supported_models,
    render_command,
    resolve_model,
    run_cli,
)
from atlas_dispatch.dispatcher import (
    McpRuntime,
    TaskSpec,
    dispatch,
    load_task,
    render_prompt,
)
from atlas_dispatch.verify import (
    PROTECTED_PATTERNS,
    CheckResult,
    VerifyReport,
    check_allowlist,
    check_protected_paths,
    protected_patterns,
    run_acceptance_commands,
)
from atlas_dispatch.worktree import (
    BranchPublishResult,
    Worktree,
    commit_all,
    create_worktree,
    has_uncommitted_changes,
    is_git_repo,
    list_changed_files,
    publish_branch,
    remove_worktree,
)

__all__ = [
    "CLIS",
    "MODELS",
    "PROTECTED_PATTERNS",
    "Adapter",
    "AdapterResult",
    "BranchPublishResult",
    "CLIDefinition",
    "CheckResult",
    "Classification",
    "DispatchErrorKind",
    "McpRuntime",
    "ModelDefinition",
    "SubprocessAdapter",
    "TaskSpec",
    "VerifyReport",
    "Worktree",
    "check_allowlist",
    "check_protected_paths",
    "classify_result",
    "cli_doctor",
    "commit_all",
    "create_worktree",
    "dispatch",
    "has_uncommitted_changes",
    "inject_mcp_config",
    "is_git_repo",
    "list_changed_files",
    "list_supported_clis",
    "list_supported_models",
    "load_task",
    "protected_patterns",
    "publish_branch",
    "remove_worktree",
    "render_command",
    "render_prompt",
    "resolve_model",
    "run_acceptance_commands",
    "run_cli",
]
