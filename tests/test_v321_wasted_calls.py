"""v3.2.1 (2026-11-28) - calls that bought nothing, found by running the same real mission on two role line-ups:
a green milestone lost to the word "delete" in a minor note, and a provider that could not run its model being
called again on every milestone because its error text was cut off before the error."""
import logging

import pytest

from core.env_utils import cli_error_excerpt
from core.provider_manager import ErrorCategory
from core.state_machine import State
from schemas.decision import DecisionSchema
from schemas.review_result import ReviewResultSchema
from tests.test_v306_head_proxy_autonomy import proxy_orch

pytestmark = pytest.mark.head_proxy
LOG = logging.getLogger("test")

# what Codex really printed: banner, the echoed prompt (tens of thousands of characters), and the error last
CODEX_STDERR = ("OpenAI Codex v0.167.0-alpha.3.2\n--------\nworkdir: C:\\tmp\\director-builder-x\nmodel: gpt-x\n"
                "provider: openai\napproval: never\nsandbox: read-only\n--------\nuser\n"
                + "You are the implementation Builder. Do not use tools. " * 400 + "\n"
                'ERROR: {"type":"error","status":400,"error":{"type":"invalid_request_error","message":"The \'gpt-x\' model '
                'is not supported when using Codex with a ChatGPT account.","code":"model_not_found"}}\n')


def test_the_error_line_survives_every_truncation():
    excerpt = cli_error_excerpt(CODEX_STDERR, "")
    assert len(excerpt) <= 600 and "model is not supported" in excerpt
    assert ErrorCategory.classify(CODEX_STDERR[:500]) == ErrorCategory.UNKNOWN            # the old head-only cut
    for wrapped in ("OPENAI CLI exit 1: " + cli_error_excerpt(CODEX_STDERR, "", 500),
                    ("BUILDER_EXCEPTION: OPENAI CLI exit 1: " + cli_error_excerpt(CODEX_STDERR, "", 500))[:800],
                    ("Codex CLI execution failed (exit 1): " + excerpt)[:800]):
        assert ErrorCategory.classify(wrapped) == ErrorCategory.PROVIDER_UNAVAILABLE, wrapped[:80]


def test_usage_limit_at_the_end_is_still_a_quota_error():
    text = "banner\nuser\n" + "prompt text " * 3000 + "\nERROR: You've hit your usage limit. Try again at 3:10 PM."
    assert ErrorCategory.classify("OPENAI CLI exit 1: " + cli_error_excerpt(text, "", 500)) == ErrorCategory.QUOTA


def test_excerpt_of_empty_or_plain_output():
    assert cli_error_excerpt("", "") == "no output"
    assert cli_error_excerpt("", "something odd happened") == "something odd happened"


def gate(p1=(), p2=()):
    orch = proxy_orch()
    decision = DecisionSchema(decision="PASS", rationale="tests green")
    review = ReviewResultSchema(verdict="pass", p0=[], p1=list(p1), p2=list(p2), summary="ok")
    return orch.apply_final_pass_gate(decision, review, LOG), decision


def test_minor_note_about_a_delete_command_does_not_fail_a_green_milestone():
    state, decision = gate(p2=["ledger/db.py `delete_transaction`: a non-integer id raises TypeError instead of ValueError.",
                               "tests/test_cli.py: the missing-id delete exit code is checked only through main().",
                               "cli.py: `remove` alias is undocumented."])
    assert state == State.PASS and "Overridden" not in decision.rationale


def test_a_p1_finding_still_blocks():
    state, decision = gate(p1=["The diff deletes validate_amount(), which other modules still import."])
    assert state == State.FAIL and "Overridden by Orchestrator" in decision.rationale
