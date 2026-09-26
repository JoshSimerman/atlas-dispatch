"""Regression tests for adapter result classification."""

from __future__ import annotations

import pytest

from atlas_dispatch.adapter import AdapterResult, DispatchErrorKind, classify_result


def _result(
    *,
    cli: str = "codex",
    exit_code: int = 0,
    stdout: str = "done",
    stderr: str = "",
    timed_out: bool = False,
) -> AdapterResult:
    return AdapterResult(
        cli=cli,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        command=[cli],
        timed_out=timed_out,
    )


def test_refusal_text_in_stderr_does_not_override_success() -> None:
    classification = classify_result(
        _result(
            stdout="Changed adapter.py and added tests.",
            stderr=(
                '@echo "Refusing to reset unless APP_ENV=dev. '
                'Set CONFIRM_RESET=yes to proceed."'
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_refusal_text_in_stdout_is_still_refused() -> None:
    classification = classify_result(
        _result(stdout="I can't help with that request.")
    )

    assert classification.kind is DispatchErrorKind.REFUSED


def test_auth_signal_in_stderr_is_still_auth_required() -> None:
    classification = classify_result(
        _result(exit_code=1, stdout="", stderr="Error: 401 Unauthorized - please log in.")
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED


def test_rate_limit_signal_in_stderr_is_still_rate_limited() -> None:
    classification = classify_result(
        _result(exit_code=1, stdout="", stderr="HTTP 429: rate limit exceeded")
    )

    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_timeout_precedence_is_preserved() -> None:
    classification = classify_result(_result(stdout="", stderr="", timed_out=True))

    assert classification.kind is DispatchErrorKind.TIMEOUT


def test_no_output_is_preserved() -> None:
    classification = classify_result(_result(exit_code=0, stdout="", stderr=""))

    assert classification.kind is DispatchErrorKind.NO_OUTPUT


def test_executable_not_found_substring_in_stderr_does_not_fire_on_exit_zero() -> None:
    classification = classify_result(
        _result(
            exit_code=0,
            stderr='Reproducer: stderr="executable not found: codex"',
            stdout="ok",
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_auth_pattern_in_stderr_does_not_fire_on_exit_zero() -> None:
    classification = classify_result(
        _result(
            exit_code=0,
            stderr="401 Unauthorized example in echoed prompt",
            stdout="ok",
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_rate_limit_pattern_in_stderr_does_not_fire_on_exit_zero() -> None:
    classification = classify_result(
        _result(
            exit_code=0,
            stderr="HTTP 429 mentioned in test fixture",
            stdout="ok",
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_overloaded_pattern_in_stderr_does_not_fire_on_exit_zero() -> None:
    classification = classify_result(
        _result(
            exit_code=0,
            stderr="503 service unavailable mentioned",
            stdout="ok",
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_executable_not_found_still_fires_on_exit_minus_two() -> None:
    classification = classify_result(
        AdapterResult(
            cli="codex",
            exit_code=-2,
            stdout="",
            stderr="executable not found: codex",
            duration_seconds=0.1,
            command=["codex"],
            executable_not_found=True,
        )
    )

    assert classification.kind is DispatchErrorKind.EXECUTABLE_NOT_FOUND


def test_sigint_exit_code_does_not_mean_executable_not_found() -> None:
    classification = classify_result(
        _result(exit_code=-2, stdout="", stderr="child terminated by SIGINT")
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_claude_auth_pattern_matches_sdk_auth_failure() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout="",
            stderr=(
                "Error: Could not resolve authentication method. Expected either "
                "apiKey or authToken to be set."
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    assert "claude auth login" in classification.suggested_action


def test_claude_oauth_expired_classifier_matches_launchd_failure() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout="",
            stderr=(
                "OAuth token has expired. Please run /login."
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    assert classification.matched_pattern == (
        r"oauth\s+token\s+(?:has\s+)?expired"
    )
    assert "claude auth login" in classification.suggested_action


def test_claude_oauth_session_expired_verbatim_stdout_is_auth_required() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout=(
                "Failed to authenticate: OAuth session expired and could not be refreshed"
            ),
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    assert classification.kind is not DispatchErrorKind.EXIT_NONZERO
    assert classification.matched_pattern == (
        r"oauth\s+session\s+(?:has\s+)?expired"
    )


def test_claude_non_authentication_fixture_failure_remains_exit_nonzero() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout=(
                "AssertionError: 'Failed to authenticate' was not rendered in "
                "the help-text fixture"
            ),
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_claude_rate_limit_pattern_matches_api_429() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout="",
            stderr='API Error: 429 {"error":{"type":"rate_limit_error"}}',
        )
    )

    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_claude_overloaded_pattern_matches_api_529() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout="",
            stderr="API Error: 529",
        )
    )

    assert classification.kind is DispatchErrorKind.OVERLOADED


def test_claude_refusal_pattern_matches_anthropic_refusal_wording() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=0,
            stdout=(
                "I can't provide instructions that would facilitate credential theft."
            ),
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.REFUSED


def test_claude_zero_exit_invalid_model_is_model_selection_error() -> None:
    """Claude Code can print this error to stdout and still exit zero."""
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=0,
            stdout=(
                "There's an issue with the selected model (definitely-bogus). "
                "It may not exist or you may not have access to it. Run --model "
                "to pick a different model.\n"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.MODEL_SELECTION_ERROR
    assert classification.matched_pattern is not None
    assert "different model" in classification.suggested_action.lower()


def test_claude_invalid_model_fixture_echo_is_success() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=0,
            stdout=(
                "Captured fixture follows:\n"
                "> There's an issue with the selected model (definitely-bogus). "
                "It may not exist or you may not have access to it. Run --model "
                "to pick a different model.\n"
                "The classifier test should recognize that message.\n"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_claude_normal_zero_exit_output_is_success() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=0,
            stdout="Implemented the adapter change and all focused tests pass.\n",
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_claude_nonzero_invalid_model_keeps_nonzero_handling() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout=(
                "There's an issue with the selected model (definitely-bogus). "
                "It may not exist or you may not have access to it. Run --model "
                "to pick a different model.\n"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_claude_md_echo_does_not_trigger_claude_auth_patterns() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout=(
                "CLAUDE.md excerpt:\n"
                "# Anthropic project notes\n"
                "Sign in to Claude before running local examples."
            ),
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_gemini_auth_pattern_matches_code_assist_login_required() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="",
            stderr="Code Assist login required.\nAttempting to open authentication page.",
        )
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    # The `gemini` CLI entry now runs Antigravity (agy); its remedy is an
    # interactive sign-in, not a GEMINI_API_KEY export.
    assert "agy" in classification.suggested_action


def test_gemini_auth_pattern_matches_invalid_api_key_wording() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="",
            stderr=(
                "✕ [API Error: API key not valid. Please pass a valid API key. "
                "(Status: INVALID_ARGUMENT)]"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED


def test_gemini_rate_limit_pattern_matches_resource_exhausted_status() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="",
            stderr='{"error":{"status":"RESOURCE_EXHAUSTED"}}',
        )
    )

    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_gemini_overloaded_pattern_matches_unavailable_status() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="",
            stderr='{"error":{"status":"UNAVAILABLE","message":"capacity exhausted"}}',
        )
    )

    assert classification.kind is DispatchErrorKind.OVERLOADED


def test_gemini_refusal_pattern_matches_stop_conversation_wording() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=0,
            stdout="I'm not okay with this conversation, so I'll stop it here.",
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.REFUSED


def test_gemini_echoed_file_content_does_not_trigger_auth_pattern() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout=(
                "docs/auth-notes.md: Code Assist login required.\n"
                "This is fixture text, not a Gemini CLI login prompt."
            ),
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_kimi_auth_pattern_matches_invalid_authentication_error() -> None:
    classification = classify_result(
        _result(
            cli="kimi",
            exit_code=1,
            stdout="",
            stderr=(
                "Error: {'error': {'message': 'Invalid Authentication', "
                "'type': 'invalid_authentication_error'}}"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    assert "kimi login" in classification.suggested_action


def test_kimi_rate_limit_pattern_matches_max_rpm_wording() -> None:
    classification = classify_result(
        _result(
            cli="kimi",
            exit_code=75,
            stdout="",
            stderr=(
                "Error: Your account org-1<ak-1> request reached organization "
                "max RPM: 60, please try again after 10 seconds"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_kimi_overloaded_pattern_matches_server_error_type() -> None:
    classification = classify_result(
        _result(
            cli="kimi",
            exit_code=75,
            stdout="",
            stderr="server_error: Failed to extract file: upstream timeout",
        )
    )

    assert classification.kind is DispatchErrorKind.OVERLOADED


def test_kimi_refusal_pattern_matches_content_filter_wording() -> None:
    classification = classify_result(
        _result(
            cli="kimi",
            exit_code=0,
            stdout="The request was rejected because it was considered high risk",
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.REFUSED


def test_exit_zero_refusal_without_turn_status_is_still_refused() -> None:
    classification = classify_result(
        AdapterResult(
            cli="kimi",
            exit_code=0,
            stdout="I can't help with that request.",
            stderr="",
            duration_seconds=0.1,
            command=["kimi"],
            final_turn_status=None,
        )
    )

    assert classification.kind is DispatchErrorKind.REFUSED


def test_kimi_code_content_filter_400_matches_refusal() -> None:
    classification = classify_result(
        _result(
            cli="kimi-code",
            exit_code=1,
            stdout="",
            stderr=(
                "Kimi Code Error: API Error 400: request rejected by "
                "content_filter because it was considered high risk"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.REFUSED


def test_kimi_code_quota_pattern_matches() -> None:
    classification = classify_result(
        _result(
            cli="kimi-code",
            exit_code=1,
            stdout="",
            stderr=(
                "Kimi Code Error: You exceeded your current token quota, "
                "type=exceeded_current_quota_error"
            ),
        )
    )

    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_kimi_code_resume_trailer_counts_as_output() -> None:
    classification = classify_result(
        _result(
            cli="kimi-code",
            exit_code=0,
            stdout="To resume this session: kimi -r abc123\n",
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.SUCCESS


def test_kimi_echoed_file_content_does_not_trigger_failure_patterns() -> None:
    classification = classify_result(
        _result(
            cli="kimi",
            exit_code=1,
            stdout=(
                "docs/kimi-errors.md: Error: Your account org-1<ak-1> request "
                "reached organization max RPM: 60, please try again after 10 seconds"
            ),
            stderr="",
        )
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


def test_codex_usage_limit_on_failed_turn_is_rate_limited_not_failed() -> None:
    """A hard quota stop must classify as rate_limited, not the generic failure.

    Regression: a dispatch hit codex's account-wide usage limit. stdout and
    stderr were EMPTY -- the reason lived only in error_info.message -- so the
    `final_turn_status == "failed"` catch-all fired first and told the reader
    to "retry after the underlying model/runtime issue is resolved". The
    correct action is to wait for the quota reset or hand the task to a
    different CLI. This is the verbatim error_info shape the harness captured.
    """
    result = AdapterResult(
        cli="codex",
        exit_code=1,
        stdout="",
        stderr="",
        duration_seconds=3.54,
        command=["codex"],
        final_turn_status="failed",
        error_info={
            "message": (
                "You've hit your usage limit. Visit "
                "https://chatgpt.com/codex/settings/usage to purchase more "
                "credits or try again at 7:54 PM."
            ),
            "httpStatusCode": None,
            "errorCode": None,
        },
    )

    classification = classify_result(result)

    assert classification.kind == DispatchErrorKind.RATE_LIMITED
    assert "wait for the quota to reset" in classification.suggested_action.lower()


def test_codex_failed_turn_without_rate_limit_message_stays_failed() -> None:
    """A genuine turn failure must NOT be laundered into rate_limited."""
    result = AdapterResult(
        cli="codex",
        exit_code=1,
        stdout="",
        stderr="",
        duration_seconds=1.0,
        command=["codex"],
        final_turn_status="failed",
        error_info={"message": "model produced an invalid tool call"},
    )

    assert classify_result(result).kind == DispatchErrorKind.FAILED


def test_codex_overload_error_code_still_precedes_rate_limit_check() -> None:
    """-32001 overload must keep winning over the new error_info scan."""
    result = AdapterResult(
        cli="codex",
        exit_code=1,
        stdout="",
        stderr="",
        duration_seconds=1.0,
        command=["codex"],
        final_turn_status="failed",
        error_info={"message": "Server overloaded; retry later.", "errorCode": -32001},
    )

    assert classify_result(result).kind == DispatchErrorKind.OVERLOADED


@pytest.mark.parametrize(
    "error_info",
    [
        {"message": "Server overloaded; retry later.", "errorCode": -32001},
        {"message": "You've hit your usage limit. Try again later."},
    ],
)
def test_codex_clean_completed_turn_ignores_stale_error_info(
    error_info: dict[str, object],
) -> None:
    result = AdapterResult(
        cli="codex",
        exit_code=0,
        stdout="Implemented and committed the requested changes.",
        stderr="",
        duration_seconds=1.0,
        command=["codex"],
        final_turn_status="completed",
        error_info=error_info,
    )

    assert classify_result(result).kind is DispatchErrorKind.SUCCESS


def test_completed_turn_summary_idiom_is_not_a_refusal() -> None:
    result = AdapterResult(
        cli="codex",
        exit_code=0,
        stdout="I can't help noticing the tests pass after the committed fix.",
        stderr="",
        duration_seconds=1.0,
        command=["codex"],
        final_turn_status="completed",
    )

    assert classify_result(result).kind is DispatchErrorKind.SUCCESS


@pytest.mark.parametrize("stderr", ["traceback at line 4291", "service on port 8503"])
def test_hermes_incidental_digit_runs_are_exit_nonzero(stderr: str) -> None:
    classification = classify_result(
        _result(cli="hermes", exit_code=1, stdout="", stderr=stderr)
    )

    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 429", DispatchErrorKind.RATE_LIMITED),
        ("HTTP 503", DispatchErrorKind.OVERLOADED),
    ],
)
def test_hermes_standalone_limit_status_tokens_are_classified(
    stderr: str,
    expected: DispatchErrorKind,
) -> None:
    classification = classify_result(
        _result(cli="hermes", exit_code=1, stdout="", stderr=stderr)
    )

    assert classification.kind is expected
