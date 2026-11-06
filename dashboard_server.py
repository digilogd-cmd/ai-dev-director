import sqlite3
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import socket
import sys
from pathlib import Path
from datetime import datetime, timedelta
import threading
import os
import html as html_lib
import uuid
import re
from typing import Optional
import collections

from core.chat_manager import ChatManager
from core.provider_manager import ProviderManager, FailoverMode, ProviderStatus
from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State, TERMINAL_STATES
from core.safety import SafetyPolicy
from core.db_path import get_db_path, SOURCE_ROOT
from schemas.order import OrderSchema
from director import load_config
from core.result_manager import ResultManager
from core import autopilot
from core.mission import MissionRunner, MissionStore, missions_to_resume, latest_deliverable
import dashboard_i18n as I18N
import time as _time

# v0.2.3.1: localhost-only is the security boundary of the MVP dashboard.
BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

GLOBAL_SESSION_ID = "main_session"
DIRECTOR_ROOT = SOURCE_ROOT            # the AI DEV DIRECTOR repository itself
ACTIVE_RUN_WINDOW_SEC = 900            # a non-terminal run older than this is treated as stale

# In-process guard against concurrent DEV_TASK execution (project key -> run_id)
_RUN_LOCK = threading.Lock()
_ACTIVE_PROJECTS = {}

BUSY_MESSAGE = "BUSY: A run is already active for this project."

# v0.2.3.8: HEAD chat submissions are single-in-flight and idempotent (request_id). The page never reloads;
# it polls read-only JSON (/status, /chat/messages) and appends new messages only.
_CHAT_STATE_LOCK = threading.Lock()
_CHAT_INFLIGHT = {"request_id": None, "started_at": None}
_RECENT_CHAT_REQUESTS = collections.OrderedDict()     # request_id -> final status (idempotency window)
RECENT_CHAT_REQUESTS_MAX = 500
_APPROVAL_LOCK = threading.Lock()
_APPROVAL_INFLIGHT = set()                              # run_ids whose approve/resume is running
_RECENT_APPROVAL_REQUESTS = collections.OrderedDict()   # request_id -> status (idempotent Approve clicks)
APPROVAL_REASONS = {
    "SENSITIVE_FILE_IN_SCOPE": ("Sensitive file in scope", "Dependency/configuration file modification requires user approval."),
    "PROTECTED_PATH_IN_SCOPE": ("Protected path in scope", "allowed_paths overlaps a protected path; a human must confirm the scope."),
    "PROTECTED_PATH_INSIDE_ALLOWED": ("Protected path inside allowed scope", "A protected path lies under an allowed path; confirm the scope."),
    "AMBIGUOUS_SCOPE": ("Ambiguous scope", "allowed_paths is empty or whole-repo; a human must confirm the scope."),
    "HIGH_RISK": ("High risk order", "HEAD rated this ORDER HIGH risk; a human must approve execution."),
    "DESTRUCTIVE_CHANGE": ("Destructive change", "The ORDER allows deletions; a human must approve."),
    "EXPLICIT_HUMAN_APPROVAL_REQUESTED": ("HEAD requested approval", "HEAD explicitly asked for human approval before executing."),
    "GIT_INTERNALS_IN_SCOPE": ("Git internals in scope", ".git is inside allowed_paths; confirm or reject."),
    "INVALID_PATH": ("Invalid path in ORDER", "The ORDER contains a path that is not repo-relative; this is not approvable."),
    "CONTROL_PLANE_IN_SCOPE": ("Director control-plane path in scope", "Director-owned control files (.agents, .director_worktrees, GEMINI.md) are never approvable."),
    "DEPENDENCY_CONFIGURATION_MODIFIED": ("Dependency configuration modified", "The builder changed a dependency file; a human must approve this specific file before verification continues."),
}
HEAD_WORKING_MESSAGE = "HEAD is working..."
# v0.2.4.7: /status is served from a short-TTL cache; every state-changing action invalidates it
STATUS_CACHE_TTL_SEC = 2.0
_STATUS_CACHE = {"at": 0.0, "payload": None}
_STATUS_CACHE_LOCK = threading.Lock()


def invalidate_status_cache() -> None:
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE["at"] = 0.0
        _STATUS_CACHE["payload"] = None


def cached_status_payload() -> dict:
    now = _time.time()
    with _STATUS_CACHE_LOCK:
        if _STATUS_CACHE["payload"] is not None and now - _STATUS_CACHE["at"] < STATUS_CACHE_TTL_SEC:
            return _STATUS_CACHE["payload"]
    payload = build_status_payload()
    with _STATUS_CACHE_LOCK:
        _STATUS_CACHE["payload"] = payload; _STATUS_CACHE["at"] = _time.time()
    return payload


class DashboardAlreadyRunning(RuntimeError):
    """Raised when 127.0.0.1:<port> is already bound by another dashboard instance."""


class DashboardServer(ThreadingHTTPServer):
    """Single-instance server: no SO_REUSEADDR (Windows would otherwise allow a duplicate LISTEN),
    SO_EXCLUSIVEADDRUSE where available, and a clear error when the port is taken."""
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            super().server_bind()
        except OSError as e:
            host, port = self.server_address[:2]
            raise DashboardAlreadyRunning(
                f"Dashboard already running: {host}:{port} is in use (another dashboard instance?). {e}"
            ) from e


ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")


def is_request_origin_allowed(headers, port: int):
    """Minimal CSRF defence for state-changing POSTs: Host must be localhost:<port>; if an Origin header is
    present it must be http://localhost:<port> too. Returns (ok, reason)."""
    host = (headers.get("Host") or "").strip().lower()
    allowed_hosts = {f"{h}:{port}" for h in ALLOWED_HOSTNAMES}
    if port == 80:
        allowed_hosts |= set(ALLOWED_HOSTNAMES)
    if host not in allowed_hosts:
        return False, f"bad Host header: {host!r}"
    origin = headers.get("Origin")
    if origin is not None:
        origin = origin.strip().lower()
        if origin == "null" or origin not in {f"http://{h}" for h in allowed_hosts}:
            return False, f"foreign Origin: {origin!r}"
    return True, ""
NO_PROJECT_MESSAGE = "HUMAN_REQUIRED: PROJECT_SELECTION_REQUIRED (no project selected; use the project form first)"


def resolve_db_path() -> Path:
    """Single canonical DB path (never cwd-relative)."""
    return get_db_path()


def get_db_conn():
    conn = sqlite3.connect(str(resolve_db_path()))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema():
    StateMachine()
    ChatManager()
    ProviderManager()


def _project_key(path: str) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except Exception:
        return str(path).lower()


def validate_project_path(path: str):
    """Returns (ok, reason). Rejects non-git dirs and the AI DEV DIRECTOR repository itself."""
    if not path or not path.strip():
        return False, "empty path"
    p = Path(path.strip())
    try:
        p = p.resolve()
    except Exception as e:
        return False, f"unresolvable path: {e}"
    if not p.exists() or not p.is_dir():
        return False, f"not a directory: {p}"
    if not (p / ".git").exists():
        return False, f"not a git repository: {p}"
    root = DIRECTOR_ROOT.resolve()
    if p == root or root in p.parents:
        return False, "the AI DEV DIRECTOR repository itself cannot be selected as a chat target"
    return True, str(p)


PROVIDER_LABELS = {"OPENAI": "Codex", "ANTHROPIC": "Claude backup", "GOOGLE": "Antigravity builder"}


