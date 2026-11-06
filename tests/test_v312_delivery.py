"""
v3.2 DELIVERY: a mission's finished worktree is applied to the project file by file (new / changed-by-the-AIs /
conflict-with-the-user), chains of milestones resolve to the project snapshot they started from, and the finished
mission applies itself under the auto_apply policy. Plus the goal-level bridge Solomon uses and execution profiles.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core import autopilot
from core.bootstrap import snapshot
from core.git_manager import GitManager
from core.result_manager import ResultManager
from core.state_machine import StateMachine, State
from tests.test_v301_companion_http import http_server, request  # noqa: F401

from core.db_path import get_runs_dir
RUNS = get_runs_dir()


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    git(p, "init", "-q"); git(p, "config", "user.email", "t@t"); git(p, "config", "user.name", "t")
    (p / "a.py").write_text("a = 1\n", encoding="utf-8")
    (p / "b.py").write_text("b = 1\n", encoding="utf-8")
    git(p, "add", "-A"); git(p, "commit", "-qm", "base")
    (p / "u.txt").write_text("untracked original\n", encoding="utf-8")        # never committed by the user
    return p


def fake_run(proj, run_id, base_ref, base_worktree=None, evidence_ok=True, state=State.PASS):
    sm = StateMachine()
    sm.create_run(run_id, str(proj), "goal")
    wt, branch = GitManager.create_worktree(str(proj), run_id, base_ref=base_ref)
    sm.update_run_worktree(run_id, wt, branch)
    for nxt in (State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, state):
        sm.transition_to(run_id, nxt, details="t")
    d = RUNS / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "source_snapshot.json").write_text(json.dumps({"base_ref": base_ref, "source_head_unchanged": True, "base_worktree": base_worktree}), encoding="utf-8")
    if evidence_ok:
        (d / "final_evidence.json").write_text(json.dumps({"reasons": [], "canonical_containment": "PASS", "verification": "PASS",
                                                           "baseline": "PASS", "regression": "PASS", "reviewer_verdict": "pass"}), encoding="utf-8")
        (d / "verification.json").write_text(json.dumps({"test_counts": {"passed": 0}}), encoding="utf-8")
    return Path(wt)


@pytest.fixture
def cleanup_runs():
    made = []
    yield made
    for rid in made:
        shutil.rmtree(RUNS / rid, ignore_errors=True)


def test_tree_manifest_classifies_new_changed_unchanged_and_conflicting_files(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    base = snapshot(str(proj))                                   # includes the untracked u.txt
    rid = "RUN-TEST-DLV1"; cleanup_runs.append(rid)
    wt = fake_run(proj, rid, base)
    (wt / "a.py").write_text("a = 2\n", encoding="utf-8")        # AI changed
    (wt / "u.txt").write_text("untracked edited by AI\n", encoding="utf-8")
    (wt / "new.py").write_text("new = 1\n", encoding="utf-8")   # AI added
    (wt / "b.py").write_text("b = 2\n", encoding="utf-8")        # AI changed ...
    (proj / "b.py").write_text("b = 99\n", encoding="utf-8")     # ... and so did the user: conflict
    (proj / "mine.md").write_text("user file\n", encoding="utf-8")   # user-only file: irrelevant
    tree = ResultManager().tree_manifest(rid)
    kinds = {m["path"]: m["type"] for m in tree["manifest"]}
    assert kinds == {"a.py": "M", "u.txt": "M", "new.py": "A"}
    assert [c["path"] for c in tree["conflicts"]] == ["b.py"]
    assert tree["origin_ref"] == base and tree["deleted"] == []


def test_apply_tree_delivers_into_the_project_and_blocks_on_conflicts(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    base = snapshot(str(proj))
    rid = "RUN-TEST-DLV2"; cleanup_runs.append(rid)
    wt = fake_run(proj, rid, base)
    (wt / "a.py").write_text("a = 2\n", encoding="utf-8")
    (wt / "new.py").write_text("new = 1\n", encoding="utf-8")
    (wt / "b.py").write_text("b = 2\n", encoding="utf-8")
    (proj / "b.py").write_text("b = 99\n", encoding="utf-8")
    rm = ResultManager(config={"verification": {"test_command": None}})
    blocked = rm.apply_tree(rid, request_id="dlv2-a")
    assert blocked["status"] == "blocked" and any("Conflicts" in b for b in blocked["blocks"])
    assert (proj / "a.py").read_text(encoding="utf-8") == "a = 1\n"            # nothing written when blocked
    done = rm.apply_tree(rid, request_id="dlv2-b", on_conflict="skip")
    assert done["status"] == "applied" and sorted(done["files"]) == ["a.py", "new.py"]
    assert (proj / "a.py").read_text(encoding="utf-8") == "a = 2\n" and (proj / "new.py").exists()
    assert (proj / "b.py").read_text(encoding="utf-8") == "b = 99\n"           # the user's edit survives
    assert [c["path"] for c in done["skipped_conflicts"]] == ["b.py"]
    assert rm.apply_tree(rid, request_id="dlv2-c")["status"] == "blocked"      # already applied


def test_milestone_chain_resolves_to_the_project_snapshot_it_started_from(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    base = snapshot(str(proj))
    r1, r2 = "RUN-TEST-DLV3A", "RUN-TEST-DLV3B"; cleanup_runs += [r1, r2]
    wt1 = fake_run(proj, r1, base)
    (wt1 / "a.py").write_text("a = 2\n", encoding="utf-8")                    # milestone 1's change
    base2 = snapshot(str(wt1))                                                # milestone 2 builds on milestone 1
    wt2 = fake_run(proj, r2, base2, base_worktree=str(wt1))
    (wt2 / "new.py").write_text("new = 1\n", encoding="utf-8")                # milestone 2's change
    rm = ResultManager()
    assert rm.origin_ref(r2) == base
    kinds = {m["path"]: m["type"] for m in rm.tree_manifest(r2)["manifest"]}
    assert kinds == {"a.py": "M", "new.py": "A"}                                # both milestones are delivered


def test_apply_tree_requires_full_pass_evidence(tmp_path, cleanup_runs):
    proj = project(tmp_path)
    rid = "RUN-TEST-DLV4"; cleanup_runs.append(rid)
    wt = fake_run(proj, rid, snapshot(str(proj)), evidence_ok=False)
    (wt / "a.py").write_text("a = 2\n", encoding="utf-8")
    res = ResultManager().apply_tree(rid, request_id="dlv4")
    assert res["status"] == "blocked" and any("final_evidence" in b for b in res["blocks"])


def test_profiles_and_tags():
    assert autopilot.split_profile_tag("[빠르게] 캡션 검사기") == ("quick", "캡션 검사기")
    assert autopilot.split_profile_tag("  [deep] big goal") == ("deep", "big goal")
    assert autopilot.split_profile_tag("plain goal") == (None, "plain goal")
    assert autopilot.profile_overrides({}, "quick")["max_steps"] == 2
    assert autopilot.profile_overrides({"autonomy": {"profiles": {"quick": {"max_steps": 3}}}}, "quick")["max_steps"] == 3
    assert autopilot.profile_overrides({}, None) == {} and autopilot.profile_overrides({}, "nope") == {}


def test_mission_records_profile_source_and_auto_apply_policy(tmp_path):
    from core.mission import MissionRunner, MissionStore, deliverable_run_id
    from schemas.order import OrderSchema
    runner = MissionRunner(lambda: None, MissionStore(tmp_path / "missions"))
    m = runner.create(str(tmp_path / "p"), "g", OrderSchema(goal="g", allowed_paths=["src/**"]), config={"autonomy": {"auto_apply": "on_done"}},
                      profile="quick", source="solomon", request_id="req-12345678")
    assert m["max_steps"] == 2 and m["profile"] == "quick" and m["source"] == "solomon" and m["auto_apply"] == "on_done"
    assert deliverable_run_id(m) is None
    m["base_worktree"] = "X/.director_worktrees/RUN-Z"
    m["steps"][0].update(run_id="RUN-Z", result={"final_state": "PASS", "worktree_path": "X/.director_worktrees/RUN-Z"})
    assert deliverable_run_id(m) == "RUN-Z"


# ------------------------------------------------------------------ bridge v2 (Solomon -> goal-level missions)

def test_bridge_missions_need_the_token_and_respect_the_allowlist(http_server, monkeypatch):
    import companion_server
    monkeypatch.setattr(companion_server.d, "load_config", lambda: {"solomon": {"allowed_roots": ["E:/work"], "denied": ["E:/work/SOLOMON"]}, "projects": {}})
    assert request(http_server, "GET", "/bridge/projects")[0] == 403
    body = json.dumps({"request_id": "solomon-req-0001", "project_path": "C:/elsewhere/proj", "goal": "make a thing"})
    code, data = request(http_server, "POST", "/bridge/missions", body=body, headers={"X-Solomon-Bridge": "test-token", "Content-Type": "application/json"})
    assert code == 400 and "허용된 폴더 밖" in json.loads(data)["error"]
    body = json.dumps({"request_id": "solomon-req-0002", "project_path": "E:/work/SOLOMON", "goal": "modify yourself"})
    code, data = request(http_server, "POST", "/bridge/missions", body=body, headers={"X-Solomon-Bridge": "test-token", "Content-Type": "application/json"})
    assert code == 400 and "지시할 수 없" in json.loads(data)["error"]


def test_bridge_mission_start_decide_and_duplicate_request(http_server, monkeypatch, tmp_path):
    import companion_server
    from core.mission import MissionStore
    monkeypatch.setattr(companion_server.d, "load_config", lambda: {"solomon": {"allowed_roots": [str(tmp_path)], "denied": []}, "projects": {}})
    target = tmp_path / "game"; target.mkdir()
    calls = []
    monkeypatch.setattr(companion_server.d, "start_goal_mission", lambda path, goal, **kw: calls.append((path, goal, kw)) or {"status": "started", "mission_id": "MSN-X"})
    hdr = {"X-Solomon-Bridge": "test-token", "Content-Type": "application/json"}
    body = json.dumps({"request_id": "solomon-req-1234", "project_path": str(target), "goal": "[빠르게] 데일리 미션 추가", "profile": "quick"})
    code, data = request(http_server, "POST", "/bridge/missions", body=body, headers=hdr)
    assert code == 202 and json.loads(data)["mission_id"] == "MSN-X"
    assert calls[0][2]["source"] == "solomon" and calls[0][2]["request_id"] == "solomon-req-1234" and calls[0][2]["profile"] == "quick"
    # a retry with the same request_id finds the mission instead of starting another
    store = MissionStore()
    m = {"mission_id": "MSN-X", "project_path": str(target), "goal": "g", "status": "RUNNING", "steps": [], "request_id": "solomon-req-1234",
         "updated_at": "2026-10-24T00:00:00", "created_at": "2026-10-24T00:00:00", "max_steps": 2}
    store.save(m)
    code, data = request(http_server, "POST", "/bridge/missions", body=body, headers=hdr)
    assert code == 200 and json.loads(data)["status"] == "duplicate" and len(calls) == 1
    code, data = request(http_server, "GET", "/bridge/missions/MSN-X", headers=hdr)
    assert code == 200 and json.loads(data)["source"] in ("chat", "solomon") and json.loads(data)["mission_id"] == "MSN-X"
    decided = []
    monkeypatch.setattr(companion_server.d, "decide_mission", lambda mid, action, **kw: decided.append((mid, action, kw)) or {"status": "applied", "files": ["a.py"]})
    code, data = request(http_server, "POST", "/bridge/missions/MSN-X/decide", body=json.dumps({"action": "apply", "on_conflict": "skip"}), headers=hdr)
    assert code == 200 and decided[0][0] == "MSN-X" and decided[0][2]["by"] == "SOLOMON" and decided[0][2]["on_conflict"] == "skip"
    code, data = request(http_server, "GET", "/bridge/digest", headers=hdr)
    assert code == 200 and "awaiting_decision" in json.loads(data)
