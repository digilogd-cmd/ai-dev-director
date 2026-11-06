"""
v0.2.4.7 IDLE PERFORMANCE + KOREAN UX — /status spawns no subprocess and scans no run list, persisted
human_action_state / result_state tables, 2 s cache, and a Korean-first presentation layer with raw codes kept
in data-raw / Details.
"""
from core.db_path import get_runs_dir
import json
import time
import sqlite3
import statistics
import subprocess
from pathlib import Path

import pytest

import dashboard_server
import dashboard_i18n as I18N
from dashboard_server import render_html, build_status_payload, cached_status_payload
from core.state_machine import StateMachine, State
from core.result_manager import ResultManager
from core.db_path import get_db_path
from tests.test_v0238_dashboard_ux import env, server, _get_json, _post  # noqa: F401


# ------------------------------------------------------------------ 1-3: mappings

def test_1_status_and_state_mapping():
    assert I18N.status_ko("HEALTHY") == "정상" and I18N.status_ko("RECOVERING") == "복구 중"
    assert I18N.status_ko("DEGRADED") == "일부 기능 제한" and I18N.status_ko("STOP") == "실행 중지"
    assert I18N.state_ko("BUILDING") == "코드 작성 중" and I18N.state_ko("VERIFYING") == "테스트·검증 중"
    assert I18N.state_ko("REVIEWING") == "코드 검토 중" and I18N.state_ko("DECIDING") == "최종 판단 중"
    assert I18N.state_ko("PASS") == "완료" and I18N.state_ko("HUMAN_REQUIRED") == "사용자 확인 필요"
    assert I18N.result_ko("APPLIED") == "적용 완료" and I18N.result_ko("DISCARDED") == "폐기됨" and I18N.result_ko("READY") == "적용 대기"   # v0.2.4.8 terminology pass
    assert I18N.role_ko("HEAD, REVIEWER") == "HEAD · REVIEWER (작업 지휘 · 코드 검토)"
    assert I18N.status_ko("SOMETHING_NEW") == "SOMETHING_NEW"                 # unknown enum passes through unchanged


def test_2_reason_mapping():
    assert I18N.reason_ko("SENSITIVE_FILE_IN_SCOPE")[0] == "의존성·환경 설정 파일 변경 승인 필요"
    assert "실행 환경이나 의존성" in I18N.reason_ko("SENSITIVE_FILE_IN_SCOPE")[1]
    assert I18N.reason_ko("DEPENDENCY_CONFIGURATION_MODIFIED")[1].startswith("의존성 설정 파일이 실제로 변경")
    assert "작업 폴더 밖" in I18N.reason_ko("CANONICAL_CONTAINMENT")[1]
    assert "나빠져" in I18N.reason_ko("REGRESSION")[1]
    assert "작업 사본" in I18N.reason_ko("WORKTREE_EVIDENCE_MISSING")[1]
    assert "신뢰할 수 없어" in I18N.reason_ko("REVIEWER_FAIL_CLOSED")[1]
    assert "보호 설정 파일" in I18N.reason_ko("CONTROL_PLANE_MODIFIED")[1]
    assert "Git 내부" in I18N.reason_ko("GIT_INTERNALS_IN_SCOPE")[1]
    e = I18N.explain_reason("Scope violation: Dependency configuration modified: requirements.txt")
    assert e["code"] == "DEPENDENCY_CONFIGURATION_MODIFIED" and e["raw"].startswith("Scope violation")
    assert I18N.explain_reason("FINAL_GATE: PASS refused, CANONICAL_CONTAINMENT=FAIL")["code"] == "CANONICAL_CONTAINMENT"   # most specific code wins
    assert I18N.explain_reason("Main project changed since this run was created")["code"] == "BASE_COMMIT_MISMATCH"


def test_3_unknown_reason_safe_fallback():
    e = I18N.explain_reason("SOMETHING_COMPLETELY_NEW happened: xyz")
    assert e["code"] == "UNKNOWN" and e["title"] == "자동 판단 불가" and "상세 정보를 확인" in e["why"]
    assert e["raw"] == "SOMETHING_COMPLETELY_NEW happened: xyz"                  # raw kept for Details
    assert I18N.event_ko("totally unexpected system text") == ("· 시스템 알림", "ev-info")