def active_session_id() -> str:
    """The chat session the page shows and new messages go to: the most recently created session.
    Older sessions (and their messages / provenance) stay in the DB untouched."""
    try:
        with get_db_conn() as conn:
            row = conn.execute("SELECT id FROM chat_sessions ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
        return row["id"] if row else GLOBAL_SESSION_ID
    except Exception:
        return GLOBAL_SESSION_ID


def new_chat_session() -> dict:
    invalidate_status_cache()
    """[New Chat]: start a fresh session that keeps the current project; nothing is deleted."""
    if chat_in_flight():
        return {"status": "busy", "message": HEAD_WORKING_MESSAGE}
    cm = ChatManager()
    prev = active_session_id()
    sess = cm.get_session(prev) or {}
    new_id = f"chat-{datetime.utcnow().strftime('%y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    cm.create_session(new_id, (sess.get("project_path") or "").strip(), sess.get("head_provider") or _configured_head_domain())
    cm.add_message(new_id, "SYSTEM", f"NEW_CHAT: new session started (previous history kept in {prev})")
    return {"status": "ok", "session_id": new_id, "previous_session_id": prev}


def _mode_hint(mode: str, statuses: dict, candidates: list) -> str:
    """One-line meaning of SYSTEM MODE for humans (presentation only; the mode itself comes from ProviderManager)."""
    parts = []
    for dom in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        st = statuses.get(dom, "UNKNOWN")
        if st == "HEALTHY":
            continue
        label = PROVIDER_LABELS.get(dom, dom)
        if st == "RECOVERING":
            parts.append(f"{label} recovering" + (" (trial available)" if dom in candidates else ""))
        else:
            parts.append(f"{label} {st.lower().replace('_', ' ')}")
    if mode == "NORMAL":
        return "all 3 providers verified"
    if mode == "DEGRADED":
        return ("; ".join(parts) or "2 of 3 providers usable") + " · 2 of 3 usable"
    return "insufficient providers" + (": " + "; ".join(parts) if parts else "")


ACTIVE_STATES = {"PLANNING", "ORDER_READY", "BUILDING", "VERIFYING", "REVIEWING", "DECIDING"}


def _node_activity(state: str, providers: dict) -> dict:
    """Which AI Core is doing work RIGHT NOW, from real backend facts only: the run state and the roles
    ProviderManager.resolve_roles() recorded (current_role). No run in flight -> nothing is active."""
    def roles(dom):
        return [r.strip().upper() for r in str((providers.get(dom, {}) or {}).get("role", "") or "").replace("·", ",").split(",") if r.strip()]
    stage = {"PLANNING": "HEAD", "ORDER_READY": "HEAD", "BUILDING": "BUILDER", "VERIFYING": "VERIFY",
             "REVIEWING": "REVIEWER", "DECIDING": "HEAD"}.get(state)
    out = {"director": state in ACTIVE_STATES, "stage": stage, "nodes": {}}
    for dom in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        rs = roles(dom)
        active = bool(stage) and ((stage == "HEAD" and "HEAD" in rs) or (stage == "REVIEWER" and "REVIEWER" in rs)
                                  or (stage == "BUILDER" and "BUILDER" in rs))
        working_roles = [r for r in rs if r in ("HEAD", "REVIEWER", "BUILDER")]
        out["nodes"][dom] = {"active": active, "roles": rs, "backup": dom == "ANTHROPIC" and not working_roles and not active}
    return out


def _recovery_summary(preview: dict, providers: dict) -> dict:
    """Short line for the main screen + detailed reasons for the Details panel."""
    short, short_ko, details = [], [], []
    for dom in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        st = preview.get("statuses", {}).get(dom, "UNKNOWN")
        row = providers.get(dom, {}) or {}
        if st == "RECOVERING":
            short.append(f"{PROVIDER_LABELS[dom]} recovering")
            short_ko.append(f"{I18N.PROVIDER_LABEL[dom][0]} 복구 중")
        elif st != "HEALTHY":
            short.append(f"{PROVIDER_LABELS[dom]} {st.lower().replace('_', ' ')}")
            short_ko.append(f"{I18N.PROVIDER_LABEL[dom][0]} {I18N.status_ko(st)}")
        if st != "HEALTHY" and row.get("failure_reason"):
            details.append(f"{dom}: {row.get('failure_reason')}")
    return {"short": "; ".join(short) or "all providers verified", "short_ko": " · ".join(short_ko) or "모든 AI 정상 확인됨", "details": details}


def set_project(path: str) -> str:
    invalidate_status_cache()
    """Explicit project selection from the UI. Returns a SYSTEM message."""
    cm = ChatManager()
    sid = active_session_id()
    if load_config().get('bootstrap', {}).get('enabled'):
        try:
            from core.bootstrap import prepare_project
            path = prepare_project(path)
        except Exception as exc:
            if cm.get_session(sid):
                cm.add_message(sid, "SYSTEM", f"PROJECT_SELECTION_REJECTED: {exc}")
            return 'PROJECT_SELECTION_REJECTED: ' + str(exc)
    ok, info = validate_project_path(path)
    if not ok:
        cm.add_message(sid, "SYSTEM", f"PROJECT_SELECTION_REJECTED: {info}") if cm.get_session(sid) else None
        return f"PROJECT_SELECTION_REJECTED: {info}"
    cm.set_project(sid, info)
    cm.add_message(sid, "SYSTEM", f"PROJECT_SELECTED: {info}")
    return f"PROJECT_SELECTED: {info}"


def _configured_head_domain() -> str:
    """Domain of the HEAD provider the next chat will call (config.yaml head.provider; same mapping as the orchestrator)."""
    try:
        prov = (load_config().get("head", {}) or {}).get("provider", "openai")
    except Exception:
        prov = "openai"
    return ProviderManager.domain_for_provider(prov)


def _effective_mode_preview() -> dict:
    """Read-only mode preview for the page: identical policy to the chat gate (candidate = HEAD only)."""
    head_domain = _configured_head_domain()
    only = [head_domain] if head_domain not in ("MOCK", "UNKNOWN") else []
    return ProviderManager().get_effective_mode_for_next_run(only=only)


def _recovery_trial_line(preview: dict, providers: dict) -> str:
    """Plain text: 'OPENAI available' / 'OPENAI unavailable (cooldown ...)' for RECOVERING providers ('' if none)."""
    parts = []
    for dom, st in preview.get("statuses", {}).items():
        if st != ProviderStatus.RECOVERING.value:
            continue
        if dom in preview.get("trial_candidates", []):
            parts.append(f"{dom} available")
        else:
            row = providers.get(dom, {}) or {}
            until = row.get("next_trial_at") or row.get("trial_started_at") or ""
            parts.append(f"{dom} unavailable (cooldown/in-flight{' until ' + str(until) if until else ''}; not the HEAD provider or trial blocked)")
    return "; ".join(parts)


def chat_in_flight() -> bool:
    with _CHAT_STATE_LOCK:
        return _CHAT_INFLIGHT["request_id"] is not None


def submit_chat(msg: str, request_id: str = "") -> dict:
    invalidate_status_cache()
    """Idempotent, single-in-flight HEAD chat submission used by POST /chat.
    - same request_id seen before  -> {"status": "duplicate"} (no second HEAD call)
    - another request in flight    -> {"status": "busy"}      (no second HEAD call)
    - otherwise                    -> runs handle_chat_message once -> {"status": "accepted"}"""
    if not msg or not msg.strip():
        return {"status": "empty"}
    rid = (request_id or "").strip() or uuid.uuid4().hex
    with _CHAT_STATE_LOCK:
        if rid in _RECENT_CHAT_REQUESTS:
            return {"status": "duplicate", "request_id": rid, "message": "already submitted"}
        if _CHAT_INFLIGHT["request_id"] is not None:
            return {"status": "busy", "request_id": rid, "message": HEAD_WORKING_MESSAGE}
        _CHAT_INFLIGHT["request_id"] = rid
        _CHAT_INFLIGHT["started_at"] = datetime.utcnow().isoformat()
        _RECENT_CHAT_REQUESTS[rid] = "inflight"
        while len(_RECENT_CHAT_REQUESTS) > RECENT_CHAT_REQUESTS_MAX:
            _RECENT_CHAT_REQUESTS.popitem(last=False)
    try:
        handle_chat_message(msg)
        result = {"status": "accepted", "request_id": rid}
    except Exception as e:
        result = {"status": "error", "request_id": rid, "error": str(e)[:300]}
    finally:
        with _CHAT_STATE_LOCK:
            _RECENT_CHAT_REQUESTS[rid] = "done"
            _CHAT_INFLIGHT["request_id"] = None
            _CHAT_INFLIGHT["started_at"] = None
    return result


def refresh_providers() -> dict:
    invalidate_status_cache()
    """Manual provider refresh: cheap probes with force recheck, status follows the actual result."""
    pm = ProviderManager()
    statuses = pm.check_health(force=True)
    return {k: v.value for k, v in statuses.items()}


def _project_busy(sm: StateMachine, project_path: str) -> bool:
    """A DB-visible non-terminal, non-stale run for the same project counts as active."""
    key = _project_key(project_path)
    now = datetime.utcnow()
    for r in sm.list_runs():
        if r["current_state"] in TERMINAL_STATES:
            continue
        if _project_key(r.get("project_path") or "") != key:
            continue
        try:
            updated = datetime.fromisoformat(r["updated_at"])
        except Exception:
            continue
        if (now - updated).total_seconds() <= ACTIVE_RUN_WINDOW_SEC:
            return True
    return False


def _new_orchestrator() -> Orchestrator:
    """Factory for the orchestrator used by approve/reject (tests substitute a mock one)."""
    return Orchestrator(config=load_config())


def parse_approval_reason(details: str) -> dict:
    """'APPROVAL_REQUIRED: SENSITIVE_FILE_IN_SCOPE (requirements.txt); HIGH_RISK' -> structured items."""
    items = []
    body = details.split(":", 1)[1] if details.startswith("APPROVAL_REQUIRED") and ":" in details else details
    for part in [x.strip() for x in body.split(";") if x.strip()]:
        code = part.split(" ", 1)[0].strip()
        file = part[len(code):].strip().strip("()").strip() if "(" in part else ""
        title, why = APPROVAL_REASONS.get(code, (code.replace("_", " ").capitalize(), "This entry policy requires a human decision."))
        items.append({"code": code, "file": file, "title": title, "why": why, "approvable": code in SafetyPolicy.APPROVABLE_ENTRY})
    return {"raw": details, "items": items, "approvable": bool(items) and all(i["approvable"] for i in items)}


def approval_info(run_id: str, state: str, details: str) -> dict:
    """Display + action state for a HUMAN_REQUIRED run (read-only). Approvable only when the orchestrator's
    classification says so (ENTRY policy halt, or POST_BUILD halt made only of waivable gate+file pairs)."""
    if state != State.HUMAN_REQUIRED:
        return {"required": False, "state": state, "reason_raw": details, "run_id": run_id}
    try:
        st = _new_orchestrator().approval_state(run_id)
    except Exception as e:
        st = {"approvable": False, "error": str(e)[:200], "stage": None, "items": [], "blocking": []}
    items = []
    for i in st.get("items") or []:
        title, why = APPROVAL_REASONS.get(i["gate"], (i["gate"].replace("_", " ").capitalize(), "This gate requires a human decision."))
        items.append({"code": i["gate"], "file": i.get("file", ""), "title": title, "why": why, "approvable": True})
    blocking = []
    for b in st.get("blocking") or []:
        if isinstance(b, dict):
            gate, file, text = b.get("gate", ""), b.get("file", ""), b.get("text", "")
        else:
            text = str(b)
            gate, file = I18N.reason_code(text), ""
        title, why = APPROVAL_REASONS.get(gate, ((gate.replace("_", " ").capitalize() if gate else "Blocked"), "This gate cannot be approved; it requires HEAD to replan."))
        blocking.append({"code": gate, "file": file, "text": text, "title": title, "why": why, "approvable": False})
    with _APPROVAL_LOCK:
        inflight = run_id in _APPROVAL_INFLIGHT
    approvable = bool(st.get("approvable")) and not inflight
    stage = st.get("stage")
    return {
        "required": approvable, "state": state, "run_id": run_id, "reason_raw": details, "items": items,
        "blocking": blocking, "stage": stage,
        "stage_label": {"ENTRY": "ENTRY / before build", "POST_BUILD": "POST-BUILD / VERIFYING"}.get(stage, "runtime gate"),
        "builder": "COMPLETED" if st.get("builder_ran") else "NOT RUN",
        "rejected": bool(st.get("rejected")), "approved": bool(st.get("approved")),
        "builder_ran": bool(st.get("builder_ran")), "inflight": inflight,
        "headline": "HUMAN APPROVAL REQUIRED" if approvable else
                    ("REJECTED BY HUMAN" if st.get("rejected") else ("APPROVED · RESUMING" if inflight or st.get("approved") else "HUMAN REQUIRED (not approvable)")),
        "next": "Approval required before continuing" if approvable else "",
    }


def classify_stale_human_actions(limit: int = 25) -> int:
    """Persists the approval classification for HUMAN_REQUIRED runs that do not have one yet (new halts and
    legacy rows). Bounded per call; after the backlog is drained this costs one indexed query per poll."""
    sm = StateMachine()
    stale = sm.stale_human_actions(limit)
    if not stale:
        return 0
    orch = _new_orchestrator()
    for run_id in stale:
        try:
            st = orch.approval_state(run_id)
            sm.set_human_action(run_id, st.get("stage"), bool(st.get("approvable")), st.get("reason") or "",
                                st.get("items") or [], st.get("blocking") or [], resolved=bool(st.get("rejected") or st.get("approved")))
        except Exception:
            sm.set_human_action(run_id, None, False, "classification failed", [], [], resolved=False)
    return len(stale)


def pending_actions() -> list:
    """Runs waiting for a human decision, from the persisted human_action_state table (indexed SQL, no run scan,
    no subprocess). Newest-run independence is preserved."""
    out = []
    try:
        classify_stale_human_actions()
        rows = StateMachine().list_pending_actions()
    except Exception:
        return out
    for r in rows:
        if str(r.get("run_id", "")).startswith("RUN-TEST-"):
            continue
        proj = r.get("project_path") or ""
        if not proj:
            continue
        try:
            items = json.loads(r.get("items_json") or "[]")
        except Exception:
            items = []
        with _APPROVAL_LOCK:
            inflight = r["run_id"] in _APPROVAL_INFLIGHT
        first = (items or [{}])[0]
        code = first.get("gate", "")
        title_en = APPROVAL_REASONS.get(code, (code, ""))[0]
        title_ko = I18N.reason_ko(code)[0]
        out.append({"run_id": r["run_id"], "project_name": Path(proj).name, "project_path": proj, "stage": r.get("stage"),
                    "title": title_ko, "title_en": title_en, "code": code, "file": first.get("file", ""), "files": [i.get("file") for i in items],
                    "status": "RESUMING" if inflight else "PENDING", "created_at": r.get("created_at")})
    return out


def inspect_run(run_id: str) -> dict:
    """Read-only inspection bundle for the Inspect panel."""
    sm = StateMachine()
    run = sm.get_run(run_id)
    if not run:
        return {"error": "run not found", "run_id": run_id}
    orch = _new_orchestrator()
    order = orch._load_json(run_id, "order.json") or {}
    trans = sm.get_transitions(run_id)
    last = (trans[-1]["details"] or "") if trans else ""
    parsed = parse_approval_reason(last) if last.startswith("APPROVAL_REQUIRED") else {"items": []}
    files = [i["file"] for i in parsed["items"] if i.get("file")]
    ap = orch.approval_state(run_id)
    builder = orch._load_json(run_id, "builder.json") or {}
    prov = (orch._load_json(run_id, "provenance.json") or {}).get("records", [])
    bprov = next((p for p in prov if p.get("agent") == "BUILDER"), {})
    verification = orch._load_json(run_id, "verification.json") or {}
    baseline = orch._load_json(run_id, "baseline_verification.json") or {}
    diff_stat, file_diffs, changed = [], {}, []
    wt = run.get("worktree_path")
    if wt and Path(wt).exists():
        try:
            from core.git_manager import GitManager
            full, numstat, untracked = GitManager.inspect_changes(wt)
            changed = [f for _, _, f in numstat] + untracked
            diff_stat = [f"{f}: +{a} -{dl}" for a, dl, f in numstat]
            for f in [i.get("file") for i in ap.get("items") or []]:
                if not f:
                    continue
                marker = f"diff --git a/{f} b/{f}"
                if marker in full:
                    seg = full.split(marker, 1)[1]
                    nxt = seg.find("\ndiff --git ")
                    file_diffs[f] = (marker + (seg if nxt < 0 else seg[:nxt]))[:4000]
                else:
                    p = Path(wt) / f
                    if p.exists() and f in changed:
                        file_diffs[f] = "(new untracked file)\n" + p.read_text(encoding="utf-8", errors="replace")[:4000]
        except Exception as e:
            diff_stat = [f"(diff unavailable: {e})"]
    stage_order = ["PLANNING", "ORDER_READY", "BUILDING", "VERIFYING", "REVIEWING", "DECIDING"]
    executed = [t["to_state"] for t in trans if t["to_state"] in stage_order]
    resume_at = {"ENTRY": "ORDER_READY", "POST_BUILD": "VERIFYING"}.get(ap.get("stage"))
    upcoming = stage_order[stage_order.index(resume_at):] + ["FINAL_GATE"] if resume_at else []
    return {
        "run_id": run_id, "state": run["current_state"], "project_path": run.get("project_path"), "goal": run.get("goal"),
        "created_at": run.get("created_at"), "updated_at": run.get("updated_at"),
        "approval": {"stage": ap.get("stage"), "approvable": ap.get("approvable"), "items": ap.get("items"), "blocking": ap.get("blocking"),
                     "decisions": [{"action": a.get("action"), "at": a.get("timestamp"), "payload": (a.get("payload") or "")[:300]} for a in ap.get("approvals_so_far") or []]},
        "changed_files": changed, "diff_stat": diff_stat, "file_diffs": file_diffs,
        "builder": {"provider": bprov.get("provider"), "version": bprov.get("version"), "exit_code": bprov.get("exit_code"),
                    "duration_sec": bprov.get("duration_sec"), "status": builder.get("status"), "files_modified": builder.get("files_modified")},
        "tests": {"baseline": baseline.get("test_counts"), "post": verification.get("test_counts"), "post_passed": verification.get("passed")},
        "scope_checks": [(t["details"] or "")[:300] for t in trans if (t["details"] or "").startswith("Scope violation")],
        "stages_upcoming": upcoming,
        "order": {k: order.get(k) for k in ("goal", "allowed_paths", "protected_paths", "allowed_deletions", "risk_level",
                                            "requires_human_approval", "acceptance_criteria", "context", "max_files_changed", "max_diff_lines")},
        "approval_reason": last, "approval_items": parsed["items"], "related_files": files,
        "transitions": [{"from": t["from_state"], "to": t["to_state"], "at": t["timestamp"], "details": (t["details"] or "")[:300]} for t in trans],
        "audit": [{"agent": a["agent"], "action": a["action"], "at": a["timestamp"], "payload": (a["payload"] or "")[:300]} for a in sm.get_audit_logs(run_id)],
        "builder_ran": orch._load_json(run_id, "builder.json") is not None or bool(run.get("worktree_path")),
        "worktree_path": run.get("worktree_path"),
        "stages_executed": executed,
    }


def approve_run(run_id: str, request_id: str = "", note: str = "") -> dict:
    invalidate_status_cache()
    """Explicit dashboard approval: one approve/resume per run, idempotent per request_id, executed in the background."""
    rid = (request_id or "").strip() or uuid.uuid4().hex
    with _APPROVAL_LOCK:
        if rid in _RECENT_APPROVAL_REQUESTS:
            return {"status": "duplicate", "run_id": run_id, "request_id": rid}
        if run_id in _APPROVAL_INFLIGHT:
            return {"status": "busy", "run_id": run_id, "message": "approval already in progress for this run"}
        _RECENT_APPROVAL_REQUESTS[rid] = "inflight"
        while len(_RECENT_APPROVAL_REQUESTS) > RECENT_CHAT_REQUESTS_MAX:
            _RECENT_APPROVAL_REQUESTS.popitem(last=False)
        _APPROVAL_INFLIGHT.add(run_id)
    orch = _new_orchestrator()
    st = orch.approval_state(run_id)
    if not st.get("approvable"):
        with _APPROVAL_LOCK:
            _APPROVAL_INFLIGHT.discard(run_id); _RECENT_APPROVAL_REQUESTS[rid] = "rejected"
        return {"status": "not_applicable", "run_id": run_id, "detail": st}
    run = orch.sm.get_run(run_id) or {}
    project_path = run.get("project_path") or ""
    key = _project_key(project_path)
    with _RUN_LOCK:
        if key in _ACTIVE_PROJECTS or _project_busy(orch.sm, project_path):
            with _APPROVAL_LOCK:
                _APPROVAL_INFLIGHT.discard(run_id); _RECENT_APPROVAL_REQUESTS[rid] = "busy"
            return {"status": "busy", "run_id": run_id, "message": BUSY_MESSAGE}
        _ACTIVE_PROJECTS[key] = run_id
    cm = ChatManager(); sid = active_session_id()
    cm.add_message(sid, "SYSTEM", f"HUMAN_APPROVED({st.get('stage')}): resuming run {run_id} ({st.get('reason')})", run_id=run_id)

    def go():
        try:
            summary = orch.approve_and_resume(run_id, approver="HUMAN", note=note, request_id=rid)
            final = summary.get("final_state") or (orch.sm.get_run(run_id) or {}).get("current_state", "UNKNOWN")
            cm.add_message(sid, "SYSTEM", f"Run {run_id} finished: {final}", run_id=run_id)
            mission = MissionStore().by_run(run_id) if autopilot.enabled(orch.config) else None
            if mission and mission.get("status") in ("BLOCKED", "INCOMPLETE"):
                # the user resolved the blocking decision: hand the project slot back to the mission
                with _RUN_LOCK:
                    _ACTIVE_PROJECTS[key] = mission["mission_id"]
                cm.add_message(sid, "SYSTEM", f"승인 반영 - 미션 {mission['mission_id']} 이어서 진행", run_id=run_id)
                _run_mission_thread(sid, mission["mission_id"], key, reopen=True)
        except Exception as e:
            cm.add_message(sid, "SYSTEM", f"Run failed: {str(e)}", run_id=run_id)
        finally:
            with _RUN_LOCK:
                if _ACTIVE_PROJECTS.get(key) == run_id:
                    _ACTIVE_PROJECTS.pop(key, None)
            with _APPROVAL_LOCK:
                _APPROVAL_INFLIGHT.discard(run_id); _RECENT_APPROVAL_REQUESTS[rid] = "done"
    threading.Thread(target=go, name=f"director-approve-{run_id}", daemon=True).start()
    return {"status": "accepted", "run_id": run_id, "request_id": rid}


def reject_run(run_id: str, note: str = "") -> dict:
    invalidate_status_cache()
    orch = _new_orchestrator()
    res = orch.reject_human_required(run_id, rejecter="HUMAN", note=note)
    if res.get("status") == "REJECTED":
        cm = ChatManager(); cm.add_message(active_session_id(), "SYSTEM", f"HUMAN_REJECTED: run {run_id} will not execute", run_id=run_id)
    return {"status": "ok" if res.get("status") == "REJECTED" else "not_applicable", "run_id": run_id, "detail": res}


def _result_manager() -> ResultManager:
    return ResultManager(config=load_config())


def result_info(run_id: str) -> dict:
    """Result summary for the current run from persisted state (DB row + JSON artifacts). No git, no subprocess."""
    try:
        return _result_manager().cached_summary(run_id)
    except Exception as e:
        return {"run_id": run_id, "error": str(e)[:300], "is_pass": False}


def _record_human_required(orch: Orchestrator, project_path: str, goal: str, reason: str, order=None) -> str:
    """Creates a run row that ends immediately in HUMAN_REQUIRED (never touches the Builder)."""
    run_id = orch._create_run_with_retry(None, project_path or "NONE", goal)
    if order is not None:
        orch._save_json(run_id, "order.json", order)
    orch.sm.log_audit(run_id, "ORCHESTRATOR", "CHAT_PREFLIGHT_HALT", reason)
    orch.sm.transition_to(run_id, State.HUMAN_REQUIRED, details=reason)
    return run_id


def _head_provenance_record(head) -> dict:
    """Actual execution provenance of the HEAD chat() call (exit code / duration from the real run)."""
    if not hasattr(head, "get_provenance"):
        return {}
    info = getattr(head, "last_execution_info", {}) or {}
    executed = "exit_code" in info                      # a real process ran only if the agent recorded an exit code
    try:
        rec = head.get_provenance(exit_code=info.get("exit_code") if executed else None)
        rec = rec.model_dump() if hasattr(rec, "model_dump") else dict(rec)
    except Exception as e:
        # double exception: no measurement exists -> fail closed with NULLs and an explicit empty observed list
        return {"agent": "HEAD", "provider": getattr(head, "provider", "unknown"), "error": str(e)[:300],
                "executable": None, "version": None, "invocation_command": None,
                "exit_code": None, "duration_sec": None, "observed_fields": [], "declared_fields": [],
                "authentication_mode": None, "api_key_policy": "disabled", "api_key_used": False}
    if rec.get("observed_fields") is None:
        observed = ["executable", "version", "exit_code", "duration_sec", "invocation_command"]
    else:
        observed = list(rec["observed_fields"])         # an explicit [] stays []
    if executed:
        rec["exit_code"] = info.get("exit_code")
        rec["duration_sec"] = info.get("execution_duration_sec")
        if "execution_duration_sec" not in info:
            observed = [f for f in observed if f != "duration_sec"]
    else:
        # pre-execution exception: no process was observed -> NULLs; only configuration remains (not observed)
        rec["exit_code"] = None
        rec["duration_sec"] = None
        rec["version"] = None
        rec["invocation_command"] = None
        observed = []
    rec["observed_fields"] = observed
    rec["agent"] = "HEAD"
    rec["api_key_used"] = False
    return rec


def _mission_runner(sid: str) -> MissionRunner:
    cm = ChatManager()

    def notify(text, run_id=None, role="SYSTEM"):
        try:
            cm.add_message(sid, role, text, run_id=run_id)
        except Exception:
            pass
        invalidate_status_cache()
    return MissionRunner(orchestrator_factory=_new_orchestrator, store=MissionStore(), notify=notify)


def _run_mission_thread(sid: str, mission_id: str, key: str, reopen: bool = False) -> None:
    """Runs a mission to its end in the background while holding the project's run slot."""
    runner = _mission_runner(sid)

    def go():
        try:
            if reopen:
                runner.reopen(mission_id)
            runner.run(mission_id)
        except Exception as e:
            import traceback
            traceback.print_exc()
            runner.notify(f"미션 {mission_id} 실행 오류: {str(e)[:500]}")
        finally:
            with _RUN_LOCK:
                if _ACTIVE_PROJECTS.get(key) == mission_id:
                    _ACTIVE_PROJECTS.pop(key, None)
            invalidate_status_cache()
    threading.Thread(target=go, name=f"director-mission-{mission_id}", daemon=True).start()


def _start_mission(sid: str, orch: Orchestrator, project_path: str, goal: str, order, notes, profile: Optional[str] = None,
                   source: str = "chat", request_id: Optional[str] = None, auto_apply: Optional[str] = None) -> Optional[str]:
    key = _project_key(project_path)
    with _RUN_LOCK:
        if key in _ACTIVE_PROJECTS or _project_busy(orch.sm, project_path):
            ChatManager().add_message(sid, "SYSTEM", BUSY_MESSAGE)
            return None
        mission = _mission_runner(sid).create(project_path, goal, order, config=orch.config, notes=notes, profile=profile,
                                              source=source, request_id=request_id, auto_apply=auto_apply)
        _ACTIVE_PROJECTS[key] = mission["mission_id"]
    ChatManager().add_message(
        sid, "SYSTEM",
        f"미션 시작 {mission['mission_id']}: HEAD가 목표 달성까지 단계별로 계획·판단합니다 (최대 {mission['max_steps']}단계). "
        "결제·실계좌 주문·자격증명·외부 비가역 작업만 사용자에게 확인합니다.")
    _run_mission_thread(sid, mission["mission_id"], key)
    return mission["mission_id"]


_USAGE_BACKFILLED = {"done": False}
STAGE_KO = {"PLANNING": "계획", "ORDER_READY": "구현 준비", "BUILDING": "코드 작성", "VERIFYING": "테스트",
            "REVIEWING": "독립 검토", "DECIDING": "HEAD 판단", "PASS": "통과", "FAIL": "실패",
            "HUMAN_REQUIRED": "사용자 확인", "TIMEOUT": "시간 초과", "IDLE": "대기", "MISSION_PLANNING": "다음 단계 계획",
            "ENTRY_REVIEW": "오더 검토", "CHAT": "대화"}


def build_activity_payload(limit_missions: int = 5) -> dict:
    """What the three AIs are doing and have done: missions step by step, who is working right now, per-role work
    (calls, CLI time, tokens) and the latest pipeline events. Read-only; shared by 8080 and the 8081 console."""
    from core import usage
    from core.mission import _pid_alive
    store = MissionStore()
    missions = sorted(store.all(), key=lambda m: m.get("updated_at", ""), reverse=True)
    if not _USAGE_BACKFILLED["done"]:
        try:
            from core.db_path import get_runs_dir
            usage.backfill_from_runs(get_runs_dir(), missions)
        except Exception:
            pass
        _USAGE_BACKFILLED["done"] = True
    sm = StateMachine()
    views = [mission_view(m, sm) for m in missions[:limit_missions]]
    focus = next((v for v in views if v["live"]), views[0] if views else None)
    running = usage.running_now()
    for r in running:
        r["stage_ko"] = STAGE_KO.get(r.get("stage"), r.get("stage") or "")
        r["role_ko"] = usage.ROLE_NAME.get(r.get("role"), r.get("role") or "")
    with get_db_conn() as conn:
        events = [dict(e) for e in conn.execute(
            "SELECT t.timestamp AS at, t.run_id, t.to_state, substr(t.details, 1, 200) AS details, r.project_path "
            "FROM state_transitions t JOIN runs r ON r.run_id = t.run_id ORDER BY t.id DESC LIMIT 25").fetchall()]
    for e in events:
        e["project"] = Path(e.pop("project_path") or "").name
        e["state_ko"] = STAGE_KO.get(e["to_state"], e["to_state"])
    return {"server_time": datetime.utcnow().isoformat(), "missions": views,
            "focus_mission_id": focus["mission_id"] if focus else None, "working_now": running,
            "usage": usage.summary(focus["mission_id"] if focus else None), "recent_calls": usage.recent_calls(15),
            "events": events, "names": {"provider": usage.PROVIDER_NAME, "role": usage.ROLE_NAME}}


def build_replay_payload(mission_id: str = "") -> dict:
    """Recorded AI calls of one finished mission (default: the latest one with recorded calls) so the console can
    replay how the three AIs worked. Real ledger data only; read-only."""
    from core import usage
    missions = sorted(MissionStore().all(), key=lambda m: m.get("updated_at", ""), reverse=True)
    for m in missions:
        if mission_id and m["mission_id"] != mission_id:
            continue
        if not mission_id and m.get("status") == "RUNNING":
            continue
        calls = usage.mission_calls(m["mission_id"])
        if not calls and not mission_id:
            continue
        steps = [{"index": s["index"], "run_id": s.get("run_id"),
                  "state": (s.get("result") or {}).get("final_state"),
                  "goal": (s.get("order") or {}).get("goal", "")[:160],
                  "tests": (s.get("result") or {}).get("tests")} for s in m.get("steps", [])]
        return {"mission_id": m["mission_id"], "goal": m.get("goal", "")[:400], "status": m.get("status"),
                "project": Path(m.get("project_path", "")).name, "max_steps": m.get("max_steps"),
                "steps": steps, "calls": calls}
    return {"mission_id": None, "calls": [], "steps": []}


def mission_view(m: dict, sm: Optional[StateMachine] = None) -> dict:
    """One mission as the console, the bridge and Solomon see it (status, steps, deliverable, apply state)."""
    from core.mission import _pid_alive, deliverable_run_id
    sm = sm or StateMachine()

    def step_view(step):
        res = step.get("result") or {}
        row = sm.get_run(step["run_id"]) if step.get("run_id") else None
        state = (row or {}).get("current_state") or res.get("final_state")
        return {"index": step["index"], "goal": (step.get("order") or {}).get("goal", "")[:220],
                "run_id": step.get("run_id"), "state": state, "state_ko": STAGE_KO.get(state, state),
                "tests": res.get("tests"), "review": res.get("review_verdict"), "repairs": res.get("repair_rounds"),
                "builds_on_failed_attempt": bool(step.get("builds_on_failed_attempt")),
                "note": (res.get("stop_detail") or "")[:220] if state not in ("PASS", None) else ""}
    applied = m.get("applied") or None
    return {"mission_id": m["mission_id"], "goal": m.get("goal", "")[:400], "status": m.get("status"),
            "live": m.get("status") == "RUNNING" and _pid_alive(m.get("owner_pid")),
            "project": Path(m.get("project_path", "")).name, "project_path": m.get("project_path"),
            "max_steps": m.get("max_steps"), "profile": m.get("profile", "standard"), "source": m.get("source", "chat"),
            "request_id": m.get("request_id"),
            "passed_steps": sum(1 for s in m.get("steps", []) if (s.get("result") or {}).get("final_state") == "PASS"),
            "steps": [step_view(s) for s in m.get("steps", [])],
            "summary": (m.get("summary") or "")[:1200], "blocked_reason": (m.get("blocked_reason") or "")[:500],
            "deliverable": m.get("base_worktree"), "deliverable_run_id": deliverable_run_id(m),
            "applied": applied, "apply_state": (applied or {}).get("status") or ("pending" if m.get("base_worktree") and m.get("status") != "RUNNING" else None),
            "created_at": m.get("created_at"), "updated_at": m.get("updated_at")}


def start_goal_mission(project_path: str, goal: str, profile: Optional[str] = None, source: str = "api",
                       request_id: Optional[str] = None, create_project: bool = False, auto_apply: Optional[str] = None) -> dict:
    """Goal in, mission out: HEAD turns the goal into an ORDER, the proxy gate narrows it, and the mission runs in
    the background. Used by the Solomon bridge and the CLI. Returns {status: started|busy|question|escalated|
    rejected, ...}; nothing here bypasses the entry gate or the Scope Guard."""
    from core.bootstrap import prepare_project
    sid = active_session_id()
    cm = ChatManager()
    if not cm.get_session(sid):
        cm.create_session(sid, "", _configured_head_domain())
    tag_profile, goal = autopilot.split_profile_tag(goal)
    profile = profile or tag_profile or "standard"
    if profile not in autopilot.PROFILE_DEFAULTS and profile not in (autopilot.setting(load_config(), "profiles", {}) or {}):
        return {"status": "rejected", "error": f"unknown profile {profile!r}"}
    if not goal or not goal.strip():
        return {"status": "rejected", "error": "goal required"}
    try:
        if create_project:
            project_path = prepare_project(project_path)
        ok, info = validate_project_path(project_path)
        if not ok:
            return {"status": "rejected", "error": info}
        project_path = info
        orch = Orchestrator(config=load_config())
    except Exception as e:
        return {"status": "rejected", "error": str(e)[:300]}
    label = {"solomon": "솔로몬 CEO 지시", "cli": "CLI 지시", "api": "외부 지시"}.get(source, source)
    cm.add_message(sid, "USER", f"[{label} · {profile}] {goal}")
    try:
        previous = latest_deliverable(project_path) if autopilot.enabled(orch.config) else None
        project_context = orch._read_project_context(previous or project_path)
        if previous:
            project_context["note"] = "Continuing from the latest mission deliverable (isolated worktree) of this project; the listed files already exist there."
        from core import usage
        usage.set_context(stage="CHAT", run_id=None, mission_id=None)
        resp = orch.head.chat(goal, project_context, [])
    except Exception as e:
        cm.add_message(sid, "SYSTEM", f"HEAD Error: {str(e)[:500]}")
        return {"status": "rejected", "error": "HEAD unavailable: " + str(e)[:300]}
    if resp.get("type") == "QUESTION":
        cm.add_message(sid, "HEAD", resp.get("message", ""))
        return {"status": "question", "message": resp.get("message", "")}
    if resp.get("type") != "DEV_TASK" or "order" not in resp:
        return {"status": "rejected", "error": "HEAD did not produce a work order", "message": resp.get("message", "")}
    try:
        order = OrderSchema(**resp["order"])
    except Exception as e:
        return {"status": "rejected", "error": f"order invalid: {str(e)[:300]}"}
    cm.add_message(sid, "HEAD", resp.get("message", ""))
    gate = autopilot.entry_gate(order, project_path, orch.head, cwd=project_path)
    order = gate["order"]
    if gate["notes"]:
        cm.add_message(sid, "SYSTEM", "HEAD 대리 처리: " + "; ".join(gate["notes"])[:1500])
    if gate["escalate"]:
        run_id = _record_human_required(orch, project_path, order.goal, gate["escalate"], order)
        cm.add_message(sid, "SYSTEM", f"HUMAN_REQUIRED: {gate['escalate']}", run_id=run_id)
        return {"status": "escalated", "run_id": run_id, "reason": gate["escalate"]}
    mission_id = _start_mission(sid, orch, project_path, goal, order, gate["notes"], profile=profile, source=source,
                                request_id=request_id, auto_apply=auto_apply)
    if not mission_id:
        return {"status": "busy", "error": BUSY_MESSAGE}
    return {"status": "started", "mission_id": mission_id, "project_path": project_path, "profile": profile}


STOP_INTENT = re.compile(r"(중단|중지|정지|멈춰|멈추|그만|스톱|\bstop\b|\bhalt\b|\babort\b)", re.IGNORECASE)


def is_stop_request(msg: str) -> bool:
    """A short chat message whose point is 'stop' (e.g. '즉시 작업 중단', '멈춰', 'stop everything').
    Long messages that merely mention stopping are HEAD's business, not an operator command."""
    text = (msg or "").strip()
    return 0 < len(text) <= 80 and bool(STOP_INTENT.search(text)) and not re.search(r"(하지\s*마|말고|않|않고|말아)", text)


def stop_missions(reason: str = "", mission_id: Optional[str] = None, by: str = "사용자") -> dict:
    """작업 정지: mark RUNNING missions STOPPED and kill the CLI processes working on them. The dashboard stays up.
    Records and worktrees are kept; the mission can be continued later with reopen (decide: extend/replan)."""
    from core.mission import stop_request
    from core.process_runner import ProcessTreeRunner
    store = MissionStore()
    targets = [m for m in store.all() if m.get("status") == "RUNNING" and (not mission_id or m["mission_id"] == mission_id)]
    stopped = [stop_request(m["mission_id"], reason, by, store)["mission_id"] for m in targets]
    # one named mission: only its own processes; "stop everything": every CLI the console has running
    killed = ProcessTreeRunner.kill_active(owners=stopped if mission_id else None) if stopped else []
    sid = active_session_id()
    if stopped:
        ChatManager().add_message(sid, "SYSTEM", f"[{by}] 작업 정지: 미션 {', '.join(stopped)} 정지 · 실행 중이던 AI 프로세스 {len(killed)}개 종료 · 기록과 작업본은 남습니다" + (f" · 사유: {reason[:200]}" if reason else ""))
    invalidate_status_cache()
    return {"status": "stopped" if stopped else "idle", "missions": stopped, "killed": killed}


def decide_mission(mission_id: str, action: str, reason: str = "", max_steps: Optional[int] = None,
                   on_conflict: str = "block", by: str = "HUMAN") -> dict:
    """A decision about a finished mission: apply its deliverable, discard it, extend its budget and continue,
    or send a blocked step back to HEAD planning. The stopped run itself is never approved by this."""
    from core.mission import deliverable_run_id
    store = MissionStore()
    m = store.load(mission_id)
    if not m:
        return {"status": "error", "error": "mission not found"}
    sid = active_session_id()
    if action == "stop":
        return stop_missions(reason, mission_id, by=by)
    if action == "apply":
        rid = deliverable_run_id(m)
        if not rid:
            return {"status": "blocked", "blocks": ["no PASSED milestone to apply"]}
        result = _result_manager().apply_tree(rid, request_id=f"{by.lower()}-{mission_id}-{datetime.utcnow().strftime('%H%M%S')}",
                                              applied_by=by, on_conflict=on_conflict)
        m = store.load(mission_id) or m
        m["applied"] = {"run_id": rid, "at": datetime.utcnow().isoformat(), "by": by, **{k: v for k, v in result.items() if k != "request_id"}}
        store.save(m)
        ChatManager().add_message(sid, "SYSTEM", f"[{by}] 미션 {mission_id} 결과물 적용: {result.get('status')}"
                                  + (f" · {len(result.get('files', []))}개 파일" if result.get("files") else "")
                                  + (" · " + "; ".join(result.get("blocks", []))[:300] if result.get("blocks") else ""), run_id=rid)
        invalidate_status_cache()
        return result
    if action == "discard":
        rid = deliverable_run_id(m)
        result = _result_manager().discard(rid, request_id=f"{by.lower()}-{mission_id}", by=by) if rid else {"status": "refused", "reason": "no deliverable"}
        m["applied"] = {"run_id": rid, "at": datetime.utcnow().isoformat(), "by": by, "status": "discarded" if result.get("status") == "discarded" else result.get("status"), "reason": reason[:300]}
        store.save(m)
        ChatManager().add_message(sid, "SYSTEM", f"[{by}] 미션 {mission_id} 결과물 폐기: {result.get('status')} {reason[:200]}")
        invalidate_status_cache()
        return result
    if action in ("extend", "replan"):
        if m.get("status") == "RUNNING":
            return {"status": "busy", "error": "mission is still running"}
        key = _project_key(m["project_path"])
        with _RUN_LOCK:
            if key in _ACTIVE_PROJECTS:
                return {"status": "busy", "error": BUSY_MESSAGE}
            if max_steps:
                m["max_steps"] = max(int(max_steps), len(m["steps"]) + 1)
            elif action == "extend":
                m["max_steps"] = len(m["steps"]) + 2
            if action == "replan":
                m["steps"][-1]["operator_replan"] = (reason or f"{by} re-plan")[:300]
            store.save(m)
            _ACTIVE_PROJECTS[key] = mission_id
        ChatManager().add_message(sid, "SYSTEM", f"[{by}] 미션 {mission_id} {'재계획' if action == 'replan' else '연장'}: 단계 예산 {m['max_steps']}" + (f" · {reason[:200]}" if reason else ""))
        _run_mission_thread(sid, mission_id, key, reopen=True)
        return {"status": "resumed", "mission_id": mission_id, "max_steps": m["max_steps"]}
    return {"status": "error", "error": f"unknown action {action!r}"}


def build_digest_payload() -> dict:
    """What a CEO needs at a glance: what is running, what waits for a decision, what waits for the operator, what was
    delivered recently, and today's AI spend. Read-only."""
    from core import usage
    from core.mission import _pid_alive
    store = MissionStore()
    sm = StateMachine()
    missions = sorted(store.all(), key=lambda m: m.get("updated_at", ""), reverse=True)
    views = [mission_view(m, sm) for m in missions[:40]]
    running = [v for v in views if v["live"]]
    awaiting = [v for v in views if v["status"] in ("DONE", "INCOMPLETE") and v["deliverable"] and not v["applied"]]
    blocked = [v for v in views if v["status"] == "BLOCKED" or (v["status"] == "RUNNING" and not v["live"])]
    week = (datetime.utcnow() - timedelta(days=7)).isoformat()
    recent = [v for v in views if (v.get("updated_at") or "") >= week]
    summary = usage.summary(None)
    return {"server_time": datetime.utcnow().isoformat(),
            "running": running, "awaiting_decision": awaiting[:10], "needs_david": blocked[:10],
            "recent": [{k: v[k] for k in ("mission_id", "project", "goal", "status", "passed_steps", "apply_state", "updated_at")} for v in recent[:15]],
            "counts_7d": {s: sum(1 for v in recent if v["status"] == s) for s in ("DONE", "INCOMPLETE", "BLOCKED", "RUNNING")},
            "applied_7d": sum(1 for v in recent if (v.get("applied") or {}).get("status") == "applied"),
            "usage_today": summary.get("today", {}).get("total"), "working_now": usage.running_now(),
            "pending_results": len(_result_manager().pending_results())}


def resume_interrupted_missions() -> list:
    """Server start: continue RUNNING missions whose process died (reboot/crash). One mission per project."""
    started = []
    for m in missions_to_resume():
        key = _project_key(m["project_path"])
        with _RUN_LOCK:
            if key in _ACTIVE_PROJECTS:
                continue
            _ACTIVE_PROJECTS[key] = m["mission_id"]
        sid = active_session_id()
        ChatManager().add_message(sid, "SYSTEM", f"재시작 후 미션 자동 재개: {m['mission_id']} ({m['goal'][:200]})")
        _run_mission_thread(sid, m["mission_id"], key)
        started.append(m["mission_id"])
    return started


def handle_chat_message(msg: str):
    cm = ChatManager()
    pm = ProviderManager()
    sid = active_session_id()

    # Ensure session (project_path stays empty until the user selects one explicitly)
    if not cm.get_session(sid):
        cm.create_session(sid, "", _configured_head_domain())

    cm.add_message(sid, "USER", msg)

    # "멈춰" is an operator command, not a conversation: stop the running mission(s) here instead of asking HEAD
    # to talk about stopping (2026-11-02: HEAD acknowledged '즉시 작업 중단' in words while the mission kept building).
    if is_stop_request(msg):
        result = stop_missions(msg, by="사용자")
        if result["status"] == "idle":
            cm.add_message(sid, "SYSTEM", "정지할 실행 중 미션이 없습니다. (대기 상태)")
        return

    # Cheap health probe (binary + --version, bounded recheck of stale failures). No inference calls.
    try:
        pm.check_health()
    except Exception:
        pass
    session = cm.get_session(sid) or {}
    project_path = (session.get("project_path") or "").strip()

    try:
        orch = Orchestrator(config=load_config())
    except Exception as e:
        cm.add_message(sid, "SYSTEM", f"Orchestrator init failed: {str(e)}")
        return

    # Only the provider that this chat will actually call (the HEAD) may be a recovery-trial candidate.
    head_domain = orch._get_agent_domain(orch.head)
    pm.mark_trial_candidates(only=[head_domain] if head_domain not in ("MOCK", "UNKNOWN") else [])
    mode, down = pm.get_failover_mode()
    if mode == FailoverMode.STOP:
        cm.add_message(sid, "SYSTEM", f"SYSTEM STOP: insufficient healthy providers. Down: {', '.join(down)}")
        return
    if head_domain in pm.trial_candidates:
        # claim exactly at the moment of use; atomic and cooldown-bound (a failed trial keeps its cooldown)
        if pm.claim_trial(head_domain):
            cm.add_message(sid, "SYSTEM", f"RECOVERY TRIAL: {head_domain} is RECOVERING; this request is its single verification call")
        else:
            cm.add_message(sid, "SYSTEM", f"SYSTEM STOP: {head_domain} is RECOVERING and its trial is on cooldown or owned by another process")
            return

    hist = cm.get_messages(sid)

    try:
        if project_path:
            previous = latest_deliverable(project_path) if autopilot.enabled(orch.config) else None
            project_context = orch._read_project_context(previous or project_path)
            if previous:
                project_context["note"] = ("Continuing from the latest mission deliverable (isolated worktree) of this "
                                           "project; the listed files already exist there.")
        else:
            project_context = {"project_path": None, "existing_files": [], "note": "NO PROJECT SELECTED"}
        from core import usage
        usage.set_context(stage="CHAT", run_id=None, mission_id=None)
        resp = orch.head.chat(msg, project_context, hist)
    except Exception as e:
        # real call failed: record the real provider state (quota/auth/timeout) or leave it (config/unknown)
        head_domain = orch._get_agent_domain(orch.head)
        if head_domain not in ("MOCK", "UNKNOWN"):
            try:
                pm.parse_cli_error(head_domain, str(e))
            except Exception:
                pass
        # provenance + audit of the failed HEAD call before terminating this request
        try:
            failed_prov = _head_provenance_record(orch.head)
            if failed_prov:
                cm.add_provenance(sid, failed_prov, outcome="HEAD_EXCEPTION")
        except Exception:
            pass
        try:
            orch.sm.log_audit(f"CHAT:{sid}", "HEAD", "HEAD_EXCEPTION",
                              json.dumps({"error": str(e)[:500], "provider": head_domain}))
        except Exception:
            pass
        cm.add_message(sid, "SYSTEM", f"HEAD Error: {str(e)}")
        return

    # real call succeeded (exit 0): the only path that promotes RECOVERING/failed -> HEALTHY
    try:
        orch._record_provider_success(orch.head)
    except Exception:
        pass

    head_prov = _head_provenance_record(orch.head)
    if head_prov:
        try:
            cm.add_provenance(sid, head_prov, outcome="OK")
        except Exception:
            pass

    if resp.get("type") == "QUESTION":
        cm.add_message(sid, "HEAD", resp.get("message", ""))
        return

    if resp.get("type") != "DEV_TASK":
        cm.add_message(sid, "SYSTEM", "Error: Unknown response type from HEAD")
        return

    if "order" not in resp:
        cm.add_message(sid, "SYSTEM", "Error: DEV_TASK missing order JSON")
        return

    try:
        order = OrderSchema(**resp["order"])
    except Exception as e:
        cm.add_message(sid, "SYSTEM", f"Error parsing order: {str(e)}")
        return

    # AUTO-RESOLVE FIRST: a chat-generated ORDER commonly gets path formatting wrong (drive-absolute
    # instead of repo-relative, or protecting an out-of-project sibling that the Builder can't reach
    # anyway). Fix that representational mistake here, once, before Scope Guard ever evaluates the
    # order - validate_order_paths()/assess_order_for_auto_run() below are completely unmodified and
    # still fail closed on any genuine attempt to read/write outside the project.
    if project_path:
        from core.entry_repair import repair_root_scope
        try:
            order, repair_notes = repair_root_scope(order, project_path, orch.head)
            if repair_notes:
                cm.add_message(sid, 'SYSTEM', 'ORDER_PATH_REPAIR: ' + '; '.join(repair_notes))
        except Exception as exc:
            cm.add_message(sid, 'SYSTEM', 'ORDER_PATH_REPAIR_FAILED: ' + str(exc)[:250])

    cm.add_message(sid, "HEAD", resp.get("message", ""))

    # 1. Project selection is mandatory for execution
    if not project_path:
        run_id = _record_human_required(orch, "", order.goal, "PROJECT_SELECTION_REQUIRED", order)
        cm.add_message(sid, "SYSTEM", NO_PROJECT_MESSAGE, run_id=run_id)
        return
    ok, info = validate_project_path(project_path)
    if not ok:
        run_id = _record_human_required(orch, project_path, order.goal, f"PROJECT_SELECTION_REQUIRED: {info}", order)
        cm.add_message(sid, "SYSTEM", f"HUMAN_REQUIRED: PROJECT_SELECTION_REQUIRED ({info})", run_id=run_id)
        return

    # 2a. HEAD proxy (user-delegated): narrow/repair the ORDER, HEAD approves on the user's behalf, and the request
    #     runs as a goal-driven MISSION. Only money / credentials / irreversible external actions reach the user.
    if autopilot.enabled(orch.config):
        gate = autopilot.entry_gate(order, project_path, orch.head, cwd=project_path)
        order = gate["order"]
        if gate["notes"]:
            cm.add_message(sid, "SYSTEM", "HEAD 대리 처리: " + "; ".join(gate["notes"])[:1500])
        if gate["escalate"]:
            run_id = _record_human_required(orch, project_path, order.goal, gate["escalate"], order)
            cm.add_message(sid, "SYSTEM", f"HUMAN_REQUIRED: {gate['escalate']}", run_id=run_id)
            return
        tag_profile, _rest = autopilot.split_profile_tag(msg)
        _start_mission(sid, orch, project_path, msg, order, gate["notes"], profile=tag_profile)
        return

    # 2. Entry approval policy: LOW/MEDIUM risk + valid + clearly scoped -> autonomous; otherwise HUMAN_REQUIRED
    reasons = SafetyPolicy.assess_order_for_auto_run(order)
    if reasons:
        reason = "APPROVAL_REQUIRED: " + "; ".join(reasons)
        run_id = _record_human_required(orch, project_path, order.goal, reason, order)
        cm.add_message(sid, "SYSTEM", f"HUMAN_REQUIRED: {reason}", run_id=run_id)
        return

    # 3. Concurrency guard (in-process registry + DB-visible active runs)
    key = _project_key(project_path)
    with _RUN_LOCK:
        if key in _ACTIVE_PROJECTS or _project_busy(orch.sm, project_path):
            cm.add_message(sid, "SYSTEM", BUSY_MESSAGE)
            return
        try:
            run_id = orch.reserve_run_id(project_path, order.goal)   # reserved IDLE row, unique id, adopted by orch.run()
        except Exception as e:
            cm.add_message(sid, "SYSTEM", f"Run allocation failed: {str(e)}")
            return
        _ACTIVE_PROJECTS[key] = run_id

    cm.add_message(sid, "SYSTEM", f"Starting background run {run_id}...", run_id=run_id)

    def run_orch():
        try:
            orch.run(project_path, order.goal, run_id=run_id, order=order, head_provenance=head_prov or None)
            final = (orch.sm.get_run(run_id) or {}).get("current_state", "UNKNOWN")
            cm.add_message(sid, "SYSTEM", f"Run {run_id} finished: {final}", run_id=run_id)
        except Exception as e:
            import traceback
            traceback.print_exc()
            cm.add_message(sid, "SYSTEM", f"Run failed: {str(e)}", run_id=run_id)
        finally:
            try:
                row = orch.sm.get_run(run_id) or {}
                if row.get("current_state") == State.IDLE:
                    orch.sm.transition_to(run_id, State.FAIL, details="RUN_NOT_STARTED: run ended before its first state transition")
            except Exception:
                pass
            with _RUN_LOCK:
                if _ACTIVE_PROJECTS.get(key) == run_id:
                    _ACTIVE_PROJECTS.pop(key, None)

    t = threading.Thread(target=run_orch, name=f"director-run-{run_id}", daemon=True)
    t.start()


def build_status_payload() -> dict:
    """Read-only snapshot shared by the initial page render and the /status poller (one source of truth).
    Never marks, claims or probes anything."""
    with get_db_conn() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM provider_health')
        providers = {row['provider']: dict(row) for row in cursor.fetchall()}
        cursor.execute('SELECT * FROM runs ORDER BY created_at DESC')
        all_active = cursor.fetchall()

    current_run = None
    last_run = None
    stale_runs = []
    now = datetime.utcnow()
    for row in all_active:
        if str(row['run_id']).startswith("RUN-TEST-"):
            continue
        if last_run is None:
            last_run = row
        if row['current_state'] in TERMINAL_STATES:
            continue
        updated = datetime.fromisoformat(row['updated_at'])
        if (now - updated).total_seconds() > ACTIVE_RUN_WINDOW_SEC:
            stale_runs.append(row)
        elif current_run is None:
            current_run = row
    if current_run is None:
        current_run = last_run

    raw_state = current_run['current_state'] if current_run else "NONE"
    state = raw_state
    recovered = None
    if current_run and raw_state == State.FAIL:
        from core.db_path import get_runs_dir
        recovery_path = get_runs_dir() / current_run['run_id'] / "recovery.json"
        try:
            candidate = json.loads(recovery_path.read_text(encoding="utf-8"))
            if candidate.get("status") == "VERIFIED" and candidate.get("project_path") == current_run['project_path']:
                recovered = candidate
        except (OSError, ValueError, TypeError):
            recovered = None
    last_stage, last_details = None, ""
    if current_run:
        try:
            with get_db_conn() as conn:
                tr = conn.execute("SELECT from_state, details FROM state_transitions WHERE run_id = ? ORDER BY id DESC LIMIT 1",
                                  (current_run['run_id'],)).fetchone()
            if tr:
                last_stage = tr['from_state']
                last_details = (tr['details'] or "")[:300]
        except Exception:
            pass
    if recovered:
        last_details = ('수동 복구 기록 있음 · 전체 파이프라인 재검증 미완료. ' + str(recovered.get('summary') or ''))[:300]
    preview = _effective_mode_preview()
    cm = ChatManager()
    sid = active_session_id()
    session = cm.get_session(sid) or {}
    recovery = _recovery_summary(preview, providers)
    prov_out = {}
    for name in ('OPENAI', 'ANTHROPIC', 'GOOGLE'):
        data = providers.get(name, {}) or {}
        usage = data.get('usage_percent', None)
        prov_out[name] = {
            "status": str(data.get('status', 'UNKNOWN')),
            "role": str(data.get('current_role', 'UNKNOWN')),
            "version": str(data.get('version', 'UNKNOWN')),
            "latency": data.get('last_latency_sec', 0.0) or 0.0,
            "usage": f"{usage}%" if usage is not None else "UNKNOWN",
            "error": str(data.get('failure_reason', 'NONE') or 'NONE'),
        }
    unprobed = bool(providers) and preview["mode"].value == "NORMAL" and not any(d.get('version') for d in providers.values())
    payload = {
        "server_time": now.isoformat(),
        "system_mode": preview["mode"].value,
        "system_mode_hint": "providers not checked yet" if unprobed else _mode_hint(preview["mode"].value, preview.get("statuses", {}), preview.get("trial_candidates", [])),
        "recovery_trial": _recovery_trial_line(preview, providers),
        "recovery_short": recovery["short"],
        "recovery_short_ko": recovery.get("short_ko") or recovery["short"],
        "recovery_details": recovery["details"],
        "session_id": sid,
        "providers": prov_out,
        "run": {
            "run_id": current_run['run_id'] if current_run else "NONE",
            "state": state,
            "created_at": current_run['created_at'] if current_run else "",
            "updated_at": current_run['updated_at'] if current_run else "",
            "last_stage": last_stage,
            "details": last_details,
            "human_required": "YES" if state == "HUMAN_REQUIRED" else "NO",
            "stale": bool(stale_runs),
        },
        "stale_runs": [{"run_id": r['run_id'], "state": r['current_state'], "updated_at": r['updated_at']} for r in stale_runs],
        "project_path": (session.get("project_path") or "").strip(),
        "project_name": Path((session.get("project_path") or "").strip()).name if (session.get("project_path") or "").strip() else "",
        "head_busy": chat_in_flight(),
        "activity": _node_activity(state, prov_out),
        "system_mode_sentence": "아직 AI 상태를 확인하지 않았습니다. 첫 작업을 시작할 때 확인합니다." if unprobed else I18N.mode_sentence(preview["mode"].value, preview.get("statuses", {})),
        "stage_sentence": I18N.stage_sentence(state, {d: prov_out[d]["role"] for d in prov_out}),
        "next_step": I18N.NEXT_STEP.get(state, ""),
        "approval": approval_info(current_run['run_id'], raw_state, last_details) if current_run else {"required": False},
        "pending": pending_actions(),
        "result": result_info(current_run['run_id']) if current_run and raw_state == State.PASS else {"is_pass": False},
        "results": _result_manager().pending_results(),
        "research": build_research_status_payload(),
    }
    payload = payload if isinstance(payload, dict) else {}
    payload["human_required_count"] = len(payload["pending"])
    payload["pending_result_count"] = len(payload["results"])
    act = payload["activity"]
    payload["active_provider"] = next((d for d, n in (act.get("nodes") or {}).items() if n.get("active")), None)
    ap = payload["approval"]
    if ap.get("required"):
        first = (ap.get("items") or [{}])[0]
        payload["todo"] = (first.get("file") + " 변경 승인 필요") if first.get("file") else (I18N.reason_ko(first.get("code", ""))[0] + " 승인 필요")
    elif payload["human_required_count"]:
        payload["todo"] = f"승인 대기 {payload['human_required_count']}건"
    elif payload["pending_result_count"]:
        payload["todo"] = f"적용 대기 결과물 {payload['pending_result_count']}건"
    else:
        payload["todo"] = I18N.t("nothing_todo")
    return payload


_RESEARCH_STAGE_KO = {
    "RESEARCH_INTAKE": "연구 준비",
    "INDEPENDENT_RESEARCH": "3개 독립 제안",
    "CROSS_CRITIQUE": "교차 비평",
    "SYNTHESIS": "가설 합성",
    "EXPERIMENT_PLANNING": "실험 계획",
    "EXPERIMENT_BUILDING": "실험 구현",
    "EXPERIMENT_VERIFYING": "실험 검증",
    "EXPERIMENT_REVIEW": "독립 검토",
    "RESULT_INTERPRETATION": "결과 해석",
    "NEXT_CYCLE": "다음 사이클 준비",
    "MILESTONE_COMPLETE": "마일스톤 완료",
    "HUMAN_REQUIRED": "사용자 확인 필요",
    "RESEARCH_STOPPED": "연구 중지",
}


def _research_json(value, fallback):
    """Decode persisted research JSON without allowing malformed evidence to break the dashboard."""
    if value is None or value == "":
        return fallback
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return decoded if isinstance(decoded, type(fallback)) else fallback


def build_research_status_payload(session_id: Optional[str] = None) -> dict:
    """Pure read-only TRIAD snapshot.

    Importantly, this does not construct ``ResearchStateMachine``: an old/frozen production DB with no
    V3 tables is observed as unavailable instead of being migrated by a GET request.
    """
    empty = {
        "mode": "TRIAD_RESEARCH", "available": False, "active": False, "session_id": None,
        "moonshot": "", "project_path": "", "status": "NONE", "cycle_number": 0,
        "cycle_budget": 0, "current_stage": "NONE", "current_stage_ko": "연구 없음",
        "proposals": [], "cross_critiques": [], "synthesis": None, "experiment": None,
        "evidence": {}, "interpretations": [], "rejected_ideas": [], "what_we_learned": "",
        "next_step": "TRIAD 연구 세션을 시작하면 여기에 진행 상황이 표시됩니다.",
        "provenance": [],
    }
    required = {
        "research_sessions", "research_cycles", "research_stage_outputs", "research_call_provenance",
        "research_experiments", "research_baseline_evidence", "research_memory",
    }
    try:
        with get_db_conn() as conn:
            present = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'research_%'"
            ).fetchall()}
            if not required.issubset(present):
                return empty
            if session_id:
                session = conn.execute(
                    "SELECT * FROM research_sessions WHERE session_id=?", (session_id,)
                ).fetchone()
            else:
                session = conn.execute(
                    "SELECT * FROM research_sessions ORDER BY updated_at DESC, created_at DESC LIMIT 1"
                ).fetchone()
            if not session:
                result = dict(empty); result["available"] = True
                return result
            s = dict(session)
            cycle = conn.execute(
                "SELECT * FROM research_cycles WHERE session_id=? ORDER BY cycle_number DESC LIMIT 1",
                (s["session_id"],),
            ).fetchone()
            outputs = []
            provenance = []
            experiment = baseline = None
            memory_rows = []
            if cycle:
                outputs = [dict(r) for r in conn.execute(
                    "SELECT * FROM research_stage_outputs WHERE cycle_id=? ORDER BY created_at, domain",
                    (cycle["cycle_id"],),
                ).fetchall()]
                provenance = [dict(r) for r in conn.execute(
                    "SELECT * FROM research_call_provenance WHERE cycle_id=? ORDER BY id",
                    (cycle["cycle_id"],),
                ).fetchall()]
                experiment = conn.execute(
                    "SELECT * FROM research_experiments WHERE cycle_id=?", (cycle["cycle_id"],)
                ).fetchone()
                baseline = conn.execute(
                    "SELECT * FROM research_baseline_evidence WHERE cycle_id=?", (cycle["cycle_id"],)
                ).fetchone()
            memory_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM research_memory WHERE session_id=? ORDER BY id DESC", (s["session_id"],)
            ).fetchall()]
    except (sqlite3.Error, OSError):
        return empty

    decoded_outputs = []
    for row in outputs:
        payload = _research_json(row.pop("payload_json", None), {})
        row["payload"] = payload
        decoded_outputs.append(row)

    def stage_outputs(stage):
        return [r for r in decoded_outputs if r.get("stage") == stage]

    proposals = []
    for row in stage_outputs("INDEPENDENT_RESEARCH"):
        p = row["payload"]
        proposals.append({
            "domain": row.get("domain"), "hypothesis": p.get("hypothesis", ""),
            "rationale": p.get("rationale", ""), "proposed_experiment": p.get("proposed_experiment", ""),
            "confidence": p.get("confidence"), "freeform_insight": p.get("freeform_insight", ""),
        })
    critiques = [{"domain": r.get("domain"), **r["payload"]} for r in stage_outputs("CROSS_CRITIQUE")]
    synthesis_rows = stage_outputs("SYNTHESIS")
    synthesis = synthesis_rows[-1]["payload"] if synthesis_rows else None
    interpretation_rows = stage_outputs("RESULT_INTERPRETATION_INDEPENDENT") + stage_outputs("RESULT_INTERPRETATION_CROSS")
    interpretations = [{"domain": r.get("domain"), "stage": r.get("stage"), **r["payload"]} for r in interpretation_rows]

    experiment_out = None
    evidence = {}
    if experiment:
        e = dict(experiment)
        order = _research_json(e.pop("order_json", None), {})
        result = _research_json(e.pop("result_json", None), {})
        experiment_out = {
            "status": e.get("status"), "normal_run_id": e.get("normal_run_id"),
            "normal_final_state": e.get("normal_final_state"), "worktree_path": e.get("worktree_path"),
            "goal": order.get("goal", ""), "allowed_paths": order.get("allowed_paths", []), "result": result,
        }
        evidence["experiment"] = result
    if baseline:
        b = dict(baseline)
        evidence["baseline"] = _research_json(b.pop("evidence_json", None), {})
        evidence["baseline_state"] = b.get("baseline_state")

    memories = []
    for row in memory_rows:
        payload = _research_json(row.get("payload_json"), {})
        memories.append(payload)
    latest_memory = memories[0] if memories else {}
    rejected = []
    for mem in memories:
        rejected.extend(str(x) for x in (mem.get("rejected_ideas") or []) if str(x).strip())

    clean_provenance = []
    for p in provenance:
        clean_provenance.append({
            "stage": p.get("stage"), "domain": p.get("domain"), "role": p.get("research_role"),
            "executable": p.get("executable"), "version": p.get("version"), "exit_code": p.get("exit_code"),
            "duration_sec": p.get("duration_sec"), "outcome": p.get("outcome"),
            "observed_fields": _research_json(p.get("observed_fields"), []),
            "declared_fields": _research_json(p.get("declared_fields"), []),
        })

    stage = (dict(cycle).get("stage") if cycle else s.get("current_stage")) or "NONE"
    active = s.get("status") == "ACTIVE"
    next_step = latest_memory.get("next_direction") or {
        "RESEARCH_INTAKE": "세 제공자의 준비 상태를 확인합니다.",
        "INDEPENDENT_RESEARCH": "서로 격리된 세 제안을 수집합니다.",
        "CROSS_CRITIQUE": "제안의 약점과 결합 가능성을 교차 검토합니다.",
        "SYNTHESIS": "실험 가능한 단일 가설을 합성합니다.",
        "EXPERIMENT_PLANNING": "안전한 실험 ORDER를 작성합니다.",
        "EXPERIMENT_BUILDING": "NORMAL 안전 파이프라인으로 실험을 구현합니다.",
        "EXPERIMENT_VERIFYING": "실험 결과와 회귀를 검증합니다.",
        "EXPERIMENT_REVIEW": "독립 Reviewer가 변경을 검토합니다.",
        "RESULT_INTERPRETATION": "세 연구자가 결과를 독립 해석합니다.",
        "NEXT_CYCLE": "학습 내용을 반영해 다음 사이클을 준비합니다.",
    }.get(stage, s.get("stopped_reason") or "")
    return {
        "mode": "TRIAD_RESEARCH", "available": True, "active": active,
        "session_id": s.get("session_id"), "moonshot": s.get("moonshot", ""),
        "project_path": s.get("project_path", ""), "status": s.get("status", "UNKNOWN"),
        "cycle_number": int(dict(cycle).get("cycle_number") or 0) if cycle else int(s.get("current_cycle") or 0),
        "cycle_budget": int(s.get("cycle_budget") or 0), "current_stage": stage,
        "current_stage_ko": _RESEARCH_STAGE_KO.get(stage, stage), "proposals": proposals,
        "cross_critiques": critiques, "synthesis": synthesis, "experiment": experiment_out,
        "evidence": evidence, "interpretations": interpretations, "rejected_ideas": rejected,
        "what_we_learned": latest_memory.get("lesson", ""), "next_step": next_step,
        "provenance": clean_provenance,
    }


