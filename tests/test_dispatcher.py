"""Dispatcher tests for task-spec loading and runtime argv helpers."""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import io
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import textwrap
import threading
import time
import tomllib
from collections.abc import Callable
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import atlas_dispatch.dispatcher as dispatcher_mod
import atlas_dispatch.verify as verify_mod
from atlas_dispatch import (
    AdapterResult,
    CheckResult,
    Classification,
    DispatchErrorKind,
    TaskSpec,
    Worktree,
    check_allowlist,
    commit_all,
    create_worktree,
    dispatch,
    inject_mcp_config,
    load_task,
    publish_branch,
    render_prompt,
    run_cli,
)
from atlas_dispatch.adapter import QuotaResetWindowProvenance, classify_result
from atlas_dispatch.dispatcher import (
    PreDispatchGitSyncError,
    _expected_review_deliverable_path,
    _extend_git_info_exclude,
    _pre_dispatch_sync_base_ref,
    _prepare_mcp_runtime,
    _render_mcp_servers,
    main,
)
from atlas_dispatch.worktree import verify_remote_ref_contains_sha


def test_dispatcher_uses_canonical_public_acceptance_runner() -> None:
    import atlas_dispatch

    assert dispatcher_mod.run_acceptance_commands is verify_mod.run_acceptance_commands
    assert atlas_dispatch.run_acceptance_commands is verify_mod.run_acceptance_commands
    assert dispatcher_mod.check_protected_paths is verify_mod.check_protected_paths
    assert atlas_dispatch.check_protected_paths is verify_mod.check_protected_paths
    assert "subprocess.Popen" not in inspect.getsource(dispatcher_mod)


