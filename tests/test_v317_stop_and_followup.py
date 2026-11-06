"""
Regressions found by running the real pipeline:
- a stop must end the current step: no further CLI launch, no advance, no DONE, no auto-apply;
- a follow-up mission may change a file an earlier mission delivered, while a user edit still conflicts;
- a TRIAD greenfield experiment accepts the private snapshot that adds nothing to the qualified commit;
- `with connect_db(...)` closes the handle, so Windows can delete the DB file.
"""
import hashlib
import json
import os
import time
import subprocess
from pathlib import Path

import pytest

from core.bootstrap import snapshot
from core.db_path import connect_db
from core.mission import MissionRunner, MissionStore, stop_request
from core.process_runner import OperationStopped, ProcessTreeRunner, set_stop_guard
from core.research_experiment import TriadExperimentOrchestrator
from core.result_manager import ResultManager
from core.state_machine import State, StateMachine
from tests.test_v301_companion_http import http_server  # noqa: F401
from tests.test_v312_delivery import RUNS, cleanup_runs, fake_run, git, project  # noqa: F401


@pytest.fixture
def stopped_guard():
    set_stop_guard(lambda: "operator stop")
    yield
    set_stop_guard(None)


def _mission(tmp_path):
    store = MissionStore(tmp_path)
    m = {"mission_id": "MSN-STOP", "goal": "g", "project_path": "D:/p", "status": "RUNNING", "summary": "",
         "steps": [{"run_id": None}], "max_steps": 3, "max_consecutive_failures": 3, "owner_pid": None,
         "auto_apply": "on_pass"}
    store.save(m, force=True)
    return store, m


def test_stop_refuses_new_cli_launch(stopped_guard):
    with pytest.raises(OperationStopped):
        ProcessTreeRunner.run(["python", "--version"])


def test_stop_blocks_advance_and_pass_but_allows_ending(tmp_path, stopped_guard):
    sm = StateMachine(db_path=str(tmp_path / "s.db"))
    sm.create_run("RUN-S", "D:/p", "g")
    with pytest.raises(OperationStopped):
        sm.transition_to("RUN-S", State.PLANNING)
    sm.transition_to("RUN-S", State.FAIL, details="stopped")
    assert sm.get_run("RUN-S")["current_state"] == State.FAIL


def test_stopped_mission_is_not_finished_or_applied(tmp_path):
    store, m = _mission(tmp_path)
    applied, notes = [], []
    runner = MissionRunner(lambda: None, store=store, notify=lambda *a, **k: notes.append(a))
    runner._auto_apply = lambda mission: applied.append(mission["mission_id"])
    stop_request("MSN-STOP", "test", store=store)
    result = runner._finish(m, "DONE", summary="done")
    assert result["status"] == "STOPPED" and store.load("MSN-STOP")["status"] == "STOPPED"
    assert applied == [] and notes == []


def test_stop_survives_a_save_with_any_status(tmp_path):
    store, m = _mission(tmp_path)
    stop_request("MSN-STOP", "test", store=store)
    m["status"] = "DONE"
    store.save(m)
    assert store.load("MSN-STOP")["status"] == "STOPPED"


def test_mission_save_and_load_survive_a_locked_file(tmp_path, monkeypatch):
    import core.mission as mission_module
    store, m = _mission(tmp_path)
    real_replace, failures = os.replace, []

    def locked_twice(src, dst):
        if len(failures) < 2:
            failures.append(dst)
            raise PermissionError(5, "Access is denied")
        return real_replace(src, dst)
    monkeypatch.setattr(mission_module.os, "replace", locked_twice)
    monkeypatch.setattr(mission_module.time, "sleep", lambda _: None)
    m["summary"] = "saved"
    store.save(m)
    assert len(failures) == 2 and store.load("MSN-STOP")["summary"] == "saved"
    assert list(tmp_path.glob("*.tmp")) == []


def _delivered(proj, rid, parent_wt=None):
    wt = fake_run(proj, rid, snapshot(parent_wt or proj), base_worktree=str(parent_wt) if parent_wt else None)
    return wt