# ------------------------------------------------------------------ 4-9: page/status texts

def _set(dom, status, role=None):
    with sqlite3.connect(str(get_db_path())) as c:
        c.execute("UPDATE provider_health SET status=? WHERE provider=?", (status, dom))
        if role is not None:
            c.execute("UPDATE provider_health SET current_role=? WHERE provider=?", (role, dom))
        c.commit()


def _run(rid, chain, details="x", project="C:/p"):
    sm = StateMachine(); sm.create_run(rid, project, "g")
    for st in chain:
        sm.transition_to(rid, st, details=details)
    return sm


def test_4_provider_status_display(server, env):
    _set("OPENAI", "HEALTHY", "HEAD, REVIEWER"); _set("ANTHROPIC", "RECOVERING", "BACKUP"); _set("GOOGLE", "HEALTHY", "BUILDER")
    html = render_html()
    assert 'id="prov-OPENAI-status" data-raw="HEALTHY">정상</td>' in html
    assert 'id="prov-ANTHROPIC-status" data-raw="RECOVERING">복구 중</td>' in html
    assert 'id="system-mode" data-raw="DEGRADED">일부 기능 제한</span>' in html
    assert "Claude 백업 AI가 복구 중입니다." in html and "정상 사용 가능합니다." in html
    assert "HEAD · REVIEWER (작업 지휘 · 코드 검토)" in html and "BACKUP (예비 AI)" in html
    assert "OpenAI" in html and "Google" in html and "Anthropic" in html                 # vendor labels kept
    st = _get_json(server, "/status")[1]
    assert st["system_mode_sentence"].startswith("Claude 백업 AI가 복구 중입니다.")
    for dom in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        _set(dom, "HEALTHY")
    assert 'data-raw="NORMAL">정상</span>' in render_html()
    _set("OPENAI", "UNAVAILABLE"); _set("ANTHROPIC", "UNAVAILABLE")
    assert 'data-raw="STOP">실행 중지</span>' in render_html()


def test_5_6_building_and_reviewing_sentences(server, env):
    _set("OPENAI", "HEALTHY", "HEAD, REVIEWER"); _set("GOOGLE", "HEALTHY", "BUILDER"); _set("ANTHROPIC", "RECOVERING", "BACKUP")
    _run("RUN-KO-1", [State.PLANNING, State.ORDER_READY, State.BUILDING])
    st = _get_json(server, "/status")[1]
    assert st["stage_sentence"] == "Antigravity가 코드를 작성하고 있습니다" and st["next_step"] == "테스트 및 안전 검증"
    html = render_html()
    assert "Antigravity가 코드를 작성하고 있습니다" in html and "다음 단계: 테스트 및 안전 검증" in html
    assert 'id="run-state" data-raw="BUILDING">코드 작성 중</span>' in html and 'id="core-stage" data-raw="ORCHESTRATING · BUILDER">지휘 중 · 코드 작성' in html
    sm = StateMachine(); sm.transition_to("RUN-KO-1", State.VERIFYING); sm.transition_to("RUN-KO-1", State.REVIEWING)
    st = _get_json(server, "/status")[1]
    assert st["stage_sentence"] == "Codex가 변경 내용을 검토하고 있습니다" and st["next_step"] == "최종 판단"


