"""
v0.2.3.9 GLOB CANONICAL CONTAINMENT FALSE POSITIVE FIX — glob patterns are never resolved as paths;
their static prefix is canonically checked; actual matched files are still resolved individually.
"""
from core.db_path import get_runs_dir
import os
import json
from pathlib import Path

import pytest

from core.safety import SafetyPolicy
from core.orchestrator import Orchestrator
from schemas.order import OrderSchema
from tests.test_v0233_codex_fixes import _make_link
from tests.test_v0231_chat_stabilization import make_git_project
from tests.test_v0234_resume_parity import _stuck_run
from tests.test_v0236_evidence_isolation import _orch_pass_head


def _order(protected=None, allowed=None):
    return OrderSchema(goal="g", allowed_paths=allowed or ["src/", "tests/"], protected_paths=protected or [])


def test_static_prefix():
    sp = SafetyPolicy.static_prefix
    assert sp("src/__pycache__/**") == "src/__pycache__"
    assert sp("tests/**/*.py") == "tests"
    assert sp("**/*.py") == ""
    assert sp(".pytest_cache/**") == ".pytest_cache"
    assert sp("src/calc.py") == "src/calc.py"
    assert sp("a/b?/c") == "a"
    assert sp("a/[xy]/c") == "a"


# ------------------------------------------------------------------ A, B, C, D: normal globs -> canonical PASS

@pytest.mark.parametrize("pattern", [".pytest_cache/**", "src/__pycache__/**", "tests/__pycache__/**", "src/**/*.py", "**/*.py"])
def test_abcd_glob_patterns_pass(tmp_path, pattern):
    wt = tmp_path / "wt"; (wt / "src").mkdir(parents=True); (wt / "tests").mkdir()
    assert SafetyPolicy.validate_order_paths(_order(protected=[pattern]), worktree_path=str(wt)) == []
    assert SafetyPolicy.canonical_within_worktree(wt, pattern) is True
    # the gate's evidence function reports PASS for the exact ORDER that failed in the real E2E
    order = _order(protected=[".pytest_cache/**", "src/__pycache__/**", "tests/__pycache__/**"], allowed=["src/calc.py", "tests/test_calc.py"])
    assert SafetyPolicy.validate_order_paths(order, worktree_path=str(wt)) == []


def test_glob_pattern_never_resolved(tmp_path, monkeypatch):
    wt = tmp_path / "wt"; wt.mkdir()
    seen = []
    real_resolve = Path.resolve
    def spy(self, *a, **k):
        seen.append(str(self)); return real_resolve(self, *a, **k)
    monkeypatch.setattr(Path, "resolve", spy)
    SafetyPolicy.validate_order_paths(_order(protected=["src/**/*.py", "a/b?/c", "x/[ab]/**"]), worktree_path=str(wt))
    assert not any(SafetyPolicy.has_glob(s) for s in seen), seen


def test_glob_rules_match_subtrees_without_matching_siblings():
    assert SafetyPolicy.is_path_allowed("src/a.py", ["src/**"])
    assert SafetyPolicy.is_path_allowed("src/deep/a.py", ["src/**"])
    assert not SafetyPolicy.is_path_allowed("src2/a.py", ["src/**"])
    assert SafetyPolicy.is_path_allowed("src/a.py", ["src/*.py"])
    assert not SafetyPolicy.is_path_allowed("src/deep/a.py", ["src/*.py"])
    assert SafetyPolicy.is_path_protected("docs/release/note.md", ["docs/**"])
    assert not SafetyPolicy.is_path_protected("docs2/release/note.md", ["docs/**"])


# ------------------------------------------------------------------ E, F: lexical safety unchanged (glob or not)

@pytest.mark.parametrize("bad", ["../outside/**", "C:\\temp\\**", "C:/temp/**", "/abs/**", "\\\\server\\share\\**", "~/x/**", "src/../../**", "..", "a\x00b/**"])
def test_ef_traversal_absolute_unc_rejected(tmp_path, bad):
    wt = tmp_path / "wt"; wt.mkdir()
    v = SafetyPolicy.validate_order_paths(_order(protected=[bad]), worktree_path=str(wt))
    assert v and "protected_paths" in v[0]
    v2 = SafetyPolicy.validate_order_paths(_order(protected=[bad]))          # lexical check even without a worktree
    assert v2


# ------------------------------------------------------------------ G: junction under a glob prefix -> actual matched path FAIL

def test_g_symlink_under_glob_prefix_caught_on_actual_path(tmp_path):
    outside = tmp_path / "outside"; outside.mkdir(); (outside / "x.py").write_text("x=1\n")
    wt = tmp_path / "wt"; (wt / "src").mkdir(parents=True)
    if not _make_link(wt / "src" / "escape", outside):
        pytest.skip("symlink/junction not supported")
    # pattern itself: static prefix 'src' is inside -> the pattern is acceptable (planning only)
    assert SafetyPolicy.validate_order_paths(_order(allowed=["src/**"]), worktree_path=str(wt)) == []
    # a pattern whose static prefix IS the junction resolves outside -> FAIL
    v = SafetyPolicy.validate_order_paths(_order(allowed=["src/escape/**"]), worktree_path=str(wt))
    assert v and "resolves outside worktree" in v[0]
    # actual matched file through the junction -> Scope Guard blocks on the real path
    ok, viol = SafetyPolicy.validate_scope_post_build(_order(allowed=["src/**"]), [(1, 0, "src/escape/x.py")], [], "", [], worktree_path=str(wt))
    assert not ok and any("escapes worktree" in x for x in viol)
    ok2, viol2 = SafetyPolicy.validate_scope_post_build(_order(allowed=["src/**"]), [(1, 0, "src/calc.py")], [], "", [], worktree_path=str(wt))
    assert not any("escapes worktree" in x for x in viol2)


# ------------------------------------------------------------------ final gate on the real E2E ORDER shape -> PASS

def test_final_gate_passes_with_glob_protected_paths(isolate_provider_db):
    with make_git_project() as proj:
        order = OrderSchema(goal="g", allowed_paths=["src/calc.py", "tests/test_calc.py"],
                            protected_paths=[".pytest_cache/**", "src/__pycache__/**", "tests/__pycache__/**"])
        orch = _orch_pass_head()
        run_id, wt, sm = _stuck_run(proj, "DECIDING", order)
        (Path(wt) / ".pytest_cache").mkdir(exist_ok=True)
        orch._save_json(run_id, "baseline_verification.json", {"passed": True, "test_counts": {"passed": 1}})
        orch._save_json(run_id, "verification.json", {"passed": True, "git_diff": "", "test_counts": {"passed": 2}})
        orch._save_json(run_id, "review.json", {"verdict": "pass", "p0": [], "p1": [], "p2": [], "summary": "ok"})
        ev = orch.final_evidence(run_id, order, wt)
        assert ev["canonical_containment"] == "PASS" and ev["reasons"] == []
        summary = orch.resume(run_id)
        assert summary["final_state"] == "PASS"
        saved = json.loads((get_runs_dir() / run_id / "final_evidence.json").read_text(encoding="utf-8"))
        assert saved["canonical_containment"] == "PASS"
