"""Characterisation tests for classify_result's check ORDER and rarely-hit branches.

docs/FAILURE_MODES.md documents classify_result as an ordered list: the first
matching condition wins. These tests pin each adjacent pair of that order (an
input that satisfies both checks must land on the earlier one), plus branches
the per-CLI regression tests do not reach: an unregistered CLI (no definition,
generic patterns only), a quota stop reported only in a failed turn's
error_info, an interrupted turn, and the bounded model-selection scan.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlas_dispatch.adapter import (
    MAX_CLASSIFY_HAYSTACK,
    AdapterResult,
    DispatchErrorKind,
    QuotaResetWindowProvenance,
    classify_result,
)

GEMINI_QUOTA_BANNER = (
    "Individual quota reached. Please upgrade your subscription to increase "
    "your limits. Resets in 1h 2m 3s."
)
CLAUDE_SESSION_LIMIT = "You've hit your session limit · resets 4pm (America/Chicago)"
CLAUDE_MODEL_SELECTION_ERROR = (
    "There's an issue with the selected model (claude-nope). It may not exist "
    "or you may not have access to it. Run --model to pick a different model."
)
SCOPE_REFUSAL = (
    "I cannot complete the task because the required change to settings.py "
    "is outside the allowed paths."
)


def _result(
    *,
    cli: str = "codex",
    exit_code: int = 0,
    stdout: str = "done",
    stderr: str = "",
    timed_out: bool = False,
    executable_not_found: bool = False,
    idle_classification: str | None = None,
    final_turn_status: str | None = None,
    error_info: dict[str, Any] | None = None,
    produced_expected_deliverable: bool = False,
) -> AdapterResult:
    return AdapterResult(
        cli=cli,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        command=[cli],
        timed_out=timed_out,
        executable_not_found=executable_not_found,
        idle_classification=idle_classification,
        final_turn_status=final_turn_status,
        error_info=error_info,
        produced_expected_deliverable=produced_expected_deliverable,
    )


# --------------------------------------------------------------------------- #
# Adjacent-pair precedence, in documented order                               #
# --------------------------------------------------------------------------- #


def test_approval_blocked_precedes_every_other_signal() -> None:
    classification = classify_result(
        _result(
            exit_code=1,
            stderr="429 too many requests",
            timed_out=True,
            idle_classification="approval_blocked",
            final_turn_status="failed",
            error_info={"message": "usage limit", "errorCode": "-32001"},
        )
    )
    assert classification.kind is DispatchErrorKind.APPROVAL_BLOCKED


def test_stalled_precedes_codex_overload_and_timeout() -> None:
    classification = classify_result(
        _result(
            exit_code=1,
            timed_out=True,
            idle_classification="stalled",
            error_info={"message": "server busy", "errorCode": "-32001"},
        )
    )
    assert classification.kind is DispatchErrorKind.STALLED


def test_codex_overload_code_precedes_quota_message_in_error_info() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            final_turn_status="failed",
            error_info={"message": GEMINI_QUOTA_BANNER, "errorCode": -32001},
        )
    )
    assert classification.kind is DispatchErrorKind.OVERLOADED


def test_quota_in_failed_turn_error_info_is_quota_exhausted_not_failed() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="",
            final_turn_status="failed",
            error_info={"message": GEMINI_QUOTA_BANNER},
        )
    )
    assert classification.kind is DispatchErrorKind.QUOTA_EXHAUSTED
    assert classification.quota_reset_window == "1h 2m 3s"
    assert (
        classification.quota_reset_window_provenance
        is QuotaResetWindowProvenance.VENDOR_DECLARED
    )
    assert classification.matched_pattern is not None
    assert "gemini" in classification.suggested_action


def test_error_info_quota_is_ignored_for_a_clean_completed_turn() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=0,
            final_turn_status="completed",
            error_info={"message": GEMINI_QUOTA_BANNER},
        )
    )
    assert classification.kind is DispatchErrorKind.SUCCESS


def test_error_info_quota_precedes_output_rate_limit_text() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stderr="429 too many requests",
            final_turn_status="failed",
            error_info={"message": GEMINI_QUOTA_BANNER},
        )
    )
    assert classification.kind is DispatchErrorKind.QUOTA_EXHAUSTED


def test_error_info_rate_limit_precedes_output_quota_banner() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout=GEMINI_QUOTA_BANNER,
            final_turn_status="failed",
            error_info={"message": "rate limit reached for this model"},
        )
    )
    assert classification.kind is DispatchErrorKind.RATE_LIMITED
    assert "usage/rate limit" in classification.suggested_action


def test_output_quota_banner_precedes_failed_turn_and_timeout() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            exit_code=1,
            stdout=CLAUDE_SESSION_LIMIT,
            timed_out=True,
            final_turn_status="failed",
        )
    )
    assert classification.kind is DispatchErrorKind.QUOTA_EXHAUSTED
    assert classification.quota_reset_window == "4pm (America/Chicago)"


def test_exit_zero_quota_banner_must_be_the_final_line() -> None:
    last_line = classify_result(
        _result(cli="claude", stdout=f"working...\n{CLAUDE_SESSION_LIMIT}\n")
    )
    quoted = classify_result(
        _result(cli="claude", stdout=f"{CLAUDE_SESSION_LIMIT}\nthen I finished.")
    )
    assert last_line.kind is DispatchErrorKind.QUOTA_EXHAUSTED
    assert quoted.kind is DispatchErrorKind.SUCCESS


def test_failed_turn_precedes_interrupted_timeout_and_patterns() -> None:
    classification = classify_result(
        _result(
            exit_code=1,
            stderr="401 unauthorized",
            timed_out=True,
            final_turn_status="failed",
        )
    )
    assert classification.kind is DispatchErrorKind.FAILED


def test_interrupted_turn_is_classified_interrupted() -> None:
    classification = classify_result(
        _result(exit_code=0, stdout="", final_turn_status="interrupted")
    )
    assert classification.kind is DispatchErrorKind.INTERRUPTED
    assert "interrupted" in classification.suggested_action


def test_interrupted_turn_precedes_timeout() -> None:
    classification = classify_result(
        _result(exit_code=1, timed_out=True, final_turn_status="interrupted")
    )
    assert classification.kind is DispatchErrorKind.INTERRUPTED


def test_timeout_precedes_executable_not_found_and_auth() -> None:
    classification = classify_result(
        _result(
            exit_code=-2,
            stderr="401 unauthorized",
            timed_out=True,
            executable_not_found=True,
        )
    )
    assert classification.kind is DispatchErrorKind.TIMEOUT
    assert "codex" in classification.suggested_action


def test_executable_not_found_precedes_auth_patterns() -> None:
    classification = classify_result(
        _result(exit_code=-2, stderr="401 unauthorized", executable_not_found=True)
    )
    assert classification.kind is DispatchErrorKind.EXECUTABLE_NOT_FOUND


def test_executable_not_found_names_the_registered_binary_and_override() -> None:
    classification = classify_result(
        _result(cli="gemini", exit_code=-2, executable_not_found=True)
    )
    assert classification.kind is DispatchErrorKind.EXECUTABLE_NOT_FOUND
    assert "`agy` is not on PATH" in classification.suggested_action
    assert "ATLAS_DISPATCH_GEMINI_CMD" in classification.suggested_action


def test_auth_precedes_rate_limit_precedes_overload_on_nonzero_exit() -> None:
    auth = classify_result(_result(exit_code=1, stderr="401; 429; 503"))
    rate = classify_result(_result(exit_code=1, stderr="429; 503"))
    overload = classify_result(_result(exit_code=1, stderr="503"))
    assert auth.kind is DispatchErrorKind.AUTH_REQUIRED
    assert auth.suggested_action == "Run `codex login` to refresh credentials."
    assert rate.kind is DispatchErrorKind.RATE_LIMITED
    assert "rate-limit signal" in rate.suggested_action
    assert overload.kind is DispatchErrorKind.OVERLOADED
    assert overload.matched_pattern == r"\b503\b"


def test_overload_precedes_scope_refusal_on_nonzero_exit() -> None:
    classification = classify_result(
        _result(exit_code=1, stdout=SCOPE_REFUSAL, stderr="503 service unavailable")
    )
    assert classification.kind is DispatchErrorKind.OVERLOADED


def test_scope_refusal_precedes_generic_refusal() -> None:
    classification = classify_result(
        _result(stdout=f"I can't help with that.\n{SCOPE_REFUSAL}")
    )
    assert classification.kind is DispatchErrorKind.REFUSED_OUT_OF_SCOPE
    assert SCOPE_REFUSAL in classification.suggested_action
    assert classification.matched_pattern is not None


def test_scope_refusal_fires_even_for_a_completed_turn() -> None:
    classification = classify_result(
        _result(stdout=SCOPE_REFUSAL, final_turn_status="completed")
    )
    assert classification.kind is DispatchErrorKind.REFUSED_OUT_OF_SCOPE


def test_nonzero_refusal_reads_stderr_but_exit_zero_refusal_reads_stdout_only() -> None:
    nonzero = classify_result(
        _result(exit_code=1, stdout="", stderr="I can't help with that.")
    )
    zero = classify_result(_result(exit_code=0, stderr="I can't help with that."))
    assert nonzero.kind is DispatchErrorKind.REFUSED
    assert zero.kind is DispatchErrorKind.SUCCESS


def test_refusal_precedes_post_completion_timeout_success() -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="I can't help with that.\nerror: timeout waiting for response",
            produced_expected_deliverable=True,
        )
    )
    assert classification.kind is DispatchErrorKind.REFUSED


@pytest.mark.parametrize(
    ("produced", "expected"),
    [(True, DispatchErrorKind.SUCCESS), (False, DispatchErrorKind.EXIT_NONZERO)],
)
def test_post_completion_timeout_needs_the_deliverable(
    produced: bool, expected: DispatchErrorKind
) -> None:
    classification = classify_result(
        _result(
            cli="gemini",
            exit_code=1,
            stdout="wrote review.md\nerror: timeout waiting for response",
            produced_expected_deliverable=produced,
        )
    )
    assert classification.kind is expected
    if produced:
        assert classification.observed_exit_code == 1
        assert "`agy` exited with code 1" in classification.suggested_action
    else:
        assert classification.observed_exit_code is None


def test_nonzero_model_selection_text_is_exit_nonzero() -> None:
    classification = classify_result(
        _result(cli="claude", exit_code=1, stdout=CLAUDE_MODEL_SELECTION_ERROR)
    )
    assert classification.kind is DispatchErrorKind.EXIT_NONZERO
    assert "cli.stderr.txt" in classification.suggested_action


def test_model_selection_error_precedes_completed_turn_success() -> None:
    classification = classify_result(
        _result(
            cli="claude",
            stdout=CLAUDE_MODEL_SELECTION_ERROR,
            final_turn_status="completed",
        )
    )
    assert classification.kind is DispatchErrorKind.MODEL_SELECTION_ERROR


def test_model_selection_scan_is_skipped_for_oversized_stdout() -> None:
    classification = classify_result(
        _result(cli="claude", stdout="x" * (MAX_CLASSIFY_HAYSTACK + 1))
    )
    assert classification.kind is DispatchErrorKind.SUCCESS


def test_completed_turn_with_empty_stdout_is_success_not_no_output() -> None:
    classification = classify_result(
        _result(stdout="", final_turn_status="completed")
    )
    assert classification.kind is DispatchErrorKind.SUCCESS
    assert classification.suggested_action == ""


def test_whitespace_only_stdout_is_no_output() -> None:
    classification = classify_result(_result(stdout="  \n\t"))
    assert classification.kind is DispatchErrorKind.NO_OUTPUT


def test_signals_beyond_the_tail_window_are_not_seen() -> None:
    """The scan is tail-biased: text before the last MAX_CLASSIFY_HAYSTACK
    characters of stderr + stdout is not classified."""
    classification = classify_result(
        _result(
            exit_code=1,
            stderr="401 unauthorized\n",
            stdout="x" * MAX_CLASSIFY_HAYSTACK,
        )
    )
    assert classification.kind is DispatchErrorKind.EXIT_NONZERO


# --------------------------------------------------------------------------- #
# Unregistered CLI: generic patterns only                                     #
# --------------------------------------------------------------------------- #


def test_unregistered_cli_auth_uses_generic_hint() -> None:
    classification = classify_result(
        _result(cli="MyCLI", exit_code=1, stderr="Error: not logged in")
    )
    assert classification.kind is DispatchErrorKind.AUTH_REQUIRED
    assert (
        classification.suggested_action
        == "Refresh authentication for `mycli` and try again."
    )


def test_unregistered_cli_quota_wording_is_generic_rate_limit() -> None:
    classification = classify_result(
        _result(cli="mycli", exit_code=1, stderr=GEMINI_QUOTA_BANNER + " usage limit")
    )
    assert classification.kind is DispatchErrorKind.RATE_LIMITED


def test_unregistered_cli_ignores_vendor_specific_patterns() -> None:
    classification = classify_result(
        _result(cli="mycli", exit_code=1, stderr="API Error: 529 overloaded_error")
    )
    # Generic "overloaded" still matches; the claude-only 529 pattern does not.
    assert classification.kind is DispatchErrorKind.OVERLOADED
    assert classification.matched_pattern == "overloaded"


def test_unregistered_cli_executable_not_found_names_cli_and_override() -> None:
    classification = classify_result(
        _result(cli="my-cli", exit_code=-2, executable_not_found=True)
    )
    assert classification.kind is DispatchErrorKind.EXECUTABLE_NOT_FOUND
    assert "`my-cli` is not on PATH" in classification.suggested_action
    assert "ATLAS_DISPATCH_MY_CLI_CMD" in classification.suggested_action


def test_unregistered_cli_generic_refusal_and_exit_nonzero() -> None:
    refused = classify_result(
        _result(cli="mycli", exit_code=0, stdout="I won't help with that.")
    )
    failed = classify_result(_result(cli="mycli", exit_code=3, stderr="boom"))
    assert refused.kind is DispatchErrorKind.REFUSED
    assert failed.kind is DispatchErrorKind.EXIT_NONZERO
    assert "`mycli` exited with code 3" in failed.suggested_action


def test_unregistered_cli_never_reports_model_selection_or_post_completion() -> None:
    selection = classify_result(
        _result(cli="mycli", stdout=CLAUDE_MODEL_SELECTION_ERROR)
    )
    post_completion = classify_result(
        _result(
            cli="mycli",
            exit_code=1,
            stdout="error: timeout waiting for response",
            produced_expected_deliverable=True,
        )
    )
    assert selection.kind is DispatchErrorKind.SUCCESS
    assert post_completion.kind is DispatchErrorKind.EXIT_NONZERO
