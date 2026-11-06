"""
v0.2.3.1 CHAT STABILIZATION — tests A..L from the patch specification.
"""
from core.db_path import get_runs_dir
import os
import sys
import json
import time
import threading
import subprocess
import tempfile
import contextlib
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import dashboard_server
from dashboard_server import handle_chat_message, GLOBAL_SESSION_ID, BUSY_MESSAGE
from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager, ProviderStatus, FailoverMode
from core.state_machine import StateMachine, State, TERMINAL_STATES
from core.orchestrator import Orchestrator
from core.db_path import get_db_path
from core.safety import SafetyPolicy
from schemas.order import OrderSchema
from schemas.provenance import AgentProvenanceRecord


# ------------------------------------------------------------------ helpers

@contextlib.contextmanager
def make_git_project():
    """Temporary git repository usable as a chat target project."""
    with tempfile.TemporaryDirectory() as tmpdir:
        proj = Path(tmpdir) / "target_project"
        proj.mkdir()
        subprocess.run(["git", "init"], cwd=str(proj), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(proj), check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=str(proj), check=True)
        (proj / "src").mkdir()
        (proj / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(proj), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(proj), check=True, capture_output=True)
        yield str(proj)



class _InlineThread:
    """Runs the target synchronously; used to make handle_chat_message's background run deterministic."""
    def __init__(self, target=None, name=None, daemon=None, args=(), kwargs=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
    def start(self):
        self._target(*self._args, **self._kwargs)
    def join(self, timeout=None):
        return None


def inline_threads(monkeypatch):
    """Patch only dashboard_server's Thread (not the global threading module, which subprocess needs)."""
    import types
    ns = types.SimpleNamespace(Thread=_InlineThread, Lock=threading.Lock, Event=threading.Event)
    monkeypatch.setattr(dashboard_server, "threading", ns)


def _healthy_providers():
    pm = ProviderManager()
    for p in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        pm.set_status(p, ProviderStatus.HEALTHY)
    return pm


@pytest.fixture
def chat_env(isolate_provider_db, monkeypatch):
    """Isolated DB, healthy providers, no real health probing, in-process run registry cleared."""
    StateMachine()
    cm = ChatManager()
    pm = _healthy_providers()
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())
    dashboard_server._ACTIVE_PROJECTS.clear()
    return cm, pm


def _order(**over):
    base = {
        "goal": "add helper",
        "allowed_paths": ["src/"],
        "protected_paths": [".git/"],
        "acceptance_criteria": ["tests pass"],
        "allowed_deletions": [],
        "max_files_changed": 3,
        "max_diff_lines": 50,
        "context": "",
    }
    base.update(over)
    return base


class HeadStub:
    """Mock HEAD whose chat() returns a fixed DEV_TASK and records real-looking execution info."""
    provider = "openai"

    def __init__(self, order_dict, exit_code=0, duration=1.25):
        self._order = order_dict
        self.last_execution_info = {"exit_code": exit_code, "execution_duration_sec": duration,
                                    "invocation_command": "codex exec --sandbox read-only <prompt>"}

    def chat(self, msg, ctx, hist):
        return {"type": "DEV_TASK", "message": "ok", "order": self._order}

    def get_provenance(self, exit_code=0):
        return AgentProvenanceRecord(
            agent="HEAD", provider="codex", runtime="codex-cli", model="codex-cli",
            executable="codex", invocation_command=self.last_execution_info["invocation_command"],
            version="codex-cli 0.153.4", exit_code=exit_code,
            duration_sec=self.last_execution_info["execution_duration_sec"],
            authentication="subscription", api_key_used=False, timestamp=datetime.utcnow().isoformat(),
        )


def _install_head(monkeypatch, head):
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: head)


# ------------------------------------------------------------------ TEST A: bind 127.0.0.1 only

def test_a_dashboard_binds_localhost_only(chat_env):
    captured = {}

    class FakeServer:
        def __init__(self, address, handler):
            captured["address"] = address

        def serve_forever(self):
            return None

    dashboard_server.run(server_class=FakeServer, port=8099)
    assert captured["address"] == ("127.0.0.1", 8099)
    assert dashboard_server.BIND_HOST == "127.0.0.1"

    with pytest.raises(ValueError):
        dashboard_server.run(server_class=FakeServer, host="0.0.0.0", port=8099)