def _write_task_spec(tmp_path: Path, extra: dict[str, object] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Do the task.", encoding="utf-8")

    spec: dict[str, object] = {
        "id": "D-TEST",
        "title": "Dispatcher test",
        "target_repo": str(repo),
        "cli": "codex",
        "prompt_template": str(prompt),
        "worktree_branch": "codex/test",
        "allowed_paths": ["**"],
    }
    spec.update(extra or {})

    spec_path = tmp_path / "task.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path


@pytest.fixture(autouse=True)
def _stable_ref_guard_for_mock_repository_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give legacy mocked-repository tests a valid, unchanged ref snapshot.

    Many dispatcher unit tests replace ``is_git_repo`` and ``create_worktree``
    without creating a Git repository. The production guard correctly fails
    closed when local refs cannot be read; this fixture keeps those unrelated
    tests focused on their mocked subsystem. Real temporary repositories still
    exercise the production capture, and the dedicated ref-guard suite covers
    movement and unreadable-ref behavior end to end.
    """
    real_capture = dispatcher_mod._capture_orchestrator_refs
    stable_sha = "0" * 40

    def stable_snapshot() -> dict[str, object]:
        return {
            "refs": {
                "main": stable_sha,
                "HEAD": stable_sha,
                "origin/main": stable_sha,
            },
            "errors": {},
        }

    def capture(repo: Path) -> dict[str, object]:
        if not (Path(repo) / ".git").exists():
            return stable_snapshot()
        has_main = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/main"],
            cwd=repo,
            check=False,
            capture_output=True,
        ).returncode == 0
        if has_main:
            return real_capture(Path(repo))
        return stable_snapshot()

    monkeypatch.setattr(dispatcher_mod, "_capture_orchestrator_refs", capture)




def _write_publish_spec(
    tmp_path: Path,
    *,
    title: str = "Ordinary dispatch-infra task",
    task_id: str = "TASK-DISPATCHER",
    worktree_branch: str = "codex/task-dispatcher",
    tags: list[str] | None = None,
) -> Path:
    repo = tmp_path / "target"
    repo.mkdir(exist_ok=True)
    prompt = tmp_path / "prompt.md"
    prompt.write_text("Implement {{task_summary}}.", encoding="utf-8")
    spec: dict[str, object] = {
        "id": task_id,
        "title": title,
        "target_repo": str(repo),
        "model": "gpt-5.5",
        "prompt_template": str(prompt),
        "worktree_branch": worktree_branch,
        "base_ref": "main",
        "allowed_paths": ["atlas_dispatch/dispatcher.py"],
        "acceptance": [".venv/bin/python -m pytest tests/test_dispatcher.py -q"],
        "extra_prompt_vars": {"task_summary": "Verify the dispatcher pipeline."},
    }
    if tags is not None:
        spec["tags"] = tags
    spec_path = tmp_path / "publish-task.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return spec_path




def _make_prompt_task(
    tmp_path: Path,
    *,
    template_text: str,
    extra: dict[str, str] | None = None,
) -> TaskSpec:
    repo = tmp_path / "repo"
    repo.mkdir()
    template = tmp_path / "prompt.md"
    template.write_text(template_text, encoding="utf-8")
    return TaskSpec(
        id="D-PROMPT",
        title="Prompt validator test",
        target_repo=repo,
        cli="codex",
        prompt_template=template,
        worktree_branch="codex/prompt-validator",
        allowed_paths=["**"],
        extra_prompt_vars=dict(extra or {}),
        spec_path=tmp_path / "task.json",
        resolved_via="explicit",
    )


def _git_result(
    args: list[str],
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git", *args],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _repo_with_dot_git(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def test_pre_dispatch_sync_fast_forwards_base_ref_from_origin(tmp_path: Path) -> None:
    repo = _repo_with_dot_git(tmp_path)
    state = {"head": "abc123", "remote": "def456"}
    calls: list[list[str]] = []

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["checkout", "main"]:
            return _git_result(args)
        if args == ["fetch", "origin", "main"]:
            return _git_result(args)
        if args == ["rev-parse", "origin/main"]:
            return _git_result(args, stdout=f"{state['remote']}\n")
        if args == ["rev-parse", "HEAD"]:
            return _git_result(args, stdout=f"{state['head']}\n")
        if args == ["merge", "--ff-only", "origin/main"]:
            state["head"] = state["remote"]
            return _git_result(args)
        raise AssertionError(f"unexpected git args: {args}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        _pre_dispatch_sync_base_ref(target_repo=repo, base_ref="main")

    assert state["head"] == "def456"
    assert ["merge", "--ff-only", "origin/main"] in calls


def test_pre_dispatch_sync_no_op_when_already_at_origin_head(tmp_path: Path) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args in (["checkout", "main"], ["fetch", "origin", "main"]):
            return _git_result(args)
        if args in (["rev-parse", "origin/main"], ["rev-parse", "HEAD"]):
            return _git_result(args, stdout="def456\n")
        raise AssertionError(f"unexpected git args: {args}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        _pre_dispatch_sync_base_ref(target_repo=repo, base_ref="main")

    assert ["merge", "--ff-only", "origin/main"] not in calls


@pytest.mark.parametrize(
    ("base_ref", "fetch_args", "resolved_base_ref"),
    [
        ("main", ["fetch", "origin", "main"], "main"),
        (
            "topic",
            ["fetch", "origin", "topic:refs/remotes/origin/topic"],
            "origin/topic",
        ),
    ],
)
def test_pre_dispatch_sync_retries_transient_fetch_then_succeeds(
    tmp_path: Path,
    base_ref: str,
    fetch_args: list[str],
    resolved_base_ref: str,
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []
    delays: list[float] = []
    fetch_attempts = 0

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal fetch_attempts
        calls.append(args)
        if args == ["rev-parse", "--verify", "refs/heads/topic"]:
            return _git_result(args, returncode=128, stderr="unknown revision")
        if args == ["checkout", "main"]:
            return _git_result(args)
        if args == fetch_args:
            fetch_attempts += 1
            if fetch_attempts < 3:
                return _git_result(
                    args,
                    returncode=128,
                    stderr="fatal: unable to access git.example.com: Connection refused",
                )
            return _git_result(args)
        if args in (
            ["rev-parse", "origin/main"],
            ["rev-parse", "origin/topic"],
            ["rev-parse", "HEAD"],
        ):
            return _git_result(args, stdout="def456\n")
        raise AssertionError(f"unexpected git args: {args}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        result = _pre_dispatch_sync_base_ref(
            target_repo=repo,
            base_ref=base_ref,
            sleep_fn=delays.append,
        )

    assert result == resolved_base_ref
    assert calls.count(fetch_args) == 3
    assert delays == [0.1, 0.2]


def test_pre_dispatch_sync_non_transient_fetch_failure_is_not_retried(
    tmp_path: Path,
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []
    delays: list[float] = []
    fetch_args = ["fetch", "origin", "main"]

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["checkout", "main"]:
            return _git_result(args)
        if args == fetch_args:
            return _git_result(
                args,
                returncode=128,
                stderr="fatal: couldn't find remote ref main",
            )
        raise AssertionError(f"unexpected git args: {args}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        with pytest.raises(
            PreDispatchGitSyncError,
            match="manual intervention required",
        ):
            _pre_dispatch_sync_base_ref(
                target_repo=repo,
                base_ref="main",
                sleep_fn=delays.append,
            )

    assert calls.count(fetch_args) == 1
    assert delays == []


def test_pre_dispatch_sync_exhausts_transient_fetch_retries(
    tmp_path: Path,
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []
    delays: list[float] = []
    fetch_args = ["fetch", "origin", "main"]

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["checkout", "main"]:
            return _git_result(args)
        if args == fetch_args:
            return _git_result(
                args,
                returncode=128,
                stderr="fatal: connect failed: ECONNREFUSED",
            )
        raise AssertionError(f"unexpected git args: {args}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        with pytest.raises(
            PreDispatchGitSyncError,
            match="manual intervention required",
        ):
            _pre_dispatch_sync_base_ref(
                target_repo=repo,
                base_ref="main",
                sleep_fn=delays.append,
            )

    assert calls.count(fetch_args) == 3
    assert delays == [0.1, 0.2]


def test_pre_dispatch_sync_serializes_same_repo_checkout(
    tmp_path: Path,
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    start_barrier = threading.Barrier(2)
    active_lock = threading.Lock()
    active_checkouts = 0
    max_active_checkouts = 0
    checkout_count = 0
    errors: list[BaseException] = []

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        nonlocal active_checkouts, checkout_count, max_active_checkouts
        if args == ["checkout", "main"]:
            with active_lock:
                active_checkouts += 1
                checkout_count += 1
                max_active_checkouts = max(max_active_checkouts, active_checkouts)
            time.sleep(0.05)
            with active_lock:
                active_checkouts -= 1
            return _git_result(args)
        if args == ["fetch", "origin", "main"]:
            return _git_result(args)
        if args in (["rev-parse", "origin/main"], ["rev-parse", "HEAD"]):
            return _git_result(args, stdout="def456\n")
        raise AssertionError(f"unexpected git args: {args}")

    def run_sync() -> None:
        try:
            start_barrier.wait(timeout=1)
            _pre_dispatch_sync_base_ref(target_repo=repo, base_ref="main")
        except BaseException as exc:
            errors.append(exc)

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        threads = [threading.Thread(target=run_sync) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

    assert not errors
    assert checkout_count == 2
    assert max_active_checkouts == 1


def test_pre_dispatch_sync_retries_and_classifies_persistent_index_lock(
    tmp_path: Path,
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []
    delays: list[float] = []
    index_lock_stderr = (
        "fatal: Unable to create '/tmp/repo/.git/index.lock': File exists.\n"
        "Another git process seems to be running in this repository."
    )

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        assert args == ["checkout", "main"]
        return _git_result(args, returncode=128, stderr=index_lock_stderr)

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        with pytest.raises(PreDispatchGitSyncError) as excinfo:
            _pre_dispatch_sync_base_ref(
                target_repo=repo,
                base_ref="main",
                sleep_fn=delays.append,
            )

    exc = excinfo.value
    assert calls == [["checkout", "main"]] * 4
    assert delays == [0.1, 0.25, 0.5]
    assert exc.classification == "git_lock_contention"
    assert exc.transient is True
    assert exc.suggested_action is not None
    assert "retry" in exc.suggested_action.lower()
    assert "redispatch" in exc.suggested_action.lower()
    assert "manual intervention" not in str(exc)
    assert "dispatch_exception" not in str(exc)
    assert "transient" in str(exc).lower()


def test_pre_dispatch_sync_normal_noop_sequence_has_no_retry_sleep(
    tmp_path: Path,
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args in (["checkout", "main"], ["fetch", "origin", "main"]):
            return _git_result(args)
        if args in (["rev-parse", "origin/main"], ["rev-parse", "HEAD"]):
            return _git_result(args, stdout="def456\n")
        raise AssertionError(f"unexpected git args: {args}")

    def unexpected_sleep(delay: float) -> None:
        raise AssertionError(f"unexpected retry sleep: {delay}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        _pre_dispatch_sync_base_ref(
            target_repo=repo,
            base_ref="main",
            sleep_fn=unexpected_sleep,
        )

    assert calls == [
        ["checkout", "main"],
        ["fetch", "origin", "main"],
        ["rev-parse", "origin/main"],
        ["rev-parse", "HEAD"],
    ]


def test_pre_dispatch_sync_raises_on_diverged_history(tmp_path: Path) -> None:
    repo = _repo_with_dot_git(tmp_path)

    def fake_run_git(_repo: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        if args in (["checkout", "main"], ["fetch", "origin", "main"]):
            return _git_result(args)
        if args == ["rev-parse", "origin/main"]:
            return _git_result(args, stdout="def456\n")
        if args == ["rev-parse", "HEAD"]:
            return _git_result(args, stdout="abc123\n")
        if args == ["merge", "--ff-only", "origin/main"]:
            return _git_result(
                args,
                returncode=1,
                stderr="Not possible to fast-forward\n",
            )
        if args == ["rev-list", "--left-right", "--count", "HEAD...origin/main"]:
            return _git_result(args, stdout="1\t1\n")
        raise AssertionError(f"unexpected git args: {args}")

    with patch("atlas_dispatch.dispatcher._run_git", side_effect=fake_run_git):
        with pytest.raises(PreDispatchGitSyncError, match="DIVERGED"):
            _pre_dispatch_sync_base_ref(target_repo=repo, base_ref="main")


def test_pre_dispatch_sync_unchanged_for_remote_refs(tmp_path: Path) -> None:
    repo = _repo_with_dot_git(tmp_path)

    with patch("atlas_dispatch.dispatcher._run_git") as run_git:
        _pre_dispatch_sync_base_ref(target_repo=repo, base_ref="origin/foo")

    run_git.assert_not_called()


def test_dispatcher_git_helpers_route_through_hardened_run_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo_with_dot_git(tmp_path)
    calls: list[list[str]] = []

    def fake_run_git(
        args: list[str],
        *,
        cwd: Path,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd == repo
        assert check is False
        calls.append(args)
        if args == ["rev-list", "--count", "main..HEAD"]:
            return _git_result(args, stdout="3\n")
        if args == ["status", "--porcelain"]:
            return _git_result(args, stdout=" M file.py\n")
        if args in (["checkout", "main"], ["fetch", "origin", "main"]):
            return _git_result(args)
        if args in (["rev-parse", "origin/main"], ["rev-parse", "HEAD"]):
            return _git_result(args, stdout="abc123\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(dispatcher_mod, "run_git", fake_run_git, raising=False)

    assert dispatcher_mod._commits_ahead_of_base(repo, base_ref="main") == 3
    assert dispatcher_mod._has_uncommitted_work(repo) is True
    dispatcher_mod._pre_dispatch_sync_base_ref(target_repo=repo, base_ref="main")

    assert ["rev-list", "--count", "main..HEAD"] in calls
    assert ["status", "--porcelain"] in calls
    assert ["fetch", "origin", "main"] in calls


def test_prompt_keeps_readable_local_context_output_byte_identical(
    tmp_path: Path,
) -> None:
    context_file = tmp_path / "context.md"
    context_file.write_text("alpha\nbeta", encoding="utf-8")
    task = _make_prompt_task(
        tmp_path,
        template_text="Before\n\n{{context_block}}\n\nAfter",
    )
    task.context_files.append(context_file)

    assert render_prompt(task) == (
        f"Before\n\n## {context_file}\n\n```\nalpha\nbeta\n```\n\nAfter"
    )


def test_context_block_keeps_missing_file_output_unchanged(tmp_path: Path) -> None:
    missing = tmp_path / "missing.md"

    assert dispatcher_mod._build_context_block([missing]) == (
        f"## {missing}\n\n(file not found at {missing})"
    )


def test_timed_out_context_does_not_hide_unfilled_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "blocked-validator-context.md"
    os.mkfifo(fifo)
    monkeypatch.setattr(
        dispatcher_mod,
        "CONTEXT_FILE_READ_TIMEOUT_SECONDS",
        0.02,
    )
    task = _make_prompt_task(
        tmp_path,
        template_text="{{context_block}}\n\nStill unfilled: {{missing_var}}",
    )
    task.context_files.append(fifo)

    try:
        with pytest.raises(ValueError, match="unfilled placeholders") as exc:
            render_prompt(task)
    finally:
        try:
            writer_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            pass
        else:
            os.close(writer_fd)

    assert "{{missing_var}}" in str(exc.value)




def test_a_genuinely_unfilled_template_slot_must_still_raise(
    tmp_path: Path,
) -> None:
    """MUST-STILL-FIRE: the check's real purpose is unchanged.

    A template slot the spec never filled is still a hard error, and the
    message still names the template -- which is now always the true source.
    """

    context_file = tmp_path / "ctx.md"
    context_file.write_text("harmless\n", encoding="utf-8")
    task = _make_prompt_task(
        tmp_path,
        template_text="{{context_block}}\n\nDo {{task_summary}} now.\n",
    )
    task.context_files.append(context_file)

    with pytest.raises(ValueError, match="unfilled placeholders") as exc:
        render_prompt(task)

    assert "{{task_summary}}" in str(exc.value)


class PromptValidatorCodeFenceTests:
    __test__ = True

    def test_context_fence_cannot_hide_later_unfilled_placeholder(
        self,
        tmp_path: Path,
    ) -> None:
        context_file = tmp_path / "context.md"
        context_file.write_text(
            "A context example with an unmatched fence:\n```\n",
            encoding="utf-8",
        )
        rendered = (
            dispatcher_mod._build_context_block([context_file])
            + "\n\nOutside {{orphan}}"
        )

        assert dispatcher_mod._find_unfilled_prompt_placeholders(rendered) == [
            "{{orphan}}"
        ]

    def test_placeholder_outside_fence_still_caught(self, tmp_path: Path) -> None:
        task = _make_prompt_task(
            tmp_path,
            template_text="Dispatch {{task_id}} and leave {{missing_var}}.",
        )

        with pytest.raises(ValueError, match="unfilled placeholders") as exc:
            render_prompt(task)

        message = str(exc.value)
        assert "{{missing_var}}" in message
        assert "extra_prompt_vars or remove them from the template" in message

    def test_placeholder_inside_triple_backtick_fence_ignored(
        self,
        tmp_path: Path,
    ) -> None:
        template = textwrap.dedent(
            """
            Explain this template:

            ```
            title: {{literal_title}}
            ```
            """
        ).strip()
        task = _make_prompt_task(tmp_path, template_text=template)

        assert render_prompt(task) == template

    def test_placeholder_inside_single_backtick_inline_span_ignored(
        self,
        tmp_path: Path,
    ) -> None:
        template = "Use `{{literal_var}}` to document a placeholder."
        task = _make_prompt_task(tmp_path, template_text=template)

        assert render_prompt(task) == template

    def test_mixed_placeholders_only_flags_outside_ones(
        self,
        tmp_path: Path,
    ) -> None:
        template = textwrap.dedent(
            """
            Outside {{outside_var}}
            Inline `{{inline_var}}`
            ```
            Fenced {{fenced_var}}
            ```
            Outside again {{second_outside_var}}
            """
        ).strip()
        task = _make_prompt_task(tmp_path, template_text=template)

        with pytest.raises(ValueError) as exc:
            render_prompt(task)

        message = str(exc.value)
        assert "{{outside_var}}" in message
        assert "{{second_outside_var}}" in message
        assert "{{inline_var}}" not in message
        assert "{{fenced_var}}" not in message

    def test_nested_fence_blocks_handled_sanely(self, tmp_path: Path) -> None:
        template = textwrap.dedent(
            """
            Before
            ```
            ignored {{inside_first_fence}}
            ```
            Between {{outside_nested_fence}}
            ```
            ignored {{inside_second_fence}}
            ```
            After
            """
        ).strip()
        task = _make_prompt_task(tmp_path, template_text=template)

        with pytest.raises(ValueError) as exc:
            render_prompt(task)

        message = str(exc.value)
        assert "{{outside_nested_fence}}" in message
        assert "{{inside_first_fence}}" not in message
        assert "{{inside_second_fence}}" not in message

    def test_language_tagged_fence_still_toggles(self, tmp_path: Path) -> None:
        template = textwrap.dedent(
            """
            ```python
            VALUE = "{{literal_value}}"
            ```
            """
        ).strip()
        task = _make_prompt_task(tmp_path, template_text=template)

        assert render_prompt(task) == template

    def test_empty_prompt_passes(self, tmp_path: Path) -> None:
        task = _make_prompt_task(tmp_path, template_text="")

        assert render_prompt(task) == ""






def test_expected_review_deliverable_comes_from_agreeing_spec_fields(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "worktree").resolve()
    workspace.mkdir()
    review_path = "reviews/spec-declared.md"
    task = TaskSpec(
        id="REVIEW-STRUCTURED-DELIVERABLE",
        title="Structured review deliverable",
        target_repo=tmp_path / "repo",
        cli="gemini",
        prompt_template=tmp_path / "review.md",
        worktree_branch="gemini/structured-review",
        allowed_paths=["reviews/**"],
        acceptance=[f"test -f {review_path}"],
        extra_prompt_vars={"review_output_path": review_path},
    )

    assert _expected_review_deliverable_path(
        task,
        workspace=workspace,
    ) == workspace / review_path


@pytest.mark.parametrize(
    ("cli", "review_path", "acceptance"),
    [
        ("codex", "reviews/expected.md", ["test -f reviews/expected.md"]),
        ("gemini", "../outside.md", ["test -f ../outside.md"]),
        ("gemini", "reviews/expected.md", ["test -f reviews/different.md"]),
        ("gemini", "reviews/expected.md", ["python -m pytest -q"]),
    ],
)
def test_expected_review_deliverable_fails_closed_without_spec_agreement(
    tmp_path: Path,
    cli: str,
    review_path: str,
    acceptance: list[str],
) -> None:
    workspace = (tmp_path / "worktree").resolve()
    workspace.mkdir()
    task = TaskSpec(
        id="REVIEW-STRUCTURED-DELIVERABLE",
        title="Structured review deliverable",
        target_repo=tmp_path / "repo",
        cli=cli,
        prompt_template=tmp_path / "review.md",
        worktree_branch=f"{cli}/structured-review",
        allowed_paths=["reviews/**"],
        acceptance=acceptance,
        extra_prompt_vars={"review_output_path": review_path},
    )

    assert _expected_review_deliverable_path(task, workspace=workspace) is None


def test_task_spec_default_has_empty_mcp_servers(tmp_path: Path) -> None:
    task = load_task(_write_task_spec(tmp_path))

    assert task.mcp_servers == []


def test_task_spec_round_trips_protected_head_sha(tmp_path: Path) -> None:
    protected_head_sha = "4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a"

    task = load_task(
        _write_task_spec(tmp_path, {"protected_head_sha": protected_head_sha})
    )

    assert task.protected_head_sha == protected_head_sha




def test_acceptance_timeout_per_spec_override(tmp_path: Path) -> None:
    task = load_task(
        _write_task_spec(tmp_path, {"acceptance_timeout_seconds": 60})
    )

    assert task.acceptance_timeout_seconds == 60


def _successful_adapter_result(cli: str = "codex") -> AdapterResult:
    return AdapterResult(
        cli=cli,
        exit_code=0,
        stdout="done",
        stderr="",
        duration_seconds=0.0,
        command=[cli],
        classification=Classification(
            kind=DispatchErrorKind.SUCCESS,
            suggested_action="",
        ),
    )


def _failed_adapter_result(cli: str = "codex") -> AdapterResult:
    return AdapterResult(
        cli=cli,
        exit_code=1,
        stdout="",
        stderr="failed",
        duration_seconds=0.0,
        command=[cli],
        classification=Classification(
            kind=DispatchErrorKind.FAILED,
            suggested_action="retry",
        ),
    )


def _write_real_duplicate_guard_spec(
    tmp_path: Path,
    *,
    repo: Path,
    branch: str,
    artifact: str,
) -> Path:
    return _write_task_spec(
        tmp_path,
        {
            "id": "D-REAL-DUPLICATE-GUARD",
            "target_repo": str(repo),
            "cli": "gemini",
            "model_id": "gemini-3.6-flash",
            "reasoning_effort": "high",
            "worktree_branch": branch,
            "runs_dir": str(tmp_path / "runs"),
            "base_ref": "main",
            "allowed_paths": [artifact],
            "acceptance": [f"test -f {artifact}"],
        },
    )


def _seed_real_duplicate_guard_run(
    spec_path: Path,
    *,
    source_base_sha: str,
    verify_passed: bool,
) -> Path:
    task = load_task(spec_path)
    assert task.runs_dir is not None
    prior_run = task.runs_dir / "20200101T000000Z"
    prior_run.mkdir(parents=True)
    (prior_run / "prompt.md").write_text(render_prompt(task), encoding="utf-8")
    (prior_run / "task.json").write_text(
        json.dumps(asdict(task), indent=2, default=str),
        encoding="utf-8",
    )
    (prior_run / "cli.stdout.txt").write_text("completed\n", encoding="utf-8")
    (prior_run / "cli.stderr.txt").write_text("", encoding="utf-8")
    (prior_run / "cli.summary.json").write_text(
        json.dumps(
            {
                "cli": task.cli,
                "exit_code": 0,
                "timed_out": False,
                "classification": {
                    "kind": "success",
                    "suggested_action": "",
                    "matched_pattern": None,
                },
                "verify_passed": verify_passed,
                "acceptance_failed": not verify_passed,
                "acceptance_outcome": "passed" if verify_passed else "failed",
                "source_base_sha": source_base_sha,
                "verified_head_sha": "f" * 40,
                "acceptance_commands": [],
                "acceptance_skip_reason": None,
                "review_model_evidence": {
                    "schema": "atlas-dispatch.review-model-evidence",
                    "schema_version": 1,
                    "effective_cli": "agy",
                    "effective_model": "gemini/3.6-flash-agy",
                    "model_resolution_source": "fixed-runtime-declaration",
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (prior_run / "changed_files.txt").write_text(
        "\n".join(task.allowed_paths),
        encoding="utf-8",
    )
    (prior_run / "report.md").write_text(
        f"- Verify passed: **{verify_passed}**\n",
        encoding="utf-8",
    )
    return prior_run


def _set_real_gemini_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    marker: Path,
    artifact: str,
) -> None:
    script = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('invoked\\n', encoding='utf-8'); "
        f"path = Path({artifact!r}); "
        "path.parent.mkdir(parents=True, exist_ok=True); "
        "path.write_text('generated\\n', encoding='utf-8'); "
        "print('completed')"
    )
    monkeypatch.setenv(
        "ATLAS_DISPATCH_GEMINI_CMD",
        shlex.join([sys.executable, "-c", script]),
    )


def test_dispatch_real_prior_cli_success_with_failed_verify_runs_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    artifact = "artifact.txt"
    spec_path = _write_real_duplicate_guard_spec(
        tmp_path,
        repo=repo,
        branch="gemini/real-failed-verify-retry",
        artifact=artifact,
    )
    _seed_real_duplicate_guard_run(
        spec_path,
        source_base_sha=_git_output(repo, "rev-parse", "main"),
        verify_passed=False,
    )
    marker = tmp_path / "cli-invoked"
    _set_real_gemini_probe(monkeypatch, marker=marker, artifact=artifact)

    rc = dispatch(spec_path)

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "invoked\n"
    run_dirs = sorted(path for path in (tmp_path / "runs").iterdir() if path.is_dir())
    assert len(run_dirs) == 2
    retry_summary = json.loads(
        (run_dirs[-1] / "cli.summary.json").read_text(encoding="utf-8")
    )
    assert retry_summary.get("cli_invoked", True) is True
    assert retry_summary["verify_passed"] is True


def test_dispatch_real_different_branch_and_artifact_runs_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_real_duplicate_guard_spec(
        tmp_path,
        repo=repo,
        branch="gemini/review-old",
        artifact="reviews/old.md",
    )
    _seed_real_duplicate_guard_run(
        spec_path,
        source_base_sha=_git_output(repo, "rev-parse", "main"),
        verify_passed=True,
    )
    raw_spec = json.loads(spec_path.read_text(encoding="utf-8"))
    raw_spec.update(
        {
            "worktree_branch": "gemini/review-new",
            "allowed_paths": ["reviews/new.md"],
            "acceptance": ["test -f reviews/new.md"],
        }
    )
    spec_path.write_text(json.dumps(raw_spec), encoding="utf-8")
    marker = tmp_path / "cli-invoked"
    _set_real_gemini_probe(
        monkeypatch,
        marker=marker,
        artifact="reviews/new.md",
    )

    rc = dispatch(spec_path)

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "invoked\n"


def test_dispatch_real_same_base_ref_name_at_new_commit_runs_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    artifact = "artifact.txt"
    spec_path = _write_real_duplicate_guard_spec(
        tmp_path,
        repo=repo,
        branch="gemini/ref-advanced",
        artifact=artifact,
    )
    prior_sha = _git_output(repo, "rev-parse", "main")
    _seed_real_duplicate_guard_run(
        spec_path,
        source_base_sha=prior_sha,
        verify_passed=True,
    )
    (repo / "README.md").write_text("advanced\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "advance mutable base")
    assert _git_output(repo, "rev-parse", "main") != prior_sha
    marker = tmp_path / "cli-invoked"
    _set_real_gemini_probe(monkeypatch, marker=marker, artifact=artifact)

    rc = dispatch(spec_path)

    assert rc == 0
    assert marker.read_text(encoding="utf-8") == "invoked\n"


def test_dispatch_real_identical_verified_same_sha_suppresses_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    artifact = "artifact.txt"
    spec_path = _write_real_duplicate_guard_spec(
        tmp_path,
        repo=repo,
        branch="gemini/identical-same-sha",
        artifact=artifact,
    )
    prior_run = _seed_real_duplicate_guard_run(
        spec_path,
        source_base_sha=_git_output(repo, "rev-parse", "main"),
        verify_passed=True,
    )
    marker = tmp_path / "cli-invoked"
    _set_real_gemini_probe(monkeypatch, marker=marker, artifact=artifact)

    rc = dispatch(spec_path)

    assert rc == 0
    assert not marker.exists()
    run_dirs = sorted(path for path in (tmp_path / "runs").iterdir() if path.is_dir())
    assert len(run_dirs) == 2
    satisfied_run = run_dirs[-1]
    summary = json.loads(
        (satisfied_run / "cli.summary.json").read_text(encoding="utf-8")
    )
    assert summary["cli_invoked"] is False
    assert summary["duration_seconds"] == 0.0
    assert summary["run_reuse"]["reused"] is True
    assert summary["verify_passed"] is True
    assert summary["satisfied_by_prior_run"] == str(prior_run.resolve())
    assert summary["review_model_evidence"]["effective_cli"] == "agy"
    assert (
        summary["review_model_evidence"]["effective_model"]
        == "gemini/3.6-flash-agy"
    )
    assert json.loads(
        (satisfied_run / "task.json").read_text(encoding="utf-8")
    ) == json.loads((prior_run / "task.json").read_text(encoding="utf-8"))
    assert (satisfied_run / "cli.stdout.txt").read_text(encoding="utf-8") == ""
    assert "Reused from the prior successful run; acceptance was not re-run." in (
        satisfied_run / "report.md"
    ).read_text(encoding="utf-8")


def _run_duplicate_guard_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cli_results: list[AdapterResult],
    between_attempts: Callable[[Path, Path], None] | None = None,
) -> tuple[list[dict[str, object]], tuple[int, int], list[Path]]:
    """Run the real dispatch funnel twice with only external work dependencies faked."""

    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(tmp_path / "repo"),
            "worktree_branch": "codex/duplicate-guard",
            "runs_dir": str(tmp_path / "runs"),
            "base_ref": "base-ref-X",
            "allowed_paths": ["src/**"],
            "acceptance": ["python -c pass"],
        },
    )
    repo = _init_dispatch_repo(tmp_path)
    _git(repo, "branch", "-M", "base-ref-X")
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    pending_results = list(cli_results)
    cli_calls: list[dict[str, object]] = []

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cli_calls.append(dict(kwargs))
        if not pending_results:
            raise AssertionError("run_cli was invoked more often than expected")
        return pending_results.pop(0)

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="codex/duplicate-guard",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        first_rc = dispatch(spec_path)
        prior_run = next(path for path in (tmp_path / "runs").iterdir() if path.is_dir())
        if between_attempts is not None:
            between_attempts(spec_path, prior_run)
        second_rc = dispatch(spec_path)

    run_dirs = sorted(
        path for path in (tmp_path / "runs").iterdir() if path.is_dir()
    )
    return cli_calls, (first_rc, second_rc), run_dirs


def _write_consecutive_cap_spec(tmp_path: Path) -> Path:
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(tmp_path / "repo"),
            "worktree_branch": "codex/consecutive-cap",
            "runs_dir": str(tmp_path / "runs"),
            "base_ref": "base-ref-X",
            "allowed_paths": ["src/**"],
            "acceptance": ["python -c pass"],
        },
    )
    repo = _init_dispatch_repo(tmp_path)
    _git(repo, "branch", "-M", "base-ref-X")
    return spec_path


def _seed_consecutive_cap_run(
    spec_path: Path,
    *,
    run_time: dt.datetime,
    task_id: str | None = None,
    base_ref: str | None = None,
) -> Path:
    task = load_task(spec_path)
    assert task.runs_dir is not None
    run_dir = task.runs_dir / run_time.strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True)
    task_record = json.loads(json.dumps(asdict(task), default=str))
    if task_id is not None:
        task_record["id"] = task_id
    if base_ref is not None:
        task_record["base_ref"] = base_ref
    (run_dir / "task.json").write_text(
        json.dumps(task_record, indent=2),
        encoding="utf-8",
    )
    return run_dir


def _run_dispatch_with_fake_runtime(
    spec_path: Path,
    tmp_path: Path,
    *,
    cli_result: AdapterResult | None = None,
) -> tuple[int, list[dict[str, object]]]:
    return_codes, cli_calls = _run_dispatch_attempts_with_fake_runtime(
        spec_path,
        tmp_path,
        attempts=1,
        cli_result=cli_result,
    )
    return return_codes[0], cli_calls


def _run_dispatch_attempts_with_fake_runtime(
    spec_path: Path,
    tmp_path: Path,
    *,
    attempts: int,
    cli_result: AdapterResult | None = None,
) -> tuple[list[int], list[dict[str, object]]]:
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir(exist_ok=True)
    task = load_task(spec_path)
    cli_calls: list[dict[str, object]] = []

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cli_calls.append(dict(kwargs))
        return cli_result or _successful_adapter_result()

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=task.target_repo,
                worktree_path=worktree_path,
                branch=task.worktree_branch,
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        return_codes = [dispatch(spec_path) for _attempt in range(attempts)]
    return return_codes, cli_calls


def test_dispatch_refuses_fourth_consecutive_recent_dispatch_at_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    monkeypatch.setenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", "1")
    return_codes, cli_calls = _run_dispatch_attempts_with_fake_runtime(
        spec_path,
        tmp_path,
        attempts=3,
    )
    assert return_codes == [0, 0, 0]
    assert len(cli_calls) == 3
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN")
    prior_runs = sorted(
        path for path in (tmp_path / "runs").iterdir() if path.is_dir()
    )

    with (
        caplog.at_level(logging.ERROR, logger="atlas_dispatch.dispatcher"),
        patch(
            "atlas_dispatch.dispatcher.run_cli",
            side_effect=AssertionError("cap should refuse before invoking the CLI"),
        ) as run_cli_mock,
    ):
        rc = dispatch(spec_path)

    assert rc == 1
    run_cli_mock.assert_not_called()
    run_dirs = sorted(
        path for path in (tmp_path / "runs").iterdir() if path.is_dir()
    )
    assert len(run_dirs) == 4
    refusal_run = run_dirs[-1]
    assert {
        "changed_files.txt",
        "cli.stderr.txt",
        "cli.stdout.txt",
        "cli.summary.json",
        "prompt.md",
        "report.md",
        "task.json",
    }.issubset(path.name for path in refusal_run.iterdir())
    summary = json.loads(
        (refusal_run / "cli.summary.json").read_text(encoding="utf-8")
    )
    refusal_marker = json.loads(
        (refusal_run / "consecutive_dispatch_cap_refusal.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["classification"]["kind"] == "consecutive_dispatch_cap_reached"
    assert summary["cli_invoked"] is False
    assert summary["changed_files"] == "UNKNOWN"
    assert summary["changed_files_state"] == "unknown"
    assert summary["changed_files_unknown_reason"] == (
        "cli_not_invoked: consecutive_dispatch_cap_reached"
    )
    assert (refusal_run / "changed_files.txt").read_text(encoding="utf-8") == (
        "UNKNOWN: cli_not_invoked: consecutive_dispatch_cap_reached"
    )
    assert summary["retry_policy"] == {
        "retry": False,
        "status": "stop_and_investigate",
        "reason": "consecutive_dispatch_cap_reached",
    }
    assert summary["consecutive_dispatch_cap"] == {
        "refused": True,
        "task_id": "D-TEST",
        "base_ref": "base-ref-X",
        "consecutive_recent_runs": 3,
        "cap": 3,
        "window_seconds": 300,
        "prior_run_paths": [str(path.resolve()) for path in reversed(prior_runs)],
        "cli_invoked": False,
    }
    assert refusal_marker == {
        "classification": "consecutive_dispatch_cap_reached",
        "retry_policy": summary["retry_policy"],
        "consecutive_dispatch_cap": summary["consecutive_dispatch_cap"],
    }
    report = (refusal_run / "report.md").read_text(encoding="utf-8")
    assert "CLI invocation: **REFUSED**" in report
    assert "Consecutive recent runs: **3**" in report
    assert "No CLI was invoked for this attempt." in report
    assert "## Changed files\n\n**UNKNOWN**" in report
    stdout = capsys.readouterr().out
    assert "CONSECUTIVE DISPATCH CAP REACHED" in stdout
    assert "task=D-TEST" in stdout
    assert "base_ref=base-ref-X" in stdout
    assert "consecutive_recent_runs=3" in stdout
    for prior_run in prior_runs:
        assert str(prior_run.resolve()) in stdout
    assert any(
        "CONSECUTIVE DISPATCH CAP REACHED" in record.getMessage()
        for record in caplog.records
    )




def test_dispatch_consecutive_cap_ignores_different_base_refs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    for offset in (90, 60, 30):
        _seed_consecutive_cap_run(
            spec_path,
            run_time=now - dt.timedelta(seconds=offset),
            base_ref="base-ref-Y",
        )

    rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert rc == 0
    assert len(cli_calls) == 1


def test_dispatch_consecutive_cap_ignores_runs_outside_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    for offset in (360, 420, 480):
        _seed_consecutive_cap_run(
            spec_path,
            run_time=now - dt.timedelta(seconds=offset),
        )

    rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert rc == 0
    assert len(cli_calls) == 1


def test_dispatch_consecutive_cap_override_runs_cli_and_logs_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", "1")
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    return_codes, prior_cli_calls = _run_dispatch_attempts_with_fake_runtime(
        spec_path,
        tmp_path,
        attempts=3,
    )
    assert return_codes == [0, 0, 0]
    assert len(prior_cli_calls) == 3
    prior_runs = sorted(
        path for path in (tmp_path / "runs").iterdir() if path.is_dir()
    )
    for prior_run in prior_runs:
        assert (prior_run / "cli.summary.json").is_file()
        assert (prior_run / "prompt.md").is_file()

    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN")
    monkeypatch.setenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", "1")
    rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert rc == 0
    assert len(cli_calls) == 1
    assert len(
        [path for path in (tmp_path / "runs").iterdir() if path.is_dir()]
    ) == 4
    stdout = capsys.readouterr().out
    assert "consecutive-dispatch override honoured" in stdout
    assert "ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH=1" in stdout
    assert "consecutive_recent_runs=3" in stdout
    assert "DUPLICATE RUN SUPPRESSED" not in stdout
    for prior_run in prior_runs:
        assert str(prior_run.resolve()) in stdout


def test_dispatch_consecutive_cap_future_evidence_counts_toward_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A future-dated run dir is runaway evidence, not permission to dispatch.

    A runaway can drift its own directory names into the future (see
    `_new_run_dir`), and a cap that ignored future-dated names would thereby be
    switched off by the very loop it exists to stop. The names ARE the symptom,
    so they must count.
    """

    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    for offset in (600, 660, 720):
        _seed_consecutive_cap_run(
            spec_path,
            run_time=now + dt.timedelta(seconds=offset),
        )

    with caplog.at_level(logging.WARNING, logger="atlas_dispatch.dispatcher"):
        _rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert cli_calls == []
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "counting implausibly future run directory" in message
        for message in messages
    )


