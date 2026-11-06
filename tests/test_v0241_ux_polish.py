"""
v0.2.4.1 DASHBOARD UX POLISH PASS 2 — New Chat (history preserved), UTF-8 end-to-end, mode hint,
recovery summary, compact SYSTEM events, provider cards without clipping.
"""
import json
import threading
import urllib.parse
import urllib.request

import pytest

import dashboard_server
from dashboard_server import (render_html, active_session_id, new_chat_session, GLOBAL_SESSION_ID,
                              DashboardServer, _mode_hint)
from core.state_machine import StateMachine
from core.chat_manager import ChatManager
from tests.test_v0238_dashboard_ux import env, server, _get_json, _post, SlowHead  # noqa: F401 (fixtures)


KOREAN = "calc.py에 multiply(a, b) 함수를 추가하고 테스트도 추가해서 통과시켜줘. — ✓ 한글/emoji 😀"


# ------------------------------------------------------------------ New Chat

def test_new_chat_keeps_history_and_project(server, env, monkeypatch):
    env.add_message(GLOBAL_SESSION_ID, "USER", "old question")
    env.add_message(GLOBAL_SESSION_ID, "HEAD", "old answer")
    env.set_project(GLOBAL_SESSION_ID, "C:/proj/x")
    before = env.get_messages(GLOBAL_SESSION_ID)
    assert active_session_id() == GLOBAL_SESSION_ID
    code, j = _post(server, "/chat/new", {})
    assert code == 200 and j["status"] == "ok" and j["previous_session_id"] == GLOBAL_SESSION_ID
    new_id = j["session_id"]
    assert new_id != GLOBAL_SESSION_ID and active_session_id() == new_id
    # old history untouched, still retrievable by explicit session id
    assert env.get_messages(GLOBAL_SESSION_ID) == before
    code, old = _get_json(server, f"/chat/messages?after_id=0&session_id={GLOBAL_SESSION_ID}")
    assert [m["content"] for m in old["messages"]] == ["old question", "old answer"]
    # the page / default poll shows only the new session (one NEW_CHAT event)
    code, cur = _get_json(server, "/chat/messages?after_id=0")
    assert cur["session_id"] == new_id and [m["role"] for m in cur["messages"]] == ["SYSTEM"]
    html = render_html()
    assert "old question" not in html and "NEW_CHAT" in html
    # project preserved in the new session; new messages go to the new session
    assert (env.get_session(new_id) or {}).get("project_path") == "C:/proj/x"
    assert _get_json(server, "/status")[1]["project_path"] == "C:/proj/x"
    monkeypatch.setattr(dashboard_server, "handle_chat_message", SlowHead(env, delay=0.0))
    # SlowHead writes to GLOBAL_SESSION_ID directly; the real handler resolves active_session_id() -> check via set_project path
    from core.bootstrap import SOURCE_ROOT   # any other folder is now prepared automatically; the control root never is
    dashboard_server.set_project(str(SOURCE_ROOT))
    msgs = env.get_messages(new_id)
    assert any("PROJECT_SELECTION_REJECTED" in m["content"] for m in msgs)
    assert env.get_messages(GLOBAL_SESSION_ID) == before                       # old session never written again


def test_new_chat_refused_while_head_busy(server, env, monkeypatch):
    slow = SlowHead(env, delay=1.2); monkeypatch.setattr(dashboard_server, "handle_chat_message", slow)
    t = threading.Thread(target=lambda: _post(server, "/chat", {"message": "work", "request_id": "nc-1"})); t.start()
    import time; time.sleep(0.3)
    code, j = _post(server, "/chat/new", {})
    assert code == 409 and j["status"] == "busy"
    t.join(timeout=10)
    assert active_session_id() == GLOBAL_SESSION_ID


def test_handle_chat_message_writes_active_session(env, monkeypatch):
    from core.provider_manager import ProviderManager
    monkeypatch.setattr(ProviderManager, "check_health", lambda self, force=False: self.status_map.copy())

    class Head:
        provider = "openai"
        last_execution_info = {"exit_code": 0, "execution_duration_sec": 0.1}
        def chat(self, msg, ctx, hist):
            return {"type": "QUESTION", "message": "answer: " + msg}
        def get_provenance(self, exit_code=0):
            return {"agent": "HEAD", "provider": "openai", "observed_fields": []}
    from core.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "_init_head", lambda self, p=None: Head())
    new_id = new_chat_session()["session_id"]
    dashboard_server.handle_chat_message(KOREAN)
    roles = [(m["role"], m["content"]) for m in env.get_messages(new_id)]
    assert ("USER", KOREAN) in roles and ("HEAD", "answer: " + KOREAN) in roles
    assert env.get_provenance(new_id) and not env.get_provenance(GLOBAL_SESSION_ID)
    assert all(m["role"] != "USER" for m in env.get_messages(GLOBAL_SESSION_ID))