# ------------------------------------------------------------------ TEST B: no project -> PROJECT_SELECTION_REQUIRED, Builder 0

def test_b_no_project_selected_blocks_dev_task(chat_env, monkeypatch):
    cm, pm = chat_env
    _install_head(monkeypatch, HeadStub(_order()))
    run_calls = []
    monkeypatch.setattr(Orchestrator, "run", lambda self, *a, **k: run_calls.append(1))

    handle_chat_message("add a helper")

    msgs = cm.get_messages(GLOBAL_SESSION_ID)
    assert msgs[-1]["role"] == "SYSTEM"
    assert "PROJECT_SELECTION_REQUIRED" in msgs[-1]["content"]
    assert run_calls == []
    run_row = StateMachine().get_run(msgs[-1]["run_id"])
    assert run_row["current_state"] == State.HUMAN_REQUIRED

    # Director repository itself can never be selected
    ok, reason = dashboard_server.validate_project_path(str(dashboard_server.DIRECTOR_ROOT))
    assert ok is False and "DIRECTOR" in reason


# ------------------------------------------------------------------ TEST C: LOW risk valid DEV_TASK runs without approval

def test_c_low_risk_valid_dev_task_runs_autonomously(chat_env, monkeypatch):
    cm, pm = chat_env
    _install_head(monkeypatch, HeadStub(_order(risk_level="LOW")))
    seen = {}

    def mock_run(self, project_path, goal, run_id=None, builder_fn=None, order=None, head_provenance=None):
        seen.update(project_path=project_path, run_id=run_id, order=order, head_provenance=head_provenance)

    monkeypatch.setattr(Orchestrator, "run", mock_run)
    inline_threads(monkeypatch)

    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        handle_chat_message("add a helper")

    assert seen["order"].goal == "add helper"
    assert Path(seen["project_path"]).resolve() == Path(proj).resolve()
    assert seen["head_provenance"]["exit_code"] == 0
    contents = [m["content"] for m in cm.get_messages(GLOBAL_SESSION_ID)]
    assert not any("HUMAN_REQUIRED" in c for c in contents)
    assert any(c.startswith("Starting background run") for c in contents)


# ------------------------------------------------------------------ TEST D: HIGH risk -> HUMAN_REQUIRED

@pytest.mark.parametrize("bad_order,expected", [
    (_order(risk_level="HIGH"), "HIGH_RISK"),
    (_order(requires_human_approval=True), "EXPLICIT_HUMAN_APPROVAL"),
    (_order(allowed_deletions=["old_func"]), "DESTRUCTIVE_CHANGE"),
    (_order(allowed_paths=[]), "AMBIGUOUS_SCOPE"),
    (_order(allowed_paths=["src/", ".git/"]), "PROTECTED_PATH"),
    (_order(allowed_paths=["requirements.txt"]), "SENSITIVE_FILE"),
])
def test_d_high_risk_orders_require_human(chat_env, monkeypatch, bad_order, expected):
    cm, pm = chat_env
    _install_head(monkeypatch, HeadStub(bad_order))
    run_calls = []
    monkeypatch.setattr(Orchestrator, "run", lambda self, *a, **k: run_calls.append(1))

    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        handle_chat_message("do something risky")

    msgs = cm.get_messages(GLOBAL_SESSION_ID)
    assert msgs[-1]["role"] == "SYSTEM"
    assert "HUMAN_REQUIRED" in msgs[-1]["content"] and expected in msgs[-1]["content"]
    assert run_calls == []
    assert StateMachine().get_run(msgs[-1]["run_id"])["current_state"] == State.HUMAN_REQUIRED


# ------------------------------------------------------------------ TEST E: concurrent DEV_TASK -> BUSY, Builder duplicate 0

