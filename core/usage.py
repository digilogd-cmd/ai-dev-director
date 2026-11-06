"""AI work ledger: every official-CLI call (Claude / Codex / Antigravity) with its role, stage, time and tokens.

Recorded at the single choke point every AI CLI call passes through (ProcessTreeRunner), so nothing escapes it.
A row is written when a call STARTS (status RUNNING) and completed when it ends; the dashboard can therefore show
which AI is working right now even when the mission runs in another process. Tokens are the CLI's own numbers when
it reports them (Claude's JSON envelope, Codex's "tokens used" line, Antigravity usage metadata); otherwise an
estimate from the text size, flagged as such.
"""
import contextvars
import json
import os
import re
import sqlite3
from core.db_path import connect_db
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROVIDER_BY_CLI = {"claude": "ANTHROPIC", "codex": "OPENAI", "agy": "GOOGLE"}
PROVIDER_NAME = {"ANTHROPIC": "Claude", "OPENAI": "Codex", "GOOGLE": "Antigravity"}
ROLE_NAME = {"HEAD": "HEAD · 지휘", "BUILDER": "BUILDER · 구현", "REVIEWER": "REVIEWER · 검수"}
STALE_AFTER = timedelta(minutes=30)          # longer than any CLI timeout: a RUNNING row older than this died with its process

_CTX: contextvars.ContextVar = contextvars.ContextVar("director_usage_ctx", default=None)


def set_context(**fields) -> None:
    current = dict(_CTX.get() or {})
    current.update(fields)
    _CTX.set(current)


def context() -> Dict[str, Any]:
    return dict(_CTX.get() or {})


def provider_of(argv0: str) -> Optional[str]:
    base = os.path.basename(str(argv0 or "")).lower()
    return PROVIDER_BY_CLI.get(base.split(".")[0])


def estimate_tokens(text: Optional[str]) -> int:
    """~4 chars/token for ASCII (code, English), ~1.5 for Hangul and other scripts."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return int(ascii_chars / 4 + (len(text) - ascii_chars) / 1.5) + 1


def reported_tokens(provider: str, stdout: str, stderr: str) -> Optional[Dict[str, int]]:
    """Token counts the CLI itself reported, when it did."""
    try:
        if provider == "ANTHROPIC":
            envelope = json.loads(stdout)
            use = envelope.get("usage") or {}
            tin = sum(int(use.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            tout = int(use.get("output_tokens") or 0)
            if tin or tout:
                return {"in": tin, "out": tout}
        if provider == "OPENAI":
            match = re.search(r"tokens used[:\s]*([\d,]+)", (stdout or "") + "\n" + (stderr or ""), re.I)
            if match:
                total = int(match.group(1).replace(",", ""))
                return {"in": None, "out": None, "total": total}
        if provider == "GOOGLE":
            ins = [int(x) for x in re.findall(r'"promptTokenCount"\s*:\s*(\d+)', stdout or "")]
            outs = [int(x) for x in re.findall(r'"candidatesTokenCount"\s*:\s*(\d+)', stdout or "")]
            if ins or outs:
                return {"in": max(ins or [0]), "out": sum(outs)}
    except (ValueError, TypeError, AttributeError):
        return None
    return None


# ------------------------------------------------------------------ storage

def _conn() -> sqlite3.Connection:
    from core.db_path import get_db_path
    conn = connect_db(get_db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS ai_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
        provider TEXT NOT NULL, role TEXT, stage TEXT, run_id TEXT, mission_id TEXT, pid INTEGER,
        duration_sec REAL, prompt_chars INTEGER, output_chars INTEGER,
        tokens_in INTEGER, tokens_out INTEGER, tokens_total INTEGER, tokens_exact INTEGER DEFAULT 0,
        exit_code INTEGER, source TEXT DEFAULT 'live')""")
    conn.execute("CREATE INDEX IF NOT EXISTS ai_usage_started ON ai_usage(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ai_usage_mission ON ai_usage(mission_id)")
    return conn


def call_started(argv: List[str], prompt: Optional[str]) -> Optional[int]:
    provider = provider_of(argv[0] if argv else "")
    # Real work always sends a prompt on stdin; prompt-less calls are health/version probes, not AI work.
    if not provider or not (prompt or "").strip():
        return None
    ctx = context()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO ai_usage (started_at, status, provider, role, stage, run_id, mission_id, pid, prompt_chars) "
            "VALUES (?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?)",
            (datetime.utcnow().isoformat(), provider, ctx.get("role"), ctx.get("stage"), ctx.get("run_id"),
             ctx.get("mission_id"), os.getpid(), len(prompt or "")))
        return cur.lastrowid