# ------------------------------------------------------------------ UTF-8 end-to-end

def test_utf8_round_trip_form_json_html(server, env, monkeypatch):
    monkeypatch.setattr(dashboard_server, "handle_chat_message", SlowHead(env, delay=0.0))
    code, j = _post(server, "/chat", {"message": KOREAN, "request_id": "utf8-1"})
    assert code == 200 and j["status"] == "accepted"
    stored = [m["content"] for m in env.get_messages(GLOBAL_SESSION_ID) if m["role"] == "USER"]
    assert stored == [KOREAN] and "\ufffd" not in stored[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{server}/chat/messages?after_id=0", timeout=5) as r:
        assert "charset=utf-8" in r.headers.get("Content-type", "").lower()
        body = json.loads(r.read().decode("utf-8"))
    assert body["messages"][0]["content"] == KOREAN
    with urllib.request.urlopen(f"http://127.0.0.1:{server}/", timeout=5) as r:
        assert "charset=utf-8" in r.headers.get("Content-type", "").lower()
        html = r.read().decode("utf-8")
    import html as html_lib
    assert html_lib.escape(KOREAN) in html and '<meta charset="utf-8">' in html and 'accept-charset="utf-8"' in html


# ------------------------------------------------------------------ mode hint / recovery summary

def test_mode_hint_text():
    assert _mode_hint("NORMAL", {"OPENAI": "HEALTHY", "ANTHROPIC": "HEALTHY", "GOOGLE": "HEALTHY"}, []) == "all 3 providers verified"
    h = _mode_hint("DEGRADED", {"OPENAI": "HEALTHY", "ANTHROPIC": "RECOVERING", "GOOGLE": "HEALTHY"}, [])
    assert h.startswith("Claude backup recovering")
    h2 = _mode_hint("DEGRADED", {"OPENAI": "RECOVERING", "ANTHROPIC": "RECOVERING", "GOOGLE": "HEALTHY"}, ["OPENAI"])
    assert "Codex recovering (trial available)" in h2 and "Claude backup recovering" in h2
    h3 = _mode_hint("STOP", {"OPENAI": "QUOTA_EXHAUSTED", "ANTHROPIC": "UNAVAILABLE", "GOOGLE": "HEALTHY"}, [])
    assert h3.startswith("insufficient providers") and "Codex quota exhausted" in h3


def test_status_payload_and_page_show_hint_and_short_recovery(server, env):
    import sqlite3
    from core.db_path import get_db_path
    with sqlite3.connect(str(get_db_path())) as c:
        c.execute("UPDATE provider_health SET status='RECOVERING', failure_reason='RECOVERING: cheap probe ok (forced recheck); quota/inference unverified until a real CLI call succeeds' WHERE provider='ANTHROPIC'"); c.commit()
    st = _get_json(server, "/status")[1]
    assert st["system_mode"] == "DEGRADED" and st["system_mode_hint"].startswith("Claude backup recovering")
    assert st["recovery_short"] == "Claude backup recovering"
    assert any(d.startswith("ANTHROPIC: RECOVERING: cheap probe ok") for d in st["recovery_details"])
    html = render_html()
    assert 'id="system-mode-hint" data-raw="Claude backup recovering' in html
    assert 'id="recovery-short" data-raw="Claude backup recovering">CLAUDE 복구 중</div>' in html
    # long explanation only inside the Details element, not in the provider card's visible table
    card = html.split('id="node-ANTHROPIC"')[1].split("</table>")[0]
    assert "cheap probe ok" in card.split("<table>")[0] and "cheap probe ok" not in card.split("<table>")[1]
    assert "Quota" not in card.split("<table>")[1]                                # Quota moved to Details


# ------------------------------------------------------------------ chat readability + provider cards

def test_system_events_compact_and_cards_not_clipped(env):
    env.add_message(GLOBAL_SESSION_ID, "USER", "hi there")
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "Starting background run RUN-X...")
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "Run RUN-X finished: PASS")
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "SYSTEM STOP: insufficient healthy providers")
    html = render_html()
    assert "<div class='msg msg-USER'" in html and "<pre>hi there</pre>" in html
    assert html.count("<div class='msg msg-SYSTEM'") == 3
    assert "<span class='ev ev-ok'>RUN PASS</span>" in html and "<span class='ev ev-bad'>SYSTEM STOP</span>" in html and "<span class='ev ev-info'>RUN STARTED</span>" in html
    assert "<pre>Starting background run" not in html                              # SYSTEM rows are compact, no <pre>
    assert "overflow: hidden" not in dashboard_server.DASHBOARD_CSS.split(".node {")[1].split("}")[0]
    assert "min-height: 0; line-height: 1.25" in dashboard_server.DASHBOARD_CSS
    assert 'id="new-chat-btn"' in html and "/chat/new" in html
