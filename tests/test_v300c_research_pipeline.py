"""V3.0-C: concurrent independent research, cross critique, and evidence-led synthesis."""
import json
import threading
import time
from pathlib import Path

from core.process_runner import ProcessTreeRunner
from core.research_orchestrator import ResearchOrchestrator
from core.research_runner import TriadResearchRunner
from core.research_state import ResearchState, ResearchStateMachine


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "src" / "system.py").write_text("CURRENT = True\n", encoding="utf-8")
    return project


def _proposal(domain):
    return {
        "hypothesis": f"{domain} hypothesis", "rationale": f"{domain} rationale", "novelty": "novel",
        "assumptions": ["a1"], "falsifiable_prediction": "metric improves", "proposed_experiment": "run trial",
        "evidence_needed": ["metric"], "failure_modes": ["noise"], "kill_criteria": ["metric falls"],
        "risks": ["bias"], "confidence": 0.6, "freeform_insight": f"insight-{domain}",
    }


def _critique(domain):
    return {
        "strongest_point": "testable", "weakest_assumption": "stationarity", "logical_flaw": "none proven",
        "hidden_risk": "selection bias", "falsification_method": "holdout", "experiment_flaw": "sample size",
        "useful_combination": "combine mechanisms", "self_critique": f"{domain} may be overconfident",
        "target_observations": {"OPENAI": "good", "ANTHROPIC": "risky", "GOOGLE": "novel"},
    }


def _synthesis():
    return {
        "selection_type": "NEW_D", "title": "Derived D", "synthesized_hypothesis": "combine causal checks",
        "rationale": "critique exposed a shared confound", "source_domains": ["ANTHROPIC", "GOOGLE"],
        "critique_lessons": ["use holdout"], "falsifiable_prediction": "holdout metric improves",
        "proposed_experiment": "controlled comparison", "kill_criteria": ["no holdout gain"],
        "risks": ["small sample"], "confidence": 0.7,
    }


class DeterministicCalls:
    def __init__(self, fail_domain=None, invalid_stage=None):
        self.fail_domain = fail_domain
        self.invalid_stage = invalid_stage
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def __call__(self, domain, prompt, stage, cwd):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append({"domain": domain, "stage": stage, "prompt": prompt, "cwd": cwd})
        time.sleep(0.04)
        try:
            if domain == self.fail_domain and stage == ResearchState.INDEPENDENT_RESEARCH:
                raise RuntimeError("forced provider failure")
            if stage == self.invalid_stage:
                output = "not-json"
            elif stage == ResearchState.INDEPENDENT_RESEARCH:
                output = json.dumps(_proposal(domain))
            elif stage == ResearchState.CROSS_CRITIQUE:
                output = json.dumps(_critique(domain))
            else:
                output = json.dumps(_synthesis())
            return {
                "output": output, "executable": f"stub-{domain.lower()}", "version": "test-1",
                "invocation": f"stub {stage}", "started_at": "2026-01-01T00:00:00",
                "ended_at": "2026-01-01T00:00:01", "duration_sec": 1.0, "exit_code": 0,
                "token_usage": "UNKNOWN", "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
            }
        finally:
            with self.lock:
                self.active -= 1


def _ready(tmp_path, provider_state, calls=None):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    calls = calls or DeterministicCalls()
    orch = ResearchOrchestrator(
        state_machine=ResearchStateMachine(), provider_manager=pm,
        artifact_root=tmp_path / "research", provider_call=calls,
    )
    session = orch.start_session(str(_project(tmp_path)), "Find a new falsifiable mechanism")
    return orch, session, calls


def test_1_independent_research_is_concurrent_and_undisclosed(tmp_path, provider_state):
    before = len(ProcessTreeRunner.blocked_real_cli_calls)
    orch, session, calls = _ready(tmp_path, provider_state)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.CROSS_CRITIQUE
    assert calls.max_active >= 2
    independent = [c for c in calls.calls if c["stage"] == ResearchState.INDEPENDENT_RESEARCH]
    assert len(independent) == 3
    for call in independent:
        prompt = json.loads(call["prompt"])
        assert "proposals" not in prompt and "critiques" not in prompt
        assert set(prompt["sources"]) == {
            "USER_CHALLENGE", "PROJECT_CONTEXT", "VERIFIED_EVIDENCE", "RESEARCH_MEMORY_FACTS"
        }
    assert len(ProcessTreeRunner.blocked_real_cli_calls) == before

    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    outputs = ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert {o["domain"] for o in outputs} == {"OPENAI", "ANTHROPIC", "GOOGLE"}
    provenance = ResearchStateMachine().get_call_provenance(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert len(provenance) == 3
    assert all(p["outcome"] == "SUCCESS" and p["token_usage"] == "UNKNOWN" for p in provenance)
    assert all(p["prompt_sha256"] and p["output_sha256"] and p["response_bytes"] > 0 for p in provenance)


def test_2_any_independent_failure_is_terminal_and_never_discloses_partial_work(tmp_path, provider_state):
    calls = DeterministicCalls(fail_domain="GOOGLE")
    orch, session, _ = _ready(tmp_path, provider_state, calls)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert not [c for c in calls.calls if c["stage"] == ResearchState.CROSS_CRITIQUE]
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    assert len(ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)) == 2