def test_dispatch_consecutive_cap_unreadable_runs_root_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Blind means refuse. Breaking the guard's inputs must not be a bypass."""

    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)

    with (
        caplog.at_level(logging.WARNING, logger="atlas_dispatch.dispatcher"),
        patch.object(Path, "iterdir", side_effect=PermissionError("denied")),
    ):
        _rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert cli_calls == []
    stdout = capsys.readouterr().out
    assert "REFUSED" in stdout.upper() or "CAP REACHED" in stdout.upper()


def test_dispatch_consecutive_cap_malformed_run_dir_is_skipped_not_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One junk directory must neither switch the cap OFF nor freeze dispatch.

    Fail-closed is scoped to the risk class: inability to read the WHOLE history
    refuses, but a single unparseable entry is skipped and evaluation continues.
    Otherwise any stray directory would block a task's dispatch forever.
    """

    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    malformed = tmp_path / "runs" / "not-a-timestamp"
    malformed.mkdir(parents=True)

    with caplog.at_level(logging.WARNING, logger="atlas_dispatch.dispatcher"):
        rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert rc == 0
    assert len(cli_calls) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any("skipping malformed run directory" in message for message in messages)
    assert not any("cap could not be evaluated" in message for message in messages)


def test_absolute_run_cap_refuses_regardless_of_timestamps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The clock-independent backstop: many run dirs refuse on COUNT alone.

    Every directory here is stamped far in the PAST, so the five-minute window
    cap sees nothing and would happily dispatch. This cap must still refuse —
    that independence is the whole point, because the window cap is exactly what
    a clock-drifting runaway can defeat.
    """

    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_CONSECUTIVE_DISPATCH", raising=False)
    monkeypatch.setattr(dispatcher_mod, "ABSOLUTE_DISPATCH_RUN_CAP", 5)
    spec_path = _write_consecutive_cap_spec(tmp_path)
    old = dt.datetime.now(dt.UTC).replace(microsecond=0) - dt.timedelta(days=30)
    for index in range(5):
        _seed_consecutive_cap_run(
            spec_path,
            run_time=old + dt.timedelta(seconds=index * 3600),
        )

    _rc, cli_calls = _run_dispatch_with_fake_runtime(spec_path, tmp_path)

    assert cli_calls == []
    assert "ABSOLUTE RUN CAP REACHED" in capsys.readouterr().out


def test_new_run_dir_collision_suffixes_instead_of_advancing_the_clock(
    tmp_path: Path,
) -> None:
    """Same-second collisions must not push the NAME into the future.

    Adding a second per collision would let a loop faster than 1/s drift names
    arbitrarily far ahead of real time, which disables the window cap. Every
    name here must still parse to the same real instant.
    """

    runs_root = tmp_path / "runs"
    made = [dispatcher_mod._new_run_dir(runs_root) for _ in range(50)]

    assert len({path.name for path in made}) == 50
    stamps = {dispatcher_mod._parse_run_dir_timestamp(path.name) for path in made}
    assert None not in stamps
    # 50 collisions would drift the newest name 49s ahead; allow only the
    # ~1s of genuine wall-clock movement this loop can consume.
    assert max(stamps) - min(stamps) <= dt.timedelta(seconds=1)


def test_dispatch_duplicate_success_skips_cli_and_writes_complete_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_calls, return_codes, run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result()],
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 1
    assert len(run_dirs) == 2
    prior_run, satisfied_run = run_dirs
    assert {
        "changed_files.txt",
        "cli.stderr.txt",
        "cli.stdout.txt",
        "cli.summary.json",
        "prompt.md",
        "report.md",
        "task.json",
    }.issubset(path.name for path in satisfied_run.iterdir())
    summary = json.loads(
        (satisfied_run / "cli.summary.json").read_text(encoding="utf-8")
    )
    assert "post_run_incomplete" not in summary
    assert summary["classification"]["kind"] == "success"
    assert summary["cli_invoked"] is False
    assert summary["satisfied_by_prior_run"] == str(prior_run.resolve())
    assert summary["duplicate_run"] == {
        "prior_run_directory": prior_run.name,
        "prior_run_path": str(prior_run.resolve()),
        "base_ref": "base-ref-X",
        "source_base_sha": _git_output(tmp_path / "repo", "rev-parse", "base-ref-X"),
        "worktree_branch": "codex/duplicate-guard",
        "allowed_paths": ["src/**"],
        "cli_invoked": False,
    }
    assert (satisfied_run / "changed_files.txt").read_text(
        encoding="utf-8"
    ) == "src/app.py"
    report = (satisfied_run / "report.md").read_text(encoding="utf-8")
    assert f"SATISFIED BY prior run: `{prior_run.resolve()}`" in report
    assert "No CLI was invoked for this attempt." in report
    stdout = capsys.readouterr().out
    assert "DUPLICATE RUN SUPPRESSED" in stdout
    assert f"prior_run_dir={prior_run.resolve()}" in stdout
    assert "base_ref=base-ref-X" in stdout


