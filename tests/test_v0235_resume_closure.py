"""
v0.2.3.5 RESUME FINAL SAFETY CLOSURE — tests A..H.
"""
from core.db_path import get_runs_dir
import os
import sys
import json
import subprocess
from pathlib import Path

import pytest

from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from core.git_manager import GitManager
from schemas.order import OrderSchema
from schemas.decision import DecisionSchema
from agents.mock_head import MockHead
from agents.codex_reviewer import CodexReviewer
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0233_codex_fixes import _make_link
from tests.test_v0234_resume_parity import _stuck_run, _mock_orch, _rogue


def _audit(sm, run_id):
    return [r["action"] for r in sm._get_conn().execute("SELECT action FROM audit_logs WHERE run_id=?", (run_id,)).fetchall()]


def _details(sm, run_id):
    return " | ".join((t["details"] or "") for t in sm.get_transitions(run_id))


PYTEST_CMD = "python -m pytest tests/ -q"


def _project_with_tests():
    ctx = make_git_project()
    proj = ctx.__enter__()
    tests_dir = Path(proj) / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_x.py").write_text("def test_1(): pass\ndef test_2(): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "tests"], cwd=proj, check=True, capture_output=True)
    return ctx, proj


# ------------------------------------------------------------------ TEST A: BUILDING + no baseline + modified worktree -> never re-measured

def test_a_building_without_baseline_is_not_remeasured(isolate_provider_db, tmp_path):
    ctx, proj = _project_with_tests()
    try:
        order = OrderSchema(goal="g", allowed_paths=["tests/", "src/"])
        run_id, wt, sm = _stuck_run(proj, State.BUILDING, order)
        # the builder already damaged the worktree: one test removed
        (Path(wt) / "tests" / "test_x.py").write_text("def test_1(): pass\n", encoding="utf-8")
        assert not (get_runs_dir() / run_id / "baseline_verification.json").exists()
        calls = []
        orch = _mock_orch(verification={"test_command": PYTEST_CMD})
        summary = orch.resume(run_id, builder_fn=lambda b, o, w: calls.append(w) or [])
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "BASELINE_MISSING" in _details(sm, run_id)
        assert calls == []                                                  # no further builder stage
        assert not (get_runs_dir() / run_id / "baseline_verification.json").exists()   # modified tree never became a baseline
        assert "BASELINE_MEASURED" not in _audit(sm, run_id)
    finally:
        ctx.__exit__(None, None, None)


# ------------------------------------------------------------------ TEST B: BUILDING + persisted baseline -> reused, not re-measured

def test_b_building_with_persisted_baseline_is_reused(isolate_provider_db):
    ctx, proj = _project_with_tests()
    try:
        order = OrderSchema(goal="g", allowed_paths=["tests/", "src/"])
        run_id, wt, sm = _stuck_run(proj, State.BUILDING, order)
        orch = _mock_orch(verification={"test_command": PYTEST_CMD})
        persisted = {"passed": True, "summary": "Test: PASS", "test_counts": {"passed": 2, "failed": 0, "total": 2}, "marker": "persisted"}
        orch._save_json(run_id, "baseline_verification.json", persisted)

        def adding(b, o, w):
            (Path(w) / "tests" / "test_y.py").write_text("def test_3(): pass\n", encoding="utf-8")
            return ["tests/test_y.py"]
        summary = orch.resume(run_id, builder_fn=adding)
        assert summary["final_state"] == State.PASS
        assert json.loads((get_runs_dir() / run_id / "baseline_verification.json").read_text(encoding="utf-8"))["marker"] == "persisted"
        assert "BASELINE_MEASURED" not in _audit(sm, run_id)

        # persisted baseline with fewer tests than post-build is respected as the reference (regression detection)
        run_id2, wt2, sm2 = _stuck_run(proj, State.BUILDING, order)
        orch2 = _mock_orch(verification={"test_command": PYTEST_CMD})
        orch2._save_json(run_id2, "baseline_verification.json", {"passed": True, "summary": "x", "test_counts": {"passed": 5, "failed": 0, "total": 5}})
        summary2 = orch2.resume(run_id2, builder_fn=adding)                 # post = 3 < baseline 5
        assert summary2["final_state"] == State.HUMAN_REQUIRED
        assert "Baseline test count decreased" in _details(sm2, run_id2)
    finally:
        ctx.__exit__(None, None, None)


# ------------------------------------------------------------------ TEST C: VERIFYING + no baseline -> BASELINE_MISSING

