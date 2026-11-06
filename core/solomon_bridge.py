"""Durable local order intake. Never bypasses the existing Orchestrator safety gates."""
import hashlib
import json
import re
import sqlite3
from core.db_path import connect_db
import threading
from datetime import datetime
from pathlib import Path


class SolomonBridge:
    def __init__(self, dashboard, allowed_projects):
        self.d = dashboard
        self.allowed = {str(Path(p).resolve()).casefold() for p in allowed_projects}
        self.lock = threading.Lock()
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS solomon_orders (
                request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL, run_id TEXT, received_at TEXT NOT NULL,
                error TEXT)''')

    def connect(self):
        db = connect_db(self.d.resolve_db_path(), timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def list(self):
        with self.connect() as db:
            rows = db.execute('''SELECT s.request_id,s.run_id,s.received_at,s.error,
                r.goal,r.current_state,r.project_path,r.updated_at
                FROM solomon_orders s LEFT JOIN runs r ON r.run_id=s.run_id
                ORDER BY s.received_at DESC LIMIT 50''').fetchall()
        return [dict(r) for r in rows]

    def status(self, request_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM solomon_orders WHERE request_id=?', (request_id,)).fetchone()
        if not row:
            raise ValueError('알 수 없는 요청입니다')
        row = dict(row)
        run_id = row['run_id']
        run = self.d.StateMachine().get_run(run_id) if run_id else None
        return {'request_id': request_id, 'run_id': run_id, 'run': run,
                'error': row['error'], 'state': (run or {}).get('current_state', 'INTERRUPTED'),
                'result': self.d.result_info(run_id) if run and run['current_state'] == 'PASS' else None,
                'inspection': self.d.inspect_run(run_id) if run else None,
                'business_outcome': 'unmeasured', 'auto_applied': False}

    def submit(self, payload):
        from schemas.order import OrderSchema
        from core.safety import SafetyPolicy
        from core.state_machine import State
        ident = payload.get('request_id', '')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', ident):
            raise ValueError('안정적인 request_id가 필요합니다')
        ok, project = self.d.validate_project_path(payload.get('project_path', ''))
        if not ok:
            raise ValueError(project)
        if str(Path(project).resolve()).casefold() not in self.allowed:
            raise ValueError('솔로몬에 실행 허용된 프로젝트가 아닙니다')
        order = OrderSchema(**payload.get('order', {}))
        if not order.goal.strip() or not order.acceptance_criteria:
            raise ValueError('작업 목표와 인수 기준이 필요합니다')
        if not 1 <= order.max_files_changed <= 15 or not 1 <= order.max_diff_lines <= 800:
            raise ValueError('작업 한도를 초과했습니다')
        canonical = json.dumps({'project': project, 'order': order.model_dump()}, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        with self.lock:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                existing = db.execute('SELECT payload_hash,run_id FROM solomon_orders WHERE request_id=?', (ident,)).fetchone()
                if existing:
                    if existing['payload_hash'] != digest:
                        raise ValueError('동일 request_id에 다른 작업을 보낼 수 없습니다')
                    return {'request_id': ident, 'run_id': existing['run_id'], 'status': 'duplicate'}
                # Persist before dispatch. A crash here is visible and is NOT silently replayed.
                db.execute('INSERT INTO solomon_orders VALUES(?,?,?,?,?,NULL)',
                           (ident, digest, canonical, None, datetime.utcnow().isoformat()))
            orch = self.d._new_orchestrator()
            key = self.d._project_key(project)
            try:
                with self.d._RUN_LOCK:
                    if key in self.d._ACTIVE_PROJECTS or self.d._project_busy(orch.sm, project):
                        raise ValueError('이 프로젝트의 작업이 이미 진행 중입니다')
                    reasons = SafetyPolicy.assess_order_for_auto_run(order)
                    if reasons:
                        run_id = self.d._record_human_required(orch, project, order.goal,
                            'APPROVAL_REQUIRED: ' + '; '.join(reasons), order)
                    else:
                        run_id = orch.reserve_run_id(project, order.goal)
                        self.d._ACTIVE_PROJECTS[key] = run_id
                    with self.connect() as db:
                        db.execute('UPDATE solomon_orders SET run_id=? WHERE request_id=?', (run_id, ident))
                orch.sm.log_audit(run_id, 'ORCHESTRATOR', 'SOLOMON_INTAKE', ident)
                if not reasons:
                    def work():
                        try:
                            orch.run(project, order.goal, run_id=run_id, order=order)
                        except Exception as exc:
                            with self.connect() as db:
                                db.execute('UPDATE solomon_orders SET error=? WHERE request_id=?', (str(exc)[:1000], ident))
                            # Only orchestrator owns transitions; leave actual state and failure visible.
                        finally:
                            with self.d._RUN_LOCK:
                                self.d._ACTIVE_PROJECTS.pop(key, None)
                            self.d.invalidate_status_cache()
                    threading.Thread(target=work, name='solomon-' + ident, daemon=True).start()
                self.d.invalidate_status_cache()
                return {'request_id': ident, 'run_id': run_id,
                        'status': 'awaiting_approval' if reasons else 'submitted', 'auto_applied': False}
            except Exception as exc:
                with self.connect() as db:
                    db.execute('UPDATE solomon_orders SET error=? WHERE request_id=?', (str(exc)[:1000], ident))
                raise
