"""Goal-oriented missions (HEAD proxy autonomy).

One user request becomes a MISSION. HEAD turns it into milestone ORDERs; each ORDER runs through the normal
pipeline (worktree -> Builder -> Director tests -> independent Reviewer -> HEAD) and every new milestone starts
from a private snapshot of the latest PASSED milestone's worktree. After each milestone HEAD re-plans from the
real evidence until the goal is DONE, BLOCKED on a delegated exception (money / credentials / irreversible
external action), or the step budget is spent. The user's project is never modified; the deliverable is the
final worktree. Missions are persisted so a restart continues where it stopped.
"""
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.process_runner import set_stop_guard
from core import autopilot
from core.state_machine import State, TERMINAL_STATES
from schemas.order import OrderSchema

END_STATES = {"DONE", "BLOCKED", "INCOMPLETE", "FAILED"}


def _now() -> str:
    return datetime.utcnow().isoformat()


def default_mission_dir() -> Path:
    """Next to the DB, so test isolation (TEST_DB_PATH) also isolates missions."""
    from core.db_path import get_db_path
    return get_db_path().parent / "missions"


def _retry_while_locked(action: Callable[[], Any], attempts: int = 20, delay: float = 0.1) -> Any:
    """On Windows, a file another thread or process (console poll, antivirus) has open cannot be replaced and can
    refuse a read for a moment. That must not kill a mission."""
    for attempt in range(attempts):
        try:
            return action()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


