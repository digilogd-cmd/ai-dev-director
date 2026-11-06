"""
v3.0-L ZERO WRITE-BUDGET ENTRY GATE — an ORDER that provably cannot change any file
(max_files_changed == 0 and max_diff_lines == 0, no allowed_deletions) must not be blocked
at ENTRY for AMBIGUOUS_SCOPE just because allowed_paths is empty or whole-repo: the
post-build cap (validate_scope_post_build) already enforces zero changes independently,
so there is nothing for a human to bless. Every other ENTRY gate (HIGH_RISK, protected/
sensitive paths, destructive change) must still fire exactly as before.
"""
from core.safety import SafetyPolicy
from schemas.order import OrderSchema


def _ro_order(**over):
    base = dict(goal="investigate only", allowed_paths=[], protected_paths=["**"],
                max_files_changed=0, max_diff_lines=0, allowed_deletions=[])
    base.update(over)
    return OrderSchema(**base)


def test_zero_write_budget_empty_scope_is_auto_runnable():
    assert SafetyPolicy.assess_order_for_auto_run(_ro_order()) == []


def test_zero_write_budget_whole_repo_scope_is_not_ambiguous_but_stays_protected():
    # The exemption narrows AMBIGUOUS_SCOPE only; every other gate must still fire (see the module docstring).
    reasons = SafetyPolicy.assess_order_for_auto_run(_ro_order(allowed_paths=["."]))
    assert not any(r.startswith("AMBIGUOUS_SCOPE") for r in reasons)
    assert any(r.startswith("PROTECTED_PATH_IN_SCOPE") for r in reasons)


def test_nonzero_write_budget_empty_scope_still_ambiguous():
    reasons = SafetyPolicy.assess_order_for_auto_run(_ro_order(max_files_changed=1))
    assert any(r.startswith("AMBIGUOUS_SCOPE") for r in reasons)


def test_zero_write_budget_but_deletions_present_still_ambiguous():
    reasons = SafetyPolicy.assess_order_for_auto_run(_ro_order(allowed_deletions=["old_func"]))
    assert any(r.startswith("AMBIGUOUS_SCOPE") for r in reasons)
    assert any(r.startswith("DESTRUCTIVE_CHANGE") for r in reasons)


def test_zero_write_budget_does_not_suppress_high_risk():
    reasons = SafetyPolicy.assess_order_for_auto_run(_ro_order(risk_level="HIGH"))
    assert reasons == ["HIGH_RISK"]


def test_zero_write_budget_does_not_suppress_explicit_human_approval():
    reasons = SafetyPolicy.assess_order_for_auto_run(_ro_order(requires_human_approval=True))
    assert reasons == ["EXPLICIT_HUMAN_APPROVAL_REQUESTED"]