def _record_apply(rid, rel, source):
    """What apply_tree leaves behind for a run whose file was delivered to the project."""
    run_dir = RUNS / rid
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (run_dir / "result_state.json").write_text(json.dumps({"state": "APPLIED"}), encoding="utf-8")
    (run_dir / "result_manifest.json").write_text(json.dumps({"files": [{"path": rel, "sha256": digest}]}), encoding="utf-8")


def test_followup_may_change_a_delivered_file(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    cleanup_runs += ["RUN-F1", "RUN-F2"]
    first = _delivered(proj, "RUN-F1")
    (first / "new.py").write_text("one = 1\n", encoding="utf-8")
    (proj / "new.py").write_text("one = 1\n", encoding="utf-8")        # what auto-apply delivered
    _record_apply("RUN-F1", "new.py", first / "new.py")
    second = _delivered(proj, "RUN-F2", first)
    (second / "new.py").write_text("one = 1\ntwo = 2\n", encoding="utf-8")
    tree = ResultManager(config={}).tree_manifest("RUN-F2")
    assert tree["conflicts"] == []
    assert [m["path"] for m in tree["manifest"] if m["path"] == "new.py"] == ["new.py"]


def test_user_edit_of_a_delivered_file_still_conflicts(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    cleanup_runs += ["RUN-G1", "RUN-G2"]
    first = _delivered(proj, "RUN-G1")
    (first / "new.py").write_text("one = 1\n", encoding="utf-8")
    _record_apply("RUN-G1", "new.py", first / "new.py")
    (proj / "new.py").write_text("one = 'edited by the user'\n", encoding="utf-8")
    second = _delivered(proj, "RUN-G2", first)
    (second / "new.py").write_text("one = 1\ntwo = 2\n", encoding="utf-8")
    tree = ResultManager(config={}).tree_manifest("RUN-G2")
    assert [c["path"] for c in tree["conflicts"]] == ["new.py"]


def test_reverting_to_an_older_delivered_version_counts_as_a_user_edit(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    cleanup_runs += ["RUN-J1", "RUN-J2", "RUN-J3"]
    first = _delivered(proj, "RUN-J1")
    (first / "new.py").write_text("v = 'b'\n", encoding="utf-8")
    _record_apply("RUN-J1", "new.py", first / "new.py")
    second = _delivered(proj, "RUN-J2", first)
    (second / "new.py").write_text("v = 'c'\n", encoding="utf-8")
    _record_apply("RUN-J2", "new.py", second / "new.py")
    (proj / "new.py").write_text("v = 'b'\n", encoding="utf-8")        # the user went back to the older version
    third = _delivered(proj, "RUN-J3", second)
    (third / "new.py").write_text("v = 'd'\n", encoding="utf-8")
    tree = ResultManager(config={}).tree_manifest("RUN-J3")
    assert [c["path"] for c in tree["conflicts"]] == ["new.py"]


def test_apply_refuses_when_the_target_changed_after_the_manifest(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    cleanup_runs.append("RUN-H1")
    wt = _delivered(proj, "RUN-H1")
    (wt / "a.py").write_text("a = 2\n", encoding="utf-8")
    rm = ResultManager(config={"verification": {"test_command": None}})
    real = rm.tree_preconditions

    def edit_after_check(*args, **kwargs):
        pre = real(*args, **kwargs)
        (proj / "a.py").write_text("a = 'user edit during apply'\n", encoding="utf-8")
        return pre
    rm.tree_preconditions = edit_after_check
    result = rm.apply_tree("RUN-H1", request_id="r1")
    assert result["status"] != "applied"
    assert (proj / "a.py").read_text(encoding="utf-8") == "a = 'user edit during apply'\n"


def test_triad_prompts_state_what_the_director_enforces():
    from core.research_prompts import build_experiment_order_prompt, build_research_memory_prompt
    order = build_experiment_order_prompt("c", {"synthesized_hypothesis": "h"}, {}, "GREENFIELD", "NA",
                                          verification_command="python -m pytest tests/")
    assert order["prompt"]["verification_command"] == "python -m pytest tests/"
    assert "verification_command" in order["prompt"]["instructions"]
    memory = build_research_memory_prompt({"synthesized_hypothesis": "h"}, {}, {"A": {}, "B": {}}, {"A": {}, "B": {}})
    assert "exact character-for-character copy" in memory["prompt"]["instructions"]


def test_network_drop_is_retried_but_quota_is_not():
    from core.research_runner import TriadResearchRunner
    assert TriadResearchRunner._is_retryable(
        'GOOGLE_STREAM_FAILED: request failed: read tcp 1.2.3.4:1->5.6.7.8:443: wsarecv: A connection attempt failed')
    assert not TriadResearchRunner._is_retryable("GOOGLE_STREAM_FAILED: quota exhausted")


def _commit(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", *args], cwd=repo, check=True)


def _rev(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def test_greenfield_accepts_only_a_snapshot_with_unchanged_content(tmp_path):
    repo = tmp_path / "g"
    repo.mkdir()
    git(repo, "init", "-q")
    _commit(repo, "--allow-empty", "-m", "start")
    start = _rev(repo)
    same = TriadExperimentOrchestrator._same_content_as
    assert same(str(repo), start, start)
    _commit(repo, "--allow-empty", "-m", "snapshot")
    assert same(str(repo), _rev(repo), start)
    (repo / "x.py").write_text("x = 1\n", encoding="utf-8")
    git(repo, "add", "-A")
    _commit(repo, "-m", "content")
    assert not same(str(repo), _rev(repo), start)
    assert not same(str(repo), None, start)


def test_connection_closes_after_with_and_keeps_the_commit(tmp_path):
    db = tmp_path / "c.db"
    with connect_db(db) as conn:
        conn.execute("CREATE TABLE t (v INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
    with connect_db(db) as conn:
        assert conn.execute("SELECT v FROM t").fetchone()[0] == 1
    os.unlink(db)
    assert not Path(db).exists()


def test_stopping_one_mission_kills_only_its_own_processes():
    import sys
    import threading
    from core.process_runner import active_processes

    def run_as(owner):
        set_stop_guard(lambda: None, owner=owner)
        try:
            ProcessTreeRunner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
        finally:
            set_stop_guard(None)
    threads = [threading.Thread(target=run_as, args=(o,), daemon=True) for o in ("MSN-A", "MSN-B")]
    for t in threads:
        t.start()
    deadline = time.time() + 15
    while len(active_processes()) < 2 and time.time() < deadline:
        time.sleep(0.1)
    before = set(active_processes())
    killed = ProcessTreeRunner.kill_active(owners=["MSN-A"])
    assert len(killed) == 1 and len(before) == 2
    survivor = set(active_processes())
    assert len(survivor) == 1 and survivor <= before and not (survivor & set(killed))
    ProcessTreeRunner.kill_active()
    for t in threads:
        t.join(timeout=15)


def test_runs_go_to_the_test_folder_not_the_source_tree():
    from core.db_path import SOURCE_ROOT, get_runs_dir
    assert get_runs_dir() != SOURCE_ROOT / "runs"
    assert ResultManager.run_dir("RUN-X").parent == get_runs_dir()


def test_test_runs_folder_only_applies_in_test_mode(monkeypatch):
    from core.db_path import SOURCE_ROOT, get_runs_dir
    monkeypatch.delenv("DIRECTOR_TESTING", raising=False)
    assert get_runs_dir() == SOURCE_ROOT / "runs"


def test_stalled_oversize_body_gets_a_quick_rejection(http_server):
    import socket
    sock = socket.create_connection(http_server.server_address, timeout=10)
    host = b"Host: 127.0.0.1:%d\r\n" % http_server.server_address[1]
    sock.sendall(b"POST /bridge/orders HTTP/1.1\r\n" + host + b"X-Solomon-Bridge: test-token\r\n"
                 b"Content-Type: application/json\r\nContent-Length: 40000\r\n\r\n{\"a\":")   # then stalls
    started = time.time()
    reply = sock.recv(200)
    sock.close()
    assert reply.startswith(b"HTTP/1.") and b" 400 " in reply.split(b"\r\n")[0]
    assert time.time() - started < 8