def chat_messages_after(after_id: int, session_id: Optional[str] = None) -> list:
    """Messages with id > after_id (append-only polling; the page never re-renders old messages)."""
    session_id = session_id or active_session_id()
    with get_db_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, run_id, created_at FROM chat_messages WHERE session_id = ? AND id > ? ORDER BY id ASC",
            (session_id, int(after_id))).fetchall()
    return [dict(r) for r in rows]


DASHBOARD_JS = r"""
(function () {
  var I18N = window.__I18N || { status: {}, state: {}, result: {}, role: {}, stage: {}, ui: {}, events: [], reasons: {}, provider: {} };
  function ko(map, raw) { return (map && map[raw]) ? map[raw] : raw; }
  var POLL_MS = 5000;                                   // adaptive: 3s active run, 5s human-required, 12s idle
  var pollStatusTimer = null, pollMsgTimer = null, lastStatusMode = '';
  function scheduleStatus(ms) { if (pollStatusTimer) clearTimeout(pollStatusTimer); pollStatusTimer = setTimeout(function () { pollStatus(); }, ms); }
  function scheduleMessages(ms) { if (pollMsgTimer) clearTimeout(pollMsgTimer); pollMsgTimer = setTimeout(function () { pollMessages(); }, ms); }
  var STAGES = ['HEAD', 'BUILD', 'VERIFY', 'REVIEW', 'DECIDE'];
  var STATE_INDEX = { PLANNING: 0, ORDER_READY: 0, BUILDING: 1, VERIFYING: 2, REVIEWING: 3, DECIDING: 4 };
  var TERMINAL = { PASS: 1, RECOVERED: 1, FAIL: 1, HUMAN_REQUIRED: 1, TIMEOUT: 1 };
  var EVENT_LABELS = [
    [/RESULT_APPLIED/, 'RESULT APPLIED', 'ev-ok'], [/RESULT_ROLLED_BACK|RESULT_BLOCKED/, 'APPLY BLOCKED', 'ev-bad'], [/RESULT_DISCARDED/, 'RESULT DISCARDED', 'ev-info'],
    [/finished: PASS/, 'RUN PASS', 'ev-ok'], [/finished: HUMAN_REQUIRED/, 'HUMAN REQUIRED', 'ev-bad'],
    [/finished: (FAIL|TIMEOUT)/, 'RUN FAILED', 'ev-bad'], [/Starting background run/, 'RUN STARTED', 'ev-info'],
    [/RECOVERY TRIAL/, 'RECOVERY TRIAL', 'ev-info'], [/SYSTEM STOP/, 'SYSTEM STOP', 'ev-bad'],
    [/PROJECT_SELECTED/, 'PROJECT SELECTED', 'ev-ok'], [/PROJECT_SELECTION_REJECTED|PROJECT_SELECTION_REQUIRED/, 'PROJECT REJECTED', 'ev-bad'],
    [/NEW_CHAT/, 'NEW CHAT', 'ev-ok'], [/HUMAN_REQUIRED|APPROVAL_REQUIRED/, 'HUMAN REQUIRED', 'ev-bad'],
    [/BUSY/, 'BUSY', 'ev-bad'], [/HEAD Error|Error|failed/, 'ERROR', 'ev-bad']
  ];
  var chatBox = document.getElementById('chat-box');
  var form = document.getElementById('chat-form');
  var input = document.getElementById('chat-input');
  var sendBtn = document.getElementById('send-btn');
  var sendStatus = document.getElementById('send-status');
  var lastId = parseInt(chatBox.getAttribute('data-last-id') || '0', 10);
  var sending = false;
  var serverBusy = false;
  var runInfo = { created_at: '', updated_at: '', state: 'NONE' };

  function setText(id, text) { var el = document.getElementById(id); if (el && el.textContent !== text) el.textContent = text; }
  function setStatusCell(id, status, map) {
    var el = document.getElementById(id); if (!el) return;
    var label = ko(map || I18N.status, status);
    if (el.textContent !== label) el.textContent = label;
    el.setAttribute('data-raw', status);
    el.className = 'status-' + status;
  }
  function eventLabel(text) {
    for (var i = 0; i < EVENT_LABELS.length; i++) if (EVENT_LABELS[i][0].test(text)) return [EVENT_LABELS[i][1], EVENT_LABELS[i][2]];
    return ['SYSTEM', 'ev-info'];
  }
  function eventKo(text) {
    for (var i = 0; i < (I18N.events || []).length; i++) { try { if (new RegExp(I18N.events[i][0]).test(text)) return [I18N.events[i][1], I18N.events[i][2]]; } catch (e) {} }
    return ['· 시스템 알림', 'ev-info'];
  }
  function nearBottom() { return chatBox.scrollTop + chatBox.clientHeight >= chatBox.scrollHeight - 40; }
  function appendMessage(m) {
    var stick = nearBottom();
    var div = document.createElement('div');
    div.className = 'msg msg-' + m.role; div.setAttribute('data-id', m.id);
    if (m.role === 'SYSTEM') {
      var lk = eventLabel(m.content); var kk = eventKo(m.content);
      var tag = document.createElement('span'); tag.className = 'ev ' + lk[1]; tag.textContent = lk[0];
      var txt = document.createElement('span'); txt.className = 'evt'; txt.textContent = kk[0]; txt.title = m.content; txt.setAttribute('data-raw', m.content);
      var raw = document.createElement('span'); raw.className = 'evraw'; raw.textContent = m.content;
      div.appendChild(tag); div.appendChild(txt); div.appendChild(raw);
      div.addEventListener('click', function () { div.classList.toggle('expanded'); });
    } else {
      var strong = document.createElement('strong'); strong.textContent = m.role;
      var pre = document.createElement('pre'); pre.textContent = m.content;
      div.appendChild(strong); div.appendChild(pre);
    }
    chatBox.appendChild(div);
    if (stick) chatBox.scrollTop = chatBox.scrollHeight;     // only auto-scroll when the reader was already at the bottom
  }
  function updateSendUi() {
    var busy = sending || serverBusy;
    sendBtn.disabled = busy;
    var nb = document.getElementById('new-chat-btn'); if (nb) nb.disabled = busy;
    var badge = document.getElementById('chat-badge');
    if (sending) { sendStatus.textContent = 'HEAD is working...'; sendStatus.setAttribute('data-ko', ko(I18N.ui, 'head_working')); }
    else if (serverBusy) { sendStatus.textContent = 'BUSY: HEAD is working on another request...'; }
    else if (sendStatus.textContent.indexOf('HEAD is working') === 0 || sendStatus.textContent.indexOf('BUSY') === 0) sendStatus.textContent = '';
    if (badge) {
      var txt = (sending || serverBusy) ? ko(I18N.ui, 'head_working') : (runInfo.state === 'HUMAN_REQUIRED' ? ko(I18N.ui, 'human_required') : ko(I18N.result, 'READY'));
      badge.setAttribute('data-raw', (sending || serverBusy) ? 'HEAD is working...' : (runInfo.state === 'HUMAN_REQUIRED' ? 'HUMAN_REQUIRED' : 'READY'));
      if (badge.textContent !== txt) badge.textContent = txt;
      badge.className = 'badge ' + ((sending || serverBusy) ? 'b-busy' : (runInfo.state === 'HUMAN_REQUIRED' ? 'b-red' : 'b-green'));
    }
    var codex = document.getElementById('node-OPENAI');
    if (codex) codex.classList.toggle('chat-active', !!(sending || serverBusy));   // HEAD chat call in flight = Codex working
  }
  function renderPipeline(state, lastStage) {
    var cur = STATE_INDEX[state];
    var terminal = !!TERMINAL[state];
    var stopAt = terminal ? STATE_INDEX[lastStage] : -1;
    for (var i = 0; i < STAGES.length; i++) {
      var el = document.getElementById('stage-' + i); if (!el) continue;
      var mark, cls;
      if (state === 'PASS') { mark = '\u2713'; cls = 'done'; }
      else if (terminal) {
        var stop = (stopAt === undefined || stopAt === null) ? -1 : stopAt;
        if (i < stop) { mark = '\u2713'; cls = 'done'; }
        else if (i === stop) { mark = state === 'HUMAN_REQUIRED' ? '\u26a0' : '\u2717'; cls = state === 'HUMAN_REQUIRED' ? 'warn' : 'failed'; }
        else { mark = '\u25cb'; cls = 'todo'; }
      }
      else if (cur === undefined) { mark = '\u25cb'; cls = 'todo'; }
      else { mark = i < cur ? '\u2713' : (i === cur ? '\u25cf' : '\u25cb'); cls = i < cur ? 'done' : (i === cur ? 'active' : 'todo'); }
      var m = el.querySelector('.mark'); if (m && m.textContent !== mark) m.textContent = mark;
      el.className = 'stage ' + cls;
    }
    var pb = document.getElementById('pipeline-badge');
    if (pb) {
      var t = (state === 'PASS' || state === 'RECOVERED') ? ko(I18N.state, state) : (state === 'HUMAN_REQUIRED' ? ko(I18N.ui, 'human_required') : (state === 'FAIL' || state === 'TIMEOUT' ? ko(I18N.state, state) : (cur !== undefined ? '진행 중' : '')));
      pb.setAttribute('data-raw', state);
      if (pb.textContent !== t) pb.textContent = t;
      pb.className = 'badge ' + (state === 'PASS' ? 'b-green' : (terminal ? 'b-red' : (cur !== undefined ? 'b-blue' : 'b-muted')));
      pb.style.display = t ? '' : 'none';
    }
  }
  var approvalBusy = false;
  var lastApprovalKey = '';
  var selectedRunId = null;                              // a pending run chosen from PENDING ACTIONS (never changes the DB current run)
  var lastPendingKey = '';
  function actionRunId() { return selectedRunId || runInfo.run_id; }
  var resultBusy = false; var lastResultKey = ''; var lastResultsKey = ''; var selectedResultId = null; var pendingConfirm = null;
  var resultsExpanded = false; var resultModalOpen = false;
  function setResultModalOpen(v) { resultModalOpen = v; var ov = document.getElementById('result-modal-overlay'); if (ov) { ov.style.display = v ? 'flex' : 'none'; ov.classList.toggle('open', v); } }
  function resultRunId() { return selectedResultId || runInfo.run_id; }
  function makeResultLi(p) {
    var li = document.createElement('li'); li.className = 'pend res' + (p.run_id === selectedResultId ? ' sel' : '');
    var a = document.createElement('div'); a.className = 'pname'; a.textContent = p.project_name;
    var b = document.createElement('div'); b.className = 'ptitle2 blue'; b.textContent = '완료 \u00b7 변경 파일 ' + p.changed_count + '개 \u00b7 적용 대기';
    var c = document.createElement('div'); c.className = 'prun mono'; c.textContent = p.run_id.slice(0, 26);
    li.appendChild(a); li.appendChild(b); li.appendChild(c);
    li.addEventListener('click', function () {
      var turningOn = selectedResultId !== p.run_id;
      selectedResultId = turningOn ? p.run_id : null;
      lastResultsKey = ''; lastResultKey = '';
      setResultModalOpen(turningOn);
      renderResults(window.__lastResultsList || []);
      refreshSelectedResult();
    });
    return li;
  }
  // only the latest 3 results show by default (page height never grows); the rest live in a grouped,
  // height-capped 전체 보기 panel so past PASS runs never crowd the fixed 1080p layout
  function renderResults(list) {
    list = list || []; window.__lastResultsList = list;
    setText('results-count', ko(I18N.ui, 'results') + ' ' + list.length + '건'); var rc = document.getElementById('results-count'); if (rc) rc.setAttribute('data-raw', 'RESULTS (' + list.length + ')');
    var key = JSON.stringify(list.map(function (p) { return [p.run_id, p.result_state, p.changed_count]; })) + '|' + resultsExpanded + '|' + selectedResultId;
    if (key === lastResultsKey) return;
    lastResultsKey = key;
    var head = list.slice(0, 3); var restCount = Math.max(0, list.length - 3);
    var ul = document.getElementById('results-list');
    if (ul) { while (ul.firstChild) ul.removeChild(ul.firstChild); head.forEach(function (p) { ul.appendChild(makeResultLi(p)); }); }
    var moreRow = document.getElementById('results-more-row'); var moreBtn = document.getElementById('results-more-btn');
    if (moreRow) moreRow.style.display = restCount > 0 ? '' : 'none';
    if (moreBtn) moreBtn.textContent = resultsExpanded ? ko(I18N.ui, 'close') : ('나머지 ' + restCount + '건 보기');
    var grouped = document.getElementById('results-grouped');
    if (grouped) {
      grouped.style.display = resultsExpanded ? '' : 'none';
      while (grouped.firstChild) grouped.removeChild(grouped.firstChild);
      if (resultsExpanded) {
        var groups = {}; var order = [];
        list.forEach(function (p) { var nm = p.project_name || ''; if (!groups[nm]) { groups[nm] = []; order.push(nm); } groups[nm].push(p); });
        var cur = window.__curProjectName || '';
        order.sort(function (x, y) { if (x === cur && y !== cur) return -1; if (y === cur && x !== cur) return 1; return x.localeCompare(y); });
        order.forEach(function (nm) {
          var det = document.createElement('details'); det.className = 'resgroup'; if (nm === cur) det.open = true;
          var sum = document.createElement('summary'); sum.textContent = (nm || '(project)') + ' (' + groups[nm].length + ')'; det.appendChild(sum);
          var iul = document.createElement('ul');
          groups[nm].forEach(function (p) { iul.appendChild(makeResultLi(p)); });
          det.appendChild(iul); grouped.appendChild(det);
        });
      }
    }
    var box = document.getElementById('results-panel'); if (box) box.className = 'pendbox ' + (list.length ? 'has' : 'none');
    // a selected result stays on screen after apply/discard (so RESULT APPLIED / DISCARDED is visible) until closed
  }
  function refreshSelectedResult() {
    if (!selectedResultId) { renderResult(lastResult); return; }
    fetch('/result?run_id=' + encodeURIComponent(selectedResultId), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) { window.__selres = j; renderResult(j); }).catch(function () {});
  }
  var lastResult = null;
  function renderResult(res) {
    res = res || { is_pass: false };
    var st = (res.result || {}).state || 'READY';
    var show = !!res.is_pass;
    var card = document.getElementById('result-card'); if (!card) return;
    var isCurrent = !!(res.run_id && runInfo && res.run_id === runInfo.run_id);
    var chatResult = document.getElementById('chat-result-card');
    if (chatResult) chatResult.style.display = (show && isCurrent && runInfo.state === 'PASS' && st === 'READY') ? '' : 'none';
    var overlay = document.getElementById('result-modal-overlay');
    if (overlay) { overlay.style.display = (show && resultModalOpen) ? 'flex' : 'none'; overlay.classList.toggle('open', show && resultModalOpen); }
    // 결과물 카드는 항상 모달(Result Center)에서만 뜬다 - 다른 run의 result가 CURRENT RUN 열에 섞이지 않는다
    if (!show) { if (isCurrent || !res.run_id) { setText('run-result-state', ''); var rrs0 = document.getElementById('run-result-state'); if (rrs0) rrs0.setAttribute('data-raw', ''); } lastResultKey = ''; return; }
    var key = JSON.stringify([res.run_id, st, res.changed_count, (res.control_plane || []).length, resultBusy, pendingConfirm]);
    if (key === lastResultKey) return;
    lastResultKey = key;
    var blocked = (res.control_plane || []).length > 0 || !res.worktree_exists;
    var rawLabel = st === 'APPLIED' ? 'RESULT APPLIED' : (st === 'DISCARDED' ? 'RESULT DISCARDED' : (st.indexOf('ROLLED_BACK') >= 0 ? 'APPLY ROLLED BACK' : (st === 'APPLY_BLOCKED' ? 'APPLY BLOCKED' : (blocked ? 'APPLY BLOCKED' : 'RESULT READY'))));
    var label = st === 'APPLIED' ? ko(I18N.ui, 'result_applied') : (st === 'DISCARDED' ? ko(I18N.ui, 'result_discarded') : (st.indexOf('ROLLED_BACK') >= 0 ? ko(I18N.ui, 'apply_rolled_back') : (st === 'APPLY_BLOCKED' || blocked ? ko(I18N.ui, 'apply_blocked') : ko(I18N.ui, 'result_ready'))));
    var rh = document.getElementById('result-headline'); if (rh) rh.setAttribute('data-raw', rawLabel);
    setText('result-headline', label + (res.run_id && res.run_id !== runInfo.run_id ? ' \u00b7 ' + res.run_id.slice(0, 22) : ''));
    card.className = 'resultcard ' + (st === 'APPLIED' ? 'r-applied' : (st === 'DISCARDED' ? 'r-discarded' : ((st.indexOf('ROLLED_BACK') >= 0 || st === 'APPLY_BLOCKED' || blocked) ? 'r-blocked' : 'r-ready')));
    var detail = (res.result || {}).detail || '';
    var blockedKo = function (txt) { var e = (I18N.reasons || {}); var code = /Main project changed/.test(txt) ? 'BASE_COMMIT_MISMATCH' : (/not clean/.test(txt) ? 'WORKING_TREE_DIRTY' : (/collides/.test(txt) ? 'UNTRACKED_COLLISION' : (/Control/.test(txt) ? 'CONTROL_PLANE_MODIFIED' : ''))); return code && e[code] ? (e[code].why + ' (상세 코드: ' + code + ')') : txt; };
    setText('result-sub', st === 'APPLIED' ? '프로젝트가 업데이트되었습니다 \u00b7 테스트 통과' : (st === 'DISCARDED' ? '원본 프로젝트는 변경되지 않았습니다 \u00b7 검증 증거 보존' : (st.indexOf('ROLLED_BACK') >= 0 || st === 'APPLY_BLOCKED' ? blockedKo(detail) : (blocked ? ((res.control_plane || []).length ? 'AI DEV DIRECTOR 보호 파일이 결과에 포함되어 적용할 수 없습니다: ' + res.control_plane.join(', ') : '작업 결과 폴더가 없습니다') : '프로젝트에 적용할 준비가 되었습니다'))));
    var rs = document.getElementById('result-sub'); if (rs) rs.setAttribute('data-raw', detail);
    var t = res.tests || {};
    setText('result-files', ko(I18N.ui, 'changed_files') + ' ' + (res.changed_count === undefined ? '?' : res.changed_count) + '개  (추가 ' + ((res.counts || {}).A || 0) + ' / 수정 ' + ((res.counts || {}).M || 0) + ' / 삭제 ' + ((res.counts || {}).D || 0) + ')');
    var rf = document.getElementById('result-files'); if (rf) rf.setAttribute('data-raw', 'Changed files: ' + res.changed_count);
    setText('result-tests', ko(I18N.ui, 'tests') + ' ' + (t.post_passed ? ko(I18N.ui, 'pass') : ko(I18N.ui, 'fail')) + '  \u00b7  ' + ko(I18N.ui, 'review') + ' ' + ((t.reviewer || '').toLowerCase() === 'pass' ? ko(I18N.ui, 'pass') : ko(I18N.ui, 'fail')) + '  \u00b7  ' + ko(I18N.ui, 'safety') + ' ' + (t.final_gate === 'PASS' ? ko(I18N.ui, 'pass') : (t.final_gate || '?')));
    if (isCurrent) {
      var rrs = document.getElementById('run-result-state'); if (rrs) rrs.setAttribute('data-raw', 'RESULT: ' + (st === 'READY' && blocked ? 'APPLY BLOCKED' : st));
      setText('run-result-state', ko(I18N.ui, 'result_state') + ': ' + (st === 'READY' && blocked ? ko(I18N.ui, 'apply_blocked') : ko(I18N.result, st)));
    }
    var canAct = show && st !== 'APPLIED' && st !== 'DISCARDED' && !resultBusy;
    ['result-apply-btn', 'result-discard-btn'].forEach(function (id) { var b = document.getElementById(id); if (b) { b.style.display = (st === 'APPLIED' || st === 'DISCARDED') ? 'none' : ''; b.disabled = !canAct || (id === 'result-apply-btn' && blocked); } });
    var ow = document.getElementById('result-open-btn'); if (ow) { ow.style.display = res.worktree_exists && st !== 'DISCARDED' ? '' : 'none'; ow.disabled = resultBusy; }
    var op = document.getElementById('result-openproj-btn'); if (op) op.style.display = st === 'APPLIED' ? '' : 'none';
    var cf = document.getElementById('result-confirm'); if (cf) cf.style.display = pendingConfirm ? '' : 'none';
    if (pendingConfirm) {
      setText('confirm-title', pendingConfirm === 'apply' ? ko(I18N.ui, 'apply_confirm_title') : ko(I18N.ui, 'discard_confirm_title'));
      var pname = (res.project_path || '').split(/[\\/]/).filter(Boolean).pop() || '';
      setText('confirm-body', pendingConfirm === 'apply'
        ? ('프로젝트: ' + pname + '\n작업: ' + (res.run_id || '') + '\n변경 파일: ' + res.changed_count + '개\n\n' + ko(I18N.ui, 'apply_confirm_body'))
        : ko(I18N.ui, 'discard_confirm_body'));
      setText('confirm-ok', pendingConfirm === 'apply' ? ko(I18N.ui, 'apply_confirm_ok') : ko(I18N.ui, 'discard_confirm_ok'));
    }
  }
  function resultAction(kind) {
    if (resultBusy) return;
    var runId = resultRunId(); if (!runId) return;
    resultBusy = true; pendingConfirm = null; lastResultKey = ''; renderResult(lastResultShown()); setText('result-status', kind === 'apply' ? 'Applying & verifying\u2026' : 'Discarding\u2026');
    var rid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now() + '-' + Math.random().toString(16).slice(2));
    fetch(kind === 'apply' ? '/result/apply' : '/result/discard', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
      body: new URLSearchParams({ run_id: runId, request_id: rid }).toString() })
    .then(function (r) { return r.json(); })
    .then(function (j) { var sk = {applied: '적용 완료', discarded: '폐기 완료', blocked: '적용할 수 없음', rolled_back: '실패 · 복구됨', duplicate: '이미 처리됨', busy: '다른 적용 진행 중'}; setText('result-status', (kind === 'apply' ? '적용: ' : '폐기: ') + (sk[j.status] || j.status || '') + (j.reason ? ' \u2014 ' + j.reason : '') + (j.blocks ? ' \u2014 ' + j.blocks.join('; ') : '') + (j.files ? ' \u2014 ' + j.files.join(', ') : '')); var rst = document.getElementById('result-status'); if (rst) rst.setAttribute('data-raw', (kind === 'apply' ? 'Apply: ' : 'Discard: ') + (j.status || '')); })
    .catch(function (e) { setText('result-status', 'Request failed: ' + e); })
    .then(function () { resultBusy = false; lastResultKey = ''; pollStatus().then(refreshSelectedResult); pollMessages(); });
  }
  function lastResultShown() { return selectedResultId ? (window.__selres || lastResult) : lastResult; }
  function openAction(kind) {
    var runId = resultRunId(); if (!runId) return;
    fetch(kind === 'worktree' ? '/result/open_worktree' : '/result/open_project', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
      body: new URLSearchParams({ run_id: runId }).toString() })
    .then(function (r) { return r.json(); }).then(function (j) { setText('result-status', 'Open: ' + (j.status || '') + (j.reason ? ' \u2014 ' + j.reason : '') + (j.path ? ' \u2014 ' + j.path : '')); }).catch(function () {});
  }
  function showResultDetail(tab) {
    var runId = resultRunId(); if (!runId) return;
    fetch('/result?run_id=' + encodeURIComponent(runId), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      window.__selres = j;
      var panel = document.getElementById('result-detail'); if (!panel) return;
      panel.style.display = ''; panel.setAttribute('data-tab', tab);
      document.getElementById('rd-tab-changes').className = 'ghost' + (tab === 'changes' ? ' on' : '');
      document.getElementById('rd-tab-tests').className = 'ghost' + (tab === 'tests' ? ' on' : '');
      var files = document.getElementById('rd-files'); while (files.firstChild) files.removeChild(files.firstChild);
      var diff = document.getElementById('rd-diff');
      var head = document.getElementById('rd-head');
      head.textContent = 'Run ' + j.run_id + '\nRequest: ' + (j.goal || '') + '\nProject: ' + (j.project_path || '') + '\nBuilder: ' + ((j.builder || {}).provider || '') + ' ' + ((j.builder || {}).version || '') + ' exit=' + ((j.builder || {}).exit_code) + '\nReviewer: ' + ((j.reviewer || {}).verdict || '') + '\nApprovals: ' + (j.approvals || []).map(function (a) { return a.action; }).join(', ');
      if (tab === 'changes') {
        (j.changed_files || []).forEach(function (c) {
          var li = document.createElement('li'); li.textContent = c.type + ' ' + c.path; li.className = 'rdfile t-' + c.type;
          li.addEventListener('click', function () { diff.textContent = (j.file_diffs || {})[c.path] || '(no diff)'; [].slice.call(files.children).forEach(function (x) { x.classList.remove('sel'); }); li.classList.add('sel'); });
          files.appendChild(li);
        });
        var first = (j.changed_files || [])[0];
        diff.textContent = first ? ((j.file_diffs || {})[first.path] || '') : '(no changes)';
        if (files.firstChild) files.firstChild.classList.add('sel');
        setText('rd-evidence', 'Final evidence: ' + JSON.stringify(j.final_evidence || {}) + '\nPreconditions: ' + JSON.stringify((j.preconditions || {}).blocks || []));
      } else {
        var t = j.tests || {};
        diff.textContent = 'TEST RESULTS\n\nBaseline:   ' + JSON.stringify(t.baseline) + '\nPost-build: ' + JSON.stringify(t.post) + '\nRegression: ' + (t.regression || '') + '\nScope: ' + (t.scope || '') + '\nCanonical containment: ' + (t.canonical || '') + '\nReviewer: ' + (t.reviewer || '') + '\nFinal Gate: ' + (t.final_gate || '') + '\n\nVerifier summary: ' + (t.verifier_summary || '') + '\n\n--- verifier output (tail) ---\n' + (t.verifier_output_tail || '');
        setText('rd-evidence', 'Final evidence: ' + JSON.stringify(j.final_evidence || {}));
      }
    }).catch(function () {});
  }
  var rbind = function (id, fn) { var b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
  rbind('result-view-btn', function () { showResultDetail('changes'); });
  rbind('result-tests-btn', function () { showResultDetail('tests'); });
  rbind('rd-tab-changes', function () { showResultDetail('changes'); });
  rbind('rd-tab-tests', function () { showResultDetail('tests'); });
  rbind('rd-close', function () { document.getElementById('result-detail').style.display = 'none'; });
  rbind('result-open-btn', function () { openAction('worktree'); });
  rbind('result-openproj-btn', function () { openAction('project'); });
  rbind('result-apply-btn', function () { pendingConfirm = 'apply'; lastResultKey = ''; renderResult(lastResultShown()); });
  rbind('result-discard-btn', function () { pendingConfirm = 'discard'; lastResultKey = ''; renderResult(lastResultShown()); });
  rbind('confirm-cancel', function () { pendingConfirm = null; lastResultKey = ''; renderResult(lastResultShown()); });
  rbind('confirm-ok', function () { var k = pendingConfirm; if (k) resultAction(k); });
  function openCurrentResultModal() { selectedResultId = null; setResultModalOpen(true); lastResultKey = ''; renderResult(lastResult); }
  rbind('chat-view-result-btn', function () { openCurrentResultModal(); showResultDetail('changes'); });
  rbind('todo-view-result-btn', function () { openCurrentResultModal(); });
  rbind('results-more-btn', function () { resultsExpanded = !resultsExpanded; lastResultsKey = ''; renderResults(window.__lastResultsList || []); });
  var resModalOverlayEl = document.getElementById('result-modal-overlay');
  if (resModalOverlayEl) resModalOverlayEl.addEventListener('click', function (ev) { if (ev.target === resModalOverlayEl) { setResultModalOpen(false); selectedResultId = null; lastResultKey = ''; renderResult(lastResultShown()); } });
  rbind('result-close-btn', function () { setResultModalOpen(false); selectedResultId = null; lastResultsKey = ''; lastResultKey = ''; renderResult(lastResult); pollStatus(); });
  function renderPending(list) {
    list = list || [];
    var key = JSON.stringify(list.map(function (p) { return [p.run_id, p.status, p.title, p.file]; }));
    setText('pending-count', ko(I18N.ui, 'pending_actions') + ' ' + list.length + '건'); var pc = document.getElementById('pending-count'); if (pc) pc.setAttribute('data-raw', 'PENDING ACTIONS (' + list.length + ')');
    if (key === lastPendingKey) return;
    lastPendingKey = key;
    var ul = document.getElementById('pending-list'); if (!ul) return;
    while (ul.firstChild) ul.removeChild(ul.firstChild);
    list.forEach(function (p) {
      var li = document.createElement('li'); li.className = 'pend' + (p.run_id === selectedRunId ? ' sel' : '');
      var a = document.createElement('div'); a.className = 'pname'; a.textContent = p.project_name + (p.status === 'RESUMING' ? ' \u00b7 재개 중' : '');
      var b = document.createElement('div'); b.className = 'ptitle2'; b.textContent = (p.file ? p.file + ' 변경 승인 필요' : (p.title + ' 승인 필요')) + ' \u00b7 ' + (p.stage === 'POST_BUILD' ? '코드 작성 후' : '작업 시작 전'); b.title = p.title_en || p.title;
      var c = document.createElement('div'); c.className = 'prun mono'; c.textContent = p.run_id.slice(0, 26);
      li.appendChild(a); li.appendChild(b); li.appendChild(c);
      li.addEventListener('click', function () { selectedRunId = (selectedRunId === p.run_id) ? null : p.run_id; lastPendingKey = ''; lastApprovalKey = ''; refreshSelectedApproval(); renderPending(list); });
      ul.appendChild(li);
    });
    var pl = document.getElementById('pending-panel'); if (pl) pl.className = 'pendbox ' + (list.length ? 'has' : 'none');
    if (selectedRunId && !list.some(function (p) { return p.run_id === selectedRunId; })) { selectedRunId = null; lastApprovalKey = ''; renderApproval(lastApproval); }
  }
  function refreshSelectedApproval() {
    if (!selectedRunId) { renderApproval(lastApproval); return; }
    fetch('/run/approval?run_id=' + encodeURIComponent(selectedRunId), { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (ap) { renderApproval(ap); }).catch(function () {});
  }
  function retryPromptJs(code, file) {
    var r = (I18N.reasons || {})[code] || {};
    var loc = file ? (file + '은(는) ') : '';
    return loc + '수정하지 말고, ' + (r.title || '해당 문제') + ' 부분을 작업 범위에서 제외한 뒤 기존 요청을 다시 계획해줘.';
  }
  function renderApproval(ap) {
    ap = ap || { required: false };
    var key = JSON.stringify([ap.required, ap.run_id, ap.headline, ap.rejected, ap.approved, ap.inflight, ap.stage, (ap.blocking && ap.blocking[0] && ap.blocking[0].code) || '']);
    var show = !!(ap.required || ap.inflight || ap.rejected || (ap.state === 'HUMAN_REQUIRED' && ap.reason_raw && ap.reason_raw.indexOf('APPROVAL_REQUIRED') === 0));
    var resolved = !!(ap.rejected || ap.inflight || ap.approved);
    var nonapprovable = show && !ap.required && !resolved;
    ['approval-card', 'chat-action-card'].forEach(function (id) { var el = document.getElementById(id); if (el) el.style.display = show ? '' : 'none'; });
    var ael = document.getElementById('approval-approvable'); if (ael) ael.style.display = ap.required ? '' : 'none';
    var nel = document.getElementById('approval-nonapprovable'); if (nel) nel.style.display = nonapprovable ? '' : 'none';
    var btns = ['approve-btn', 'reject-btn', 'chat-approve-btn', 'chat-reject-btn', 'alert-approve-btn', 'alert-reject-btn'];
    btns.forEach(function (id) { var b = document.getElementById(id); if (b) { b.style.display = ap.required ? '' : 'none'; b.disabled = approvalBusy || !ap.required; } });
    if (key === lastApprovalKey) return;                 // polling never rebuilds the card while nothing changed
    lastApprovalKey = key;
    var item = (ap.items && ap.items[0]) || {};
    var block = (ap.blocking && ap.blocking[0]) || {};
    var rk = function (it) { var r = (I18N.reasons || {})[it.code]; return r ? { title: r.title, why: r.why } : { title: it.title || '', why: it.why || '' }; };
    var hl = ap.required ? ko(I18N.ui, 'human_approval_required') : (ap.rejected ? '사용자가 거절함' : (ap.inflight || ap.approved ? '승인됨 \u00b7 작업 재개 중' : ko(I18N.ui, 'not_approvable')));
    var ah = document.getElementById('approval-headline'); if (ah) ah.setAttribute('data-raw', ap.headline || '');
    setText('approval-headline', hl + (ap.run_id && ap.run_id !== runInfo.run_id ? ' \u00b7 ' + ap.run_id.slice(0, 22) : ''));
    setText('approval-stage', ap.stage === 'POST_BUILD' ? '코드 작성 후 검증 단계 (POST-BUILD / VERIFYING)' : (ap.stage === 'ENTRY' ? '작업 시작 전 (ENTRY)' : (ap.stage_label || '')));
    setText('approval-builder', ap.builder === 'COMPLETED' ? '코드 작성 완료' : (ap.builder === 'NOT RUN' ? '아직 실행 안 됨' : (ap.builder || '')));
    var an = document.getElementById('approval-next'); if (an) an.setAttribute('data-raw', ap.next || '');
    setText('approval-next', ap.required ? ko(I18N.ui, 'human_required_sub') : (ap.rejected ? '거절됨: 이 작업은 실행되지 않습니다.' : (ap.inflight || ap.approved ? '승인됨: 전체 안전 검사를 거쳐 작업을 계속합니다.' : (ap.state === 'HUMAN_REQUIRED' ? ko(I18N.ui, 'not_approvable_sub') : ''))));
    setText('approval-reason', item.code ? rk(item).title : ((ap.explain || {}).title || item.title || ''));
    setText('approval-file', (ap.items || []).map(function (i) { return i.file; }).filter(Boolean).join(', ') || (item.file || ''));
    setText('approval-why', item.code ? rk(item).why : ((ap.explain || {}).why || item.why || ''));
    setText('approval-raw', (ko(I18N.ui, 'technical_reason') + ': ' + (item.code || block.code || (ap.explain || {}).code || '') + ' \u00b7 ' + (ap.reason_raw || '')).slice(0, 400));
    var rt = item.code ? rk(item).title : (item.title || ''); setText('chat-action-text', hl + (rt ? ' \u2014 ' + rt + (item.file ? ' (' + item.file + ')' : '') : ''));
    setText('chat-action-next', ap.required ? ko(I18N.ui, 'human_required_sub') : '');
    var card = document.getElementById('approval-card'); if (card) card.className = 'approval ' + (ap.required ? 'ap-need' : (ap.rejected ? 'ap-rejected' : 'ap-done'));
    if (nonapprovable) {
      var bk = rk(block);
      setText('ap-na-desc', bk.title); setText('ap-na-why', bk.why);
      var pr = document.getElementById('ap-na-path-row'); if (pr) pr.style.display = block.file ? '' : 'none';
      setText('ap-na-path', block.file || '');
      setText('ap-na-reco-text', (I18N.reasons || {})[block.code] ? ((I18N.reasons || {})[block.code].recommendation || '') : 'HEAD에게 다시 요청하거나 상세 정보를 확인하세요.');
      var rb = document.getElementById('approval-retry-btn'); if (rb) rb.setAttribute('data-prompt', retryPromptJs(block.code, block.file));
    }
  }
  // "내가 할 일" - a single glance, a single next action, scoped to the CURRENT run only. All six possible
  // buttons are static markup (server-rendered) toggled purely via style.display - no DOM node creation,
  // same pattern as every other action row on this page.
  var TODO_BUTTONS = { approvable: ['todo-inspect-btn', 'todo-approve-btn', 'todo-reject-btn'], pass: ['todo-view-result-btn'], nonapprovable: ['todo-retry-btn', 'todo-details-btn'] };
  function renderTodo(run, ap, res) {
    var box = document.getElementById('run-todo-box'); if (!box) return;
    ap = ap || {}; res = res || {};
    var human = run && run.human_required === 'YES';
    var apReq = !!ap.required;
    var apShow = !!(ap.required || ap.inflight || ap.rejected || (run && run.state === 'HUMAN_REQUIRED' && ap.reason_raw && ap.reason_raw.indexOf('APPROVAL_REQUIRED') === 0));
    var apNonapprovable = apShow && !apReq && !ap.rejected && !(ap.inflight || ap.approved);
    var blocked = (res.control_plane || []).length > 0 || !res.worktree_exists;
    var resReady = res.is_pass && ((res.result || {}).state || 'READY') === 'READY' && !blocked;
    var kind = 'busy', text = '', show = true;
    if (resReady) {
      kind = 'pass'; text = '결과물을 확인하고 적용 여부를 결정하세요.';
    } else if (human && apReq) {
      kind = 'approvable';
      var it = (ap.items && ap.items[0]) || {};
      text = it.file ? (it.file + ' 변경을 확인해주세요.') : '작업 범위를 확인해주세요.';
    } else if (human && apNonapprovable) {
      kind = 'nonapprovable'; text = '작업 계획을 수정해야 합니다.';
    } else if (human) {
      kind = 'resolved'; text = ap.rejected ? '거절되어 이 작업은 실행되지 않습니다.' : '승인되어 전체 안전 검사를 거쳐 작업을 계속합니다.';
    } else if (!run || run.state === 'NONE') {
      kind = 'idle'; text = ko(I18N.ui, 'head_ready');
    } else if (run.state === 'FAIL' || run.state === 'TIMEOUT') {
      kind = 'failed'; text = '작업이 실패했습니다. 새로운 요청을 HEAD 대화창에 보내주세요.';
    } else {
      show = false;
    }
    box.style.display = show ? '' : 'none';
    box.className = 'todobox ' + kind;
    setText('run-todo-text', text);
    ['todo-inspect-btn', 'todo-approve-btn', 'todo-reject-btn', 'todo-view-result-btn', 'todo-retry-btn', 'todo-details-btn'].forEach(function (id) {
      var b = document.getElementById(id); if (b) b.style.display = (TODO_BUTTONS[kind] || []).indexOf(id) >= 0 ? '' : 'none';
    });
    if (kind === 'nonapprovable') {
      var bl = (ap.blocking && ap.blocking[0]) || {};
      var rb = document.getElementById('todo-retry-btn'); if (rb) rb.setAttribute('data-prompt', retryPromptJs(bl.code, bl.file));
    }
  }
  function approvalAction(kind) {
    if (approvalBusy) return;
    var runId = actionRunId(); if (!runId) return;
    approvalBusy = true; renderApproval(lastApproval); setText('approval-result', kind === 'approve' ? 'Approving…' : 'Rejecting…');
    var rid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now() + '-' + Math.random().toString(16).slice(2));
    fetch(kind === 'approve' ? '/run/approve' : '/run/reject', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
      body: new URLSearchParams({ run_id: runId, request_id: rid }).toString() })
    .then(function (r) { return r.json(); })
    .then(function (j) { setText('approval-result', (kind === 'approve' ? '승인: ' : '거절: ') + (j.status === 'accepted' ? '접수됨' : (j.status === 'ok' ? '완료' : (j.status || ''))) + (j.message ? ' \u2014 ' + j.message : '')); })
    .catch(function (e) { setText('approval-result', 'Request failed: ' + e); })
    .then(function () { approvalBusy = false; lastApprovalKey = ''; pollStatus().then(refreshSelectedApproval); pollMessages(); });
  }
  var lastApproval = null;
  function inspectRun() {
    var runId = actionRunId(); if (!runId) return;
    fetch('/run/inspect?run_id=' + encodeURIComponent(runId), { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      var pre = document.getElementById('inspect-body'); if (!pre) return;
      var o = j.order || {};
      var lines = [
        'Run ID: ' + j.run_id, 'State: ' + j.state, 'Project: ' + (j.project_path || ''), 'Request: ' + (j.goal || ''),
        '', 'HEAD ORDER: ' + (o.goal || ''), 'risk_level: ' + (o.risk_level || '') + '   requires_human_approval: ' + o.requires_human_approval,
        'allowed_paths: ' + JSON.stringify(o.allowed_paths || []), 'protected_paths: ' + JSON.stringify(o.protected_paths || []),
        'allowed_deletions: ' + JSON.stringify(o.allowed_deletions || []), 'context: ' + (o.context || ''),
        '', 'Approval stage: ' + ((j.approval || {}).stage || 'n/a'), 'Approval reason: ' + (j.approval_reason || ''), 'Related files: ' + JSON.stringify(j.related_files || []),
        'Blocking (non-approvable) violations: ' + JSON.stringify((j.approval || {}).blocking || []),
        'Decisions so far: ' + JSON.stringify((j.approval || {}).decisions || []),
        'Builder executed: ' + (j.builder_ran ? 'YES' : 'NO (nothing has been built)') + '  provider=' + ((j.builder || {}).provider || '-') + ' exit_code=' + ((j.builder || {}).exit_code === undefined ? '-' : (j.builder || {}).exit_code) + ' version=' + ((j.builder || {}).version || '-'),
        'Worktree: ' + (j.worktree_path || 'none'),
        'Changed files: ' + JSON.stringify(j.changed_files || []), 'Diff stat: ' + JSON.stringify(j.diff_stat || []),
        'Tests so far: baseline=' + JSON.stringify((j.tests || {}).baseline || null) + ' post=' + JSON.stringify((j.tests || {}).post || null),
        'Scope checks so far: ' + JSON.stringify(j.scope_checks || []),
        '', 'Stages executed: ' + (j.stages_executed || []).join(' → '), 'Stages upcoming after approval: ' + (j.stages_upcoming || []).join(' → '),
        'Transitions:'].concat((j.transitions || []).map(function (t) { return '  ' + t.at + '  ' + t.from + ' → ' + t.to + '  ' + (t.details || ''); }))
        .concat(['Audit:']).concat((j.audit || []).map(function (a) { return '  ' + a.at + '  ' + a.agent + ' ' + a.action + '  ' + (a.payload || ''); }));
      (j.approval_items || []).forEach(function (it) { lines.splice(lines.indexOf('Approval reason: ' + (j.approval_reason || '')) + 1, 0, '  - ' + it.title + (it.file ? ' (' + it.file + ')' : '') + ': ' + it.why); });
      Object.keys(j.file_diffs || {}).forEach(function (f) { lines.push('', '--- diff of ' + f + ' ---', j.file_diffs[f]); });
      pre.textContent = lines.join('\n');
      var d = document.getElementById('run-details-panel'); if (d) d.open = true;
      var ip = document.getElementById('inspect-panel'); if (ip) ip.open = true;
    }).catch(function () {});
  }
  rbind('todo-inspect-btn', inspectRun);
  rbind('todo-approve-btn', function () { approvalAction('approve'); });
  rbind('todo-reject-btn', function () { approvalAction('reject'); });
  rbind('todo-retry-btn', function () { var b = document.getElementById('todo-retry-btn'); var t = b && b.getAttribute('data-prompt'); if (t) { input.value = t; input.focus(); } });
  rbind('todo-details-btn', function () { var d = document.getElementById('approval-raw-details'); if (d) d.open = true; var c = document.getElementById('approval-card'); if (c) c.scrollIntoView({ block: 'nearest' }); });
  rbind('approval-retry-btn', function () { var b = document.getElementById('approval-retry-btn'); var t = b && b.getAttribute('data-prompt'); if (t) { input.value = t; input.focus(); } });
  rbind('approval-details-btn', function () { var d = document.getElementById('approval-raw-details'); if (d) d.open = true; });
  ['approve-btn', 'chat-approve-btn', 'alert-approve-btn'].forEach(function (id) { var b = document.getElementById(id); if (b) b.addEventListener('click', function () { approvalAction('approve'); }); });
  ['reject-btn', 'chat-reject-btn', 'alert-reject-btn'].forEach(function (id) { var b = document.getElementById(id); if (b) b.addEventListener('click', function () { approvalAction('reject'); }); });
  ['inspect-btn', 'chat-inspect-btn'].forEach(function (id) { var b = document.getElementById(id); if (b) b.addEventListener('click', inspectRun); });
  function renderActivity(act) {
    if (!act) return;
    var core = document.getElementById('director-core');
    if (core) { core.classList.toggle('active', !!act.director); var cs = document.getElementById('core-stage'); if (cs) { cs.setAttribute('data-raw', act.director ? ('ORCHESTRATING \u00b7 ' + (act.stage || '')) : 'IDLE'); } setText('core-stage', act.director ? ('지휘 중 \u00b7 ' + ko(I18N.stage, act.stage === 'BUILDER' ? 'BUILD' : (act.stage === 'REVIEWER' ? 'REVIEW' : (act.stage || '')))) : ko(I18N.state, 'IDLE')); }
    Object.keys(act.nodes || {}).forEach(function (dom) {
      var n = act.nodes[dom]; var el = document.getElementById('node-' + dom); if (!el) return;
      el.classList.toggle('active', !!n.active);
      el.classList.toggle('backup', !!n.backup);
      var ind = document.getElementById('node-' + dom + '-ind'); if (ind) { ind.textContent = n.active ? '작업 중' : (n.backup ? '예비' : ''); ind.setAttribute('data-raw', n.active ? 'ACTIVE' : (n.backup ? 'BACKUP' : '')); }
      var tap = document.getElementById('tap-' + dom); if (tap) tap.classList.toggle('active', !!n.active);
    });
  }
  function fmtElapsed(created, updated, state) {
    if (!created) return '';
    var t0 = Date.parse(created + 'Z'); if (isNaN(t0)) t0 = Date.parse(created);
    var t1 = TERMINAL[state] ? (Date.parse(updated + 'Z') || Date.parse(updated)) : Date.now();
    var sec = Math.max(0, Math.round((t1 - t0) / 1000));
    return sec < 60 ? sec + 's' : Math.floor(sec / 60) + 'm' + (sec % 60) + 's';
  }
  function tickElapsed() {
    var e = fmtElapsed(runInfo.created_at, runInfo.updated_at, runInfo.state);
    setText('run-elapsed', e);
    var label = ko(I18N.state, runInfo.state || 'NONE') + (e ? ' \u00b7 ' + e : '');
    setText('run-label', label); setText('hdr-run-label', label);
    var rl0 = document.getElementById('run-label'); if (rl0) rl0.setAttribute('data-raw', runInfo.state || 'NONE');
    var sl = document.getElementById('stage-sentence'); if (sl && runInfo.stage_sentence !== undefined) { var base = runInfo.stage_sentence || ''; var act = {PLANNING:1, ORDER_READY:1, BUILDING:1, VERIFYING:1, REVIEWING:1, DECIDING:1}[runInfo.state]; setText('stage-sentence', base + (act && e ? ' \u00b7 ' + e : '')); }
    var rl = document.getElementById('run-label'); if (rl) rl.className = 'status-' + runInfo.state;
    var hl = document.getElementById('hdr-run-label'); if (hl) hl.className = 'status-' + runInfo.state;
  }
  function pollMessages() {
    return fetch('/chat/messages?after_id=' + lastId, { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      (j.messages || []).forEach(function (m) { if (m.id > lastId) { appendMessage(m); lastId = m.id; } });
      serverBusy = !!j.head_busy; updateSendUi();
      scheduleMessages((j.head_busy || lastStatusMode === 'active') ? 3000 : 10000);
    }).catch(function () { scheduleMessages(10000); });
  }
  function replaceList(id, items, formatter) {
    var el = document.getElementById(id); if (!el) return;
    while (el.firstChild) el.removeChild(el.firstChild);
    (items || []).forEach(function (item) {
      var li = document.createElement('li'); li.textContent = formatter(item); el.appendChild(li);
    });
    if (!(items || []).length) { var li = document.createElement('li'); li.className = 'research-empty'; li.textContent = '아직 기록된 내용이 없습니다.'; el.appendChild(li); }
  }
  function renderResearch(r) {
    r = r || {};
    setText('research-session-id', r.session_id || '없음');
    setText('research-moonshot', r.moonshot || 'TRIAD 연구 세션이 없습니다.');
    setText('research-cycle', (r.cycle_number || 0) + ' / ' + (r.cycle_budget || 0));
    setText('research-stage', r.current_stage_ko || r.current_stage || '연구 없음');
    setText('research-status', r.status || 'NONE');
    replaceList('research-proposals', r.proposals, function (p) {
      return (p.domain || 'AI') + ' · ' + (p.hypothesis || '가설 없음') + (p.confidence === null || p.confidence === undefined ? '' : ' · 신뢰도 ' + p.confidence);
    });
    replaceList('research-critiques', r.cross_critiques, function (c) {
      return (c.domain || 'AI') + ' · ' + (c.weakest_assumption || c.logical_flaw || c.self_critique || '비평 기록');
    });
    var syn = r.synthesis || {};
    setText('research-synthesis', syn.synthesized_hypothesis || syn.title || '아직 합성된 가설이 없습니다.');
    var exp = r.experiment || {};
    setText('research-experiment', exp.goal || exp.status || '아직 계획된 실험이 없습니다.');
    var ev = r.evidence || {};
    var evParts = [];
    if (ev.baseline_state) evParts.push('기준선: ' + ev.baseline_state);
    if (exp.normal_final_state) evParts.push('실험 검증: ' + exp.normal_final_state);
    if (exp.normal_run_id) evParts.push('Run: ' + exp.normal_run_id);
    setText('research-evidence', evParts.join(' · ') || '아직 검증 증거가 없습니다.');
    replaceList('research-rejected', r.rejected_ideas, function (x) { return String(x); });
    setText('research-learned', r.what_we_learned || '아직 정리된 학습 내용이 없습니다.');
    setText('research-next', r.next_step || '다음 단계가 정해지지 않았습니다.');
    var raw = document.getElementById('research-raw'); if (raw) raw.textContent = JSON.stringify(r, null, 2);
    var badge = document.getElementById('triad-mode-badge');
    if (badge) { badge.textContent = r.active ? '진행 중' : (r.status === 'MILESTONE_COMPLETE' ? '완료' : '읽기 전용'); badge.className = 'badge ' + (r.active ? 'b-blue' : (r.status === 'MILESTONE_COMPLETE' ? 'b-green' : 'b-muted')); }
  }
  function setDashboardView(mode) {
    var triad = mode === 'TRIAD';
    var core = document.getElementById('corestrip'), main = document.getElementById('main'), research = document.getElementById('triad-research-center');
    if (core) core.style.display = triad ? 'none' : 'grid';
    if (main) main.style.display = triad ? 'none' : 'grid';
    if (research) research.style.display = triad ? 'grid' : 'none';
    var nb = document.getElementById('normal-mode-btn'), tb = document.getElementById('triad-mode-btn');
    if (nb) nb.classList.toggle('on', !triad); if (tb) tb.classList.toggle('on', triad);
    try { window.localStorage.setItem('director-dashboard-mode', mode); } catch (e) {}
  }
  function pollStatus() {
    return fetch('/status', { cache: 'no-store' }).then(function (r) { return r.json(); }).then(function (j) {
      setStatusCell('system-mode', j.system_mode);
      setText('system-mode-hint', j.system_mode_sentence || j.system_mode_hint || '');
      var smh = document.getElementById('system-mode-hint'); if (smh) smh.setAttribute('data-raw', j.system_mode_hint || '');
      setStatusCell('core-mode', j.system_mode);
      setText('todo-line', j.todo || ''); setText('next-step', j.next_step ? (ko(I18N.ui, 'next_step') + ': ' + j.next_step) : '');
      var rt = document.getElementById('recovery-trial');
      if (rt) { var t = j.recovery_trial ? 'Recovery trial: ' + j.recovery_trial : ''; if (rt.textContent !== t) rt.textContent = t; rt.style.display = t ? '' : 'none'; }
      setText('recovery-short', j.recovery_short_ko || j.recovery_short || ''); var rsh = document.getElementById('recovery-short'); if (rsh) rsh.setAttribute('data-raw', j.recovery_short || '');
      var rr = document.getElementById('recovery-reasons');
      if (rr) { var want = (j.recovery_details || []).join('\n'); if (rr.textContent !== want) rr.textContent = want; }
      Object.keys(j.providers || {}).forEach(function (name) {
        var pr = j.providers[name];
        setStatusCell('prov-' + name + '-status', pr.status);
        var node = document.getElementById('node-' + name);
        if (node) { node.classList.toggle('healthy', pr.status === 'HEALTHY'); node.classList.toggle('recovering', pr.status === 'RECOVERING'); node.classList.toggle('down', pr.status !== 'HEALTHY' && pr.status !== 'RECOVERING'); }
        var g = document.getElementById('prov-' + name + '-glyph'); if (g) { var gl = pr.status === 'HEALTHY' ? '\u25c9' : (pr.status === 'RECOVERING' ? '\u25cc' : '\u25cb'); if (g.textContent !== gl) g.textContent = gl; g.className = 'glyph status-' + pr.status; }
        var roleParts = String(pr.role || '').replace(/\u00b7/g, ',').split(',').map(function (x) { return x.trim().toUpperCase(); }).filter(Boolean);
        setText('prov-' + name + '-role', roleParts.length ? (roleParts.join(' \u00b7 ') + ' (' + roleParts.map(function (r) { return ko(I18N.role, r); }).join(' \u00b7 ') + ')') : '');
        var roleEl = document.getElementById('prov-' + name + '-role'); if (roleEl) roleEl.setAttribute('data-raw', pr.role);
        setText('prov-' + name + '-version', pr.version);
        setText('prov-' + name + '-latency', (pr.latency ? pr.latency + 's' : '--')); setText('prov-' + name + '-usage', pr.usage);
        setText('prov-' + name + '-error', pr.error);
      });
      if (j.run) {
        runInfo = j.run;
        setText('run-id', j.run.run_id); setText('hdr-run', (j.run.run_id || 'NONE').slice(0, 18));
        runInfo.stage_sentence = j.stage_sentence;
        setStatusCell('run-state', j.run.state, I18N.state);
        setText('run-created', j.run.created_at || ''); setText('run-human', j.run.human_required === 'YES' ? '예' : '아니오');
        setText('run-details', j.run.details || '');
        renderPipeline(j.run.state, j.run.last_stage); tickElapsed();
        var hr = document.getElementById('hdr-human'); if (hr) { var ht = j.run.human_required === 'YES' ? ko(I18N.ui, 'human_required') : ko(I18N.ui, 'safe'); hr.setAttribute('data-raw', j.run.human_required === 'YES' ? 'HUMAN REQUIRED' : 'SAFE'); if (hr.textContent !== ht) hr.textContent = ht; hr.className = 'badge ' + (j.run.human_required === 'YES' ? 'b-red' : 'b-green'); }
        var ab = document.getElementById('alert-bar');
        if (ab) {
          var apx = j.approval || {}; var it = (apx.items && apx.items[0]) || {};
          var apxResolved = !!(apx.rejected || apx.inflight || apx.approved);
          var apxNonapprovable = j.run.human_required === 'YES' && !apx.required && !apxResolved;
          var apxBlock = (apx.blocking && apx.blocking[0]) || {};
          var alertText = j.run.human_required === 'YES'
            ? (apx.required ? ('\u26a0 ' + ko(I18N.ui, 'human_approval_required') + ' ' + ((it.code && (I18N.reasons || {})[it.code]) ? I18N.reasons[it.code].title : (it.title || '')) + (it.file ? ' (' + it.file + ')' : ''))
              : apxNonapprovable ? ('\u26a0 작업 계획 수정 필요 · ' + ((apxBlock.code && (I18N.reasons || {})[apxBlock.code]) ? I18N.reasons[apxBlock.code].why : ((apx.explain || {}).why || (apx.explain || {}).title || j.run.details || j.run.run_id)))
              : ('\u26a0 ' + ko(I18N.ui, 'human_required')))
            : ko(I18N.ui, 'safe_no_action');
          ab.setAttribute('data-raw', j.run.human_required === 'YES' ? ('HUMAN REQUIRED \u2014 ' + (j.run.details || '')) : 'SAFE / NO ACTION REQUIRED');
          setText('alert-text', alertText);
          ab.className = 'alert ' + (j.run.human_required === 'YES' ? 'a-red' : 'a-ok');
        }
      }
      renderActivity(j.activity);
      lastApproval = j.approval; if (!selectedRunId) renderApproval(j.approval); renderPending(j.pending);
      lastResult = j.result; if (!selectedResultId) renderResult(j.result); renderResults(j.results);
      renderTodo(j.run, j.approval, j.result);
      renderResearch(j.research);
      if (selectedResultId) refreshSelectedResult();
      var cvr = document.getElementById('chat-view-result-btn'); if (cvr) cvr.style.display = (j.result && j.result.is_pass && ((j.result.result || {}).state || 'READY') === 'READY') ? '' : 'none';
      if (selectedRunId) refreshSelectedApproval();
      var sw = document.getElementById('stale-warning');
      if (sw) { var n = (j.stale_runs || []).length; var st = n ? ('STALE RUN' + ' DETECTED (' + n + ') \u00b7 오래 멈춘 작업') : ''; if (sw.textContent !== st) sw.textContent = st; var sd = document.getElementById('stale-details'); if (sd) sd.style.display = n ? '' : 'none'; }
      var sl = document.getElementById('stale-list');
      if (sl) {
        var wantL = (j.stale_runs || []).map(function (r) { return r.run_id + ' \u00b7 ' + r.state + ' \u00b7 ' + (r.updated_at || ''); });
        var have = [].slice.call(sl.children).map(function (li) { return li.textContent; });
        if (wantL.join('|') !== have.join('|')) { while (sl.firstChild) sl.removeChild(sl.firstChild); wantL.forEach(function (t) { var li = document.createElement('li'); li.textContent = t; sl.appendChild(li); }); }
      }
      var pn = document.getElementById('project-name'); var pp = document.getElementById('project-path');
      if (pn) { var nm = j.project_name || ko(I18N.ui, 'no_project'); if (pn.textContent !== nm) { pn.textContent = nm; } pn.className = j.project_path ? 'proj-name' : 'proj-name proj-none'; }
      if (pp) setText('project-path', j.project_path || '');
      var pl = document.getElementById('project-label'); if (pl) setText('project-label', j.project_name || ko(I18N.ui, 'no_project'));
      window.__curProjectName = j.project_name || '';
      setText('session-label', j.session_id || '');
      serverBusy = !!j.head_busy; updateSendUi();
      var active = {PLANNING:1, ORDER_READY:1, BUILDING:1, VERIFYING:1, REVIEWING:1, DECIDING:1}[(j.run || {}).state] || j.head_busy;
      var need = (j.approval || {}).required || (j.human_required_count || 0) > 0;
      lastStatusMode = active ? 'active' : (need ? 'human' : 'idle');
      scheduleStatus(active ? 3000 : (need ? 5000 : 12000));
    }).catch(function () { scheduleStatus(12000); });
  }
  function submitChat() {
    if (sending) return;                                  // double-click / rapid click / Enter+click -> one POST
    var text = input.value.trim();
    if (!text) return;
    sending = true; updateSendUi();
    var rid = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now() + '-' + Math.random().toString(16).slice(2));
    fetch('/chat', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
      body: new URLSearchParams({ message: text, request_id: rid }).toString() })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.status === 'accepted' || j.status === 'duplicate') { input.value = ''; sendStatus.textContent = ''; }
      else if (j.status === 'busy') { sendStatus.textContent = 'BUSY: ' + (j.message || 'HEAD is working...'); }
      else { sendStatus.textContent = 'Error: ' + (j.error || j.status); }
    })
    .catch(function (e) { sendStatus.textContent = 'Send failed: ' + e; })
    .then(function () { sending = false; updateSendUi(); input.focus(); pollMessages(); pollStatus(); });
  }
  form.addEventListener('submit', function (ev) { ev.preventDefault(); submitChat(); });
  input.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); submitChat(); }   // Enter = send once, Shift+Enter = newline
  });
  [].slice.call(document.querySelectorAll('.msg-SYSTEM')).forEach(function (d) { d.addEventListener('click', function () { d.classList.toggle('expanded'); }); });
  var nbtn = document.getElementById('new-chat-btn');
  if (nbtn) nbtn.addEventListener('click', function () {
    if (sending || serverBusy) return;
    nbtn.disabled = true;
    fetch('/chat/new', { method: 'POST', headers: { 'Accept': 'application/json', 'X-Requested-With': 'fetch' } })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.status === 'ok') { while (chatBox.firstChild) chatBox.removeChild(chatBox.firstChild); lastId = 0; sendStatus.textContent = ''; }
      else { sendStatus.textContent = 'New chat refused: ' + (j.message || j.status); }
    })
    .catch(function (e) { sendStatus.textContent = 'New chat failed: ' + e; })
    .then(function () { nbtn.disabled = false; pollMessages(); pollStatus(); input.focus(); });
  });
  var pform = document.getElementById('project-form');
  var pchange = document.getElementById('project-change');
  if (pchange && pform) pchange.addEventListener('click', function () { pform.style.display = pform.style.display === 'none' ? '' : 'none'; if (pform.style.display !== 'none') document.getElementById('project-input').focus(); });
  if (pform) pform.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var pin = document.getElementById('project-input'); var pbtn = document.getElementById('project-btn');
    var path = pin.value.trim(); if (!path || pbtn.disabled) return;
    pbtn.disabled = true;
    fetch('/project', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
      body: new URLSearchParams({ path: path }).toString() })
    .then(function (r) { return r.json(); }).then(function (j) { setText('project-result', j.message || ''); if (j.status === 'ok') { pform.style.display = 'none'; pin.value = ''; } })
    .catch(function (e) { setText('project-result', 'Failed: ' + e); })
    .then(function () { pbtn.disabled = false; pollStatus(); pollMessages(); });
  });
  var rbtn = document.getElementById('refresh-btn');
  if (rbtn) rbtn.addEventListener('click', function () {
    rbtn.disabled = true;
    fetch('/providers/refresh', { method: 'POST', headers: { 'Accept': 'application/json', 'X-Requested-With': 'fetch' } })
      .catch(function () {}).then(function () { rbtn.disabled = false; pollStatus(); });
  });
  var inspect = document.getElementById('alert-inspect');
  if (inspect) inspect.addEventListener('click', inspectRun);
  var normalModeBtn = document.getElementById('normal-mode-btn');
  var triadModeBtn = document.getElementById('triad-mode-btn');
  if (normalModeBtn) normalModeBtn.addEventListener('click', function () { setDashboardView('NORMAL'); });
  if (triadModeBtn) triadModeBtn.addEventListener('click', function () { setDashboardView('TRIAD'); });
  var initialMode = 'NORMAL';
  try { initialMode = window.localStorage.getItem('director-dashboard-mode') || 'NORMAL'; } catch (e) {}
  setDashboardView(initialMode === 'TRIAD' ? 'TRIAD' : 'NORMAL');
  chatBox.scrollTop = chatBox.scrollHeight;               // initial load only
  pollStatus();
  pollMessages();
  setInterval(tickElapsed, 1000);
  // the page is never reloaded automatically: JSON polling + append-only chat only
})();
"""


