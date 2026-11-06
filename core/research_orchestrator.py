"""V3 TRIAD Research Orchestrator: milestone B session intake and readiness gate.

No provider inference occurs in this milestone.  The class owns research policy decisions while
ResearchStateMachine remains the only mechanism that mutates research state.
"""
import json
import hashlib
import os
import uuid
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, get_args, get_origin

from core.db_path import SOURCE_ROOT
from core.greenfield import (
    BASELINE_NOT_APPLICABLE_GREENFIELD,
    PROJECT_TYPE_GREENFIELD,
    assess_project_type,
    build_greenfield_baseline_evidence,
)
from core.provider_manager import ProviderManager
from core.research_prompts import (
    build_cross_critique_prompt,
    build_experiment_order_prompt,
    build_independent_interpretation_prompt,
    build_interpretation_cross_prompt,
    build_research_memory_prompt,
    build_synthesis_prompt,
    persist_independent_prompt_artifacts,
    persist_stage_prompt,
)
from core.research_experiment import TriadExperimentOrchestrator
from core.research_runner import TriadResearchRunner, cleanup_research_workspaces, create_research_workspaces
from core.safety import SafetyPolicy
from core.state_machine import State as NormalState
from core.research_state import (
    DEFAULT_CYCLE_BUDGET,
    INTERPRETATION_CROSS_STAGE,
    INTERPRETATION_INDEPENDENT_STAGE,
    PARTICIPANT_STATUS_PENDING,
    PARTICIPANT_STATUS_COMPLETE,
    PARTICIPANT_STATUS_UNAVAILABLE,
    ResearchState,
    ResearchStateMachine,
    RESEARCH_MEMORY_STAGE,
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_HUMAN_REQUIRED,
    SESSION_STATUS_MILESTONE_COMPLETE,
    SESSION_STATUS_STOPPED,
)
from schemas.research import (
    InterpretationCritiqueSchema,
    ResearchCritiqueSchema,
    ResearchInterpretationSchema,
    ResearchMemorySchema,
    ResearchProposalSchema,
    ResearchSynthesisSchema,
)
from schemas.order import OrderSchema
from core.triad_provider import (
    ALL_TRIAD_DOMAINS,
    TRIAD_BLOCKED,
    TRIAD_DEGRADED,
    TRIAD_READY,
    assess_triad_readiness,
)


