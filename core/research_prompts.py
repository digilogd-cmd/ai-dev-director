"""Independent-research prompt construction and artifact persistence for TRIAD.

Milestone V3.0-B deliberately builds prompts without invoking any provider.  The API accepts only common
inputs available to every researcher; peer proposals and critiques are structurally forbidden until the
later CROSS_CRITIQUE stage.
"""
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from core.db_path import SOURCE_ROOT
from core.research_state import (
    INTERPRETATION_CROSS_STAGE,
    INTERPRETATION_INDEPENDENT_STAGE,
    RESEARCH_MEMORY_STAGE,
    ResearchState,
    ResearchStateMachine,
)
from core.triad_provider import ALL_TRIAD_DOMAINS


FORBIDDEN_INDEPENDENT_KEYS = {
    "proposal", "proposals", "peer_proposal", "peer_proposals",
    "critique", "critiques", "cross_critique", "cross_critiques",
    "synthesis", "researcher_output", "researcher_outputs",
}
FORBIDDEN_INTERPRETATION_KEYS = {
    "interpretation", "interpretations", "peer_interpretation", "peer_interpretations",
    "interpretation_critique", "interpretation_critiques", "research_memory",
}
ALLOWED_SOURCE_KINDS = (
    "USER_CHALLENGE", "PROJECT_CONTEXT", "VERIFIED_EVIDENCE", "RESEARCH_MEMORY_FACTS"
)


class IndependentResearchContaminationError(ValueError):
    pass


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _find_forbidden_keys(value: Any, path: str = "$") -> List[str]:
    hits: List[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in FORBIDDEN_INDEPENDENT_KEYS:
                hits.append(f"{path}.{key}")
            hits.extend(_find_forbidden_keys(child, f"{path}.{key}"))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            hits.extend(_find_forbidden_keys(child, f"{path}[{index}]"))
    return hits


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"independent prompt input is not JSON serializable: {exc}") from exc


