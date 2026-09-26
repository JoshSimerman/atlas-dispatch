"""Verify that acceptance imports resolve to the worktree under test.

The provenance guard is only meaningful when it probes the same execution
context that acceptance will use: the same cwd, the same process environment,
and the same Python interpreter named by the import-bearing acceptance command.
If any leg of that triple is unknown, the result is unknown rather than pass.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

IMPORT_PROVENANCE_ARTIFACT = "import_provenance.json"
IMPORT_PROVENANCE_STALE_CLASSIFICATION = "stale_import_binding"
IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION = "unknown"
IMPORT_PROVENANCE_REMEDY = "re-install the editable packages against this worktree"
NO_PACKAGES_REASON = (
    "no top-level packages discovered under libs/*/src, services/*/src, "
    "src/*, declared hatch wheel package paths, or declared setuptools "
    "packages/find roots"
)
UNRESOLVED_INTERPRETER = "<unresolved>"
INTERPRETER_UNRESOLVED_REASON = (
    "acceptance interpreter could not be resolved from import-bearing command"
)
INTERPRETER_MISSING_REASON = "acceptance interpreter does not exist"
PROBE_STDOUT_PREFIX = "__ATLAS_IMPORT_PROVENANCE_JSON__="
DEFAULT_PROBE_TIMEOUT_SECONDS = 60
INTERPRETER_RESOLUTION_TIMEOUT_SECONDS = 30
KNOWN_ACCEPTANCE_WRAPPERS = frozenset({"bash", "env", "sh", "uv"})
#: `uv run` options that consume the FOLLOWING token as their value. An option
#: missing from this set is parsed as a bare flag, so its value is then mistaken
#: for the executable and the interpreter cannot be resolved.
#:
#: Example: without `--extra` in this set, `uv run --extra dev python -m pytest …`
#: parses `dev` as the executable and the guard returns `unknown`. Because
#: unknown is (correctly) not a pass, a build that had already succeeded would
#: be recorded as failed with its acceptance tests never executed.
#:
#: Errors in this table fail CLOSED in both directions — a missing entry eats
#: the executable, and a wrongly-added entry eats the executable too. Neither can
#: manufacture a false pass, so the cost of a mistake here is blocked work, never
#: unverified work. Entries are still limited to options known to take a value.
UV_RUN_OPTIONS_WITH_VALUES = frozenset(
    {
        "--directory",
        "--extra",
        "--group",
        "--index",
        "--no-extra",
        "--no-group",
        "--only-group",
        "--package",
        "--project",
        "--python",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-p",
    }
)
PYTEST_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.])pytest(?![A-Za-z0-9_.-])"
)

ImportProvenanceVerdict = Literal["pass", "stale_import_binding", "unknown"]


@dataclass(frozen=True, kw_only=True)
class DiscoveredPackage:
    name: str
    source_paths: list[str]


@dataclass(frozen=True, kw_only=True)
class PackageImportResult:
    name: str
    source_paths: list[str]
    resolved_path: str | None
    inside_worktree: bool
    verdict: str
    error: str | None = None


@dataclass(frozen=True, kw_only=True)
class ImportProvenanceResult:
    worktree_path: str
    interpreter: str
    verdict: ImportProvenanceVerdict
    reason: str
    packages: list[PackageImportResult] = field(default_factory=list)
    probe_return_code: int | None = None
    probe_stdout: str = ""
    probe_stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_import_provenance_check(
    worktree_path: Path,
    *,
    acceptance_command: str | None = None,
    run_dir: Path | None = None,
    timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ImportProvenanceResult:
    """Probe package imports and optionally write import_provenance.json."""
    result = check_import_provenance(
        worktree_path,
        acceptance_command=acceptance_command,
        timeout_seconds=timeout_seconds,
    )
    if run_dir is not None:
        write_import_provenance_artifact(run_dir, result)
    return result


def check_import_provenance(
    worktree_path: Path,
    *,
    acceptance_command: str | None = None,
    timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
) -> ImportProvenanceResult:
    """Import every provided top-level package in the acceptance interpreter."""
    worktree = worktree_path.resolve(strict=False)
    packages = discover_import_packages(worktree)
    if not packages:
        return ImportProvenanceResult(
            worktree_path=str(worktree),
            interpreter=_interpreter_label(
                resolve_probe_interpreter(worktree, acceptance_command)
            ),
            verdict=IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION,
            reason=NO_PACKAGES_REASON,
        )

    interpreter = resolve_probe_interpreter(worktree, acceptance_command)
    if interpreter is None:
        return _unknown_result(
            worktree=worktree,
            interpreter=UNRESOLVED_INTERPRETER,
            packages=packages,
            reason=_reason_with_command(
                INTERPRETER_UNRESOLVED_REASON,
                acceptance_command=acceptance_command,
            ),
        )

    if not interpreter.exists():
        return _unknown_result(
            worktree=worktree,
            interpreter=str(interpreter),
            packages=packages,
            reason=f"{INTERPRETER_MISSING_REASON}: {interpreter}",
        )

    package_names = [package.name for package in packages]
    probe_env = _probe_environment(acceptance_command)
    try:
        completed = subprocess.run(
            [str(interpreter), "-c", _PROBE_CODE],
            input=json.dumps(package_names),
            cwd=worktree_path,
            env=probe_env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _probe_failure_result(
            worktree=worktree,
            interpreter=interpreter,
            packages=packages,
            reason=f"import provenance probe timed out after {timeout_seconds}s",
            error=f"TimeoutExpired: {exc}",
        )
    except OSError as exc:
        return _probe_failure_result(
            worktree=worktree,
            interpreter=interpreter,
            packages=packages,
            reason="import provenance probe could not start",
            error=f"{type(exc).__name__}: {exc}",
        )

    payload = _extract_probe_payload(completed.stdout)
    if payload is None:
        return _probe_failure_result(
            worktree=worktree,
            interpreter=interpreter,
            packages=packages,
            reason="import provenance probe did not return JSON",
            error="probe_json_missing",
            probe_return_code=completed.returncode,
            probe_stdout=completed.stdout,
            probe_stderr=completed.stderr,
        )

    package_results = _package_results_from_payload(
        packages=packages,
        payload=payload,
        worktree=worktree,
    )
    if completed.returncode != 0:
        return ImportProvenanceResult(
            worktree_path=str(worktree),
            interpreter=str(interpreter),
            verdict=IMPORT_PROVENANCE_STALE_CLASSIFICATION,
            reason=f"import provenance probe exited {completed.returncode}",
            packages=package_results,
            probe_return_code=completed.returncode,
            probe_stdout=completed.stdout,
            probe_stderr=completed.stderr,
        )

    stale = [
        package
        for package in package_results
        if package.verdict != "inside_worktree"
    ]
    if stale:
        return ImportProvenanceResult(
            worktree_path=str(worktree),
            interpreter=str(interpreter),
            verdict=IMPORT_PROVENANCE_STALE_CLASSIFICATION,
            reason="one or more packages did not resolve inside the worktree",
            packages=package_results,
            probe_return_code=completed.returncode,
            probe_stdout=completed.stdout,
            probe_stderr=completed.stderr,
        )

    return ImportProvenanceResult(
        worktree_path=str(worktree),
        interpreter=str(interpreter),
        verdict="pass",
        reason="all discovered packages resolved inside the worktree",
        packages=package_results,
        probe_return_code=completed.returncode,
        probe_stdout=completed.stdout,
        probe_stderr=completed.stderr,
    )


def discover_import_packages(worktree_path: Path) -> list[DiscoveredPackage]:
    worktree = worktree_path.resolve(strict=False)
    discovered: dict[str, set[Path]] = {}

    for src_root in _src_roots(worktree):
        _add_immediate_packages(discovered, src_root)

    for package_name, package_dir in _declared_build_package_dirs(worktree):
        _add_package(discovered, package_dir, name=package_name)

    packages = [
        DiscoveredPackage(
            name=name,
            source_paths=[str(path) for path in sorted(paths, key=str)],
        )
        for name, paths in sorted(discovered.items())
    ]
    return packages


def is_import_bearing_acceptance_command(command: str) -> bool:
    argv = _acceptance_command_argv(command)
    if argv:
        return _argv_is_import_bearing(argv)
    return _unparseable_command_is_pytest_bearing_wrapper(command)


def _argv_is_import_bearing(argv: list[str]) -> bool:
    if not argv:
        return False
    executable = argv[0]
    if _is_python_executable(executable) and _argv_invokes_pytest_module(argv[1:]):
        return True
    if Path(executable).name == "pytest":
        return True
    executable_name = Path(executable).name
    if executable_name == "uv":
        uv_parts = _uv_run_parts(argv)
        if uv_parts is not None:
            _uv_options, inner_argv = uv_parts
            if _argv_is_import_bearing(inner_argv):
                return True
        return _known_wrapper_contains_pytest(argv)
    if executable_name == "env":
        env_parts = _env_inner_parts(argv)
        if env_parts is not None:
            inner_argv, _inner_env = env_parts
            if _argv_is_import_bearing(inner_argv):
                return True
        return _known_wrapper_contains_pytest(argv)
    shell_parts = _shell_inner_command_parts(argv)
    if shell_parts is not None:
        inner_command, _shell_lookup_flag = shell_parts
        inner_argv, _inner_env = _acceptance_command_parts(inner_command)
        if _argv_is_import_bearing(inner_argv):
            return True
    if executable_name in KNOWN_ACCEPTANCE_WRAPPERS:
        return _known_wrapper_contains_pytest(argv)
    return False


def _argv_is_direct_pytest_invocation(argv: list[str]) -> bool:
    if not argv:
        return False
    if Path(argv[0]).name == "pytest":
        return True
    return _is_python_executable(argv[0]) and _argv_invokes_pytest_module(argv[1:])


def resolve_probe_interpreter(
    worktree_path: Path,
    acceptance_command: str | None,
) -> Path | None:
    argv, env_assignments = _acceptance_command_parts(acceptance_command)
    return _resolve_probe_interpreter_parts(
        worktree_path,
        argv,
        env_assignments=env_assignments,
        shell_lookup=None,
    )


def _resolve_probe_interpreter_parts(
    worktree_path: Path,
    argv: list[str],
    *,
    env_assignments: dict[str, str],
    shell_lookup: tuple[str, str] | None,
) -> Path | None:
    if not argv:
        return None
    executable = argv[0]
    if _is_python_executable(executable) and _argv_invokes_pytest_module(argv[1:]):
        return _resolve_executable_path(
            worktree_path,
            executable,
            env_assignments=env_assignments,
            shell_lookup=shell_lookup,
        )
    if Path(executable).name == "pytest":
        if shell_lookup is None:
            return None
        pytest_path = _resolve_executable_path(
            worktree_path,
            executable,
            env_assignments=env_assignments,
            shell_lookup=shell_lookup,
        )
        if pytest_path is None:
            return None
        return _pytest_shebang_interpreter(
            worktree_path,
            pytest_path,
            env_assignments=env_assignments,
            shell_lookup=shell_lookup,
        )
    executable_name = Path(executable).name
    if executable_name == "uv":
        uv_parts = _uv_run_parts(argv)
        if uv_parts is None:
            return None
        uv_options, inner_argv = uv_parts
        if not _argv_is_direct_pytest_invocation(inner_argv):
            return None
        return _resolve_uv_probe_interpreter(
            worktree_path,
            uv_executable=executable,
            uv_options=uv_options,
            inner_argv=inner_argv,
            env_assignments=env_assignments,
            shell_lookup=shell_lookup,
        )
    if executable_name == "env":
        env_parts = _env_inner_parts(argv)
        if env_parts is None:
            return None
        inner_argv, inner_env = env_parts
        merged_env = dict(env_assignments)
        merged_env.update(inner_env)
        return _resolve_probe_interpreter_parts(
            worktree_path,
            inner_argv,
            env_assignments=merged_env,
            shell_lookup=shell_lookup,
        )
    shell_parts = _shell_inner_command_parts(argv)
    if shell_parts is not None:
        inner_command, shell_lookup_flag = shell_parts
        inner_argv, inner_env = _acceptance_command_parts(inner_command)
        merged_env = dict(env_assignments)
        merged_env.update(inner_env)
        return _resolve_probe_interpreter_parts(
            worktree_path,
            inner_argv,
            env_assignments=merged_env,
            shell_lookup=(executable, shell_lookup_flag),
        )
    return None


def _acceptance_command_argv(command: str | None) -> list[str]:
    argv, _env_assignments = _acceptance_command_parts(command)
    return argv


def _acceptance_command_parts(command: str | None) -> tuple[list[str], dict[str, str]]:
    if not command:
        return [], {}
    try:
        argv = shlex.split(command)
    except ValueError:
        return [], {}
    env_assignments: dict[str, str] = {}
    while argv and _is_env_assignment(argv[0]):
        name, _separator, value = argv.pop(0).partition("=")
        env_assignments[name] = value
    return argv, env_assignments


def _is_env_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return (
        bool(separator)
        and bool(name)
        and not name[0].isdigit()
        and all(character.isalnum() or character == "_" for character in name)
    )


def _uv_run_parts(argv: list[str]) -> tuple[list[str], list[str]] | None:
    if len(argv) < 2 or Path(argv[0]).name != "uv" or argv[1] != "run":
        return None
    uv_options: list[str] = []
    index = 2
    while index < len(argv):
        token = argv[index]
        if token == "--":
            uv_options.append(token)
            index += 1
            break
        if token == "-" or not token.startswith("-"):
            break
        uv_options.append(token)
        option_name, has_inline_value, _value = token.partition("=")
        if option_name in UV_RUN_OPTIONS_WITH_VALUES and not has_inline_value:
            index += 1
            if index >= len(argv):
                return None
            uv_options.append(argv[index])
        index += 1
    return uv_options, argv[index:]


def _env_inner_parts(argv: list[str]) -> tuple[list[str], dict[str, str]] | None:
    if not argv or Path(argv[0]).name != "env":
        return None
    index = 1
    if index < len(argv) and argv[index] == "--":
        index += 1
    elif index < len(argv) and argv[index].startswith("-"):
        return None
    env_assignments: dict[str, str] = {}
    while index < len(argv) and _is_env_assignment(argv[index]):
        name, _separator, value = argv[index].partition("=")
        env_assignments[name] = value
        index += 1
    return argv[index:], env_assignments


def _shell_inner_command_parts(argv: list[str]) -> tuple[str, str] | None:
    if len(argv) < 3 or Path(argv[0]).name not in {"bash", "sh"}:
        return None
    login_shell = False
    for index, token in enumerate(argv[1:], start=1):
        if token == "--":
            return None
        if not token.startswith("-") or token.startswith("--"):
            continue
        flag_bundle = token[1:]
        login_shell = login_shell or "l" in flag_bundle
        if flag_bundle.endswith("c"):
            if index + 1 >= len(argv):
                return None
            return argv[index + 1], "-lc" if login_shell else "-c"
    return None


def _known_wrapper_contains_pytest(argv: list[str]) -> bool:
    return bool(argv) and Path(argv[0]).name in KNOWN_ACCEPTANCE_WRAPPERS and bool(
        PYTEST_TOKEN_PATTERN.search(" ".join(argv[1:]))
    )


def _unparseable_command_is_pytest_bearing_wrapper(command: str) -> bool:
    wrapper_pattern = r"(?:^|\s)(?:[^\s/]+/)?(?:bash|env|sh|uv)(?:\s|$)"
    return bool(re.search(wrapper_pattern, command)) and bool(
        PYTEST_TOKEN_PATTERN.search(command)
    )


def _is_python_executable(executable: str) -> bool:
    name = Path(executable).name
    if name == "python":
        return True
    if not name.startswith("python"):
        return False
    suffix = name.removeprefix("python")
    return bool(suffix) and all(character.isdigit() or character == "." for character in suffix)


def _argv_invokes_pytest_module(argv: list[str]) -> bool:
    for index, arg in enumerate(argv):
        if arg == "-m":
            return index + 1 < len(argv) and argv[index + 1] == "pytest"
        if arg.startswith("-m") and len(arg) > 2:
            return arg[2:] == "pytest"
        if arg == "-c":
            return False
    return False


def _resolve_executable_path(
    worktree_path: Path,
    executable: str,
    *,
    env_assignments: dict[str, str],
    shell_lookup: tuple[str, str] | None = None,
) -> Path | None:
    path = Path(executable)
    if path.is_absolute():
        return path
    if path.parent != Path("."):
        return worktree_path / path
    if shell_lookup is not None:
        return _resolve_executable_in_shell(
            worktree_path,
            executable,
            env_assignments=env_assignments,
            shell_lookup=shell_lookup,
        )
    resolved = shutil.which(
        executable,
        path=env_assignments.get("PATH", os.environ.get("PATH")),
    )
    return Path(resolved) if resolved else None


def _resolve_executable_in_shell(
    worktree_path: Path,
    executable: str,
    *,
    env_assignments: dict[str, str],
    shell_lookup: tuple[str, str],
) -> Path | None:
    shell_executable, shell_flag = shell_lookup
    resolved_shell = _resolve_executable_path(
        worktree_path,
        shell_executable,
        env_assignments=env_assignments,
    )
    if resolved_shell is None or not resolved_shell.exists():
        return None
    assignment_prefix = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in env_assignments.items()
    )
    lookup = f"command -v {shlex.quote(executable)}"
    if assignment_prefix:
        lookup = f"{assignment_prefix} {lookup}"
    try:
        completed = subprocess.run(
            [str(resolved_shell), shell_flag, lookup],
            cwd=worktree_path,
            env=_environment_with_assignments(env_assignments),
            capture_output=True,
            text=True,
            timeout=INTERPRETER_RESOLUTION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    resolved = Path(lines[0])
    if not resolved.is_absolute():
        resolved = worktree_path / resolved
    return resolved


def _pytest_shebang_interpreter(
    worktree_path: Path,
    pytest_path: Path,
    *,
    env_assignments: dict[str, str],
    shell_lookup: tuple[str, str],
) -> Path | None:
    try:
        first_line = pytest_path.read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    try:
        shebang_argv = shlex.split(first_line[2:].strip())
    except ValueError:
        return None
    if not shebang_argv:
        return None
    interpreter = shebang_argv[0]
    if Path(interpreter).name == "env":
        if len(shebang_argv) != 2 or shebang_argv[1].startswith("-"):
            return None
        interpreter = shebang_argv[1]
    return _resolve_executable_path(
        worktree_path,
        interpreter,
        env_assignments=env_assignments,
        shell_lookup=shell_lookup,
    )


def _resolve_uv_probe_interpreter(
    worktree_path: Path,
    *,
    uv_executable: str,
    uv_options: list[str],
    inner_argv: list[str],
    env_assignments: dict[str, str],
    shell_lookup: tuple[str, str] | None,
) -> Path | None:
    resolved_uv = _resolve_executable_path(
        worktree_path,
        uv_executable,
        env_assignments=env_assignments,
        shell_lookup=shell_lookup,
    )
    if resolved_uv is None or not resolved_uv.exists():
        return None
    python_executable = (
        inner_argv[0] if _is_python_executable(inner_argv[0]) else "python"
    )
    try:
        completed = subprocess.run(
            [
                str(resolved_uv),
                "run",
                *uv_options,
                python_executable,
                "-c",
                "import sys; print(sys.executable)",
            ],
            cwd=worktree_path,
            env=_environment_with_assignments(env_assignments),
            capture_output=True,
            text=True,
            timeout=INTERPRETER_RESOLUTION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    interpreter = Path(lines[0])
    if not interpreter.is_absolute():
        interpreter = worktree_path / interpreter
    return interpreter


def _interpreter_label(interpreter: Path | None) -> str:
    return str(interpreter) if interpreter is not None else UNRESOLVED_INTERPRETER


def _probe_environment(acceptance_command: str | None) -> dict[str, str] | None:
    argv, env_assignments = _acceptance_command_parts(acceptance_command)
    while argv:
        env_parts = _env_inner_parts(argv)
        if env_parts is not None:
            argv, inner_env = env_parts
            env_assignments.update(inner_env)
            continue
        shell_parts = _shell_inner_command_parts(argv)
        if shell_parts is None:
            break
        inner_command, _shell_lookup_flag = shell_parts
        argv, shell_env = _acceptance_command_parts(inner_command)
        env_assignments.update(shell_env)
    if not env_assignments:
        return None
    return _environment_with_assignments(env_assignments)


def _environment_with_assignments(env_assignments: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(env_assignments)
    return env


def write_import_provenance_artifact(
    run_dir: Path,
    result: ImportProvenanceResult,
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = run_dir / IMPORT_PROVENANCE_ARTIFACT
    artifact_path.write_text(
        json.dumps(result.to_json_dict(), indent=2),
        encoding="utf-8",
    )
    return artifact_path


def format_import_provenance_failure(result: ImportProvenanceResult) -> str:
    if result.verdict == IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION:
        return "\n".join(
            [
                "Import provenance guard could not verify acceptance imports: unknown.",
                f"Reason: {result.reason}.",
                "This is unknown, not a pass.",
                f"Worktree: {result.worktree_path}",
                f"Interpreter: {result.interpreter}",
            ]
        )

    lines = [
        "Import provenance guard refused to run acceptance: stale import bindings detected.",
        f"Remedy: {IMPORT_PROVENANCE_REMEDY}.",
        f"Reason: {result.reason}.",
        f"Worktree: {result.worktree_path}",
        f"Interpreter: {result.interpreter}",
        "Packages:",
    ]
    for package in result.packages:
        resolved_path = package.resolved_path or "<import failed>"
        line = (
            f"- {package.name}: resolved_path={resolved_path} "
            f"verdict={package.verdict}"
        )
        if package.error:
            line += f" error={package.error}"
        lines.append(line)
    return "\n".join(lines)


def _src_roots(worktree: Path) -> list[Path]:
    roots: list[Path] = []
    roots.extend(sorted((worktree / "libs").glob("*/src")))
    roots.extend(sorted((worktree / "services").glob("*/src")))
    roots.append(worktree / "src")
    return [root for root in roots if root.is_dir()]


def _add_immediate_packages(discovered: dict[str, set[Path]], src_root: Path) -> None:
    for child in sorted(src_root.iterdir()):
        _add_package(discovered, child)


def _add_package(
    discovered: dict[str, set[Path]],
    package_dir: Path,
    *,
    name: str | None = None,
) -> None:
    if not package_dir.is_dir() or not (package_dir / "__init__.py").is_file():
        return
    discovered.setdefault(name or package_dir.name, set()).add(
        package_dir.resolve(strict=False)
    )


def _declared_build_package_dirs(worktree: Path) -> list[tuple[str, Path]]:
    pyproject_path = worktree / "pyproject.toml"
    if not pyproject_path.is_file():
        return []
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    package_dirs: list[tuple[str, Path]] = []
    for value in _hatch_wheel_packages(pyproject):
        path = Path(value)
        package_dir = path if path.is_absolute() else worktree / path
        if _path_is_inside_worktree(package_dir, worktree):
            package_dirs.append((package_dir.name, package_dir))

    setuptools = _setuptools_config(pyproject)
    if setuptools is None:
        return package_dirs

    packages = setuptools.get("packages")
    if isinstance(packages, list):
        package_dir_map = _setuptools_package_dir_map(setuptools)
        for package_name in packages:
            if not isinstance(package_name, str):
                continue
            package_dir = _resolve_setuptools_package_dir(
                worktree,
                package_name,
                package_dir_map,
            )
            if package_dir is not None and _path_is_inside_worktree(
                package_dir, worktree
            ):
                package_dirs.append((package_name, package_dir))

    for search_root in _setuptools_find_roots(worktree, setuptools):
        if not search_root.is_dir():
            continue
        for child in sorted(search_root.iterdir()):
            if _path_is_inside_worktree(child, worktree):
                package_dirs.append((child.name, child))
    return package_dirs


def _path_is_inside_worktree(path: Path, worktree: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(worktree)
    except ValueError:
        return False
    return True


def _setuptools_config(pyproject: dict[str, Any]) -> dict[str, Any] | None:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return None
    setuptools = tool.get("setuptools")
    return setuptools if isinstance(setuptools, dict) else None


def _setuptools_package_dir_map(setuptools: dict[str, Any]) -> dict[str, str]:
    package_dir = setuptools.get("package-dir")
    if not isinstance(package_dir, dict):
        return {}
    return {
        name: path
        for name, path in package_dir.items()
        if isinstance(name, str) and isinstance(path, str)
    }


def _resolve_setuptools_package_dir(
    worktree: Path,
    package_name: str,
    package_dir_map: dict[str, str],
) -> Path | None:
    package_parts = package_name.split(".")
    if not package_name or any(not part for part in package_parts):
        return None

    matching_names = [
        name
        for name in package_dir_map
        if name == "" or package_name == name or package_name.startswith(f"{name}.")
    ]
    mapped_name = max(matching_names, key=len, default=None)
    if mapped_name is None:
        return worktree.joinpath(*package_parts)

    mapped_dir = Path(package_dir_map[mapped_name])
    base_dir = mapped_dir if mapped_dir.is_absolute() else worktree / mapped_dir
    if mapped_name == "":
        remainder = package_parts
    else:
        remainder = package_name.removeprefix(mapped_name).removeprefix(".").split(".")
        if remainder == [""]:
            remainder = []
    return base_dir.joinpath(*remainder)


def _setuptools_find_roots(
    worktree: Path,
    setuptools: dict[str, Any],
) -> list[Path]:
    packages = setuptools.get("packages")
    if not isinstance(packages, dict):
        return []
    find = packages.get("find")
    if not isinstance(find, dict):
        return []
    where = find.get("where")
    if not isinstance(where, list):
        return []

    roots: list[Path] = []
    for value in where:
        if not isinstance(value, str):
            continue
        path = Path(value)
        search_root = path if path.is_absolute() else worktree / path
        if _path_is_inside_worktree(search_root, worktree):
            roots.append(search_root)
    return roots


def _hatch_wheel_packages(pyproject: dict[str, Any]) -> list[str]:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return []
    hatch = tool.get("hatch")
    if not isinstance(hatch, dict):
        return []
    build = hatch.get("build")
    if not isinstance(build, dict):
        return []
    targets = build.get("targets")
    if not isinstance(targets, dict):
        return []
    wheel = targets.get("wheel")
    if not isinstance(wheel, dict):
        return []
    packages = wheel.get("packages")
    if not isinstance(packages, list):
        return []
    return [value for value in packages if isinstance(value, str)]


def _extract_probe_payload(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if not line.startswith(PROBE_STDOUT_PREFIX):
            continue
        try:
            payload = json.loads(line.removeprefix(PROBE_STDOUT_PREFIX))
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _package_results_from_payload(
    *,
    packages: list[DiscoveredPackage],
    payload: dict[str, Any],
    worktree: Path,
) -> list[PackageImportResult]:
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        raw_results = []
    by_name = {
        item.get("name"): item
        for item in raw_results
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }

    results: list[PackageImportResult] = []
    for package in packages:
        raw = by_name.get(package.name)
        if raw is None:
            results.append(
                PackageImportResult(
                    name=package.name,
                    source_paths=package.source_paths,
                    resolved_path=None,
                    inside_worktree=False,
                    verdict="import_error",
                    error="probe did not report this package",
                )
            )
            continue
        error = raw.get("error")
        if isinstance(error, str) and error:
            results.append(
                PackageImportResult(
                    name=package.name,
                    source_paths=package.source_paths,
                    resolved_path=None,
                    inside_worktree=False,
                    verdict="import_error",
                    error=error,
                )
            )
            continue
        module_file = raw.get("file")
        if not isinstance(module_file, str) or not module_file:
            results.append(
                PackageImportResult(
                    name=package.name,
                    source_paths=package.source_paths,
                    resolved_path=None,
                    inside_worktree=False,
                    verdict="import_error",
                    error="module __file__ is missing",
                )
            )
            continue
        resolved_path = Path(module_file).resolve(strict=False)
        inside_worktree = _is_inside(resolved_path, worktree)
        results.append(
            PackageImportResult(
                name=package.name,
                source_paths=package.source_paths,
                resolved_path=str(resolved_path),
                inside_worktree=inside_worktree,
                verdict="inside_worktree" if inside_worktree else "outside_worktree",
                error=None,
            )
        )
    return results


def _unknown_result(
    *,
    worktree: Path,
    interpreter: str,
    packages: list[DiscoveredPackage],
    reason: str,
) -> ImportProvenanceResult:
    return ImportProvenanceResult(
        worktree_path=str(worktree),
        interpreter=interpreter,
        verdict=IMPORT_PROVENANCE_UNKNOWN_CLASSIFICATION,
        reason=reason,
        packages=[
            PackageImportResult(
                name=package.name,
                source_paths=package.source_paths,
                resolved_path=None,
                inside_worktree=False,
                verdict="not_probed",
                error=reason,
            )
            for package in packages
        ],
    )


def _reason_with_command(reason: str, *, acceptance_command: str | None) -> str:
    command = acceptance_command if acceptance_command else "<missing>"
    return f"{reason}: {command}"


def _probe_failure_result(
    *,
    worktree: Path,
    interpreter: Path,
    packages: list[DiscoveredPackage],
    reason: str,
    error: str,
    probe_return_code: int | None = None,
    probe_stdout: str = "",
    probe_stderr: str = "",
) -> ImportProvenanceResult:
    return ImportProvenanceResult(
        worktree_path=str(worktree),
        interpreter=str(interpreter),
        verdict=IMPORT_PROVENANCE_STALE_CLASSIFICATION,
        reason=reason,
        packages=[
            PackageImportResult(
                name=package.name,
                source_paths=package.source_paths,
                resolved_path=None,
                inside_worktree=False,
                verdict="import_error",
                error=error,
            )
            for package in packages
        ],
        probe_return_code=probe_return_code,
        probe_stdout=probe_stdout,
        probe_stderr=probe_stderr,
    )


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(path), str(parent)]) == str(parent)
    except ValueError:
        return False


_PROBE_CODE = f"""
import importlib
import json
import sys

sys.dont_write_bytecode = True
package_names = json.loads(sys.stdin.read())
results = []
for package_name in package_names:
    try:
        module = importlib.import_module(package_name)
        results.append({{
            "name": package_name,
            "file": getattr(module, "__file__", None),
            "error": None,
        }})
    except BaseException as exc:
        results.append({{
            "name": package_name,
            "file": None,
            "error": f"{{type(exc).__name__}}: {{exc}}",
        }})
print({PROBE_STDOUT_PREFIX!r} + json.dumps({{"results": results}}, sort_keys=True))
"""