class ResearchOrchestrator:
    def __init__(self, state_machine: Optional[ResearchStateMachine] = None,
                 provider_manager: Optional[ProviderManager] = None, artifact_root: Optional[Path] = None,
                 provider_call: Optional[Callable[..., Dict[str, Any]]] = None,
                 research_runner: Optional[TriadResearchRunner] = None,
                 experiment_factory: Optional[Callable[..., TriadExperimentOrchestrator]] = None,
                 experiment_config: Optional[Dict[str, Any]] = None):
        self.rsm = state_machine or ResearchStateMachine()
        self.pm = provider_manager or ProviderManager()
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None
        self.provider_call = provider_call
        self.research_runner = research_runner
        self.experiment_factory = experiment_factory
        self.experiment_config = experiment_config

    @staticmethod
    def generate_session_id() -> str:
        return f"TRIAD-{datetime.utcnow().strftime('%y%m%d%H%M%S')}-{uuid.uuid4().hex.upper()}"

    @staticmethod
    def _project_context(project_path: str) -> Dict[str, Any]:
        project = Path(project_path).resolve()
        if not project.exists() or not project.is_dir():
            raise ValueError(f"project directory does not exist: {project_path}")
        files = []
        for path in project.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(project)
            if any(part in {".git", ".director_worktrees", "__pycache__", ".pytest_cache"} for part in rel.parts):
                continue
            files.append(rel.as_posix())
            if len(files) >= 100:
                break
        return {"project_path": str(project), "existing_files": sorted(files)}

    def _activate_independent_stage(self, session_id: str, cycle_id: str, readiness: Dict[str, Any],
                                    verified_evidence: Optional[Dict[str, Any]] = None,
                                    research_memory_facts: Optional[list] = None) -> list:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        prompts = persist_independent_prompt_artifacts(
            state_machine=self.rsm,
            session_id=session_id,
            cycle_id=cycle_id,
            domains=readiness["usable"],
            user_challenge=session["moonshot"],
            project_context=self._project_context(session["project_path"]),
            verified_evidence=verified_evidence,
            research_memory_facts=research_memory_facts,
            artifact_root=self.artifact_root,
        )
        self.rsm.set_session_status(session_id, SESSION_STATUS_ACTIVE)
        self.rsm.transition_cycle(
            cycle_id, ResearchState.INDEPENDENT_RESEARCH,
            details=f"intake gate passed: {readiness['state']}; independent prompts prepared",
        )
        return prompts

    def start_session(self, project_path: str, user_challenge: str, cycle_budget: int = DEFAULT_CYCLE_BUDGET,
                      allow_degraded: bool = False, session_id: Optional[str] = None,
                      verified_evidence: Optional[Dict[str, Any]] = None,
                      new_project: bool = False,
                      existing_product_modification_request: bool = False) -> Dict[str, Any]:
        """Create one session/cycle and enforce TRIAD readiness before any research provider can run."""
        context = self._project_context(project_path)
        project_evidence = assess_project_type(
            context["project_path"],
            user_explicit_new_project=bool(new_project),
            existing_product_modification_request=bool(existing_product_modification_request),
        )
        readiness = assess_triad_readiness(self.pm)
        sid = session_id or self.generate_session_id()
        cycle_id = f"{sid}-C1"
        self.rsm.create_session(
            sid, context["project_path"], user_challenge,
            project_type=project_evidence["project_type"], cycle_budget=cycle_budget,
        )
        self.rsm.create_cycle(cycle_id, sid, 1)
        project_artifact = self._persist_project_evidence(sid, project_evidence)
        self.rsm.record_project_evidence(sid, project_evidence, project_artifact)
        participant_statuses = {
            domain: (PARTICIPANT_STATUS_PENDING if domain in readiness["usable"] else PARTICIPANT_STATUS_UNAVAILABLE)
            for domain in ALL_TRIAD_DOMAINS
        }
        self.rsm.register_participants(cycle_id, participant_statuses)
        approved = readiness["state"] == TRIAD_DEGRADED and bool(allow_degraded)
        self.rsm.record_intake(sid, readiness, degraded_approved=approved)

        prompt_artifacts = []
        human_required = False
        if new_project and project_evidence["project_type"] != PROJECT_TYPE_GREENFIELD:
            human_required = True
            failed = ", ".join(project_evidence["failed_predicates"])
            self.rsm.set_session_status(
                sid, SESSION_STATUS_HUMAN_REQUIRED,
                f"GREENFIELD_QUALIFICATION_FAILED: {failed}",
            )
            message = "새 프로젝트 GREENFIELD 조건을 증명하지 못해 연구 실행을 차단했습니다."
        elif readiness["state"] == TRIAD_READY:
            prompt_artifacts = self._activate_independent_stage(sid, cycle_id, readiness, verified_evidence)
            message = "세 AI를 모두 사용할 수 있습니다. 독립 연구 준비가 완료되었습니다."
        elif readiness["state"] == TRIAD_DEGRADED and approved:
            prompt_artifacts = self._activate_independent_stage(sid, cycle_id, readiness, verified_evidence)
            message = "현재 2/3 AI만 사용 가능합니다. 명시적 승인에 따라 degraded 연구를 준비했습니다."
        elif readiness["state"] == TRIAD_DEGRADED:
            human_required = True
            self.rsm.set_session_status(
                sid, SESSION_STATUS_HUMAN_REQUIRED,
                "현재 2/3 AI만 사용 가능합니다. degraded 진행에 대한 명시적 사용자 결정이 필요합니다.",
            )
            message = "현재 2/3 AI만 사용 가능합니다. 계속하려면 degraded 진행을 명시적으로 승인해야 합니다."
        else:
            human_required = True
            self.rsm.set_session_status(
                sid, SESSION_STATUS_HUMAN_REQUIRED,
                "TRIAD_BLOCKED: 두 개 이상의 provider를 사용할 수 없어 연구를 시작할 수 없습니다.",
            )
            message = "두 개 이상의 AI를 사용할 수 없어 TRIAD 연구를 시작할 수 없습니다."

        return self.session_summary(sid, message=message, human_required=human_required,
                                    prompt_artifacts=prompt_artifacts)

    def approve_degraded_session(self, session_id: str, approved_by: str,
                                 verified_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Explicitly authorize a previously recorded 2-provider intake; never upgrades its label to full TRIAD."""
        if not approved_by or not str(approved_by).strip():
            raise ValueError("approved_by is required")
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        evidence = json.loads(session.get("intake_evidence") or "{}")
        if evidence.get("state") != TRIAD_DEGRADED or len(evidence.get("usable") or []) != 2:
            raise ValueError("session is not awaiting degraded TRIAD approval")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.RESEARCH_INTAKE:
            raise ValueError("degraded approval is only valid during RESEARCH_INTAKE")
        evidence["degraded_approved_by"] = str(approved_by)
        evidence["degraded_approved_at"] = datetime.utcnow().isoformat()
        self.rsm.record_intake(session_id, evidence, degraded_approved=True)
        memory_facts = self.retrieve_research_memory_facts(session_id)
        prompts = self._activate_independent_stage(
            session_id, cycle["cycle_id"], evidence, verified_evidence, memory_facts
        )
        return self.session_summary(
            session_id,
            message="현재 2/3 AI만 사용 가능합니다. 명시적 승인에 따라 degraded 연구를 준비했습니다.",
            human_required=False,
            prompt_artifacts=prompts,
        )

    def session_summary(self, session_id: str, message: str = "", human_required: Optional[bool] = None,
                        prompt_artifacts: Optional[list] = None) -> Dict[str, Any]:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycles = self.rsm.list_cycles(session_id)
        cycle = cycles[-1] if cycles else None
        participants = self.rsm.get_participants(cycle["cycle_id"]) if cycle else []
        readiness = session.get("readiness_state")
        is_human_required = session["status"] == SESSION_STATUS_HUMAN_REQUIRED if human_required is None else human_required
        return {
            "session_id": session_id,
            "mode": session.get("mode") or "TRIAD",
            "project": session["project_path"],
            "project_type": session.get("project_type") or "UNKNOWN",
            "user_challenge": session["moonshot"],
            "created_at": session["created_at"],
            "cycle_budget": session["cycle_budget"],
            "current_cycle": session.get("current_cycle", 0),
            "current_stage": session.get("current_stage") or ResearchState.RESEARCH_INTAKE,
            "status": session["status"],
            "readiness": readiness,
            "degraded": bool(session.get("degraded")),
            "degraded_approved": bool(session.get("degraded_approved")),
            "missing_domains": json.loads(session.get("missing_domains") or "[]"),
            "participant_count": sum(1 for p in participants if p["status"] != PARTICIPANT_STATUS_UNAVAILABLE),
            "participants": participants,
            "human_required": is_human_required,
            "execution_authorized": bool(cycle and cycle["stage"] == ResearchState.INDEPENDENT_RESEARCH),
            "message": message,
            "prompt_artifacts": prompt_artifacts if prompt_artifacts is not None else self.rsm.get_prompt_artifacts(
                cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH
            ) if cycle else [],
            "project_evidence": self.rsm.get_project_evidence(session_id),
        }

    # -------------------------------------------------------------- V3.0-C execution stages

    def _root(self) -> Path:
        return self.artifact_root if self.artifact_root is not None else SOURCE_ROOT / "research"

    def _persist_project_evidence(self, session_id: str, evidence: Dict[str, Any]) -> str:
        root = self._root().resolve()
        target = (root / session_id / "evidence" / "project_evidence.json").resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise ValueError("project evidence path escapes artifact root")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, target)
        return str(target)

    def prepare_greenfield_baseline(self, session_id: str,
                                    existing_product_modification_request: bool = False) -> Dict[str, Any]:
        """Revalidate and persist a non-PASS baseline exemption immediately before an experiment build.

        The normal Verifier is intentionally not called.  Milestone E consumes this evidence only for a
        session explicitly classified as GREENFIELD, while post-build verification remains mandatory.
        """
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycles = self.rsm.list_cycles(session_id)
        if not cycles:
            raise ValueError("research session has no cycle")
        cycle = cycles[-1]
        if cycle["stage"] != ResearchState.EXPERIMENT_PLANNING:
            raise ValueError("GREENFIELD baseline may only be prepared during EXPERIMENT_PLANNING")
        if session.get("project_type") != PROJECT_TYPE_GREENFIELD:
            return self._fail_cycle(session_id, cycle["cycle_id"], "GREENFIELD_BASELINE_FORBIDDEN: session is not GREENFIELD")

        fresh = assess_project_type(
            session["project_path"], user_explicit_new_project=True,
            existing_product_modification_request=existing_product_modification_request,
        )
        project_artifact = self._persist_project_evidence(session_id, fresh)
        self.rsm.record_project_evidence(session_id, fresh, project_artifact)
        if fresh["project_type"] != PROJECT_TYPE_GREENFIELD:
            failed = ", ".join(fresh["failed_predicates"])
            return self._fail_cycle(
                session_id, cycle["cycle_id"], f"GREENFIELD_QUALIFICATION_FAILED: {failed}"
            )

        baseline = build_greenfield_baseline_evidence(fresh)
        root = self._root().resolve()
        target = (root / session_id / cycle["cycle_id"] / "experiment_planning" / "greenfield_baseline.json").resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise ValueError("greenfield baseline path escapes artifact root")
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, target)
        canonical = json.dumps(baseline, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        baseline_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.rsm.record_baseline_evidence(cycle["cycle_id"], baseline, str(target), baseline_sha256)
        baseline["artifact_path"] = str(target)
        baseline["evidence_sha256"] = baseline_sha256
        return baseline

    @staticmethod
    def _extract_json(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        text = str(raw or "").strip()
        if not text:
            raise ValueError("provider returned an empty response")
        try:
            data = json.loads(text)
            if (
                isinstance(data, dict)
                and isinstance(data.get("result"), str)
                and any(key in data for key in ("type", "subtype", "session_id", "is_error", "duration_ms"))
            ):
                return ResearchOrchestrator._extract_json(data["result"])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        if "```json" in text:
            candidate = text.split("```json", 1)[1].split("```", 1)[0]
            return json.loads(candidate.strip())
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise ValueError("provider response did not contain a JSON object")

    @staticmethod
    def _coerce_list_fields(model_cls, data: Dict[str, Any]) -> Dict[str, Any]:
        """Real subscription-CLI JSON output occasionally returns a single string for a field the schema
        declares as List[str] (e.g. one assumption instead of a one-item list) even when the prompt's type
        contract is explicit about it. This is shape tolerance, not content leniency: it never changes what
        a field says, never adds or drops a key, and only ever widens a bare non-empty string into a
        single-element list for a field the schema itself declares list-of-string - every other type
        (dict, number, bool, list of non-string, or a field the model does not declare) is left untouched
        and still fails schema validation exactly as before if malformed."""
        if not isinstance(data, dict):
            return data
        coerced = dict(data)
        for name, field in model_cls.model_fields.items():
            if name not in coerced:
                continue
            value = coerced[name]
            if not isinstance(value, str):
                continue
            origin = get_origin(field.annotation)
            args = get_args(field.annotation)
            if origin is list and len(args) == 1 and args[0] is str:
                coerced[name] = [value] if value.strip() else []
        return coerced

    @staticmethod
    def _hypothesis_sha256(hypothesis: str) -> str:
        normalized = " ".join(str(hypothesis or "").casefold().split())
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _output_path(self, session_id: str, cycle_id: str, stage: str, domain: str) -> Path:
        root = self._root().resolve()
        target = (root / session_id / cycle_id / stage.lower() / f"{domain.lower()}_output.json").resolve()
        if os.path.commonpath([str(root), str(target)]) != str(root):
            raise ValueError("research output path escapes artifact root")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def _record_successful_output(self, session_id: str, cycle_id: str, stage: str, domain: str,
                                  prompt: Dict[str, Any], call_result: Dict[str, Any], payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        output_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        path = self._output_path(session_id, cycle_id, stage, domain)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
        self.rsm.record_stage_output(
            cycle_id, stage, domain, str(path), output_hash, len(encoded), len(encoded.encode("utf-8")), payload
        )
        raw_path, raw_hash, raw_chars, raw_bytes = self._persist_raw_output(
            session_id, cycle_id, stage, domain, call_result.get("output")
        )
        self._record_call(session_id, cycle_id, stage, domain, prompt, call_result, "SUCCESS",
                          output_path=raw_path, output_hash=raw_hash,
                          response_chars=raw_chars, response_bytes=raw_bytes)

    def _persist_raw_output(self, session_id: str, cycle_id: str, stage: str, domain: str, raw: Any):
        text = str(raw or "")
        structured_path = self._output_path(session_id, cycle_id, stage, domain)
        path = structured_path.with_name(f"{domain.lower()}_raw.txt")
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, path)
        encoded = text.encode("utf-8")
        return str(path), hashlib.sha256(encoded).hexdigest(), len(text), len(encoded)

    def _record_invalid_output(self, session_id: str, cycle_id: str, stage: str, domain: str,
                               prompt: Dict[str, Any], call_result: Dict[str, Any], error: str) -> None:
        raw_path, raw_hash, raw_chars, raw_bytes = self._persist_raw_output(
            session_id, cycle_id, stage, domain, call_result.get("output")
        )
        self._record_call(session_id, cycle_id, stage, domain, prompt, call_result, "FAILED", error,
                          output_path=raw_path, output_hash=raw_hash,
                          response_chars=raw_chars, response_bytes=raw_bytes)

    def _record_call(self, session_id: str, cycle_id: str, stage: str, domain: str,
                     prompt: Dict[str, Any], result: Optional[Dict[str, Any]], outcome: str,
                     error: Optional[str] = None, output_path: Optional[str] = None,
                     output_hash: Optional[str] = None, response_chars: Optional[int] = None,
                     response_bytes: Optional[int] = None) -> None:
        result = result or {}
        roles = {
            ResearchState.SYNTHESIS: "HEAD_SYNTHESIZER",
            ResearchState.EXPERIMENT_PLANNING: "HEAD_EXPERIMENT_PLANNER",
            INTERPRETATION_INDEPENDENT_STAGE: "RESULT_INTERPRETER",
            INTERPRETATION_CROSS_STAGE: "RESULT_INTERPRETATION_CRITIC",
            RESEARCH_MEMORY_STAGE: "HEAD_MEMORY_CURATOR",
        }
        research_role = roles.get(stage, "RESEARCHER")
        # V3.0-H bounded transient retry: `result["attempts"]` (if present) holds every prior FAILED attempt
        # for this same call (same domain/stage/prompt - never a different provider, never the bad output fed
        # forward). Each one gets its own truthful provenance row before the final (this) row, so the full
        # attempt history - not just the outcome - is preserved.
        prior_attempts = result.get("attempts") or []
        for prior in prior_attempts:
            self.rsm.record_call_provenance({
                "session_id": session_id, "cycle_id": cycle_id, "stage": stage, "domain": domain,
                "research_role": research_role,
                "executable": prior.get("executable"), "version": prior.get("version"),
                "invocation": prior.get("invocation"), "started_at": prior.get("started_at"),
                "ended_at": prior.get("ended_at"), "duration_sec": prior.get("duration_sec"),
                "exit_code": prior.get("exit_code"), "prompt_artifact": prompt.get("artifact_path"),
                "prompt_sha256": prompt.get("prompt_sha256"), "output_artifact": None, "output_sha256": None,
                "prompt_chars": prompt.get("prompt_chars"), "prompt_bytes": prompt.get("prompt_bytes"),
                "response_chars": None, "response_bytes": None,
                "token_usage": prior.get("token_usage") or "UNKNOWN", "failover": False,
                "authentication_mode": prior.get("authentication_mode") or "subscription",
                "api_key_used": bool(prior.get("api_key_used", False)),
                "observed_fields": prior.get("observed_fields") or [],
                "declared_fields": prior.get("declared_fields") or ["authentication_mode", "api_key_used"],
                "outcome": "FAILED", "error": prior.get("error"),
                "attempt_number": prior.get("attempt_number", 1), "retry_reason": prior.get("retry_reason"),
            })
        self.rsm.record_call_provenance({
            "session_id": session_id, "cycle_id": cycle_id, "stage": stage, "domain": domain,
            "research_role": research_role,
            "executable": result.get("executable"), "version": result.get("version"),
            "invocation": result.get("invocation"), "started_at": result.get("started_at"),
            "ended_at": result.get("ended_at"), "duration_sec": result.get("duration_sec"),
            "exit_code": result.get("exit_code"), "prompt_artifact": prompt.get("artifact_path"),
            "prompt_sha256": prompt.get("prompt_sha256"), "output_artifact": output_path,
            "output_sha256": output_hash, "prompt_chars": prompt.get("prompt_chars"),
            "prompt_bytes": prompt.get("prompt_bytes"), "response_chars": response_chars,
            "response_bytes": response_bytes, "token_usage": result.get("token_usage") or "UNKNOWN",
            "failover": result.get("failover", False),
            "authentication_mode": result.get("authentication_mode") or "subscription",
            "api_key_used": bool(result.get("api_key_used", False)),
            "observed_fields": result.get("observed_fields") or [],
            "declared_fields": result.get("declared_fields") or ["authentication_mode", "api_key_used"],
            "outcome": outcome, "error": error,
            "attempt_number": result.get("attempt_number", len(prior_attempts) + 1),
            "retry_reason": prior_attempts[-1].get("error") if prior_attempts else None,
        })

    def _invoke_one(self, domain: str, prompt: Dict[str, Any], stage: str, cwd: str) -> Dict[str, Any]:
        if self.provider_call is not None:
            return self.provider_call(domain=domain, prompt=prompt["prompt_text"], stage=stage, cwd=cwd)
        runner = self.research_runner or TriadResearchRunner()
        self.research_runner = runner
        return runner.invoke(domain, prompt["prompt_text"], cwd, stage)

    def _invoke_concurrently(self, session: Dict[str, Any], cycle_id: str, stage: str,
                             prompts: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        workspaces: Dict[str, str] = {}
        if self.provider_call is None:
            workspace_root = self._root() / session["session_id"] / cycle_id / "workspaces" / stage.lower()
            workspaces = create_research_workspaces(session["project_path"], workspace_root, prompts)
        else:
            workspaces = {domain: session["project_path"] for domain in prompts}
        results: Dict[str, Dict[str, Any]] = {}
        try:
            with ThreadPoolExecutor(max_workers=len(prompts), thread_name_prefix=f"triad-{stage.lower()}") as pool:
                futures = {
                    pool.submit(self._invoke_one, domain, prompt, stage, workspaces[domain]): domain
                    for domain, prompt in prompts.items()
                }
                for future in as_completed(futures):
                    domain = futures[future]
                    try:
                        results[domain] = {"ok": True, "result": future.result()}
                    except Exception as exc:
                        # `failure_result` (set only by TriadResearchRunner.invoke on total, non-retryable-or-
                        # exhausted failure) carries the last attempt's own metadata plus every prior FAILED
                        # attempt, so the truthful per-attempt provenance survives even a fully failed call.
                        results[domain] = {
                            "ok": False, "error": f"{type(exc).__name__}: {exc}",
                            "result": getattr(exc, "failure_result", None),
                        }
        finally:
            if self.provider_call is None:
                cleanup_research_workspaces(session["project_path"], workspaces)
        return results

    def _fail_cycle(self, session_id: str, cycle_id: str, message: str) -> Dict[str, Any]:
        self.rsm.transition_cycle(cycle_id, ResearchState.HUMAN_REQUIRED, details=message)
        self.rsm.set_session_status(session_id, SESSION_STATUS_HUMAN_REQUIRED, message)
        return self.session_summary(session_id, message=message, human_required=True)

    def run_independent_research(self, session_id: str) -> Dict[str, Any]:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.INDEPENDENT_RESEARCH:
            raise ValueError("session is not at INDEPENDENT_RESEARCH")
        prompt_rows = self.rsm.get_prompt_artifacts(cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)
        prompts = {row["domain"]: {**json.loads(Path(row["artifact_path"]).read_text(encoding="utf-8")),
                                    "artifact_path": row["artifact_path"]} for row in prompt_rows}
        expected = {p["domain"] for p in self.rsm.get_participants(cycle["cycle_id"])
                    if p["status"] != PARTICIPANT_STATUS_UNAVAILABLE}
        if set(prompts) != expected or len(expected) < 2:
            return self._fail_cycle(session_id, cycle["cycle_id"], "INDEPENDENT_PROMPT_EVIDENCE_MISSING")
        call_results = self._invoke_concurrently(session, cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH, prompts)
        failures = []
        for domain in sorted(expected):
            item = call_results[domain]
            if not item["ok"]:
                failures.append(f"{domain}: {item['error']}")
                self._record_call(session_id, cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH,
                                  domain, prompts[domain], item.get("result"), "FAILED", item["error"])
                self.rsm.set_participant_status(cycle["cycle_id"], domain, PARTICIPANT_STATUS_UNAVAILABLE)
                continue
            result = item["result"]
            try:
                proposal = ResearchProposalSchema.model_validate(
                    self._coerce_list_fields(ResearchProposalSchema, self._extract_json(result.get("output")))
                ).model_dump()
                self._record_successful_output(session_id, cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH,
                                               domain, prompts[domain], result, proposal)
                self.rsm.set_participant_status(cycle["cycle_id"], domain, PARTICIPANT_STATUS_COMPLETE)
            except Exception as exc:
                error = f"INVALID_PROPOSAL: {type(exc).__name__}: {exc}"
                failures.append(f"{domain}: {error}")
                self._record_invalid_output(session_id, cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH,
                                            domain, prompts[domain], result, error)
        if failures:
            return self._fail_cycle(session_id, cycle["cycle_id"], "INDEPENDENT_RESEARCH_FAILED: " + "; ".join(failures))
        if len(self.rsm.get_stage_outputs(cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)) != len(expected):
            return self._fail_cycle(session_id, cycle["cycle_id"], "INDEPENDENT_RESEARCH_EVIDENCE_INCOMPLETE")
        self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.CROSS_CRITIQUE,
                                  details="all independent proposals completed before disclosure")
        return self.session_summary(session_id, message="모든 독립 연구가 완료되어 상호 비판 단계로 전환했습니다.")

    def run_cross_critique(self, session_id: str) -> Dict[str, Any]:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.CROSS_CRITIQUE:
            raise ValueError("session is not at CROSS_CRITIQUE")
        proposal_rows = self.rsm.get_stage_outputs(cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)
        proposals = {row["domain"]: row["payload"] for row in proposal_rows}
        domains = {p["domain"] for p in self.rsm.get_participants(cycle["cycle_id"])
                   if p["status"] != PARTICIPANT_STATUS_UNAVAILABLE}
        if set(proposals) != domains or len(domains) < 2:
            return self._fail_cycle(session_id, cycle["cycle_id"], "CROSS_CRITIQUE_BLOCKED: proposal evidence incomplete")
        prompts = {}
        for domain in sorted(domains):
            envelope = build_cross_critique_prompt(domain, session["moonshot"], proposals, bool(session["degraded"]))
            prompts[domain] = persist_stage_prompt(self.rsm, session_id, cycle["cycle_id"], envelope, self.artifact_root)
        call_results = self._invoke_concurrently(session, cycle["cycle_id"], ResearchState.CROSS_CRITIQUE, prompts)
        failures = []
        for domain in sorted(domains):
            item = call_results[domain]
            if not item["ok"]:
                failures.append(f"{domain}: {item['error']}")
                self._record_call(session_id, cycle["cycle_id"], ResearchState.CROSS_CRITIQUE,
                                  domain, prompts[domain], item.get("result"), "FAILED", item["error"])
                continue
            result = item["result"]
            try:
                critique = ResearchCritiqueSchema.model_validate(
                    self._coerce_list_fields(ResearchCritiqueSchema, self._extract_json(result.get("output")))
                ).model_dump()
                self._record_successful_output(session_id, cycle["cycle_id"], ResearchState.CROSS_CRITIQUE,
                                               domain, prompts[domain], result, critique)
            except Exception as exc:
                error = f"INVALID_CRITIQUE: {type(exc).__name__}: {exc}"
                failures.append(f"{domain}: {error}")
                self._record_invalid_output(session_id, cycle["cycle_id"], ResearchState.CROSS_CRITIQUE,
                                            domain, prompts[domain], result, error)
        if failures:
            return self._fail_cycle(session_id, cycle["cycle_id"], "CROSS_CRITIQUE_FAILED: " + "; ".join(failures))
        if len(self.rsm.get_stage_outputs(cycle["cycle_id"], ResearchState.CROSS_CRITIQUE)) != len(domains):
            return self._fail_cycle(session_id, cycle["cycle_id"], "CROSS_CRITIQUE_EVIDENCE_INCOMPLETE")
        self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.SYNTHESIS,
                                  details="all researchers completed cross critique")
        return self.session_summary(session_id, message="상호 비판이 완료되어 통합 가설 단계로 전환했습니다.")

    def run_synthesis(self, session_id: str) -> Dict[str, Any]:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.SYNTHESIS:
            raise ValueError("session is not at SYNTHESIS")
        proposals = {r["domain"]: r["payload"] for r in self.rsm.get_stage_outputs(
            cycle["cycle_id"], ResearchState.INDEPENDENT_RESEARCH)}
        critiques = {r["domain"]: r["payload"] for r in self.rsm.get_stage_outputs(
            cycle["cycle_id"], ResearchState.CROSS_CRITIQUE)}
        if "OPENAI" not in proposals:
            return self._fail_cycle(session_id, cycle["cycle_id"], "SYNTHESIS_BLOCKED: Codex HEAD is unavailable")
        try:
            envelope = build_synthesis_prompt(session["moonshot"], proposals, critiques, bool(session["degraded"]))
        except Exception as exc:
            return self._fail_cycle(session_id, cycle["cycle_id"], f"SYNTHESIS_EVIDENCE_INCOMPLETE: {exc}")
        prompt = persist_stage_prompt(self.rsm, session_id, cycle["cycle_id"], envelope, self.artifact_root)
        calls = self._invoke_concurrently(session, cycle["cycle_id"], ResearchState.SYNTHESIS, {"OPENAI": prompt})
        item = calls["OPENAI"]
        if not item["ok"]:
            self._record_call(session_id, cycle["cycle_id"], ResearchState.SYNTHESIS,
                              "OPENAI", prompt, item.get("result"), "FAILED", item["error"])
            return self._fail_cycle(session_id, cycle["cycle_id"], f"SYNTHESIS_FAILED: {item['error']}")
        result = item["result"]
        try:
            synthesis = ResearchSynthesisSchema.model_validate(
                self._coerce_list_fields(ResearchSynthesisSchema, self._extract_json(result.get("output")))
            ).model_dump()
            self._record_successful_output(session_id, cycle["cycle_id"], ResearchState.SYNTHESIS,
                                           "OPENAI", prompt, result, synthesis)
        except Exception as exc:
            error = f"INVALID_SYNTHESIS: {type(exc).__name__}: {exc}"
            self._record_invalid_output(session_id, cycle["cycle_id"], ResearchState.SYNTHESIS,
                                        "OPENAI", prompt, result, error)
            return self._fail_cycle(session_id, cycle["cycle_id"], error)
        self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING,
                                  details="Codex HEAD synthesized proposals and critiques without majority voting")
        return self.session_summary(session_id, message="통합 가설이 생성되어 실험 계획 단계로 전환했습니다.")

    def run_through_synthesis(self, session_id: str) -> Dict[str, Any]:
        for operation in (self.run_independent_research, self.run_cross_critique, self.run_synthesis):
            result = operation(session_id)
            if result["human_required"]:
                return result
        return result

    # -------------------------------------------------------------- V3.0-E experiment integration

    def plan_experiment(self, session_id: str) -> Dict[str, Any]:
        """Translate persisted synthesis into a validated NORMAL OrderSchema without executing Builder."""
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.EXPERIMENT_PLANNING:
            raise ValueError("session is not at EXPERIMENT_PLANNING")
        existing = self.rsm.get_experiment(cycle["cycle_id"])
        if existing and existing["status"] == "PLANNED":
            return {**self.session_summary(session_id, message="저장된 실험 ORDER를 재사용합니다."),
                    "experiment": existing}

        synthesis_rows = self.rsm.get_stage_outputs(cycle["cycle_id"], ResearchState.SYNTHESIS)
        if len(synthesis_rows) != 1 or synthesis_rows[0]["domain"] != "OPENAI":
            return self._fail_cycle(session_id, cycle["cycle_id"], "EXPERIMENT_PLAN_BLOCKED: synthesis evidence missing")
        hypothesis_hash = self._hypothesis_sha256(synthesis_rows[0]["payload"].get("synthesized_hypothesis", ""))
        if self.rsm.has_memory_hypothesis(session_id, hypothesis_hash):
            return self._fail_cycle(
                session_id, cycle["cycle_id"], "REPEATED_HYPOTHESIS: prior research memory already rejected or tested this hypothesis"
            )
        project_row = self.rsm.get_project_evidence(session_id)
        if not project_row:
            return self._fail_cycle(session_id, cycle["cycle_id"], "EXPERIMENT_PLAN_BLOCKED: project evidence missing")

        envelope = build_experiment_order_prompt(
            session["moonshot"], synthesis_rows[0]["payload"], self._project_context(session["project_path"]),
            session["project_type"], project_row["baseline_state"],
            verification_command=self._verification_command(session["project_path"]),
        )
        prompt = persist_stage_prompt(self.rsm, session_id, cycle["cycle_id"], envelope, self.artifact_root)
        calls = self._invoke_concurrently(
            session, cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING, {"OPENAI": prompt}
        )
        item = calls["OPENAI"]
        if not item["ok"]:
            self._record_call(session_id, cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING,
                              "OPENAI", prompt, item.get("result"), "FAILED", item["error"])
            return self._fail_cycle(session_id, cycle["cycle_id"], f"EXPERIMENT_PLAN_FAILED: {item['error']}")
        result = item["result"]
        try:
            order = OrderSchema.model_validate(self._extract_json(result.get("output")))
            violations = SafetyPolicy.validate_order_paths(order)
            if violations:
                raise ValueError("ORDER_PATH_VIOLATION: " + "; ".join(violations))
            payload = order.model_dump()
            self._record_successful_output(
                session_id, cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING,
                "OPENAI", prompt, result, payload,
            )
        except Exception as exc:
            error = f"INVALID_EXPERIMENT_ORDER: {type(exc).__name__}: {exc}"
            self._record_invalid_output(session_id, cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING,
                                        "OPENAI", prompt, result, error)
            return self._fail_cycle(session_id, cycle["cycle_id"], error)

        row = self.rsm.get_stage_outputs(cycle["cycle_id"], ResearchState.EXPERIMENT_PLANNING)[0]
        self.rsm.record_experiment_plan(
            cycle["cycle_id"], payload, row["artifact_path"], row["output_sha256"],
            session["project_type"], project_row["baseline_state"],
        )
        return {**self.session_summary(session_id, message="통합 가설을 안전한 실험 ORDER로 변환했습니다."),
                "experiment": self.rsm.get_experiment(cycle["cycle_id"])}

    def _experiment_settings(self) -> Dict[str, Any]:
        if self.experiment_config is not None:
            return self.experiment_config
        with (SOURCE_ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _verification_command(self, project_path: str) -> Any:
        """The test command the verifier will run for this project (per-project setting first)."""
        config = self._experiment_settings()
        for root, settings in (config.get("projects") or {}).items():
            if Path(root).resolve() == Path(project_path).resolve():
                command = ((settings or {}).get("verification") or {}).get("test_command")
                if command:
                    return command
        return (config.get("verification") or {}).get("test_command")

    def _default_experiment_orchestrator(self, project_evidence: Dict[str, Any],
                                         baseline_evidence: Optional[Dict[str, Any]]):
        config = self.experiment_config
        if config is None:
            with (SOURCE_ROOT / "config.yaml").open("r", encoding="utf-8") as handle:
                config = yaml.safe_load(handle) or {}
        return TriadExperimentOrchestrator(
            config=config,
            greenfield_project_evidence=project_evidence if baseline_evidence else None,
            greenfield_baseline_evidence=baseline_evidence,
        )

    def execute_experiment(self, session_id: str, builder_fn=None) -> Dict[str, Any]:
        """Run a planned experiment through the existing NORMAL safety pipeline and mirror its evidence."""
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.EXPERIMENT_PLANNING:
            raise ValueError("session is not at EXPERIMENT_PLANNING")
        experiment = self.rsm.get_experiment(cycle["cycle_id"])
        if not experiment or experiment["status"] != "PLANNED":
            return self._fail_cycle(session_id, cycle["cycle_id"], "EXPERIMENT_ORDER_MISSING")
        try:
            order = OrderSchema.model_validate(experiment["order"])
        except Exception as exc:
            return self._fail_cycle(session_id, cycle["cycle_id"], f"EXPERIMENT_ORDER_INVALID: {exc}")
        violations = SafetyPolicy.validate_order_paths(order)
        if violations:
            return self._fail_cycle(
                session_id, cycle["cycle_id"], "ORDER_PATH_VIOLATION: " + "; ".join(violations)
            )

        project_row = self.rsm.get_project_evidence(session_id)
        if not project_row:
            return self._fail_cycle(session_id, cycle["cycle_id"], "PROJECT_EVIDENCE_MISSING")
        baseline = None
        if session["project_type"] == PROJECT_TYPE_GREENFIELD:
            baseline = self.prepare_greenfield_baseline(session_id)
            if baseline.get("human_required"):
                return baseline
            project_row = self.rsm.get_project_evidence(session_id)

        try:
            if self.experiment_factory is not None:
                normal = self.experiment_factory(
                    project_evidence=project_row["evidence"], baseline_evidence=baseline
                )
            else:
                normal = self._default_experiment_orchestrator(project_row["evidence"], baseline)
        except Exception as exc:
            return self._fail_cycle(session_id, cycle["cycle_id"], f"EXPERIMENT_PIPELINE_SETUP_FAILED: {exc}")

        self.rsm.transition_cycle(
            cycle["cycle_id"], ResearchState.EXPERIMENT_BUILDING,
            details="validated ORDER handed to unchanged NORMAL safety pipeline",
        )
        requested_run_id = f"TRIAD-EXP-{uuid.uuid4().hex.upper()}"
        try:
            result = normal.run(
                session["project_path"], order.goal, run_id=requested_run_id,
                builder_fn=builder_fn, order=order,
            )
        except Exception as exc:
            self.rsm.record_experiment_result(
                cycle["cycle_id"], None, "FAILED", None, None,
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            stopped = self._fail_cycle(session_id, cycle["cycle_id"], f"EXPERIMENT_PIPELINE_EXCEPTION: {exc}")
            stopped["experiment"] = self.rsm.get_experiment(cycle["cycle_id"])
            return stopped

        run_id = result.get("run_id")
        run_row = normal.sm.get_run(run_id) if run_id else None
        final_state = result.get("final_state")
        self.rsm.record_experiment_result(
            cycle["cycle_id"], run_id, "COMPLETED" if final_state == NormalState.PASS else "FAILED",
            final_state, run_row.get("worktree_path") if run_row else None, result,
        )
        if final_state != NormalState.PASS:
            verification = result.get("verification") or {}
            builder = result.get("builder") or {}
            review = result.get("review") or {}
            scope_violations = int(result.get("scope_violations") or 0)
            # A clean _finalize() decision (no Scope Guard/protected-path/provider/busy/baseline halt reached
            # this experiment through _stop()) always carries scope_violations == 0 - traced every _stop()
            # call site in core/orchestrator.py to confirm every one of them passes a non-empty violations
            # list or falls back to a one-element [details] list, so this is a reliable "we actually reached
            # a real HEAD/Reviewer decision" signal, not a heuristic.
            verifier_rejected = (
                final_state == NormalState.FAIL
                and builder.get("status") == "COMPLETED"
                and verification.get("passed") is False
                and scope_violations == 0
            )
            reviewer_found_defect = (
                builder.get("status") == "COMPLETED"
                and verification.get("passed") is True
                and scope_violations == 0
                and review.get("verdict") == "changes_required"
                and not (review.get("p0") or [])
            )
            safe_research_failure = verifier_rejected or reviewer_found_defect
            if safe_research_failure:
                if verifier_rejected:
                    review_detail = "deterministic verification rejected the experiment"
                    message = "실험 검증은 실패했으며 성공으로 처리하지 않았습니다. 실패 증거를 독립 해석합니다."
                else:
                    review_detail = "reviewer found an ordinary implementation defect (no P0, no scope violation)"
                    message = "Reviewer가 통상적인 구현 결함을 발견해 PASS로 처리하지 않았습니다. 실패 증거를 독립 해석합니다."
                self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_VERIFYING,
                                          details="NORMAL deterministic verification PASS" if reviewer_found_defect
                                          else "deterministic verification rejected the experiment")
                self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_REVIEW, details=review_detail)
                self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.RESULT_INTERPRETATION,
                                          details="failed experiment retained as evidence for interpretation")
                return {
                    **self.session_summary(session_id, message=message),
                    "experiment": self.rsm.get_experiment(cycle["cycle_id"]),
                }
            stopped = self._fail_cycle(
                session_id, cycle["cycle_id"], f"EXPERIMENT_{final_state}: NORMAL safety pipeline did not PASS"
            )
            stopped["experiment"] = self.rsm.get_experiment(cycle["cycle_id"])
            return stopped

        self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_VERIFYING,
                                  details="NORMAL deterministic verification PASS")
        self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.EXPERIMENT_REVIEW,
                                  details="NORMAL reviewer and final evidence gate PASS")
        self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.RESULT_INTERPRETATION,
                                  details="experiment evidence ready for independent interpretation")
        return {**self.session_summary(session_id, message="실험이 안전 파이프라인을 통과했습니다."),
                "experiment": self.rsm.get_experiment(cycle["cycle_id"])}

    # -------------------------------------------------------------- V3.0-F result interpretation + memory

    def _interpretation_evidence(self, session_id: str, cycle_id: str):
        synthesis_rows = self.rsm.get_stage_outputs(cycle_id, ResearchState.SYNTHESIS)
        experiment = self.rsm.get_experiment(cycle_id)
        if len(synthesis_rows) != 1 or synthesis_rows[0]["domain"] != "OPENAI":
            raise ValueError("SYNTHESIS_EVIDENCE_MISSING")
        if not experiment or not experiment.get("result") or experiment.get("status") not in {"COMPLETED", "FAILED"}:
            raise ValueError("EXPERIMENT_EVIDENCE_MISSING")
        return synthesis_rows[0], experiment

    def run_result_interpretation(self, session_id: str) -> Dict[str, Any]:
        """Run the first result interpretation concurrently with zero peer interpretation disclosure."""
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.RESULT_INTERPRETATION:
            raise ValueError("session is not at RESULT_INTERPRETATION")
        try:
            synthesis_row, experiment = self._interpretation_evidence(session_id, cycle["cycle_id"])
        except Exception as exc:
            return self._fail_cycle(session_id, cycle["cycle_id"], f"RESULT_INTERPRETATION_BLOCKED: {exc}")
        domains = {
            row["domain"] for row in self.rsm.get_participants(cycle["cycle_id"])
            if row["status"] != PARTICIPANT_STATUS_UNAVAILABLE
        }
        existing = self.rsm.get_stage_outputs(cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE)
        if len(existing) == len(domains) and {row["domain"] for row in existing} == domains:
            return {**self.session_summary(session_id, message="저장된 독립 결과 해석을 재사용합니다."),
                    "interpretation_substage": INTERPRETATION_CROSS_STAGE}
        if existing:
            return self._fail_cycle(session_id, cycle["cycle_id"], "RESULT_INTERPRETATION_EVIDENCE_PARTIAL")
        if len(domains) < 2:
            return self._fail_cycle(session_id, cycle["cycle_id"], "RESULT_INTERPRETATION_REQUIRES_TWO_PROVIDERS")

        prompts = {}
        for domain in sorted(domains):
            envelope = build_independent_interpretation_prompt(
                domain, session["moonshot"], synthesis_row["payload"], experiment
            )
            prompts[domain] = persist_stage_prompt(
                self.rsm, session_id, cycle["cycle_id"], envelope, self.artifact_root
            )
        calls = self._invoke_concurrently(
            session, cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE, prompts
        )
        failures = []
        for domain in sorted(domains):
            item = calls[domain]
            if not item["ok"]:
                failures.append(f"{domain}: {item['error']}")
                self._record_call(session_id, cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE,
                                  domain, prompts[domain], item.get("result"), "FAILED", item["error"])
                continue
            result = item["result"]
            try:
                payload = ResearchInterpretationSchema.model_validate(
                    self._coerce_list_fields(ResearchInterpretationSchema, self._extract_json(result.get("output")))
                ).model_dump()
                self._record_successful_output(
                    session_id, cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE,
                    domain, prompts[domain], result, payload,
                )
            except Exception as exc:
                error = f"INVALID_RESULT_INTERPRETATION: {type(exc).__name__}: {exc}"
                failures.append(f"{domain}: {error}")
                self._record_invalid_output(session_id, cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE,
                                            domain, prompts[domain], result, error)
        if failures:
            return self._fail_cycle(
                session_id, cycle["cycle_id"], "RESULT_INTERPRETATION_FAILED: " + "; ".join(failures)
            )
        return {**self.session_summary(session_id, message="세 AI의 독립 결과 해석이 완료되었습니다."),
                "interpretation_substage": INTERPRETATION_CROSS_STAGE}

    def run_interpretation_cross_critique(self, session_id: str) -> Dict[str, Any]:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.RESULT_INTERPRETATION:
            raise ValueError("session is not at RESULT_INTERPRETATION")
        try:
            _, experiment = self._interpretation_evidence(session_id, cycle["cycle_id"])
        except Exception as exc:
            return self._fail_cycle(session_id, cycle["cycle_id"], f"INTERPRETATION_CROSS_BLOCKED: {exc}")
        domains = {
            row["domain"] for row in self.rsm.get_participants(cycle["cycle_id"])
            if row["status"] != PARTICIPANT_STATUS_UNAVAILABLE
        }
        interpretations = {
            row["domain"]: row["payload"] for row in self.rsm.get_stage_outputs(
                cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE
            )
        }
        if set(interpretations) != domains or len(domains) < 2:
            return self._fail_cycle(session_id, cycle["cycle_id"], "INTERPRETATION_CROSS_BLOCKED: evidence incomplete")
        existing = self.rsm.get_stage_outputs(cycle["cycle_id"], INTERPRETATION_CROSS_STAGE)
        if len(existing) == len(domains) and {row["domain"] for row in existing} == domains:
            return {**self.session_summary(session_id, message="저장된 결과 해석 교차 비판을 재사용합니다."),
                    "interpretation_substage": RESEARCH_MEMORY_STAGE}
        if existing:
            return self._fail_cycle(session_id, cycle["cycle_id"], "INTERPRETATION_CROSS_EVIDENCE_PARTIAL")

        prompts = {}
        for domain in sorted(domains):
            envelope = build_interpretation_cross_prompt(domain, experiment, interpretations)
            prompts[domain] = persist_stage_prompt(
                self.rsm, session_id, cycle["cycle_id"], envelope, self.artifact_root
            )
        calls = self._invoke_concurrently(session, cycle["cycle_id"], INTERPRETATION_CROSS_STAGE, prompts)
        failures = []
        for domain in sorted(domains):
            item = calls[domain]
            if not item["ok"]:
                failures.append(f"{domain}: {item['error']}")
                self._record_call(session_id, cycle["cycle_id"], INTERPRETATION_CROSS_STAGE,
                                  domain, prompts[domain], item.get("result"), "FAILED", item["error"])
                continue
            result = item["result"]
            try:
                payload = InterpretationCritiqueSchema.model_validate(
                    self._coerce_list_fields(InterpretationCritiqueSchema, self._extract_json(result.get("output")))
                ).model_dump()
                self._record_successful_output(
                    session_id, cycle["cycle_id"], INTERPRETATION_CROSS_STAGE,
                    domain, prompts[domain], result, payload,
                )
            except Exception as exc:
                error = f"INVALID_INTERPRETATION_CRITIQUE: {type(exc).__name__}: {exc}"
                failures.append(f"{domain}: {error}")
                self._record_invalid_output(session_id, cycle["cycle_id"], INTERPRETATION_CROSS_STAGE,
                                            domain, prompts[domain], result, error)
        if failures:
            return self._fail_cycle(
                session_id, cycle["cycle_id"], "INTERPRETATION_CROSS_FAILED: " + "; ".join(failures)
            )
        return {**self.session_summary(session_id, message="결과 해석 교차 비판이 완료되었습니다."),
                "interpretation_substage": RESEARCH_MEMORY_STAGE}

    def consolidate_research_memory(self, session_id: str) -> Dict[str, Any]:
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] != ResearchState.RESULT_INTERPRETATION:
            raise ValueError("session is not at RESULT_INTERPRETATION")
        synthesis_row, experiment = self._interpretation_evidence(session_id, cycle["cycle_id"])
        interpretations_rows = self.rsm.get_stage_outputs(cycle["cycle_id"], INTERPRETATION_INDEPENDENT_STAGE)
        critiques_rows = self.rsm.get_stage_outputs(cycle["cycle_id"], INTERPRETATION_CROSS_STAGE)
        interpretations = {row["domain"]: row["payload"] for row in interpretations_rows}
        critiques = {row["domain"]: row["payload"] for row in critiques_rows}
        domains = {
            row["domain"] for row in self.rsm.get_participants(cycle["cycle_id"])
            if row["status"] != PARTICIPANT_STATUS_UNAVAILABLE
        }
        if set(interpretations) != domains or set(critiques) != domains:
            return self._fail_cycle(session_id, cycle["cycle_id"], "RESEARCH_MEMORY_BLOCKED: interpretation evidence incomplete")
        if self.rsm.list_research_memory(session_id) and any(
            row["cycle_id"] == cycle["cycle_id"] for row in self.rsm.list_research_memory(session_id)
        ):
            return {**self.session_summary(session_id, message="저장된 Research Memory를 재사용합니다."),
                    "research_memory": self.rsm.list_research_memory(session_id)}

        envelope = build_research_memory_prompt(synthesis_row["payload"], experiment, interpretations, critiques)
        prompt = persist_stage_prompt(self.rsm, session_id, cycle["cycle_id"], envelope, self.artifact_root)
        calls = self._invoke_concurrently(session, cycle["cycle_id"], RESEARCH_MEMORY_STAGE, {"OPENAI": prompt})
        item = calls["OPENAI"]
        if not item["ok"]:
            self._record_call(session_id, cycle["cycle_id"], RESEARCH_MEMORY_STAGE,
                              "OPENAI", prompt, item.get("result"), "FAILED", item["error"])
            return self._fail_cycle(session_id, cycle["cycle_id"], f"RESEARCH_MEMORY_FAILED: {item['error']}")
        result = item["result"]
        try:
            memory = ResearchMemorySchema.model_validate(
                self._coerce_list_fields(ResearchMemorySchema, self._extract_json(result.get("output")))
            ).model_dump()
            canonical_hypothesis = synthesis_row["payload"]["synthesized_hypothesis"]
            if self._hypothesis_sha256(memory["hypothesis"]) != self._hypothesis_sha256(canonical_hypothesis):
                raise ValueError("memory hypothesis does not match the tested synthesis")
            if memory["milestone_complete"] and memory["no_progress"]:
                raise ValueError("milestone_complete and no_progress cannot both be true")
            if not memory["next_direction"].strip() and not memory["milestone_complete"]:
                raise ValueError("next_direction is required for continuing research")
            hypothesis_hash = self._hypothesis_sha256(canonical_hypothesis)
            if self.rsm.has_memory_hypothesis(session_id, hypothesis_hash):
                raise ValueError("REPEATED_HYPOTHESIS")
            self._record_successful_output(
                session_id, cycle["cycle_id"], RESEARCH_MEMORY_STAGE, "OPENAI", prompt, result, memory
            )
            source = {
                "synthesis_sha256": synthesis_row["output_sha256"],
                "experiment_order_sha256": experiment["order_sha256"],
                "interpretation_sha256": {row["domain"]: row["output_sha256"] for row in interpretations_rows},
                "critique_sha256": {row["domain"]: row["output_sha256"] for row in critiques_rows},
            }
            self.rsm.record_research_memory(session_id, cycle["cycle_id"], hypothesis_hash, memory, source)
        except Exception as exc:
            error = f"INVALID_RESEARCH_MEMORY: {type(exc).__name__}: {exc}"
            self._record_invalid_output(session_id, cycle["cycle_id"], RESEARCH_MEMORY_STAGE,
                                        "OPENAI", prompt, result, error)
            return self._fail_cycle(session_id, cycle["cycle_id"], error)

        if memory["milestone_complete"]:
            self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.MILESTONE_COMPLETE,
                                      details="evidence-backed milestone complete")
            self.rsm.set_session_status(session_id, SESSION_STATUS_MILESTONE_COMPLETE, memory["lesson"])
        elif memory["no_progress"]:
            self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.RESEARCH_STOPPED,
                                      details="no-progress detector stopped autonomous research")
            self.rsm.set_session_status(session_id, SESSION_STATUS_STOPPED, "NO_PROGRESS")
        elif int(cycle["cycle_number"]) >= int(session["cycle_budget"]):
            self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.RESEARCH_STOPPED,
                                      details="cycle budget exhausted")
            self.rsm.set_session_status(session_id, SESSION_STATUS_STOPPED, "CYCLE_BUDGET_EXHAUSTED")
        else:
            self.rsm.increment_cycles_completed(session_id)
            self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.NEXT_CYCLE,
                                      details="memory persisted; next evidence-driven cycle allowed")
        return {**self.session_summary(session_id, message="Research Memory가 증거와 함께 저장되었습니다."),
                "research_memory": self.rsm.list_research_memory(session_id)}

    def retrieve_research_memory_facts(self, session_id: str, query: str = "", limit: int = 5) -> list:
        """Return compact relevant facts, never the full raw research/call history."""
        if limit < 1:
            return []
        terms = {term for term in " ".join(str(query).casefold().split()).split(" ") if len(term) > 1}
        scored = []
        for row in self.rsm.list_research_memory(session_id):
            payload = row["payload"]
            searchable = " ".join([
                payload.get("hypothesis", ""), payload.get("lesson", ""),
                payload.get("next_direction", ""), " ".join(payload.get("open_questions") or []),
            ]).casefold()
            score = sum(1 for term in terms if term in searchable)
            scored.append((score, row["id"], {
                "hypothesis": payload["hypothesis"], "result": payload["result"],
                "lesson": payload["lesson"], "do_not_repeat": payload["do_not_repeat"],
                "open_questions": payload["open_questions"], "next_direction": payload["next_direction"],
                "evidence_sha256": row["hypothesis_sha256"],
            }))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored[:limit] if item[0] > 0 or not terms]

    def start_next_cycle(self, session_id: str, allow_degraded: bool = False,
                         verified_evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create the next bounded cycle, rechecking provider usability and retrieving only relevant memory."""
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycles = self.rsm.list_cycles(session_id)
        if not cycles or cycles[-1]["stage"] != ResearchState.NEXT_CYCLE:
            raise ValueError("session is not awaiting NEXT_CYCLE")
        if session["status"] != SESSION_STATUS_ACTIVE:
            raise ValueError("research session is not ACTIVE")
        next_number = int(cycles[-1]["cycle_number"]) + 1
        if next_number > int(session["cycle_budget"]) or self.rsm.cycle_budget_exhausted(session_id):
            return self.stop_session(session_id, "CYCLE_BUDGET_EXHAUSTED")

        readiness = assess_triad_readiness(self.pm)
        cycle_id = f"{session_id}-C{next_number}"
        self.rsm.create_cycle(cycle_id, session_id, next_number)
        self.rsm.register_participants(cycle_id, {
            domain: (PARTICIPANT_STATUS_PENDING if domain in readiness["usable"] else PARTICIPANT_STATUS_UNAVAILABLE)
            for domain in ALL_TRIAD_DOMAINS
        })
        approved = readiness["state"] == TRIAD_DEGRADED and bool(allow_degraded)
        self.rsm.record_intake(session_id, readiness, degraded_approved=approved)
        facts = self.retrieve_research_memory_facts(session_id)
        if readiness["state"] == TRIAD_READY or approved:
            prompts = self._activate_independent_stage(
                session_id, cycle_id, readiness, verified_evidence, facts
            )
            message = (
                "다음 cycle의 독립 연구를 시작했습니다."
                if readiness["state"] == TRIAD_READY
                else "현재 2/3 AI만 사용 가능합니다. 명시적 승인으로 다음 degraded cycle을 시작했습니다."
            )
            return self.session_summary(session_id, message=message, prompt_artifacts=prompts)

        if readiness["state"] == TRIAD_DEGRADED:
            reason = "현재 2/3 AI만 사용 가능합니다. 다음 cycle의 degraded 진행 승인이 필요합니다."
        else:
            reason = "TRIAD_BLOCKED: 두 개 이상의 provider를 사용할 수 없어 다음 cycle을 시작할 수 없습니다."
        self.rsm.set_session_status(session_id, SESSION_STATUS_HUMAN_REQUIRED, reason)
        return self.session_summary(session_id, message=reason, human_required=True, prompt_artifacts=[])

    def stop_session(self, session_id: str, reason: str = "USER_STOP") -> Dict[str, Any]:
        """Idempotent explicit user stop; never starts another provider call."""
        session = self.rsm.get_session(session_id)
        if not session:
            raise ValueError(f"unknown research session: {session_id}")
        cycle = self.rsm.list_cycles(session_id)[-1]
        if cycle["stage"] not in {
            ResearchState.MILESTONE_COMPLETE, ResearchState.HUMAN_REQUIRED, ResearchState.RESEARCH_STOPPED
        }:
            self.rsm.transition_cycle(cycle["cycle_id"], ResearchState.RESEARCH_STOPPED, details=reason)
        self.rsm.set_session_status(session_id, SESSION_STATUS_STOPPED, reason)
        return self.session_summary(session_id, message=f"연구를 중단했습니다: {reason}")
