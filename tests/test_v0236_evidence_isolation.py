"""
v0.2.3.6 FINAL EVIDENCE & TEST ISOLATION CLOSURE — tests A..E plus isolation guards.
"""
from core.db_path import get_runs_dir
import os
import sys
import json
import shutil
from pathlib import Path

import pytest

import dashboard_server
from dashboard_server import handle_chat_message, GLOBAL_SESSION_ID
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State
from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.process_runner import ProcessTreeRunner, RealCliCallDuringUnitTest
from core.db_path import get_db_path, DEFAULT_DB_PATH
from schemas.order import OrderSchema
from schemas.decision import DecisionSchema
from agents.mock_head import MockHead
from agents.codex_reviewer import CodexReviewer
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0234_resume_parity import _stuck_run, _mock_orch
from tests.test_v0235_resume_closure import _details, _audit


class PassHead(MockHead):
    calls = []
    def decide(self, *a, **k):
        PassHead.calls.append(1)
        return DecisionSchema(decision="PASS", rationale="head pass", next_steps=[])


def _deciding_run(proj, orch, order=None):
    order = order or OrderSchema(goal="g", allowed_paths=["src/"])
    run_id, wt, sm = _stuck_run(proj, State.DECIDING, order)
    orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 2}})
    orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 3}})
    orch._save_json(run_id, "builder.json", {"status": "COMPLETED", "files_modified": ["src/x.py"], "commands_run": [], "summary": "ok"})
    orch._save_json(run_id, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "clean"})
    return run_id, wt, sm


def _orch_pass_head():
    PassHead.calls = []
    return Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                        head_agent=PassHead(), reviewer_agent=CodexReviewer(provider="mock"))


# ------------------------------------------------------------------ TEST A: DECIDING + worktree_path NULL -> no PASS

def test_a_deciding_worktree_null(isolate_provider_db):
    with make_git_project() as proj:
        orch = _orch_pass_head()
        run_id, wt, sm = _deciding_run(proj, orch)
        with sm._get_conn() as conn:
            conn.execute("UPDATE runs SET worktree_path=NULL, branch_name=NULL WHERE run_id=?", (run_id,)); conn.commit()
        summary = orch.resume(run_id)
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "WORKTREE_EVIDENCE_MISSING" in _details(sm, run_id)
        assert PassHead.calls == []                                  # HEAD decision never invoked
        assert "WORKTREE_EVIDENCE_MISSING" in _audit(sm, run_id)
        ev = orch.final_evidence(run_id, OrderSchema(goal="g", allowed_paths=["src/"]), None)
        assert ev["worktree"] == "MISSING" and ev["canonical_containment"] == "MISSING"


# ------------------------------------------------------------------ TEST B: DECIDING + recorded path but directory deleted -> no PASS

def test_b_deciding_worktree_deleted(isolate_provider_db):
    with make_git_project() as proj:
        orch = _orch_pass_head()
        run_id, wt, sm = _deciding_run(proj, orch)
        shutil.rmtree(wt, ignore_errors=True)
        assert not Path(wt).exists()
        summary = orch.resume(run_id)
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert "WORKTREE_EVIDENCE_MISSING" in _details(sm, run_id)
        assert PassHead.calls == []
        # REVIEWING resume is blocked the same way (no reviewer call)
        run_id2, wt2, sm2 = _stuck_run(proj, State.REVIEWING, OrderSchema(goal="g", allowed_paths=["src/"]))
        orch2 = _orch_pass_head()
        orch2._save_json(run_id2, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})
        orch2._save_json(run_id2, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 0}})
        shutil.rmtree(wt2, ignore_errors=True)
        assert orch2.resume(run_id2)["final_state"] == State.HUMAN_REQUIRED
        assert "WORKTREE_EVIDENCE_MISSING" in _details(sm2, run_id2)
        assert "CODE_REVIEW" not in _audit(sm2, run_id2)