def test_7_human_required_display(server, env):
    _run("RUN-KO-2", [State.PLANNING, State.HUMAN_REQUIRED], details="APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt)")
    (get_runs_dir() / "RUN-KO-2").mkdir(parents=True, exist_ok=True)
    ((get_runs_dir() / "RUN-KO-2") / "order.json").write_text(json.dumps({"goal": "g", "allowed_paths": ["requirements.txt"], "protected_paths": []}), encoding="utf-8")
    html = render_html()
    assert 'id="hdr-human" class="badge b-red" data-raw="HUMAN REQUIRED">사용자 확인 필요</span>' in html
    assert "사용자 승인이 필요합니다." in html and "requirements.txt" in html
    assert "실행 환경이나 의존성에 영향을 줄 수 있는 파일" in html
    assert "Technical reason: SENSITIVE_FILE_IN_SCOPE" in html                     # raw code kept in the small details line
    assert "이 작업을 계속하려면 다음 내용을 확인해주세요." in html
    st = _get_json(server, "/status")[1]
    assert st["todo"] == "requirements.txt 변경 승인 필요" and st["human_required_count"] == 1
    assert st["pending"][0]["title"] == "의존성·환경 설정 파일 변경 승인 필요" and st["pending"][0]["title_en"] == "Sensitive file in scope"
    # non-approvable: clear Korean explanation, no approve button
    _run("RUN-KO-3", [State.PLANNING, State.HUMAN_REQUIRED], details="APPROVAL_REQUIRED: GIT_INTERNALS_IN_SCOPE (.git/hooks/x)")
    html = render_html()
    assert "승인할 수 없는 안전 문제" in html and "Git 내부 관리 영역(.git)은 프로젝트의 Git 상태를 관리하는 보호 영역이므로 AI가 수정할 수 없습니다" in html
    assert 'id="approve-btn" class="approve" style="display:none;"' in html


def test_8_9_pass_and_result_ready(server, env):
    _set("OPENAI", "HEALTHY", "HEAD, REVIEWER"); _set("GOOGLE", "HEALTHY", "BUILDER"); _set("ANTHROPIC", "RECOVERING", "BACKUP")
    sm = _run("RUN-KO-4", [State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.PASS])
    d = (get_runs_dir() / "RUN-KO-4"); d.mkdir(parents=True, exist_ok=True)
    (d / "final_evidence.json").write_text(json.dumps({"reasons": [], "reviewer_verdict": "pass", "regression": "PASS", "canonical_containment": "PASS"}), encoding="utf-8")
    (d / "verification.json").write_text(json.dumps({"passed": True, "test_counts": {"passed": 3}}), encoding="utf-8")
    sm.upsert_result_state("RUN-KO-4", state="READY", changed_count=3, counts_json={"A": 1, "M": 2, "D": 0}, tests_state="PASS", reviewer_state="PASS", safety_state="PASS", worktree_state="PRESENT")
    with sqlite3.connect(str(get_db_path())) as c:
        c.execute("UPDATE runs SET worktree_path=? WHERE run_id=?", (str(get_runs_dir()), "RUN-KO-4")); c.commit()
    st = _get_json(server, "/status")[1]
    assert st["stage_sentence"] == "작업이 완료되었습니다" and st["result"]["is_pass"] and st["result"]["changed_count"] == 3
    html = render_html()
    assert "결과물이 준비되었습니다." in html and 'data-raw="RESULT READY"' in html
    assert "변경 파일 3개" in html and "테스트 통과" in html and "코드 검토 통과" in html and "안전 검사 통과" in html
    assert "프로젝트에 적용하고 검증" in html and "결과 폐기" in html and "변경사항 보기" in html and "테스트 결과" in html and "작업 결과 폴더 열기" in html
    assert 'data-raw="PASS">완료' in html and "작업이 완료되었습니다 · 결과물이 준비되었습니다" in html
    assert "결과물 1건" in html and 'data-raw="RESULTS (1)"' in html


# ------------------------------------------------------------------ 10-11: confirmation / blocked texts (page script + i18n bundle)

def test_10_11_apply_confirmation_and_blocked_texts(env):
    html = render_html()
    b = I18N.i18n_bundle()
    assert b["ui"]["apply_confirm_title"] == "프로젝트에 결과를 적용할까요?" and "커밋이나 Push는 자동으로 하지 않습니다." in b["ui"]["apply_confirm_body"]
    assert "적용 전 상태로 자동 복구합니다." in b["ui"]["apply_confirm_body"] and b["ui"]["apply_confirm_ok"] == "적용하고 검증"
    assert b["reasons"]["BASE_COMMIT_MISMATCH"]["why"].startswith("작업이 시작된 뒤 원본 프로젝트가 변경되었습니다")
    assert '"apply_confirm_title": "프로젝트에 결과를 적용할까요?"' in html            # bundle embedded for the page script
    assert "BASE_COMMIT_MISMATCH" in dashboard_server.DASHBOARD_JS and "상세 코드" in dashboard_server.DASHBOARD_JS