def test_3_cross_critique_discloses_all_proposals_only_after_all_complete(tmp_path, provider_state):
    orch, session, calls = _ready(tmp_path, provider_state)
    orch.run_independent_research(session["session_id"])
    result = orch.run_cross_critique(session["session_id"])
    assert result["current_stage"] == ResearchState.SYNTHESIS
    critique_calls = [c for c in calls.calls if c["stage"] == ResearchState.CROSS_CRITIQUE]
    assert len(critique_calls) == 3 and calls.max_active >= 2
    for call in critique_calls:
        prompt = json.loads(call["prompt"])
        assert set(prompt["proposals"]) == {"OPENAI", "ANTHROPIC", "GOOGLE"}
        assert "self_critique" in prompt["instructions"]


def test_4_codex_synthesis_can_create_new_d_without_self_priority_or_majority_vote(tmp_path, provider_state):
    orch, session, calls = _ready(tmp_path, provider_state)
    orch.run_independent_research(session["session_id"])
    orch.run_cross_critique(session["session_id"])
    result = orch.run_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.EXPERIMENT_PLANNING
    synthesis_calls = [c for c in calls.calls if c["stage"] == ResearchState.SYNTHESIS]
    assert len(synthesis_calls) == 1 and synthesis_calls[0]["domain"] == "OPENAI"
    prompt = json.loads(synthesis_calls[0]["prompt"])
    instructions = prompt["instructions"].lower()
    assert "do not prefer the openai proposal" in instructions
    assert "do not use majority vote" in instructions
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    synthesis = ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.SYNTHESIS)[0]["payload"]
    assert synthesis["selection_type"] == "NEW_D"
    assert synthesis["source_domains"] == ["ANTHROPIC", "GOOGLE"]


def test_5_end_to_end_stub_pipeline_uses_seven_calls_and_persists_raw_evidence(tmp_path, provider_state):
    orch, session, calls = _ready(tmp_path, provider_state)
    result = orch.run_through_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.EXPERIMENT_PLANNING
    assert len(calls.calls) == 7
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    provenance = ResearchStateMachine().get_call_provenance(cycle_id)
    assert len(provenance) == 7
    assert all(Path(p["prompt_artifact"]).is_file() and Path(p["output_artifact"]).is_file() for p in provenance)
    assert all(p["api_key_used"] == 0 and p["authentication_mode"] == "subscription" for p in provenance)


def test_6_explicit_degraded_pipeline_stays_two_provider_and_uses_five_calls(tmp_path, provider_state):
    pm = provider_state(OPENAI="HEALTHY", ANTHROPIC="UNAVAILABLE", GOOGLE="HEALTHY")
    calls = DeterministicCalls()
    orch = ResearchOrchestrator(ResearchStateMachine(), pm, tmp_path / "research", calls)
    session = orch.start_session(str(_project(tmp_path)), "Moonshot", allow_degraded=True)
    result = orch.run_through_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.EXPERIMENT_PLANNING
    assert result["degraded"] is True and result["participant_count"] == 2
    assert len(calls.calls) == 5
    assert {c["domain"] for c in calls.calls if c["stage"] != ResearchState.SYNTHESIS} == {"OPENAI", "GOOGLE"}