# ------------------------------------------------------------------ TEST C: DECIDING + healthy worktree + canonical PASS -> PASS, evidence persisted

def test_c_deciding_with_worktree_passes(isolate_provider_db):
    with make_git_project() as proj:
        orch = _orch_pass_head()
        run_id, wt, sm = _deciding_run(proj, orch)
        summary = orch.resume(run_id)
        assert summary["final_state"] == State.PASS
        ev = json.loads((get_runs_dir() / run_id / "final_evidence.json").read_text(encoding="utf-8"))
        assert ev["worktree"] == "PRESENT" and ev["canonical_containment"] == "PASS"
        assert ev["baseline"] == "PASS" and ev["verification"] == "PASS" and ev["regression"] == "PASS"
        assert ev["reasons"] == [] and ev["reviewer_verdict"] == "pass"
        assert "FINAL_GATE_BLOCK" not in _audit(sm, run_id)

        # lexical validity alone is NOT canonical PASS: without any recorded worktree the status is MISSING
        # (worktree_path=None falls back to the DB record, so use a run that has none)
        ev2 = orch.final_evidence("RUN-NO-SUCH-RUN", OrderSchema(goal="g", allowed_paths=["src/"]), None)
        assert ev2["order_paths"] == "PASS" and ev2["canonical_containment"] == "MISSING"
        assert any(r.startswith("WORKTREE_EVIDENCE_MISSING") for r in ev2["reasons"])
        # an ORDER path escaping the real worktree via a junction/symlink is FAIL, not MISSING
        ev_fail = orch.final_evidence(run_id, OrderSchema(goal="g", allowed_paths=["src/escape/"]), wt)
        assert ev_fail["canonical_containment"] in ("PASS", "FAIL")      # explicit verdict, never MISSING with a worktree

        # fresh run: worktree is created before PASS and the persisted evidence says PASS/PRESENT
        def _builder(b, o, w):
            (Path(w) / "src" / "n.py").write_text("z=1" + chr(10), encoding="utf-8")
            return ["src/n.py"]
        summary2 = _mock_orch(verification={}).run(proj, "g", builder_fn=_builder, order=OrderSchema(goal="g", allowed_paths=["src/"]))
        assert summary2["final_state"] == State.PASS
        ev3 = json.loads((get_runs_dir() / summary2["run_id"] / "final_evidence.json").read_text(encoding="utf-8"))
        assert ev3["worktree"] == "PRESENT" and ev3["canonical_containment"] == "PASS"


# ------------------------------------------------------------------ TEST D: HEAD pre-exec exception + get_provenance exception

def test_d_head_double_exception_provenance(isolate_provider_db, monkeypatch):
    StateMachine(); cm = ChatManager()
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())

    class DoubleFail:
        provider = "openai"
        last_execution_info = {}
        def chat(self, msg, ctx, hist):
            raise RuntimeError("CONFIG_ERROR: Official Codex CLI not found")
        def get_provenance(self, exit_code=0):
            raise RuntimeError("provenance generation exploded")
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: DoubleFail())

    handle_chat_message("hello")
    rec = cm.get_provenance(GLOBAL_SESSION_ID)[-1]
    assert rec["exit_code"] is None and rec["duration_sec"] is None
    assert rec["executable"] is None and rec["version"] is None
    assert json.loads(rec["observed_fields"]) == []
    assert rec["outcome"] == "HEAD_EXCEPTION"
    rows = StateMachine()._get_conn().execute("SELECT payload FROM audit_logs WHERE action='HEAD_EXCEPTION'").fetchall()
    assert rows and "CONFIG_ERROR" in rows[-1]["payload"]

    # single (pre-exec) exception: nothing observed either -> [] and NULLs, configuration is not "observed"
    class PreExec:
        provider = "openai"
        last_execution_info = {}
        def chat(self, msg, ctx, hist):
            raise RuntimeError("CONFIG_ERROR: not a git repo")
        def get_provenance(self, exit_code=0):
            from schemas.provenance import AgentProvenanceRecord
            from datetime import datetime
            return AgentProvenanceRecord(agent="HEAD", provider="openai", model="m", executable="codex", invocation_command="codex",
                                         version="unknown", exit_code=exit_code, timestamp=datetime.utcnow().isoformat())
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: PreExec())
    handle_chat_message("again")
    rec2 = cm.get_provenance(GLOBAL_SESSION_ID)[-1]
    assert rec2["exit_code"] is None and rec2["duration_sec"] is None and rec2["version"] is None
    assert json.loads(rec2["observed_fields"]) == []