def build_independent_research_prompt(
    domain: str,
    user_challenge: str,
    project_context: Dict[str, Any],
    verified_evidence: Optional[Dict[str, Any]] = None,
    research_memory_facts: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a provider-specific prompt envelope made only from common, non-peer inputs."""
    normalized_domain = str(domain).upper()
    if normalized_domain not in ALL_TRIAD_DOMAINS:
        raise ValueError(f"unsupported TRIAD domain: {domain}")
    if not user_challenge or not str(user_challenge).strip():
        raise ValueError("user_challenge is required")

    sources: Dict[str, Any] = {
        "USER_CHALLENGE": str(user_challenge),
        "PROJECT_CONTEXT": dict(project_context or {}),
        "VERIFIED_EVIDENCE": dict(verified_evidence or {}),
        "RESEARCH_MEMORY_FACTS": list(research_memory_facts or []),
    }
    forbidden = _find_forbidden_keys(sources)
    if forbidden:
        raise IndependentResearchContaminationError(
            "peer research material is forbidden during INDEPENDENT_RESEARCH: " + ", ".join(forbidden)
        )

    instructions = (
        "Act as an independent research scientist. Develop your own falsifiable proposal for the user "
        "challenge. Do not request, infer, or rely on any peer researcher's proposal or critique. Return "
        "structured JSON with: hypothesis, rationale, novelty, assumptions, falsifiable_prediction, "
        "proposed_experiment, evidence_needed, failure_modes, kill_criteria, risks, confidence, "
        "freeform_insight. Treat verified evidence as evidence, not as a prescribed answer."
    )
    prompt_payload = {
        "stage": ResearchState.INDEPENDENT_RESEARCH,
        "research_role": "RESEARCHER",
        "provider_domain": normalized_domain,
        "instructions": instructions,
        "sources": sources,
        "source_manifest": list(ALLOWED_SOURCE_KINDS),
        "output_contract": {
            "hypothesis": "string", "rationale": "string", "novelty": "string",
            "assumptions": ["string"], "falsifiable_prediction": "string",
            "proposed_experiment": "string", "evidence_needed": ["string"],
            "failure_modes": ["string"], "kill_criteria": ["string"], "risks": ["string"],
            "confidence": 0.5, "freeform_insight": "string",
        },
    }
    prompt_payload["instructions"] += (
        " The output_contract is a strict type contract, not a content example. Return exactly one JSON object "
        "with exactly those keys. Every field shown as 'string' MUST be one JSON string, never an array or "
        "object; [\"string\"] means an array of strings; confidence MUST be one number from 0 to 1. Do not add keys."
    )
    prompt_text = _stable_json(prompt_payload)
    return {
        "schema_version": "3.0-B",
        "stage": ResearchState.INDEPENDENT_RESEARCH,
        "provider_domain": normalized_domain,
        "source_manifest": list(ALLOWED_SOURCE_KINDS),
        "contamination_guard": "PASS",
        "prompt": prompt_payload,
        "prompt_text": prompt_text,
        "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(prompt_text),
        "prompt_bytes": len(prompt_text.encode("utf-8")),
    }


def persist_independent_prompt_artifacts(
    state_machine: ResearchStateMachine,
    session_id: str,
    cycle_id: str,
    domains: Iterable[str],
    user_challenge: str,
    project_context: Dict[str, Any],
    verified_evidence: Optional[Dict[str, Any]] = None,
    research_memory_facts: Optional[Iterable[Dict[str, Any]]] = None,
    artifact_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Persist one isolated JSON prompt per domain and record its measured size/hash in the research DB."""
    root = Path(artifact_root) if artifact_root is not None else SOURCE_ROOT / "research"
    target_dir = (root / session_id / cycle_id / "independent").resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(target_dir)]) != str(root_resolved):
        raise ValueError("research artifact path escapes artifact_root")
    target_dir.mkdir(parents=True, exist_ok=True)

    records: List[Dict[str, Any]] = []
    for raw_domain in sorted(set(domains)):
        envelope = build_independent_research_prompt(
            raw_domain, user_challenge, project_context, verified_evidence, research_memory_facts
        )
        domain = envelope["provider_domain"]
        path = target_dir / f"{domain.lower()}_prompt.json"
        temp_path = path.with_suffix(path.suffix + ".tmp")
        encoded = json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True)
        temp_path.write_text(encoded, encoding="utf-8")
        os.replace(temp_path, path)
        state_machine.record_prompt_artifact(
            cycle_id=cycle_id,
            domain=domain,
            stage=ResearchState.INDEPENDENT_RESEARCH,
            artifact_path=str(path),
            prompt_sha256=envelope["prompt_sha256"],
            prompt_chars=envelope["prompt_chars"],
            prompt_bytes=envelope["prompt_bytes"],
        )
        records.append({**envelope, "artifact_path": str(path)})
    return records


def build_cross_critique_prompt(domain: str, user_challenge: str,
                                proposals: Dict[str, Dict[str, Any]], degraded: bool = False) -> Dict[str, Any]:
    normalized_domain = str(domain).upper()
    if normalized_domain not in ALL_TRIAD_DOMAINS:
        raise ValueError(f"unsupported TRIAD domain: {domain}")
    if len(proposals) < 2:
        raise ValueError("cross critique requires at least two completed proposals")
    payload = {
        "stage": ResearchState.CROSS_CRITIQUE,
        "research_role": "RESEARCHER_CRITIC",
        "provider_domain": normalized_domain,
        "user_challenge": user_challenge,
        "degraded_research": bool(degraded),
        "proposals": proposals,
        "output_contract": {
            "strongest_point": "string", "weakest_assumption": "string", "logical_flaw": "string",
            "hidden_risk": "string", "falsification_method": "string", "experiment_flaw": "string",
            "useful_combination": "string", "self_critique": "string",
            "target_observations": {"target_name": "string"},
        },
        "instructions": (
            "Critique every disclosed proposal, including your own. Return structured JSON with: "
            "strongest_point, weakest_assumption, logical_flaw, hidden_risk, falsification_method, "
            "experiment_flaw, useful_combination, self_critique, target_observations. Prefer evidence and "
            "falsification over agreement or majority voting. The output_contract is strict: every value shown "
            "as string MUST be one JSON string, target_observations MUST map strings to strings, and no extra keys are allowed."
        ),
    }
    text = _stable_json(payload)
    return {
        "schema_version": "3.0-C", "stage": ResearchState.CROSS_CRITIQUE,
        "provider_domain": normalized_domain, "prompt": payload, "prompt_text": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(text), "prompt_bytes": len(text.encode("utf-8")),
    }


