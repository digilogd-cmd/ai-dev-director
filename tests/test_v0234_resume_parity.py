"""
v0.2.3.4 RESUME PARITY & WINDOWS FINAL BLOCKER FIX — tests A..S.
"""
from core.db_path import get_runs_dir
import os
import sys
import json
import time
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import psutil

import dashboard_server
from dashboard_server import handle_chat_message, GLOBAL_SESSION_ID
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State, TERMINAL_STATES
from core.git_manager import GitManager
from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager, ProviderStatus
from core.safety import SafetyPolicy
from core.env_utils import probe_version
from schemas.order import OrderSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema
from schemas.provenance import AgentProvenanceRecord
from agents.mock_head import MockHead
from agents.codex_reviewer import CodexReviewer
from agents.antigravity_builder import AntigravityBuilder
from tests.test_v0231_chat_stabilization import make_git_project, inline_threads, HeadStub, _order
from tests.test_v0233_codex_fixes import _make_link


# ------------------------------------------------------------------ helpers

STATE_PATH = [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING]


def _mock_orch(sm=None, **cfg_over):
    cfg = {"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}}
    cfg.update(cfg_over)
    return Orchestrator(config=cfg, state_machine=sm, head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"))


def _stuck_run(proj, target_state, order=None, with_worktree=True, goal="Goal"):
    """Creates a run row advanced to `target_state` (valid path), with a worktree and order.json."""
    sm = StateMachine()
    run_id = f"RUN-RESUME-{target_state}-{int(time.time()*1000)}-{os.getpid()}"
    sm.create_run(run_id, str(proj), goal)
    wt = None
    if with_worktree:
        wt, br = GitManager.create_worktree(str(proj), run_id)
        sm.update_run_worktree(run_id, wt, br)
    for st in STATE_PATH:
        sm.transition_to(run_id, st)
        if st == target_state:
            break
    orch = _mock_orch()
    if order is not None:
        orch._save_json(run_id, "order.json", order)
    return run_id, wt, sm


def _rogue(sentinel, calls):
    def builder(b, o, wt):
        calls.append(wt)
        sentinel.write_text("HACKED", encoding="utf-8")
        return []
    return builder


# ------------------------------------------------------------------ TEST A/B: resume with bad ORDER paths -> Builder 0

@pytest.mark.parametrize("bad", ["../outside", "../../outside", r"C:\temp", "/tmp/foo", r"\\server\share", "~/x"])
def test_a_b_resume_bad_paths_fail_closed(isolate_provider_db, tmp_path, bad):
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("ORIGINAL", encoding="utf-8")
    calls = []
    with make_git_project() as proj:
        order = OrderSchema(goal="attack", allowed_paths=[bad])
        run_id, wt, sm = _stuck_run(proj, State.ORDER_READY, order)
        summary = _mock_orch().resume(run_id, builder_fn=_rogue(sentinel, calls))
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert calls == []
    assert sentinel.read_text(encoding="utf-8") == "ORIGINAL"
    assert any("ORDER_PATH_VIOLATION" in (t["details"] or "") for t in sm.get_transitions(run_id))
    # the same ORDER resumed from BUILDING is refused too
    calls2 = []
    with make_git_project() as proj:
        run_id2, wt2, sm2 = _stuck_run(proj, State.BUILDING, order)
        summary2 = _mock_orch().resume(run_id2, builder_fn=_rogue(sentinel, calls2))
    assert summary2["final_state"] == State.HUMAN_REQUIRED and calls2 == []


# ------------------------------------------------------------------ TEST C: resume symlink escape -> Builder 0

def test_c_resume_symlink_escape(isolate_provider_db, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "s.txt"
    sentinel.write_text("ORIGINAL", encoding="utf-8")
    calls = []
    with make_git_project() as proj:
        order = OrderSchema(goal="attack", allowed_paths=["src/escape/"])
        run_id, wt, sm = _stuck_run(proj, State.ORDER_READY, order)
        if not _make_link(Path(wt) / "src" / "escape", outside):
            pytest.skip("symlink/junction creation not permitted")
        summary = _mock_orch().resume(run_id, builder_fn=_rogue(sentinel, calls))
        assert summary["final_state"] == State.HUMAN_REQUIRED
        assert calls == []
        assert sentinel.read_text(encoding="utf-8") == "ORIGINAL"
        assert any("resolves outside worktree" in (t["details"] or "") for t in sm.get_transitions(run_id))


# ------------------------------------------------------------------ TEST D: resume clamps caps

def test_d_resume_safety_cap_clamp(isolate_provider_db):
    with make_git_project() as proj:
        order = OrderSchema(goal="g", allowed_paths=["src/"], max_files_changed=999, max_diff_lines=99999)
        run_id, wt, sm = _stuck_run(proj, State.ORDER_READY, order)
        orch = _mock_orch(safety={"max_files_changed": 4, "max_diff_lines": 120})

        def builder(b, o, wt_):
            for i in range(6):
                (Path(wt_) / "src" / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
            return []
        summary = orch.resume(run_id, builder_fn=builder)
        saved = json.loads((get_runs_dir() / run_id / "order.json").read_text(encoding="utf-8"))
        assert saved["max_files_changed"] == 4 and saved["max_diff_lines"] == 120
        actions = [r["action"] for r in sm._get_conn().execute("SELECT action FROM audit_logs WHERE run_id=?", (run_id,)).fetchall()]
        assert "SAFETY_CAP_APPLIED" in actions
        assert summary["final_state"] == State.HUMAN_REQUIRED          # 6 files > effective cap 4 -> scope guard
        assert "SCOPE_VIOLATION" in actions


# ------------------------------------------------------------------ TEST E: resume ORDER_READY with failing baseline -> Builder 0

def test_e_resume_baseline_failure_blocks_builder(isolate_provider_db):
    calls = []
    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.ORDER_READY, OrderSchema(goal="g", allowed_paths=["src/"]))
        assert not (get_runs_dir() / run_id / "baseline_verification.json").exists()
        orch = _mock_orch(verification={"test_command": f'{sys.executable} -c "import sys; print(\'1 failed\'); sys.exit(1)"'})
        summary = orch.resume(run_id, builder_fn=lambda b, o, w: calls.append(w) or [])
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert calls == []
    assert any("BASELINE_FAILED" in (t["details"] or "") for t in sm.get_transitions(run_id))
    assert (get_runs_dir() / run_id / "baseline_verification.json").exists()   # measured, not defaulted


# ------------------------------------------------------------------ TEST F: resume baseline PASS -> build -> regression, same as fresh run

def test_f_resume_full_pipeline_regression(isolate_provider_db):
    with make_git_project() as proj:
        tests_dir = Path(proj) / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_x.py").write_text("def test_1(): pass\ndef test_2(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "tests"], cwd=proj, check=True, capture_output=True)

        # regression: builder deletes one test -> HUMAN_REQUIRED via the shared regression gate
        run_id, wt, sm = _stuck_run(proj, State.ORDER_READY, OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))
        orch = _mock_orch(verification={"test_command": "python -m pytest tests/ -q"})

        def deleting_builder(b, o, wt_):
            (Path(wt_) / "tests" / "test_x.py").write_text("def test_1(): pass\n", encoding="utf-8")
            return ["tests/test_x.py"]
        summary = orch.resume(run_id, builder_fn=deleting_builder)
        base = json.loads((get_runs_dir() / run_id / "baseline_verification.json").read_text(encoding="utf-8"))
        assert base["test_counts"]["passed"] == 2                       # baseline measured before the build
        assert summary["final_state"] == State.HUMAN_REQUIRED
        details = " ".join((t["details"] or "") for t in sm.get_transitions(run_id))
        assert "Unauthorized deletion" in details or "Baseline test count decreased" in details

        # healthy path: builder adds a test -> resume walks BUILDING/VERIFYING/REVIEWING/DECIDING -> PASS
        run_id2, wt2, sm2 = _stuck_run(proj, State.ORDER_READY, OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))

        def adding_builder(b, o, wt_):
            (Path(wt_) / "tests" / "test_y.py").write_text("def test_3(): pass\n", encoding="utf-8")
            return ["tests/test_y.py"]
        summary2 = _mock_orch(verification={"test_command": "python -m pytest tests/ -q"}).resume(run_id2, builder_fn=adding_builder)
        assert summary2["final_state"] == State.PASS
        seen = [t["to_state"] for t in sm2.get_transitions(run_id2)]
        assert seen[-5:] == [State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.PASS]
        actions = [r["action"] for r in sm2._get_conn().execute("SELECT action FROM audit_logs WHERE run_id=?", (run_id2,)).fetchall()]
        assert "VERIFICATION" in actions and "CODE_REVIEW" in actions and "FINAL_DECISION" in actions

        # a VERIFYING run without any baseline result never passes silently
        run_id3, wt3, sm3 = _stuck_run(proj, State.VERIFYING, OrderSchema(goal="g", allowed_paths=["tests/", "src/"]))
        summary3 = _mock_orch(verification={"test_command": "python -m pytest tests/ -q"}).resume(run_id3)
        assert summary3["final_state"] == State.HUMAN_REQUIRED
        assert any("BASELINE_MISSING" in (t["details"] or "") for t in sm3.get_transitions(run_id3))