# ------------------------------------------------------------------ 12-13: UTF-8 / no replacement characters

def test_12_13_utf8_and_no_replacement_chars(server, env):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{server}/", timeout=5) as r:
        raw = r.read()
        assert "charset=utf-8" in r.headers.get("Content-type", "").lower()
    html = raw.decode("utf-8")
    assert "\ufffd" not in html and "시스템 상태" in html and "HEAD 대화" in html
    with urllib.request.urlopen(f"http://127.0.0.1:{server}/status", timeout=5) as r:
        st = json.loads(r.read().decode("utf-8"))
    assert "\ufffd" not in json.dumps(st, ensure_ascii=False)
    for v in (I18N.UI_TEXT, I18N.STATUS_TEXT, I18N.STATE_TEXT, I18N.RESULT_TEXT):
        assert all("\ufffd" not in x for x in v.values())


# ------------------------------------------------------------------ 16-18: performance

@pytest.fixture
def big_db(env):
    """3,000+ runs, 12 PASS results with persisted summaries, 6 HUMAN_REQUIRED (3 actionable), like production."""
    sm = StateMachine()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with sqlite3.connect(str(get_db_path())) as c:
        rows = [(f"RUN-BIG-{i:05d}", "C:/p", "g", "PASS" if i % 7 else "FAIL", None, None, f"2026-01-01T00:{i//60:02d}:{i%60:02d}", f"2026-01-01T00:{i//60:02d}:{i%60:02d}") for i in range(3000)]
        c.executemany("INSERT INTO runs (run_id, project_path, goal, current_state, worktree_path, branch_name, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)", rows)
        c.executemany("INSERT INTO state_transitions (run_id, from_state, to_state, details, timestamp) VALUES (?,?,?,?,?)",
                      [(r[0], "DECIDING", r[3], "Final decision: ok", r[6]) for r in rows])
        c.commit()
    wt = get_runs_dir()
    for i in range(12):
        rid = f"RUN-BIGPASS-{i}"
        sm.create_run(rid, str(Path.cwd()), "g")
        for st in (State.PLANNING, State.ORDER_READY, State.BUILDING, State.VERIFYING, State.REVIEWING, State.DECIDING, State.PASS):
            sm.transition_to(rid, st)
        with sqlite3.connect(str(get_db_path())) as c:
            c.execute("UPDATE runs SET worktree_path=? WHERE run_id=?", (str(wt), rid)); c.commit()
        sm.upsert_result_state(rid, state="READY", changed_count=2, counts_json={"A": 1, "M": 1, "D": 0}, tests_state="PASS", reviewer_state="PASS", safety_state="PASS", worktree_state="PRESENT")
    for i in range(6):
        rid = f"RUN-BIGHR-{i}"
        sm.create_run(rid, str(Path.cwd()), "g")
        sm.transition_to(rid, State.PLANNING)
        sm.transition_to(rid, State.HUMAN_REQUIRED, details=("APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt)" if i < 3 else "FINAL_GATE: refused"))
        (get_runs_dir() / f"{rid}").mkdir(parents=True, exist_ok=True)
        ((get_runs_dir() / f"{rid}") / "order.json").write_text(json.dumps({"goal": "g", "allowed_paths": ["requirements.txt"], "protected_paths": []}), encoding="utf-8")
    return sm