@pytest.mark.parametrize(
    "prior_result",
    [
        _failed_adapter_result(),
        AdapterResult(
            cli="codex",
            exit_code=124,
            stdout="",
            stderr="timed out",
            duration_seconds=1.0,
            command=["codex"],
            timed_out=True,
            classification=Classification(
                kind=DispatchErrorKind.TIMEOUT,
                suggested_action="retry",
            ),
        ),
        AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr="rate limited",
            duration_seconds=0.0,
            command=["codex"],
            classification=Classification(
                kind=DispatchErrorKind.RATE_LIMITED,
                suggested_action="retry later",
            ),
        ),
    ],
    ids=["failed", "timed-out", "rate-limited"],
)
def test_dispatch_prior_non_success_does_not_suppress_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_result: AdapterResult,
) -> None:
    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[prior_result, _successful_adapter_result()],
    )

    assert return_codes == (1, 0)
    assert len(cli_calls) == 2


def test_dispatch_different_base_ref_does_not_suppress_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def change_base_ref(spec_path: Path, _prior_run: Path) -> None:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        spec["base_ref"] = "base-ref-Y"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=change_base_ref,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2


@pytest.mark.parametrize(
    "artifact,mutation",
    [
        ("cli.summary.json", "corrupt"),
        ("cli.summary.json", "missing"),
        ("task.json", "corrupt"),
    ],
)
def test_dispatch_unreadable_prior_evidence_does_not_suppress_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact: str,
    mutation: str,
) -> None:
    def damage_prior_artifact(_spec_path: Path, prior_run: Path) -> None:
        artifact_path = prior_run / artifact
        if mutation == "missing":
            artifact_path.unlink()
        else:
            artifact_path.write_text("{not-json", encoding="utf-8")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=damage_prior_artifact,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2


def test_dispatch_requires_nested_success_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def flatten_classification(_spec_path: Path, prior_run: Path) -> None:
        summary_path = prior_run / "cli.summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["classification"] = "success"
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=flatten_classification,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2


@pytest.mark.parametrize("replacement", [None, False, "true"])
def test_dispatch_requires_literal_true_verify_passed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: object,
) -> None:
    def damage_verify_evidence(_spec_path: Path, prior_run: Path) -> None:
        summary_path = prior_run / "cli.summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if replacement is None:
            summary.pop("verify_passed")
        else:
            summary["verify_passed"] = replacement
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=damage_verify_evidence,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2


def test_dispatch_missing_source_sha_does_not_suppress_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def remove_source_sha(_spec_path: Path, prior_run: Path) -> None:
        summary_path = prior_run / "cli.summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary.pop("source_base_sha")
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=remove_source_sha,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2


def test_dispatch_changed_rendered_prompt_does_not_suppress_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def change_prompt(spec_path: Path, _prior_run: Path) -> None:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        Path(spec["prompt_template"]).write_text("Changed task.\n", encoding="utf-8")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=change_prompt,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2


def test_dispatch_duplicate_override_runs_cli_and_logs_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def enable_override(_spec_path: Path, _prior_run: Path) -> None:
        monkeypatch.setenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", "1")

    cli_calls, return_codes, _run_dirs = _run_duplicate_guard_attempts(
        tmp_path,
        monkeypatch,
        cli_results=[_successful_adapter_result(), _successful_adapter_result()],
        between_attempts=enable_override,
    )

    assert return_codes == (0, 0)
    assert len(cli_calls) == 2
    stdout = capsys.readouterr().out
    assert "duplicate-run override honored" in stdout
    assert "ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN=1" in stdout


def test_dispatch_serializes_concurrent_attempts_before_duplicate_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ATLAS_DISPATCH_ALLOW_DUPLICATE_RUN", raising=False)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(tmp_path / "repo"),
            "worktree_branch": "codex/concurrent-duplicate-guard",
            "runs_dir": str(tmp_path / "runs"),
            "base_ref": "base-ref-X",
            "allowed_paths": ["src/**"],
            "acceptance": ["python -c pass"],
        },
    )
    repo = _init_dispatch_repo(tmp_path)
    _git(repo, "branch", "-M", "base-ref-X")
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    first_cli_entered = threading.Event()
    release_first_cli = threading.Event()
    second_cli_entered = threading.Event()
    call_lock = threading.Lock()
    cli_call_count = 0
    return_codes: list[int] = []
    errors: list[BaseException] = []

    def fake_run_cli(**_kwargs: object) -> AdapterResult:
        nonlocal cli_call_count
        with call_lock:
            cli_call_count += 1
            call_number = cli_call_count
        if call_number == 1:
            first_cli_entered.set()
            if not release_first_cli.wait(timeout=5.0):
                raise AssertionError("test did not release the first CLI invocation")
        else:
            second_cli_entered.set()
        return _successful_adapter_result()

    def run_dispatch() -> None:
        try:
            return_codes.append(dispatch(spec_path))
        except BaseException as exc:  # pragma: no cover - assertion handoff
            errors.append(exc)

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="codex/concurrent-duplicate-guard",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        first = threading.Thread(target=run_dispatch)
        second = threading.Thread(target=run_dispatch)
        first.start()
        assert first_cli_entered.wait(timeout=5.0)
        second.start()
        assert not second_cli_entered.wait(timeout=0.2)
        release_first_cli.set()
        first.join(timeout=5.0)
        second.join(timeout=5.0)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sorted(return_codes) == [0, 0]
    assert cli_call_count == 1
    assert len([path for path in (tmp_path / "runs").iterdir() if path.is_dir()]) == 2


def _report_verify_passed(run_dir: Path) -> bool:
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    if "- Verify passed: **True**" in report:
        return True
    if "- Verify passed: **False**" in report:
        return False
    raise AssertionError("report.md did not contain a Verify passed line")


def _dispatch_and_capture_legacy_mcp_servers(
    tmp_path: Path,
    extra: dict[str, object],
) -> list[dict[str, object]]:
    spec_path = _write_task_spec(
        tmp_path,
        {
            "cli": "kimi",
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["python -c pass"],
            **extra,
        },
    )
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    captured: dict[str, list[dict[str, object]]] = {}

    def fake_write_legacy_mcp_config(
        worktree: Path, mcp_servers: list[dict[str, object]]
    ) -> Path:
        captured["mcp_servers"] = mcp_servers
        return worktree / ".atlas-dispatch-mcp.json"

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="codex/test",
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._write_legacy_mcp_config",
            side_effect=fake_write_legacy_mcp_config,
        ),
        patch(
            "atlas_dispatch.dispatcher.run_cli",
            return_value=_successful_adapter_result("kimi"),
        ),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        rc = dispatch(spec_path)

    assert rc == 0
    return captured["mcp_servers"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_dispatch_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    return repo




def test_dispatch_records_unknown_when_worktree_disappears_before_diff(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    runs_dir = tmp_path / "runs"
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/worktree-gone-before-diff",
            "runs_dir": str(runs_dir),
        },
    )

    def remove_worktree_during_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        _git(repo, "worktree", "remove", "--force", str(cwd))
        return _successful_adapter_result()

    with (
        patch(
            "atlas_dispatch.dispatcher.run_cli",
            side_effect=remove_worktree_during_cli,
        ),
        pytest.raises(RuntimeError, match="Rescue artifact:"),
    ):
        dispatch(spec_path)

    run_dir = next(runs_dir.iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["changed_files"] == "UNKNOWN", (
        "a missing worktree must not be recorded as an empty diff"
    )
    assert summary["changed_files_state"] == "unknown"
    assert summary["cli_invoked"] is True
    assert "post_run_incomplete" not in summary
    assert summary["changed_files_unknown_reason"].startswith(
        "diff_not_observed_after_cli_invocation: dispatch_exception"
    )


def test_dispatch_real_review_template_reaches_completion_evidence_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    review_path = "reviews/real-template-review.md"
    runs_dir = tmp_path / "runs"
    script = (
        "from pathlib import Path; import sys; "
        f"path = Path({review_path!r}); "
        "path.parent.mkdir(parents=True, exist_ok=True); "
        "path.write_text('# complete review\\n', encoding='utf-8'); "
        "sys.stderr.write('Error: timeout waiting for response\\n'); "
        "raise SystemExit(7)"
    )
    monkeypatch.setenv(
        "ATLAS_DISPATCH_GEMINI_CMD",
        shlex.join([sys.executable, "-c", script]),
    )
    spec_path = _write_task_spec(
        tmp_path,
        {
            "id": "REVIEW-REAL-TEMPLATE",
            "title": "Real review template regression",
            "target_repo": str(repo),
            "cli": "gemini",
            "model_id": "gemini-3.6-flash",
            "reasoning_effort": "high",
            "prompt_template": str(
                Path(__file__).resolve().parents[1]
                / "examples"
                / "prompts"
                / "review.md"
            ),
            "worktree_branch": "gemini/real-template-regression",
            "allowed_paths": ["reviews/**"],
            "acceptance": [f"test -f {review_path}"],
            "runs_dir": str(runs_dir),
            "extra_prompt_vars": {
                "review_output_path": review_path,
                "source_task_id": "TASK-SOURCE",
                "source_agent": "codex-builder",
                "source_branch": "codex/source",
                "source_remote_ref": "origin/codex/source",
                "source_run_report": "",
                "source_run_summary": "Source run completed.",
                "source_changed_files": "- `atlas_dispatch/adapter.py`",
            },
        },
    )

    rc = dispatch(spec_path)

    assert rc == 0
    run_dir = next(runs_dir.iterdir())
    rendered_prompt = (run_dir / "prompt.md").read_text(encoding="utf-8")
    assert (
        "the harness will save it — it won't. After you exit, the dispatcher runs\n"
        f"`test -f {review_path}`"
    ) in rendered_prompt
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["classification"]["kind"] == "success"
    assert summary["exit_code"] == 0
    assert summary["run_identity"]["process_exit_code"] == 7
    assert summary["acceptance_outcome"] == "passed"
    assert summary["verify_passed"] is True


def test_verification_summary_carries_not_attempted_reason() -> None:
    report = verify_mod.VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        command_results=[
            CheckResult(
                name="import provenance guard",
                passed=False,
                details="import provenance guard refused acceptance: stale_import_binding",
                classification="stale_import_binding",
                pytest_counts="unavailable",
            )
        ],
    )

    summary = dispatcher_mod._verification_summary(
        report, verified_head_sha="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a"
    )

    assert summary["acceptance_outcome"] == "not_attempted"
    assert summary["acceptance_failed"] is True
    assert summary["verify_passed"] is False
    assert summary["acceptance_not_attempted_reason"] == (
        report.acceptance_not_attempted_reason
    )
    assert summary["acceptance_not_attempted_reason"]


def _init_dispatch_repo_with_local_only_base(tmp_path: Path) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    repo = _init_dispatch_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")

    base_ref = "implementer/local-only"
    _git(repo, "checkout", "-b", base_ref)
    (repo / "feature.txt").write_text("local only\n", encoding="utf-8")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-m", "local-only implementation")
    _git(repo, "checkout", "main")

    assert _git_output(repo, "ls-remote", "--heads", "origin", base_ref) == ""
    return repo, base_ref


