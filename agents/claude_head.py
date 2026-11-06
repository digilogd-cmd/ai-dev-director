import os
import sys
import json
import re
import time
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from agents.head import BaseHead
from core.env_utils import resolve_cli, get_sanitized_env
from core.process_runner import ProcessTreeRunner
from schemas.order import OrderSchema
from schemas.builder_result import BuilderResultSchema
from schemas.review_result import ReviewResultSchema
from schemas.decision import DecisionSchema
from schemas.provenance import AgentProvenanceRecord

class ClaudeHead(BaseHead):
    """
    Anthropic Claude Head Agent using official Claude Code CLI (`claude`) via ProcessTreeRunner.
    Direct API calls and PAYG API keys are strictly forbidden.
    """
    provider: str = "anthropic"
    agent_family: str = "claude"

    def __init__(
        self,
        executable: Optional[str] = None,
        model: str = "claude-code-cli",
        timeout: int = 300,
        no_progress_timeout: int = 180,
        heartbeat_interval: int = 30,
        **kwargs
    ):
        self.provider = "anthropic"
        self.executable = executable or resolve_cli("claude")
        if self.executable:
            self.executable = str(Path(self.executable).resolve())

        self.model = model
        self.timeout = timeout
        self.no_progress_timeout = no_progress_timeout
        self.heartbeat_interval = heartbeat_interval
        self.logger = logging.getLogger("director.claude_head")
        self.last_execution_info = {}

    def get_version(self) -> str:
        if not self.executable or not Path(self.executable).exists():
            return "unknown"
        from core.env_utils import probe_version
        v = probe_version(self.executable, timeout=10)
        return v if v != "UNKNOWN" else "unknown"

    def get_provenance(self, exit_code: int = 0) -> AgentProvenanceRecord:
        exec_path = str(Path(self.executable).resolve()) if self.executable else "claude"
        invocation = self.last_execution_info.get("invocation_command", "claude --print --output-format json --strict-mcp-config")
        version = self.get_version()
        duration = self.last_execution_info.get("execution_duration_sec")
        actual_exit_code = self.last_execution_info.get("exit_code")   # None when no process ran (never a default)

        return AgentProvenanceRecord(
            agent="HEAD",
            provider="anthropic",
            runtime="claude-code-cli",
            model=self.model,
            executable=exec_path,
            invocation_command=invocation,
            version=version,
            exit_code=actual_exit_code,
            duration_sec=duration,
            authentication_mode="subscription",
            api_key_used=False,
            timestamp=datetime.utcnow().isoformat()
        )

    def _extract_json(self, raw_text: str) -> dict:
        text = raw_text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            cand = text[start:end+1]
            return json.loads(cand)

        raise ValueError(f"Could not parse JSON from output: {raw_text[:200]}...")

    def _invoke_cli(self, prompt: str, cwd: Optional[str] = None) -> str:
        from core import usage
        usage.set_context(role=getattr(self, "usage_role", "HEAD"))
        if not self.executable or not Path(self.executable).exists():
            raise RuntimeError(f"CONFIG_ERROR: Official Claude Code CLI not found at: {self.executable}")

        cmd = [self.executable, "--print", "--output-format", "json", "--strict-mcp-config"]
        invocation_desc = "claude --print --output-format json --strict-mcp-config"
        self.logger.info(f"[Claude Head] Invoking CLI via ProcessTreeRunner: {self.executable} (cwd={cwd})")

        sanitized_env = get_sanitized_env()
        res = ProcessTreeRunner.run(
            argv=cmd,
            cwd=cwd,
            env=sanitized_env,
            timeout=self.timeout,
            no_output_timeout=self.no_progress_timeout,
            heartbeat_interval=self.heartbeat_interval,
            stdin_data=prompt
        )

        self.last_execution_info = {
            "executable": self.executable,
            "version": self.get_version(),
            "invocation_command": invocation_desc,
            "exit_code": res.exit_code,
            "execution_duration_sec": res.duration_sec
        }

        if res.timed_out:
            self.logger.error(f"[Claude Head] Process timed out: {res.timeout_reason}")
            raise TimeoutError(f"TIMEOUT: Claude Head CLI exceeded limit: {res.timeout_reason}")

        if res.exit_code != 0:
            err_msg = res.stderr.strip() or res.stdout.strip()
            self.logger.error(f"Claude CLI exited with code {res.exit_code}: {err_msg}")
            raise RuntimeError(f"Claude Code CLI execution failed (exit {res.exit_code}): {err_msg}")

        try:
            envelope = json.loads(res.stdout)
            return envelope.get("result", "")
        except json.JSONDecodeError:
            return res.stdout

    def create_order(self, goal: str, project_context: Dict[str, Any]) -> OrderSchema:
        prompt = (
            "You are the HEAD architect of an autonomous software engineering system.\n"
            "Your task is to analyze the user goal and project files, then generate a strict work ORDER in JSON format.\n"
            "Rules:\n"
            "1. Output MUST be valid JSON adhering strictly to the schema.\n"
            "2. Define minimal 'allowed_paths' necessary to achieve the goal.\n"
            "3. Include critical files/directories in 'protected_paths' that must not be modified.\n"
            "4. Define clear, testable 'acceptance_criteria'. Include required_outputs: exact project-relative deliverable file paths, including required behavior tests. Include reference_paths for explicit user-supplied absolute document paths.\n"
            "5. Specify realistic 'max_files_changed' (<=15) and 'max_diff_lines' (<=800).\n"
            "6. 'allowed_deletions': Must be empty [] unless the user explicitly requested deleting specific functions, classes, tests, or files.\n"
            "7. Explicitly remind the builder to preserve all existing functions, classes, and tests alongside new ones.\n\n"
            "SCHEMA SPECIFICATION:\n"
            "{\n"
            '  "goal": string,\n'
            '  "allowed_paths": string[],\n'
            '  "protected_paths": string[],\n'
            '  "acceptance_criteria": string[],\n'
            '  "allowed_deletions": string[],\n'
            '  "max_files_changed": integer,\n'
            '  "max_diff_lines": integer,\n'
            '  "context": string\n'
            "}\n\n"
            f"USER GOAL:\n{goal}\n\n"
            f"PROJECT CONTEXT:\n{json.dumps(project_context, indent=2)}\n\n"
            "Respond ONLY with the JSON object. Do not include introductory text."
        )

        proj_dir = project_context.get("project_path")
        raw_output = self._invoke_cli(prompt, cwd=proj_dir)
        data = self._extract_json(raw_output)
        return OrderSchema.model_validate(data)

    def decide(
        self,
        order: OrderSchema,
        builder_result: BuilderResultSchema,
        verification: Dict[str, Any],
        review_result: ReviewResultSchema,
        cwd: Optional[str] = None,
        **kwargs
    ) -> DecisionSchema:
        test_info = verification.get("test", {})
        evidence = {
            "order": order.model_dump(),
            "builder_result": builder_result.model_dump(),
            "verification": {
                "passed": verification.get("passed"),
                "summary": verification.get("summary"),
                "test_command": test_info.get("command"),
                "test_exit_code": test_info.get("exit_code"),
                "test_counts": verification.get("test_counts"),
                "test_output": (test_info.get("output") or "")[:800],
            },
            "git_diff": verification.get("git_diff") or "",
            "review": review_result.model_dump()
        }

        prompt = (
            "You are the HEAD architect making the final verdict on a development run.\n"
            "Review the work order, builder output, automated verification results, and code review findings.\n\n"
            "STRICT PASS RULES:\n"
            "1. 'PASS': ONLY permitted if:\n"
            "   - Automated build and tests passed.\n"
            "   - NO existing functions, classes, or tests were deleted (unless permitted in allowed_deletions).\n"
            "   - NO baseline test regressions occurred.\n"
            "   - Reviewer reported NO P0/P1 issues and did NOT report accidental deletion of existing behaviors.\n"
            "   - Reviewer verdict is pass, OR the reviewer reported only P2 (minor) findings: then PASS and list those P2 items in next_steps as follow-ups. Any P0/P1 needs FAIL with concrete corrections.\n"
            "2. 'HUMAN_REQUIRED': MUST be returned if:\n"
            "   - Any existing function, test, or behavior was removed without explicit order permission.\n"
            "   - External authority, credentials, spending or live deployment is required (start the rationale with USER_DECISION:).\n"
            "Product and business choices inside the ORDER's goal are YOURS as the user's proxy: pick one, state it, and direct the Builder.\n"
            "3. 'FAIL': Implementation is incomplete or tests/review found defects. Give concrete developer repair instructions in next_steps.\n"
            "Routine implementation architecture, input contracts and fixable review findings are YOUR decisions: resolve them and direct the Builder. Do not ask the user.\n"
            "An ORDER that cannot be met as written (conflicting acceptance criteria, a needed file outside allowed_paths or in protected_paths, a broken test fixture) is NOT a user decision: return FAIL, name the conflict and the scope change needed; the mission planner re-plans.\n\n"
            "Before issuing repairs, cross-check every instruction against the ORIGINAL acceptance criteria. Never solve one defect by contradicting an existing requirement.\n"
            "SCHEMA SPECIFICATION:\n"
            "{\n"
            '  "decision": "PASS" | "HUMAN_REQUIRED" | "FAIL",\n'
            '  "rationale": string,\n'
            '  "next_steps": string[]\n'
            "}\n\n"
            f"RUN EVIDENCE:\n{json.dumps(evidence, indent=2)}\n\n"
            "Respond ONLY with the JSON object. Do not include introductory text."
        )

        target_cwd = cwd or kwargs.get("worktree_path") or kwargs.get("project_path")
        raw_output = self._invoke_cli(prompt, cwd=target_cwd)
        data = self._extract_json(raw_output)
        return DecisionSchema.model_validate(data)

    def chat(self, message: str, project_context: Dict[str, Any], history: list) -> Dict[str, Any]:
        prompt = (
            "You are the HEAD architect of an autonomous software engineering system.\n"
            "The user is talking to you directly. You must determine if their message is a simple QUESTION or a DEV_TASK.\n\n"
            "If it's a simple QUESTION, output:\n"
            "{\n"
            '  "type": "QUESTION",\n'
            '  "message": "Your answer to the question"\n'
            "}\n\n"
            "If it's a DEV_TASK, output a strict ORDER JSON as well:\n"
            "All allowed_paths must be narrow project-relative files/subtrees (e.g. src/**, tests/**, README.md). Never put the absolute project directory or a whole-project wildcard in allowed_paths. Include required_outputs listing exact deliverable file paths and reference_paths listing user-supplied absolute document paths.\n"
            "{\n"
            '  "type": "DEV_TASK",\n'
            '  "message": "I will proceed with this task.",\n'
            '  "order": {\n'
            '      "goal": "...",\n'
            '      "allowed_paths": ["..."],\n'
            '      "protected_paths": ["..."],\n'
            '      "acceptance_criteria": ["..."],\n'
            '      "allowed_deletions": [],\n'
            '      "max_files_changed": 10,\n'
            '      "max_diff_lines": 500,\n'
            '      "context": "...",\n'
            '      "risk_level": "LOW" | "MEDIUM" | "HIGH",\n'
            '      "requires_human_approval": false\n'
            '  }\n'
            "}\n\n"
            "RISK LEVEL GUIDANCE (HIGH always halts for human approval before the Builder runs - use it "
            "for real-world exposure, not for ambition):\n"
            "- 'HIGH': the goal could submit a real broker/exchange order, touch live credentials or an "
            "external account, delete or overwrite data that cannot be recovered from this worktree, or "
            "take any action that is not undone by discarding the run.\n"
            "- 'MEDIUM': ordinary development or research work, including an ambitious scope (a new "
            "strategy, a demanding backtest target, a large new module) - as long as it stays inside a "
            "sandboxed/demo/backtest-only worktree and within max_files_changed/max_diff_lines. An "
            "ambitious GOAL is not by itself a reason for HIGH.\n"
            "- 'LOW': a small, narrowly-scoped change.\n"
            "A DEV_TASK runs as a multi-step MISSION: make this ORDER the first focused, testable milestone "
            "(core behavior + tests). You will plan the following milestones yourself from real results.\n\n"
            f"PROJECT CONTEXT:\n{json.dumps(project_context, indent=2)}\n\n"
            "CHAT HISTORY:\n"
            f"{json.dumps(history[-5:], indent=2)}\n\n"
            f"USER MESSAGE: {message}\n\n"
            "Respond ONLY with the JSON object. Do not include introductory text."
        )

        proj_dir = project_context.get("project_path")
        raw_output = self._invoke_cli(prompt, cwd=proj_dir)
        data = self._extract_json(raw_output)
        if data.get("type") not in ("QUESTION", "DEV_TASK"):
            raise ValueError("Invalid chat response type")
        if data["type"] == "DEV_TASK":
            # validate order schema implicitly
            OrderSchema.model_validate(data.get("order", {}))
        return data
