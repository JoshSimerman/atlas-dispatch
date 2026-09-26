"""`verify_passed=False` must not mean two different things.

WHY THIS EXISTS. A build can be killed at its timeout while its test suite is
still running. The chain is:

    classification=timeout -> acceptance SKIPPED -> verify_passed=False

and a report whose only headline is `- Verify passed: **False**` invites the
reading "it failed, start over", which would discard a coherent change that has
never been shown to be wrong. Nothing has been measured about the code at all.

It is the familiar exit-code collapse: "found problems" and "could not
measure" reported as the same value.

The consumer is what makes this instance the expensive one. A wrong exit code
misleads a pager; a wrong `verify_passed` misleads a REVIEWER deciding whether to
throw work away.

The boolean is deliberately UNCHANGED — things gate on it. What is fixed is that
the report no longer states it without saying which fact it means.
"""

from __future__ import annotations

from atlas_dispatch.dispatcher import (
    VERIFY_STATE_FAIL,
    VERIFY_STATE_NOT_ATTEMPTED,
    VERIFY_STATE_PASS,
    verification_verdict,
)
from atlas_dispatch.verify import CheckResult, VerifyReport


def _check(passed: bool) -> CheckResult:
    return CheckResult(
        name="python -m pytest -q",
        passed=passed,
        classification="success" if passed else "test_failure",
    )


def _report(**kw) -> VerifyReport:
    return VerifyReport(changed_files_present=True, allowlist_passed=True, **kw)


# --------------------------------------------------------------------------
# The distinction this module is about.
# --------------------------------------------------------------------------

def test_a_skipped_acceptance_is_NOT_ATTEMPTED_not_a_failure() -> None:
    """The timeout case: the run died before acceptance could run."""
    state, reason = verification_verdict(
        _report(command_results=[], acceptance_skip_reason=["cli_failed"]),
        ref_guard_passed=True,
    )
    assert state == VERIFY_STATE_NOT_ATTEMPTED
    assert state != VERIFY_STATE_FAIL
    assert "COULD NOT CHECK" in reason
    assert "NO evidence" in reason


def test_the_skip_reason_is_carried_into_the_verdict() -> None:
    """A reviewer must not have to open another file to learn WHY."""
    _, reason = verification_verdict(
        _report(command_results=[], acceptance_skip_reason=["cli_failed", "classification=timeout"]),
        ref_guard_passed=True,
    )
    assert "cli_failed" in reason and "classification=timeout" in reason


def test_a_real_acceptance_failure_IS_a_verdict() -> None:
    """The other side: this one genuinely says something about the code."""
    state, reason = verification_verdict(
        _report(command_results=[_check(False)]), ref_guard_passed=True
    )
    assert state == VERIFY_STATE_FAIL
    assert "IS a verdict" in reason


def test_passing_acceptance_with_a_clean_ref_guard_is_a_pass() -> None:
    state, _ = verification_verdict(
        _report(command_results=[_check(True)]), ref_guard_passed=True
    )
    assert state == VERIFY_STATE_PASS


# --------------------------------------------------------------------------
# The guards that stop the fix becoming a new way to be wrong.
# --------------------------------------------------------------------------

def test_NOT_ATTEMPTED_is_never_reported_as_a_pass() -> None:
    """The failure mode in the other direction, and the worse one.

    Refusing to call a skipped run FAILED must not make it look VERIFIED.
    Nothing was measured; that is neither outcome.
    """
    state, _ = verification_verdict(
        _report(command_results=[], acceptance_skip_reason=["cli_failed"]),
        ref_guard_passed=True,
    )
    assert state != VERIFY_STATE_PASS


def test_a_moved_ref_fails_even_when_acceptance_passed() -> None:
    """A worktree isolates files, not git refs. Green tests do not excuse it."""
    state, reason = verification_verdict(
        _report(command_results=[_check(True)]), ref_guard_passed=False
    )
    assert state == VERIFY_STATE_FAIL
    assert "ref outside the worktree" in reason


def test_a_moved_ref_does_not_upgrade_a_skipped_run_into_a_failure() -> None:
    """Ordering: NOT_ATTEMPTED is decided before the ref guard, because a run that
    measured nothing still measured nothing."""
    state, _ = verification_verdict(
        _report(command_results=[], acceptance_skip_reason=["cli_failed"]),
        ref_guard_passed=False,
    )
    assert state == VERIFY_STATE_NOT_ATTEMPTED


def test_missing_skip_reason_still_reads_as_not_attempted() -> None:
    """An absent reason is less information, not a different state."""
    state, reason = verification_verdict(
        _report(command_results=[], acceptance_skip_reason=None), ref_guard_passed=True
    )
    assert state == VERIFY_STATE_NOT_ATTEMPTED
    assert "acceptance did not run" in reason


def test_the_three_states_are_distinct_strings() -> None:
    """A tripwire: collapsing two of these back into one constant is the bug."""
    assert len({VERIFY_STATE_PASS, VERIFY_STATE_FAIL, VERIFY_STATE_NOT_ATTEMPTED}) == 3


def test_a_no_op_run_is_described_as_nothing_built_not_as_unverified_code() -> None:
    """Do not flatten the two ways nothing got verified.

    A CLI that produced no changes has no code to verify; a CLI killed mid-suite
    has code nobody checked. Both are NOT_ATTEMPTED, but telling a reviewer "no
    evidence about the code" when there IS no code is the same collapse one level
    down. verify.VerificationState keeps the same distinction (NOT_RUN_NO_CHANGE).
    """
    state, reason = verification_verdict(
        VerifyReport(
            changed_files_present=False,
            allowlist_passed=True,
            command_results=[],
            acceptance_skip_reason=["no_changes"],
        ),
        ref_guard_passed=True,
    )
    assert state == VERIFY_STATE_NOT_ATTEMPTED
    assert "nothing to verify" in reason
    assert "NOTHING WAS BUILT" in reason
    assert "NO evidence" not in reason, "that phrasing is for a killed build, not a no-op"


def test_an_empty_diff_beside_a_moved_ref_is_not_described_as_a_no_op() -> None:
    """An agent that merges its own branch leaves an empty diff and a moved ref.

    Saying "nothing was built" there would be false: the work exists, on main.
    """
    state, reason = verification_verdict(
        VerifyReport(
            changed_files_present=False,
            allowlist_passed=True,
            command_results=[],
            acceptance_skip_reason=["no_files_changed"],
        ),
        ref_guard_passed=False,
    )
    assert state == VERIFY_STATE_NOT_ATTEMPTED
    assert "NOTHING WAS BUILT" not in reason
    assert "merged elsewhere" in reason


def test_a_protected_path_fails_the_verdict_even_when_acceptance_passed() -> None:
    """The headline must agree with verify_passed: the floor beats a green suite."""
    report = VerifyReport(
        changed_files_present=True,
        allowlist_passed=True,
        protected_path_violations=["migrations/0001_init.sql"],
        command_results=[_check(True)],
    )
    state, reason = verification_verdict(report, ref_guard_passed=True)

    assert report.passed is False
    assert state == VERIFY_STATE_FAIL
    assert "protected path" in reason
