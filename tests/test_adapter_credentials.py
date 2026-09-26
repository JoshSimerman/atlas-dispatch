from __future__ import annotations

import pytest

from atlas_dispatch.adapter import CLIS, CLIDefinition, _load_cli_credentials_env


def test_credentials_file_is_loaded_and_mapped(tmp_path) -> None:
    creds = tmp_path / "credentials.env"
    creds.write_text(
        "# comment\nUPSTREAM_BASE_URL=https://example.invalid/api\n"
        "UPSTREAM_API_KEY=sk-test\nUNRELATED=ignored\n",
        encoding="utf-8",
    )
    definition = CLIDefinition(
        name="probe",
        executable="example-cli",
        argv_template=["example-cli"],
        auth_setup_hint="",
        env_allowlist=("SERVICE_AUTH_TOKEN", "SERVICE_BASE_URL"),
        credentials_file=str(creds),
        credentials_env_map=(
            ("UPSTREAM_BASE_URL", "SERVICE_BASE_URL"),
            ("UPSTREAM_API_KEY", "SERVICE_AUTH_TOKEN"),
        ),
    )
    env, provenance = _load_cli_credentials_env(definition)
    assert env == {
        "SERVICE_BASE_URL": "https://example.invalid/api",
        "SERVICE_AUTH_TOKEN": "sk-test",
    }
    assert provenance["exists"] is True
    assert provenance["missing_source_keys"] == []
    # The provenance record names KEYS, never values -- it is written into run artifacts.
    assert "sk-test" not in str(provenance)


def test_credentials_map_outside_the_allowlist_is_refused(tmp_path) -> None:
    """The allowlist is the single statement of what may reach a CLI."""
    creds = tmp_path / "credentials.env"
    creds.write_text("UPSTREAM_API_KEY=sk-test\n", encoding="utf-8")
    definition = CLIDefinition(
        name="probe",
        executable="example-cli",
        argv_template=["example-cli"],
        auth_setup_hint="",
        env_allowlist=("SERVICE_AUTH_TOKEN",),
        credentials_file=str(creds),
        credentials_env_map=(("UPSTREAM_API_KEY", "UNSAFE_API_KEY"),),
    )
    with pytest.raises(ValueError, match="not in its env_allowlist"):
        _load_cli_credentials_env(definition)


def test_missing_credentials_file_is_not_an_error(tmp_path) -> None:
    """It surfaces later as auth_required, which carries the actionable hint."""
    definition = CLIDefinition(
        name="probe",
        executable="example-cli",
        argv_template=["example-cli"],
        auth_setup_hint="",
        env_allowlist=("SERVICE_AUTH_TOKEN",),
        credentials_file=str(tmp_path / "absent.env"),
        credentials_env_map=(("UPSTREAM_API_KEY", "SERVICE_AUTH_TOKEN"),),
    )
    env, provenance = _load_cli_credentials_env(definition)
    assert env == {}
    assert provenance["exists"] is False


def test_a_cli_without_a_credentials_file_is_unaffected() -> None:
    """MUST STILL FIRE: no registered CLI silently acquires a credentials-file env source."""
    for name, definition in sorted(CLIS.items()):
        assert not definition.credentials_file, name
        env, provenance = _load_cli_credentials_env(definition)
        assert env == {}, name
        assert provenance == {"configured": False}, name