def test_e_concurrent_dev_task_second_is_busy(chat_env, monkeypatch):
    cm, pm = chat_env
    _install_head(monkeypatch, HeadStub(_order()))
    release = threading.Event()
    started = threading.Event()
    run_calls = []

    def blocking_run(self, project_path, goal, run_id=None, builder_fn=None, order=None, head_provenance=None):
        run_calls.append(run_id)
        started.set()
        release.wait(timeout=10)

    monkeypatch.setattr(Orchestrator, "run", blocking_run)

    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        handle_chat_message("first task")            # starts background thread
        assert started.wait(timeout=5)
        handle_chat_message("second task")           # must be refused while the first is active
        msgs = cm.get_messages(GLOBAL_SESSION_ID)
        assert msgs[-1]["content"] == BUSY_MESSAGE
        release.set()
        time.sleep(0.3)
        assert len(run_calls) == 1

        # after the first run finished the project is free again
        started.clear(); release.set()
        handle_chat_message("third task")
        assert started.wait(timeout=5)
        assert len(run_calls) == 2


def test_e2_antigravity_global_lock_held_during_build(monkeypatch, tmp_path):
    from agents import antigravity_builder as ab
    from core.process_runner import ProcessResult

    observed = {}

    def fake_run(argv, cwd=None, env=None, timeout=None, no_output_timeout=None, heartbeat_interval=None, **kw):
        observed["locked_during_run"] = ab._AGY_GLOBAL_LOCK.locked()
        return ProcessResult(exit_code=0, stdout='{"event":"result","result":{"status":"SUCCESS","response":"done"}}\n', stderr="", timed_out=False, duration_sec=0.1, pid=1)

    monkeypatch.setattr(ab.ProcessTreeRunner, "run", staticmethod(fake_run))
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))  # keep the real ~/.gemini untouched

    wt = tmp_path / "wt"
    wt.mkdir()
    subprocess.run(["git", "init"], cwd=str(wt), check=True, capture_output=True)
    builder = ab.AntigravityBuilder(executable=sys.executable)
    builder.run(OrderSchema(goal="g", allowed_paths=["src/"]), str(wt))

    assert observed["locked_during_run"] is True
    assert ab._AGY_GLOBAL_LOCK.locked() is False   # released afterwards


# ------------------------------------------------------------------ TEST F: run_id unique within the same second

def test_f_generate_run_id_unique_100(chat_env):
    orch = Orchestrator(config={"head": {"provider": "mock"}, "reviewer": {"provider": "mock"}})
    ids = [orch.generate_run_id() for _ in range(100)]
    assert len(set(ids)) == 100
    assert all(i.startswith("RUN-") and len(i.split("-")) == 3 for i in ids)


# ------------------------------------------------------------------ TEST G: stale QUOTA_EXHAUSTED can recover after real recheck

def test_g_provider_recovery_bounded_and_real(isolate_provider_db, monkeypatch):
    pm = ProviderManager()
    monkeypatch.setattr("core.provider_manager.resolve_cli", lambda name: sys.executable)
    monkeypatch.setattr(ProviderManager, "_get_cli_version", lambda self, b, arg="--version": "cli 1.0.0")

    pm.set_status("OPENAI", ProviderStatus.QUOTA_EXHAUSTED, "You've hit your usage limit")
    pm.check_health()                                   # fresh failure: bounded -> stays down
    assert pm.get_status("OPENAI") == ProviderStatus.QUOTA_EXHAUSTED

    # age the failure beyond the recheck window -> cheap probe succeeds -> HEALTHY again
    old = (datetime.utcnow() - timedelta(seconds=pm.RECHECK_AFTER_SEC + 60)).isoformat()
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET last_failure=? WHERE provider='OPENAI'", (old,))
        conn.commit()
    pm.check_health()
    assert pm.get_status("OPENAI") == ProviderStatus.RECOVERING   # v0.2.3.2: probe never yields HEALTHY
    pm.record_success("OPENAI")                                    # only a real exit-0 call promotes
    assert pm.get_status("OPENAI") == ProviderStatus.HEALTHY

    # reset_at in the past unlocks a recheck even when last_failure is fresh
    pm.set_status("ANTHROPIC", ProviderStatus.QUOTA_EXHAUSTED, "limit")
    past = (datetime.utcnow() - timedelta(minutes=1)).isoformat()
    with pm._get_conn() as conn:
        conn.execute("UPDATE provider_health SET reset_at=? WHERE provider='ANTHROPIC'", (past,))
        conn.commit()
    pm.check_health()
    assert pm.get_status("ANTHROPIC") == ProviderStatus.RECOVERING

    # no fake HEALTHY: forced recheck whose probe fails keeps the provider down
    pm.set_status("GOOGLE", ProviderStatus.TIMEOUT, "agy hung")
    monkeypatch.setattr(ProviderManager, "_get_cli_version", lambda self, b, arg="--version": "UNKNOWN")
    pm.check_health(force=True)
    assert pm.get_status("GOOGLE") != ProviderStatus.HEALTHY

    # reset_at parsing from the real Codex message
    assert pm.parse_reset_at("ERROR: You've hit your usage limit ... or try again at 11:13 PM.") is not None
    assert pm.parse_reset_at("plain failure") is None