def build_synthesis_prompt(user_challenge: str, proposals: Dict[str, Dict[str, Any]],
                           critiques: Dict[str, Dict[str, Any]], degraded: bool = False) -> Dict[str, Any]:
    if len(proposals) < 2 or len(critiques) != len(proposals):
        raise ValueError("synthesis requires one critique for every completed proposal")
    payload = {
        "stage": ResearchState.SYNTHESIS,
        "research_role": "HEAD_SYNTHESIZER",
        "provider_domain": "OPENAI",
        "user_challenge": user_challenge,
        "degraded_research": bool(degraded),
        "proposals": proposals,
        "critiques": critiques,
        "output_contract": {
            "selection_type": "A", "title": "string", "synthesized_hypothesis": "string",
            "rationale": "string", "source_domains": ["OPENAI"], "critique_lessons": ["string"],
            "falsifiable_prediction": "string", "proposed_experiment": "string",
            "kill_criteria": ["string"], "risks": ["string"], "confidence": 0.5,
        },
        "instructions": (
            "Synthesize the strongest falsifiable next hypothesis. Do not prefer the OPENAI proposal and do "
            "not use majority vote. You may select A/B/C, combine proposals, discard all, or derive a new D. "
            "Return structured JSON with: selection_type, title, synthesized_hypothesis, rationale, "
            "source_domains, critique_lessons, falsifiable_prediction, proposed_experiment, kill_criteria, "
            "risks, confidence. The output_contract is strict: string means one JSON string, arrays contain "
            "strings only, confidence is one number from 0 to 1, selection_type is one of "
            "A/B/C/A+B/A+C/B+C/A+B+C/DISCARD_ALL/NEW_D, and no extra keys are allowed."
        ),
    }
    text = _stable_json(payload)
    return {
        "schema_version": "3.0-C", "stage": ResearchState.SYNTHESIS,
        "provider_domain": "OPENAI", "prompt": payload, "prompt_text": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(text), "prompt_bytes": len(text.encode("utf-8")),
    }


def build_experiment_order_prompt(user_challenge: str, synthesis: Dict[str, Any],
                                  project_context: Dict[str, Any], project_type: str,
                                  baseline_state: str, verification_command: Any = None) -> Dict[str, Any]:
    """Ask Codex HEAD to translate a synthesis into a safety-bounded NORMAL OrderSchema."""
    if not synthesis or not synthesis.get("synthesized_hypothesis"):
        raise ValueError("experiment planning requires persisted synthesis evidence")
    payload = {
        "stage": ResearchState.EXPERIMENT_PLANNING,
        "research_role": "HEAD_EXPERIMENT_PLANNER",
        "provider_domain": "OPENAI",
        "user_challenge": user_challenge,
        "synthesis": synthesis,
        "project_context": project_context,
        "project_type": project_type,
        "baseline_state": baseline_state,
        "verification_command": verification_command,
        "output_contract": {
            "goal": "string", "allowed_paths": ["relative/exact-file"], "protected_paths": ["relative/path"],
            "acceptance_criteria": ["string"], "max_files_changed": 4, "max_diff_lines": 400,
            "allowed_deletions": [], "context": "string", "risk_level": "LOW",
            "requires_human_approval": False,
        },
        "instructions": (
            "Translate the synthesized hypothesis into one minimal falsifiable coding experiment. Return only "
            "a JSON object matching OrderSchema with: goal, allowed_paths, protected_paths, "
            "acceptance_criteria, max_files_changed, max_diff_lines, allowed_deletions, context, risk_level, "
            "requires_human_approval. Use relative repository paths only. Do not weaken protected paths, "
            "request broad filesystem access, skip tests, or claim GREENFIELD is a baseline PASS. Include "
            "post-build tests in the acceptance criteria. The Director verifies the experiment only by running "
            "verification_command: every test must be placed where that exact command collects it, because a run "
            "that collects no tests fails. For a GREENFIELD experiment, select only one narrow "
            "mechanism from the synthesis: enumerate at most four exact file paths (no directory or glob scope), "
            "use at most six acceptance criteria, set max_files_changed no higher than 4 and max_diff_lines no "
            "higher than 400, and do not design a production framework or attempt every synthesized feature. "
            "The output_contract is strict: paths and acceptance "
            "criteria are arrays of strings, caps are integers, context is a string or null, risk_level is "
            "LOW/MEDIUM/HIGH, requires_human_approval is boolean, and no extra keys are allowed."
        ),
    }
    text = _stable_json(payload)
    return {
        "schema_version": "3.0-E", "stage": ResearchState.EXPERIMENT_PLANNING,
        "provider_domain": "OPENAI", "prompt": payload, "prompt_text": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(text), "prompt_bytes": len(text.encode("utf-8")),
    }


