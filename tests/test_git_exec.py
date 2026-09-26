from __future__ import annotations

import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from atlas_dispatch.git_exec import noninteractive_git_env, run_git


def test_noninteractive_git_env_disables_prompts() -> None:
    env = noninteractive_git_env({"PATH": "/usr/bin", "GIT_TERMINAL_PROMPT": "1"})

    assert env["PATH"] == "/usr/bin"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/usr/bin/true"
    assert env["SSH_ASKPASS"] == "/usr/bin/true"
    assert env["GCM_INTERACTIVE"] == "never"


def test_run_git_uses_noninteractive_env_and_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setenv("ATLAS_DISPATCH_GIT_TIMEOUT_SECONDS", "7")
    monkeypatch.delenv("ATLAS_GIT_BIN", raising=False)
    monkeypatch.setattr("atlas_dispatch.git_exec.sys.platform", "linux")
    monkeypatch.setattr("atlas_dispatch.git_exec.subprocess.run", fake_run)

    run_git(["fetch", "origin", "main"], cwd=tmp_path)

    assert seen["cmd"] == [
        "git",
        "fetch",
        "origin",
        "main",
    ]
    assert seen["cwd"] == tmp_path
    assert seen["timeout"] == 7.0
    assert seen["executable"] == "git"
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == "/usr/bin/true"


def test_run_git_timeout_returns_failed_completed_process(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("ATLAS_GIT_BIN", raising=False)
    monkeypatch.setattr("atlas_dispatch.git_exec.sys.platform", "linux")

    result = run_git(
        ["status"],
        cwd=tmp_path,
        timeout=0.01,
        runner=lambda cmd, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])
        ),
    )

    assert result.returncode == 124
    assert "timed out after 0.01" in result.stderr
    assert result.args == [
        "git",
        "status",
    ]


def test_run_git_invokes_atlas_git_bin_override(tmp_path: Path, monkeypatch) -> None:
    argv_path = tmp_path / "argv.txt"
    stub_path = tmp_path / "git-stub"
    stub_path.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {str(argv_path)!r}\n",
        encoding="utf-8",
    )
    stub_path.chmod(0o755)
    monkeypatch.setenv("ATLAS_GIT_BIN", str(stub_path))

    result = run_git(["status", "--short"], cwd=tmp_path)

    assert result.returncode == 0
    assert argv_path.read_text(encoding="utf-8").splitlines() == [
        "status",
        "--short",
    ]


def test_run_git_rejects_missing_atlas_git_bin_without_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    missing_path = tmp_path / "missing-git"
    invoked = False

    def fake_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("runner must not be called for an invalid ATLAS_GIT_BIN")

    monkeypatch.setenv("ATLAS_GIT_BIN", str(missing_path))

    with pytest.raises(RuntimeError, match=r"ATLAS_GIT_BIN.*does not exist"):
        run_git(["status"], cwd=tmp_path, runner=fake_run)

    assert not invoked


@pytest.mark.skipif(
    sys.platform != "darwin"
    or not Path("/usr/bin/git").is_file()
    or not os.access("/usr/bin/git", os.X_OK),
    reason="requires the executable system git shipped with macOS",
)
def test_run_git_prefers_system_git_on_darwin(tmp_path: Path, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        seen["command"] = cmd
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.delenv("ATLAS_GIT_BIN", raising=False)

    run_git(["status"], cwd=tmp_path, runner=fake_run)

    assert seen["command"] == [
        "git",
        "status",
    ]
    assert seen["executable"] == "/usr/bin/git"


def test_run_git_fails_fast_on_an_auth_challenge_with_a_configured_helper(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(
        "[credential]\n\thelper = osxkeychain\n",
        encoding="utf-8",
    )

    class AuthRequiredHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="git"')
            self.end_headers()

        def log_message(self, format: str, *_args: object) -> None:
            del format
            return

    with socketserver.TCPServer(("127.0.0.1", 0), AuthRequiredHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(
            [
                "git",
                "remote",
                "add",
                "origin",
                f"http://127.0.0.1:{port}/repo.git",
            ],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

        start = time.monotonic()
        result = run_git(
            ["fetch", "origin"],
            cwd=repo,
            timeout=5,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        )
        elapsed = time.monotonic() - start
        server.shutdown()
        thread.join(timeout=2)

    assert elapsed < 5
    assert result.returncode != 0
    output = f"{result.stdout}\n{result.stderr}"
    assert "127.0.0.1" in output
    assert "could not read Username" in output or "Authentication failed" in output