# ------------------------------------------------------------------ TEST G: resume with RECOVERING provider and no trial -> CLI 0

def test_g_resume_trial_unavailable_blocks_cli(isolate_provider_db, monkeypatch):
    pm = ProviderManager()
    for d in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        pm.set_status(d, ProviderStatus.HEALTHY)
    pm.set_status("OPENAI", ProviderStatus.RECOVERING, "probe ok")
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET next_trial_at=? WHERE provider='OPENAI'",
                     ((datetime.utcnow() + timedelta(minutes=5)).isoformat(),))
        conn.commit()
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())
    calls = []

    class OpenAIHead:
        provider = "openai"
        agent_family = "gpt"
        last_execution_info = {}
        def create_order(self, goal, ctx):
            calls.append("create_order")
            return OrderSchema(goal=goal, allowed_paths=["src/"])
        def decide(self, *a, **k):
            calls.append("decide")
            return DecisionSchema(decision="PASS", rationale="x", next_steps=[])
        def get_provenance(self, exit_code=0):
            return AgentProvenanceRecord(agent="HEAD", provider="openai", model="m", executable="codex",
                                         invocation_command="codex", version="1", exit_code=exit_code, timestamp=datetime.utcnow().isoformat())

    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.PLANNING, with_worktree=False)
        orch = Orchestrator(config={"verification": {}}, head_agent=OpenAIHead(), reviewer_agent=CodexReviewer(provider="mock"))
        summary = orch.resume(run_id)
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert calls == []                                                 # no AI CLI call happened
    assert any("PROVIDER_TRIAL_UNAVAILABLE" in (t["details"] or "") for t in sm.get_transitions(run_id))
    assert ProviderManager().get_status("OPENAI") == ProviderStatus.RECOVERING   # never promoted