DASHBOARD_CSS = r"""
    :root { --bg:#121317; --panel:#1b1d22; --panel2:#202329; --border:#2a2d35; --text:#d8dae0; --muted:#8a8f9b; --blue:#5aa9ff; --purple:#b987d8;
            --green:#4fc37a; --amber:#e2b34a; --red:#ef5b5b; }
    html, body { height: 100%; margin: 0; }
    body { background: var(--bg); color: var(--text); font: 13px/1.3 "Segoe UI", system-ui, sans-serif; overflow: hidden;
           display: grid; grid-template-rows: 48px auto minmax(0, 1fr) 38px; height: 100vh; }
    .mono { font-family: Consolas, "Cascadia Mono", monospace; }
    .status-HEALTHY, .status-NORMAL, .status-PASS, .status-RECOVERED { color: var(--green); font-weight: 600; }
    .status-DEGRADED, .status-RECOVERING, .status-QUOTA_LOW, .status-UNKNOWN { color: var(--amber); font-weight: 600; }
    .status-STOP, .status-FAIL, .status-HUMAN_REQUIRED, .status-TIMEOUT, .status-QUOTA_EXHAUSTED, .status-AUTH_FAILED, .status-UNAVAILABLE, .status-CONFIG_ERROR { color: var(--red); font-weight: 600; }
    .status-PLANNING, .status-ORDER_READY, .status-BUILDING, .status-VERIFYING, .status-REVIEWING, .status-DECIDING, .status-IDLE { color: var(--blue); font-weight: 600; }
    .status-NONE { color: var(--muted); }
    #system-mode, #core-mode { padding: 1px 8px; border-radius: 4px; border: 1px solid currentColor; font-size: 12px; }
    #system-mode-hint { color: var(--muted); font-size: 11px; margin-left: 6px; }
    .badge { white-space: nowrap; display: inline-block; padding: 1px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; border: 1px solid currentColor; line-height: 1.4; }
    .b-green { color: var(--green); } .b-red { color: var(--red); } .b-busy { color: var(--amber); } .b-blue { color: var(--blue); } .b-muted { color: var(--muted); }
    /* header */
    #hdr { display: flex; align-items: center; gap: 12px; padding: 0 12px; border-bottom: 1px solid var(--border); background: var(--panel); white-space: nowrap; overflow: hidden; }
    #hdr h1 { font-size: 15px; margin: 0; color: var(--blue); letter-spacing: 0.6px; }
    #hdr h1 small { color: var(--muted); font-weight: 400; font-size: 10px; letter-spacing: 1.2px; margin-left: 8px; }
    #hdr .sep { color: var(--border); }
    #hdr .lbl { color: var(--muted); font-size: 11px; text-transform: uppercase; margin-right: 4px; }
    #hdr button { margin-left: auto; flex-shrink: 0; }
    #hdr #mode-switch { display:flex; gap:4px; margin-left:auto; }
    #hdr #mode-switch button { margin-left:0; }
    #mode-switch button.on { color:var(--blue); border-color:var(--blue); background:rgba(90,169,255,0.10); }
    #refresh-btn { margin-left:0 !important; }
    #todo-line { display: inline-block; max-width: 300px; overflow: hidden; text-overflow: ellipsis; vertical-align: bottom; }
    #core-mode { white-space: nowrap; }
    #right h2 { flex-wrap: wrap; row-gap: 2px; }
    #right h2 > * { white-space: nowrap; }
    #run-result-state { white-space: normal; }
    .kv .k { white-space: nowrap; }
    button { padding: 3px 10px; background: #2d5f9a; color: #fff; border: 1px solid #3c74b5; border-radius: 4px; font-size: 12px; cursor: pointer; }
    button:disabled { background: #3a3d45; border-color: #4a4d55; color: #9a9da5; cursor: default; }
    button.ghost { background: transparent; border-color: var(--border); color: var(--muted); }
    /* AI core strip: DIRECTOR CORE + bus + 3 nodes */
    #corestrip { display: grid; grid-template-columns: 200px 1fr; gap: 0; padding: 8px 12px 6px 12px; border-bottom: 1px solid var(--border); align-items: stretch; }
    #director-core { background: var(--panel2); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; justify-content: center; gap: 3px; position: relative; }
    #director-core .ctitle { font-size: 11px; letter-spacing: 1.4px; color: var(--blue); font-weight: 700; }
    #director-core .csub { font-size: 10px; color: var(--muted); letter-spacing: 0.5px; }
    #director-core .cstage { font-size: 11px; color: var(--muted); font-weight: 600; }
    #director-core.active { border-color: var(--blue); box-shadow: 0 0 0 1px rgba(90,169,255,0.25), 0 0 14px rgba(90,169,255,0.15); }
    #director-core.active .cstage { color: var(--blue); }
    #director-core::after { content: ""; position: absolute; right: -1px; top: 50%; width: 1px; height: 1px; }
    #bus { position: relative; padding-left: 26px; }
    #bus .busline { position: absolute; left: 0; top: 50%; width: 26px; height: 2px; background: var(--border); }
    #bus .busbar { position: absolute; left: 26px; right: 12px; top: 7px; height: 2px; background: var(--border); }
    #nodes { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; padding-top: 14px; height: 100%; box-sizing: border-box; }
    .node { position: relative; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; min-height: 0; line-height: 1.25; }
    .node .tap { position: absolute; left: 50%; top: -14px; width: 2px; height: 14px; background: var(--border); }
    .node .tap.active { background: var(--blue); }
    .node .tap.active::after { content: ""; position: absolute; left: -2px; width: 6px; height: 6px; border-radius: 50%; background: var(--blue); animation: signal 1.1s linear infinite; }
    @keyframes signal { from { top: -3px; opacity: 0.2; } to { top: 11px; opacity: 1; } }
    .node .ntitle { display: flex; align-items: baseline; gap: 8px; margin-bottom: 3px; }
    .node .nname { font-size: 13px; font-weight: 700; letter-spacing: 0.8px; color: var(--text); }
    .node h3 { margin: 0; font-size: 10px; color: var(--muted); letter-spacing: 0.5px; font-weight: 400; }
    .node .ind { margin-left: auto; font-size: 9px; font-weight: 700; letter-spacing: 1px; color: var(--blue); }
    .node.backup .ind { color: var(--amber); }
    .node .ntitle details { font-size: 11px; color: var(--muted); }
    .node .ntitle details[open] { position: relative; }
    .node .ntitle details .dbox { position: absolute; right: 0; top: 18px; z-index: 5; background: #24262c; border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px; min-width: 320px; max-width: 520px; white-space: normal; word-break: break-word; color: var(--text); font-size: 11px; line-height: 1.35; }
    .node table { border-collapse: collapse; font-size: 12px; }
    .node th { text-align: left; color: var(--muted); font-weight: 400; padding: 0 10px 0 0; width: 52px; }
    .node td { padding: 0; white-space: nowrap; }
    .glyph { margin-right: 4px; }
    .node.active { border-color: var(--blue); box-shadow: 0 0 0 1px rgba(90,169,255,0.25), 0 0 12px rgba(90,169,255,0.18); animation: pulse 1.6s ease-in-out infinite; }
    .node.active .nname { color: var(--blue); }
    .node.chat-active { border-color: var(--purple); box-shadow: 0 0 10px rgba(185,135,216,0.2); }
    .node.recovering { border-color: rgba(226,179,74,0.45); }
    .node.down { border-color: rgba(239,91,91,0.5); }
    @keyframes pulse { 0%, 100% { box-shadow: 0 0 0 1px rgba(90,169,255,0.25), 0 0 8px rgba(90,169,255,0.12); } 50% { box-shadow: 0 0 0 1px rgba(90,169,255,0.45), 0 0 16px rgba(90,169,255,0.28); } }
    /* main */
    #main { display: grid; grid-template-columns: 250px minmax(600px, 1fr) 300px; gap: 10px; padding: 8px 12px; min-height: 0; }
    #triad-research-center { display:none; grid-row:2 / 4; grid-template-columns:minmax(0,1.15fr) minmax(0,1fr) minmax(0,1fr); grid-template-rows:auto minmax(0,1fr); gap:10px; padding:8px 12px; min-height:0; overflow:auto; }
    .research-hero { grid-column:1 / 4; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px 18px; align-items:start; }
    .research-hero h2 { color:var(--purple); margin-bottom:2px; }
    .research-meta { display:flex; gap:12px; flex-wrap:wrap; color:var(--muted); font-size:11px; }
    .research-moonshot { font-size:15px; line-height:1.45; color:var(--text); margin-top:4px; white-space:pre-wrap; }
    .research-column { min-height:0; overflow:auto; }
    .research-card { border:1px solid var(--border); border-radius:6px; background:var(--panel2); padding:8px 10px; margin-bottom:8px; }
    .research-card h3 { color:var(--muted); font-size:11px; letter-spacing:.5px; text-transform:uppercase; margin:0 0 5px; }
    .research-card p { margin:0; white-space:pre-wrap; word-break:break-word; }
    .research-card ul { margin:0; padding-left:18px; }
    .research-card li { margin:3px 0; line-height:1.35; }
    .research-empty { color:var(--muted); }
    #research-next { color:var(--green); }
    #research-raw { max-height:280px; overflow:auto; white-space:pre; font:10px/1.35 Consolas,monospace; color:var(--muted); }
    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; min-height: 0; display: flex; flex-direction: column; overflow: hidden; }
    .panel h2 { font-size: 12px; margin: 0 0 6px 0; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; display: flex; align-items: center; gap: 8px; }
    .proj-name { font-size: 15px; font-weight: 600; color: var(--text); }
    .proj-none { color: var(--red); }
    .proj-path { font-size: 11px; color: var(--muted); word-break: break-all; }
    #project-form input { width: 100%; box-sizing: border-box; margin: 6px 0; }
    input[type=text], textarea { padding: 5px 7px; background: #111214; color: var(--text); border: 1px solid var(--border); border-radius: 4px; font-family: inherit; font-size: 13px; }
    .kv { font-size: 12px; display: grid; grid-template-columns: 62px 1fr; gap: 2px 6px; }
    .kv .k { color: var(--muted); }
    .kv .v { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    details summary { cursor: pointer; color: var(--muted); font-size: 12px; }
    #recovery-short { font-size: 12px; color: var(--amber); }
    #recovery-details div, #recovery-reasons { font-size: 11px; color: var(--muted); white-space: pre-wrap; word-break: break-word; margin-top: 4px; }
    #stale-warning { color: var(--red); font-weight: 600; font-size: 12px; }
    #stale-list { margin: 4px 0 0 0; padding-left: 16px; font-size: 11px; max-height: 120px; overflow-y: auto; color: var(--muted); }
    /* chat */
    #chat-panel { padding: 0; }
    #chat-head { display: flex; align-items: center; gap: 8px; padding: 6px 10px; border-bottom: 1px solid var(--border); }
    #chat-head h2 { margin: 0; }
    #chat-head .session-label { color: var(--muted); font-size: 10px; margin-left: auto; }
    #chat-box { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 8px 12px; font-size: 13.5px; }
    .msg { margin-bottom: 10px; }
    .msg-USER, .msg-HEAD { padding: 6px 10px; border-left: 3px solid var(--blue); background: rgba(90,169,255,0.05); border-radius: 0 4px 4px 0; }
    .msg-HEAD { border-left-color: var(--purple); background: rgba(185,135,216,0.06); }
    .msg strong { font-size: 10px; letter-spacing: 0.6px; display: block; color: var(--muted); margin-bottom: 2px; }
    .msg-USER strong { color: var(--blue); } .msg-HEAD strong { color: var(--purple); }
    .msg pre { white-space: pre-wrap; margin: 0; font-family: inherit; font-size: 13.5px; line-height: 1.4; }
    .msg-SYSTEM { display: flex; align-items: center; gap: 6px; margin: 2px 0 6px 0; font-size: 11px; color: var(--muted); cursor: pointer; }
    .msg-SYSTEM .ev { display: none; flex: 0 0 auto; font-size: 9px; font-weight: 700; letter-spacing: 0.6px; padding: 0 5px; border-radius: 3px; border: 1px solid currentColor; white-space: nowrap; }
    .msg-SYSTEM.expanded .ev { display: inline-block; }
    .ev-ok { color: var(--green); } .ev-bad { color: var(--red); } .ev-info { color: var(--amber); }
    .msg-SYSTEM .evt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .msg-SYSTEM.expanded { align-items: flex-start; }
    .msg-SYSTEM.expanded .evt { white-space: pre-wrap; word-break: break-word; color: var(--text); }
    #chat-form { display: flex; gap: 8px; align-items: flex-end; padding: 8px 10px; border-top: 1px solid var(--border); }
    #chat-input { flex: 1; resize: none; height: 40px; }
    #send-status { color: var(--amber); font-size: 12px; min-height: 14px; }
    /* run */
    .pipeline { display: flex; gap: 4px; margin: 4px 0 8px 0; align-items: stretch; }
    .stage { flex: 1; text-align: center; border: 1px solid var(--border); border-radius: 4px; padding: 3px 0; font-size: 10.5px; color: var(--muted); position: relative; }
    .stage .mark { display: block; font-size: 14px; line-height: 16px; }
    .stage.done { color: var(--green); border-color: var(--green); }
    .stage.active { color: var(--blue); border-color: var(--blue); background: rgba(90,169,255,0.10); font-weight: 700; animation: pulse 1.6s ease-in-out infinite; }
    .stage.failed { color: var(--red); border-color: var(--red); background: rgba(239,91,91,0.08); }
    .stage.warn { color: var(--amber); border-color: var(--amber); background: rgba(226,179,74,0.10); }
    #run-label { font-size: 13px; }
    #pipeline-badge { margin-left: auto; }
    #run-details-panel div { font-size: 11px; color: var(--muted); word-break: break-all; }
    /* pending actions */
    .pendbox { margin-top: 12px; }
    #pending-count { font-size: 12px; font-weight: 700; letter-spacing: 0.5px; color: var(--muted); }
    .pendbox.has #pending-count { color: var(--amber); }
    #pending-list { list-style: none; margin: 4px 0 0 0; padding: 0; max-height: 150px; overflow-y: auto; }
    li.pend { border: 1px solid var(--border); border-left: 3px solid var(--amber); border-radius: 4px; padding: 4px 6px; margin-bottom: 4px; cursor: pointer; font-size: 11px; }
    li.pend.sel { background: rgba(226,179,74,0.10); border-color: var(--amber); }
    li.pend .pname { font-weight: 700; color: var(--text); }
    li.pend .ptitle2 { color: var(--amber); }
    li.pend .prun { color: var(--muted); font-size: 10px; }
    .rawtag { display: none; }
    /* RESULTS: latest 3 shown flat; the rest live in a height-capped, project-grouped "전체 보기" panel
       so the left column never grows past its allotted height */
    #results-more-row { margin-top: 4px; }
    #results-more-row button { width: 100%; font-size: 10px; }
    #results-grouped { max-height: 200px; overflow-y: auto; margin-top: 4px; border-top: 1px dashed var(--border); padding-top: 4px; }
    .resgroup { margin-bottom: 4px; }
    .resgroup summary { cursor: pointer; font-size: 11px; color: var(--muted); padding: 2px 0; list-style: none; }
    .resgroup summary::-webkit-details-marker { display: none; }
    .resgroup ul { list-style: none; margin: 2px 0 0 0; padding: 0 0 0 6px; }
    /* "내가 할 일" - one glance, one next action, scoped to the CURRENT run only (never a past
       run's result) so it stays true 3-second-glance material */
    .todobox { margin: 6px 0 8px 0; padding: 8px 10px; border-radius: 6px; background: var(--panel2); border: 1px solid var(--border); }
    .todobox .tt { font-size: 10px; text-transform: uppercase; color: var(--muted); letter-spacing: 0.5px; margin-bottom: 2px; }
    #run-todo-text { font-size: 13px; font-weight: 700; color: var(--text); }
    .todobox.approvable, .todobox.nonapprovable { border-color: var(--amber); background: rgba(226,179,74,0.06); }
    .todobox.pass { border-color: var(--blue); background: rgba(90,169,255,0.06); }
    .todobox.idle, .todobox.resolved { opacity: 0.85; }
    .todobox.failed { border-color: var(--red); }
    .todobox .abtns { margin-top: 6px; }
    /* Result Center: past PASS results never render inside "Current Run" - they only ever appear here, in a
       dedicated modal opened explicitly from the CURRENT run's own CTA or from the RESULTS list */
    .modal-overlay { position: fixed; inset: 0; background: rgba(8,9,11,0.65); z-index: 80; align-items: center; justify-content: center; }
    .modal-overlay.open { display: flex; }
    #result-card.modalbox { max-width: 640px; width: 92%; max-height: 85vh; overflow-y: auto; box-shadow: 0 10px 44px rgba(0,0,0,0.55); }
    #approval-nonapprovable #ap-na-desc { font-weight: 700; color: var(--text); margin-bottom: 2px; }
    #approval-nonapprovable #ap-na-blocked { color: var(--red); font-size: 11px; margin: 4px 0; }
    #approval-nonapprovable #ap-na-reco { margin-top: 2px; }
    #approval-raw-details { margin-top: 6px; font-size: 10px; color: var(--muted); }
    #approval-raw-details summary { cursor: pointer; }
    .stage small { display: block; font-size: 9px; color: var(--muted); font-weight: 400; }
    .stage.active small, .stage.done small { color: inherit; }
    .stage-sentence { font-size: 12px; color: var(--text); margin: 2px 0 0 0; }
    .next-step { font-size: 11px; color: var(--muted); margin-bottom: 4px; }
    .node .vendor { font-size: 10px; color: var(--muted); }
    .msg-SYSTEM .evraw { display: none; }
    .msg-SYSTEM.expanded .evraw { display: block; color: var(--muted); font-size: 10px; white-space: pre-wrap; word-break: break-word; }
    /* result center */
    .resultcard { border: 1px solid var(--blue); border-radius: 6px; padding: 8px 10px; margin: 6px 0; background: rgba(90,169,255,0.06); font-size: 12px; }
    .resultcard.r-applied { border-color: var(--green); background: rgba(79,195,122,0.07); }
    .resultcard.r-discarded { border-color: var(--border); background: transparent; }
    .resultcard.r-blocked { border-color: var(--amber); background: rgba(226,179,74,0.07); }
    .resultcard .rh { font-weight: 700; letter-spacing: 0.6px; color: var(--blue); }
    .resultcard.r-applied .rh { color: var(--green); } .resultcard.r-blocked .rh { color: var(--amber); } .resultcard.r-discarded .rh { color: var(--muted); }
    .resultcard .rs { color: var(--muted); margin: 2px 0 4px 0; white-space: pre-wrap; }
    .resultcard .abtns { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
    .resultcard .abtns button { font-size: 11px; padding: 3px 8px; }
    button.apply { background: #2f7d4a; border-color: #3f9a5e; }
    button.discard { background: #5a3a3a; border-color: #7a4a4a; }
    #result-status { font-size: 11px; color: var(--muted); margin-top: 4px; white-space: pre-wrap; }
    #result-confirm { border: 1px solid var(--amber); border-radius: 6px; padding: 8px 10px; margin-top: 6px; background: rgba(226,179,74,0.08); }
    #result-confirm .ct { font-weight: 700; color: var(--amber); }
    #result-confirm pre { white-space: pre-wrap; font-family: inherit; font-size: 11px; margin: 4px 0; }
    #run-result-state { font-size: 11px; color: var(--blue); font-weight: 700; margin-left: 6px; }
    #result-detail { margin-top: 6px; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; display: flex; flex-direction: column; min-height: 0; max-height: 46vh; }
    #result-detail .rdtabs { display: flex; gap: 6px; margin-bottom: 4px; }
    #result-detail .rdtabs button.on { color: var(--blue); border-color: var(--blue); }
    #rd-head { font-size: 10px; color: var(--muted); white-space: pre-wrap; margin-bottom: 4px; }
    #rd-files { list-style: none; margin: 0 0 4px 0; padding: 0; max-height: 90px; overflow-y: auto; font-size: 11px; }
    #rd-files li { cursor: pointer; padding: 1px 4px; border-radius: 3px; font-family: Consolas, monospace; }
    #rd-files li.sel { background: rgba(90,169,255,0.15); }
    #rd-files li.t-A { color: var(--green); } #rd-files li.t-M { color: var(--amber); } #rd-files li.t-D { color: var(--red); }
    #rd-diff { flex: 1 1 auto; min-height: 0; overflow: auto; font-family: Consolas, monospace; font-size: 10.5px; white-space: pre; margin: 0; background: #111214; padding: 6px; border-radius: 4px; }
    #rd-evidence { font-size: 10px; color: var(--muted); white-space: pre-wrap; margin-top: 4px; max-height: 60px; overflow-y: auto; }
    li.pend.res { border-left-color: var(--blue); } li.pend .ptitle2.blue { color: var(--blue); }
    /* approval */
    .approval { border: 1px solid var(--amber); border-radius: 6px; padding: 8px 10px; margin: 6px 0; background: rgba(226,179,74,0.07); font-size: 12px; }
    .approval.ap-rejected { border-color: var(--red); background: rgba(239,91,91,0.06); }
    .approval.ap-done { border-color: var(--green); background: rgba(79,195,122,0.06); }
    .approval .ah { font-weight: 700; letter-spacing: 0.6px; color: var(--amber); font-size: 12px; }
    .approval.ap-rejected .ah { color: var(--red); } .approval.ap-done .ah { color: var(--green); }
    .approval .an { color: var(--text); margin: 2px 0 6px 0; }
    .approval .ak { color: var(--muted); }
    .approval .abtns { display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }
    .approval .abtns button { font-size: 11px; padding: 3px 8px; }
    button.approve { background: #2f7d4a; border-color: #3f9a5e; }
    button.reject { background: #8a2e2e; border-color: #b04040; }
    #approval-raw { font-size: 10px; color: var(--muted); word-break: break-all; }
    #approval-result { font-size: 11px; color: var(--muted); margin-top: 4px; }
    #chat-action-card { margin: 0 10px 6px 10px; }
    #inspect-body { white-space: pre-wrap; font-size: 11px; color: var(--text); max-height: 260px; overflow-y: auto; margin: 4px 0; }
    .alert .abtns { display: flex; gap: 6px; margin-left: 12px; }
    /* alert */
    .alert { display: flex; align-items: center; gap: 12px; padding: 0 12px; border-top: 1px solid var(--border); font-size: 12px; overflow: hidden; white-space: nowrap; }
    .alert #alert-text { overflow: hidden; text-overflow: ellipsis; }
    .a-ok { color: var(--green); background: var(--panel); }
    .a-red { color: #fff; background: #7a2727; }
    .a-red button { background: #a33; border-color: #c55; }
    @media (prefers-reduced-motion: reduce) { .node.active, .stage.active, .node .tap.active::after { animation: none !important; } }
    @media (max-width: 1439px) { #main { grid-template-columns: 230px minmax(500px, 1fr) 240px; } #corestrip { grid-template-columns: 170px 1fr; } }
    @media (max-width: 1023px) { body { display: block; overflow: auto; height: auto; } #main { display: block; } #triad-research-center { grid-template-columns:1fr; grid-template-rows:auto; } .research-hero { grid-column:1; } .panel { margin-bottom: 8px; max-height: 60vh; } #chat-box { max-height: 40vh; } #corestrip { grid-template-columns: 1fr; } #nodes { grid-template-columns: 1fr; } #bus { padding-left: 0; } }
"""