def test_16_17_status_no_subprocess_no_full_scan(big_db, monkeypatch):
    import core.result_manager as R
    spawns = []
    def no_spawn(*a, **k):
        spawns.append(a); raise AssertionError("subprocess during /status")
    monkeypatch.setattr(subprocess, "Popen", no_spawn); monkeypatch.setattr(subprocess, "run", no_spawn)
    monkeypatch.setattr(R.subprocess, "Popen", no_spawn); monkeypatch.setattr(R.subprocess, "run", no_spawn)
    scans = []
    real_list_runs = StateMachine.list_runs
    monkeypatch.setattr(StateMachine, "list_runs", lambda self: (scans.append(1), real_list_runs(self))[1])
    # SQL trace: count queries per /status
    counts = []
    real_connect = sqlite3.connect
    def traced_connect(*a, **k):
        conn = real_connect(*a, **k)
        conn.set_trace_callback(lambda q: counts.append(q))
        return conn
    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    dashboard_server.STATUS_CACHE_TTL_SEC = 0.0
    lat = []
    for i in range(100):
        counts.clear()
        t0 = time.perf_counter(); p = build_status_payload(); lat.append(time.perf_counter() - t0)
        if i == 5:
            q_per_status = len([q for q in counts if q.strip().upper().startswith("SELECT")])
    assert spawns == [] and scans == []                                          # no subprocess, no Python full scan
    assert p["human_required_count"] == 3 and p["pending_result_count"] == 12
    assert all(r["run_id"].startswith("RUN-BIGHR-") for r in p["pending"])
    p95 = sorted(lat)[94]
    print(f"\n/status over 3,018 runs: avg {statistics.mean(lat)*1000:.1f} ms, p95 {p95*1000:.1f} ms, SELECTs per status: {q_per_status}")
    assert p95 < 0.5 and q_per_status < 60
    # EXPLAIN QUERY PLAN uses the new indexes (no full scan of runs for the pending queries)
    with real_connect(str(get_db_path())) as c:
        plan = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN SELECT r.run_id FROM result_state s JOIN runs r ON r.run_id = s.run_id WHERE s.state='READY' AND r.current_state='PASS'"))
        assert "idx_result_state" in plan or "USING INDEX" in plan.upper() or "PRIMARY KEY" in plan.upper()
        plan2 = " ".join(r[3] for r in c.execute("EXPLAIN QUERY PLAN SELECT r.run_id FROM runs r LEFT JOIN human_action_state h ON h.run_id=r.run_id WHERE r.current_state='HUMAN_REQUIRED' AND (h.run_id IS NULL OR h.needs_classify=1)"))
        assert "idx_runs_state" in plan2 or "idx_runs_project_state" in plan2


def test_18_status_cache_ttl(big_db):
    dashboard_server.STATUS_CACHE_TTL_SEC = 2.0
    dashboard_server._STATUS_CACHE.update(at=0.0, payload=None)
    a = cached_status_payload(); b = cached_status_payload()
    assert a is b                                                                # served from cache within TTL
    dashboard_server.invalidate_status_cache()
    assert cached_status_payload() is not a
    dashboard_server.STATUS_CACHE_TTL_SEC = 0.0


def test_19_pending_action_state_is_persisted_and_resolved(env):
    sm = _run("RUN-HA-1", [State.PLANNING, State.HUMAN_REQUIRED], details="APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt)")
    (get_runs_dir() / "RUN-HA-1").mkdir(parents=True, exist_ok=True)
    ((get_runs_dir() / "RUN-HA-1") / "order.json").write_text(json.dumps({"goal": "g", "allowed_paths": ["requirements.txt"], "protected_paths": []}), encoding="utf-8")
    assert sm.stale_human_actions() == ["RUN-HA-1"]
    assert [p["run_id"] for p in dashboard_server.pending_actions()] == ["RUN-HA-1"]
    assert sm.stale_human_actions() == []                                         # classified once, persisted
    sm.mark_human_action_resolved("RUN-HA-1")
    assert dashboard_server.pending_actions() == []
    # a new HUMAN_REQUIRED transition re-opens classification automatically
    sm2 = _run("RUN-HA-2", [State.PLANNING, State.HUMAN_REQUIRED], details="FINAL_GATE: refused")
    assert sm2.stale_human_actions() == ["RUN-HA-2"]
    assert dashboard_server.pending_actions() == [] and sm2.stale_human_actions() == []   # non-approvable -> persisted as not actionable
