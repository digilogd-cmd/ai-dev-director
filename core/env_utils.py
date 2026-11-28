import os
import shutil
from pathlib import Path
from typing import Dict, Optional

# Named examples kept for callers/tests; the real rule is in is_billing_variable().
FORBIDDEN_API_KEYS = [
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "CODEX_API_KEY",
]

# Variables that move a CLI off its subscription login: bearer tokens, endpoint overrides and cloud-billing switches.
BILLING_VARIABLES = {
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID", "ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION",
    "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA", "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL",
}


def is_billing_variable(name: str) -> bool:
    """Rule, not a list (2026-11-20, public review: five named keys let CODEX_API_KEY and friends through):
    every *_API_KEY, plus the known token / endpoint / cloud-billing switches."""
    upper = (name or "").upper()
    return upper.endswith("_API_KEY") or upper.endswith("_API_KEYS") or upper in BILLING_VARIABLES


def get_sanitized_env() -> Dict[str, str]:
    """
    Environment for every CLI call and every verification command, with pay-per-use credentials stripped.
    This covers environment variables only: a CLI that was itself logged in with an API key (for example
    `codex login --with-api-key`) still bills that key, and the Director cannot see that from here.
    """
    env = os.environ.copy()
    for k in list(env):
        if is_billing_variable(k):
            env.pop(k, None)
    return env


# Claude Code flags for roles that only read (HEAD, REVIEWER, research, the proposal Builder). Without them the
# tool set and permission mode came from the user's own Claude settings, so a permissive setup was inherited.
CLAUDE_READ_ONLY_FLAGS = ["--tools", "Read,Glob,Grep", "--permission-mode", "default",
                          "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

_ROOT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"
_config_cache = {"mtime": None, "data": {}}


def root_config() -> dict:
    """config.yaml next to the Director, re-read when it changes. Unreadable -> {}."""
    try:
        mtime = _ROOT_CONFIG.stat().st_mtime
        if _config_cache["mtime"] != mtime:
            import yaml
            _config_cache["data"] = yaml.safe_load(_ROOT_CONFIG.read_text(encoding="utf-8")) or {}
            _config_cache["mtime"] = mtime
    except Exception:
        return {}
    return _config_cache["data"]


def antigravity_enabled() -> bool:
    """Google's Antigravity FAQ says wrapping `agy` inside another application is not supported on consumer plans
    and can lead to account suspension. `antigravity.enabled: false` in config.yaml makes the Director never
    discover or call it. A missing key keeps the earlier behaviour; the published config ships it as false."""
    if os.environ.get("DIRECTOR_TESTING") == "1":
        return True
    return bool((root_config().get("antigravity") or {}).get("enabled", True))


_ERROR_HINTS = ("error", "limit", "quota", "not supported", "unauthorized", "denied", "expired", "failed", "unavailable")


def cli_error_excerpt(stderr: Optional[str], stdout: Optional[str] = None, limit: int = 600) -> str:
    """What a failed CLI actually complained about, short enough to survive every later truncation.
    Codex prints its banner and the whole echoed prompt first and the error last, so keeping the first 500
    characters kept none of the error (2026-11-28: 'model is not supported' was recorded as UNKNOWN and the dead
    provider was called again on every milestone). The last line that looks like an error comes first, then the
    tail of the output."""
    text = (stderr or "").strip() or (stdout or "").strip()
    if not text:
        return "no output"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    key = next((ln for ln in reversed(lines) if any(h in ln.lower() for h in _ERROR_HINTS)), "")
    key = key[: limit // 2]
    tail = " ".join(lines)[-(limit - len(key) - 3):]
    return (key + " | " + tail) if key and key not in tail[-len(key) - 5:] else (key or tail)


def resolve_cli(name: str) -> Optional[str]:
    """
    Discovers the official CLI executable from OS PATH or standard install locations.
    Returns the resolved absolute path or None.
    """
    if name == "agy" and not antigravity_enabled():
        return None
    # Desktop CLIs live in versioned directories that change after app updates.
    # Prefer native binaries over stale npm wrappers, without changing PATH globally.
    if os.name == 'nt' and os.environ.get('DIRECTOR_TESTING') != '1':
        local = Path(os.environ.get('LOCALAPPDATA', ''))
        roaming = Path(os.environ.get('APPDATA', ''))
        root = {'codex': local / 'OpenAI/Codex/bin',
                'claude': roaming / 'Claude/claude-code'}.get(name)
        if root:
            try:
                candidates = sorted(root.glob('*/' + name + '.exe'),
                                    key=lambda p: p.stat().st_mtime, reverse=True)
                if candidates:
                    return str(candidates[0].resolve())
            except OSError:
                pass
    try:
        p = shutil.which(name)
    except (PermissionError, OSError):
        p = None
    if p:
        try:
            return str(Path(p).resolve())
        except (PermissionError, OSError):
            return str(p)

    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    app_data = Path(os.environ.get("APPDATA", ""))
    home = Path.home()
    # macOS/Linux (unverified): native installer -> ~/.local/bin, Homebrew -> /opt/homebrew/bin, npm -g -> /usr/local/bin
    posix_dirs = [home / ".local" / "bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]

    known = {
        "claude": [
            app_data / "npm" / "claude.cmd",
            app_data / "npm" / "claude",
            *[d / "claude" for d in posix_dirs],
            home / ".claude" / "local" / "claude",
        ],
        "codex": [
            app_data / "npm" / "codex.cmd",
            app_data / "npm" / "codex",
            *[d / "codex" for d in posix_dirs],
        ],
        "agy": [
            local_app_data / "agy" / "bin" / "agy.exe",
            local_app_data / "agy" / "bin" / "agy.cmd",
            *[d / "agy" for d in posix_dirs],
            home / ".agy" / "bin" / "agy",
        ]
    }

    for cand in known.get(name, []):
        # A candidate that cannot even be stat'ed (WinError 5 / PermissionError / OSError) is skipped;
        # the next candidate is tried and, if none works, None (= CLI unavailable) is returned.
        try:
            present = cand.exists()
        except (PermissionError, OSError):
            continue
        if not present:
            continue
        try:
            bin_dir = str(cand.parent)
            if bin_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")
            try:
                return str(cand.resolve())
            except (PermissionError, OSError):
                return str(cand)
        except (PermissionError, OSError):
            continue

    return None


def run_capture(argv, cwd=None, timeout=None, env=None):
    """
    subprocess.run wrapper for short non-AI commands (git, --version probes).
    Always shell=False, captured output decoded as UTF-8 with replacement so Korean/Unicode
    paths on Windows (cp949 default codec) never raise inside the reader thread.
    """
    import subprocess
    return subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        timeout=timeout,
        env=env,
    )


def probe_version(executable, timeout: float = 5.0, arg: str = "--version") -> str:
    """
    Runtime-availability probe (NOT an inference call). Runs `<executable> --version` through
    ProcessTreeRunner: shell=False, hard timeout, stdout/stderr drained, whole process tree killed on
    timeout (Windows taskkill /T). Returns the first stdout line or "UNKNOWN".
    """
    from core.process_runner import ProcessTreeRunner
    if not executable:
        return "UNKNOWN"
    try:
        res = ProcessTreeRunner.run(
            argv=[str(executable), arg],
            env=get_sanitized_env(),
            timeout=float(timeout),
            no_output_timeout=None,
            heartbeat_interval=max(float(timeout), 1.0),
        )
    except Exception:
        return "UNKNOWN"
    if res.timed_out or res.exit_code != 0:
        return "UNKNOWN"
    first = (res.stdout or "").strip().split("\n")[0].strip()
    return first or "UNKNOWN"
