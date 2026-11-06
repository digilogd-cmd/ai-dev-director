import json
import os
import sys
import tempfile
import sqlite3
import pytest

import atexit
import shutil

# Run evidence written by the suite goes to a private folder instead of <repo>/runs (set before any test module
# computes a runs path at import time).
_RUNS_DIR = tempfile.mkdtemp(prefix="director_runs_")
os.environ["TEST_RUNS_DIR"] = _RUNS_DIR
os.environ["DIRECTOR_TESTING"] = "1"   # the per-test fixture sets it too; this covers paths computed at import
atexit.register(shutil.rmtree, _RUNS_DIR, True)

from core.db_path import DEFAULT_DB_PATH


# ----------------------------------------------------------------------------------------------
# Stub CLI: a tiny script that answers `--version`; resolve_cli() returns it for codex/claude/agy so the
# default regression never depends on which real CLIs are installed / logged in / within quota.
# ----------------------------------------------------------------------------------------------
_STUB_DIR = tempfile.mkdtemp(prefix="director_stub_cli_")
if os.name == "nt":
    _STUB_CLI = os.path.join(_STUB_DIR, "director-stub-cli.cmd")
    with open(_STUB_CLI, "w", encoding="utf-8") as fh:
        fh.write("@echo off\r\necho stub-cli 0.0.0-test\r\n")
else:
    _STUB_CLI = os.path.join(_STUB_DIR, "director-stub-cli")
    with open(_STUB_CLI, "w", encoding="utf-8") as fh:
        fh.write("#!/bin/sh\necho stub-cli 0.0.0-test\n")
    os.chmod(_STUB_CLI, 0o755)

_RESOLVE_CLI_SITES = (
    "core.provider_manager.resolve_cli",
    "agents.codex_head.resolve_cli",
    "agents.claude_head.resolve_cli",
    "agents.codex_reviewer.resolve_cli",
    "agents.claude_reviewer.resolve_cli",
    "agents.antigravity_builder.resolve_cli",
)


def stub_resolve_cli(name: str):
    return _STUB_CLI


@pytest.fixture(autouse=True)
def isolate_provider_db(request, monkeypatch, tmp_path):
    """Every test gets (1) a private SQLite DB (never data/director.db), (2) provider_health rows that start
    HEALTHY, (3) stubbed CLI discovery so health probes are deterministic, (4) the real-CLI guard armed
    (ProcessTreeRunner refuses codex/claude/agy) unless the test carries @pytest.mark.integration_cli."""
    db_path = tmp_path / "director_test.db"

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS provider_health (
            provider TEXT PRIMARY KEY,
            cli TEXT,
            version TEXT,
            status TEXT NOT NULL,
            last_success DATETIME,
            last_failure DATETIME,
            failure_reason TEXT,
            current_role TEXT,
            last_latency_sec REAL,
            consecutive_failures INTEGER DEFAULT 0,
            usage_percent REAL,
            remaining REAL,
            reset_at DATETIME,
            last_checked DATETIME
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS provider_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            usage_percent REAL,
            latency REAL,
            success INTEGER NOT NULL
        )
    ''')
    for prov in ("OPENAI", "ANTHROPIC", "GOOGLE"):
        cursor.execute("INSERT OR IGNORE INTO provider_health (provider, status, consecutive_failures) VALUES (?, 'HEALTHY', 0)", (prov,))
    conn.commit()
    conn.close()

    monkeypatch.setenv("DIRECTOR_TESTING", "1")
    monkeypatch.setenv("TEST_DB_PATH", str(db_path))

    from core.db_path import get_db_path
    assert get_db_path() == db_path.resolve(), "test must never touch the production DB"
    assert get_db_path() != DEFAULT_DB_PATH

    integration = request.node.get_closest_marker("integration_cli") is not None
    if integration:
        monkeypatch.setenv("DIRECTOR_ALLOW_REAL_CLI", "1")
    else:
        monkeypatch.delenv("DIRECTOR_ALLOW_REAL_CLI", raising=False)
        for site in _RESOLVE_CLI_SITES:
            monkeypatch.setattr(site, stub_resolve_cli)

    from core.process_runner import ProcessTreeRunner
    # dashboard /status cache must not hide state changes inside a test (perf tests set their own TTL)
    monkeypatch.setattr("dashboard_server.STATUS_CACHE_TTL_SEC", 0.0, raising=False)
    monkeypatch.setattr("dashboard_server._STATUS_CACHE", {"at": 0.0, "payload": None}, raising=False)
    before = len(ProcessTreeRunner.blocked_real_cli_calls)
    yield db_path
    if not integration:
        blocked = ProcessTreeRunner.blocked_real_cli_calls[before:]
        assert blocked == [], f"REAL_CLI_CALL_DURING_UNIT_TEST attempted by this test: {blocked}"


@pytest.fixture(autouse=True)
def pinned_role_settings(monkeypatch, tmp_path):
    """The suite must not depend on the operator's local data/role_settings.json. The mode/truthfulness tests
    (v0237/v0241/v0247) were written against the HEAD=OpenAI assignment, so every test starts from that manual
    assignment in a private file; tests that exercise role_settings itself re-point PATH to their own file."""
    from core import role_settings
    pinned = tmp_path / "pinned_roles.json"
    pinned.write_text(json.dumps({"head": "openai", "builder": "antigravity", "reviewer": "claude"}), encoding="utf-8")
    monkeypatch.setattr(role_settings, "PATH", pinned)


@pytest.fixture(autouse=True)
def strict_policy_unless_head_proxy(request, monkeypatch):
    """Production runs with autonomy.head_proxy=true (user decision 2026-09-29). The legacy suite verifies the strict
    human-approval policy, so it keeps running in strict mode; HEAD proxy tests opt in with @pytest.mark.head_proxy."""
    if request.node.get_closest_marker("head_proxy") is not None:
        return
    import director
    import dashboard_server
    real = director.load_config

    def strict_config():
        cfg = real()
        cfg.setdefault("autonomy", {})["head_proxy"] = False
        return cfg
    monkeypatch.setattr(director, "load_config", strict_config)
    monkeypatch.setattr(dashboard_server, "load_config", strict_config)


@pytest.fixture
def provider_state(isolate_provider_db):
    """Explicit provider state helper: provider_state(OPENAI='QUOTA_EXHAUSTED', ...) -> statuses in the test DB."""
    from core.provider_manager import ProviderManager, ProviderStatus

    def _set(**states):
        pm = ProviderManager()
        for dom, st in states.items():
            pm.set_status(dom.upper(), ProviderStatus(st) if isinstance(st, str) else st, "provider_state fixture")
        return pm
    return _set