# ------------------------------------------------------------------ TEST H: resume reviewer exception -> terminal + audit + provenance

def test_h_resume_reviewer_exception(isolate_provider_db):
    class Boom:
        provider = "mock"
        def review(self, *a, **k):
            raise RuntimeError("reviewer crashed during resume")
        def get_provenance(self, exit_code=0):
            return AgentProvenanceRecord(agent="REVIEWER", provider="mock", model="m", executable="x",
                                         invocation_command="x", version="1", exit_code=exit_code, timestamp=datetime.utcnow().isoformat())

    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.REVIEWING, OrderSchema(goal="g", allowed_paths=["src/"]))
        orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                            head_agent=MockHead(), reviewer_agent=Boom())
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})   # v0.2.3.5: late states need evidence
        orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 0}})
        summary = orch.resume(run_id)
    assert summary["final_state"] in TERMINAL_STATES and summary["final_state"] != State.PASS
    assert sm.get_run(run_id)["current_state"] != State.REVIEWING
    actions = [r["action"] for r in sm._get_conn().execute("SELECT action FROM audit_logs WHERE run_id=?", (run_id,)).fetchall()]
    assert "REVIEWER_EXCEPTION" in actions
    prov = json.loads((get_runs_dir() / run_id / "provenance.json").read_text(encoding="utf-8"))
    assert any(r.get("agent") == "REVIEWER" and r.get("exit_code") == 1 for r in prov["records"])


# ------------------------------------------------------------------ TEST I: resume changes_required + P2 only -> no PASS

def test_i_resume_reviewer_verdict_gate(isolate_provider_db):
    class PassHead(MockHead):
        def decide(self, *a, **k):
            return DecisionSchema(decision="PASS", rationale="head says pass", next_steps=[])

    with make_git_project() as proj:
        run_id, wt, sm = _stuck_run(proj, State.DECIDING, OrderSchema(goal="g", allowed_paths=["src/"]))
        orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                            head_agent=PassHead(), reviewer_agent=CodexReviewer(provider="mock"))
        orch._save_json(run_id, "builder.json", {"status": "COMPLETED", "files_modified": [], "commands_run": [], "summary": "ok"})
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 0}})   # v0.2.3.5: late states need evidence
        orch._save_json(run_id, "verification.json", {"passed": True, "test_counts": {"passed": 0}})
        orch._save_json(run_id, "review.json", {"verdict": "changes_required", "p0": [], "p1": [], "p2": ["issue"], "summary": "minor"})
        summary = orch.resume(run_id)
    assert summary["final_state"] == State.HUMAN_REQUIRED
    assert "not 'pass'" in summary["decision"]["rationale"]