def test_7_degraded_without_codex_fails_closed_before_synthesis_call(tmp_path, provider_state):
    pm = provider_state(OPENAI="UNAVAILABLE", ANTHROPIC="HEALTHY", GOOGLE="HEALTHY")
    calls = DeterministicCalls()
    orch = ResearchOrchestrator(ResearchStateMachine(), pm, tmp_path / "research", calls)
    session = orch.start_session(str(_project(tmp_path)), "Moonshot", allow_degraded=True)
    result = orch.run_through_synthesis(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    assert result["human_required"] is True
    assert len(calls.calls) == 4
    assert not [c for c in calls.calls if c["stage"] == ResearchState.SYNTHESIS]


def test_8_invalid_structured_output_is_fail_closed_with_raw_artifact(tmp_path, provider_state):
    calls = DeterministicCalls(invalid_stage=ResearchState.INDEPENDENT_RESEARCH)
    orch, session, _ = _ready(tmp_path, provider_state, calls)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    provenance = ResearchStateMachine().get_call_provenance(cycle_id)
    assert len(provenance) == 3 and all(p["outcome"] == "FAILED" for p in provenance)
    assert all(Path(p["output_artifact"]).read_text(encoding="utf-8") == "not-json" for p in provenance)


def test_8b_bare_string_for_a_list_field_is_coerced_not_rejected(tmp_path, provider_state):
    """A real H attempt (2026-09-10) captured Claude returning a well-formed, on-topic proposal where
    'assumptions' (List[str]) was a single bare string instead of a one-item list, even though the prompt's
    type contract was explicit. This is a JSON-shape slip, not a content problem - the fix widens a bare
    string into [string] only for fields the schema itself declares List[str], and must not touch any other
    field or mask a genuinely wrong type."""
    from schemas.research import ResearchProposalSchema

    class OneStringAssumption(DeterministicCalls):
        def __call__(self, domain, prompt, stage, cwd):
            if stage != ResearchState.INDEPENDENT_RESEARCH:
                return super().__call__(domain, prompt, stage, cwd)
            proposal = _proposal(domain)
            proposal["assumptions"] = "single assumption returned as a bare string, not a list"
            return {
                "output": json.dumps(proposal), "executable": f"stub-{domain.lower()}", "version": "test-1",
                "invocation": "stub", "started_at": "2026-01-01T00:00:00", "ended_at": "2026-01-01T00:00:01",
                "duration_sec": 1.0, "exit_code": 0, "token_usage": "UNKNOWN",
                "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
            }

    calls = OneStringAssumption()
    orch, session, _ = _ready(tmp_path, provider_state, calls)
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.CROSS_CRITIQUE and not result["human_required"]
    cycle_id = ResearchStateMachine().list_cycles(session["session_id"])[0]["cycle_id"]
    outputs = ResearchStateMachine().get_stage_outputs(cycle_id, ResearchState.INDEPENDENT_RESEARCH)
    assert len(outputs) == 3
    for row in outputs:
        assert row["payload"]["assumptions"] == ["single assumption returned as a bare string, not a list"]
        assert isinstance(row["payload"]["hypothesis"], str)   # a real string field must never be wrapped in a list


def test_8c_coercion_never_masks_a_genuinely_wrong_type(tmp_path, provider_state):
    """The same coercion must still fail closed when a list-typed field is a dict/number, not a string -
    only a bare string is ever widened; nothing else is tolerated."""
    calls = DeterministicCalls(invalid_stage=None)

    class WrongTypeAssumption(DeterministicCalls):
        def __call__(self, domain, prompt, stage, cwd):
            if stage != ResearchState.INDEPENDENT_RESEARCH:
                return super().__call__(domain, prompt, stage, cwd)
            proposal = _proposal(domain)
            proposal["assumptions"] = {"not": "a list or a string"}
            return {
                "output": json.dumps(proposal), "executable": f"stub-{domain.lower()}", "version": "test-1",
                "invocation": "stub", "started_at": "2026-01-01T00:00:00", "ended_at": "2026-01-01T00:00:01",
                "duration_sec": 1.0, "exit_code": 0, "token_usage": "UNKNOWN",
                "authentication_mode": "subscription", "api_key_used": False,
                "observed_fields": ["executable", "version", "exit_code", "duration_sec"],
                "declared_fields": ["authentication_mode", "api_key_used"],
            }

    orch, session, _ = _ready(tmp_path, provider_state, WrongTypeAssumption())
    result = orch.run_independent_research(session["session_id"])
    assert result["current_stage"] == ResearchState.HUMAN_REQUIRED and result["human_required"]


def test_9_official_cli_commands_are_read_only_and_subscription_only():
    runner = TriadResearchRunner(executables={"OPENAI": "codex", "ANTHROPIC": "claude", "GOOGLE": "agy"})
    codex, codex_stdin, _ = runner._command("OPENAI", "codex", "p")
    claude, claude_stdin, _ = runner._command("ANTHROPIC", "claude", "p")
    google, google_stdin, _ = runner._command("GOOGLE", "agy", "p")
    assert codex == ["codex", "exec", "--sandbox", "read-only", "-"] and codex_stdin == "p"
    assert claude == ["claude", "--print", "--output-format", "json", "--strict-mcp-config"] and claude_stdin == "p"
    assert google == ["agy", "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"]
    google_message = json.loads(google_stdin)
    assert google_message["event"] == "user"
    assert google_message["message"]["content"][0]["text"] == "p"
    assert "accept-edits" not in google
