from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import venv
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import atlas_dispatch.worktree as worktree_mod
from atlas_dispatch.worktree import (
    create_worktree,
    list_changed_files,
    worktree_path_for,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_worktree_path_disambiguates_colliding_branch_slugs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    branch_a = "codex/a-b"
    branch_b = "codex-a/b"

    path_a = worktree_path_for(repo, branch_a)
    path_b = worktree_path_for(repo, branch_b)

    assert path_a != path_b
    assert path_a.name.startswith("repo-wt-codex-a-b-")
    assert path_b.name.startswith("repo-wt-codex-a-b-")
    assert path_a == worktree_path_for(repo, branch_a)
    digest_a = hashlib.sha256(branch_a.encode("utf-8")).hexdigest()[:8]
    digest_b = hashlib.sha256(branch_b.encode("utf-8")).hexdigest()[:8]
    assert digest_a != digest_b
    assert path_a.name == f"repo-wt-codex-a-b-{digest_a}"
    assert path_b.name == f"repo-wt-codex-a-b-{digest_b}"


def _git_output(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", str(repo))
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "-M", "main")
    return repo


def test_create_worktree_disambiguates_colliding_branch_slugs_by_effect(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    branch_a = "codex/a-b"
    branch_b = "codex-a/b"
    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()

    worktree_a = create_worktree(
        repo_root=repo, branch=branch_a, parent_dir=worktree_parent
    )
    worktree_b = create_worktree(
        repo_root=repo, branch=branch_b, parent_dir=worktree_parent
    )

    paths = {worktree_a.worktree_path, worktree_b.worktree_path}
    assert len(paths) == 2
    assert all(path.is_dir() for path in paths)
    assert worktree_a.worktree_path == worktree_path_for(
        repo, branch_a, parent=worktree_parent
    ).resolve()
    assert worktree_b.worktree_path == worktree_path_for(
        repo, branch_b, parent=worktree_parent
    ).resolve()

    registered_paths = {
        Path(line.removeprefix("worktree ")).resolve()
        for line in _git_output(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    }
    assert paths <= registered_paths

    prefix = f"{repo.name}-wt-codex-a-b-"
    suffixes = {path.name.removeprefix(prefix) for path in paths}
    assert all(path.name.startswith(prefix) for path in paths)
    assert len(suffixes) == 2
    assert all(len(suffix) == 8 for suffix in suffixes)
    assert all(set(suffix) <= set("0123456789abcdef") for suffix in suffixes)


def _init_bare_remote(tmp_path: Path, name: str) -> Path:
    remote = tmp_path / name
    _git(tmp_path, "init", "--bare", str(remote))
    return remote


def _commit_file(cwd: Path, path: str, content: str, message: str) -> str:
    file_path = cwd / path
    file_path.write_text(content, encoding="utf-8")
    _git(cwd, "add", path)
    _git(cwd, "commit", "-m", message)
    return _git_output(cwd, "rev-parse", "HEAD")


def _write_run_task(repo: Path, *, task_id: str, timestamp: str, branch: str) -> Path:
    run_dir = repo / ".atlas-dispatch" / "runs" / task_id / timestamp
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(
        json.dumps({"worktree_branch": branch}), encoding="utf-8"
    )
    return run_dir


def _require_uv() -> None:
    if shutil.which("uv") is None:
        pytest.skip("uv is required for isolated worktree venv tests")


def _run_uv(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    _require_uv()
    return subprocess.run(
        ["uv", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_python(
    python: Path, code: str, *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(python), "-c", code],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _create_parent_venv(repo: Path) -> Path:
    venv.EnvBuilder(with_pip=False, symlinks=True).create(repo / ".venv")
    return repo / ".venv" / "bin" / "python"


def _site_packages(venv_python: Path) -> Path:
    result = _run_python(
        venv_python,
        "import sysconfig; print(sysconfig.get_path('purelib'))",
        cwd=venv_python.parent,
    )
    return Path(result.stdout.strip())


def _snapshot_site_packages(site_packages: Path) -> dict[str, tuple[int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, str]] = {}
    paths = [site_packages, *site_packages.rglob("*")]
    for path in sorted(
        paths,
        key=lambda item: (
            item.relative_to(site_packages).as_posix()
            if item != site_packages
            else "."
        ),
    ):
        stat = path.lstat()
        relative = (
            "."
            if path == site_packages
            else path.relative_to(site_packages).as_posix()
        )
        if path.is_symlink():
            digest = f"symlink:{path.readlink()}"
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        elif path.is_dir():
            digest = "dir"
        else:
            digest = "other"
        snapshot[relative] = (stat.st_mode, stat.st_mtime_ns, stat.st_size, digest)
    return snapshot


def _write_pyproject_package(
    package_root: Path,
    *,
    project_name: str,
    package_name: str,
    value: str,
) -> Path:
    package_dir = package_root / "src" / package_name
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text(f"VALUE = {value!r}\n", encoding="utf-8")
    (package_root / "pyproject.toml").write_text(
        f"""
[build-system]
requires = ["hatchling>=1.21"]
build-backend = "hatchling.build"

[project]
name = "{project_name}"
version = "0.0.0"

[tool.hatch.build.targets.wheel]
packages = ["src/{package_name}"]
""".lstrip(),
        encoding="utf-8",
    )
    return package_root


def _write_src_layout_package(repo: Path, *, value: str) -> Path:
    package_root = repo / "libs" / "demo"
    return _write_pyproject_package(
        package_root,
        project_name="demo-pkg",
        package_name="demo_pkg",
        value=value,
    )


def _write_flat_layout_project(repo: Path) -> None:
    package_dir = repo / "flat_pkg"
    tests_dir = repo / "tests"
    package_dir.mkdir()
    tests_dir.mkdir()
    (package_dir / "__init__.py").write_text("VALUE = 'flat-worktree'\n", encoding="utf-8")
    (tests_dir / "test_flat_pkg.py").write_text(
        "import flat_pkg\n\n"
        "def test_flat_pkg_value():\n"
        "    assert flat_pkg.VALUE == 'flat-worktree'\n",
        encoding="utf-8",
    )
    (repo / "pyproject.toml").write_text(
        """
[build-system]
requires = ["hatchling>=1.21"]
build-backend = "hatchling.build"

[project]
name = "flat-pkg"
version = "0.0.0"

[tool.hatch.build.targets.wheel]
packages = ["flat_pkg"]
""".lstrip(),
        encoding="utf-8",
    )


def test_auto_clean_existing_path_before_create(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _init_repo(tmp_path)
    caplog.set_level(logging.INFO, logger=worktree_mod.LOGGER.name)

    stale = create_worktree(repo_root=repo, branch="codex/stale", parent_dir=tmp_path)
    recreated = create_worktree(repo_root=repo, branch="codex/stale", parent_dir=tmp_path)

    assert recreated.worktree_path == stale.worktree_path
    assert recreated.worktree_path.is_dir()
    assert _git_output(recreated.worktree_path, "branch", "--show-current") == "codex/stale"
    assert any(
        record.event == "worktree_autoclean"
        and record.path == str(stale.worktree_path)
        and record.reason == "stale_dir_or_branch_lock"
        for record in caplog.records
    )


def test_auto_clean_branch_lock_at_different_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _init_repo(tmp_path)
    caplog.set_level(logging.INFO, logger=worktree_mod.LOGGER.name)

    locked = create_worktree(
        repo_root=repo, branch="codex/locked", parent_dir=tmp_path / "old"
    )
    replacement = create_worktree(
        repo_root=repo, branch="codex/locked", parent_dir=tmp_path / "new"
    )

    assert not locked.worktree_path.exists()
    assert replacement.worktree_path.is_dir()
    assert replacement.worktree_path != locked.worktree_path
    assert _git_output(replacement.worktree_path, "branch", "--show-current") == "codex/locked"
    assert any(
        record.event == "worktree_autoclean"
        and record.path == str(locked.worktree_path)
        and record.reason == "stale_dir_or_branch_lock"
        for record in caplog.records
    )


def test_auto_clean_branch_lock_at_deleted_target_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    stale = create_worktree(repo_root=repo, branch="codex/deleted", parent_dir=tmp_path)
    shutil.rmtree(stale.worktree_path)

    recreated = create_worktree(repo_root=repo, branch="codex/deleted", parent_dir=tmp_path)

    assert recreated.worktree_path == stale.worktree_path
    assert recreated.worktree_path.is_dir()
    assert _git_output(recreated.worktree_path, "branch", "--show-current") == "codex/deleted"


def test_reused_branch_resets_to_base_ref(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    commit_b = _commit_file(repo, "b.txt", "b\n", "main B")
    first = create_worktree(repo_root=repo, branch="feature/x", parent_dir=tmp_path)
    assert _git_output(first.worktree_path, "rev-parse", "HEAD") == commit_b
    _git(repo, "worktree", "remove", str(first.worktree_path))

    commit_c = _commit_file(repo, "c.txt", "c\n", "main C")
    second = create_worktree(repo_root=repo, branch="feature/x", parent_dir=tmp_path)

    assert _git_output(second.worktree_path, "rev-parse", "HEAD") == commit_c
    assert _git_output(second.worktree_path, "rev-parse", "HEAD") != commit_b


def test_reused_branch_with_local_commits_is_rescued_and_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _init_repo(tmp_path)
    caplog.set_level(logging.WARNING, logger=worktree_mod.LOGGER.name)
    _commit_file(repo, "b.txt", "b\n", "main B")
    first = create_worktree(repo_root=repo, branch="feature/x", parent_dir=tmp_path)
    commit_x = _commit_file(first.worktree_path, "x.txt", "x\n", "feature X")
    _git(repo, "worktree", "remove", str(first.worktree_path))

    _commit_file(repo, "c.txt", "c\n", "main C")
    run_dir = _write_run_task(
        repo,
        task_id="T-124",
        timestamp="20260709T000100Z",
        branch="feature/x",
    )

    with pytest.raises(ValueError) as excinfo:
        create_worktree(repo_root=repo, branch="feature/x", parent_dir=tmp_path)

    assert _git_output(repo, "rev-parse", "refs/heads/feature/x") == commit_x
    rescue_refs = _git_output(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/rescue/feature-x",
    ).splitlines()
    assert len(rescue_refs) == 1
    assert _git_output(repo, "rev-parse", rescue_refs[0]) == commit_x
    message = str(excinfo.value)
    assert f"Rescue ref: {rescue_refs[0]}" in message
    assert f"Full sha: {commit_x}" in message
    assert "Host: " in message
    assert f"Inspect: git log {rescue_refs[0]}" in message
    assert (
        "Proceed: set allow_destructive_branch_reset=True by adding "
        '"allow_destructive_branch_reset": true to the task spec, then re-run.'
    ) in message
    assert f"Rescue artifact: {run_dir / 'rescue_refs.json'}" in message
    artifact = json.loads((run_dir / "rescue_refs.json").read_text(encoding="utf-8"))
    assert artifact == [
        {
            "branch": "feature/x",
            "created_at": artifact[0]["created_at"],
            "ref": rescue_refs[0],
            "sha": commit_x,
        }
    ]
    assert any(
        record.event == "branch_reset_refused"
        and record.branch == "feature/x"
        and record.rescue_ref == rescue_refs[0]
        for record in caplog.records
    )


def test_reused_branch_reset_refuses_when_rev_list_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    first = create_worktree(repo_root=repo, branch="feature/timeout", parent_dir=tmp_path)
    unpushed = _commit_file(first.worktree_path, "x.txt", "x\n", "feature X")
    _git(repo, "worktree", "remove", str(first.worktree_path))
    _commit_file(repo, "main.txt", "main\n", "main advances")
    real_run_git = worktree_mod._run_git

    def timeout_runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del args, kwargs
        raise subprocess.TimeoutExpired(cmd=["git", "rev-list"], timeout=0.01)

    def timeout_rev_list(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["rev-list", "--max-count=20"]:
            kwargs["runner"] = timeout_runner
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(worktree_mod, "_run_git", timeout_rev_list)

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        create_worktree(
            repo_root=repo,
            branch="feature/timeout",
            parent_dir=tmp_path,
            allow_destructive_branch_reset=True,
        )

    assert excinfo.value.returncode == 124
    assert _git_output(repo, "rev-parse", "refs/heads/feature/timeout") == unpushed


def test_reused_branch_with_remote_commits_resets_without_rescue(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    origin = _init_bare_remote(tmp_path, "origin.git")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    first = create_worktree(repo_root=repo, branch="feature/remote", parent_dir=tmp_path)
    commit_x = _commit_file(first.worktree_path, "x.txt", "x\n", "feature X")
    _git(first.worktree_path, "push", "-u", "origin", "feature/remote")
    _git(repo, "fetch", "origin", "feature/remote:refs/remotes/origin/feature/remote")
    _git(repo, "worktree", "remove", str(first.worktree_path))

    commit_c = _commit_file(repo, "c.txt", "c\n", "main C")
    second = create_worktree(repo_root=repo, branch="feature/remote", parent_dir=tmp_path)

    assert _git_output(second.worktree_path, "rev-parse", "HEAD") == commit_c
    assert _git_output(origin, "rev-parse", "refs/heads/feature/remote") == commit_x
    rescue_refs = _git_output(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/rescue/feature-remote",
    )
    assert rescue_refs == ""


@pytest.mark.parametrize("allow_destructive_branch_reset", [False, True])
def test_reused_fully_pushed_reviewed_head_is_protected_from_reset(
    tmp_path: Path,
    allow_destructive_branch_reset: bool,
) -> None:
    repo = _init_repo(tmp_path)
    origin = _init_bare_remote(tmp_path, "origin-reviewed.git")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    first = create_worktree(
        repo_root=repo,
        branch="feature/reviewed",
        parent_dir=tmp_path,
    )
    reviewed_head = _commit_file(
        first.worktree_path,
        "reviewed.txt",
        "reviewed\n",
        "reviewed feature head",
    )
    _git(first.worktree_path, "push", "-u", "origin", "feature/reviewed")
    _git(
        repo,
        "fetch",
        "origin",
        "feature/reviewed:refs/remotes/origin/feature/reviewed",
    )
    _git(repo, "worktree", "remove", str(first.worktree_path))
    _commit_file(repo, "main.txt", "main advances\n", "main advances")

    # The dangerous shape: the reviewed commit is fully remote-reachable,
    # so the pre-existing unpushed-commit guard has no reason to fire.
    assert (
        _git_output(
            repo,
            "rev-list",
            "refs/heads/feature/reviewed",
            "--not",
            "main",
            "--remotes",
        )
        == ""
    )

    with pytest.raises(ValueError) as excinfo:
        create_worktree(
            repo_root=repo,
            branch="feature/reviewed",
            parent_dir=tmp_path,
            allow_destructive_branch_reset=allow_destructive_branch_reset,
            protected_head_sha=reviewed_head,
        )

    assert _git_output(repo, "rev-parse", "refs/heads/feature/reviewed") == reviewed_head
    message = str(excinfo.value)
    assert reviewed_head in message
    assert "recorded review verdict describes that exact SHA" in message
    assert "repoint `worktree_branch` to a NEW branch name" in message
    assert "reviewed head is never touched" in message
    assert "does not override reviewed-head protection" in message
    assert (
        _git_output(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/rescue/feature-reviewed",
        )
        == ""
    )


def test_reviewed_head_movement_during_reset_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _init_repo(tmp_path)
    origin = _init_bare_remote(tmp_path, "origin-race.git")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    first = create_worktree(
        repo_root=repo,
        branch="feature/race",
        parent_dir=tmp_path,
    )
    initial_head = _commit_file(
        first.worktree_path,
        "feature.txt",
        "feature\n",
        "published feature",
    )
    _git(first.worktree_path, "push", "-u", "origin", "feature/race")
    _git(repo, "fetch", "origin", "feature/race:refs/remotes/origin/feature/race")
    _git(repo, "worktree", "remove", str(first.worktree_path))
    reviewed_head = _commit_file(repo, "reviewed.txt", "reviewed\n", "reviewed main")
    _commit_file(repo, "latest.txt", "latest\n", "latest main")
    real_run_git = worktree_mod.run_git
    moved = False

    def move_branch_before_atomic_reset(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal moved
        if args and args[0] == "update-ref" and not moved:
            moved = True
            _git(repo, "update-ref", "refs/heads/feature/race", reviewed_head)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(worktree_mod, "run_git", move_branch_before_atomic_reset)

    with pytest.raises(ValueError, match="atomic ref update did not complete"):
        create_worktree(
            repo_root=repo,
            branch="feature/race",
            parent_dir=tmp_path,
            protected_head_sha=reviewed_head,
        )

    assert moved is True
    assert initial_head != reviewed_head
    assert _git_output(repo, "rev-parse", "refs/heads/feature/race") == reviewed_head


def test_reused_branch_with_base_reachable_commits_resets_without_rescue(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "b.txt", "b\n", "main B")
    first = create_worktree(repo_root=repo, branch="feature/base", parent_dir=tmp_path)
    _git(repo, "worktree", "remove", str(first.worktree_path))

    commit_c = _commit_file(repo, "c.txt", "c\n", "main C")
    second = create_worktree(repo_root=repo, branch="feature/base", parent_dir=tmp_path)

    assert _git_output(second.worktree_path, "rev-parse", "HEAD") == commit_c
    rescue_refs = _git_output(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/rescue/feature-base",
    )
    assert rescue_refs == ""


def test_destructive_branch_reset_still_records_rescue_artifact(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "b.txt", "b\n", "main B")
    first = create_worktree(repo_root=repo, branch="feature/force", parent_dir=tmp_path)
    commit_x = _commit_file(first.worktree_path, "x.txt", "x\n", "feature X")
    _git(repo, "worktree", "remove", str(first.worktree_path))
    commit_c = _commit_file(repo, "c.txt", "c\n", "main C")
    run_dir = _write_run_task(
        repo,
        task_id="T-123",
        timestamp="20260709T000000Z",
        branch="feature/force",
    )

    forced = create_worktree(
        repo_root=repo,
        branch="feature/force",
        parent_dir=tmp_path,
        allow_destructive_branch_reset=True,
    )

    assert _git_output(forced.worktree_path, "rev-parse", "HEAD") == commit_c
    rescue_refs = json.loads((run_dir / "rescue_refs.json").read_text(encoding="utf-8"))
    assert rescue_refs == [
        {
            "branch": "feature/force",
            "created_at": rescue_refs[0]["created_at"],
            "ref": rescue_refs[0]["ref"],
            "sha": commit_x,
        }
    ]
    assert rescue_refs[0]["ref"].startswith("refs/rescue/feature-force/")
    assert _git_output(repo, "rev-parse", rescue_refs[0]["ref"]) == commit_x


def test_rescue_artifact_write_failure_refuses_even_with_destructive_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    repo = _init_repo(tmp_path)
    caplog.set_level(logging.WARNING, logger=worktree_mod.LOGGER.name)
    _commit_file(repo, "b.txt", "b\n", "main B")
    first = create_worktree(
        repo_root=repo,
        branch="feature/artifact-failure",
        parent_dir=tmp_path,
    )
    commit_x = _commit_file(first.worktree_path, "x.txt", "x\n", "feature X")
    _git(repo, "worktree", "remove", str(first.worktree_path))
    _commit_file(repo, "c.txt", "c\n", "main C")
    run_dir = _write_run_task(
        repo,
        task_id="T-125",
        timestamp="20260709T000200Z",
        branch="feature/artifact-failure",
    )
    artifact_path = run_dir / "rescue_refs.json"
    real_write_text = worktree_mod.Path.write_text

    def fail_rescue_artifact_write(
        self: Path, data: str, *args: object, **kwargs: object
    ) -> int:
        if self == artifact_path:
            raise OSError("metadata path unavailable")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(
        worktree_mod.Path,
        "write_text",
        fail_rescue_artifact_write,
    )

    with pytest.raises(ValueError) as excinfo:
        create_worktree(
            repo_root=repo,
            branch="feature/artifact-failure",
            parent_dir=tmp_path,
            allow_destructive_branch_reset=True,
        )

    message = str(excinfo.value)
    assert "writing rescue artifact" in message
    assert str(artifact_path) in message
    assert "Fix write access to the run metadata path, then re-run." in message
    assert (
        _git_output(repo, "rev-parse", "refs/heads/feature/artifact-failure")
        == commit_x
    )
    rescue_refs = _git_output(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/rescue/feature-artifact-failure",
    ).splitlines()
    assert len(rescue_refs) == 1
    assert _git_output(repo, "rev-parse", rescue_refs[0]) == commit_x
    assert not artifact_path.exists()
    assert any(
        record.event == "branch_reset_rescue_artifact_write_failed"
        and record.branch == "feature/artifact-failure"
        and record.rescue_ref == rescue_refs[0]
        for record in caplog.records
    )


def test_no_cleanup_when_path_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    real_run = worktree_mod.subprocess.run
    remove_calls: list[list[str]] = []

    def tracked_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "worktree", "remove"]:
            remove_calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(worktree_mod.subprocess, "run", tracked_run)

    created = create_worktree(repo_root=repo, branch="codex/fresh", parent_dir=tmp_path)

    assert created.worktree_path.is_dir()
    assert remove_calls == []


def test_worktree_venv_is_real_and_site_packages_isolated(tmp_path: Path) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    parent_python = _create_parent_venv(repo)
    parent_site_packages = _site_packages(parent_python)

    created = create_worktree(repo_root=repo, branch="codex/venv", parent_dir=tmp_path)
    worktree_venv = created.worktree_path / ".venv"
    worktree_python = worktree_venv / "bin" / "python"
    worktree_site_packages = _site_packages(worktree_python)

    assert worktree_venv.exists()
    assert not worktree_venv.is_symlink()
    assert worktree_python.exists()
    assert worktree_site_packages.stat().st_ino != parent_site_packages.stat().st_ino


def test_worktree_editable_install_does_not_modify_parent_site_packages(
    tmp_path: Path,
) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    _write_src_layout_package(repo, value="main")
    _git(repo, "add", "libs")
    _git(repo, "commit", "-m", "add demo package")
    parent_python = _create_parent_venv(repo)
    parent_site_packages = _site_packages(parent_python)
    before = _snapshot_site_packages(parent_site_packages)

    created = create_worktree(
        repo_root=repo,
        branch="codex/parent-site-packages",
        parent_dir=tmp_path,
    )
    _run_uv(
        created.worktree_path,
        "pip",
        "install",
        "--python",
        str(created.worktree_path / ".venv" / "bin" / "python"),
        "--no-deps",
        "-e",
        str(created.worktree_path / "libs" / "demo"),
    )

    after = _snapshot_site_packages(parent_site_packages)
    assert after == before


def test_worktree_venv_ignores_deleted_parent_editable_and_imports_own_sources(
    tmp_path: Path,
) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    _write_src_layout_package(repo, value="main")
    _git(repo, "add", "libs/demo")
    _git(repo, "commit", "-m", "add demo package")
    parent_python = _create_parent_venv(repo)
    deleted_root = _write_pyproject_package(
        repo / "libs" / "deleted",
        project_name="deleted-pkg",
        package_name="deleted_pkg",
        value="deleted",
    )
    _run_uv(
        repo,
        "pip",
        "install",
        "--python",
        str(parent_python),
        "--no-deps",
        "-e",
        str(deleted_root),
    )
    shutil.rmtree(deleted_root)
    parent_freeze = _run_uv(
        repo, "pip", "freeze", "--python", str(parent_python)
    ).stdout
    assert "-e file://" in parent_freeze
    assert "libs/deleted" in parent_freeze

    created = create_worktree(
        repo_root=repo,
        branch="codex/deleted-parent-editable",
        parent_dir=tmp_path,
    )
    worktree_init = (
        created.worktree_path / "libs" / "demo" / "src" / "demo_pkg" / "__init__.py"
    )
    worktree_init.write_text("VALUE = 'worktree'\n", encoding="utf-8")
    worktree_python = created.worktree_path / ".venv" / "bin" / "python"

    result = _run_python(
        worktree_python,
        "import demo_pkg; print(demo_pkg.VALUE); print(demo_pkg.__file__)",
        cwd=created.worktree_path,
    )

    assert result.stdout.splitlines() == ["worktree", str(worktree_init)]


def test_parent_freeze_editable_outside_repo_is_refused_with_repair_hint(
    tmp_path: Path,
) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    _write_src_layout_package(repo, value="main")
    _git(repo, "add", "libs/demo")
    _git(repo, "commit", "-m", "add demo package")
    parent_python = _create_parent_venv(repo)
    outside_root = _write_pyproject_package(
        tmp_path / "outside-editable",
        project_name="outside-pkg",
        package_name="outside_pkg",
        value="outside",
    )
    _run_uv(
        repo,
        "pip",
        "install",
        "--python",
        str(parent_python),
        "--no-deps",
        "-e",
        str(outside_root),
    )

    with pytest.raises(worktree_mod.WorktreeVenvError) as excinfo:
        create_worktree(
            repo_root=repo,
            branch="codex/outside-parent-editable",
            parent_dir=tmp_path,
        )

    message = str(excinfo.value)
    assert str(outside_root.resolve()) in message
    assert "parent venv is corrupted" in message
    assert "recreate the affected worktrees" in message


def test_two_worktree_editable_installs_keep_imports_isolated(tmp_path: Path) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    _write_src_layout_package(repo, value="main")
    _git(repo, "add", "libs")
    _git(repo, "commit", "-m", "add demo package")
    parent_python = _create_parent_venv(repo)
    parent_site_packages = _site_packages(parent_python)
    before_parent_site_packages = _snapshot_site_packages(parent_site_packages)

    first = create_worktree(repo_root=repo, branch="codex/first", parent_dir=tmp_path)
    second = create_worktree(repo_root=repo, branch="codex/second", parent_dir=tmp_path)
    first_init = first.worktree_path / "libs" / "demo" / "src" / "demo_pkg" / "__init__.py"
    second_init = second.worktree_path / "libs" / "demo" / "src" / "demo_pkg" / "__init__.py"
    first_init.write_text("VALUE = 'first'\n", encoding="utf-8")
    second_init.write_text("VALUE = 'second'\n", encoding="utf-8")

    first_python = first.worktree_path / ".venv" / "bin" / "python"
    second_python = second.worktree_path / ".venv" / "bin" / "python"
    first_site_packages = _site_packages(first_python)
    second_site_packages = _site_packages(second_python)

    import_code = "import demo_pkg; print(demo_pkg.VALUE); print(demo_pkg.__file__)"
    first_import = _run_python(first_python, import_code, cwd=first.worktree_path)
    second_import = _run_python(second_python, import_code, cwd=second.worktree_path)

    assert first_import.stdout.splitlines() == [
        "first",
        str(first_init),
    ]
    assert second_import.stdout.splitlines() == [
        "second",
        str(second_init),
    ]
    assert first_site_packages != second_site_packages
    assert first_site_packages.stat().st_ino != second_site_packages.stat().st_ino
    assert _snapshot_site_packages(parent_site_packages) == before_parent_site_packages


def test_worktree_setup_replaces_seeded_same_version_first_party_wheel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    package_root = _write_src_layout_package(repo, value="stale")
    (repo / ".gitignore").write_text(".venv*\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add stale first-party source")
    _create_parent_venv(repo)
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "shared-uv-cache"))

    stale_wheels = tmp_path / "stale-wheels"
    _run_uv(
        repo,
        "build",
        "--wheel",
        "--out-dir",
        str(stale_wheels),
        str(package_root),
    )
    stale_wheel = next(stale_wheels.glob("demo_pkg-0.0.0-*.whl"))
    source = package_root / "src" / "demo_pkg" / "__init__.py"
    source.write_text("VALUE = 'current'\n", encoding="utf-8")
    _git(repo, "add", "libs/demo")
    _git(repo, "commit", "-m", "change source without changing version")

    real_uv = shutil.which("uv")
    assert real_uv is not None
    uv_wrapper = tmp_path / "uv-shared-cache-proxy"
    # Model a shared-cache hit deterministically: an unisolated editable build
    # receives the real stale wheel, while an isolated build runs real uv on
    # the current worktree source. The assertion below is solely import-based.
    uv_wrapper.write_text(
        f"""#!{sys.executable}
import os
import sys

args = sys.argv[1:]
if args[:2] == ["pip", "install"] and "-e" in args and "--cache-dir" not in args:
    editable_index = args.index("-e")
    args = args[:editable_index] + [{str(stale_wheel)!r}]
os.execv({real_uv!r}, [{real_uv!r}, *args])
""",
        encoding="utf-8",
    )
    uv_wrapper.chmod(0o755)
    monkeypatch.setattr(worktree_mod, "_find_uv", lambda: uv_wrapper)

    created = create_worktree(
        repo_root=repo,
        branch="codex/same-version-wheel",
        parent_dir=tmp_path,
    )
    worktree_source = (
        created.worktree_path
        / "libs"
        / "demo"
        / "src"
        / "demo_pkg"
        / "__init__.py"
    )
    imported = _run_python(
        created.worktree_path / ".venv" / "bin" / "python",
        "import demo_pkg; print(demo_pkg.VALUE); print(demo_pkg.__file__)",
        cwd=created.worktree_path,
    )

    assert imported.stdout.splitlines() == ["current", str(worktree_source)]


def test_worktree_editable_install_argv_uses_only_discovered_worktree_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    _write_src_layout_package(repo, value="main")
    _git(repo, "add", "libs/demo")
    _git(repo, "commit", "-m", "add demo package")
    _create_parent_venv(repo)
    commands: list[tuple[list[str], str]] = []

    def fake_run_venv_command(
        cmd: list[str], *, cwd: Path, reason: str
    ) -> subprocess.CompletedProcess[str]:
        commands.append((cmd, reason))
        if reason == "parent_freeze_failed":
            stale_in_repo = (repo / "libs" / "stale").resolve().as_uri()
            outside_file = (tmp_path / "outside-wheel").resolve().as_uri()
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=(
                    f"-e {stale_in_repo}\n"
                    f"outside-wheel @ {outside_file}\n"
                    "third-party==1.2.3\n"
                ),
                stderr="",
            )
        if reason == "uv_venv_failed":
            venv_path = Path(cmd[-1])
            (venv_path / "bin").mkdir(parents=True)
            (venv_path / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(worktree_mod, "_find_uv", lambda: Path("/usr/bin/uv"))
    monkeypatch.setattr(worktree_mod, "_run_venv_command", fake_run_venv_command)

    created = create_worktree(
        repo_root=repo,
        branch="codex/editable-argv",
        parent_dir=tmp_path,
    )

    editable_install_commands = [
        cmd for cmd, reason in commands if reason == "editable_install_failed"
    ]
    editable_paths = [
        Path(cmd[cmd.index("-e") + 1]).resolve() for cmd in editable_install_commands
    ]
    dependency_install_commands = [
        cmd for cmd, reason in commands if reason == "dependency_install_failed"
    ]
    worktree_root = created.worktree_path.resolve()

    assert editable_paths == [(created.worktree_path / "libs" / "demo").resolve()]
    assert all(path.is_relative_to(worktree_root) for path in editable_paths)
    assert all("--reinstall" in command for command in editable_install_commands)
    assert [
        Path(command[command.index("--cache-dir") + 1])
        for command in editable_install_commands
    ] == [
        created.worktree_path
        / ".venv"
        / worktree_mod._FIRST_PARTY_UV_CACHE_DIRNAME
    ]
    assert dependency_install_commands == [
        [
            "/usr/bin/uv",
            "pip",
            "install",
            "--python",
            str(created.worktree_path / ".venv" / "bin" / "python"),
            "--no-deps",
            "third-party==1.2.3",
        ]
    ]
    assert all(
        "file://" not in arg
        for command in dependency_install_commands
        for arg in command
    )


def test_flat_layout_worktree_acceptance_passes_with_real_venv(tmp_path: Path) -> None:
    _require_uv()
    repo = _init_repo(tmp_path)
    _write_flat_layout_project(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "add flat package")
    parent_python = _create_parent_venv(repo)
    _run_uv(
        repo,
        "pip",
        "install",
        "--python",
        str(parent_python),
        f"pytest=={pytest.__version__}",
    )
    _run_uv(
        repo,
        "pip",
        "install",
        "--python",
        str(parent_python),
        "--no-deps",
        "-e",
        str(repo),
    )

    created = create_worktree(repo_root=repo, branch="codex/flat", parent_dir=tmp_path)
    worktree_venv = created.worktree_path / ".venv"
    result = subprocess.run(
        [str(worktree_venv / "bin" / "python"), "-m", "pytest", "tests", "-q"],
        cwd=created.worktree_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert not worktree_venv.is_symlink()
    assert "1 passed" in result.stdout


def test_venv_creation_failure_never_silently_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path)
    _create_parent_venv(repo)
    monkeypatch.setattr(worktree_mod, "_find_uv", lambda: None)

    with pytest.raises(worktree_mod.WorktreeVenvError, match="reason=uv_not_found"):
        create_worktree(repo_root=repo, branch="codex/venv-failure", parent_dir=tmp_path)

    worktree_venv = worktree_path_for(repo, "codex/venv-failure") / ".venv"
    assert not worktree_venv.is_symlink()


def test_no_venv_when_repo_has_no_parent_venv(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    created = create_worktree(repo_root=repo, branch="codex/no-venv", parent_dir=tmp_path)

    assert not (created.worktree_path / ".venv").exists()


def test_remove_failure_logs_and_add_fails_when_path_persists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A real remove failure leaves the stale checkout in place, so add fails."""
    repo = _init_repo(tmp_path)
    stale = create_worktree(repo_root=repo, branch="codex/remove-fails", parent_dir=tmp_path)
    real_run = worktree_mod.subprocess.run
    caplog.set_level(logging.WARNING, logger=worktree_mod.LOGGER.name)

    def run_with_failing_remove(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["git", "worktree", "remove"]:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="simulated remove failure",
            )
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(worktree_mod.subprocess, "run", run_with_failing_remove)

    with pytest.raises(subprocess.CalledProcessError):
        create_worktree(repo_root=repo, branch="codex/remove-fails", parent_dir=tmp_path)

    assert stale.worktree_path.is_dir()
    assert any(
        record.levelno == logging.WARNING
        and record.event == "worktree_autoclean"
        and record.path == str(stale.worktree_path)
        and record.remove_failed is True
        for record in caplog.records
    )


def test_non_directory_path_collision_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    collision = worktree_path_for(repo, "codex/file-collision")
    collision.write_text("not a worktree\n", encoding="utf-8")

    with pytest.raises(ValueError, match="worktree path already exists"):
        create_worktree(repo_root=repo, branch="codex/file-collision", parent_dir=tmp_path)

    assert collision.read_text(encoding="utf-8") == "not a worktree\n"


def test_detached_head_commit_is_rescued_before_redispatch(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    first = create_worktree(
        repo_root=repo,
        branch="codex/detached",
        parent_dir=tmp_path,
        detach=True,
    )
    detached_commit = _commit_file(
        first.worktree_path,
        "detached.txt",
        "detached\n",
        "detached work",
    )

    second = create_worktree(
        repo_root=repo,
        branch="codex/detached",
        parent_dir=tmp_path,
        detach=True,
    )

    rescue_refs = _git_output(
        repo,
        "for-each-ref",
        "--format=%(refname)",
        "refs/rescue/codex-detached",
    ).splitlines()
    assert first.worktree_path == worktree_path_for(repo, "codex/detached")
    assert second.worktree_path == first.worktree_path
    assert second.worktree_path.is_dir()
    assert len(rescue_refs) == 1
    assert _git_output(repo, "rev-parse", rescue_refs[0]) == detached_commit


def test_concurrent_dispatch_safety(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    def create() -> Path:
        worktree = create_worktree(repo_root=repo, branch="codex/race", parent_dir=tmp_path)
        return worktree.worktree_path

    with ThreadPoolExecutor(max_workers=2) as executor:
        paths = list(executor.map(lambda _: create(), range(2)))

    assert paths == [paths[0], paths[0]]
    assert paths[0].is_dir()
    assert _git_output(paths[0], "branch", "--show-current") == "codex/race"


def test_dirty_existing_worktree_is_not_auto_cleaned(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    stale = create_worktree(repo_root=repo, branch="codex/dirty", parent_dir=tmp_path)
    (stale.worktree_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="refusing to auto-clean dirty worktree"):
        create_worktree(repo_root=repo, branch="codex/dirty", parent_dir=tmp_path)

    assert (stale.worktree_path / "dirty.txt").read_text(encoding="utf-8") == "dirty\n"


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "oserror"])
def test_auto_remove_refuses_when_cleanliness_is_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = tmp_path / "possibly-dirty-worktree"
    path.mkdir()
    calls: list[list[str]] = []

    def uncertain_status(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        if args == ["status", "--porcelain"]:
            if failure == "oserror":
                raise OSError("status unavailable")
            return subprocess.CompletedProcess(
                args=args,
                returncode=124 if failure == "timeout" else 1,
                stdout="",
                stderr="status failed",
            )
        raise AssertionError(f"unexpected destructive git call: {args}")

    monkeypatch.setattr(worktree_mod, "run_git", uncertain_status)

    with pytest.raises(ValueError, match="without confirmed clean status"):
        worktree_mod._auto_remove_worktree(
            tmp_path,
            path,
            branch="codex/uncertain",
            base_ref="main",
        )

    assert ["worktree", "remove", "--force", str(path)] not in calls
    assert path.is_dir()


def test_auto_remove_proceeds_when_cleanliness_is_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "clean-worktree"
    path.mkdir()
    calls: list[list[str]] = []

    def clean_status(
        args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(args)
        if args == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if args == ["symbolic-ref", "--quiet", "HEAD"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="refs/heads/codex/clean\n",
                stderr="",
            )
        if args == ["worktree", "remove", "--force", str(path)]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(worktree_mod, "run_git", clean_status)

    worktree_mod._auto_remove_worktree(
        tmp_path,
        path,
        branch="codex/clean",
        base_ref="main",
    )

    assert ["worktree", "remove", "--force", str(path)] in calls


@pytest.mark.parametrize("dirty_state", ["unstaged", "staged"])
def test_dirty_existing_worktree_with_tracked_modifications_is_not_auto_cleaned(
    tmp_path: Path, dirty_state: str
) -> None:
    repo = _init_repo(tmp_path)
    stale = create_worktree(
        repo_root=repo, branch=f"codex/dirty-{dirty_state}", parent_dir=tmp_path
    )
    readme = stale.worktree_path / "README.md"
    readme.write_text(f"{dirty_state} change\n", encoding="utf-8")
    if dirty_state == "staged":
        _git(stale.worktree_path, "add", "README.md")

    with pytest.raises(ValueError, match="refusing to auto-clean dirty worktree"):
        create_worktree(
            repo_root=repo, branch=f"codex/dirty-{dirty_state}", parent_dir=tmp_path
        )

    assert readme.read_text(encoding="utf-8") == f"{dirty_state} change\n"


def test_worktree_paths_for_branch_excludes_main_checkout(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)

    assert worktree_mod._worktree_paths_for_branch(repo, "main") == []


def test_publish_branch_honors_explicit_remote_name(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    origin = _init_bare_remote(tmp_path, "origin.git")
    mirror = _init_bare_remote(tmp_path, "mirror.git")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "remote", "add", "mirror", str(mirror))
    worktree = create_worktree(
        repo_root=repo,
        branch="codex/publish-mirror",
        parent_dir=tmp_path,
    )
    _commit_file(
        worktree.worktree_path,
        "feature.txt",
        "feature\n",
        "feature",
    )

    result = worktree_mod.publish_branch(worktree, remote_name="mirror")

    assert result.pushed
    assert result.remote_name == "mirror"
    assert result.remote_ref == "mirror/codex/publish-mirror"
    assert _git_output(
        mirror,
        "show-ref",
        "--verify",
        "refs/heads/codex/publish-mirror",
    )


def test_publish_branch_single_origin_happy_path_unchanged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    origin = _init_bare_remote(tmp_path, "origin.git")
    _git(repo, "remote", "add", "origin", str(origin))
    worktree = create_worktree(
        repo_root=repo,
        branch="codex/publish-origin",
        parent_dir=tmp_path,
    )
    _commit_file(
        worktree.worktree_path,
        "feature.txt",
        "feature\n",
        "feature",
    )

    result = worktree_mod.publish_branch(worktree)

    assert result.pushed
    assert result.remote_name == "origin"
    assert result.remote_ref == "origin/codex/publish-origin"


def test_publish_branch_preserves_two_independent_runs_on_reused_branch(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    origin = _init_bare_remote(tmp_path, "origin.git")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-u", "origin", "main")
    branch = "codex/reused-review"

    first = create_worktree(
        repo_root=repo,
        branch=branch,
        parent_dir=tmp_path / "first-worktrees",
    )
    first_sha = _commit_file(
        first.worktree_path,
        "review.txt",
        "first independent review\n",
        "first review",
    )
    first_publish = worktree_mod.publish_branch(first)
    assert first_publish.pushed
    assert first_publish.remote_ref == f"origin/{branch}"

    second_repo = tmp_path / "second-repo"
    _git(tmp_path, "clone", str(origin), str(second_repo))
    _git(second_repo, "config", "user.email", "test@example.invalid")
    _git(second_repo, "config", "user.name", "Test User")
    _git(second_repo, "checkout", "-b", branch, "origin/main")
    second_sha = _commit_file(
        second_repo,
        "review.txt",
        "second independent review\n",
        "second review",
    )
    second = worktree_mod.Worktree(
        repo_root=second_repo,
        worktree_path=second_repo,
        branch=branch,
    )

    second_publish = worktree_mod.publish_branch(second)

    assert second_publish.pushed
    assert second_publish.requested_remote_ref == f"origin/{branch}"
    assert second_publish.remote_ref == f"origin/{branch}-run2"
    assert second_publish.remote_ref != second_publish.requested_remote_ref
    assert first_sha != second_sha
    assert _git_output(origin, "rev-parse", f"refs/heads/{branch}") == first_sha
    assert _git_output(origin, "rev-parse", f"refs/heads/{branch}-run2") == second_sha
    assert (
        _git_output(origin, "show", f"refs/heads/{branch}:review.txt")
        == "first independent review"
    )
    assert (
        _git_output(origin, "show", f"refs/heads/{branch}-run2:review.txt")
        == "second independent review"
    )


def test_list_changed_files_records_both_endpoints_of_a_detected_rename(
    tmp_path: Path,
) -> None:
    """A detected rename must not hide the pre-rename path.

    Git detects renames by default and then records ONLY the post-rename path.
    The protected-path check measures the paths it is handed, so without
    ``--no-renames`` a high-similarity rename of a protected file escapes it:
    `configs/prod.yaml -> docs/notes.md` would be recorded as only
    `docs/notes.md`, and no protected path would appear touched.

    The file below is deliberately 40 lines with a single line edited. A tiny
    file edited in full scores 0% similarity, git records A+D rather than R,
    and the bug does not reproduce -- so this test would pass vacuously on the
    defective code if the fixture were small.
    """

    repo = _init_repo(tmp_path)
    configs = repo / "configs"
    configs.mkdir()
    original = configs / "prod.yaml"
    original.write_text(
        "\n".join(f"key_{i}: value_{i}" for i in range(40)) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "add prod config")

    worktree_parent = tmp_path / "worktrees"
    worktree_parent.mkdir()
    worktree = create_worktree(
        repo_root=repo, branch="codex/rename", parent_dir=worktree_parent
    )

    moved = worktree.worktree_path / "configs" / "prod_renamed.yaml"
    _git(worktree.worktree_path, "mv", "configs/prod.yaml", str(moved))
    moved.write_text(
        moved.read_text(encoding="utf-8").replace("key_0: value_0", "key_0: CHANGED"),
        encoding="utf-8",
    )
    _git(worktree.worktree_path, "add", "-A")
    _git(worktree.worktree_path, "commit", "-m", "rename prod config")

    changed = list_changed_files(worktree, base_ref="main")

    # Guard the fixture itself: if git did not treat this as a rename, the test
    # cannot discriminate and would pass on the defective code.
    name_status = subprocess.run(
        ["git", "diff", "--name-status", "main...HEAD"],
        cwd=worktree.worktree_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert name_status.startswith("R"), (
        f"fixture did not produce a detected rename; git said: {name_status!r}"
    )

    assert "configs/prod_renamed.yaml" in changed
    assert "configs/prod.yaml" in changed, (
        "pre-rename path missing: a rename can escape the protected-path check"
    )