def test_run_uses_local_only_base_ref_without_fetching_origin(tmp_path: Path) -> None:
    repo, base_ref = _init_dispatch_repo_with_local_only_base(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "base_ref": base_ref,
            "worktree_branch": "codex/review-local-only",
            "allowed_paths": ["review.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["test -f review.txt"],
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        assert (cwd / "feature.txt").read_text(encoding="utf-8") == "local only\n"
        (cwd / "review.txt").write_text("reviewed\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = main(["run", str(spec_path)])

    assert rc == 0
    assert _git_output(repo, "merge-base", base_ref, "codex/review-local-only") == (
        _git_output(repo, "rev-parse", base_ref)
    )


def test_local_only_base_ref_preserves_no_change_classification(tmp_path: Path) -> None:
    repo, base_ref = _init_dispatch_repo_with_local_only_base(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "base_ref": base_ref,
            "worktree_branch": "codex/review-local-only-no-change",
            "runs_dir": str(tmp_path / "runs"),
        },
    )

    with patch(
        "atlas_dispatch.dispatcher.run_cli",
        return_value=_successful_adapter_result(),
    ):
        rc = main(["run", str(spec_path)])

    assert rc == 1
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["classification"]["kind"] == "no_change"




def test_interrupted_post_run_is_marked_incomplete_and_distinct_from_completed_no_op(
    tmp_path: Path,
) -> None:
    """A real committed run interrupted before verification must fail loud on disk."""

    partial_case = tmp_path / "partial"
    partial_case.mkdir()
    partial_repo = _init_dispatch_repo(partial_case)
    partial_origin = partial_case / "origin.git"
    _git(partial_case, "init", "--bare", str(partial_origin))
    _git(partial_repo, "remote", "add", "origin", str(partial_origin))
    _git(partial_repo, "push", "-u", "origin", "main")
    partial_branch = "codex/interrupted-post-run"
    partial_spec = _write_task_spec(
        partial_case,
        {
            "target_repo": str(partial_repo),
            "worktree_branch": partial_branch,
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(partial_case / "runs"),
            "acceptance": ["test -f generated.txt"],
            "push_branch": True,
            "timeout_seconds": 9000,
        },
    )

    def write_committed_artifact(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("real work\n", encoding="utf-8")
        return _successful_adapter_result()

    with (
        patch(
            "atlas_dispatch.dispatcher.run_cli",
            side_effect=write_committed_artifact,
        ),
        patch(
            "atlas_dispatch.dispatcher.check_allowlist",
            side_effect=KeyboardInterrupt("simulated abrupt post-run interruption"),
        ),
        pytest.raises(
            KeyboardInterrupt,
            match="simulated abrupt post-run interruption",
        ),
    ):
        dispatch(partial_spec)

    assert _git_output(
        partial_repo,
        "rev-list",
        "--count",
        f"main..{partial_branch}",
    ) == "1"
    assert _git_output(partial_repo, "show", f"{partial_branch}:generated.txt") == (
        "real work"
    )
    partial_run = next((partial_case / "runs").iterdir())
    partial_summary = json.loads(
        (partial_run / "cli.summary.json").read_text(encoding="utf-8")
    )
    assert partial_summary.get("post_run_incomplete") is True, (
        "interrupted post-run summary must carry post_run_incomplete: true"
    )
    assert partial_summary["exit_code"] == 0
    assert partial_summary["classification"]["kind"] == "post_run_incomplete"
    assert partial_summary["cli_classification"]["kind"] == "success"
    assert partial_summary["changed_files"] == ["generated.txt"]
    assert partial_summary["changed_files_state"] == "observed"
    assert partial_summary["verify_passed"] is False
    assert partial_summary["acceptance_failed"] is False
    assert partial_summary["acceptance_outcome"] == "skipped"
    assert partial_summary["verification_state"] == "POST_RUN_INCOMPLETE"
    assert partial_summary["acceptance_commands"] == []
    assert partial_summary["post_run"]["state"] == "incomplete"
    assert partial_summary["post_run"]["started_at"] != "not_recorded"
    assert partial_summary["post_run"]["ended_at"] != "not_recorded"
    assert partial_summary["post_run"]["supervising_timeout_seconds"] == 9000
    branch_publish = json.loads(
        (partial_run / "branch_publish.json").read_text(encoding="utf-8")
    )
    assert branch_publish["pushed"] is True
    assert _git_output(
        partial_repo,
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{partial_branch}",
    ).endswith(f"refs/heads/{partial_branch}")
    partial_report = (partial_run / "report.md").read_text(encoding="utf-8")
    assert partial_report.startswith(
        "# INCOMPLETE RUN: post-run phase did not complete"
    )
    assert "not an acceptance verdict" in partial_report
    assert "**SKIPPED (POST-RUN INCOMPLETE)**" in partial_report
    assert "- Pushed: **True**" in partial_report
    assert "(none)" not in partial_report

    no_op_case = tmp_path / "no-op"
    no_op_case.mkdir()
    no_op_repo = _init_dispatch_repo(no_op_case)
    no_op_spec = _write_task_spec(
        no_op_case,
        {
            "target_repo": str(no_op_repo),
            "worktree_branch": "codex/completed-no-op",
            "runs_dir": str(no_op_case / "runs"),
            "acceptance": ["test -f README.md"],
        },
    )
    with patch(
        "atlas_dispatch.dispatcher.run_cli",
        return_value=_successful_adapter_result(),
    ):
        no_op_rc = dispatch(no_op_spec)

    assert no_op_rc == 1
    no_op_run = next((no_op_case / "runs").iterdir())
    no_op_summary = json.loads(
        (no_op_run / "cli.summary.json").read_text(encoding="utf-8")
    )
    assert "post_run_incomplete" not in no_op_summary
    assert no_op_summary["classification"]["kind"] == "no_change"
    assert no_op_summary["changed_files"] == []
    assert no_op_summary["changed_files_state"] == "empty"
    assert no_op_summary["verify_passed"] is False
    assert no_op_summary["acceptance_failed"] is True
    assert no_op_summary["acceptance_outcome"] == "skipped"
    assert no_op_summary["verified_head_sha"] == _git_output(
        no_op_repo,
        "rev-parse",
        "codex/completed-no-op",
    )
    assert no_op_summary["acceptance_commands"] == []
    no_op_report = (no_op_run / "report.md").read_text(encoding="utf-8")
    assert "# INCOMPLETE RUN" not in no_op_report
    assert "## Changed files\n\n(none)" in no_op_report
    # The report says WHICH fact False means: a no-op produced no
    # code, which is not the same as code that failed verification. The assertion
    # keeps its original intent -- False, and distinct from an incomplete run --
    # without pinning prose that deliberately changed.
    assert "- Verify passed: **False" in no_op_report
    assert "NOTHING WAS BUILT, not that a build failed" in no_op_report
    assert "NOT A VERDICT" in no_op_report


def test_hard_kill_during_acceptance_leaves_published_explicit_non_verdict(
    tmp_path: Path,
) -> None:
    """Kill the real dispatcher mid-acceptance and inspect only durable artifacts."""

    repo = _init_dispatch_repo(tmp_path)
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", str(origin))
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    branch = "codex/hard-killed-post-run"
    acceptance_pid = tmp_path / "acceptance.pid"
    acceptance_script = (
        "from pathlib import Path; import os, time; "
        f"Path({str(acceptance_pid)!r}).write_text(str(os.getpid())); "
        "time.sleep(60)"
    )
    spec_path = _write_task_spec(
        tmp_path,
        {
            "id": "D-HARD-KILL",
            "target_repo": str(repo),
            "worktree_branch": branch,
            "runs_dir": str(tmp_path / "runs"),
            "push_branch": True,
            "acceptance": [f"{shlex.quote(sys.executable)} -c {shlex.quote(acceptance_script)}"],
        },
    )
    cli_script = (
        "from pathlib import Path; "
        "Path('generated.txt').write_text('preserved before acceptance\\n'); "
        "print('build complete')"
    )
    env = {
        **os.environ,
        "ATLAS_DISPATCH_CODEX_CMD": shlex.join(
            [sys.executable, "-c", cli_script]
        ),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "atlas_dispatch", "run", str(spec_path)],
        cwd=Path.cwd(),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    run_dir: Path | None = None
    deadline = time.monotonic() + 15.0
    try:
        while time.monotonic() < deadline:
            run_dirs = list((tmp_path / "runs").glob("*"))
            if run_dirs:
                candidate = run_dirs[0]
                lifecycle_path = candidate / "post_run.json"
                publish_path = candidate / "branch_publish.json"
                if lifecycle_path.is_file() and publish_path.is_file():
                    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
                    publication = json.loads(publish_path.read_text(encoding="utf-8"))
                    if (
                        lifecycle.get("state") == "running"
                        and publication.get("pushed") is True
                        and acceptance_pid.is_file()
                    ):
                        run_dir = candidate
                        break
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "dispatcher exited before the kill point: "
                    f"rc={process.returncode} stdout={stdout} stderr={stderr}"
                )
            time.sleep(0.05)
        assert run_dir is not None, "dispatcher never reached live acceptance"

        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=5) == -signal.SIGKILL
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if acceptance_pid.is_file():
            try:
                os.kill(int(acceptance_pid.read_text(encoding="utf-8")), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    assert run_dir is not None
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert summary["classification"]["kind"] == "post_run_incomplete"
    assert summary["post_run_incomplete"] is True
    assert summary["post_run"]["state"] == "running"
    assert summary["changed_files_state"] == "observed"
    assert summary["changed_files"] == ["generated.txt"]
    assert summary["verification_state"] == "POST_RUN_INCOMPLETE"
    assert summary["verify_passed"] is False
    assert summary["acceptance_failed"] is False
    assert summary["acceptance_outcome"] == "skipped"
    assert "POST-RUN IN PROGRESS" in report
    assert "not an acceptance verdict" in report
    assert _git_output(
        repo,
        "ls-remote",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
    ).endswith(f"refs/heads/{branch}")


@pytest.mark.parametrize(
    "kind",
    [
        DispatchErrorKind.MODEL_SELECTION_ERROR,
        DispatchErrorKind.REFUSED,
        DispatchErrorKind.NO_OUTPUT,
    ],
)
def test_no_change_preserves_specific_exit_zero_classification(
    tmp_path: Path,
    kind: DispatchErrorKind,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": f"codex/no-change-preserves-{kind.value}",
            "runs_dir": str(tmp_path / "runs"),
        },
    )
    cli_result = AdapterResult(
        cli="codex",
        exit_code=0,
        stdout="specific classifier evidence",
        stderr="",
        duration_seconds=0.0,
        command=["codex"],
        classification=Classification(
            kind=kind,
            suggested_action="specific action",
            matched_pattern="specific-pattern",
        ),
    )

    with patch("atlas_dispatch.dispatcher.run_cli", return_value=cli_result):
        rc = dispatch(spec_path)

    assert rc == 1
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["classification"] == {
        "kind": kind.value,
        "suggested_action": "specific action",
        "matched_pattern": "specific-pattern",
    }


def test_no_change_does_not_create_empty_commit(tmp_path: Path) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/no-change-no-empty-commit",
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["python -c pass"],
        },
    )
    before = _git_output(repo, "rev-parse", "main")

    with patch(
        "atlas_dispatch.dispatcher.run_cli",
        return_value=_successful_adapter_result(),
    ):
        dispatch(spec_path)

    after = _git_output(repo, "rev-parse", "codex/no-change-no-empty-commit")
    ahead_count = _git_output(
        repo,
        "rev-list",
        "--count",
        "main..codex/no-change-no-empty-commit",
    )
    assert after == before
    assert ahead_count == "0"




def test_cli_artifacts_persist_sanitized_identity_and_command(tmp_path: Path) -> None:
    secret = "must-not-be-persisted"
    identity = {
        "schema_version": 1,
        "policy_version": "1",
        "cli": "codex",
        "selected_env_keys": ["OPENAI_API_KEY", "PYTHONNOUSERSITE"],
        "forwarded_cli_env_keys": ["OPENAI_API_KEY"],
        "executable": {"resolved_path": "/usr/local/bin/codex", "sha256": "abc"},
        "isolation": {"PYTHONNOUSERSITE": "1"},
        "uv_cache_path": "/tmp/run/uv-cache",
    }
    result = AdapterResult(
        cli="codex",
        exit_code=0,
        stdout="done",
        stderr="",
        duration_seconds=0.1,
        command=[
            "codex",
            "--api-key",
            secret,
            f"--token={secret}",
            f"PASSWORD={secret}",
        ],
        run_identity=identity,
    )

    dispatcher_mod._persist_cli_result(tmp_path, result)

    summary_text = (tmp_path / "cli.summary.json").read_text(encoding="utf-8")
    manifest_text = (tmp_path / "run-identity.json").read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    manifest = json.loads(manifest_text)
    assert summary["command"] == [
        "codex",
        "--api-key",
        "<redacted>",
        "--token=<redacted>",
        "PASSWORD=<redacted>",
    ]
    assert summary["run_identity"] == identity
    assert manifest == identity
    assert secret not in summary_text
    assert secret not in manifest_text


