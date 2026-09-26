"""MCP server configuration written into a task's worktree.

Each CLI reads MCP servers from a different place: Codex from a
``config.toml`` under ``CODEX_HOME``, Gemini from ``.gemini/settings.json``,
and the others from a JSON file passed on the command line. The generated
files are added to the worktree's git exclude list so they never show up as
changes made by the agent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast


@dataclass(frozen=True, kw_only=True)
class McpRuntime:
    config_path: Path | None = None
    extra_env: dict[str, str] | None = None
    worktree_files: list[Path] = field(default_factory=list)


def _prepare_mcp_runtime(
    cli: str,
    worktree_path: Path,
    mcp_servers: list[dict[str, object]],
) -> McpRuntime | None:
    if not mcp_servers:
        return None

    cli_lower = cli.lower()
    if cli_lower == "codex":
        codex_home = (worktree_path / ".atlas-dispatch" / "codex-home").resolve()
        config_path = _write_codex_config(codex_home, mcp_servers)
        _extend_git_info_exclude(worktree_path, [".atlas-dispatch/"])
        return McpRuntime(
            extra_env={"CODEX_HOME": str(codex_home)},
            worktree_files=[config_path],
        )

    if cli_lower == "gemini":
        settings_path = _write_gemini_settings(worktree_path, mcp_servers)
        _extend_git_info_exclude(worktree_path, [".gemini/"])
        return McpRuntime(worktree_files=[settings_path])

    config_path = _write_mcp_json_config(worktree_path, mcp_servers)
    _extend_git_info_exclude(worktree_path, [".atlas-dispatch-mcp.json"])
    return McpRuntime(config_path=config_path, worktree_files=[config_path])


def _render_mcp_servers(
    mcp_servers: list[dict[str, object]]
) -> dict[str, dict[str, object]]:
    rendered_servers: dict[str, dict[str, object]] = {}
    for raw_server in mcp_servers:
        server = dict(raw_server)
        name = str(server.pop("name"))
        server.setdefault("transport", "stdio")
        rendered_servers[name] = server
    return rendered_servers


def _write_mcp_json_config(
    worktree_path: Path, mcp_servers: list[dict[str, object]]
) -> Path:
    """Write ``.atlas-dispatch-mcp.json`` for CLIs that take a config-file flag."""

    rendered_servers = _render_mcp_servers(mcp_servers)
    config_path = (worktree_path / ".atlas-dispatch-mcp.json").resolve()
    config_path.write_text(
        json.dumps({"mcpServers": rendered_servers}, indent=2), encoding="utf-8"
    )
    return config_path


def _write_gemini_settings(
    workspace: Path, mcp_servers: list[dict[str, object]]
) -> Path:
    rendered_servers = _render_mcp_servers(mcp_servers)
    settings_path = (workspace / ".gemini" / "settings.json").resolve()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps({"mcpServers": rendered_servers}, indent=2), encoding="utf-8"
    )
    return settings_path


def _write_codex_config(home_dir: Path, mcp_servers: list[dict[str, object]]) -> Path:
    home_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for name, rendered_server in _render_mcp_servers(mcp_servers).items():
        server = dict(rendered_server)
        command = str(server.get("command", ""))
        raw_args = server.get("args") or []
        args = raw_args if isinstance(raw_args, (list, tuple)) else [raw_args]

        lines.append(f"[mcp_servers.{_toml_key(name)}]")
        lines.append(f"command = {_toml_dump_str(command)}")
        lines.append(
            "args = ["
            + ", ".join(_toml_dump_str(str(arg)) for arg in args)
            + "]"
        )

        raw_env = server.get("env")
        if raw_env is not None:
            env = dict(cast(Mapping[object, object], raw_env))
            env_items = ", ".join(
                f"{_toml_key(str(key))} = {_toml_dump_str(str(value))}"
                for key, value in env.items()
            )
            lines.append(f"env = {{ {env_items} }}" if env_items else "env = {}")
        lines.append("")

    config_path = home_dir / "config.toml"
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path.resolve()


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return _toml_dump_str(value)


def _toml_dump_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _extend_git_info_exclude(worktree_path: Path, patterns: list[str]) -> None:
    if not patterns:
        return

    exclude_path = _git_info_exclude_path(worktree_path)
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_text = (
        exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    )
    existing_lines = existing_text.splitlines()
    missing = [pattern for pattern in patterns if pattern not in existing_lines]
    if not missing:
        return

    prefix = "" if not existing_text or existing_text.endswith(("\n", "\r")) else "\n"
    with exclude_path.open("a", encoding="utf-8") as handle:
        handle.write(prefix + "\n".join(missing) + "\n")


def _git_info_exclude_path(worktree_path: Path) -> Path:
    dot_git = worktree_path / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8").strip()
        prefix = "gitdir:"
        if text.lower().startswith(prefix):
            git_dir = Path(text[len(prefix) :].strip())
            if not git_dir.is_absolute():
                git_dir = (worktree_path / git_dir).resolve()
            common_dir_file = git_dir / "commondir"
            if common_dir_file.is_file():
                common_dir = Path(common_dir_file.read_text(encoding="utf-8").strip())
                if not common_dir.is_absolute():
                    common_dir = (git_dir / common_dir).resolve()
                return common_dir / "info" / "exclude"
            return git_dir / "info" / "exclude"
    return dot_git / "info" / "exclude"