def _answer_text(provider: str, stdout: str) -> str:
    """The model's actual answer: Antigravity's stream-json wraps it (and every intermediate event) in NDJSON, so
    counting the raw stream would overstate output tokens several times over."""
    if provider != "GOOGLE":
        return stdout
    answer = None
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(event, dict) and event.get("event") == "result" and isinstance(event.get("result"), dict):
            answer = str(event["result"].get("response") or "")
    return stdout if answer is None else answer


def call_finished(row_id: Optional[int], argv: List[str], prompt: Optional[str], result: Any) -> None:
    if row_id is None:
        return
    provider = provider_of(argv[0] if argv else "")
    stdout, stderr = getattr(result, "stdout", "") or "", getattr(result, "stderr", "") or ""
    exact = reported_tokens(provider, stdout, stderr)
    est_in, est_out = estimate_tokens(prompt), estimate_tokens(_answer_text(provider, stdout))
    if exact and exact.get("in") is not None:
        tin, tout, total, is_exact = exact["in"], exact["out"], exact["in"] + exact["out"], 1
    elif exact and exact.get("total"):
        tin, tout, total, is_exact = None, None, exact["total"], 1
    else:
        tin, tout, total, is_exact = est_in, est_out, est_in + est_out, 0
    exit_code = getattr(result, "exit_code", None)
    status = "TIMEOUT" if getattr(result, "timed_out", False) else ("OK" if exit_code == 0 else "ERROR")
    with _conn() as conn:
        conn.execute(
            "UPDATE ai_usage SET ended_at=?, status=?, duration_sec=?, output_chars=?, tokens_in=?, tokens_out=?, "
            "tokens_total=?, tokens_exact=?, exit_code=? WHERE id=?",
            (datetime.utcnow().isoformat(), status, getattr(result, "duration_sec", None), len(stdout),
             tin, tout, total, is_exact, exit_code, row_id))


def call_failed(row_id: Optional[int], error: BaseException) -> None:
    if row_id is None:
        return
    with _conn() as conn:
        conn.execute("UPDATE ai_usage SET ended_at=?, status='ERROR' WHERE id=?", (datetime.utcnow().isoformat(), row_id))


# ------------------------------------------------------------------ backfill of calls made before the ledger existed

def backfill_from_runs(runs_dir: Path, missions: List[Dict[str, Any]]) -> int:
    """Past runs only have provenance (who ran, how long); tokens stay unknown. Idempotent per run."""
    mission_of = {s.get("run_id"): m["mission_id"] for m in missions for s in m.get("steps", []) if s.get("run_id")}
    added = 0
    with _conn() as conn:
        done = {r[0] for r in conn.execute("SELECT DISTINCT run_id FROM ai_usage WHERE source='backfill'")}
        live = {r[0] for r in conn.execute("SELECT DISTINCT run_id FROM ai_usage WHERE source='live' AND run_id IS NOT NULL")}
        for run_id, mission_id in mission_of.items():
            if run_id in done or run_id in live:
                continue
            path = Path(runs_dir) / run_id / "provenance.json"
            try:
                records = json.loads(path.read_text(encoding="utf-8")).get("records", [])
            except (OSError, ValueError, AttributeError):
                continue
            for rec in records:
                provider = {"anthropic": "ANTHROPIC", "claude": "ANTHROPIC", "openai": "OPENAI", "codex": "OPENAI",
                            "google": "GOOGLE", "antigravity": "GOOGLE"}.get(str(rec.get("provider", "")).lower())
                if not provider:
                    continue
                conn.execute(
                    "INSERT INTO ai_usage (started_at, ended_at, status, provider, role, stage, run_id, mission_id, "
                    "duration_sec, exit_code, source) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, 'backfill')",
                    (rec.get("timestamp") or "", rec.get("timestamp") or "",
                     "OK" if rec.get("exit_code") == 0 else "ERROR", provider, rec.get("agent"), run_id, mission_id,
                     rec.get("duration_sec"), rec.get("exit_code")))
                added += 1
    return added


# ------------------------------------------------------------------ dashboard views

def _rows(where: str = "1=1", params: tuple = ()) -> List[sqlite3.Row]:
    with _conn() as conn:
        return conn.execute(f"SELECT * FROM ai_usage WHERE {where} ORDER BY id", params).fetchall()