EVENT_LABELS = [
    (r"finished: PASS", "RUN PASS", "ev-ok"), (r"finished: HUMAN_REQUIRED", "HUMAN REQUIRED", "ev-bad"),
    (r"finished: (FAIL|TIMEOUT)", "RUN FAILED", "ev-bad"), (r"Starting background run", "RUN STARTED", "ev-info"),
    (r"RECOVERY TRIAL", "RECOVERY TRIAL", "ev-info"), (r"SYSTEM STOP", "SYSTEM STOP", "ev-bad"),
    (r"PROJECT_SELECTED", "PROJECT SELECTED", "ev-ok"), (r"PROJECT_SELECTION_REJECTED|PROJECT_SELECTION_REQUIRED", "PROJECT REJECTED", "ev-bad"),
    (r"NEW_CHAT", "NEW CHAT", "ev-ok"), (r"HUMAN_REQUIRED|APPROVAL_REQUIRED", "HUMAN REQUIRED", "ev-bad"),
    (r"BUSY", "BUSY", "ev-bad"), (r"HEAD Error|Error|failed", "ERROR", "ev-bad"),
]
NODE_NAMES = {"OPENAI": "CODEX", "ANTHROPIC": "CLAUDE", "GOOGLE": "ANTIGRAVITY"}