def test_cli_summary_persists_quota_reset_window_with_provenance(
    tmp_path: Path,
) -> None:
    result = AdapterResult(
        cli="gemini",
        exit_code=1,
        stdout="",
        stderr="Individual quota reached.",
        duration_seconds=8.53,
        command=["agy"],
        classification=Classification(
            kind=DispatchErrorKind.QUOTA_EXHAUSTED,
            suggested_action="Wait for the vendor quota reset.",
            quota_reset_window="161h51m52s",
            quota_reset_window_provenance=(
                QuotaResetWindowProvenance.VENDOR_DECLARED
            ),
        ),
    )

    dispatcher_mod._persist_cli_result(tmp_path, result)

    summary = json.loads((tmp_path / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["classification"]["kind"] == "quota_exhausted"
    assert summary["classification"]["quota_reset_window"] == "161h51m52s"
    assert (
        summary["classification"]["quota_reset_window_provenance"]
        == "vendor-declared"
    )


def test_cli_summary_does_not_invent_missing_quota_reset_provenance(
    tmp_path: Path,
) -> None:
    result = AdapterResult(
        cli="gemini",
        exit_code=1,
        stdout="",
        stderr="Individual quota reached.",
        duration_seconds=1,
        command=["agy"],
        classification=Classification(
            kind=DispatchErrorKind.QUOTA_EXHAUSTED,
            suggested_action="Wait for the vendor quota reset.",
        ),
    )

    dispatcher_mod._persist_cli_result(tmp_path, result)

    summary = json.loads((tmp_path / "cli.summary.json").read_text(encoding="utf-8"))
    classification = summary["classification"]
    assert "quota_reset_window" not in classification
    assert "quota_reset_window_provenance" not in classification


def test_scrubbed_ambient_secret_is_absent_from_all_cli_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "ambient-secret-must-not-persist"
    monkeypatch.setenv("SECRET_TOKEN", secret)
    result = run_cli(
        cli="kimi",
        prompt="",
        cwd=tmp_path,
        command=[
            sys.executable,
            "-c",
            (
                "import json, os; "
                "print(json.dumps({'secret': os.environ.get('SECRET_TOKEN')}))"
            ),
        ],
        timeout_seconds=10,
    )

    dispatcher_mod._persist_cli_result(tmp_path, result)

    assert json.loads(result.stdout) == {"secret": None}
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(tmp_path.iterdir())
        if path.is_file()
    )
    assert secret not in artifact_text
    assert {path.name for path in tmp_path.iterdir() if path.is_file()} == {
        "cli.stderr.txt",
        "cli.stdout.txt",
        "cli.summary.json",
        "report.md",
        "run-identity.json",
    }


def test_acceptance_subprocess_keeps_ambient_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "acceptance-env-is-not-scrubbed"
    monkeypatch.setenv("SECRET_TOKEN", secret)
    script = (
        "import os; "
        f"raise SystemExit(os.environ.get('SECRET_TOKEN') != {secret!r})"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    check, exit_code = verify_mod._run_acceptance_shell_command(
        command=command,
        cwd=tmp_path,
        timeout_seconds=10,
    )

    assert check.passed
    assert exit_code == 0




def test_verified_build_exit_does_not_collapse_publish_failure(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/verified-unpublished",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["test -f generated.txt"],
            "push_branch": True,
            "remote_name": "missing-remote",
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("verified\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatch(spec_path)

    assert rc == 0
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    publication = json.loads(
        (run_dir / "branch_publish.json").read_text(encoding="utf-8")
    )
    assert summary["classification"]["kind"] == "success"
    assert summary["verify_passed"] is True
    assert publication["state"] == "publish_failed"
    assert publication["pushed"] is False
    assert publication["reason"]


def test_dispatch_writes_combined_diff_artifact(tmp_path: Path) -> None:
    repo = _init_dispatch_repo(tmp_path)
    branch = "codex/combined-diff"
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": branch,
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["test -f generated.txt"],
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatch(spec_path)

    assert rc == 0
    run_dir = next((tmp_path / "runs").iterdir())
    head = _git_output(repo, "rev-parse", branch)
    short_sha = _git_output(repo, "rev-parse", "--short", branch)
    artifact = run_dir / f"combined-{short_sha}.diff"
    expected = _git_output(repo, "diff", f"main...{head}")

    content = artifact.read_text(encoding="utf-8")
    assert content.strip() == expected
    assert "diff --git a/generated.txt b/generated.txt" in content
    assert "+generated" in content


def test_combined_diff_failure_writes_stub_and_run_succeeds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/combined-diff-failure",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["test -f generated.txt"],
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result()

    def fake_combined_diff_git(
        _worktree_path: Path, args: list[str]
    ) -> subprocess.CompletedProcess[str]:
        if args == ["rev-parse", "HEAD"]:
            return _git_result(args, stdout="abcdef1234567890\n")
        if args == ["rev-parse", "--short", "HEAD"]:
            return _git_result(args, stdout="abcdef1\n")
        if args == ["diff", "main...abcdef1234567890"]:
            return _git_result(
                args,
                returncode=128,
                stderr="fatal: no merge base\n",
            )
        raise AssertionError(f"unexpected git args: {args}")

    with (
        patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli),
        patch(
            "atlas_dispatch.dispatcher._combined_diff_git",
            side_effect=fake_combined_diff_git,
        ),
        caplog.at_level(logging.WARNING, logger="atlas_dispatch.dispatcher"),
    ):
        rc = dispatch(spec_path)

    assert rc == 0
    run_dir = next((tmp_path / "runs").iterdir())
    artifact = run_dir / "combined-abcdef1.diff"
    content = artifact.read_text(encoding="utf-8")
    assert "Combined diff artifact generation failed." in content
    assert "Command: git diff main...abcdef1234567890" in content
    assert "fatal: no merge base" in content
    assert any(
        "combined diff artifact generation failed" in record.message
        for record in caplog.records
    )


def test_dispatch_persists_codex_turn_lifecycle_summary_and_report(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "worktree_branch": "codex/lifecycle-summary",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["test -f generated.txt"],
            "idle_timeout_seconds": 12,
        },
    )
    captured_run: dict[str, object] = {}

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        captured_run.update(kwargs)
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return AdapterResult(
            cli="codex",
            exit_code=0,
            stdout="done",
            stderr="",
            duration_seconds=0.0,
            command=["codex", "exec", "--json"],
            classification=Classification(
                kind=DispatchErrorKind.SUCCESS,
                suggested_action="",
            ),
            turn_lifecycle=[
                {
                    "event": "turn.started",
                    "timestamp_utc": "2026-05-19T00:00:00Z",
                    "payload_keys": [],
                },
                {
                    "event": "turn.completed",
                    "timestamp_utc": "2026-05-19T00:00:01Z",
                    "payload_keys": ["usage"],
                },
            ],
            final_turn_status="completed",
            token_usage={"input": 2, "output": 3, "total": 5},
            idle_classification=None,
        )

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatch(spec_path)

    assert rc == 0
    assert captured_run["idle_timeout_seconds"] == 12
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["final_turn_status"] == "completed"
    assert summary["token_usage"] == {"input": 2, "output": 3, "total": 5}
    assert summary["idle_classification"] is None
    assert summary["turn_lifecycle"][1]["event"] == "turn.completed"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Codex Turn Lifecycle" in report
    assert "Token usage: input=2 output=3 total=5" in report


def test_non_codex_cli_summary_omits_codex_lifecycle_fields(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    spec_path = _write_task_spec(
        tmp_path,
        {
            "target_repo": str(repo),
            "cli": "kimi",
            "worktree_branch": "kimi/no-lifecycle-summary",
            "allowed_paths": ["generated.txt"],
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["test -f generated.txt"],
        },
    )

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        (cwd / "generated.txt").write_text("generated\n", encoding="utf-8")
        return _successful_adapter_result("kimi")

    with patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli):
        rc = dispatch(spec_path)

    assert rc == 0
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert "turn_lifecycle" not in summary
    assert "final_turn_status" not in summary
    assert "token_usage" not in summary
    assert "error_info" not in summary
    assert "idle_classification" not in summary


def test_published_branch_can_seed_review_worktree_from_stale_clone(tmp_path: Path) -> None:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)

    source_repo = tmp_path / "source"
    _git(tmp_path, "init", str(source_repo))
    _git(source_repo, "config", "user.email", "test@example.invalid")
    _git(source_repo, "config", "user.name", "Test User")
    (source_repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(source_repo, "add", "README.md")
    _git(source_repo, "commit", "-m", "base")
    _git(source_repo, "branch", "-M", "main")
    _git(source_repo, "remote", "add", "origin", str(remote))
    _git(source_repo, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    )

    reviewer_repo = tmp_path / "reviewer"
    subprocess.run(
        ["git", "clone", str(remote), str(reviewer_repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(reviewer_repo, "config", "user.email", "test@example.invalid")
    _git(reviewer_repo, "config", "user.name", "Test User")

    build = create_worktree(
        repo_root=source_repo,
        branch="codex/feature-1",
        base_ref="main",
        parent_dir=tmp_path / "source-worktrees",
    )
    (build.worktree_path / "feature.txt").write_text("feature\n", encoding="utf-8")
    assert commit_all(build, message="codex(T-1): feature")

    published = publish_branch(build, remote_name="origin")

    assert published.pushed
    assert published.verification_state == "verified_present"
    assert published.built_sha == _git_output(build.worktree_path, "rev-parse", "HEAD")
    assert published.remote_ref == "origin/codex/feature-1"

    review = create_worktree(
        repo_root=reviewer_repo,
        branch="gemini/review-feature-1",
        base_ref=published.remote_ref,
        parent_dir=tmp_path / "review-worktrees",
    )

    assert (review.worktree_path / "feature.txt").read_text(encoding="utf-8") == "feature\n"


def _real_publish_fixture(tmp_path: Path, branch: str) -> tuple[Path, Path, Worktree]:
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "source"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    worktree = create_worktree(
        repo_root=repo,
        branch=branch,
        base_ref="main",
        parent_dir=tmp_path / "worktrees",
    )
    (worktree.worktree_path / "built.txt").write_text("built\n", encoding="utf-8")
    assert commit_all(worktree, message="build")
    return repo, remote, worktree


def test_push_exit_zero_redirected_to_foreign_commit_fails_publication(
    tmp_path: Path,
) -> None:
    repo, remote, worktree = _real_publish_fixture(tmp_path, "codex/redirected")
    _git(repo, "checkout", "-b", "foreign", "main")
    (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git(repo, "add", "foreign.txt")
    _git(repo, "commit", "-m", "foreign")
    foreign_sha = _git_output(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "foreign:refs/heads/foreign-seed")
    hook = remote / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"git update-ref refs/heads/codex/redirected {foreign_sha}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)

    built_sha = _git_output(worktree.worktree_path, "rev-parse", "HEAD")
    result = publish_branch(worktree, remote_name="origin", built_sha=built_sha)

    assert result.return_code == 0
    assert result.pushed is False
    assert result.state == "publish_failed"
    assert result.verification_state == "verified_absent"
    assert result.remote_head_sha == foreign_sha


def test_remote_safe_descendant_of_built_sha_is_publishable(tmp_path: Path) -> None:
    _repo, _remote, worktree = _real_publish_fixture(tmp_path, "codex/descendant")
    built_sha = _git_output(worktree.worktree_path, "rev-parse", "HEAD")
    (worktree.worktree_path / "descendant.txt").write_text("safe\n", encoding="utf-8")
    assert commit_all(worktree, message="safe descendant")

    result = publish_branch(worktree, remote_name="origin", built_sha=built_sha)

    assert result.pushed is True
    assert result.verification_state == "verified_present"
    assert result.remote_head_sha != built_sha


def test_non_fast_forward_fallback_ref_contains_built_sha(tmp_path: Path) -> None:
    repo, _remote, worktree = _real_publish_fixture(tmp_path, "codex/fallback")
    _git(repo, "checkout", "-b", "foreign", "main")
    (repo / "foreign.txt").write_text("foreign\n", encoding="utf-8")
    _git(repo, "add", "foreign.txt")
    _git(repo, "commit", "-m", "foreign")
    _git(repo, "push", "origin", "foreign:refs/heads/codex/fallback")
    built_sha = _git_output(worktree.worktree_path, "rev-parse", "HEAD")

    result = publish_branch(worktree, remote_name="origin", built_sha=built_sha)

    assert result.pushed is True
    assert result.remote_ref == "origin/codex/fallback-run2"
    assert result.verification_state == "verified_present"


def test_remote_unreachable_is_verification_unavailable(tmp_path: Path) -> None:
    repo, remote, worktree = _real_publish_fixture(tmp_path, "codex/unreachable")
    built_sha = _git_output(worktree.worktree_path, "rev-parse", "HEAD")
    _git(repo, "remote", "set-url", "origin", str(remote.with_name("missing.git")))

    proof = verify_remote_ref_contains_sha(
        repo_root=repo,
        remote_name="origin",
        remote_ref="origin/codex/unreachable",
        built_sha=built_sha,
    )

    assert proof.state == "verification_unavailable"


def test_push_exit_zero_then_remote_loss_is_never_published(tmp_path: Path) -> None:
    _repo, remote, worktree = _real_publish_fixture(tmp_path, "codex/lost-after-push")
    offline = remote.with_name("origin-offline.git")
    hook = remote / "hooks" / "post-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f"mv {shlex.quote(str(remote))} {shlex.quote(str(offline))}\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)
    built_sha = _git_output(worktree.worktree_path, "rev-parse", "HEAD")

    result = publish_branch(worktree, remote_name="origin", built_sha=built_sha)

    assert result.return_code == 0
    assert result.pushed is False
    assert result.state == "publish_failed"
    assert result.verification_state == "verification_unavailable"


def test_task_spec_round_trips_mcp_servers(tmp_path: Path) -> None:
    mcp_servers = [
        {
            "name": "filesystem",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
        {
            "name": "memory",
            "command": "python",
            "args": ["-m", "memory_server"],
            "env": {"MEMORY_PATH": ".mcp-memory.json"},
            "transport": "stdio",
        },
    ]
    task = load_task(_write_task_spec(tmp_path, {"mcp_servers": mcp_servers}))

    assert task.mcp_servers == mcp_servers
    assert all(isinstance(server, dict) for server in task.mcp_servers)


def test_inject_mcp_config_adds_kimi_flag(tmp_path: Path) -> None:
    config_path = tmp_path / ".atlas-dispatch-mcp.json"
    command = ["kimi", "--print", "--input-format", "text"]

    injected = inject_mcp_config("kimi", command, config_path)

    assert injected[1:3] == ["--mcp-config-file", str(config_path)]
    assert injected[3:] == command[1:]
    assert command == ["kimi", "--print", "--input-format", "text"]


def test_inject_mcp_config_adds_claude_flag(tmp_path: Path) -> None:
    config_path = tmp_path / ".atlas-dispatch-mcp.json"
    command = ["claude", "--print", "--input-format", "text"]

    injected = inject_mcp_config("claude", command, config_path)

    assert injected[1:3] == ["--mcp-config", str(config_path)]
    assert injected[3:] == command[1:]
    assert command == ["claude", "--print", "--input-format", "text"]


def test_inject_mcp_config_skips_codex(tmp_path: Path) -> None:
    config_path = tmp_path / ".atlas-dispatch-mcp.json"

    for cli in ("codex", "gemini", "kimi-code"):
        command = [cli, "run"]

        assert inject_mcp_config(cli, command, config_path) == command


def _mcp_servers() -> list[dict[str, object]]:
    return [
        {
            "name": "filesystem",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
        {
            "name": "memory",
            "command": "python",
            "args": ["-m", "memory_server"],
            "env": {"MEMORY_PATH": ".mcp-memory.json"},
            "transport": "stdio",
        },
    ]


def _legacy_mcp_payload() -> dict[str, object]:
    return {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
                "transport": "stdio",
            },
            "memory": {
                "command": "python",
                "args": ["-m", "memory_server"],
                "env": {"MEMORY_PATH": ".mcp-memory.json"},
                "transport": "stdio",
            },
        }
    }


def test_prepare_mcp_runtime_codex_emits_config_toml(tmp_path: Path) -> None:
    runtime = _prepare_mcp_runtime("codex", tmp_path, _mcp_servers())

    assert runtime is not None
    codex_home = (tmp_path / ".atlas-dispatch" / "codex-home").resolve()
    assert runtime.config_path is None
    assert runtime.extra_env == {"CODEX_HOME": str(codex_home)}
    config_path = codex_home / "config.toml"
    assert runtime.worktree_files == [config_path.resolve()]

    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    servers = parsed["mcp_servers"]
    assert servers["filesystem"]["command"] == "npx"
    assert servers["filesystem"]["args"] == [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        ".",
    ]
    assert "env" not in servers["filesystem"]
    assert "transport" not in servers["filesystem"]
    assert servers["memory"]["command"] == "python"
    assert servers["memory"]["args"] == ["-m", "memory_server"]
    assert servers["memory"]["env"] == {"MEMORY_PATH": ".mcp-memory.json"}
    assert "transport" not in servers["memory"]


def test_prepare_mcp_runtime_gemini_writes_settings_and_excludes(
    tmp_path: Path,
) -> None:
    runtime = _prepare_mcp_runtime("gemini", tmp_path, _mcp_servers())

    assert runtime is not None
    settings_path = (tmp_path / ".gemini" / "settings.json").resolve()
    assert runtime.config_path is None
    assert runtime.extra_env is None
    assert runtime.worktree_files == [settings_path]
    assert json.loads(settings_path.read_text(encoding="utf-8")) == _legacy_mcp_payload()
    assert ".gemini/" in (tmp_path / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    ).splitlines()


def test_prepare_mcp_runtime_kimi_unchanged(tmp_path: Path) -> None:
    runtime = _prepare_mcp_runtime("kimi", tmp_path, _mcp_servers())

    assert runtime is not None
    config_path = (tmp_path / ".atlas-dispatch-mcp.json").resolve()
    assert runtime.config_path == config_path
    assert runtime.extra_env is None
    assert runtime.worktree_files == [config_path]
    assert config_path.read_text(encoding="utf-8") == json.dumps(
        _legacy_mcp_payload(), indent=2
    )

    command = inject_mcp_config("kimi", ["kimi", "--print"], runtime.config_path)
    assert command == ["kimi", "--mcp-config-file", str(config_path), "--print"]


def test_prepare_mcp_runtime_claude_unchanged(tmp_path: Path) -> None:
    runtime = _prepare_mcp_runtime("claude", tmp_path, _mcp_servers())

    assert runtime is not None
    config_path = (tmp_path / ".atlas-dispatch-mcp.json").resolve()
    assert runtime.config_path == config_path
    assert runtime.extra_env is None
    assert runtime.worktree_files == [config_path]
    assert config_path.read_text(encoding="utf-8") == json.dumps(
        _legacy_mcp_payload(), indent=2
    )

    command = inject_mcp_config("claude", ["claude", "--print"], runtime.config_path)
    assert command == ["claude", "--mcp-config", str(config_path), "--print"]


def test_prepare_mcp_runtime_empty_returns_none(tmp_path: Path) -> None:
    assert _prepare_mcp_runtime("codex", tmp_path, []) is None


def test_extend_git_info_exclude_idempotent(tmp_path: Path) -> None:
    _extend_git_info_exclude(tmp_path, [".gemini/"])
    _extend_git_info_exclude(tmp_path, [".gemini/"])

    lines = (tmp_path / ".git" / "info" / "exclude").read_text(
        encoding="utf-8"
    ).splitlines()
    assert [line for line in lines if line == ".gemini/"] == [".gemini/"]


def test_extend_git_info_exclude_follows_linked_worktree_commondir(
    tmp_path: Path,
) -> None:
    common_dir = tmp_path / "repo.git"
    git_dir = common_dir / "worktrees" / "task-worktree"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..", encoding="utf-8")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git_dir}", encoding="utf-8")

    _extend_git_info_exclude(worktree, [".gemini/"])

    lines = (common_dir / "info" / "exclude").read_text(
        encoding="utf-8"
    ).splitlines()
    assert ".gemini/" in lines
    assert not (git_dir / "info" / "exclude").exists()


def test_codex_toml_serializer_handles_special_chars(tmp_path: Path) -> None:
    runtime = _prepare_mcp_runtime(
        "codex",
        tmp_path,
        [
            {
                "name": "quoted-server",
                "command": "node",
                "args": [
                    "--label",
                    'value "with quotes"',
                    r"C:\Users\atlas dispatch\server.js",
                ],
                "env": {
                    "WINDOWS_PATH": r"C:\Users\atlas dispatch\data",
                    "QUOTE_VALUE": 'say "hello"',
                },
                "transport": "stdio",
            }
        ],
    )

    assert runtime is not None
    assert runtime.extra_env is not None
    config_path = Path(runtime.extra_env["CODEX_HOME"]) / "config.toml"
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    server = parsed["mcp_servers"]["quoted-server"]
    assert server["command"] == "node"
    assert server["args"] == [
        "--label",
        'value "with quotes"',
        r"C:\Users\atlas dispatch\server.js",
    ]
    assert server["env"] == {
        "WINDOWS_PATH": r"C:\Users\atlas dispatch\data",
        "QUOTE_VALUE": 'say "hello"',
    }
    assert "transport" not in server


def test_check_allowlist_ignores_tool_internal_paths() -> None:
    passed, forbidden, out_of_scope = check_allowlist(
        changed_files=[
            ".atlas-dispatch/codex-home/config.toml",
            ".atlas-dispatch-mcp.json",
            ".gemini/settings.json",
            "src/app.py",
        ],
        allowed_paths=["src/**"],
        forbidden_paths=[".atlas-dispatch/**", ".gemini/**", "*.json"],
    )

    assert passed
    assert forbidden == []
    assert out_of_scope == []


def test_check_allowlist_rejects_unknown_atlas_dispatch_path() -> None:
    passed, forbidden, out_of_scope = check_allowlist(
        changed_files=[".atlas-dispatch/hook.py"],
        allowed_paths=["src/**"],
        forbidden_paths=[],
    )

    assert not passed
    assert forbidden == []
    assert out_of_scope == [".atlas-dispatch/hook.py"]


def test_check_allowlist_single_star_does_not_cross_directories() -> None:
    passed, forbidden, out_of_scope = check_allowlist(
        changed_files=["libs/foo/nested/deep/x.py"],
        allowed_paths=["libs/foo/*.py"],
        forbidden_paths=[],
    )

    assert not passed
    assert forbidden == []
    assert out_of_scope == ["libs/foo/nested/deep/x.py"]


def test_check_allowlist_segment_aware_glob_guards() -> None:
    passed, forbidden, out_of_scope = check_allowlist(
        changed_files=["libs/foo/x.py", "libs/foo/nested/deep/x.py"],
        allowed_paths=["libs/foo/*.py", "libs/foo/**"],
        forbidden_paths=[],
    )

    assert passed
    assert forbidden == []
    assert out_of_scope == []


def test_check_allowlist_matches_literal_bracket_paths() -> None:
    # A Next.js dynamic-route dir named literally in allowed_paths
    # (e.g. src/app/api/tasks/[id]/route.ts) must MATCH. Plain fnmatch reads
    # [id] as a char class and false-flags the file out of scope. Brackets in
    # the pattern must be treated literally.
    passed, forbidden, out_of_scope = check_allowlist(
        changed_files=[
            "src/app/api/tasks/[id]/route.ts",
            "src/app/api/tasks/[id]/comments/route.ts",
        ],
        allowed_paths=[
            "src/app/api/tasks/[id]/route.ts",
            "src/app/api/tasks/[id]/**",
        ],
        forbidden_paths=[],
    )

    assert passed
    assert out_of_scope == []
    assert forbidden == []


def test_check_allowlist_forbidden_bracket_path_still_flags() -> None:
    # The literal-bracket handling must work on the forbidden side too: a
    # bracketed forbidden pattern must actually catch the matching path.
    passed, forbidden, _out_of_scope = check_allowlist(
        changed_files=["src/app/api/tasks/[id]/route.ts"],
        allowed_paths=["src/**"],
        forbidden_paths=["src/app/api/tasks/[id]/route.ts"],
    )

    assert not passed
    assert forbidden == ["src/app/api/tasks/[id]/route.ts"]


def test_example_dispatch_spec_keeps_scope_prediction_fields() -> None:
    spec_path = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "tasks"
        / "EX-001-implement.codex.json"
    )

    task = load_task(spec_path)

    assert task.allowed_paths == [
        "src/my_service/http_client.py",
        "tests/test_http_client.py",
    ]
    assert task.forbidden_paths == [
        "src/my_service/settings.py",
        "pyproject.toml",
    ]
    assert task.cli == "codex"
    assert task.resolved_via == "model_registry:codex/gpt-6-sol-high"


def test_dispatch_prediction_miss_runs_real_acceptance_and_reports_surface(
    tmp_path: Path,
) -> None:
    """Regression: post-run surface measurement must not destroy test evidence."""

    repo = _init_dispatch_repo(tmp_path)
    acceptance_marker = tmp_path / "acceptance-actually-ran"
    runs_dir = tmp_path / "runs"
    spec_path = _write_task_spec(
        tmp_path,
        {
            "id": "D-SURFACE-INFORMATIONAL",
            "target_repo": str(repo),
            "worktree_branch": "codex/surface-informational",
            "runs_dir": str(runs_dir),
            "allowed_paths": ["src/**"],
            "forbidden_paths": ["services/**"],
            "acceptance": [
                "test -f services/search-service/feed.py && "
                f"touch {shlex.quote(str(acceptance_marker))}"
            ],
        },
    )

    def write_actual_surface(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        for relative_path in (
            "src/predicted.py",
            "libs/storage/repository.py",
            "libs/storage/nested/queries.py",
            "services/search-service/feed.py",
        ):
            path = cwd / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("changed = True\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch(
        "atlas_dispatch.dispatcher.run_cli",
        side_effect=write_actual_surface,
    ):
        rc = dispatch(spec_path)

    assert acceptance_marker.is_file(), "the real acceptance shell command did not run"
    assert rc == 0
    run_dir = next(runs_dir.iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert summary["acceptance_skip_reason"] is None
    assert summary["acceptance_outcome"] == "passed"
    assert summary["acceptance_commands"]
    assert summary["acceptance_commands"][0]["passed"] is True
    assert summary["verify_passed"] is True
    assert "- Allowlist passed: **False**" in report
    assert "services/search-service/feed.py" in report
    assert "SURFACE: 4 files changed, spec predicted 1" in report
    assert "  + libs/storage/** (2 files)" in report
    assert "  + services/search-service/** (1 file)" in report
    assert "SURFACE: 4 files changed, spec predicted 3" not in report
    assert "  + libs/storage/** (3 files)" not in report
    assert "  + services/search-service/** (2 files)" not in report
    assert "informational; does NOT gate" in report


def test_dispatch_protected_path_violation_fails_real_verification_path(
    tmp_path: Path,
) -> None:
    repo = _init_dispatch_repo(tmp_path)
    runs_dir = tmp_path / "runs"
    protected_path = "migrations/0042_drop_users.sql"
    spec_path = _write_task_spec(
        tmp_path,
        {
            "id": "D-PROTECTED-PATH",
            "target_repo": str(repo),
            "worktree_branch": "codex/protected-path",
            "runs_dir": str(runs_dir),
            "allowed_paths": ["migrations/**"],
            "acceptance": [f"test -f {protected_path}"],
        },
    )

    def touch_protected_path(**kwargs: object) -> AdapterResult:
        cwd = kwargs["cwd"]
        assert isinstance(cwd, Path)
        path = cwd / protected_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("DROP TABLE users;\n", encoding="utf-8")
        return _successful_adapter_result()

    with patch(
        "atlas_dispatch.dispatcher.run_cli",
        side_effect=touch_protected_path,
    ):
        rc = dispatch(spec_path)

    assert rc == 1
    run_dir = next(runs_dir.iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert summary["acceptance_outcome"] == "passed"
    assert summary["acceptance_commands"]
    assert summary["protected_path_violations"] == [protected_path]
    assert summary["verify_passed"] is False
    assert f"- Protected-path violations: ['{protected_path}']" in report
    assert "- Verify passed: **False**" in report


def test_dispatch_passes_codex_home_extra_env(tmp_path: Path) -> None:
    spec_path = _write_task_spec(
        tmp_path,
        {
            "mcp_servers": _mcp_servers(),
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["python -c pass"],
        },
    )
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    captured_run: dict[str, object] = {}

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        captured_run.update(kwargs)
        return AdapterResult(
            cli="codex",
            exit_code=0,
            stdout="done",
            stderr="",
            duration_seconds=0.0,
            command=["codex"],
            classification=Classification(
                kind=DispatchErrorKind.SUCCESS,
                suggested_action="",
            ),
        )

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="codex/test",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        rc = dispatch(spec_path)

    assert rc == 0
    codex_home = (worktree_path / ".atlas-dispatch" / "codex-home").resolve()
    run_dir = next((tmp_path / "runs").iterdir())
    assert isinstance(captured_run["command"], list)
    assert Path(captured_run["command"][0]).name == "codex"
    assert captured_run["extra_env"] == {
        "CODEX_HOME": str(codex_home),
        "UV_CACHE_DIR": str((run_dir / "uv-cache").resolve()),
    }
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["review_model_evidence"] == {
        "schema": "atlas-dispatch.review-model-evidence",
        "schema_version": 1,
        "effective_cli": "codex",
        "effective_model": "unknown",
        "model_resolution_source": "unread-runtime-model",
    }


def test_dispatch_antigravity_alias_runs_and_persists_fixed_model_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ATLAS_DISPATCH_GEMINI_CMD", "")
    spec_path = _write_task_spec(
        tmp_path,
        {
            "model": "gemini/3.6-flash-agy",
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["python -c pass"],
        },
    )
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    captured_run: dict[str, object] = {}

    def fake_run_cli(**kwargs: object) -> AdapterResult:
        captured_run.update(kwargs)
        return _successful_adapter_result("gemini")

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="gemini/test",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", side_effect=fake_run_cli),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        rc = dispatch(spec_path)

    assert rc == 0
    command = captured_run["command"]
    assert isinstance(command, list)
    assert Path(command[0]).name == "agy"
    assert "--model" not in command
    assert command[command.index("-p") + 1] == "Do the task."
    run_dir = next((tmp_path / "runs").iterdir())
    summary = json.loads((run_dir / "cli.summary.json").read_text(encoding="utf-8"))
    assert summary["review_model_evidence"] == {
        "schema": "atlas-dispatch.review-model-evidence",
        "schema_version": 1,
        "effective_cli": "agy",
        "effective_model": "gemini/3.6-flash-agy",
        "model_resolution_source": "fixed-runtime-declaration",
    }


def test_dispatch_passes_acceptance_timeout_to_verify(tmp_path: Path) -> None:
    spec_path = _write_task_spec(
        tmp_path,
        {
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["python -c pass"],
            "acceptance_timeout_seconds": 60,
        },
    )
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    captured_acceptance: dict[str, object] = {}

    def fake_run_acceptance_commands(**kwargs: object) -> list[CheckResult]:
        captured_acceptance.update(kwargs)
        return [CheckResult(name="python -c pass", passed=True, details="ok")]

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="codex/test",
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher.run_cli",
            return_value=_successful_adapter_result(),
        ),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=fake_run_acceptance_commands,
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        rc = dispatch(spec_path)

    assert rc == 0
    assert captured_acceptance == {
        "commands": ["python -c pass"],
        "cwd": worktree_path,
        "timeout_seconds": 60,
        "import_provenance_required": False,
        "run_dir": next((tmp_path / "runs").iterdir()),
    }


def test_dispatch_passes_destructive_branch_reset_opt_in_to_worktree(
    tmp_path: Path,
) -> None:
    spec_path = _write_task_spec(
        tmp_path,
        {
            "runs_dir": str(tmp_path / "runs"),
            "acceptance": ["python -c pass"],
            "allow_destructive_branch_reset": True,
            "protected_head_sha": "4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        },
    )
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    captured_worktree: dict[str, object] = {}

    def fake_create_worktree(**kwargs: object) -> Worktree:
        captured_worktree.update(kwargs)
        return Worktree(
            repo_root=tmp_path / "repo",
            worktree_path=worktree_path,
            branch="codex/test",
        )

    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            side_effect=fake_create_worktree,
        ),
        patch(
            "atlas_dispatch.dispatcher.run_cli",
            return_value=_successful_adapter_result(),
        ),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=["src/app.py"],
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        rc = dispatch(spec_path)

    assert rc == 0
    assert captured_worktree["allow_destructive_branch_reset"] is True
    assert (
        captured_worktree["protected_head_sha"]
        == "4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a"
    )
    report_path = next((tmp_path / "runs").glob("*/report.md"))
    assert (
        "- Destructive branch reset opt-in: **ENABLED**. "
        "Unreachable branch commits may be reset only after rescue-ref creation."
    ) in report_path.read_text(encoding="utf-8")


def test_capabilities_prints_codex_block_to_stdout() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["capabilities", "codex"])
    assert rc == 0
    stdout = out.getvalue()
    assert "- cli: codex" in stdout
    assert "argv_template: codex" in stdout
    assert "models:" in stdout


def test_capabilities_unknown_cli_returns_1_and_writes_stderr() -> None:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = main(["capabilities", "definitely-not-a-cli"])
    assert rc == 1
    assert "unknown cli: definitely-not-a-cli" in err.getvalue()
    assert out.getvalue() == ""


def test_capabilities_lowercases_input() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["capabilities", "CODEX"])
    assert rc == 0
    assert "- cli: codex" in out.getvalue()


def test_capabilities_prints_kimi_code_block_to_stdout() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(["capabilities", "kimi-code"])
    assert rc == 0
    stdout = out.getvalue()
    assert "- cli: kimi-code" in stdout
    assert "argv_template: kimi -p {prompt} -m {model} --output-format text" in stdout
    assert "reads_prompt_from_stdin: False" in stdout
    assert (
        "models: kimi/k2.7-coding, kimi/k2.7-coding-highspeed, "
        "kimi/k3-256k, kimi/k3-max"
    ) in stdout














class _ParserCapturedError(Exception):
    pass


def _capture_parser_before_parse(callable_: Callable[[], object]) -> argparse.ArgumentParser:
    captured: list[argparse.ArgumentParser] = []

    def capture_parse_args(
        self: argparse.ArgumentParser,
        args: object = None,
        namespace: object = None,
    ) -> argparse.Namespace:
        captured.append(self)
        raise _ParserCapturedError

    with patch.object(argparse.ArgumentParser, "parse_args", capture_parse_args):
        with pytest.raises(_ParserCapturedError):
            callable_()
    return captured[0]










# --------------------------------------------------------------------------- #
# Memory regression tests                                                     #
# --------------------------------------------------------------------------- #


def test_run_cli_streams_output_via_temp_files(tmp_path: Path) -> None:
    """Regression: stdout/stderr stream through disk, not capture_output=True.

    Using capture_output=True forces subprocess.run to buffer the entire CLI
    output in RAM.  For verbose models this can be 30-50 MB per dispatch, and
    a long-lived caller running many dispatches accumulates it.  This test
    verifies that large output is redirected to temp files and read back
    correctly.
    """
    env_var = "ATLAS_DISPATCH_CODEX_CMD"
    previous = os.environ.get(env_var)
    large_payload = "A" * 20_000
    os.environ[env_var] = (
        f"{shlex.quote(sys.executable)} -c "
        + shlex.quote(f"import sys; sys.stdout.write('{large_payload}')")
    )
    try:
        result = run_cli(
            cli="codex",
            prompt="",
            cwd=tmp_path,
            timeout_seconds=10,
        )
    finally:
        if previous is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = previous

    assert result.exit_code == 0, result.stderr
    assert result.stdout == large_payload
    # Temp files must be cleaned up; nothing *.tmp should remain.
    tmp_dir = tmp_path / ".atlas-dispatch"
    if tmp_dir.exists():
        assert not list(tmp_dir.glob("*.tmp"))


def test_classify_result_bounds_haystack_for_large_output() -> None:
    """Regression: classify_result must not create unbounded temporary strings.

    Concatenating stderr + '\n' + stdout and then calling .lower() on a
    30 MB string briefly doubles memory (~70 MB transient spike).  The fix
    tail-biases the haystack to MAX_CLASSIFY_HAYSTACK (256 KB).  This test
    puts the rate-limit signal at the very end of a 500 KB payload and
    confirms it is still found.
    """
    tail_marker = "rate limit exceeded"
    large_stdout = "A" * 500_000 + tail_marker
    result = AdapterResult(
        cli="codex",
        exit_code=1,
        stdout=large_stdout,
        stderr="",
        duration_seconds=1.0,
        command=["codex"],
    )
    classification = classify_result(result)
    assert classification.kind == DispatchErrorKind.RATE_LIMITED
    assert classification.matched_pattern is not None


def _run_skip_reason_dispatch(
    tmp_path: Path,
    *,
    cli_result: AdapterResult,
    changed_files: list[str],
    allowed_paths: list[str],
    acceptance: list[str] | None = None,
    context_files: list[Path] | None = None,
    prompt_template_text: str | None = None,
) -> tuple[Path, dict[str, Any], str]:
    """Run a patched dispatch and return (run_dir, cli.summary.json, report.md)."""
    overrides: dict[str, object] = {
        "target_repo": str(tmp_path / "repo"),
        "worktree_branch": "codex/skip-reason",
        "runs_dir": str(tmp_path / "runs"),
        "allowed_paths": allowed_paths,
        # `is None` rather than falsy: an explicitly EMPTY acceptance list is a
        # legitimate spec and a test must be able to express it.
        "acceptance": ["python -c pass"] if acceptance is None else acceptance,
    }
    if context_files is not None:
        overrides["context_files"] = [str(path) for path in context_files]
    if prompt_template_text is not None:
        context_prompt = tmp_path / "context-prompt.md"
        context_prompt.write_text(prompt_template_text, encoding="utf-8")
        overrides["prompt_template"] = str(context_prompt)
    spec_path = _write_task_spec(
        tmp_path,
        overrides,
    )
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    with (
        patch("atlas_dispatch.dispatcher.is_git_repo", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.create_worktree",
            return_value=Worktree(
                repo_root=tmp_path / "repo",
                worktree_path=worktree_path,
                branch="codex/skip-reason",
            ),
        ),
        patch("atlas_dispatch.dispatcher.run_cli", return_value=cli_result),
        patch("atlas_dispatch.dispatcher.commit_all", return_value=True),
        patch(
            "atlas_dispatch.dispatcher.list_changed_files",
            return_value=changed_files,
        ),
        patch(
            # side_effect, not return_value: the real runner returns [] when it
            # is given no commands, and a stub that always yields one passing
            # result cannot express a spec with an empty acceptance list at all.
            "atlas_dispatch.dispatcher.run_acceptance_commands",
            side_effect=lambda **kwargs: (
                [CheckResult(name="python -c pass", passed=True, details="ok")]
                if kwargs.get("commands")
                else []
            ),
        ),
        patch(
            "atlas_dispatch.dispatcher._resolve_verified_head_sha",
            return_value="4e49b1d38b163c96f2ebd54248ffb720a2d9ae7a",
        ),
    ):
        dispatch(spec_path)

    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    summary = json.loads(
        (run_dir / "cli.summary.json").read_text(encoding="utf-8")
    )
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    return run_dir, summary, report


def test_dispatch_bounds_fifo_context_read_before_lock_and_reports_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fifo = tmp_path / "blocked-context.md"
    os.mkfifo(fifo)
    timeout_seconds = 0.05
    monkeypatch.setattr(
        dispatcher_mod,
        "CONTEXT_FILE_READ_TIMEOUT_SECONDS",
        timeout_seconds,
    )
    reader_name = f"atlas-context-read:{fifo.name}"
    reader_was_waiting_before_lock: list[bool] = []

    @contextmanager
    def observed_dispatch_lock(_runs_root: Path) -> Any:
        reader_was_waiting_before_lock.append(
            any(thread.name == reader_name for thread in threading.enumerate())
        )
        yield

    monkeypatch.setattr(
        dispatcher_mod,
        "_dispatch_runs_lock",
        observed_dispatch_lock,
    )

    started = time.monotonic()
    try:
        run_dir, _summary, report = _run_skip_reason_dispatch(
            tmp_path,
            cli_result=_successful_adapter_result(),
            changed_files=["src/app.py"],
            allowed_paths=["**"],
            context_files=[fifo],
            prompt_template_text="Review with this context:\n\n{{context_block}}",
        )
    finally:
        # Let the intentionally leaked daemon reader finish so the test itself
        # does not leave a blocked thread behind for the rest of the suite.
        try:
            writer_fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            pass
        else:
            os.close(writer_fd)
    elapsed = time.monotonic() - started

    prompt = (run_dir / "prompt.md").read_text(encoding="utf-8")
    marker = (
        "context file skipped: read timed out after "
        f"{timeout_seconds:g} seconds at {fifo}"
    )
    assert elapsed < 1.0
    assert reader_was_waiting_before_lock == [True]
    assert marker in prompt
    assert f"- **SKIPPED:** ({marker})" in report


def test_allowlist_failure_does_not_become_an_acceptance_skip_reason(
    tmp_path: Path,
) -> None:
    """A successful changed run executes acceptance despite its surface miss."""
    _run_dir, summary, report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=_successful_adapter_result(),
        changed_files=["not-allowed.txt"],
        allowed_paths=["allowed.txt"],
    )

    assert summary["acceptance_skip_reason"] is None
    assert summary["acceptance_outcome"] == "passed"
    assert "(skipped — allowlist check failed:" not in report
    assert "- Acceptance outcome: **PASSED**" in report
    assert "- Allowlist passed: **False**" in report


def test_allowlist_failure_is_recorded_only_when_another_cause_skips_acceptance(
    tmp_path: Path,
) -> None:
    _run_dir, summary, report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            command=["codex"],
            classification=Classification(
                kind=DispatchErrorKind.OVERLOADED,
                suggested_action="retry",
            ),
        ),
        changed_files=["not-allowed.txt"],
        allowed_paths=["allowed.txt"],
    )

    assert summary["acceptance_skip_reason"] == [
        "cli_failed",
        "allowlist_failed",
    ]
    assert summary["acceptance_outcome"] == "skipped"
    assert "CLI did not succeed cleanly: classification=overloaded" in report
    assert "allowlist check failed: 1 changed file(s) outside allowed_paths" in report


