import sqlite3
from core.db_path import connect_db
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

class State:
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    ORDER_READY = "ORDER_READY"
    BUILDING = "BUILDING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    DECIDING = "DECIDING"
    PASS = "PASS"
    FAIL = "FAIL"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    TIMEOUT = "TIMEOUT"

# Strict forward transitions
VALID_NORMAL_TRANSITIONS = {
    State.IDLE: [State.PLANNING],
    State.PLANNING: [State.ORDER_READY],
    State.ORDER_READY: [State.BUILDING],
    State.BUILDING: [State.VERIFYING],
    State.VERIFYING: [State.REVIEWING],
    State.REVIEWING: [State.DECIDING],
    State.DECIDING: [State.PASS, State.BUILDING],
}

# Terminal states
TERMINAL_STATES = {State.PASS, State.FAIL, State.HUMAN_REQUIRED, State.TIMEOUT}

class InvalidStateTransitionError(Exception):
    pass

class StateMachine:
    def __init__(self, db_path: Optional[str] = None):
        from core.db_path import get_db_path
        if db_path is None:
            self.db_path = get_db_path()
        else:
            self.db_path = Path(db_path)
        
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = connect_db(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    current_state TEXT NOT NULL,
                    worktree_path TEXT,
                    branch_name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS state_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    from_state TEXT NOT NULL,
                    to_state TEXT NOT NULL,
                    details TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS provider_health (
                    provider TEXT PRIMARY KEY,
                    cli TEXT,
                    version TEXT,
                    status TEXT NOT NULL,
                    last_success DATETIME,
                    last_failure DATETIME,
                    failure_reason TEXT,
                    current_role TEXT,
                    last_latency_sec REAL,
                    consecutive_failures INTEGER DEFAULT 0,
                    usage_percent REAL,
                    remaining REAL,
                    reset_at DATETIME,
                    last_checked DATETIME
                )
            ''')
            # v0.2.4.7: persisted dashboard state so /status never scans all runs or spawns git
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS human_action_state (
                    run_id TEXT PRIMARY KEY,
                    stage TEXT,
                    actionable INTEGER DEFAULT 0,
                    resolved INTEGER DEFAULT 0,
                    needs_classify INTEGER DEFAULT 1,
                    reason TEXT,
                    items_json TEXT,
                    blocking_json TEXT,
                    updated_at TEXT
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS result_state (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    changed_count INTEGER,
                    counts_json TEXT,
                    tests_state TEXT,
                    reviewer_state TEXT,
                    safety_state TEXT,
                    worktree_state TEXT,
                    detail TEXT,
                    request_id TEXT,
                    updated_at TEXT
                )
            ''')
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_state ON runs(current_state)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_runs_project_state ON runs(project_path, current_state)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_transitions_run ON state_transitions(run_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_run_action ON audit_logs(run_id, action)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_has_actionable ON human_action_state(actionable, resolved, needs_classify)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_result_state ON result_state(state)")
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS provider_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL,
                    usage_percent REAL,
                    latency REAL,
                    success INTEGER NOT NULL
                )
            ''')
            conn.commit()

    def create_run(self, run_id: str, project_path: str, goal: str, worktree_path: Optional[str] = None, branch_name: Optional[str] = None) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, project_path, goal, current_state, worktree_path, branch_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, project_path, goal, State.IDLE, worktree_path, branch_name, now, now)
            )
            conn.execute(
                """
                INSERT INTO state_transitions (run_id, from_state, to_state, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, "NONE", State.IDLE, "Run initialized", now)
            )
            conn.commit()

    def update_run_worktree(self, run_id: str, worktree_path: str, branch_name: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE runs
                SET worktree_path = ?, branch_name = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (worktree_path, branch_name, now, run_id)
            )
            conn.commit()

    def can_transition(self, current_state: str, new_state: str) -> bool:
        # FAIL, HUMAN_REQUIRED, and TIMEOUT can be transitioned to from any non-terminal state
        if new_state in {State.FAIL, State.HUMAN_REQUIRED, State.TIMEOUT}:
            return current_state not in TERMINAL_STATES
        
        # Check strict normal progression
        allowed = VALID_NORMAL_TRANSITIONS.get(current_state, [])
        return new_state in allowed

    def transition_to(self, run_id: str, new_state: str, details: Optional[str] = None) -> None:
        if new_state not in (State.FAIL, State.HUMAN_REQUIRED, State.TIMEOUT):
            from core.process_runner import check_stop
            check_stop()   # a stopped run may still end, but may not advance or PASS
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} does not exist.")
        
        current_state = run["current_state"]
        if not self.can_transition(current_state, new_state):
            raise InvalidStateTransitionError(
                f"Invalid transition for {run_id}: {current_state} -> {new_state}"
            )
        try:   # AI calls made next by this thread belong to this run and stage (work ledger)
            from core import usage
            usage.set_context(run_id=run_id, stage=new_state)
        except Exception:
            pass

        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE runs
                SET current_state = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (new_state, now, run_id)
            )
            conn.execute(
                """
                INSERT INTO state_transitions (run_id, from_state, to_state, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, current_state, new_state, details, now)
            )
            # dashboard bookkeeping: a HUMAN_REQUIRED entry becomes a (still unclassified) pending action;
            # any other transition resolves whatever was pending for this run
            if new_state == State.HUMAN_REQUIRED:
                conn.execute(
                    """
                    INSERT INTO human_action_state (run_id, stage, actionable, resolved, needs_classify, reason, updated_at)
                    VALUES (?, NULL, 0, 0, 1, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET actionable=0, resolved=0, needs_classify=1, reason=excluded.reason, updated_at=excluded.updated_at
                    """, (run_id, details, now))
            else:
                conn.execute("UPDATE human_action_state SET resolved=1, actionable=0, updated_at=? WHERE run_id=?", (now, run_id))
            conn.commit()

    def reopen_after_human_approval(self, run_id: str, details: str, target: str = None) -> bool:
        """The ONLY way out of HUMAN_REQUIRED: an explicit human approval reopens the run at ORDER_READY so it
        continues through the normal resume pipeline (worktree, baseline, builder, scope guard, reviewer, final gate).
        Atomic conditional UPDATE: exactly one caller wins even if Approve is clicked twice or from two processes."""
        target = target or State.ORDER_READY
        if target not in (State.ORDER_READY, State.VERIFYING):
            raise ValueError(f"human approval may only reopen at ORDER_READY (entry) or VERIFYING (post-build), not {target}")
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.isolation_level = None
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE runs SET current_state = ?, updated_at = ? WHERE run_id = ? AND current_state = ?",
                (target, now, run_id, State.HUMAN_REQUIRED))
            won = cur.rowcount == 1
            if won:
                conn.execute(
                    "INSERT INTO state_transitions (run_id, from_state, to_state, details, timestamp) VALUES (?, ?, ?, ?, ?)",
                    (run_id, State.HUMAN_REQUIRED, target, details, now))
                conn.execute("UPDATE human_action_state SET resolved=1, actionable=0, updated_at=? WHERE run_id=?", (now, run_id))
            conn.execute("COMMIT")
        return won

    def log_audit(self, run_id: str, agent: str, action: str, payload: Optional[str] = None) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO audit_logs (run_id, agent, action, payload, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, agent, action, payload, now)
            )
            conn.commit()

    def reopen_transport_failure(self, run_id: str) -> bool:
        """Explicit retry of a recorded CLI transport failure; never bypass a review/scope gate."""
        with self._get_conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            run = conn.execute('SELECT * FROM runs WHERE run_id=?', (run_id,)).fetchone()
            last = conn.execute('SELECT details FROM state_transitions WHERE run_id=? ORDER BY id DESC LIMIT 1', (run_id,)).fetchone()
            reason = (last['details'] or '') if last else ''
            retryable = any(code in reason for code in (
                'GOOGLE_STREAM_EMPTY_RESPONSE', 'GOOGLE_STREAM_RESULT_MISSING',
                'GOOGLE_STREAM_FAILED: Your previous response was cut off',
                'PROVIDER_FAILURE: Manually selected CLI unavailable:', 'MANUAL_ROLE_UNAVAILABLE:'))
            if not run or run['current_state'] not in {State.FAIL, State.HUMAN_REQUIRED, State.TIMEOUT} or not retryable:
                return False
            target = State.BUILDING if run['worktree_path'] else State.IDLE
            now = datetime.utcnow().isoformat()
            conn.execute('UPDATE runs SET current_state=?, updated_at=? WHERE run_id=?', (target, now, run_id))
            conn.execute('INSERT INTO state_transitions(run_id,from_state,to_state,details,timestamp) VALUES(?,?,?,?,?)',
                         (run_id, run['current_state'], target, 'Explicit retry of CLI failure: ' + reason, now))
            conn.execute('UPDATE human_action_state SET resolved=1, actionable=0 WHERE run_id=?', (run_id,))
            return True

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            row = cur.fetchone()
            if row:
                return dict(row)
            return None

    # ---- persisted dashboard state (read by /status without scanning runs or spawning subprocesses) ----
    def set_human_action(self, run_id: str, stage: Optional[str], actionable: bool, reason: str, items: list, blocking: list, resolved: bool = False) -> None:
        import json as _json
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO human_action_state (run_id, stage, actionable, resolved, needs_classify, reason, items_json, blocking_json, updated_at)
                VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET stage=excluded.stage, actionable=excluded.actionable, resolved=excluded.resolved,
                    needs_classify=0, reason=excluded.reason, items_json=excluded.items_json, blocking_json=excluded.blocking_json, updated_at=excluded.updated_at
                """, (run_id, stage, 1 if actionable else 0, 1 if resolved else 0, reason, _json.dumps(items, ensure_ascii=False), _json.dumps(blocking, ensure_ascii=False), now))
            conn.commit()

    def mark_human_action_resolved(self, run_id: str) -> None:
        with self._get_conn() as conn:
            conn.execute("UPDATE human_action_state SET resolved=1, actionable=0, updated_at=? WHERE run_id=?", (datetime.utcnow().isoformat(), run_id))
            conn.commit()

    def stale_human_actions(self, limit: int = 50) -> List[str]:
        """HUMAN_REQUIRED runs whose pending-action classification has not been persisted yet (incl. legacy rows)."""
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id FROM runs r LEFT JOIN human_action_state h ON h.run_id = r.run_id
                WHERE r.current_state = 'HUMAN_REQUIRED' AND (h.run_id IS NULL OR h.needs_classify = 1)
                ORDER BY r.created_at DESC LIMIT ?
                """, (limit,)).fetchall()
            return [r["run_id"] for r in rows]

    def list_pending_actions(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.project_path, r.created_at, h.stage, h.reason, h.items_json
                FROM human_action_state h JOIN runs r ON r.run_id = h.run_id
                WHERE h.actionable = 1 AND h.resolved = 0 AND h.needs_classify = 0 AND r.current_state = 'HUMAN_REQUIRED'
                ORDER BY r.created_at DESC
                """).fetchall()
            return [dict(r) for r in rows]

    def count_unresolved_human_required(self) -> Dict[str, int]:
        with self._get_conn() as conn:
            a = conn.execute("SELECT COUNT(*) FROM human_action_state h JOIN runs r ON r.run_id=h.run_id WHERE h.actionable=1 AND h.resolved=0 AND h.needs_classify=0 AND r.current_state='HUMAN_REQUIRED'").fetchone()[0]
            return {"actionable": a}

    def upsert_result_state(self, run_id: str, **fields) -> None:
        import json as _json
        allowed = {"state", "changed_count", "counts_json", "tests_state", "reviewer_state", "safety_state", "worktree_state", "detail", "request_id"}
        data = {k: (_json.dumps(v, ensure_ascii=False) if k == "counts_json" and not isinstance(v, str) else v) for k, v in fields.items() if k in allowed}
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            existing = conn.execute("SELECT run_id FROM result_state WHERE run_id=?", (run_id,)).fetchone()
            if existing:
                sets = ", ".join(f"{k}=?" for k in data) + ", updated_at=?"
                conn.execute(f"UPDATE result_state SET {sets} WHERE run_id=?", list(data.values()) + [now, run_id])
            else:
                data.setdefault("state", "READY")
                cols = ", ".join(list(data.keys()) + ["run_id", "updated_at"])
                qs = ", ".join("?" for _ in range(len(data) + 2))
                conn.execute(f"INSERT INTO result_state ({cols}) VALUES ({qs})", list(data.values()) + [run_id, now])
            conn.commit()

    def get_result_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM result_state WHERE run_id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_pending_results(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.project_path, r.created_at, s.state, s.changed_count, s.tests_state, s.reviewer_state, s.safety_state, s.worktree_state
                FROM result_state s JOIN runs r ON r.run_id = s.run_id
                WHERE s.state = 'READY' AND r.current_state = 'PASS' AND s.worktree_state = 'PRESENT'
                ORDER BY r.created_at DESC
                """).fetchall()
            return [dict(r) for r in rows]

    def pass_runs_without_result_state(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT r.run_id, r.project_path, r.worktree_path FROM runs r LEFT JOIN result_state s ON s.run_id = r.run_id
                WHERE r.current_state = 'PASS' AND s.run_id IS NULL ORDER BY r.created_at DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_audit_logs(self, run_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM audit_logs WHERE run_id = ? ORDER BY id ASC", (run_id,))
            return [dict(r) for r in cur.fetchall()]

    def get_transitions(self, run_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM state_transitions WHERE run_id = ? ORDER BY id ASC",
                (run_id,)
            )
            return [dict(row) for row in cur.fetchall()]

    def list_runs(self) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM runs ORDER BY created_at DESC")
            return [dict(row) for row in cur.fetchall()]