# ------------------------------------------------------------------ TEST J/K/L/M: settings restore & recovery

def _fake_agy(monkeypatch, seen):
    from core.process_runner import ProcessResult
    from agents import antigravity_builder as ab

    def fake_run(argv, **kw):
        seen.append(argv)
        return ProcessResult(exit_code=0, stdout='{"event":"result","result":{"status":"SUCCESS","response":"done"}}\n', stderr="", timed_out=False, duration_sec=0.1, pid=1)
    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(fake_run))


def test_j_restore_success_removes_backup(isolate_provider_db, tmp_path, monkeypatch):
    seen = []
    _fake_agy(monkeypatch, seen)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    sf = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    sf.parent.mkdir(parents=True)
    sf.write_text(json.dumps({"permissions": {"allow": [], "deny": []}, "user_pref": 7}), encoding="utf-8")
    with make_git_project() as proj:
        res = AntigravityBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res.status == "FAILED" and res.error.startswith("BUILDER_NO_CHANGES")
    assert len([a for a in seen if "--sandbox" in a]) == 1          # exactly one agy build (probes excluded)
    assert json.loads(sf.read_text(encoding="utf-8"))["user_pref"] == 7
    assert not sf.with_name(sf.name + ".director-original").exists()


def test_k_restore_failure_keeps_backup_and_escalates(isolate_provider_db, tmp_path, monkeypatch):
    seen = []
    _fake_agy(monkeypatch, seen)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    sf = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    sf.parent.mkdir(parents=True)
    original = {"permissions": {"allow": ["command(git status*)"], "deny": []}, "user_pref": 42}
    sf.write_text(json.dumps(original), encoding="utf-8")
    real_replace = os.replace

    def failing_restore(src, dst):
        if str(src).endswith(".director-restore"):
            raise PermissionError("simulated restore failure")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_restore)

    with make_git_project() as proj:
        res = AntigravityBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res.status == "FAILED" and res.error.startswith("PERMISSION_RESTORE_FAILED")
    backup = sf.with_name(sf.name + ".director-original")
    assert backup.exists() and json.loads(backup.read_text(encoding="utf-8")) == original      # original preserved
    assert "_director_scoped" in json.loads(sf.read_text(encoding="utf-8"))                     # scoped state detectable
    assert "scoped settings still active" in res.error

    # orchestrator escalates to HUMAN_REQUIRED
    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}, "verification": {}},
                        head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"),
                        builder_agent=AntigravityBuilder(executable=sys.executable))
    with make_git_project() as proj:
        summary = orch.run(proj, "g")
    assert summary["final_state"] == State.HUMAN_REQUIRED
    actions = [r["action"] for r in orch.sm._get_conn().execute("SELECT action FROM audit_logs WHERE run_id=?", (summary["run_id"],)).fetchall()]
    assert "PERMISSION_RESTORE_FAILED" in actions
    monkeypatch.setattr(os, "replace", real_replace)


