import sqlite3
from core.db_path import connect_db
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any

class ChatManager:
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
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    head_provider TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES chat_sessions(id)
                )
            ''')
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS chat_provenance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    runtime TEXT,
                    model TEXT,
                    executable TEXT,
                    invocation_command TEXT,
                    version TEXT,
                    exit_code INTEGER,
                    duration_sec REAL,
                    authentication TEXT,
                    api_key_used INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    created_at TEXT NOT NULL
                )
            ''')
            # v0.2.3.3 migration: declared vs observed provenance fields
            cols = [r[1] for r in cursor.execute("PRAGMA table_info(chat_provenance)").fetchall()]
            for col, ddl in (("authentication_mode", "TEXT"), ("api_key_policy", "TEXT"),
                             ("observed_fields", "TEXT"), ("declared_fields", "TEXT"), ("outcome", "TEXT")):
                if col not in cols:
                    cursor.execute(f"ALTER TABLE chat_provenance ADD COLUMN {col} {ddl}")
            conn.commit()

    def create_session(self, session_id: str, project_path: str, head_provider: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (id, project_path, head_provider, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, project_path, head_provider, now, now)
            )
            conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
            row = cur.fetchone()
            return dict(row) if row else None

    def add_message(self, session_id: str, role: str, content: str, run_id: Optional[str] = None) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_messages (session_id, role, content, run_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (session_id, role, content, run_id, now)
            )
            conn.execute(
                """
                UPDATE chat_sessions SET updated_at = ? WHERE id = ?
                """,
                (now, session_id)
            )
            conn.commit()

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
                (session_id,)
            )
            return [dict(row) for row in cur.fetchall()]

    def set_project(self, session_id: str, project_path: str, head_provider: str = "OPENAI") -> None:
        """Explicitly selects the target project for a chat session (creates the session if missing)."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            if conn.execute("SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)).fetchone():
                conn.execute("UPDATE chat_sessions SET project_path = ?, updated_at = ? WHERE id = ?",
                             (project_path, now, session_id))
            else:
                conn.execute(
                    "INSERT INTO chat_sessions (id, project_path, head_provider, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (session_id, project_path, head_provider, now, now)
                )
            conn.commit()

    def add_provenance(self, session_id: str, record: Dict[str, Any], run_id: Optional[str] = None,
                       outcome: Optional[str] = None) -> None:
        """Stores the execution provenance of a HEAD chat() call.
        Observed columns (executable/version/exit_code/duration/invocation) come from the real process;
        `authentication` stays NULL on purpose: auth mode is a DECLARED policy value (authentication_mode)."""
        import json as _json
        now = datetime.utcnow().isoformat()
        observed = record.get("observed_fields")
        if observed is None:
            observed = ["executable", "version", "exit_code", "duration_sec", "invocation_command"]
        declared = record.get("declared_fields")
        if declared is None:
            declared = ["authentication_mode", "api_key_policy", "api_key_used"]
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_provenance
                (session_id, agent, provider, runtime, model, executable, invocation_command, version,
                 exit_code, duration_sec, authentication, api_key_used, run_id, created_at,
                 authentication_mode, api_key_policy, observed_fields, declared_fields, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    record.get("agent", "HEAD"),
                    record.get("provider", "unknown"),
                    record.get("runtime"),
                    record.get("model"),
                    record.get("executable"),
                    record.get("invocation_command"),
                    record.get("version"),
                    record.get("exit_code"),
                    record.get("duration_sec"),
                    None,
                    1 if record.get("api_key_used") else 0,
                    run_id,
                    now,
                    record.get("authentication_mode", "subscription"),
                    record.get("api_key_policy", "disabled"),
                    _json.dumps(observed),
                    _json.dumps(declared),
                    outcome,
                )
            )
            conn.commit()

    def get_provenance(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM chat_provenance WHERE session_id = ? ORDER BY id ASC", (session_id,))
            return [dict(row) for row in cur.fetchall()]
