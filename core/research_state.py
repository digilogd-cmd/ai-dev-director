"""
V3 TRIAD RESEARCH MODE - Research Session / Research Cycle state model.

This is a deliberately SEPARATE state machine from core.state_machine.StateMachine (NORMAL mode's `runs`
table). It owns its own tables in the same SQLite file (data/director.db, or TEST_DB_PATH under
DIRECTOR_TESTING=1 - identical isolation mechanism as core/db_path.py already provides). It never reads or
writes `runs`, `state_transitions`, `human_action_state`, `result_state`, or `provider_health.current_role`.

A Research Session is the top-level unit (one user Moonshot). A Research Cycle is one iteration of
independent research -> cross critique -> synthesis -> experiment -> result interpretation within a
session. NORMAL-mode `run`s are created later, per cycle, only at the EXPERIMENT_BUILDING stage - by
Orchestrator, reusing the existing safety pipeline unmodified (see docs/V3_TRIAD_RESEARCH_PLAN.md).

Principle mirrored from core/state_machine.py: only a ResearchOrchestrator (added in a later milestone)
changes research state; this class is the mechanism, not a policy decision-maker.
"""
import sqlite3
from core.db_path import connect_db
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.db_path import get_db_path


class ResearchState:
    """Research Cycle stages (PHASE 25 of the V3 plan, adopted verbatim)."""
    RESEARCH_INTAKE = "RESEARCH_INTAKE"
    INDEPENDENT_RESEARCH = "INDEPENDENT_RESEARCH"
    CROSS_CRITIQUE = "CROSS_CRITIQUE"
    SYNTHESIS = "SYNTHESIS"
    EXPERIMENT_PLANNING = "EXPERIMENT_PLANNING"
    EXPERIMENT_BUILDING = "EXPERIMENT_BUILDING"
    EXPERIMENT_VERIFYING = "EXPERIMENT_VERIFYING"
    EXPERIMENT_REVIEW = "EXPERIMENT_REVIEW"
    RESULT_INTERPRETATION = "RESULT_INTERPRETATION"
    NEXT_CYCLE = "NEXT_CYCLE"
    MILESTONE_COMPLETE = "MILESTONE_COMPLETE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    RESEARCH_STOPPED = "RESEARCH_STOPPED"


# Strict forward-only happy path for one cycle. Deliberately NOT imported from core.state_machine - a
# future change to NORMAL mode's transition graph must never silently affect this one.
VALID_RESEARCH_TRANSITIONS: Dict[str, List[str]] = {
    ResearchState.RESEARCH_INTAKE: [ResearchState.INDEPENDENT_RESEARCH],
    ResearchState.INDEPENDENT_RESEARCH: [ResearchState.CROSS_CRITIQUE],
    ResearchState.CROSS_CRITIQUE: [ResearchState.SYNTHESIS],
    ResearchState.SYNTHESIS: [ResearchState.EXPERIMENT_PLANNING],
    ResearchState.EXPERIMENT_PLANNING: [ResearchState.EXPERIMENT_BUILDING],
    ResearchState.EXPERIMENT_BUILDING: [ResearchState.EXPERIMENT_VERIFYING],
    ResearchState.EXPERIMENT_VERIFYING: [ResearchState.EXPERIMENT_REVIEW],
    ResearchState.EXPERIMENT_REVIEW: [ResearchState.RESULT_INTERPRETATION],
    ResearchState.RESULT_INTERPRETATION: [ResearchState.NEXT_CYCLE, ResearchState.MILESTONE_COMPLETE],
    ResearchState.NEXT_CYCLE: [ResearchState.RESEARCH_INTAKE],
}

TERMINAL_CYCLE_STAGES = {ResearchState.MILESTONE_COMPLETE, ResearchState.HUMAN_REQUIRED, ResearchState.RESEARCH_STOPPED}