def test_acceptance_skip_reason_cli_failed(tmp_path: Path) -> None:
    """A failed CLI classification must be recorded as the skip cause."""
    _run_dir, summary, report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            command=["codex"],
            classification=Classification(
                kind=DispatchErrorKind.OVERLOADED,
                suggested_action="retry",
            ),
        ),
        changed_files=["src/app.py"],
        allowed_paths=["**"],
    )

    assert summary["acceptance_skip_reason"] == ["cli_failed"]
    assert summary["acceptance_outcome"] == "skipped"
    assert (
        "(skipped — CLI did not succeed cleanly: classification=overloaded)"
        in report
    )


def test_acceptance_skip_reason_no_files_changed(tmp_path: Path) -> None:
    """An empty diff must be recorded as the skip cause."""
    _run_dir, summary, report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=_successful_adapter_result(),
        changed_files=[],
        allowed_paths=["**"],
    )

    assert summary["acceptance_skip_reason"] == ["no_files_changed"]
    assert summary["acceptance_outcome"] == "skipped"
    assert "(skipped — no files changed)" in report


def test_acceptance_skip_reason_reports_multiple_causes(tmp_path: Path) -> None:
    """If several skip conditions hold, the report must list every cause."""
    _run_dir, summary, report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=AdapterResult(
            cli="codex",
            exit_code=1,
            stdout="",
            stderr="",
            duration_seconds=0.0,
            command=["codex"],
            classification=Classification(
                kind=DispatchErrorKind.OVERLOADED,
                suggested_action="retry",
            ),
        ),
        changed_files=[],
        allowed_paths=["**"],
    )

    assert summary["acceptance_skip_reason"] == [
        "cli_failed",
        "no_files_changed",
    ]
    assert summary["acceptance_outcome"] == "skipped"
    assert "CLI did not succeed cleanly: classification=overloaded" in report
    assert "no files changed" in report