def _event_label(text: str):
    for pat, label, cls in EVENT_LABELS:
        if re.search(pat, text):
            return label, cls
    return "SYSTEM", "ev-info"


def _event_kind(text: str) -> str:
    return _event_label(text)[1]


def _elapsed_label(run: dict) -> str:
    try:
        t0 = datetime.fromisoformat(run.get("created_at") or "")
        t1 = datetime.fromisoformat(run.get("updated_at") or "") if run.get("state") in (TERMINAL_STATES | {"RECOVERED"}) else datetime.utcnow()
        sec = max(0, int((t1 - t0).total_seconds()))
        return f"{sec}s" if sec < 60 else f"{sec // 60}m{sec % 60}s"
    except Exception:
        return ""


def _stale_ids_html(stale_runs) -> str:
    return "".join(f"<li>{html_lib.escape(str(r['run_id']))} \u00b7 {html_lib.escape(str(r['state']))} \u00b7 {html_lib.escape(str(r['updated_at'] or ''))}</li>" for r in stale_runs)


def render_html():
    """Initial server-side render (compact 100vh command center). Afterwards the page only polls JSON and appends chat."""
    try:
        st = build_status_payload()
        cm = ChatManager()
        sid = st.get("session_id") or active_session_id()
        messages = cm.get_messages(sid)
        last_id = max([int(m.get('id') or 0) for m in messages] or [0])
        chat_html = ""
        for msg in messages:
            role = html_lib.escape(str(msg['role']))
            content = html_lib.escape(str(msg['content']))
            mid = int(msg.get('id') or 0)
            if role == "SYSTEM":
                label, cls = _event_label(str(msg['content']))
                ko_text, _ko_cls = I18N.event_ko(str(msg['content']))
                chat_html += (f"<div class='msg msg-SYSTEM' data-id='{mid}'><span class='ev {cls}'>{label}</span>"
                              f"<span class='evt' title=\"click to expand\" data-raw=\"{content}\">{html_lib.escape(ko_text)}</span>"
                              f"<span class='evraw'>{content}</span></div>")
            else:
                chat_html += f"<div class='msg msg-{role}' data-id='{mid}'><strong>{role}</strong><pre>{content}</pre></div>"

        system_mode = st["system_mode"]
        mode_hint = html_lib.escape(st.get("system_mode_hint") or "")
        trial_text = ("Recovery trial: " + st["recovery_trial"]) if st["recovery_trial"] else ""
        trial_style = "" if trial_text else " display:none;"
        run = st["run"]
        act = st.get("activity") or {"director": False, "stage": None, "nodes": {}}
        elapsed = _elapsed_label(run)
        run_state = html_lib.escape(str(run["state"]))
        run_label = run_state + (f" \u00b7 {elapsed}" if elapsed else "")
        stale_runs = st.get("stale_runs", [])
        stale_text = f"STALE RUN DETECTED ({len(stale_runs)})" if stale_runs else ""
        stale_style = "" if stale_runs else " display:none;"
        project_path = st["project_path"]
        project_name = html_lib.escape(st.get("project_name") or "") or "NO PROJECT SELECTED"
        proj_cls = "proj-name" if project_path else "proj-name proj-none"
        busy = st["head_busy"]
        busy_attr = " disabled" if busy else ""
        busy_text = "BUSY: HEAD is working on another request..." if busy else ""
        human = run["human_required"] == "YES"
        hdr_run = html_lib.escape(str(run["run_id"])[:18])
        alert_cls = "a-red" if human else "a-ok"
        alert_text = ("HUMAN REQUIRED \u2014 " + (run.get("details") or str(run["run_id"]))) if human else "SAFE / NO ACTION REQUIRED"
        chat_badge_cls, chat_badge = ("b-busy", "HEAD is working...") if busy else (("b-red", "HUMAN_REQUIRED") if human else ("b-green", "READY"))
        recovery_short = html_lib.escape(st.get("recovery_short") or "")
        recovery_reasons = html_lib.escape("\n".join(st.get("recovery_details") or []))
        core_cls = " active" if act.get("director") else ""
        mode_sentence = st.get("system_mode_sentence") or ""
        run_label_ko = I18N.state_ko(run_state) + (f" \u00b7 {elapsed}" if elapsed else "")
        stage_map = {"HEAD": "HEAD", "BUILDER": "BUILD", "REVIEWER": "REVIEW", "VERIFY": "VERIFY"}
        core_stage_ko = ("지휘 중 \u00b7 " + I18N.STAGE_TEXT.get(stage_map.get(act.get("stage") or "", ""), act.get("stage") or "")) if act.get("director") else I18N.state_ko("IDLE")
        chat_badge_ko = {"HEAD is working...": I18N.t("head_working"), "HUMAN_REQUIRED": I18N.t("human_required"), "READY": I18N.result_ko("READY")}.get(chat_badge, chat_badge)
        ap = st.get("approval") or {"required": False}
        ap_item = (ap.get("items") or [{}])[0] if ap.get("items") else {}
        ap_show = bool(ap.get("required") or ap.get("inflight") or ap.get("rejected") or (run["state"] == "HUMAN_REQUIRED" and str(run.get("details") or "").startswith("APPROVAL_REQUIRED")))
        ap_style = "" if ap_show else "display:none;"
        ap_btn_style = "" if ap.get("required") else "display:none;"
        ap_cls = "ap-need" if ap.get("required") else ("ap-rejected" if ap.get("rejected") else "ap-done")
        ap_headline = html_lib.escape(ap.get("headline") or "")
        ap_next = html_lib.escape(ap.get("next") or ("Rejected: this run will not execute." if ap.get("rejected") else ("Approved: resuming through the full safety pipeline." if ap.get("inflight") or ap.get("approved") else "")))
        ap_reason = html_lib.escape(ap_item.get("title") or "")
        ap_file = html_lib.escape(ap_item.get("file") or "")
        ap_why = html_lib.escape(ap_item.get("why") or "")
        ap_raw = html_lib.escape(ap.get("reason_raw") or "")
        ap_file = html_lib.escape(", ".join([i.get("file") for i in (ap.get("items") or []) if i.get("file")]) or (ap_item.get("file") or ""))
        pending = st.get("pending") or []
        results = st.get("results") or []

        def _res_row(p):
            return (f"<li class='pend res'><div class='pname'>{html_lib.escape(p['project_name'])}</div>"
                    f"<div class='ptitle2 blue'>{I18N.t('pass')} \u00b7 {I18N.t('changed_files')} {p.get('changed_count')}\uac1c \u00b7 {I18N.result_ko('READY')}</div>"
                    f"<div class='prun mono'>{html_lib.escape(p['run_id'][:26])}</div></li>")

        results_head = results[:3]
        results_rest_count = max(0, len(results) - 3)
        results_html = "".join(_res_row(p) for p in results_head)
        _cur_project_raw = st.get("project_name") or None
        _groups: dict = {}
        _order: list = []
        for p in results:
            _nm = p.get("project_name") or ""
            if _nm not in _groups:
                _groups[_nm] = []
                _order.append(_nm)
            _groups[_nm].append(p)
        _order.sort(key=lambda nm: (0 if nm == _cur_project_raw else 1, nm))
        results_grouped_html = "".join(
            f"<details class='resgroup'{' open' if nm == _cur_project_raw else ''}>"
            f"<summary>{html_lib.escape(nm) or '(project)'} ({len(_groups[nm])})</summary>"
            f"<ul>{''.join(_res_row(p) for p in _groups[nm])}</ul></details>" for nm in _order)
        res = st.get("result") or {"is_pass": False}
        r_state = ((res.get("result") or {}).get("state") or "READY") if res.get("is_pass") else ""
        r_blocked = bool(res.get("control_plane")) or (res.get("is_pass") and not res.get("worktree_exists"))
        r_style = "" if res.get("is_pass") else "display:none;"
        r_cls = "r-applied" if r_state == "APPLIED" else ("r-discarded" if r_state == "DISCARDED" else ("r-blocked" if (r_blocked or "ROLLED_BACK" in r_state or r_state == "APPLY_BLOCKED") else "r-ready"))
        r_headline = {"APPLIED": "RESULT APPLIED \u2713", "DISCARDED": "RESULT DISCARDED"}.get(r_state, "APPLY BLOCKED" if (r_blocked or "ROLLED_BACK" in r_state or r_state == "APPLY_BLOCKED") else "RESULT READY")
        r_sub = html_lib.escape({"APPLIED": "Project updated \u00b7 Tests passed", "DISCARDED": "Main project not modified \u00b7 evidence preserved"}.get(r_state, (res.get("result") or {}).get("detail") or ("Control-plane file in result" if res.get("control_plane") else "READY TO APPLY")))
        rt = res.get("tests") or {}
        cnt = res.get("counts") or {}
        r_files = f"Changed files: {res.get('changed_count', 0)}  (A {cnt.get('A', 0)} / M {cnt.get('M', 0)} / D {cnt.get('D', 0)})" if res.get("is_pass") else ""
        r_tests = (f"Tests: {'PASS' if rt.get('post_passed') else 'FAIL'}  \u00b7  Reviewer: {str(rt.get('reviewer') or '').upper()}  \u00b7  Safety: {rt.get('final_gate') or '?'}") if res.get("is_pass") else ""
        r_can = res.get("is_pass") and r_state not in ("APPLIED", "DISCARDED")
        r_act_attr = "" if r_can else " disabled"
        r_apply_attr = "" if (r_can and not r_blocked) else " disabled"
        r_state_label = ("RESULT: " + ("APPLY BLOCKED" if (r_state == "READY" and r_blocked) else r_state)) if res.get("is_pass") else ""
        r_chat_style = "" if (res.get("is_pass") and res.get('run_id') == run.get('run_id') and run.get('state') == 'PASS' and r_state == "READY") else "display:none;"
        _ap_first = (ap.get("items") or [{}])[0] if ap.get("items") else {}
        _explain = I18N.explain_reason(ap.get("reason_raw") or "")
        ap_code = html_lib.escape(_ap_first.get("code") or _explain["code"])
        ap_reason_ko = html_lib.escape(I18N.reason_ko(_ap_first.get("code", ""))[0] if _ap_first else _explain["title"])
        ap_why_ko = html_lib.escape(I18N.reason_ko(_ap_first.get("code", ""))[1] if _ap_first else _explain["why"])
        ap_headline_ko = html_lib.escape(I18N.t("human_approval_required") if ap.get("required") else ("사용자가 거절함" if ap.get("rejected") else ("승인됨 \u00b7 작업 재개 중" if (ap.get("inflight") or ap.get("approved")) else (I18N.t("not_approvable") if ap_show else ""))))
        ap_next_ko = html_lib.escape(I18N.t("human_required_sub") if ap.get("required") else ("거절됨: 이 작업은 실행되지 않습니다." if ap.get("rejected") else ("승인됨: 전체 안전 검사를 거쳐 작업을 계속합니다." if (ap.get("inflight") or ap.get("approved")) else (I18N.t("not_approvable_sub") if ap_show else ""))))
        ap_stage_ko = {"ENTRY": "작업 시작 전 (ENTRY)", "POST_BUILD": "코드 작성 후 검증 단계 (POST-BUILD / VERIFYING)"}.get(ap.get("stage") or "", html_lib.escape(ap.get("stage_label") or ""))
        ap_builder_ko = {"COMPLETED": "코드 작성 완료", "NOT RUN": "아직 실행 안 됨"}.get(ap.get("builder") or "", html_lib.escape(ap.get("builder") or ""))
        r_headline_ko = {"RESULT APPLIED \u2713": I18N.t("result_applied"), "RESULT DISCARDED": I18N.t("result_discarded"), "APPLY BLOCKED": I18N.t("apply_blocked"), "RESULT READY": I18N.t("result_ready")}.get(r_headline, r_headline)
        _rdetail = (res.get("result") or {}).get("detail") or ""
        _rcode = I18N.reason_code(_rdetail)
        r_sub_ko = html_lib.escape({"APPLIED": "프로젝트가 업데이트되었습니다 \u00b7 테스트 통과", "DISCARDED": "원본 프로젝트는 변경되지 않았습니다 \u00b7 검증 증거 보존"}.get(r_state,
                   (I18N.reason_ko(_rcode)[1] + f" (상세 코드: {_rcode})") if (r_state in ("APPLY_BLOCKED",) or "ROLLED_BACK" in r_state) and _rcode != "UNKNOWN" else
                   ("AI DEV DIRECTOR 보호 파일이 결과에 포함되어 적용할 수 없습니다" if res.get("control_plane") else ("프로젝트에 적용할 준비가 되었습니다" if r_state == "READY" and not r_blocked else _rdetail))))
        r_files_ko = (f"{I18N.t('changed_files')} {res.get('changed_count', 0)}개  (추가 {cnt.get('A', 0)} / 수정 {cnt.get('M', 0)} / 삭제 {cnt.get('D', 0)})") if res.get("is_pass") else ""
        r_tests_ko = (f"{I18N.t('tests')} {I18N.t('pass') if rt.get('post_passed') else I18N.t('fail')}  \u00b7  {I18N.t('review')} {I18N.t('pass') if str(rt.get('reviewer') or '').lower() == 'pass' else I18N.t('fail')}  \u00b7  {I18N.t('safety')} {I18N.t('pass') if rt.get('final_gate') == 'PASS' else (rt.get('final_gate') or '?')}") if res.get("is_pass") else ""
        r_state_label_ko = (I18N.t("result_state") + ": " + (I18N.t("apply_blocked") if (r_state == "READY" and r_blocked) else I18N.result_ko(r_state))) if res.get("is_pass") else ""
        _alert_explain = I18N.explain_reason(run.get("details") or "")
        ap_resolved = bool(ap.get("rejected") or ap.get("inflight") or ap.get("approved"))
        ap_nonapprovable = bool(ap_show and not ap.get("required") and not ap_resolved)
        ap_approvable_style = "" if ap.get("required") else "display:none;"
        ap_nonapprovable_style = "" if ap_nonapprovable else "display:none;"
        _ap_block = (ap.get("blocking") or [{}])[0] if ap.get("blocking") else {}
        _ap_block_code = _ap_block.get("code") or _alert_explain["code"]
        ap_block_file = html_lib.escape(_ap_block.get("file") or "")
        ap_path_style = "" if ap_block_file else "display:none;"
        ap_na_desc_ko = html_lib.escape(I18N.reason_ko(_ap_block_code)[0])
        ap_na_why_ko = html_lib.escape(I18N.reason_ko(_ap_block_code)[1])
        ap_na_reco_ko = html_lib.escape(I18N.reason_recommendation(_ap_block_code))
        ap_retry_prompt = html_lib.escape(I18N.retry_prompt(_ap_block_code, _ap_block.get("file") or ""))
        alert_text_ko = ("\u26a0 " + I18N.t("human_approval_required") + " " + (I18N.reason_ko(_ap_first.get("code", ""))[0] if _ap_first else "") + ((" (" + _ap_first.get("file") + ")") if _ap_first.get("file") else "")) if (human and ap.get("required")) else \
                        (("\u26a0 \uc791\uc5c5 \uacc4\ud68d \uc218\uc815 \ud544\uc694 \u00b7 " + ap_na_why_ko) if (human and ap_nonapprovable) else
                         (("\u26a0 " + I18N.t("human_required")) if human else I18N.t("safe_no_action")))

        # "\ub0b4\uac00 \ud560 \uc77c" - one glance, one next action, specific to the CURRENT run only
        if res.get("is_pass") and r_state == "READY" and not r_blocked:
            todo_kind, todo_text_ko = "pass", "\uacb0\uacfc\ubb3c\uc744 \ud655\uc778\ud558\uace0 \uc801\uc6a9 \uc5ec\ubd80\ub97c \uacb0\uc815\ud558\uc138\uc694."
        elif human and ap.get("required"):
            todo_kind = "approvable"
            _tf = _ap_first.get("file") if _ap_first else ""
            todo_text_ko = (f"{html_lib.escape(_tf)} \ubcc0\uacbd\uc744 \ud655\uc778\ud574\uc8fc\uc138\uc694." if _tf else "\uc791\uc5c5 \ubc94\uc704\ub97c \ud655\uc778\ud574\uc8fc\uc138\uc694.")
        elif human and ap_nonapprovable:
            todo_kind, todo_text_ko = "nonapprovable", "\uc791\uc5c5 \uacc4\ud68d\uc744 \uc218\uc815\ud574\uc57c \ud569\ub2c8\ub2e4."
        elif human:
            todo_kind = "resolved"
            todo_text_ko = "\uac70\uc808\ub418\uc5b4 \uc774 \uc791\uc5c5\uc740 \uc2e4\ud589\ub418\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4." if ap.get("rejected") else "\uc2b9\uc778\ub418\uc5b4 \uc804\uccb4 \uc548\uc804 \uac80\uc0ac\ub97c \uac70\uccd0 \uc791\uc5c5\uc744 \uacc4\uc18d\ud569\ub2c8\ub2e4."
        elif run_state in ("NONE",):
            todo_kind, todo_text_ko = "idle", I18N.t("head_ready")
        elif run_state in ("FAIL", "TIMEOUT"):
            todo_kind, todo_text_ko = "failed", "\uc791\uc5c5\uc774 \uc2e4\ud328\ud588\uc2b5\ub2c8\ub2e4. \uc0c8\ub85c\uc6b4 \uc694\uccad\uc744 HEAD \ub300\ud654\ucc3d\uc5d0 \ubcf4\ub0b4\uc8fc\uc138\uc694."
        else:
            todo_kind, todo_text_ko = "busy", ""
        todo_style = "display:none;" if todo_kind == "busy" else ""

        pending_html = "".join(
            f"<li class='pend'><div class='pname'>{html_lib.escape(p['project_name'])}{' &middot; RESUMING' if p.get('status') == 'RESUMING' else ''}</div>"
            f"<div class='ptitle2'>{html_lib.escape((p.get('file') + ' · ') if p.get('file') else '')}{html_lib.escape(p.get('title') or '')} · {'post-build' if p.get('stage') == 'POST_BUILD' else 'entry'}</div>"
            f"<div class='prun mono'>{html_lib.escape(p['run_id'][:26])}</div></li>" for p in pending)
        if human and ap.get("required"):
            alert_text = "HUMAN APPROVAL REQUIRED — " + (ap_item.get("title") or "") + ((" (" + ap_item.get("file") + ")") if ap_item.get("file") else "") + " — Approval required before continuing"
        core_stage = ("ORCHESTRATING \u00b7 " + (act.get("stage") or "")) if act.get("director") else "IDLE"

        research = st.get("research") or {}
        research_proposals_html = "".join(
            f"<li>{html_lib.escape(str(p.get('domain') or 'AI'))} · "
            f"{html_lib.escape(str(p.get('hypothesis') or '가설 없음'))}</li>"
            for p in (research.get("proposals") or [])
        ) or "<li class='research-empty'>아직 기록된 내용이 없습니다.</li>"
        research_critiques_html = "".join(
            f"<li>{html_lib.escape(str(c.get('domain') or 'AI'))} · "
            f"{html_lib.escape(str(c.get('weakest_assumption') or c.get('logical_flaw') or c.get('self_critique') or '비평 기록'))}</li>"
            for c in (research.get("cross_critiques") or [])
        ) or "<li class='research-empty'>아직 기록된 내용이 없습니다.</li>"
        research_rejected_html = "".join(
            f"<li>{html_lib.escape(str(x))}</li>" for x in (research.get("rejected_ideas") or [])
        ) or "<li class='research-empty'>아직 기록된 내용이 없습니다.</li>"
        _rsyn = research.get("synthesis") or {}
        _rexp = research.get("experiment") or {}
        _rev = research.get("evidence") or {}
        _research_evidence_parts = []
        if _rev.get("baseline_state"):
            _research_evidence_parts.append("기준선: " + str(_rev["baseline_state"]))
        if _rexp.get("normal_final_state"):
            _research_evidence_parts.append("실험 검증: " + str(_rexp["normal_final_state"]))
        if _rexp.get("normal_run_id"):
            _research_evidence_parts.append("Run: " + str(_rexp["normal_run_id"]))
        research_raw = html_lib.escape(json.dumps(research, ensure_ascii=False, indent=2))

        html = f"""<!DOCTYPE html>
<html>
<head>
    <title>AI DEV DIRECTOR - Command Center</title>
    <meta charset="utf-8">
    <style>{DASHBOARD_CSS}</style>
</head>
<body>
    <div id="hdr">
        <h1>AI DEV DIRECTOR<small>{I18N.t("app_subtitle")}</small></h1>
        <span class="sep">|</span>
        <span class="lbl" title="SYSTEM MODE: {system_mode}">{I18N.t("system_mode")}: <span class="status-{system_mode}" id="system-mode" data-raw="{system_mode}">{I18N.status_ko(system_mode)}</span><span id="system-mode-hint" data-raw="{mode_hint}">{html_lib.escape(mode_sentence)}</span><span class="rawtag">SYSTEM MODE: {system_mode}</span></span>
        <span class="sep">|</span>
        <span><span class="lbl">{I18N.t("project")}</span><span id="project-label">{project_name}</span></span>
        <span class="sep">|</span>
        <span><span class="lbl">{I18N.t("last_run")}</span><span id="hdr-run" class="mono">{hdr_run}</span> <span class="status-{run_state}" id="hdr-run-label" data-raw="{run_state}">{run_label_ko}</span></span>
        <span class="sep">|</span>
        <span id="hdr-human" class="badge {'b-red' if human else 'b-green'}" data-raw="{'HUMAN REQUIRED' if human else 'SAFE'}">{I18N.t("human_required") if human else I18N.t("safe")}</span>
        <span class="lbl" style="margin-left:8px;">{I18N.t("my_todo")}</span><span id="todo-line" style="color:var(--amber);">{html_lib.escape(st.get("todo") or "")}</span>
        <span id="mode-switch"><button id="normal-mode-btn" type="button" class="ghost on">NORMAL</button><button id="triad-mode-btn" type="button" class="ghost">TRIAD 연구</button></span>
        <button id="refresh-btn" type="button" class="ghost">{I18N.t("refresh_providers")}</button>
    </div>

    <div id="corestrip">
        <div id="director-core" class="{core_cls.strip()}">
            <div class="ctitle">AI DEV DIRECTOR CORE</div>
            <div class="csub">{I18N.t("core_sub")} \u00b7 <span class="status-{system_mode}" id="core-mode" data-raw="{system_mode}">{I18N.status_ko(system_mode)}</span></div>
            <div class="cstage" id="core-stage" data-raw="{core_stage}">{core_stage_ko}</div>
        </div>
        <div id="bus">
            <div class="busline"></div><div class="busbar"></div>
            <div id="nodes">
"""
        for name in ['OPENAI', 'GOOGLE', 'ANTHROPIC']:
            pr = st["providers"][name]
            status = html_lib.escape(pr["status"])
            node = (act.get("nodes") or {}).get(name, {})
            ncls = "node" + (" active" if node.get("active") else "") + (" backup" if node.get("backup") else "")
            ncls += " healthy" if pr["status"] == "HEALTHY" else (" recovering" if pr["status"] == "RECOVERING" else " down")
            glyph = "\u25c9" if pr["status"] == "HEALTHY" else ("\u25cc" if pr["status"] == "RECOVERING" else "\u25cb")
            ind = "ACTIVE" if node.get("active") else ("BACKUP" if node.get("backup") else "")
            latency = f'{pr["latency"]}s' if pr["latency"] else "--"
            html += f"""                <div class="{ncls}" id="node-{name}">
                    <div class="tap{' active' if node.get('active') else ''}" id="tap-{name}"></div>
                    <div class="ntitle"><span class="nname">{NODE_NAMES[name]}</span><span class="vendor">{I18N.PROVIDER_LABEL[name][1]}</span><span class="ind" id="node-{name}-ind" data-raw="{ind}">{ {"ACTIVE": "작업 중", "BACKUP": "예비"}.get(ind, "") }</span>
                    <details><summary>{I18N.t("details")}</summary><div class="dbox">
                        <div>{I18N.t("quota")}: <span id="prov-{name}-usage">{html_lib.escape(pr["usage"])}</span></div>
                        <div>{I18N.t("version")}: <span id="prov-{name}-version">{html_lib.escape(pr["version"])}</span></div>
                        <div>{I18N.t("last_error")}: <span id="prov-{name}-error">{html_lib.escape(pr["error"])}</span></div>
                        <div>raw status: {status} \u00b7 role: {html_lib.escape(pr["role"])} \u00b7 {html_lib.escape(I18N.HELP_TEXT.get(status, ""))}</div>
                    </div></details></div>
                    <table>
                        <tr><th>{I18N.t("state")}</th><td class="status-{status}" id="prov-{name}-status" data-raw="{status}">{I18N.status_ko(status)}</td></tr>
                        <tr><th>{I18N.t("role")}</th><td id="prov-{name}-role" data-raw="{html_lib.escape(pr["role"])}">{html_lib.escape(I18N.role_ko(pr["role"]))}</td></tr>
                        <tr><th>{I18N.t("latency")}</th><td id="prov-{name}-latency">{latency}</td></tr>
                    </table>
                </div>
"""
        html += f"""            </div>
        </div>
    </div>

    <section id="triad-research-center" aria-label="TRIAD Research Center">
        <div class="panel research-hero">
            <div><h2>TRIAD RESEARCH CENTER <span id="triad-mode-badge" class="badge {'b-blue' if research.get('active') else 'b-muted'}">{'진행 중' if research.get('active') else '읽기 전용'}</span></h2>
                <div class="research-moonshot" id="research-moonshot">{html_lib.escape(str(research.get('moonshot') or 'TRIAD 연구 세션이 없습니다.'))}</div>
            </div>
            <div class="research-meta">
                <span>세션 <b class="mono" id="research-session-id">{html_lib.escape(str(research.get('session_id') or '없음'))}</b></span>
                <span>사이클 <b id="research-cycle">{int(research.get('cycle_number') or 0)} / {int(research.get('cycle_budget') or 0)}</b></span>
                <span>단계 <b id="research-stage">{html_lib.escape(str(research.get('current_stage_ko') or '연구 없음'))}</b></span>
                <span>상태 <b class="mono" id="research-status">{html_lib.escape(str(research.get('status') or 'NONE'))}</b></span>
            </div>
        </div>
        <div class="panel research-column">
            <div class="research-card"><h3>3개 독립 제안</h3><ul id="research-proposals">{research_proposals_html}</ul></div>
            <div class="research-card"><h3>교차 비평</h3><ul id="research-critiques">{research_critiques_html}</ul></div>
            <div class="research-card"><h3>합성 가설</h3><p id="research-synthesis">{html_lib.escape(str(_rsyn.get('synthesized_hypothesis') or _rsyn.get('title') or '아직 합성된 가설이 없습니다.'))}</p></div>
        </div>
        <div class="panel research-column">
            <div class="research-card"><h3>실험</h3><p id="research-experiment">{html_lib.escape(str(_rexp.get('goal') or _rexp.get('status') or '아직 계획된 실험이 없습니다.'))}</p></div>
            <div class="research-card"><h3>증거</h3><p id="research-evidence">{html_lib.escape(' · '.join(_research_evidence_parts) or '아직 검증 증거가 없습니다.')}</p></div>
            <div class="research-card"><h3>기각된 아이디어</h3><ul id="research-rejected">{research_rejected_html}</ul></div>
        </div>
        <div class="panel research-column">
            <div class="research-card"><h3>배운 점</h3><p id="research-learned">{html_lib.escape(str(research.get('what_we_learned') or '아직 정리된 학습 내용이 없습니다.'))}</p></div>
            <div class="research-card"><h3>다음 단계</h3><p id="research-next">{html_lib.escape(str(research.get('next_step') or '다음 단계가 정해지지 않았습니다.'))}</p></div>
            <details class="research-card"><summary>원시 JSON · provenance · 실행 정보</summary><pre id="research-raw">{research_raw}</pre></details>
        </div>
    </section>

    <div id="main">
        <div class="panel" id="left">
            <h2>{I18N.t("project")}</h2>
            <div id="project-name" class="{proj_cls}">{project_name}</div>
            <div id="project-path" class="proj-path mono">{html_lib.escape(project_path)}</div>
            <div style="margin-top:6px;"><button id="project-change" type="button" class="ghost">{I18N.t("change")}</button></div>
            <form id="project-form" method="POST" action="/project" accept-charset="utf-8" style="{'display:none;' if project_path else ''}">
                <input type="text" id="project-input" name="path" placeholder="git 저장소의 절대 경로" />
                <button type="submit" id="project-btn">{I18N.t("select_project")}</button>
                <div id="project-result" style="color:var(--amber); font-size:11px; margin-top:4px;"></div>
            </form>
            <h2 style="margin-top:14px;">{I18N.t("recovery")}</h2>
            <div id="recovery-short" data-raw="{recovery_short}">{html_lib.escape(st.get("recovery_short_ko") or st.get("recovery_short") or "")}</div>
            <details id="recovery-details">
                <summary>{I18N.t("details")}</summary>
                <div id="recovery-trial" style="{trial_style}">{html_lib.escape(trial_text)}</div>
                <div id="recovery-reasons">{recovery_reasons}</div>
                <div>{html_lib.escape(I18N.HELP_TEXT["RECOVERING"])}</div>
            </details>
            <div id="results-panel" class="pendbox {'has' if results else 'none'}">
                <div id="results-count" data-raw="RESULTS ({len(results)})">{I18N.t("results")} {len(results)}건</div>
                <ul id="results-list">{results_html}</ul>
                <div id="results-more-row" style="{'' if results_rest_count else 'display:none;'}">
                    <button type="button" id="results-more-btn" class="ghost">나머지 {results_rest_count}건 보기</button>
                </div>
                <div id="results-grouped" style="display:none;">{results_grouped_html}</div>
            </div>
            <div id="pending-panel" class="pendbox {'has' if pending else 'none'}">
                <div id="pending-count" data-raw="PENDING ACTIONS ({len(pending)})">{I18N.t("pending_actions")} {len(pending)}건</div>
                <ul id="pending-list">{pending_html}</ul>
            </div>
            <details id="stale-details" style="margin-top:14px;{stale_style}">
                <summary><span id="stale-warning">{stale_text}</span></summary>
                <ul id="stale-list">{_stale_ids_html(stale_runs)}</ul>
            </details>
        </div>

        <div class="panel" id="chat-panel">
            <div id="chat-head"><h2>{I18N.t("head_chat")}</h2><span id="chat-badge" class="badge {chat_badge_cls}" data-raw="{chat_badge}">{chat_badge_ko}</span>
                <button id="new-chat-btn" type="button" class="ghost"{busy_attr}>{I18N.t("new_chat")}</button>
                <span class="session-label mono" id="session-label">{html_lib.escape(sid)}</span></div>
            <div id="chat-box" data-last-id="{last_id}">
                {chat_html}
            </div>
            <div id="chat-result-card" class="resultcard r-ready" style="margin:0 10px 6px 10px;{r_chat_style}">
                <div class="rh" data-raw="RUN PASS \u00b7 Result ready">{I18N.t("run_pass_ready")}</div>
                <div class="abtns"><button type="button" id="chat-view-result-btn" class="ghost">{I18N.t("view_result")}</button></div>
            </div>
            <div id="chat-action-card" class="approval {ap_cls}" style="{ap_style}">
                <div class="ah" id="chat-action-text" data-raw="{ap_headline}{(" — " + ap_reason + (" (" + ap_file + ")" if ap_file else "")) if ap_reason else ""}">{ap_headline_ko}{(" — " + ap_reason_ko + (" (" + ap_file + ")" if ap_file else "")) if ap_reason_ko and ap_show else ""}</div>
                <div class="an" id="chat-action-next" data-raw="{ap_next}">{ap_next_ko}</div>
                <div class="abtns">
                    <button type="button" id="chat-inspect-btn" class="ghost">{I18N.t("inspect")}</button>
                    <button type="button" id="chat-approve-btn" class="approve" style="{ap_btn_style}">{I18N.t("approve_resume")}</button>
                    <button type="button" id="chat-reject-btn" class="reject" style="{ap_btn_style}">{I18N.t("reject")}</button>
                </div>
            </div>
            <form id="chat-form" method="POST" action="/chat" accept-charset="utf-8">
                <textarea id="chat-input" name="message" rows="2" placeholder="{I18N.t("input_placeholder")}"></textarea>
                <div><button type="submit" id="send-btn"{busy_attr}>{I18N.t("send")}</button><div id="send-status">{busy_text}</div></div>
            </form>
        </div>

        <div class="panel" id="right">
            <h2>{I18N.t("current_run")} <span id="run-label" class="status-{run_state}" data-raw="{run_state}">{run_label_ko}</span><span id="pipeline-badge" class="badge b-muted" style="display:none;"></span><span id="run-result-state" data-raw="{r_state_label}">{r_state_label_ko}</span></h2>
            <div id="stage-sentence" class="stage-sentence">{html_lib.escape(st.get("stage_sentence") or "")}</div><div id="next-step" class="next-step">{("다음 단계: " + st["next_step"]) if st.get("next_step") else ""}</div>
            <div id="run-todo-box" class="todobox {todo_kind}" style="{todo_style}">
                <div class="tt">{I18N.t("my_todo")}</div>
                <div id="run-todo-text">{todo_text_ko}</div>
                <div class="abtns" id="run-todo-actions">
                    <button type="button" id="todo-inspect-btn" class="ghost" style="{'' if todo_kind == 'approvable' else 'display:none;'}">{I18N.t("inspect")}</button>
                    <button type="button" id="todo-approve-btn" class="approve" style="{'' if todo_kind == 'approvable' else 'display:none;'}">{I18N.t("approve_resume")}</button>
                    <button type="button" id="todo-reject-btn" class="reject" style="{'' if todo_kind == 'approvable' else 'display:none;'}">{I18N.t("reject")}</button>
                    <button type="button" id="todo-view-result-btn" class="ghost" style="{'' if todo_kind == 'pass' else 'display:none;'}">{I18N.t("view_result")}</button>
                    <button type="button" id="todo-retry-btn" class="ghost" data-prompt="{ap_retry_prompt}" style="{'' if todo_kind == 'nonapprovable' else 'display:none;'}">HEAD\uc5d0\uac8c \ub2e4\uc2dc \uc694\uccad</button>
                    <button type="button" id="todo-details-btn" class="ghost" style="{'' if todo_kind == 'nonapprovable' else 'display:none;'}">{I18N.t("details")}</button>
                </div>
            </div>
            <div class="pipeline">
                <div class="stage" id="stage-0" title="{I18N.STAGE_TEXT["HEAD"]}"><span class="mark">\u25cb</span>HEAD<small>{I18N.STAGE_TEXT["HEAD"]}</small></div>
                <div class="stage" id="stage-1" title="{I18N.STAGE_TEXT["BUILD"]}"><span class="mark">\u25cb</span>BUILD<small>{I18N.STAGE_TEXT["BUILD"]}</small></div>
                <div class="stage" id="stage-2" title="{I18N.STAGE_TEXT["VERIFY"]}"><span class="mark">\u25cb</span>VERIFY<small>{I18N.STAGE_TEXT["VERIFY"]}</small></div>
                <div class="stage" id="stage-3" title="{I18N.STAGE_TEXT["REVIEW"]}"><span class="mark">\u25cb</span>REVIEW<small>{I18N.STAGE_TEXT["REVIEW"]}</small></div>
                <div class="stage" id="stage-4" title="{I18N.STAGE_TEXT["DECIDE"]}"><span class="mark">\u25cb</span>DECIDE<small>{I18N.STAGE_TEXT["DECIDE"]}</small></div>
            </div>
            <div class="kv">
                <span class="k">{I18N.t("state")}</span><span class="v status-{run_state}" id="run-state" data-raw="{run_state}">{I18N.state_ko(run_state)}</span>
                <span class="k">{I18N.t("run_id")}</span><span class="v mono" id="run-id" title="{html_lib.escape(str(run["run_id"]))}">{html_lib.escape(str(run["run_id"]))}</span>
                <span class="k">{I18N.t("elapsed")}</span><span class="v" id="run-elapsed">{elapsed}</span>
                <span class="k" title="{I18N.t("human_required")}">확인 필요</span><span class="v" id="run-human" data-raw="{run["human_required"]}">{"예" if run["human_required"] == "YES" else "아니오"}</span>
                <span class="k">{I18N.t("started")}</span><span class="v mono" id="run-created">{html_lib.escape(str(run["created_at"]))}</span>
            </div>
            <div id="approval-card" class="approval {ap_cls}" style="{ap_style}">
                <div class="ah" id="approval-headline" data-raw="{ap_headline}">{ap_headline_ko}</div>
                <div class="an" id="approval-next" data-raw="{ap_next}">{ap_next_ko}</div>
                <div id="approval-approvable" style="{ap_approvable_style}">
                    <div><span class="ak">{I18N.t("reason")}: </span><span id="approval-reason">{ap_reason_ko}</span></div>
                    <div><span class="ak">{I18N.t("file")}: </span><span id="approval-file" class="mono">{ap_file}</span></div>
                    <div><span class="ak">{I18N.t("why")}: </span><span id="approval-why">{ap_why_ko}</span></div>
                    <div><span class="ak">{I18N.t("stage")}: </span><span id="approval-stage">{ap_stage_ko}</span></div>
                    <div><span class="ak">{I18N.t("builder")}: </span><span id="approval-builder">{ap_builder_ko}</span></div>
                    <div class="abtns">
                        <button type="button" id="inspect-btn" class="ghost">{I18N.t("inspect")}</button>
                        <button type="button" id="approve-btn" class="approve" style="{ap_btn_style}">{I18N.t("approve_resume")}</button>
                        <button type="button" id="reject-btn" class="reject" style="{ap_btn_style}">{I18N.t("reject")}</button>
                    </div>
                </div>
                <div id="approval-nonapprovable" style="{ap_nonapprovable_style}">
                    <div id="ap-na-desc">{ap_na_desc_ko}</div>
                    <div id="ap-na-why">{ap_na_why_ko}</div>
                    <div id="ap-na-path-row" style="{ap_path_style}"><span class="ak">문제가 된 경로:</span> <span id="ap-na-path" class="mono">{ap_block_file}</span></div>
                    <div id="ap-na-blocked">이 작업은 승인으로 계속할 수 없습니다.</div>
                    <div id="ap-na-reco"><span class="ak">권장 조치:</span> <span id="ap-na-reco-text">{ap_na_reco_ko}</span></div>
                    <div class="abtns">
                        <button type="button" id="approval-retry-btn" class="ghost" data-prompt="{ap_retry_prompt}">HEAD에게 다시 요청</button>
                        <button type="button" id="approval-details-btn" class="ghost">{I18N.t("details")}</button>
                    </div>
                </div>
                <details id="approval-raw-details"><summary>{I18N.t("details")}</summary><div id="approval-raw">{I18N.t("technical_reason")}: {ap_code} \u00b7 {ap_raw}</div></details>
                <div id="approval-result"></div>
            </div>
            <details id="run-details-panel" style="margin-top:8px;">
                <summary>{I18N.t("details")}</summary>
                <div id="run-details">{html_lib.escape(run.get("details") or "")}</div>
                <details id="inspect-panel"><summary>{I18N.t("inspect")} (read-only)</summary><pre id="inspect-body"></pre></details>
                <div>실행 역할은 상단 AI 카드에서 확인하세요. 역할 변경 버튼의 배정은 다음 대화/작업부터 적용됩니다.</div>
            </details>
        </div>
    </div>

    <div id="result-modal-overlay" class="modal-overlay" style="display:none;">
            <div id="result-card" class="resultcard modalbox {r_cls}">
                <div class="rh" id="result-headline" data-raw="{r_headline}">{r_headline_ko}</div>
                <div class="rs" id="result-sub" data-raw="{r_sub}">{r_sub_ko}</div>
                <div id="result-files" data-raw="{r_files}">{r_files_ko}</div>
                <div id="result-tests" data-raw="{r_tests}">{r_tests_ko}</div>
                <div class="abtns">
                    <button type="button" id="result-view-btn" class="ghost">{I18N.t("view_changes")}</button>
                    <button type="button" id="result-tests-btn" class="ghost">{I18N.t("test_results")}</button>
                    <button type="button" id="result-open-btn" class="ghost">{I18N.t("open_worktree")}</button>
                    <button type="button" id="result-apply-btn" class="apply"{r_apply_attr}>{I18N.t("apply_verify")}</button>
                    <button type="button" id="result-discard-btn" class="discard"{r_act_attr}>{I18N.t("discard")}</button>
                    <button type="button" id="result-openproj-btn" class="ghost" style="display:none;">{I18N.t("open_project")}</button>
                    <button type="button" id="result-close-btn" class="ghost">{I18N.t("close")}</button>
                </div>
                <div id="result-confirm" style="display:none;">
                    <div class="ct" id="confirm-title"></div>
                    <pre id="confirm-body"></pre>
                    <div class="abtns"><button type="button" id="confirm-cancel" class="ghost">{I18N.t("cancel")}</button><button type="button" id="confirm-ok" class="apply">{I18N.t("apply_confirm_ok")}</button></div>
                </div>
                <div id="result-status"></div>
                <div id="result-detail" style="display:none;">
                    <div class="rdtabs"><button type="button" id="rd-tab-changes" class="ghost on">{I18N.t("view_changes")}</button><button type="button" id="rd-tab-tests" class="ghost">{I18N.t("test_results")}</button><button type="button" id="rd-close" class="ghost" style="margin-left:auto;">{I18N.t("close")}</button></div>
                    <div id="rd-head"></div>
                    <ul id="rd-files"></ul>
                    <pre id="rd-diff"></pre>
                    <div id="rd-evidence"></div>
                </div>
            </div>
    </div>

    <div id="alert-bar" class="alert {alert_cls}">
        <span id="alert-text" data-raw="{html_lib.escape(alert_text)}">{html_lib.escape(alert_text_ko)}</span>
        <div class="abtns" style="margin-left:auto;">
            <button id="alert-inspect" type="button" class="ghost">{I18N.t("inspect")}</button>
            <button id="alert-approve-btn" type="button" class="approve" style="{ap_btn_style}">{I18N.t("approve_resume")}</button>
            <button id="alert-reject-btn" type="button" class="reject" style="{ap_btn_style}">{I18N.t("reject")}</button>
        </div>
    </div>
    <script>window.__I18N = {json.dumps(I18N.i18n_bundle(), ensure_ascii=False)};</script>
    <script>{DASHBOARD_JS}</script>
</body>
</html>"""
        return html
    except Exception as e:
        return f"Error connecting to DB: {str(e)}\n\n"