def test_l_m_next_run_recovers_original_before_build(isolate_provider_db, tmp_path, monkeypatch):
    seen = []
    _fake_agy(monkeypatch, seen)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    sf = tmp_path / ".gemini" / "antigravity-cli" / "settings.json"
    sf.parent.mkdir(parents=True)
    original = {"permissions": {"allow": ["command(git status*)"], "deny": []}, "user_pref": 42}
    backup = sf.with_name(sf.name + ".director-original")
    scoped = {"permissions": {"allow": ["write_file(/x/*)"], "deny": []}, "_director_scoped": {"pid": 1}}
    sf.write_text(json.dumps(scoped), encoding="utf-8")
    backup.write_text(json.dumps(original), encoding="utf-8")

    # L: recovery succeeds -> build allowed, original restored afterwards, backup never overwritten with scoped content
    observed = {}
    from agents import antigravity_builder as ab
    from core.process_runner import ProcessResult

    def fake_run(argv, **kw):
        observed["backup_during"] = json.loads(backup.read_text(encoding="utf-8"))
        return ProcessResult(exit_code=0, stdout='{"event":"result","result":{"status":"SUCCESS","response":"done"}}\n', stderr="", timed_out=False, duration_sec=0.1, pid=1)
    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(fake_run))
    with make_git_project() as proj:
        res = AntigravityBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res.status == "FAILED" and res.error.startswith("BUILDER_NO_CHANGES")
    assert observed["backup_during"] == original
    assert json.loads(sf.read_text(encoding="utf-8"))["user_pref"] == 42
    assert not backup.exists()

    # M: recovery fails -> Builder 0, PERMISSION_SETUP_FAILED, backup untouched
    sf.write_text(json.dumps(scoped), encoding="utf-8")
    backup.write_text(json.dumps(original), encoding="utf-8")
    calls = []
    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(lambda argv, **kw: calls.append(argv)))
    real_replace = os.replace

    def failing_recovery(src, dst):
        if str(src).endswith(".director-original"):
            raise PermissionError("simulated recovery failure")
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", failing_recovery)
    with make_git_project() as proj:
        res2 = AntigravityBuilder(executable=sys.executable).run(OrderSchema(goal="g", allowed_paths=["src/"]), proj)
    assert res2.status == "FAILED" and res2.error.startswith("PERMISSION_SETUP_FAILED")
    assert calls == []
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    monkeypatch.setattr(os, "replace", real_replace)


# ------------------------------------------------------------------ TEST N/O/P/Q: Windows --version probe tree-kill

HANG = r'''
import sys, time, subprocess
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
Path(sys.argv[1]).write_text(str(child.pid))
print("hanging", flush=True)
time.sleep(60)
'''


def _hanging_cli(tmp_path, name):
    hang = tmp_path / "hang.py"
    hang.write_text(HANG, encoding="utf-8")
    pid_file = tmp_path / f"{name}.child.pid"
    if os.name == "nt":
        cli = tmp_path / f"{name}.cmd"
        cli.write_text(f'@echo off\r\n"{sys.executable}" "{hang}" "{pid_file}" %*\r\n', encoding="utf-8")
    else:
        cli = tmp_path / name
        cli.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{hang}" "{pid_file}" "$@"\n', encoding="utf-8")
        cli.chmod(0o755)
    return cli, pid_file


def _wait_dead(pid, timeout=5.0):
    deadline = time.time() + timeout
    while psutil.pid_exists(pid) and time.time() < deadline:
        time.sleep(0.05)
    return not psutil.pid_exists(pid)


def test_n_version_probe_kills_parent_and_child(tmp_path):
    cli, pid_file = _hanging_cli(tmp_path, "fake_codex")
    t0 = time.time()
    v = probe_version(str(cli), timeout=2.0)
    elapsed = time.time() - t0
    assert v == "UNKNOWN"
    assert elapsed < 10.0
    child_pid = int(pid_file.read_text()) if pid_file.exists() else None
    assert child_pid is not None
    assert _wait_dead(child_pid), f"child {child_pid} leaked after probe timeout"


@pytest.mark.parametrize("name", ["fake_codex", "fake_claude", "fake_agy"])
def test_o_p_cli_version_timeout_no_hang(tmp_path, name, isolate_provider_db):
    cli, pid_file = _hanging_cli(tmp_path, name)
    pm = ProviderManager()
    pm.PROBE_TIMEOUT_SEC = 2.0
    t0 = time.time()
    assert pm._get_cli_version(str(cli)) == "UNKNOWN"
    assert time.time() - t0 < 10.0
    if pid_file.exists():
        assert _wait_dead(int(pid_file.read_text()))


def test_q_full_health_check_bounded(tmp_path, isolate_provider_db, monkeypatch):
    clis = {n: _hanging_cli(tmp_path, "fake_" + n)[0] for n in ("claude", "agy", "codex")}
    monkeypatch.setattr("core.provider_manager.resolve_cli", lambda name: str(clis[name]))
    pm = ProviderManager()
    pm.PROBE_TIMEOUT_SEC = 2.0
    t0 = time.time()
    statuses = pm.check_health(force=True)
    assert time.time() - t0 < 3 * 2.0 + 8
    assert all(s == ProviderStatus.TIMEOUT for s in statuses.values())
    # only real probe children count (python running hang.py); the shell that launched pytest carries the
    # test command line (which mentions this file) and must be ignored, as must our own ancestry
    me = psutil.Process(os.getpid())
    ancestry = {me.pid} | {p.pid for p in me.parents()}
    deadline = time.time() + 5.0
    while True:
        leftovers = [p for p in psutil.process_iter(["pid", "name", "cmdline"])
                     if p.info["pid"] not in ancestry
                     and (p.info["name"] or "").lower().startswith("python")
                     and any("hang.py" in (c or "") for c in (p.info["cmdline"] or []))]
        if not leftovers or time.time() > deadline:
            break
        time.sleep(0.2)
    assert leftovers == [], [(p.pid, p.info["cmdline"]) for p in leftovers]