def test_c_verifying_without_baseline(isolate_provider_db):
    ctx, proj = _project_with_tests()
    try:
        run_id, wt, sm = _stuck_run(proj, State.VERIFYING, OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))
        summary = _mock_orch(verification={"test_command": PYTEST_CMD}).resume(run_id)
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "BASELINE_MISSING" in _details(sm, run_id)
        assert "VERIFICATION" not in _audit(sm, run_id)                     # no post-verification / reviewer ran
        # malformed evidence (no test_counts) is MISSING too, not "0 tests"
        run_id2, wt2, sm2 = _stuck_run(proj, State.VERIFYING, OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))
        o2 = _mock_orch(verification={"test_command": PYTEST_CMD})
        o2._save_json(run_id2, "baseline_verification.json", {"passed": True})
        assert o2.baseline_evidence(run_id2) is None
        assert o2.resume(run_id2)["final_state"] == State.HUMAN_REQUIRED
        assert "BASELINE_MISSING" in _details(sm2, run_id2)
        # a genuine measured 0 (no test command configured) IS evidence
        run_id3, wt3, sm3 = _stuck_run(proj, State.ORDER_READY, OrderSchema(goal="g", allowed_paths=["src/"]))
        o3 = _mock_orch(verification={})
        def touch(b, o, w):
            (Path(w) / "src" / "new.py").write_text("y = 2\n", encoding="utf-8"); return ["src/new.py"]
        assert o3.resume(run_id3, builder_fn=touch)["final_state"] == State.PASS
        ev = o3.baseline_evidence(run_id3)
        assert ev is not None and ev["test_counts"]["passed"] == 0
    finally:
        ctx.__exit__(None, None, None)


# ------------------------------------------------------------------ TEST D: REVIEWING + '../outside' ORDER -> reviewer never runs

def test_d_reviewing_invalid_order_blocks_reviewer(isolate_provider_db):
    class SpyReviewer:
        provider = "mock"
        calls = []
        def review(self, *a, **k):
            SpyReviewer.calls.append(1)
            from schemas.review_result import ReviewResultSchema
            return ReviewResultSchema(verdict="pass", p0=[], p1=[], p2=[], summary="ok")

    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.REVIEWING, OrderSchema(goal="g", allowed_paths=["../outside"]))
        orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                            head_agent=MockHead(), reviewer_agent=SpyReviewer())
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})
        orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 0}})
        summary = orch.resume(run_id)
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert "ORDER_PATH_VIOLATION" in _details(sm, run_id)
    assert SpyReviewer.calls == []


# ------------------------------------------------------------------ TEST E: DECIDING + symlink/junction escape -> HEAD PASS never executed/promoted

def test_e_deciding_symlink_escape(isolate_provider_db, tmp_path):
    outside = tmp_path / "outside"; outside.mkdir()
    decide_calls = []

    class PassHead(MockHead):
        def decide(self, *a, **k):
            decide_calls.append(1)
            return DecisionSchema(decision="PASS", rationale="head pass", next_steps=[])

    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.DECIDING, OrderSchema(goal="g", allowed_paths=["src/escape/"]))
        if not _make_link(Path(wt) / "src" / "escape", outside):
            pytest.skip("symlink/junction creation not permitted")
        orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                            head_agent=PassHead(), reviewer_agent=CodexReviewer(provider="mock"))
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})
        orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 0}})
        orch._save_json(run_id, "builder.json", {"status": "COMPLETED", "files_modified": [], "commands_run": [], "summary": "ok"})
        orch._save_json(run_id, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "ok"})
        summary = orch.resume(run_id)
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "ORDER_PATH_VIOLATION" in _details(sm, run_id) and "resolves outside worktree" in _details(sm, run_id)
        assert decide_calls == []

        # even if the HEAD were reached, the final gate refuses PASS on missing evidence
        run_id2, wt2, sm2 = _stuck_run(proj, State.DECIDING, OrderSchema(goal="g", allowed_paths=["src/"]))
        orch2 = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                             head_agent=PassHead(), reviewer_agent=CodexReviewer(provider="mock"))
        orch2._save_json(run_id2, "builder.json", {"status": "COMPLETED", "files_modified": [], "commands_run": [], "summary": "ok"})
        orch2._save_json(run_id2, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "ok"})
        # no baseline, no verification.json
        summary2 = orch2.resume(run_id2)
        assert summary2["final_state"] == State.HUMAN_REQUIRED
        assert "BASELINE_MISSING" in _details(sm2, run_id2) or "EVIDENCE_MISSING" in _details(sm2, run_id2)


# ------------------------------------------------------------------ TEST F: DECIDING + absolute / UNC ORDER -> HUMAN_REQUIRED