# ------------------------------------------------------------------ TEST H: same director.db from any cwd

def test_h_db_path_is_canonical_regardless_of_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("DIRECTOR_TESTING", raising=False)
    monkeypatch.delenv("TEST_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    canonical = get_db_path()
    assert canonical.is_absolute()
    assert canonical == dashboard_server.resolve_db_path()
    assert canonical.parent.parent == dashboard_server.DIRECTOR_ROOT
    assert tmp_path not in canonical.parents
    assert "data/director.db" in canonical.as_posix()


def test_h2_all_components_share_resolver(isolate_provider_db):
    expected = get_db_path()
    assert StateMachine().db_path == expected
    assert ChatManager().db_path == expected
    assert ProviderManager().db_path == expected
    assert dashboard_server.resolve_db_path() == expected


# ------------------------------------------------------------------ TEST I: dashboard GET works while a chat POST is blocked in HEAD

def test_i_dashboard_not_blocked_by_slow_head(chat_env, monkeypatch):
    monkeypatch.setattr(dashboard_server, "handle_chat_message", lambda msg: time.sleep(2.5))
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def slow_post():
            data = urllib.parse.urlencode({"message": "hello"}).encode()
            req = urllib.request.Request(f"http://127.0.0.1:{port}/chat", data=data, method="POST")
            try:
                urllib.request.urlopen(req, timeout=10)
            except Exception:
                pass
        pt = threading.Thread(target=slow_post, daemon=True)
        pt.start()
        time.sleep(0.3)
        t0 = time.time()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            body = r.read().decode("utf-8")
        elapsed = time.time() - t0
        assert r.status == 200 and "AI DEV DIRECTOR" in body
        assert elapsed < 1.5, f"GET blocked for {elapsed:.2f}s while HEAD chat was running"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r2:
            assert "OPENAI" in r2.read().decode("utf-8")
        pt.join(timeout=10)
    finally:
        httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ TEST J: Korean path subprocess decoding

def test_j_korean_path_subprocess_no_decode_errors(tmp_path):
    import urllib.parse as _up
    from core.env_utils import run_capture
    kdir = tmp_path / "한글경로_테스트"
    kdir.mkdir()
    subprocess.run(["git", "init"], cwd=str(kdir), check=True, capture_output=True)

    thread_errors = []
    orig_hook = threading.excepthook
    threading.excepthook = lambda args: thread_errors.append(args)
    try:
        r = run_capture(["git", "rev-parse", "--show-toplevel"], cwd=str(kdir), timeout=15)
        assert r.returncode == 0
        assert isinstance(r.stdout, str) and "한글경로_테스트" in r.stdout
        assert isinstance(r.stderr, str)

        # the reviewer/head git pre-checks use the same helper
        from agents.codex_reviewer import CodexReviewer
        src = Path("agents/codex_reviewer.py").read_text(encoding="utf-8")
        assert "run_capture(" in src and 'capture_output=True, text=True)' not in src
        src2 = Path("agents/codex_head.py").read_text(encoding="utf-8")
        assert "run_capture(" in src2
        src3 = Path("core/provider_manager.py").read_text(encoding="utf-8")
        assert "probe_version(" in src3 and "subprocess.run(" not in src3.split("def _get_cli_version")[1].split("def ")[0]
    finally:
        threading.excepthook = orig_hook
    assert thread_errors == []


# ------------------------------------------------------------------ TEST K: chat HEAD provenance recorded with actual values

def test_k_chat_head_provenance_recorded(chat_env, monkeypatch):
    cm, pm = chat_env
    head = HeadStub(_order(), exit_code=0, duration=3.75)
    _install_head(monkeypatch, head)
    monkeypatch.setattr(Orchestrator, "run", lambda self, *a, **k: None)
    inline_threads(monkeypatch)

    with make_git_project() as proj:
        cm.set_project(GLOBAL_SESSION_ID, proj)
        handle_chat_message("add a helper")

    prov = cm.get_provenance(GLOBAL_SESSION_ID)
    assert len(prov) == 1
    rec = prov[0]
    assert rec["agent"] == "HEAD"
    assert rec["provider"] == "codex" and rec["runtime"] == "codex-cli"
    assert rec["executable"] == "codex"
    assert rec["invocation_command"] == "codex exec --sandbox read-only <prompt>"
    assert rec["exit_code"] == 0
    assert rec["duration_sec"] == 3.75
    assert rec["authentication"] is None                      # never stored as if measured
    assert rec["authentication_mode"] == "subscription"       # declared policy value
    assert rec["api_key_policy"] == "disabled"
    assert "authentication_mode" in rec["declared_fields"] and "exit_code" in rec["observed_fields"]
    assert rec["api_key_used"] == 0

    # a failing HEAD call records its real exit code and duration, not an estimate
    head2 = HeadStub(_order(), exit_code=1, duration=0.5)
    head2.chat = lambda msg, ctx, hist: {"type": "QUESTION", "message": "answer"}
    _install_head(monkeypatch, head2)
    handle_chat_message("what is this?")
    rec2 = cm.get_provenance(GLOBAL_SESSION_ID)[-1]
    assert rec2["exit_code"] == 1 and rec2["duration_sec"] == 0.5


# ------------------------------------------------------------------ TEST L: safety caps

def test_l_order_caps_clamped_to_global_config(isolate_provider_db):
    from agents.mock_head import MockHead
    from agents.codex_reviewer import CodexReviewer
    cfg = {
        "head": {"provider": "mock"}, "reviewer": {"provider": "mock"},
        "safety": {"max_files_changed": 4, "max_diff_lines": 120},
        "verification": {},
    }
    orch = Orchestrator(config=cfg, head_agent=MockHead(), reviewer_agent=CodexReviewer(provider="mock"))

    big = OrderSchema(goal="g", allowed_paths=["src/"], max_files_changed=999, max_diff_lines=99999)
    notes = orch.apply_safety_caps(big)
    assert big.max_files_changed == 4 and big.max_diff_lines == 120
    assert len(notes) == 2

    small = OrderSchema(goal="g", allowed_paths=["src/"], max_files_changed=2, max_diff_lines=40)
    assert orch.apply_safety_caps(small) == []
    assert small.max_files_changed == 2 and small.max_diff_lines == 40

    # end-to-end: an oversized chat ORDER is clamped before the Scope Guard runs
    with make_git_project() as proj:
        def builder(b, o, wt):
            for i in range(6):
                (Path(wt) / "src" / f"f{i}.py").write_text("x = 1\n", encoding="utf-8")
            return [f"src/f{i}.py" for i in range(6)]
        oversized = OrderSchema(goal="g", allowed_paths=["src/"], max_files_changed=999, max_diff_lines=99999)
        summary = orch.run(proj, "g", builder_fn=builder, order=oversized)
        assert summary["final_state"] == State.HUMAN_REQUIRED          # 6 files > effective cap 4
        saved = json.loads((get_runs_dir() / summary["run_id"] / "order.json").read_text(encoding="utf-8"))
        assert saved["max_files_changed"] == 4
        actions = [r["action"] for r in orch.sm._get_conn().execute(
            "SELECT action FROM audit_logs WHERE run_id=?", (summary["run_id"],)).fetchall()]
        assert "SAFETY_CAP_APPLIED" in actions and "SCOPE_VIOLATION" in actions


# ------------------------------------------------------------------ subscription-only invariant (13)

def test_m_subscription_only_no_sdk_or_keys():
    import re
    forbidden = re.compile(r"^\s*(import|from)\s+(openai|anthropic|google\.generativeai|requests|httpx|aiohttp)\b")
    for p in list(Path("core").glob("*.py")) + list(Path("agents").glob("*.py")) + [Path("dashboard_server.py"), Path("director.py")]:
        for line in p.read_text(encoding="utf-8").splitlines():
            assert not forbidden.match(line), f"{p}: {line.strip()}"
            assert "dangerously-skip-permissions" not in line