# ------------------------------------------------------------------ TEST R: dashboard run_id collision retry

def test_r_dashboard_run_id_collision_retry(isolate_provider_db, monkeypatch):
    StateMachine(); cm = ChatManager()
    pm = ProviderManager()
    for d in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        pm.set_status(d, ProviderStatus.HEALTHY)
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())
    dashboard_server._ACTIVE_PROJECTS.clear()
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: HeadStub(_order()))
    seen = {}

    def mock_run(self, project_path, goal, run_id=None, builder_fn=None, order=None, head_provenance=None):
        seen["run_id"] = run_id
        self.sm.transition_to(run_id, State.PLANNING)
        self.sm.transition_to(run_id, State.FAIL, details="mock end")
    monkeypatch.setattr(Orchestrator, "run", mock_run)
    inline_threads(monkeypatch)

    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        # pre-existing run occupies the id the generator will produce first
        sm = StateMachine()
        taken = "RUN-000000000000-" + "A" * 32
        sm.create_run(taken, str(Path(proj).parent / "other_project"), "old")   # same id, different project
        ids = iter([taken, taken, "RUN-000000000000-" + "B" * 32])
        monkeypatch.setattr(Orchestrator, "generate_run_id", lambda self: next(ids))
        handle_chat_message("add helper")

    assert seen["run_id"] == "RUN-000000000000-" + "B" * 32
    msgs = [m["content"] for m in cm.get_messages(GLOBAL_SESSION_ID)]
    assert not any("IntegrityError" in c or "Run allocation failed" in c or "Run failed" in c for c in msgs)
    assert any(c.startswith("Starting background run RUN-000000000000-BBBB") for c in msgs)
    # explicit collision inside run() is still not silently swallowed
    import sqlite3
    orch = _mock_orch(StateMachine())
    with pytest.raises(sqlite3.IntegrityError):
        orch._create_run_with_retry(seen["run_id"], "p", "g")          # already FAIL, not a reserved IDLE row


# ------------------------------------------------------------------ TEST S: HEAD pre-exec exception provenance

def test_s_head_pre_exec_exception_provenance(isolate_provider_db, monkeypatch):
    StateMachine(); cm = ChatManager()
    pm = ProviderManager()
    for d in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        pm.set_status(d, ProviderStatus.HEALTHY)
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())

    class PreExecFailHead:
        provider = "openai"
        last_execution_info = {}                       # nothing ever ran
        def chat(self, msg, ctx, hist):
            raise RuntimeError("CONFIG_ERROR: Official Codex CLI not found")
        def get_provenance(self, exit_code=0):
            return AgentProvenanceRecord(agent="HEAD", provider="openai", runtime="codex-cli", model="m", executable="codex",
                                         invocation_command="codex", version="unknown", exit_code=exit_code,
                                         timestamp=datetime.utcnow().isoformat())
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: PreExecFailHead())

    handle_chat_message("hello")
    rec = cm.get_provenance(GLOBAL_SESSION_ID)[-1]
    assert rec["exit_code"] is None and rec["duration_sec"] is None
    observed = json.loads(rec["observed_fields"])
    assert "exit_code" not in observed and "duration_sec" not in observed
    assert rec["outcome"] == "HEAD_EXCEPTION"
    rows = StateMachine()._get_conn().execute("SELECT action, payload FROM audit_logs WHERE action='HEAD_EXCEPTION'").fetchall()
    assert rows and "CONFIG_ERROR" in rows[-1]["payload"]
    assert cm.get_messages(GLOBAL_SESSION_ID)[-1]["content"].startswith("HEAD Error")

    # real agents: no process -> exit_code None (never a default 0/1)
    from agents.codex_head import CodexHead
    h = CodexHead(executable=sys.executable)
    h.last_execution_info = {}
    assert h.get_provenance(exit_code=1).exit_code is None
    h.last_execution_info = {"exit_code": 3}
    assert h.get_provenance(exit_code=0).exit_code == 3