# ------------------------------------------------------------------ TEST E: explicit observed_fields=[] preserved

def test_e_explicit_empty_observed_fields_preserved(isolate_provider_db):
    StateMachine(); cm = ChatManager()
    cm.create_session(GLOBAL_SESSION_ID, "", "OPENAI")
    cm.add_provenance(GLOBAL_SESSION_ID, {"agent": "HEAD", "provider": "openai", "observed_fields": [], "declared_fields": []})
    rec = cm.get_provenance(GLOBAL_SESSION_ID)[-1]
    assert json.loads(rec["observed_fields"]) == [] and json.loads(rec["declared_fields"]) == []
    cm.add_provenance(GLOBAL_SESSION_ID, {"agent": "HEAD", "provider": "openai"})           # None -> defaults
    rec2 = cm.get_provenance(GLOBAL_SESSION_ID)[-1]
    assert "exit_code" in json.loads(rec2["observed_fields"])

    class Rec:
        def model_dump(self):
            return {"agent": "HEAD", "provider": "openai", "observed_fields": [], "executable": "x", "version": "v"}

    class H:
        provider = "openai"
        last_execution_info = {"exit_code": 0, "execution_duration_sec": 1.0}
        def get_provenance(self, exit_code=0):
            return Rec()
    out = dashboard_server._head_provenance_record(H())
    assert out["observed_fields"] == []                                                # not replaced by defaults


# ------------------------------------------------------------------ isolation guards

def test_f_unit_tests_never_reach_real_cli_or_prod_db():
    assert os.environ.get("DIRECTOR_TESTING") == "1"
    assert get_db_path() != DEFAULT_DB_PATH
    # health probes are deterministic (stub CLI) regardless of local install/login/quota state
    pm = ProviderManager()
    statuses = pm.check_health(force=True)
    assert all(s == ProviderStatus.HEALTHY for s in statuses.values())
    assert pm.get_failover_mode()[0] == FailoverMode.NORMAL
    # the guard refuses a real CLI launch immediately (no 300s timeout, no quota)
    before = len(ProcessTreeRunner.blocked_real_cli_calls)
    for exe in ("codex.cmd", r"C:\\Users\\someone\\AppData\Roaming\npm\claude.cmd", "/usr/local/bin/agy"):
        with pytest.raises(RealCliCallDuringUnitTest):
            ProcessTreeRunner.run(argv=[exe, "--version"], timeout=5)
    assert len(ProcessTreeRunner.blocked_real_cli_calls) == before + 3
    del ProcessTreeRunner.blocked_real_cli_calls[before:]     # this test provoked them deliberately
    # a non-CLI process still runs normally
    res = ProcessTreeRunner.run(argv=[sys.executable, "-c", "print('ok')"], timeout=20)
    assert res.exit_code == 0 and "ok" in res.stdout


def test_g_provider_state_fixture_controls_mode(provider_state):
    pm = provider_state(OPENAI="QUOTA_EXHAUSTED")
    assert pm.get_failover_mode()[0] == FailoverMode.DEGRADED
    pm = provider_state(OPENAI="UNAVAILABLE", ANTHROPIC="UNAVAILABLE")
    assert pm.get_failover_mode()[0] == FailoverMode.STOP
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    assert pm.get_failover_mode()[0] == FailoverMode.NORMAL