class DashboardHandler(BaseHTTPRequestHandler):
    def _redirect_home(self):
        self.send_response(303)
        self.send_header('Location', '/')
        self.end_headers()

    def _send_json(self, payload: dict, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _wants_json(self) -> bool:
        accept = (self.headers.get('Accept') or '').lower()
        return 'application/json' in accept or (self.headers.get('X-Requested-With') or '').lower() == 'fetch'

    def _read_form(self) -> dict:
        try:
            content_length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            content_length = 0
        post_data = self.rfile.read(content_length).decode('utf-8', errors='replace') if content_length else ""
        return urllib.parse.parse_qs(post_data)

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == '/':
            body = render_html().encode('utf-8')
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == '/status':
            try:
                payload = cached_status_payload()
            except Exception as e:
                payload = {"error": str(e)}
            self._send_json(payload)
        elif parsed.path == '/ops/activity':
            try:
                self._send_json(build_activity_payload())
            except Exception as e:
                self._send_json({"error": str(e)[:300]}, 500)
        elif parsed.path == '/ops/digest':
            try:
                self._send_json(build_digest_payload())
            except Exception as e:
                self._send_json({"error": str(e)[:300]}, 500)
        elif parsed.path == '/ops/replay':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                self._send_json(build_replay_payload(q.get('mission_id', [''])[0][:80]))
            except Exception as e:
                self._send_json({"error": str(e)[:300]}, 500)
        elif parsed.path == '/research':
            q = urllib.parse.parse_qs(parsed.query)
            session_id = q.get('session_id', [None])[0]
            try:
                payload = build_research_status_payload(session_id=session_id)
            except Exception as e:
                payload = {"mode": "TRIAD_RESEARCH", "available": False, "active": False,
                           "error": str(e)[:300]}
            self._send_json(payload)
        elif parsed.path == '/result':
            q = urllib.parse.parse_qs(parsed.query)
            run_id = q.get('run_id', [''])[0]
            try:
                payload = _result_manager().summary(run_id, with_diffs=True) if run_id else {"error": "run_id required"}
                payload["preconditions"] = _result_manager().preconditions(run_id) if run_id and payload.get("is_pass") else {"ok": False, "blocks": ["not a PASS run"]}
                payload["preconditions"].pop("manifest", None)
            except Exception as e:
                payload = {"error": str(e)[:300], "run_id": run_id}
            self._send_json(payload)
        elif parsed.path == '/run/approval':
            q = urllib.parse.parse_qs(parsed.query)
            run_id = q.get('run_id', [''])[0]
            try:
                run = StateMachine().get_run(run_id) if run_id else None
                if not run:
                    payload = {"required": False, "error": "run not found", "run_id": run_id}
                else:
                    trans = StateMachine().get_transitions(run_id)
                    payload = approval_info(run_id, run["current_state"], (trans[-1]["details"] or "") if trans else "")
            except Exception as e:
                payload = {"required": False, "error": str(e)[:300], "run_id": run_id}
            self._send_json(payload)
        elif parsed.path == '/run/inspect':
            q = urllib.parse.parse_qs(parsed.query)
            run_id = q.get('run_id', [''])[0]
            try:
                payload = inspect_run(run_id) if run_id else {"error": "run_id required"}
            except Exception as e:
                payload = {"error": str(e)[:300], "run_id": run_id}
            self._send_json(payload)
        elif parsed.path == '/chat/messages':
            q = urllib.parse.parse_qs(parsed.query)
            try:
                after_id = int(q.get('after_id', ['0'])[0])
            except ValueError:
                after_id = 0
            session_id = q.get('session_id', [None])[0] or active_session_id()
            try:
                payload = {"messages": chat_messages_after(after_id, session_id), "head_busy": chat_in_flight(), "session_id": session_id}
            except Exception as e:
                payload = {"messages": [], "head_busy": chat_in_flight(), "error": str(e)}
            self._send_json(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        port = self.server.server_address[1]
        ok, reason = is_request_origin_allowed(self.headers, port)
        if not ok:
            body = f"403 Forbidden: {reason}".encode("utf-8")
            self.send_response(403)
            self.send_header('Content-type', 'text/plain; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        wants_json = self._wants_json()
        if self.path == '/chat':
            params = self._read_form()
            msg = params.get('message', [''])[0]
            request_id = params.get('request_id', [''])[0]
            result = submit_chat(msg, request_id) if msg else {"status": "empty"}
            if wants_json:
                code = 409 if result["status"] == "busy" else (500 if result["status"] == "error" else 200)
                self._send_json(result, code)
            else:
                self._redirect_home()
        elif self.path == '/project':
            params = self._read_form()
            path = params.get('path', [''])[0]
            message = set_project(path) if path else "PROJECT_SELECTION_REJECTED: empty path"
            if wants_json:
                self._send_json({"status": "ok" if message.startswith("PROJECT_SELECTED") else "rejected", "message": message})
            else:
                self._redirect_home()
        elif self.path == '/run/approve':
            params = self._read_form()
            run_id = params.get('run_id', [''])[0]
            result = approve_run(run_id, params.get('request_id', [''])[0], params.get('note', [''])[0]) if run_id else {"status": "error", "error": "run_id required"}
            if wants_json:
                self._send_json(result, 409 if result.get("status") == "busy" else (400 if result.get("status") in ("not_applicable", "error") else 200))
            else:
                self._redirect_home()
        elif self.path == '/run/reject':
            params = self._read_form()
            run_id = params.get('run_id', [''])[0]
            result = reject_run(run_id, params.get('note', [''])[0]) if run_id else {"status": "error", "error": "run_id required"}
            if wants_json:
                self._send_json(result, 200 if result.get("status") == "ok" else 400)
            else:
                self._redirect_home()
        elif self.path == '/ops/mission/decide':
            params = self._read_form()
            try:
                result = decide_mission(params.get('mission_id', [''])[0], params.get('action', [''])[0],
                                        reason=params.get('reason', [''])[0], max_steps=int(params.get('max_steps', ['0'])[0] or 0) or None,
                                        on_conflict=params.get('on_conflict', ['block'])[0], by="HUMAN")
            except Exception as e:
                result = {"status": "error", "error": str(e)[:300]}
            self._send_json(result, 200 if result.get("status") not in ("error",) else 400)
        elif self.path in ('/result/apply', '/result/discard', '/result/open_worktree', '/result/open_project'):
            params = self._read_form()
            run_id = params.get('run_id', [''])[0]
            request_id = params.get('request_id', [''])[0]
            if not run_id:
                result = {"status": "error", "error": "run_id required"}
            else:
                rm = _result_manager()
                invalidate_status_cache()
                try:
                    if self.path == '/result/apply':
                        snapshot_based = (rm.run_dir(run_id) / "source_snapshot.json").exists()
                        result = (rm.apply_tree if snapshot_based else rm.apply)(run_id, request_id=request_id, applied_by="HUMAN")
                    elif self.path == '/result/discard':
                        result = rm.discard(run_id, request_id=request_id, by="HUMAN")
                    elif self.path == '/result/open_worktree':
                        result = rm.open_worktree(run_id)
                    else:
                        result = rm.open_project(run_id)
                except Exception as e:
                    result = {"status": "error", "error": str(e)[:300]}
                try:
                    cm = ChatManager()
                    if result.get("status") in ("applied", "rolled_back", "blocked", "discarded"):
                        cm.add_message(active_session_id(), "SYSTEM", f"RESULT_{result['status'].upper()}: run {run_id} " + (result.get("reason") or "; ".join(result.get("blocks", [])) or ", ".join(result.get("files", []))), run_id=run_id)
                except Exception:
                    pass
            code = 200 if result.get("status") in ("applied", "discarded", "ok", "duplicate") else (409 if result.get("status") == "busy" else 400)
            if wants_json:
                self._send_json(result, code)
            else:
                self._redirect_home()
        elif self.path == '/chat/new':
            result = new_chat_session()
            if wants_json:
                self._send_json(result, 409 if result.get("status") == "busy" else 200)
            else:
                self._redirect_home()
        elif self.path == '/providers/refresh':
            statuses = refresh_providers()
            if wants_json:
                self._send_json({"status": "ok", "providers": statuses})
            else:
                self._redirect_home()
        else:
            self.send_response(404)
            self.end_headers()


def run(server_class=DashboardServer, handler_class=DashboardHandler, host=BIND_HOST, port=DEFAULT_PORT):
    """Serves the dashboard on localhost only. Requests are handled concurrently (ThreadingHTTPServer)
    so a slow HEAD CLI call never blocks the status page; DEV_TASK concurrency is guarded separately."""
    if host != BIND_HOST:
        raise ValueError(f"Dashboard must bind to {BIND_HOST} only (got {host!r}); LAN exposure is forbidden")
    _ensure_schema()
    server_address = (host, port)
    try:
        httpd = server_class(server_address, handler_class)
    except DashboardAlreadyRunning as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        raise
    def _backfill():
        try:
            n = _result_manager().backfill_missing_summaries()
            classify_stale_human_actions(limit=1000)
            print(f"[dashboard] persisted result summaries for {n} legacy PASS runs; pending-action classification drained", file=sys.stderr)
        except Exception as e:
            print(f"[dashboard] backfill failed: {e}", file=sys.stderr)
    threading.Thread(target=_backfill, name="director-backfill", daemon=True).start()
    print(f'Starting dashboard server on http://{host}:{port}/ (PID {os.getpid()}) ...')
    httpd.serve_forever()


if __name__ == '__main__':
    try:
        run()
    except DashboardAlreadyRunning:
        sys.exit(2)