SESSION_STATUS_ACTIVE = "ACTIVE"
SESSION_STATUS_MILESTONE_COMPLETE = "MILESTONE_COMPLETE"
SESSION_STATUS_STOPPED = "RESEARCH_STOPPED"
SESSION_STATUS_HUMAN_REQUIRED = "HUMAN_REQUIRED"

PARTICIPANT_ROLE_RESEARCHER = "RESEARCHER"
PARTICIPANT_STATUS_PENDING = "PENDING"
PARTICIPANT_STATUS_ACTIVE = "ACTIVE"
PARTICIPANT_STATUS_COMPLETE = "COMPLETE"
PARTICIPANT_STATUS_UNAVAILABLE = "UNAVAILABLE"

DEFAULT_CYCLE_BUDGET = 8   # bounded autonomy default (PHASE 17); overridable per session at creation
RESEARCH_MODE_TRIAD = "TRIAD"
INTERPRETATION_INDEPENDENT_STAGE = "RESULT_INTERPRETATION_INDEPENDENT"
INTERPRETATION_CROSS_STAGE = "RESULT_INTERPRETATION_CROSS"
RESEARCH_MEMORY_STAGE = "RESEARCH_MEMORY"


class InvalidResearchTransitionError(Exception):
    pass


class ResearchStateMachine:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = Path(db_path) if db_path else get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = connect_db(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_path TEXT NOT NULL,
                    moonshot TEXT NOT NULL,
                    project_type TEXT NOT NULL DEFAULT 'UNKNOWN',
                    status TEXT NOT NULL,
                    cycle_budget INTEGER NOT NULL,
                    cycles_completed INTEGER NOT NULL DEFAULT 0,
                    stopped_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            session_columns = {r[1] for r in cur.execute("PRAGMA table_info(research_sessions)").fetchall()}
            session_migrations = (
                ("mode", "TEXT NOT NULL DEFAULT 'TRIAD'"),
                ("readiness_state", "TEXT"),
                ("degraded", "INTEGER NOT NULL DEFAULT 0"),
                ("degraded_approved", "INTEGER NOT NULL DEFAULT 0"),
                ("missing_domains", "TEXT NOT NULL DEFAULT '[]'"),
                ("current_cycle", "INTEGER NOT NULL DEFAULT 0"),
                ("current_stage", "TEXT NOT NULL DEFAULT 'RESEARCH_INTAKE'"),
                ("intake_evidence", "TEXT"),
            )
            for column, ddl in session_migrations:
                if column not in session_columns:
                    cur.execute(f"ALTER TABLE research_sessions ADD COLUMN {column} {ddl}")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_cycles (
                    cycle_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    cycle_number INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(session_id, cycle_number),
                    FOREIGN KEY(session_id) REFERENCES research_sessions(session_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_cycle_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    from_stage TEXT NOT NULL,
                    to_stage TEXT NOT NULL,
                    details TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_participants (
                    cycle_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, domain)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_prompt_artifacts (
                    cycle_id TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    prompt_chars INTEGER NOT NULL,
                    prompt_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, domain, stage),
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_stage_outputs (
                    cycle_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    output_chars INTEGER NOT NULL,
                    output_bytes INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (cycle_id, stage, domain),
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_call_provenance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    cycle_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    research_role TEXT NOT NULL,
                    executable TEXT,
                    version TEXT,
                    invocation TEXT,
                    started_at TEXT,
                    ended_at TEXT,
                    duration_sec REAL,
                    exit_code INTEGER,
                    prompt_artifact TEXT,
                    prompt_sha256 TEXT,
                    output_artifact TEXT,
                    output_sha256 TEXT,
                    prompt_chars INTEGER,
                    prompt_bytes INTEGER,
                    response_chars INTEGER,
                    response_bytes INTEGER,
                    token_usage TEXT NOT NULL DEFAULT 'UNKNOWN',
                    failover INTEGER NOT NULL DEFAULT 0,
                    authentication_mode TEXT NOT NULL DEFAULT 'subscription',
                    api_key_used INTEGER NOT NULL DEFAULT 0,
                    observed_fields TEXT NOT NULL DEFAULT '[]',
                    declared_fields TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT NOT NULL,
                    error TEXT,
                    attempt_number INTEGER NOT NULL DEFAULT 1,
                    retry_reason TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            provenance_columns = {r[1] for r in cur.execute("PRAGMA table_info(research_call_provenance)").fetchall()}
            for column, ddl in (("attempt_number", "INTEGER NOT NULL DEFAULT 1"), ("retry_reason", "TEXT")):
                if column not in provenance_columns:
                    cur.execute(f"ALTER TABLE research_call_provenance ADD COLUMN {column} {ddl}")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_project_evidence (
                    session_id TEXT PRIMARY KEY,
                    project_type TEXT NOT NULL,
                    baseline_state TEXT NOT NULL,
                    artifact_path TEXT,
                    evidence_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES research_sessions(session_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_baseline_evidence (
                    cycle_id TEXT PRIMARY KEY,
                    baseline_state TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    evidence_sha256 TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_experiments (
                    cycle_id TEXT PRIMARY KEY,
                    normal_run_id TEXT,
                    order_artifact TEXT NOT NULL,
                    order_sha256 TEXT NOT NULL,
                    order_json TEXT NOT NULL,
                    project_type TEXT NOT NULL,
                    baseline_state TEXT NOT NULL,
                    status TEXT NOT NULL,
                    normal_final_state TEXT,
                    worktree_path TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    cycle_id TEXT NOT NULL UNIQUE,
                    hypothesis_sha256 TEXT NOT NULL,
                    hypothesis TEXT NOT NULL,
                    result TEXT NOT NULL,
                    lesson TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    source_evidence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(session_id, hypothesis_sha256),
                    FOREIGN KEY(cycle_id) REFERENCES research_cycles(cycle_id)
                )
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_sessions_status ON research_sessions(status)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_cycles_session ON research_cycles(session_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_cycle_transitions_cycle ON research_cycle_transitions(cycle_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_prompt_artifacts_cycle ON research_prompt_artifacts(cycle_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_stage_outputs_cycle ON research_stage_outputs(cycle_id, stage)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_call_provenance_cycle ON research_call_provenance(cycle_id, stage)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_project_evidence_type ON research_project_evidence(project_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_research_memory_session ON research_memory(session_id, created_at)")
            conn.commit()

    # ---------------------------------------------------------------- sessions

    def create_session(self, session_id: str, project_path: str, moonshot: str,
                        project_type: str = "UNKNOWN", cycle_budget: int = DEFAULT_CYCLE_BUDGET) -> None:
        if not session_id or not str(session_id).strip():
            raise ValueError("session_id is required")
        if not project_path or not str(project_path).strip():
            raise ValueError("project_path is required")
        if not moonshot or not str(moonshot).strip():
            raise ValueError("moonshot is required")
        if not isinstance(cycle_budget, int) or isinstance(cycle_budget, bool) or cycle_budget < 1:
            raise ValueError("cycle_budget must be a positive integer")
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO research_sessions (session_id, project_path, moonshot, project_type, status, "
                "cycle_budget, cycles_completed, stopped_reason, created_at, updated_at, mode, current_cycle, current_stage) "
                "VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, 0, ?)",
                (session_id, project_path, moonshot, project_type, SESSION_STATUS_ACTIVE, cycle_budget, now, now,
                 RESEARCH_MODE_TRIAD, ResearchState.RESEARCH_INTAKE),
            )
            conn.commit()

    def record_intake(self, session_id: str, readiness: Dict[str, Any], degraded_approved: bool = False) -> None:
        """Persist the exact readiness snapshot and authorization decision used by the intake gate."""
        missing = list(readiness.get("missing") or [])
        degraded = readiness.get("state") == "TRIAD_DEGRADED"
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            updated = conn.execute(
                "UPDATE research_sessions SET readiness_state=?, degraded=?, degraded_approved=?, missing_domains=?, "
                "intake_evidence=?, updated_at=? WHERE session_id=?",
                (readiness.get("state"), 1 if degraded else 0, 1 if degraded_approved else 0,
                 json.dumps(missing), json.dumps(readiness, sort_keys=True), now, session_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"unknown research session: {session_id}")
            conn.commit()

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM research_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, project_path: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if project_path:
                rows = conn.execute(
                    "SELECT * FROM research_sessions WHERE project_path = ? ORDER BY created_at DESC",
                    (project_path,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM research_sessions ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def set_session_status(self, session_id: str, status: str, stopped_reason: Optional[str] = None) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE research_sessions SET status = ?, stopped_reason = ?, updated_at = ? WHERE session_id = ?",
                (status, stopped_reason, now, session_id),
            )
            conn.commit()

    def increment_cycles_completed(self, session_id: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE research_sessions SET cycles_completed = cycles_completed + 1, updated_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            conn.commit()

    def cycle_budget_exhausted(self, session_id: str) -> bool:
        """Bounded autonomy check (PHASE 17): cycles_completed >= cycle_budget."""
        s = self.get_session(session_id)
        if not s:
            return False
        return int(s["cycles_completed"]) >= int(s["cycle_budget"])

    def record_project_evidence(self, session_id: str, evidence: Dict[str, Any],
                                artifact_path: Optional[str] = None) -> None:
        """Persist the exact project classification evidence and synchronize the session label."""
        project_type = str(evidence.get("project_type") or "UNKNOWN")
        baseline_state = str(evidence.get("baseline_state") or "UNKNOWN")
        evidence_sha256 = str(evidence.get("evidence_sha256") or "")
        if not evidence_sha256:
            raise ValueError("project evidence hash is required")
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            if conn.execute("SELECT 1 FROM research_sessions WHERE session_id=?", (session_id,)).fetchone() is None:
                raise ValueError(f"unknown research session: {session_id}")
            conn.execute(
                "INSERT INTO research_project_evidence "
                "(session_id, project_type, baseline_state, artifact_path, evidence_sha256, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET project_type=excluded.project_type, "
                "baseline_state=excluded.baseline_state, artifact_path=excluded.artifact_path, "
                "evidence_sha256=excluded.evidence_sha256, evidence_json=excluded.evidence_json, "
                "created_at=excluded.created_at",
                (session_id, project_type, baseline_state, artifact_path, evidence_sha256,
                 json.dumps(evidence, ensure_ascii=False, sort_keys=True), now),
            )
            conn.execute(
                "UPDATE research_sessions SET project_type=?, updated_at=? WHERE session_id=?",
                (project_type, now, session_id),
            )
            conn.commit()

    def get_project_evidence(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_project_evidence WHERE session_id=?", (session_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def record_baseline_evidence(self, cycle_id: str, baseline: Dict[str, Any],
                                 artifact_path: str, evidence_sha256: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            if conn.execute("SELECT 1 FROM research_cycles WHERE cycle_id=?", (cycle_id,)).fetchone() is None:
                raise ValueError(f"unknown research cycle: {cycle_id}")
            conn.execute(
                "INSERT INTO research_baseline_evidence "
                "(cycle_id, baseline_state, artifact_path, evidence_sha256, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cycle_id) DO UPDATE SET baseline_state=excluded.baseline_state, "
                "artifact_path=excluded.artifact_path, evidence_sha256=excluded.evidence_sha256, "
                "evidence_json=excluded.evidence_json, created_at=excluded.created_at",
                (cycle_id, str(baseline.get("baseline_state") or "UNKNOWN"), artifact_path, evidence_sha256,
                 json.dumps(baseline, ensure_ascii=False, sort_keys=True), now),
            )
            conn.commit()

    def get_baseline_evidence(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM research_baseline_evidence WHERE cycle_id=?", (cycle_id,)
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        return item

    def record_experiment_plan(self, cycle_id: str, order: Dict[str, Any], order_artifact: str,
                               order_sha256: str, project_type: str, baseline_state: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            if conn.execute("SELECT 1 FROM research_cycles WHERE cycle_id=?", (cycle_id,)).fetchone() is None:
                raise ValueError(f"unknown research cycle: {cycle_id}")
            conn.execute(
                "INSERT INTO research_experiments "
                "(cycle_id, normal_run_id, order_artifact, order_sha256, order_json, project_type, "
                "baseline_state, status, normal_final_state, worktree_path, result_json, created_at, updated_at) "
                "VALUES (?, NULL, ?, ?, ?, ?, ?, 'PLANNED', NULL, NULL, NULL, ?, ?) "
                "ON CONFLICT(cycle_id) DO UPDATE SET order_artifact=excluded.order_artifact, "
                "order_sha256=excluded.order_sha256, order_json=excluded.order_json, "
                "project_type=excluded.project_type, baseline_state=excluded.baseline_state, "
                "status='PLANNED', normal_final_state=NULL, worktree_path=NULL, result_json=NULL, "
                "updated_at=excluded.updated_at",
                (cycle_id, order_artifact, order_sha256,
                 json.dumps(order, ensure_ascii=False, sort_keys=True), project_type, baseline_state, now, now),
            )
            conn.commit()

    def record_experiment_result(self, cycle_id: str, normal_run_id: Optional[str], status: str,
                                 normal_final_state: Optional[str], worktree_path: Optional[str],
                                 result: Dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            updated = conn.execute(
                "UPDATE research_experiments SET normal_run_id=?, status=?, normal_final_state=?, "
                "worktree_path=?, result_json=?, updated_at=? WHERE cycle_id=?",
                (normal_run_id, status, normal_final_state, worktree_path,
                 json.dumps(result, ensure_ascii=False, sort_keys=True), now, cycle_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"experiment plan missing for cycle: {cycle_id}")
            conn.commit()

    def get_experiment(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM research_experiments WHERE cycle_id=?", (cycle_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["order"] = json.loads(item.pop("order_json"))
        result_json = item.pop("result_json")
        item["result"] = json.loads(result_json) if result_json else None
        return item

    def has_memory_hypothesis(self, session_id: str, hypothesis_sha256: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM research_memory WHERE session_id=? AND hypothesis_sha256=?",
                (session_id, hypothesis_sha256),
            ).fetchone()
        return row is not None

    def record_research_memory(self, session_id: str, cycle_id: str, hypothesis_sha256: str,
                               payload: Dict[str, Any], source_evidence: Dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO research_memory "
                "(session_id, cycle_id, hypothesis_sha256, hypothesis, result, lesson, payload_json, "
                "source_evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, cycle_id, hypothesis_sha256, str(payload.get("hypothesis") or ""),
                 str(payload.get("result") or "inconclusive"), str(payload.get("lesson") or ""),
                 json.dumps(payload, ensure_ascii=False, sort_keys=True),
                 json.dumps(source_evidence, ensure_ascii=False, sort_keys=True), now),
            )
            conn.commit()

    def list_research_memory(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_memory WHERE session_id=? ORDER BY id", (session_id,)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["source_evidence"] = json.loads(item.pop("source_evidence_json"))
            result.append(item)
        return result

    # ---------------------------------------------------------------- cycles

    def can_transition(self, current_stage: str, new_stage: str) -> bool:
        if current_stage not in TERMINAL_CYCLE_STAGES and new_stage in (ResearchState.HUMAN_REQUIRED, ResearchState.RESEARCH_STOPPED):
            return True   # terminal-escape rule, mirrors core.state_machine's rule for NORMAL mode (independent copy)
        return new_stage in VALID_RESEARCH_TRANSITIONS.get(current_stage, [])

    def create_cycle(self, cycle_id: str, session_id: str, cycle_number: int) -> None:
        session = self.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        if not isinstance(cycle_number, int) or isinstance(cycle_number, bool) or cycle_number < 1:
            raise ValueError("cycle_number must be a positive integer")
        if cycle_number > int(session["cycle_budget"]):
            raise ValueError("cycle budget exceeded")
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO research_cycles (cycle_id, session_id, cycle_number, stage, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (cycle_id, session_id, cycle_number, ResearchState.RESEARCH_INTAKE, now, now),
            )
            conn.execute(
                "INSERT INTO research_cycle_transitions (cycle_id, from_stage, to_stage, details, timestamp) "
                "VALUES (?, 'NONE', ?, ?, ?)",
                (cycle_id, ResearchState.RESEARCH_INTAKE, "cycle created", now),
            )
            conn.execute(
                "UPDATE research_sessions SET current_cycle=?, current_stage=?, updated_at=? WHERE session_id=?",
                (cycle_number, ResearchState.RESEARCH_INTAKE, now, session_id),
            )
            conn.commit()

    def get_cycle(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM research_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
        return dict(row) if row else None

    def list_cycles(self, session_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_cycles WHERE session_id = ? ORDER BY cycle_number ASC", (session_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def transition_cycle(self, cycle_id: str, new_stage: str, details: Optional[str] = None) -> None:
        """The only cycle-stage-mutating method (mirrors StateMachine.transition_to's role for NORMAL mode)."""
        cycle = self.get_cycle(cycle_id)
        if not cycle:
            raise InvalidResearchTransitionError(f"unknown cycle_id: {cycle_id}")
        current = cycle["stage"]
        if not self.can_transition(current, new_stage):
            raise InvalidResearchTransitionError(f"cannot transition cycle {cycle_id} from {current} to {new_stage}")
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute("UPDATE research_cycles SET stage = ?, updated_at = ? WHERE cycle_id = ?", (new_stage, now, cycle_id))
            conn.execute(
                "INSERT INTO research_cycle_transitions (cycle_id, from_stage, to_stage, details, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (cycle_id, current, new_stage, details or "", now),
            )
            conn.execute(
                "UPDATE research_sessions SET current_cycle=?, current_stage=?, updated_at=? WHERE session_id=?",
                (cycle["cycle_number"], new_stage, now, cycle["session_id"]),
            )
            conn.commit()
        if new_stage in (ResearchState.MILESTONE_COMPLETE, ResearchState.RESEARCH_STOPPED, ResearchState.HUMAN_REQUIRED):
            # A completed cycle may already have been counted when it entered NEXT_CYCLE.  A later explicit
            # user stop must not count that same cycle twice.
            session = self.get_session(cycle["session_id"]) or {}
            if int(session.get("cycles_completed", 0)) < int(cycle["cycle_number"]):
                self.increment_cycles_completed(cycle["session_id"])

    def get_cycle_transitions(self, cycle_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_cycle_transitions WHERE cycle_id = ? ORDER BY id ASC", (cycle_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- participants (TRIAD's own role ledger)

    def register_participants(self, cycle_id: str, statuses: Dict[str, str]) -> None:
        """statuses: {"OPENAI": "ACTIVE"|"UNAVAILABLE", "ANTHROPIC": ..., "GOOGLE": ...}. Never touches
        provider_health.current_role - that column belongs to NORMAL mode's ProviderManager.resolve_roles()."""
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            for domain, status in statuses.items():
                conn.execute(
                    "INSERT INTO research_participants (cycle_id, domain, role, status, updated_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(cycle_id, domain) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                    (cycle_id, domain, PARTICIPANT_ROLE_RESEARCHER, status, now),
                )
            conn.commit()

    def set_participant_status(self, cycle_id: str, domain: str, status: str) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "UPDATE research_participants SET status = ?, updated_at = ? WHERE cycle_id = ? AND domain = ?",
                (status, now, cycle_id, domain),
            )
            conn.commit()

    def get_participants(self, cycle_id: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_participants WHERE cycle_id = ? ORDER BY domain ASC", (cycle_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- independent prompt artifacts

    def record_prompt_artifact(self, cycle_id: str, domain: str, stage: str, artifact_path: str,
                               prompt_sha256: str, prompt_chars: int, prompt_bytes: int) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO research_prompt_artifacts "
                "(cycle_id, domain, stage, artifact_path, prompt_sha256, prompt_chars, prompt_bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cycle_id, domain, stage) DO UPDATE SET artifact_path=excluded.artifact_path, "
                "prompt_sha256=excluded.prompt_sha256, prompt_chars=excluded.prompt_chars, "
                "prompt_bytes=excluded.prompt_bytes, created_at=excluded.created_at",
                (cycle_id, domain, stage, artifact_path, prompt_sha256, prompt_chars, prompt_bytes, now),
            )
            conn.commit()

    def get_prompt_artifacts(self, cycle_id: str, stage: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if stage:
                rows = conn.execute(
                    "SELECT * FROM research_prompt_artifacts WHERE cycle_id=? AND stage=? ORDER BY domain",
                    (cycle_id, stage),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM research_prompt_artifacts WHERE cycle_id=? ORDER BY stage, domain", (cycle_id,)
                ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- C: stage outputs and call provenance

    def record_stage_output(self, cycle_id: str, stage: str, domain: str, artifact_path: str,
                            output_sha256: str, output_chars: int, output_bytes: int,
                            payload: Dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO research_stage_outputs "
                "(cycle_id, stage, domain, artifact_path, output_sha256, output_chars, output_bytes, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cycle_id, stage, domain) DO UPDATE SET artifact_path=excluded.artifact_path, "
                "output_sha256=excluded.output_sha256, output_chars=excluded.output_chars, "
                "output_bytes=excluded.output_bytes, payload_json=excluded.payload_json, created_at=excluded.created_at",
                (cycle_id, stage, domain, artifact_path, output_sha256, output_chars, output_bytes,
                 json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
            )
            conn.commit()

    def get_stage_outputs(self, cycle_id: str, stage: str) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM research_stage_outputs WHERE cycle_id=? AND stage=? ORDER BY domain",
                (cycle_id, stage),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def record_call_provenance(self, record: Dict[str, Any]) -> None:
        now = datetime.utcnow().isoformat()
        columns = (
            "session_id", "cycle_id", "stage", "domain", "research_role", "executable", "version",
            "invocation", "started_at", "ended_at", "duration_sec", "exit_code", "prompt_artifact",
            "prompt_sha256", "output_artifact", "output_sha256", "prompt_chars", "prompt_bytes",
            "response_chars", "response_bytes", "token_usage", "failover", "authentication_mode",
            "api_key_used", "observed_fields", "declared_fields", "outcome", "error",
            "attempt_number", "retry_reason", "created_at",
        )
        values = []
        for column in columns:
            if column == "created_at":
                values.append(record.get(column) or now)
            elif column in ("observed_fields", "declared_fields"):
                values.append(json.dumps(record.get(column) or []))
            elif column in ("failover", "api_key_used"):
                values.append(1 if record.get(column) else 0)
            elif column == "token_usage":
                values.append(record.get(column) or "UNKNOWN")
            elif column == "authentication_mode":
                values.append(record.get(column) or "subscription")
            elif column == "attempt_number":
                values.append(int(record.get(column) or 1))
            else:
                values.append(record.get(column))
        placeholders = ",".join("?" for _ in columns)
        with self._get_conn() as conn:
            conn.execute(
                f"INSERT INTO research_call_provenance ({','.join(columns)}) VALUES ({placeholders})", values
            )
            conn.commit()

    def get_call_provenance(self, cycle_id: str, stage: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            if stage:
                rows = conn.execute(
                    "SELECT * FROM research_call_provenance WHERE cycle_id=? AND stage=? ORDER BY id",
                    (cycle_id, stage),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM research_call_provenance WHERE cycle_id=? ORDER BY id", (cycle_id,)
                ).fetchall()
        return [dict(r) for r in rows]