class MissionStore:
    def __init__(self, base: Optional[Path] = None):
        self.base = Path(base or default_mission_dir())

    def _path(self, mission_id: str) -> Path:
        return self.base / f"{mission_id}.json"

    def save(self, mission: Dict[str, Any], force: bool = False) -> None:
        """A stop request written by the operator (status STOPPED on disk) survives every save from the mission
        thread, whatever status its in-memory copy holds. Only reopen() (force=True) may clear it."""
        if not force and mission.get("status") != "STOPPED":
            current = self.load(mission["mission_id"])
            if current and current.get("status") == "STOPPED":
                mission["status"] = "STOPPED"
                mission["blocked_reason"] = current.get("blocked_reason")
                mission["stopped_at"] = current.get("stopped_at")
        mission["updated_at"] = _now()
        self.base.mkdir(parents=True, exist_ok=True)
        tmp = self._path(mission["mission_id"]).with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")   # savers never share one
        tmp.write_text(json.dumps(mission, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        _retry_while_locked(lambda: os.replace(tmp, self._path(mission["mission_id"])))

    def load(self, mission_id: str) -> Optional[Dict[str, Any]]:
        p = self._path(mission_id)
        return json.loads(_retry_while_locked(lambda: p.read_text(encoding="utf-8"))) if p.exists() else None

    def all(self) -> List[Dict[str, Any]]:
        if not self.base.exists():
            return []
        out = []
        for p in sorted(self.base.glob("MSN-*.json")):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return out

    def by_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        for m in self.all():
            if any(s.get("run_id") == run_id for s in m.get("steps", [])):
                return m
        return None


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError):
        return str(a).lower() == str(b).lower()


def latest_deliverable(project_path: str, store: Optional["MissionStore"] = None) -> Optional[str]:
    """The newest existing worktree a previous mission on this project delivered (last PASSED milestone). Nothing is
    merged into the user's project automatically, so follow-up requests continue from here instead of from scratch."""
    store = store or MissionStore()
    found = [m for m in store.all() if m.get("base_worktree") and _same_path(m.get("project_path", ""), project_path)
             and m.get("status") != "RUNNING" and Path(m["base_worktree"]).is_dir()]
    found.sort(key=lambda m: m.get("updated_at", ""))
    return found[-1]["base_worktree"] if found else None


def deliverable_run_id(mission: Dict[str, Any]) -> Optional[str]:
    """The run whose worktree is the mission's deliverable (the last PASSED milestone)."""
    base = mission.get("base_worktree")
    for step in reversed(mission.get("steps", [])):
        res = step.get("result") or {}
        if res.get("final_state") == State.PASS and (not base or res.get("worktree_path") == base) and step.get("run_id"):
            return step["run_id"]
    return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return False


class MissionRunner:
    def __init__(self, orchestrator_factory: Callable[[], Any], store: Optional[MissionStore] = None,
                 notify: Optional[Callable[..., None]] = None):
        self.factory = orchestrator_factory
        self.store = store or MissionStore()
        self.notify = notify or (lambda text, run_id=None, role="SYSTEM": None)

    # ------------------------------------------------------------------ lifecycle
    def create(self, project_path: str, goal: str, first_order: OrderSchema, config: Optional[dict] = None,
               notes: Optional[List[str]] = None, base_worktree: Optional[str] = None, profile: Optional[str] = None,
               source: str = "chat", request_id: Optional[str] = None, auto_apply: Optional[str] = None) -> Dict[str, Any]:
        if base_worktree is None and autopilot.setting(config, "continue_from_last_deliverable", True):
            base_worktree = latest_deliverable(project_path, self.store)
        knobs = autopilot.profile_overrides(config, profile)
        mission = {
            "mission_id": "MSN-" + datetime.now().strftime("%y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8].upper(),
            "project_path": str(project_path), "goal": goal, "status": "RUNNING",
            "created_at": _now(), "updated_at": _now(),
            "max_steps": int(knobs.get("max_steps", autopilot.setting(config, "max_steps", 6))),
            "max_consecutive_failures": int(knobs.get("max_consecutive_failures", autopilot.setting(config, "max_consecutive_failures", 3))),
            "profile": profile, "source": source, "request_id": request_id,            # None: the config budgets as they are
            # off | on_done | on_pass: whether the finished deliverable is applied to the project automatically
            "auto_apply": auto_apply or autopilot.setting(config, "auto_apply", "on_pass"),
            "base_worktree": base_worktree, "continued_from": base_worktree,
            "summary": "", "blocked_reason": None, "owner_pid": None, "applied": None,
            "steps": [{"index": 1, "order": first_order.model_dump(), "entry_notes": list(notes or []),
                       "run_id": None, "result": None}],
        }
        self.store.save(mission)
        return mission

    def run(self, mission_id: str) -> Dict[str, Any]:
        """Blocking mission loop. Safe to call again after a crash/restart: it continues from persisted state."""
        mission = self.store.load(mission_id)
        if not mission:
            raise ValueError(f"mission {mission_id} not found")
        mission["owner_pid"] = os.getpid()
        self.store.save(mission)
        from core import usage
        usage.set_context(mission_id=mission_id)
        set_stop_guard(lambda: self._stop_reason(mission_id), owner=mission_id)
        try:
            while mission["status"] == "RUNNING":
                mission = self._advance(mission)
                fresh = self.store.load(mission_id)
                if fresh and fresh.get("status") == "STOPPED":      # the operator pressed 작업 정지 during this step
                    mission = fresh
                    break
        finally:
            set_stop_guard(None)
            latest = self.store.load(mission_id) or mission
            latest["owner_pid"] = None
            self.store.save(latest)
        return self.store.load(mission_id)

    def _stop_reason(self, mission_id: str) -> Optional[str]:
        current = self.store.load(mission_id)
        if current and current.get("status") == "STOPPED":
            return current.get("blocked_reason") or "stopped"
        return None

    def reopen(self, mission_id: str) -> Optional[Dict[str, Any]]:
        """A BLOCKED/INCOMPLETE mission continues (e.g. after the user approved the blocking run)."""
        mission = self.store.load(mission_id)
        if not mission or mission["status"] == "DONE":
            return mission
        mission["status"], mission["blocked_reason"] = "RUNNING", None
        mission["stopped_at"] = None
        self.store.save(mission, force=True)
        return mission

    # ------------------------------------------------------------------ one step
    def _advance(self, mission: Dict[str, Any]) -> Dict[str, Any]:
        step = mission["steps"][-1]
        total = mission["max_steps"]
        orch = self.factory()
        knobs = autopilot.profile_overrides(orch.config, mission.get("profile"))
        if knobs:                                              # the profile's budgets apply to every run of this mission
            orch.config.setdefault("autonomy", {}).update({k: v for k, v in knobs.items() if k != "max_steps"})
        row = orch.sm.get_run(step["run_id"]) if step.get("run_id") else None
        # a recorded result goes stale when the run moved on afterwards (e.g. the user approved a blocked step)
        stale = step.get("result") is not None and row is not None and row["current_state"] != step["result"].get("final_state")
        if step.get("result") is None or stale:
            self._execute(orch, mission, step)
            step["result"] = self._evidence(orch, step["run_id"])
            self.store.save(mission)
            r = step["result"]
            self.notify(f"[미션 {step['index']}/{total}] 결과 {r['final_state']} - "
                        f"{self._one_line(r)}", run_id=step["run_id"])
            if self._stop_reason(mission["mission_id"]):
                return self.store.load(mission["mission_id"])
        result = step["result"]
        if result["final_state"] == State.PASS:
            mission["base_worktree"] = result.get("worktree_path") or mission.get("base_worktree")
        elif result["final_state"] not in (State.FAIL, State.TIMEOUT):
            # Only a delegated exception (money / credentials / irreversible) or a security stop waits for the user;
            # any other halted milestone is treated as failed and HEAD re-plans (a new milestone gets a fresh worktree).
            detail = result.get("stop_detail") or ""
            treated = "FAIL (not a delegated exception; HEAD re-plans)"
            if step.get("operator_replan"):
                # The operator looked at the stop and sent it back to planning (e.g. a guard false positive that
                # was fixed). Nothing is approved or applied: the stopped run stays stopped, and the next
                # milestone's own Scope Guard checks every file again.
                treated = "FAIL (operator re-plan: " + str(step["operator_replan"])[:300] + "; HEAD re-plans)"
            elif autopilot.run_stop_needs_user(detail):
                if not (autopilot.head_decision_only(detail) and step.get("user_decision_review") != "ESCALATE"):
                    return self._finish(mission, "BLOCKED", blocked=detail or result["final_state"])
                # Second look as the user's proxy: only money, credentials, irreversible external actions or
                # security wait for the user. HEAD's verdict is recorded; if it escalates, the mission blocks.
                ok, why, source = autopilot.proxy_verdict(orch.head, (
                    "A milestone's final decision asked the user (USER_DECISION). Under the user's delegation only "
                    "payments/paid services, credentials, live-money trading, irreversible external actions or security "
                    "violations may go to the user. APPROVE if this can instead be resolved by re-planning the next "
                    "milestone (e.g. widening its scope or fixing a test fixture); ESCALATE only if it is truly one of "
                    "those categories."), {"stop_detail": detail[:4000], "scan_text": detail}, cwd=mission["project_path"])
                step["user_decision_review"] = "APPROVE" if ok else "ESCALATE"
                orch.sm.log_audit(step["run_id"], "ORCHESTRATOR", "MISSION_USER_DECISION_REVIEW",
                                  f"{source}: {'re-plan' if ok else 'escalate'} - {why}")
                if not ok:
                    self.store.save(mission)
                    return self._finish(mission, "BLOCKED", blocked=detail)
                treated = f"FAIL (USER_DECISION reviewed by {source} as not a delegated exception: {why[:300]}; HEAD re-plans)"
                self.notify(f"[미션 {step['index']}/{total}] 사용자 확인 요청을 HEAD가 재검토: 위임 예외(결제·자격증명·"
                            f"실자금·보안)가 아니므로 계획을 다시 세웁니다.", run_id=step["run_id"])
            result["treated_as"] = treated
            self.store.save(mission)

        plan = self._plan(orch, mission)
        status = str(plan.get("status", "")).upper()
        if status == "DONE":
            return self._finish(mission, "DONE", summary=plan.get("summary") or plan.get("reason", ""))
        if status == "BLOCKED":
            return self._finish(mission, "BLOCKED", summary=plan.get("summary", ""), blocked=plan.get("reason", ""))
        if status != "CONTINUE":
            return self._finish(mission, "INCOMPLETE", summary=plan.get("summary", ""),
                                blocked=f"HEAD could not plan the next step: {plan.get('reason', '')}")
        if len(mission["steps"]) >= total:
            return self._finish(mission, "INCOMPLETE", summary=plan.get("summary", ""),
                                blocked=f"step budget ({total}) used; next step was: {plan['order'].get('goal', '')}")
        # Consecutive failures end a mission - but an attempt HEAD chose to build on is progress, not a dead end
        # (the step budget still bounds the mission).
        streak, steps = 0, mission["steps"]
        for i in range(len(steps) - 1, -1, -1):
            res = steps[i].get("result") or {}
            if res.get("final_state") == State.PASS:
                break
            if i + 1 < len(steps) and steps[i + 1].get("builds_on_failed_attempt") == res.get("worktree_path"):
                break
            if i + 1 == len(steps) and plan.get("build_on_last_attempt") is True and res.get("worktree_path"):
                break
            streak += 1
        if streak >= mission["max_consecutive_failures"]:
            return self._finish(mission, "FAILED", summary=plan.get("summary", ""),
                                blocked=f"{streak} consecutive milestones failed")

        order = OrderSchema(**plan["order"])
        from core import usage
        usage.set_context(stage="ENTRY_REVIEW")
        builds_on = None
        attempt_wt = result.get("worktree_path")
        if (plan.get("build_on_last_attempt") is True and result["final_state"] != State.PASS
                and attempt_wt and Path(attempt_wt).is_dir()):
            # HEAD judged the failed attempt worth keeping: continue from it rather than starting over
            mission["base_worktree"] = builds_on = attempt_wt
        gate = autopilot.entry_gate(order, mission["project_path"], orch.head,
                                    cwd=mission.get("base_worktree") or mission["project_path"])
        nxt = {"index": len(mission["steps"]) + 1, "order": gate["order"].model_dump(),
               "entry_notes": gate["notes"], "plan_summary": plan.get("summary", ""), "run_id": None, "result": None,
               "builds_on_failed_attempt": builds_on}
        mission["steps"].append(nxt)
        if gate["escalate"]:
            nxt["run_id"] = self._record_escalation(orch, mission, gate["order"], gate["escalate"])
            nxt["result"] = self._evidence(orch, nxt["run_id"])
            return self._finish(mission, "BLOCKED", summary=plan.get("summary", ""), blocked=gate["escalate"])
        self.store.save(mission)
        self.notify(f"[미션 {nxt['index']}/{total}] HEAD 계획: {plan.get('summary', '')[:400]}\n다음 단계: {order.goal[:300]}",
                    role="HEAD")
        return mission

    def _execute(self, orch, mission: Dict[str, Any], step: Dict[str, Any]) -> None:
        order = OrderSchema(**step["order"])
        project = mission["project_path"]
        rid = step.get("run_id")
        row = orch.sm.get_run(rid) if rid else None
        if row and row["current_state"] in TERMINAL_STATES:
            return
        if row and row["current_state"] != State.IDLE:
            orch.resume(rid)                              # interrupted mid-pipeline (e.g. reboot)
            return
        if not rid:
            rid = orch.reserve_run_id(project, order.goal)
            step["run_id"] = rid
            orch._save_json(rid, "mission_base.json", {"mission_id": mission["mission_id"],
                                                        "base_worktree": mission.get("base_worktree")})
            self.store.save(mission)
        else:
            orch._reserved_run_ids = set(getattr(orch, "_reserved_run_ids", set())) | {rid}
        self.notify(f"[미션 {step['index']}/{mission['max_steps']}] 실행 시작: {order.goal[:300]}", run_id=rid)
        try:
            orch.run(project, order.goal, run_id=rid, order=order, base_worktree=mission.get("base_worktree"))
        except Exception as exc:
            row = orch.sm.get_run(rid) or {}
            if row.get("current_state") not in TERMINAL_STATES:
                orch.sm.transition_to(rid, State.FAIL, details=f"MISSION_STEP_EXCEPTION: {str(exc)[:500]}")

    @staticmethod
    def _record_escalation(orch, mission, order: OrderSchema, reason: str) -> str:
        rid = orch._create_run_with_retry(None, mission["project_path"], order.goal)
        orch._save_json(rid, "order.json", order)
        # if the user approves, the resumed run must still build on the previous milestone
        orch._save_json(rid, "mission_base.json", {"mission_id": mission["mission_id"],
                                                    "base_worktree": mission.get("base_worktree")})
        orch.sm.log_audit(rid, "HEAD", "MISSION_ESCALATION", reason)
        orch.sm.transition_to(rid, State.HUMAN_REQUIRED, details=reason)
        return rid

    # ------------------------------------------------------------------ HEAD planning
    def _plan(self, orch, mission: Dict[str, Any]) -> Dict[str, Any]:
        history = [{"step": s["index"], "goal": s["order"].get("goal"),
                    "acceptance_criteria": s["order"].get("acceptance_criteria"),
                    "result": s.get("result")} for s in mission["steps"]]
        where = mission.get("base_worktree") or mission["project_path"]
        try:
            context = orch._read_project_context(where)
        except Exception:
            context = {"project_path": where}
        last_run = mission["steps"][-1].get("run_id") or mission["mission_id"]
        from core import usage
        usage.set_context(stage="MISSION_PLANNING", run_id=None)
        tried, err = set(), None
        for attempt in range(4):
            tried.add(orch._get_agent_domain(orch.head))
            try:
                plan = orch.head.plan_next(mission["goal"], history, context, cwd=where) or {}
                status = str(plan.get("status", "")).upper()
                if status == "CONTINUE":
                    problems = autopilot.order_conflicts(OrderSchema(**(plan.get("order") or {})))
                    if not problems:
                        return plan
                    # HEAD's own ORDER contradicts itself (e.g. "create data/x.json" while protecting data/*.json):
                    # it would fail in the Builder every time, so HEAD gets it back with the exact conflict.
                    err = "ORDER_CONFLICT: " + "; ".join(problems)[:600]
                    context["previous_plan_rejected"] = (
                        err + ". Fix the ORDER: every required output must be inside allowed_paths and must not match "
                        "protected_paths. To protect existing files next to a new one, list them by exact name instead "
                        "of a wildcard that also covers the new file.")
                    orch.sm.log_audit(last_run, "ORCHESTRATOR", "MISSION_PLAN_RETRY", f"attempt {attempt + 1}: {err}")
                    continue
                if status in ("DONE", "BLOCKED"):
                    return plan
                err = f"invalid plan status {status!r}"
            except Exception as exc:
                err = str(exc)[:300]
            orch.sm.log_audit(last_run, "ORCHESTRATOR", "MISSION_PLAN_RETRY", f"attempt {attempt + 1}: {err}")
            if attempt >= 1 and not orch._swap_agent("head", last_run, _NullLogger(), tried, "MISSION_PLANNING", err):
                break
        return {"status": "STALLED", "reason": err, "summary": ""}

    # ------------------------------------------------------------------ evidence / reporting
    @staticmethod
    def _evidence(orch, run_id: str) -> Dict[str, Any]:
        row = orch.sm.get_run(run_id) or {}
        trans = orch.sm.get_transitions(run_id)
        ver = orch._load_json(run_id, "verification.json") or {}
        rev = orch._load_json(run_id, "review.json") or {}
        dec = orch._load_json(run_id, "decision.json") or {}
        bld = orch._load_json(run_id, "builder.json") or {}
        return {
            "run_id": run_id, "final_state": row.get("current_state"), "worktree_path": row.get("worktree_path"),
            "stop_detail": ((trans[-1].get("details") if trans else "") or "")[:1500],
            "tests": ver.get("test_counts"), "verification": (ver.get("summary") or "")[:600],
            "review_verdict": rev.get("verdict"),
            "review_findings": (list(rev.get("p0") or []) + list(rev.get("p1") or []) + list(rev.get("p2") or []))[:10],
            "head_rationale": (dec.get("rationale") or "")[:1200],
            "files_changed": bld.get("files_modified"),
            "repair_rounds": len(orch._load_json(run_id, "repair_history.json") or []),
        }

    @staticmethod
    def _one_line(result: Dict[str, Any]) -> str:
        tests = result.get("tests") or {}
        parts = []
        if tests:
            parts.append(f"테스트 {tests.get('passed', 0)} 통과/{tests.get('failed', 0)} 실패")
        if result.get("review_verdict"):
            parts.append(f"리뷰 {result['review_verdict']}")
        if result.get("repair_rounds"):
            parts.append(f"자동 수정 {result['repair_rounds']}회")
        if result["final_state"] != State.PASS:
            parts.append((result.get("stop_detail") or "")[:200])
        return ", ".join(parts)

    def _finish(self, mission: Dict[str, Any], status: str, summary: str = "", blocked: Optional[str] = None):
        if self._stop_reason(mission["mission_id"]):   # a stopped mission is neither completed nor applied
            return self.store.load(mission["mission_id"])
        mission["status"] = status
        mission["summary"] = summary or mission.get("summary", "")
        mission["blocked_reason"] = blocked
        self.store.save(mission)
        passed = sum(1 for s in mission["steps"] if (s.get("result") or {}).get("final_state") == State.PASS)
        where = mission.get("base_worktree") or "(아직 통과한 단계 없음)"
        head_line = {"DONE": "미션 완료", "BLOCKED": "미션 보류 - 사용자 결정 필요",
                     "INCOMPLETE": "미션 중단 - 단계 예산 소진 또는 계획 불가", "FAILED": "미션 실패"}[status]
        text = (f"{head_line} ({passed}/{len(mission['steps'])} 단계 통과)\n"
                f"목표: {mission['goal'][:300]}\n{mission['summary'][:1500]}\n결과물 위치(작업본): {where}")
        if blocked:
            text += f"\n사유: {blocked[:600]}"
        self.notify(text, run_id=mission["steps"][-1].get("run_id"), role="HEAD")
        self._auto_apply(mission)
        return mission

    def _auto_apply(self, mission: Dict[str, Any]) -> None:
        """Deliver the finished work into the project when the mission's policy says so. Only PASSED milestones
        are ever applied; a conflict with the user's own edits blocks instead of overwriting."""
        policy = mission.get("auto_apply") or "off"
        rid = deliverable_run_id(mission)
        if policy == "off" or not rid or (policy == "on_done" and mission["status"] != "DONE"):
            return
        try:
            from core.result_manager import ResultManager
            orch = self.factory()
            result = ResultManager(config=getattr(orch, "config", {}) or {}).apply_tree(
                rid, request_id="auto-" + mission["mission_id"], applied_by="DIRECTOR_AUTO", on_conflict="block")
        except Exception as exc:
            result = {"status": "error", "error": str(exc)[:500]}
        mission["applied"] = {"run_id": rid, "at": _now(), **{k: v for k, v in result.items() if k != "request_id"}}
        self.store.save(mission)
        if result.get("status") == "applied":
            self.notify(f"결과물을 프로젝트에 적용했습니다: {len(result.get('files', []))}개 파일 "
                        f"({mission['project_path']}). 적용 후 테스트: {result.get('verification')}", run_id=rid)
        else:
            why = "; ".join(result.get("blocks") or []) or result.get("reason") or result.get("error") or result.get("status")
            self.notify(f"결과물을 자동 적용하지 않았습니다 ({result.get('status')}): {str(why)[:400]}. "
                        f"작업본: {mission.get('base_worktree')}", run_id=rid)


class _NullLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def stop_request(mission_id: str, reason: str = "", by: str = "사용자", store: Optional[MissionStore] = None) -> Optional[Dict[str, Any]]:
    """Mark a RUNNING mission STOPPED. The mission loop exits after the current step (or at once if the step's CLI
    is killed by the caller); resume-on-start never picks a STOPPED mission up; reopen() continues it later."""
    store = store or MissionStore()
    m = store.load(mission_id)
    if not m or m.get("status") != "RUNNING":
        return m
    m["status"] = "STOPPED"
    m["stopped_at"] = _now()
    m["blocked_reason"] = f"{by} 지시로 정지" + (f": {reason[:300]}" if reason else "") + " · 이어가려면 '재개'"
    store.save(m, force=True)
    return m


def missions_to_resume(store: Optional[MissionStore] = None) -> List[Dict[str, Any]]:
    """RUNNING missions whose owning process is gone (restart / crash)."""
    store = store or MissionStore()
    return [m for m in store.all() if m.get("status") == "RUNNING"
            and (not m.get("owner_pid") or (m["owner_pid"] != os.getpid() and not _pid_alive(m["owner_pid"])))]
