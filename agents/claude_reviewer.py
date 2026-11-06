import os
import sys
import json
import re
import time
import shutil
import subprocess
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from core.env_utils import resolve_cli, get_sanitized_env
from core.process_runner import ProcessTreeRunner
from schemas.order import OrderSchema
from schemas.review_result import ReviewResultSchema
from schemas.provenance import AgentProvenanceRecord

class ClaudeReviewer:
    """
    Read-only code reviewer agent executing the official Claude Code CLI (`claude`) as a subprocess.
    Direct API calls and pay-per-use API keys are strictly forbidden.
    Never modifies any files or executes destructive commands.
    """
    provider: str = "anthropic"
    agent_family: str = "claude"

    def __init__(
        self,
        provider: str = "anthropic",
        model: str = "claude-code-cli",
        executable: Optional[str] = None,
        timeout: int = 300,
        no_progress_timeout: int = 180,
        heartbeat_interval: int = 30,
        **kwargs
    ):
        self.provider = provider or "anthropic"
        self.model = model or "claude-code-cli"
        self.timeout = timeout
        self.no_progress_timeout = no_progress_timeout
        self.heartbeat_interval = heartbeat_interval
        
        # Discover executable strictly from config -> env -> resolve_cli("claude")
        self.executable = executable or os.environ.get("CLAUDE_BIN") or resolve_cli("claude")
        if self.executable:
            self.executable = str(Path(self.executable).resolve())

        self.logger = logging.getLogger("director.claude_reviewer")
        self.last_execution_info = {}

    def get_version(self) -> str:
        if not self.executable or not Path(self.executable).exists():
            return "unknown"
        from core.env_utils import probe_version
        v = probe_version(self.executable, timeout=10)
        return v if v != "UNKNOWN" else "unknown"

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
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                pass

        raise ValueError(f"UNPARSEABLE_REVIEW: Could not extract valid JSON from reviewer output: {text[:200]}")

    def review(
        self,
        order: OrderSchema,
        git_diff: str,
        verification: Dict[str, Any],
        project_context: Optional[Dict[str, Any]] = None,
        worktree_path: Optional[str] = None
    ) -> ReviewResultSchema:
        """
        Conducts read-only review of the git diff and test evidence against the ORDER.
        Executes Claude Code CLI executable in the worktree directory.
        """
        # If mock provider explicitly requested for offline unit tests
        if self.provider == "mock":
            self.logger.info("[Claude Reviewer] Using deterministic review fallback for mock provider")
            return self._deterministic_review(order, git_diff, verification)

        # Execute Claude Code CLI
        if self.executable and Path(self.executable).exists():
            self.logger.info(f"[Claude Reviewer] Executing Claude Code CLI at: {self.executable}")
            return self._review_with_cli(order, git_diff, verification, worktree_path)

        # If no CLI binary found, return CONFIG_ERROR
        err_msg = "CONFIG_ERROR: Official Claude CLI ('claude') not found in OS PATH. Cannot proceed."
        self.logger.error(err_msg)
        return ReviewResultSchema(
            verdict="changes_required",
            p0=[err_msg],
            p1=[],
            p2=[],
            summary=err_msg
        )

    def _review_with_cli(
        self,
        order: OrderSchema,
        git_diff: str,
        verification: Dict[str, Any],
        worktree_path: Optional[str] = None
    ) -> ReviewResultSchema:
        """Executes Claude Code CLI subprocess in read-only mode via ProcessTreeRunner."""
        wt = Path(worktree_path).resolve() if worktree_path else Path.cwd()

        from core import usage
        usage.set_context(role="REVIEWER")
        review_prompt = (
            "You are an expert read-only code reviewer evaluating code modifications in a repository.\n"
            "Review the git diff and test evidence against the work ORDER.\n\n"
            "CRITICAL REVIEW MANDATES:\n"
            "1. Read-only review: Do NOT execute any destructive commands or modify any files.\n"
            "2. Baseline & Behavior Preservation: Check whether existing functions, classes, or tests were removed or replaced.\n"
            "3. If any existing function or test was deleted without permission in allowed_deletions, you MUST classify it as P0 or P1.\n"
            "4. Check for code correctness, edge cases, and test assertions.\n"
            "5. File-count and diff-size limits are enforced by the Director, not by you: never report them as findings.\n\n"
            f"GOAL: {order.goal}\n"
            f"ACCEPTANCE CRITERIA: {json.dumps(order.acceptance_criteria)}\n"
            f"ALLOWED DELETIONS: {json.dumps(order.allowed_deletions)}\n"
            f"VERIFICATION PASSED: {verification.get('passed')}\n"
            f"VERIFICATION SUMMARY: {verification.get('summary')}\n"
            f"TEST COUNTS (Director's own run): {json.dumps(verification.get('test_counts'))}\n"
            f"TEST OUTPUT TAIL: {((verification.get('test') or {}).get('output') or '')[-1500:]}\n\n"
            f"GIT DIFF:\n```diff\n{git_diff}\n```\n\n"
            "FORMATTING MANDATE:\n"
            "Your output MUST contain ONLY a JSON code block formatted as:\n"
            "```json\n"
            "{\n"
            '  "verdict": "pass" | "changes_required",\n'
            '  "p0": string[],\n'
            '  "p1": string[],\n'
            '  "p2": string[],\n'
            '  "summary": string\n'
            "}\n"
            "```"
        )

        invocation_cmd = 'claude --print --strict-mcp-config'
        self.logger.info(f"[Claude Reviewer] Invoking via ProcessTreeRunner: {self.executable} in {wt}")

        cmd = [self.executable, "--print", "--strict-mcp-config"]
        res = ProcessTreeRunner.run(
            argv=cmd,
            cwd=str(wt),
            env=get_sanitized_env(),
            timeout=self.timeout,
            no_output_timeout=self.no_progress_timeout,
            heartbeat_interval=self.heartbeat_interval,
            stdin_data=review_prompt
        )

        self.last_execution_info = {
            "executable": self.executable,
            "version": self.get_version(),
            "invocation_command": invocation_cmd,
            "stdout": res.stdout,
            "stderr": res.stderr,
            "exit_code": res.exit_code,
            "execution_duration_sec": res.duration_sec
        }

        self.logger.info(f"[Claude Reviewer] claude finished with code {res.exit_code} in {res.duration_sec:.2f}s")

        if res.timed_out:
            self.logger.error(f"[Claude Reviewer] Process timed out: {res.timeout_reason}")
            return ReviewResultSchema(
                verdict="changes_required",
                p0=[f"TIMEOUT: Claude CLI review process timed out: {res.timeout_reason}"],
                p1=[],
                p2=[],
                summary=f"Claude review timed out: {res.timeout_reason}"
            )

        if res.exit_code != 0:
            err_text = res.stderr or res.stdout
            self.logger.error(f"Claude CLI failed: {err_text}")
            return ReviewResultSchema(
                verdict="changes_required",
                p0=[f"Claude CLI exited with error code {res.exit_code}: {err_text[:200]}"],
                p1=[],
                p2=[],
                summary=f"Claude CLI execution failed: {err_text[:200]}"
            )

        if not res.stdout or not res.stdout.strip():
            self.logger.error("[Claude Reviewer] Empty stdout received from Claude CLI")
            return ReviewResultSchema(
                verdict="changes_required",
                p0=["UNPARSEABLE_REVIEW: Reviewer stdout is empty"],
                p1=[],
                p2=[],
                summary="UNPARSEABLE_REVIEW: Empty stdout"
            )

        try:
            data = self._extract_json(res.stdout)
            if not isinstance(data, dict):
                raise ValueError("Parsed review content is not a valid JSON object")
            return ReviewResultSchema.model_validate(data)
        except Exception as e:
            self.logger.error(f"[Claude Reviewer] Review output parse error: {e}")
            return ReviewResultSchema(
                verdict="changes_required",
                p0=[f"UNPARSEABLE_REVIEW: {str(e)}"],
                p1=[],
                p2=[],
                summary=f"UNPARSEABLE_REVIEW: {str(e)}"
            )

    def get_provenance(self, exit_code: int = 0) -> AgentProvenanceRecord:
        exec_path = str(Path(self.executable).resolve()) if self.executable else "claude"
        invocation = self.last_execution_info.get("invocation_command", 'claude --print --strict-mcp-config')
        version = self.get_version()
        duration = self.last_execution_info.get("execution_duration_sec")
        actual_exit_code = self.last_execution_info.get("exit_code")   # None when no process ran (never a default)

        return AgentProvenanceRecord(
            agent="REVIEWER",
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

    def _deterministic_review(
        self,
        order: OrderSchema,
        git_diff: str,
        verification: Dict[str, Any]
    ) -> ReviewResultSchema:
        p0 = []
        p1 = []
        p2 = []

        if not verification.get("passed", False):
            p0.append(f"Verification failed: {verification.get('summary')}")

        if not git_diff.strip():
            p1.append("No diff detected: Builder made zero modifications.")

        verdict = "changes_required" if (p0 or p1) else "pass"
        summary = (
            f"Review verdict: {verdict}. P0 issues: {len(p0)}, P1 issues: {len(p1)}, P2 issues: {len(p2)}."
        )

        return ReviewResultSchema(
            verdict=verdict,
            p0=p0,
            p1=p1,
            p2=p2,
            summary=summary
        )