@pytest.mark.parametrize("bad", [r"C:\temp", "/tmp/foo", r"\\server\share", "//srv/x"])
def test_f_deciding_absolute_or_unc(isolate_provider_db, bad):
    class PassHead(MockHead):
        def decide(self, *a, **k):
            raise AssertionError("HEAD decide must not run for an invalid ORDER")

    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.DECIDING, OrderSchema(goal="g", allowed_paths=[bad]))
        orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                            head_agent=PassHead(), reviewer_agent=CodexReviewer(provider="mock"))
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})
        orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 0}})
        orch._save_json(run_id, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "ok"})
        summary = orch.resume(run_id)
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert "ORDER_PATH_VIOLATION" in _details(sm, run_id)


# ------------------------------------------------------------------ TEST G: healthy resume still passes

def test_g_normal_resume_still_passes(isolate_provider_db):
    ctx, proj = _project_with_tests()
    try:
        order = OrderSchema(goal="g", allowed_paths=["tests/", "src/"])
        # DECIDING with complete, consistent evidence -> PASS
        run_id, wt, sm = _stuck_run(proj, State.DECIDING, order)
        orch = _mock_orch(verification={"test_command": PYTEST_CMD})
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "summary": "Test: PASS", "test_counts": {"passed": 2, "failed": 0, "total": 2}})
        orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "summary": "Test: PASS", "test_counts": {"passed": 3, "failed": 0, "total": 3}})
        orch._save_json(run_id, "builder.json", {"status": "COMPLETED", "files_modified": ["tests/test_y.py"], "commands_run": [], "summary": "ok"})
        orch._save_json(run_id, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "clean"})
        assert orch.resume(run_id)["final_state"] == State.PASS
        assert "FINAL_GATE_BLOCK" not in _audit(sm, run_id)

        # ORDER_READY full pipeline -> PASS (baseline measured on the pristine worktree, BASELINE_MEASURED audited)
        run_id2, wt2, sm2 = _stuck_run(proj, State.ORDER_READY, order)

        def adding(b, o, w):
            (Path(w) / "tests" / "test_y.py").write_text("def test_3(): pass\n", encoding="utf-8")
            return ["tests/test_y.py"]
        summary2 = _mock_orch(verification={"test_command": PYTEST_CMD}).resume(run_id2, builder_fn=adding)
        assert summary2["final_state"] == State.PASS
        assert "BASELINE_MEASURED" in _audit(sm2, run_id2)

        # ORDER_READY but the worktree is already dirty -> cannot prove pre-build -> BASELINE_MISSING
        run_id3, wt3, sm3 = _stuck_run(proj, State.ORDER_READY, order)
        (Path(wt3) / "tests" / "test_x.py").write_text("def test_1(): pass\n", encoding="utf-8")
        summary3 = _mock_orch(verification={"test_command": PYTEST_CMD}).resume(run_id3, builder_fn=adding)
        assert summary3["final_state"] == State.HUMAN_REQUIRED
        assert "BASELINE_MISSING" in _details(sm3, run_id3)
    finally:
        ctx.__exit__(None, None, None)


# ------------------------------------------------------------------ TEST H: fresh run regression-free

def test_h_fresh_run_unchanged(isolate_provider_db):
    ctx, proj = _project_with_tests()
    try:
        orch = _mock_orch(verification={"test_command": PYTEST_CMD})

        def adding(b, o, w):
            (Path(w) / "tests" / "test_y.py").write_text("def test_3(): pass\n", encoding="utf-8")
            return ["tests/test_y.py"]
        summary = orch.run(proj, "g", builder_fn=adding, order=OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))
        assert summary["final_state"] == State.PASS
        sm = orch.sm
        seen = [t["to_state"] for t in sm.get_transitions(summary["run_id"])]
        assert seen == ["IDLE", State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.PASS]
        acts = _audit(sm, summary["run_id"])
        assert "BASELINE_MEASURED" in acts and "FINAL_GATE_BLOCK" not in acts
        ev = orch.baseline_evidence(summary["run_id"])
        assert ev and ev["test_counts"]["passed"] == 2

        # fresh run with a regression is still caught
        def deleting(b, o, w):
            (Path(w) / "tests" / "test_x.py").write_text("def test_1(): pass\n", encoding="utf-8")
            return ["tests/test_x.py"]
        summary2 = _mock_orch(verification={"test_command": PYTEST_CMD}).run(proj, "g", builder_fn=deleting, order=OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))
        assert summary2["final_state"] == State.HUMAN_REQUIRED
    finally:
        ctx.__exit__(None, None, None)
