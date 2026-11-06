"""Official subscription-CLI runner for read-only TRIAD research stages."""
import os
import shutil
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.env_utils import get_sanitized_env, probe_version, resolve_cli, run_capture
from core.process_runner import ProcessTreeRunner
from core.provider_manager import ErrorCategory
from core.triad_provider import ALL_TRIAD_DOMAINS


class ResearchProviderCallError(RuntimeError):
    """retryable=True marks a failure this module itself classified as a bounded-retry candidate (transport/
    runtime level only - see TriadResearchRunner._is_retryable). meta carries whatever call metadata (executable,
    version, exit_code, duration, ...) was known at the moment of failure, in the same shape as a successful
    call result, so a caller can log a truthful provenance row for the failed attempt even though it never
    produced usable output."""

    def __init__(self, message: str, retryable: bool = False, meta: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.retryable = retryable
        self.meta = dict(meta or {})


class TriadResearchRunner:
    """Invoke each official CLI without API keys. Callers provide an isolated git worktree as cwd.

    Bounded transient retry (V3.0-H hardening, 2026-09-10): a research cycle needs ~15-20 real subscription
    CLI calls to complete once, uninterrupted. Before this, any single transient transport/runtime hiccup on
    any one call discarded the whole cycle to HUMAN_REQUIRED - correctly fail-closed, but too fragile for the
    long-running autonomous research loop this system exists to run. `invoke()` now retries ONCE (initial
    attempt + MAX_ATTEMPTS-1 retries, MAX_ATTEMPTS=2) and ONLY for failures classified as transient at the
    transport/runtime level: a genuinely transient process/CLI timeout, or Antigravity's stream-json mode
    reporting terminal SUCCESS with no parseable result / an empty response. Quota exhaustion, authentication
    failure, provider-unavailable, config errors, and any content/schema-level failure are never retried -
    they fail closed on the first attempt exactly as before. A retry always resends the identical original
    prompt (never the prior attempt's bad output, never another provider's output) into a fresh call; no
    provider is ever substituted for another. Every attempt, successful or not, is preserved for provenance
    (see `attempts` on the return value / `ResearchProviderCallError`)."""

    MAX_ATTEMPTS = 2   # initial attempt + 1 bounded retry; never more, per V3.0-H hardening scope

    def __init__(self, executables: Optional[Dict[str, str]] = None, timeout: float = 300.0,
                 no_output_timeout: float = 180.0, heartbeat_interval: float = 30.0):
        configured = dict(executables or {})
        self.executables = {
            "OPENAI": configured.get("OPENAI") or resolve_cli("codex"),
            "ANTHROPIC": configured.get("ANTHROPIC") or resolve_cli("claude"),
            "GOOGLE": configured.get("GOOGLE") or resolve_cli("agy"),
        }
        self.timeout = timeout
        self.no_output_timeout = no_output_timeout
        self.heartbeat_interval = heartbeat_interval
        self._versions: Dict[str, str] = {}

    def _command(self, domain: str, executable: str, prompt: str):
        if domain == "OPENAI":
            # Codex official help: omitting PROMPT (or using '-') reads it from stdin.  This is required on
            # Windows where synthesis/cross-critique evidence can exceed CreateProcess' command-line limit.
            return [executable, "exec", "--sandbox", "read-only", "-"], prompt, "codex exec --sandbox read-only - <stdin>"
        if domain == "ANTHROPIC":
            return [executable, "--print", "--output-format", "json", "--strict-mcp-config"], prompt, "claude --print --output-format json --strict-mcp-config"
        if domain == "GOOGLE":
            # Official AGY stream-json mode reads NDJSON from stdin. This avoids Windows command-line limits
            # without file-read tools or permission bypasses, and exposes an explicit final result event.
            message = {"event": "user", "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}}
            return (
                [executable, "--input-format", "stream-json", "--output-format", "stream-json", "--sandbox"],
                json.dumps(message, ensure_ascii=False) + "\n",
                "agy --input-format stream-json --output-format stream-json --sandbox <stdin>",
            )
        raise ValueError(f"unsupported TRIAD domain: {domain}")

    @staticmethod
    def _is_retryable(message: str) -> bool:
        """Bounded-retry allowlist (V3.0-H hardening). Deliberately narrow: only failures with positive
        evidence of being a transient transport/runtime hiccup are retried. Everything else - quota, auth,
        provider-unavailable, config error, safety/scope violations, semantic schema violations, and a real
        experiment/content failure - is excluded by construction and falls through to `ErrorCategory.classify`,
        which only returns TIMEOUT for phrases naming a timeout; every other category (including UNKNOWN, for
        an error this code does not positively recognize) is NOT retried."""
        if message.startswith("GOOGLE_STREAM_EMPTY_RESPONSE") or message.startswith("GOOGLE_STREAM_RESULT_MISSING"):
            return True
        if message.startswith("TIMEOUT:"):
            return True
        lowered = message.lower()
        if any(evidence in lowered for evidence in ("wsarecv", "connection attempt failed", "connection reset",
                                                    "tls handshake timeout", "i/o timeout")):
            return True    # the network dropped mid-request: the provider never judged the prompt
        return ErrorCategory.classify(message) == ErrorCategory.TIMEOUT

    def _invoke_once(self, domain: str, prompt: str, cwd: str, stage: str) -> Dict[str, Any]:
        normalized = str(domain).upper()
        executable = self.executables.get(normalized)
        meta: Dict[str, Any] = {"executable": executable}
        if not executable or not Path(executable).exists():
            raise ResearchProviderCallError(
                f"CONFIG_ERROR: official {normalized} CLI is unavailable", retryable=False, meta=meta
            )
        workspace = Path(cwd).resolve()
        before = run_capture(["git", "status", "--porcelain=v1", "-uall"], cwd=workspace, timeout=20)
        if before.returncode != 0:
            raise ResearchProviderCallError(
                f"CONFIG_ERROR: research workspace is not a git worktree: {workspace}", retryable=False, meta=meta
            )

        argv, stdin_data, invocation = self._command(normalized, str(Path(executable).resolve()), prompt)
        meta["invocation"] = invocation
        meta["executable"] = str(Path(executable).resolve())
        started_at = datetime.utcnow().isoformat()
        meta["started_at"] = started_at
        result = ProcessTreeRunner.run(
            argv=argv,
            cwd=str(workspace),
            env=get_sanitized_env(),
            timeout=self.timeout,
            # Claude's official --print/JSON mode is intentionally silent until
            # the final document is ready.  A no-output watchdog would kill a
            # healthy long critique; the hard timeout still bounds and tree-kills
            # the process. Codex and AGY retain the progress watchdog.
            no_output_timeout=None if normalized == "ANTHROPIC" else self.no_output_timeout,
            heartbeat_interval=self.heartbeat_interval,
            stdin_data=stdin_data,
        )
        ended_at = datetime.utcnow().isoformat()
        meta.update({"ended_at": ended_at, "duration_sec": result.duration_sec, "exit_code": result.exit_code})
        after = run_capture(["git", "status", "--porcelain=v1", "-uall"], cwd=workspace, timeout=20)
        if after.returncode != 0 or after.stdout != before.stdout:
            raise ResearchProviderCallError(
                f"RESEARCH_READ_ONLY_VIOLATION: {normalized} changed its isolated research workspace",
                retryable=False, meta=meta,
            )
        if result.timed_out:
            raise ResearchProviderCallError(
                f"TIMEOUT: {normalized}: {result.timeout_reason}", retryable=True, meta=meta
            )
        if result.exit_code != 0:
            error = (result.stderr or result.stdout or "provider call failed").strip()
            message = f"{normalized} CLI exit {result.exit_code}: {error[:500]}"
            raise ResearchProviderCallError(message, retryable=self._is_retryable(message), meta=meta)
        output = result.stdout
        if normalized == "GOOGLE":
            final_event = None
            for line in (result.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if event.get("event") == "result" and isinstance(event.get("result"), dict):
                    final_event = event["result"]
            if not final_event:
                raise ResearchProviderCallError("GOOGLE_STREAM_RESULT_MISSING", retryable=True, meta=meta)
            if final_event.get("status") != "SUCCESS":
                message = "GOOGLE_STREAM_FAILED: " + str(
                    final_event.get("error") or final_event.get("status") or "UNKNOWN"
                )[:500]
                raise ResearchProviderCallError(message, retryable=self._is_retryable(message), meta=meta)
            output = str(final_event.get("response") or "")
            if not output.strip():
                raise ResearchProviderCallError("GOOGLE_STREAM_EMPTY_RESPONSE", retryable=True, meta=meta)
        if normalized not in self._versions:
            self._versions[normalized] = probe_version(executable)
        return {
            "output": output,
            "executable": meta["executable"],
            "version": self._versions[normalized],
            "invocation": invocation,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_sec": result.duration_sec,
            "exit_code": result.exit_code,
            "token_usage": "UNKNOWN",
            "failover": False,
            "authentication_mode": "subscription",
            "api_key_used": False,
            "observed_fields": ["executable", "version", "invocation", "started_at", "ended_at", "duration_sec", "exit_code"],
            "declared_fields": ["authentication_mode", "api_key_used"],
            "stage": stage,
        }

    def invoke(self, domain: str, prompt: str, cwd: str, stage: str) -> Dict[str, Any]:
        normalized = str(domain).upper()
        if normalized not in ALL_TRIAD_DOMAINS:
            raise ValueError(f"unsupported TRIAD domain: {domain}")
        attempts: List[Dict[str, Any]] = []
        last_error: Optional[ResearchProviderCallError] = None
        for attempt_number in range(1, self.MAX_ATTEMPTS + 1):
            retry_reason = attempts[-1]["error"] if attempts else None
            try:
                # Retries always resend the identical original `prompt` - never the previous attempt's
                # output, never another provider's output, never a different provider.
                attempt_prompt = prompt
                if attempts and 'output token limit' in attempts[-1]['error'].lower() and stage == 'BUILDING':
                    attempt_prompt += '\nOUTPUT BUDGET: Return a complete, compact JSON object containing only necessary changed files. Minimize commentary and redundant code. Do not continue a truncated JSON fragment.'
                result = self._invoke_once(normalized, attempt_prompt, cwd, stage)
                result["attempts"] = attempts
                result["attempt_number"] = attempt_number
                return result
            except ResearchProviderCallError as exc:
                if stage == 'BUILDING' and str(exc).startswith('GOOGLE_STREAM_FAILED:') and 'output token limit' in str(exc).lower():
                    exc.retryable = True
                record = dict(exc.meta)
                record.update({
                    "attempt_number": attempt_number, "outcome": "FAILED",
                    "error": str(exc), "retryable": exc.retryable, "retry_reason": retry_reason,
                })
                attempts.append(record)
                last_error = exc
                if not exc.retryable or attempt_number >= self.MAX_ATTEMPTS:
                    break
        prior = attempts[:-1]
        final_meta = dict(attempts[-1]) if attempts else {}
        final_meta["attempts"] = prior
        final = ResearchProviderCallError(str(last_error), retryable=False, meta=final_meta)
        final.attempts = attempts
        final.failure_result = final_meta
        raise final


def create_research_workspaces(project_path: str, workspace_root: Path, domains) -> Dict[str, str]:
    """Create detached worktrees sequentially, then provider calls can safely run concurrently in isolation."""
    project = Path(project_path).resolve()
    head = run_capture(["git", "rev-parse", "--verify", "HEAD"], cwd=project, timeout=20)
    if head.returncode != 0:
        raise ResearchProviderCallError("RESEARCH_WORKSPACE_FAILED: project has no committed HEAD")
    root = workspace_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    created: Dict[str, str] = {}
    try:
        for domain in sorted(set(domains)):
            target = (root / domain.lower()).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise ResearchProviderCallError("RESEARCH_WORKSPACE_FAILED: workspace path escaped root")
            result = run_capture(["git", "worktree", "add", "--detach", str(target), "HEAD"], cwd=project, timeout=60)
            if result.returncode != 0:
                raise ResearchProviderCallError(f"RESEARCH_WORKSPACE_FAILED: {result.stderr.strip()[:500]}")
            created[domain] = str(target)
        return created
    except Exception:
        cleanup_research_workspaces(project_path, created)
        raise


def cleanup_research_workspaces(project_path: str, workspaces: Dict[str, str]) -> None:
    project = Path(project_path).resolve()
    for path in workspaces.values():
        run_capture(["git", "worktree", "remove", "--force", str(path)], cwd=project, timeout=60)
        shutil.rmtree(path, ignore_errors=True)
    run_capture(["git", "worktree", "prune"], cwd=project, timeout=30)