def aggregate(rows) -> Dict[str, Any]:
    """Per role and per provider: calls, failures, minutes of CLI time, tokens (and how much of it is exact)."""
    def empty():
        return {"calls": 0, "failed": 0, "seconds": 0.0, "tokens": 0, "exact_tokens": 0, "unknown_calls": 0,
                "providers": {}}
    by_role, by_provider, total = {}, {}, empty()
    for r in rows:
        if r["status"] == "RUNNING":
            continue
        role_bucket = by_role.setdefault(r["role"] or "OTHER", empty())
        role_bucket["providers"][r["provider"]] = role_bucket["providers"].get(r["provider"], 0) + 1
        for bucket in (by_role.setdefault(r["role"] or "OTHER", empty()),
                       by_provider.setdefault(r["provider"], empty()), total):
            bucket["calls"] += 1
            bucket["failed"] += r["status"] != "OK"
            bucket["seconds"] += float(r["duration_sec"] or 0)
            if r["tokens_total"] is None:
                bucket["unknown_calls"] += 1
            else:
                bucket["tokens"] += int(r["tokens_total"])
                bucket["exact_tokens"] += int(r["tokens_total"]) if r["tokens_exact"] else 0
    for bucket in list(by_role.values()) + list(by_provider.values()) + [total]:
        bucket["seconds"] = round(bucket["seconds"], 1)
    return {"by_role": by_role, "by_provider": by_provider, "total": total}


def _pid_alive(pid) -> bool:
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return True                                        # unknown -> keep showing it (STALE_AFTER still applies)


def running_now() -> List[Dict[str, Any]]:
    cutoff = (datetime.utcnow() - STALE_AFTER).isoformat()
    out = []
    for r in _rows("status='RUNNING' AND started_at >= ?", (cutoff,)):
        started = datetime.fromisoformat(r["started_at"])
        if r["pid"] and not _pid_alive(r["pid"]):
            # The Director process that made this call was stopped mid-call: close the row instead of showing a
            # phantom "working now" for up to STALE_AFTER.
            with _conn() as conn:
                conn.execute("UPDATE ai_usage SET ended_at=?, status='INTERRUPTED', duration_sec=? WHERE id=? AND status='RUNNING'",
                             (datetime.utcnow().isoformat(), (datetime.utcnow() - started).total_seconds(), r["id"]))
            continue
        out.append({"provider": r["provider"], "provider_name": PROVIDER_NAME.get(r["provider"], r["provider"]),
                    "role": r["role"], "stage": r["stage"], "run_id": r["run_id"], "mission_id": r["mission_id"],
                    "started_at": r["started_at"], "elapsed_sec": int((datetime.utcnow() - started).total_seconds())})
    return out


def recent_calls(limit: int = 20) -> List[Dict[str, Any]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM ai_usage WHERE status!='RUNNING' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"at": r["ended_at"] or r["started_at"], "provider": r["provider"],
             "provider_name": PROVIDER_NAME.get(r["provider"], r["provider"]), "role": r["role"], "stage": r["stage"],
             "status": r["status"], "seconds": r["duration_sec"], "tokens": r["tokens_total"],
             "exact": bool(r["tokens_exact"]), "run_id": r["run_id"], "mission_id": r["mission_id"],
             "source": r["source"]} for r in rows]


def mission_calls(mission_id: str, limit: int = 400) -> List[Dict[str, Any]]:
    """Every finished AI call of one mission in time order (the console replays a mission from these)."""
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM ai_usage WHERE mission_id=? AND status!='RUNNING' ORDER BY started_at LIMIT ?",
                            (mission_id, limit)).fetchall()
    return [{"started_at": r["started_at"], "seconds": r["duration_sec"] or 0, "provider": r["provider"],
             "provider_name": PROVIDER_NAME.get(r["provider"], r["provider"]), "role": r["role"], "stage": r["stage"],
             "status": r["status"], "tokens": r["tokens_total"], "exact": bool(r["tokens_exact"]),
             "run_id": r["run_id"]} for r in rows]


def summary(mission_id: Optional[str] = None) -> Dict[str, Any]:
    kst_now = datetime.utcnow() + timedelta(hours=9)
    today = kst_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=9)  # KST midnight, in UTC
    out = {"today": aggregate(_rows("started_at >= ?", (today.isoformat(),))), "all": aggregate(_rows())}
    if mission_id:
        out["mission"] = aggregate(_rows("mission_id = ?", (mission_id,)))
    return out