def test_acceptance_skip_reason_is_none_when_acceptance_runs(
    tmp_path: Path,
) -> None:
    """When acceptance commands actually run, the skip reason must be None."""
    _run_dir, summary, report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=_successful_adapter_result(),
        changed_files=["src/app.py"],
        allowed_paths=["**"],
    )

    assert summary["acceptance_skip_reason"] is None
    assert summary["acceptance_outcome"] == "passed"
    assert "(skipped —" not in report
    assert "- Acceptance outcome: **PASSED**" in report
    assert "### `python -c pass` — PASS [success]" in report
    assert summary["acceptance_commands"][0]["passed"] is True


def test_red_run_is_still_published_preservation_is_not_approval(tmp_path: Path) -> None:
    """When publication is requested, a failed run is published too.

    Gating publication on verification inverts the risk: the work most likely to
    be lost (red, partial, needs another look) is exactly the work that would be
    refused preservation, and a reviewer task that starts from
    base_ref=origin/<branch> could not start at all.

    Pushing is NOT merging. This asserts a FAILED run still reaches the remote.
    """
    from atlas_dispatch.dispatcher import TaskSpec, _publish_branch_if_requested

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    _git(source_repo, "init")
    _git(source_repo, "config", "user.email", "test@example.invalid")
    _git(source_repo, "config", "user.name", "Test User")
    (source_repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(source_repo, "add", "README.md")
    _git(source_repo, "commit", "-m", "base")
    _git(source_repo, "branch", "-M", "main")
    _git(source_repo, "remote", "add", "origin", str(remote))
    _git(source_repo, "push", "-u", "origin", "main")

    build = create_worktree(
        repo_root=source_repo,
        branch="codex/red-task",
        base_ref="main",
        parent_dir=tmp_path / "wt",
    )
    (build.worktree_path / "half-done.txt").write_text("incomplete\n", encoding="utf-8")
    assert commit_all(build, message="codex(T-red): partial work")

    task = TaskSpec(
        id="T-red",
        title="a run that failed verification",
        target_repo=source_repo,
        worktree_branch="codex/red-task",
        cli="codex",
        prompt_template=tmp_path / "p.md",
        allowed_paths=["**"],
        push_branch=True,
    )

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = _publish_branch_if_requested(
        run_dir=run_dir,
        task=task,
        worktree=build,
        verification_passed=False,   # RED
        cli_succeeded=False,         # RED
    )

    assert result is not None
    assert result.pushed, "a failed run must STILL be preserved on the remote"
    out = subprocess.run(
        ["git", "--git-dir", str(remote), "branch", "--list", "codex/red-task"],
        check=True, capture_output=True, text=True,
    )
    assert "codex/red-task" in out.stdout, "branch is not actually on the remote"


def test_non_string_extra_prompt_vars_render_instead_of_raising(tmp_path: Path) -> None:
    """extra_prompt_vars is task-spec JSON, so its values are not always strings.

    Regression: an orchestrator that passes a retry counter as an int in
    extra_prompt_vars made render_prompt raise `TypeError: replace() argument 2
    must be str, not int`. That surfaced as `dispatch_exception` with
    `changed_files: []`, and the builder CLI was never invoked.

    Deleting this test removes the only evidence that a numeric prompt variable
    reaches the builder instead of aborting the dispatch.
    """

    task = _make_prompt_task(
        tmp_path,
        template_text="round={{_fixup_return_round}} ok={{flag}} note={{note}}",
    )
    task.extra_prompt_vars.update(
        {"_fixup_return_round": 1, "flag": True, "note": None}
    )

    assert render_prompt(task) == "round=1 ok=true note="


def test_container_prompt_vars_render_as_json_not_python_repr(tmp_path: Path) -> None:
    """A dict/list prompt variable must reach the builder as JSON.

    MUST STILL FIRE: a Python repr (single quotes, True/None) is not valid JSON and
    would be actively misleading inside a prompt that describes a spec.
    """

    task = _make_prompt_task(tmp_path, template_text="{{payload}}")
    task.extra_prompt_vars.update({"payload": {"b": [1, 2], "a": True}})

    rendered = render_prompt(task)
    assert json.loads(rendered) == {"a": True, "b": [1, 2]}
    assert "'" not in rendered


def test_string_prompt_vars_are_still_substituted_verbatim(tmp_path: Path) -> None:
    """MUST STILL FIRE: coercion must not reformat ordinary string variables."""

    task = _make_prompt_task(tmp_path, template_text="[{{task_summary}}]")
    task.extra_prompt_vars.update({"task_summary": "  spaced\nlines  "})

    assert render_prompt(task) == "[  spaced\nlines  ]"




def test_acceptance_skip_reason_no_acceptance_configured(tmp_path: Path) -> None:
    """A spec that declares no acceptance was NOT GATED, which is not "unknown".

    An orchestrator may emit an empty acceptance list deliberately, letting the
    agent decide how to prove its own work. Falling through every skip test into
    "unknown" would report a perfectly good run as a malfunction, which is how a
    reader learns to discount the field.

    verify_passed stays False on purpose. Not gated is not passed, and turning
    an absent check into a green one would make every real failure unreadable.
    """
    _run_dir, summary, _report = _run_skip_reason_dispatch(
        tmp_path,
        cli_result=_successful_adapter_result(),
        changed_files=["src/app.py"],
        allowed_paths=["**"],
        acceptance=[],
    )

    assert summary["acceptance_skip_reason"] == ["no_acceptance_configured"]
    assert summary["acceptance_outcome"] == "skipped"
    assert summary["verify_passed"] is not True


def test_check_import_provenance_defaults_on_and_can_be_disabled(tmp_path: Path) -> None:
    assert load_task(_write_task_spec(tmp_path)).check_import_provenance is True

    disabled = load_task(
        _write_task_spec(tmp_path, {"check_import_provenance": False})
    )

    assert disabled.check_import_provenance is False
    assert dispatcher_mod._acceptance_requires_import_provenance(
        ["python -m pytest -q"], enabled=False
    ) is False
    assert dispatcher_mod._acceptance_requires_import_provenance(
        ["python -m pytest -q"]
    ) is True


def test_check_import_provenance_must_be_a_json_boolean(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="check_import_provenance must be a JSON boolean"):
        load_task(_write_task_spec(tmp_path, {"check_import_provenance": "no"}))


def test_single_segment_target_repo_resolves_against_configured_code_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    repo = second / "my-service"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    first.mkdir()
    monkeypatch.setenv(
        dispatcher_mod.CODE_ROOTS_ENV, os.pathsep.join([str(first), str(second)])
    )
    spec_dir = tmp_path / "specs"
    spec_dir.mkdir()
    (spec_dir / "prompt.md").write_text("Do it.", encoding="utf-8")
    spec = spec_dir / "task.json"
    spec.write_text(
        json.dumps(
            {
                "id": "ROOTS",
                "title": "roots",
                "target_repo": "my-service",
                "cli": "codex",
                "prompt_template": "prompt.md",
                "worktree_branch": "codex/roots",
                "allowed_paths": ["**"],
            }
        ),
        encoding="utf-8",
    )

    assert load_task(spec).target_repo == repo.resolve()


def test_code_roots_default_to_home_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(dispatcher_mod.CODE_ROOTS_ENV, raising=False)

    assert dispatcher_mod._code_roots() == [Path.home() / "code"]