def build_independent_interpretation_prompt(domain: str, user_challenge: str,
                                            synthesis: Dict[str, Any], experiment: Dict[str, Any]) -> Dict[str, Any]:
    normalized_domain = str(domain).upper()
    if normalized_domain not in ALL_TRIAD_DOMAINS:
        raise ValueError(f"unsupported TRIAD domain: {domain}")
    sources = {"user_challenge": user_challenge, "synthesis": synthesis, "experiment_evidence": experiment}
    hits = []
    for path in _find_forbidden_keys(sources):
        if any(_normalized_key(part) in FORBIDDEN_INTERPRETATION_KEYS for part in path.split(".")):
            hits.append(path)
    # The general independent guard does not know interpretation-specific names, so scan them explicitly.
    def scan(value: Any, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _normalized_key(key) in FORBIDDEN_INTERPRETATION_KEYS:
                    hits.append(f"{path}.{key}")
                scan(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                scan(child, f"{path}[{index}]")
    scan(sources)
    if hits:
        raise IndependentResearchContaminationError(
            "peer interpretation material is forbidden during independent result interpretation: "
            + ", ".join(sorted(set(hits)))
        )
    payload = {
        "stage": INTERPRETATION_INDEPENDENT_STAGE,
        "research_role": "RESULT_INTERPRETER",
        "provider_domain": normalized_domain,
        **sources,
        "output_contract": {
            "hypothesis_assessment": "inconclusive", "reasoning": "string", "experiment_flaw": "string",
            "unexpected_observations": ["string"], "hidden_confounders": ["string"],
            "next_experiment": "string", "abandon_direction": False,
            "new_possibilities": ["string"], "confidence": 0.5,
        },
        "instructions": (
            "Interpret the experiment evidence independently. Do not infer or request peer interpretations. "
            "A failed test is evidence, not success. Return JSON with: hypothesis_assessment "
            "(supported/rejected/inconclusive), reasoning, experiment_flaw, unexpected_observations, "
            "hidden_confounders, next_experiment, abandon_direction, new_possibilities, confidence. The "
            "output_contract is strict: string fields are single strings, list fields contain strings only, "
            "abandon_direction is boolean, confidence is a number from 0 to 1, and no extra keys are allowed."
        ),
    }
    text = _stable_json(payload)
    return {
        "schema_version": "3.0-F", "stage": INTERPRETATION_INDEPENDENT_STAGE,
        "provider_domain": normalized_domain, "contamination_guard": "PASS",
        "prompt": payload, "prompt_text": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(text), "prompt_bytes": len(text.encode("utf-8")),
    }


def build_interpretation_cross_prompt(domain: str, experiment: Dict[str, Any],
                                      interpretations: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    normalized_domain = str(domain).upper()
    if normalized_domain not in ALL_TRIAD_DOMAINS:
        raise ValueError(f"unsupported TRIAD domain: {domain}")
    if len(interpretations) < 2:
        raise ValueError("interpretation cross critique requires at least two completed interpretations")
    payload = {
        "stage": INTERPRETATION_CROSS_STAGE,
        "research_role": "RESULT_INTERPRETATION_CRITIC",
        "provider_domain": normalized_domain,
        "experiment_evidence": experiment,
        "interpretations": interpretations,
        "output_contract": {
            "strongest_interpretation": "string", "weakest_inference": "string",
            "disagreements": ["string"], "hidden_confounders": ["string"], "evidence_quality": "string",
            "recommended_next_experiment": "string", "self_correction": "string",
        },
        "instructions": (
            "Cross-critique every result interpretation, including your own. Resolve disagreements using "
            "experiment evidence, not majority vote. Return JSON with: strongest_interpretation, "
            "weakest_inference, disagreements, hidden_confounders, evidence_quality, "
            "recommended_next_experiment, self_correction. The output_contract is strict: every string field "
            "is one JSON string, list fields contain strings only, and no extra keys are allowed."
        ),
    }
    text = _stable_json(payload)
    return {
        "schema_version": "3.0-F", "stage": INTERPRETATION_CROSS_STAGE,
        "provider_domain": normalized_domain, "prompt": payload, "prompt_text": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(text), "prompt_bytes": len(text.encode("utf-8")),
    }


def build_research_memory_prompt(synthesis: Dict[str, Any], experiment: Dict[str, Any],
                                 interpretations: Dict[str, Dict[str, Any]],
                                 critiques: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    if len(interpretations) < 2 or len(critiques) != len(interpretations):
        raise ValueError("memory consolidation requires complete interpretation and critique evidence")
    payload = {
        "stage": RESEARCH_MEMORY_STAGE,
        "research_role": "HEAD_MEMORY_CURATOR",
        "provider_domain": "OPENAI",
        "synthesis": synthesis,
        "experiment_evidence": experiment,
        "interpretations": interpretations,
        "interpretation_critiques": critiques,
        "output_contract": {
            "hypothesis": "string", "why_tried": "string", "result": "inconclusive", "lesson": "string",
            "rejected_ideas": ["string"], "negative_evidence": ["string"], "do_not_repeat": ["string"],
            "open_questions": ["string"], "next_direction": "string", "milestone_complete": False,
            "no_progress": False,
        },
        "instructions": (
            "Create a compact evidence-grounded research memory. Do not use majority vote and do not erase "
            "negative results. The hypothesis field must be an exact character-for-character copy of "
            "synthesis.synthesized_hypothesis: the tested hypothesis is a recorded fact, so never rephrase it. "
            "Return JSON with: hypothesis, why_tried, result "
            "(supported/rejected/inconclusive), lesson, rejected_ideas, negative_evidence, do_not_repeat, "
            "open_questions, next_direction, milestone_complete, no_progress. Mark no_progress only when the "
            "evidence provides no defensible next experiment. The output_contract is strict: string fields are "
            "single strings, list fields contain strings only, result is supported/rejected/inconclusive, the "
            "two flags are booleans, and no extra keys are allowed."
        ),
    }
    text = _stable_json(payload)
    return {
        "schema_version": "3.0-F", "stage": RESEARCH_MEMORY_STAGE,
        "provider_domain": "OPENAI", "prompt": payload, "prompt_text": text,
        "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "prompt_chars": len(text), "prompt_bytes": len(text.encode("utf-8")),
    }


def persist_stage_prompt(state_machine: ResearchStateMachine, session_id: str, cycle_id: str,
                         envelope: Dict[str, Any], artifact_root: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(artifact_root) if artifact_root is not None else SOURCE_ROOT / "research"
    stage_dir = str(envelope["stage"]).lower()
    target_dir = (root / session_id / cycle_id / stage_dir).resolve()
    root_resolved = root.resolve()
    if os.path.commonpath([str(root_resolved), str(target_dir)]) != str(root_resolved):
        raise ValueError("research artifact path escapes artifact_root")
    target_dir.mkdir(parents=True, exist_ok=True)
    domain = str(envelope["provider_domain"]).upper()
    path = target_dir / f"{domain.lower()}_prompt.json"
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, path)
    state_machine.record_prompt_artifact(
        cycle_id, domain, envelope["stage"], str(path), envelope["prompt_sha256"],
        envelope["prompt_chars"], envelope["prompt_bytes"],
    )
    return {**envelope, "artifact_path": str(path)}
