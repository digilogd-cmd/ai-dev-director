"""
v0.2.3.8 DASHBOARD UX HOTFIX — no full-page reload, read-only JSON polling, append-only chat,
idempotent single-in-flight HEAD chat submission.
"""
import json
import time
import threading
import urllib.parse
import urllib.request

import pytest

import dashboard_server
from dashboard_server import (render_html, build_status_payload, chat_messages_after, submit_chat,
                              chat_in_flight, DashboardServer, GLOBAL_SESSION_ID)
from core.state_machine import StateMachine
from core.chat_manager import ChatManager


@pytest.fixture
def env(isolate_provider_db, monkeypatch):
    StateMachine(); dashboard_server._ensure_schema()
    cm = ChatManager()
    cm.create_session(GLOBAL_SESSION_ID, "", "OPENAI")
    with dashboard_server._CHAT_STATE_LOCK:
        dashboard_server._RECENT_CHAT_REQUESTS.clear()
        dashboard_server._CHAT_INFLIGHT.update(request_id=None, started_at=None)
    return cm


@pytest.fixture
def server(env):
    httpd = DashboardServer(("127.0.0.1", 0), dashboard_server.DashboardHandler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    yield port
    httpd.shutdown(); httpd.server_close()


def _get_json(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post(port, path, data, json_mode=True):
    body = urllib.parse.urlencode(data).encode()
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    if json_mode:
        headers.update({"Accept": "application/json", "X-Requested-With": "fetch"})
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if json_mode else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, (json.loads(raw) if json_mode and raw else raw)


class SlowHead:
    """Stands in for handle_chat_message: records the USER message like the real one, then works for `delay` s."""
    def __init__(self, cm, delay=1.2):
        self.cm, self.delay, self.calls = cm, delay, []
    def __call__(self, msg):
        self.calls.append(msg)
        self.cm.add_message(GLOBAL_SESSION_ID, "USER", msg)
        time.sleep(self.delay)
        self.cm.add_message(GLOBAL_SESSION_ID, "HEAD", "reply to " + msg)


# ------------------------------------------------------------------ 1. no full page reload

def test_1_no_full_page_reload(env):
    html = render_html()
    assert 'http-equiv="refresh"' not in html and "http-equiv='refresh'" not in html
    assert "location.reload" not in html and "location.href" not in html
    assert "/status" in html and "/chat/messages" in html          # read-only polling instead
    assert 'id="chat-box"' in html and "data-last-id=" in html
    assert 'id="chat-input"' in html and 'id="send-btn"' in html and 'id="send-status"' in html
    assert "HEAD is working..." in html
    # scroll preservation + input preservation are client-side: the poller appends only and never touches the input
    assert "nearBottom()" in html and "appendMessage" in html and "chatBox.appendChild" in html
    assert "innerHTML" not in html


# ------------------------------------------------------------------ 2. /status carries every polled field

def test_2_status_endpoint_fields(server, env):
    code, st = _get_json(server, "/status")
    assert code == 200
    assert st["system_mode"] in ("NORMAL", "DEGRADED", "STOP")
    for name in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        for k in ("status", "role", "version", "latency", "usage", "error"):
            assert k in st["providers"][name]
    for k in ("run_id", "state", "created_at", "human_required", "stale"):
        assert k in st["run"]
    assert "recovery_trial" in st and st["head_busy"] is False and "project_path" in st
    # page and endpoint agree (single source)
    html = render_html()
    assert f'class="status-{st["system_mode"]}" id="system-mode" data-raw="{st["system_mode"]}"' in html


# ------------------------------------------------------------------ 3. append-only chat endpoint

def test_3_chat_messages_after(server, env):
    env.add_message(GLOBAL_SESSION_ID, "USER", "first <b>x</b>")
    env.add_message(GLOBAL_SESSION_ID, "HEAD", "second")
    code, j = _get_json(server, "/chat/messages?after_id=0")
    assert code == 200 and [m["role"] for m in j["messages"]] == ["USER", "HEAD"]
    last = j["messages"][-1]["id"]
    assert j["messages"][0]["content"] == "first <b>x</b>"           # raw; the page renders via textContent
    code, j2 = _get_json(server, f"/chat/messages?after_id={last}")
    assert j2["messages"] == []                                        # nothing re-sent, nothing re-rendered
    env.add_message(GLOBAL_SESSION_ID, "SYSTEM", "third")
    code, j3 = _get_json(server, f"/chat/messages?after_id={last}")
    assert [m["content"] for m in j3["messages"]] == ["third"]
    html = render_html()
    assert f'data-last-id="{last + 1}"' in html and "data-id='" in html


# ------------------------------------------------------------------ C. rapid submits -> one USER message

def test_c_rapid_submits_one_message(server, env, monkeypatch):
    head = SlowHead(env, delay=1.2)
    monkeypatch.setattr(dashboard_server, "handle_chat_message", head)
    rid = "req-abc-123"
    results = []
    def fire():
        results.append(_post(server, "/chat", {"message": "hello", "request_id": rid}))
    threads = [threading.Thread(target=fire) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)
    statuses = sorted(r[1]["status"] for r in results)
    assert statuses.count("accepted") == 1, statuses
    assert all(s in ("accepted", "busy", "duplicate") for s in statuses)
    assert head.calls == ["hello"]                                     # HEAD invoked exactly once
    users = [m for m in env.get_messages(GLOBAL_SESSION_ID) if m["role"] == "USER"]
    assert len(users) == 1
    # the same request_id re-sent after completion is a no-op too (idempotency window)
    code, j = _post(server, "/chat", {"message": "hello", "request_id": rid})
    assert code == 200 and j["status"] == "duplicate" and head.calls == ["hello"]
    # distinct request_ids while another is in flight -> 409 busy, no second HEAD call
    slow = SlowHead(env, delay=1.5); monkeypatch.setattr(dashboard_server, "handle_chat_message", slow)
    t = threading.Thread(target=lambda: _post(server, "/chat", {"message": "one", "request_id": "r1"})); t.start()
    time.sleep(0.3)
    code, j = _post(server, "/chat", {"message": "two", "request_id": "r2"})
    assert code == 409 and j["status"] == "busy" and j["message"] == "HEAD is working..."
    t.join(timeout=10)
    assert slow.calls == ["one"]


# ------------------------------------------------------------------ D. busy state is visible while HEAD works

def test_d_head_busy_visible(server, env, monkeypatch):
    slow = SlowHead(env, delay=1.5); monkeypatch.setattr(dashboard_server, "handle_chat_message", slow)
    t = threading.Thread(target=lambda: _post(server, "/chat", {"message": "work", "request_id": "busy-1"})); t.start()
    time.sleep(0.3)
    assert chat_in_flight() is True
    assert _get_json(server, "/status")[1]["head_busy"] is True
    assert _get_json(server, "/chat/messages?after_id=0")[1]["head_busy"] is True
    html = render_html()
    assert 'id="send-btn" disabled' in html and "BUSY: HEAD is working" in html   # a freshly loaded page is disabled too
    t.join(timeout=10)
    assert chat_in_flight() is False and _get_json(server, "/status")[1]["head_busy"] is False
    assert 'id="send-btn">' in render_html()


# ------------------------------------------------------------------ E. status polling reflects DB changes (read-only)

def test_e_status_reflects_provider_and_run_changes(server, env):
    import sqlite3
    from core.db_path import get_db_path
    with sqlite3.connect(str(get_db_path())) as c:
        c.execute("UPDATE provider_health SET status='QUOTA_EXHAUSTED', current_role='HEAD', last_latency_sec=1.5 WHERE provider='OPENAI'"); c.commit()
    st = _get_json(server, "/status")[1]
    assert st["providers"]["OPENAI"]["status"] == "QUOTA_EXHAUSTED" and st["providers"]["OPENAI"]["role"] == "HEAD"
    assert st["providers"]["OPENAI"]["latency"] == 1.5 and st["system_mode"] == "DEGRADED"
    sm = StateMachine(); sm.create_run("RUN-UX-1", "C:/x", "g")
    from core.state_machine import State
    sm.transition_to("RUN-UX-1", State.PLANNING); sm.transition_to("RUN-UX-1", State.HUMAN_REQUIRED, details="x")
    st = _get_json(server, "/status")[1]
    assert st["run"]["run_id"] == "RUN-UX-1" and st["run"]["state"] == "HUMAN_REQUIRED" and st["run"]["human_required"] == "YES"


# ------------------------------------------------------------------ legacy form POST keeps its 303 (CSRF test parity)

def test_f_legacy_form_post_and_project_json(server, env, monkeypatch):
    monkeypatch.setattr(dashboard_server, "handle_chat_message", SlowHead(env, delay=0.0))
    code, _ = _post(server, "/chat", {"message": "legacy"}, json_mode=False)
    assert code == 200                                                  # urllib followed the 303 to '/'
    from core.bootstrap import SOURCE_ROOT   # any other folder is now prepared automatically; the control root never is
    code, j = _post(server, "/project", {"path": str(SOURCE_ROOT)})
    assert code == 200 and j["status"] == "rejected" and "PROJECT_SELECTION_REJECTED" in j["message"]
    code, j = _post(server, "/chat", {"message": ""})
    assert code == 200 and j["status"] == "empty"
