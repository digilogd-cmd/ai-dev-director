"""TRIAD experiment adapter over the unchanged NORMAL Orchestrator pipeline."""
from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Optional

from core.greenfield import (
    BASELINE_NOT_APPLICABLE_GREENFIELD,
    PROJECT_TYPE_GREENFIELD,
    assess_project_type,
)
from core.orchestrator import Orchestrator
from core.state_machine import State


class TriadExperimentOrchestrator(Orchestrator):
    """NORMAL pipeline with one narrowly scoped GREENFIELD baseline adapter.

    All ORDER, worktree, Builder, Scope Guard, post-build Verifier, regression,
    Reviewer, decision, and final containment methods are inherited unchanged.
    Existing-project behavior delegates to the normal baseline implementation.
    """

    def __init__(self, *args, greenfield_project_evidence: Optional[Dict[str, Any]] = None,
                 greenfield_baseline_evidence: Optional[Dict[str, Any]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._triad_project_evidence = greenfield_project_evidence
        self._triad_greenfield_baseline = greenfield_baseline_evidence

    def _has_greenfield_exemption(self) -> bool:
        project = self._triad_project_evidence or {}
        baseline = self._triad_greenfield_baseline or {}
        return (
            project.get("project_type") == PROJECT_TYPE_GREENFIELD
            and project.get("greenfield_qualified") is True
            and baseline.get("baseline_state") == BASELINE_NOT_APPLICABLE_GREENFIELD
            and baseline.get("applicable") is False
            and baseline.get("passed") is None
            and baseline.get("test_counts") is None
            and baseline.get("project_evidence_sha256") == project.get("evidence_sha256")
        )

    def baseline_evidence(self, run_id: str) -> Optional[Dict[str, Any]]:
        if not self._has_greenfield_exemption():
            return super().baseline_evidence(run_id)
        data = self._load_json(run_id, "baseline_verification.json")
        if not isinstance(data, dict):
            return None
        if (
            data.get("baseline_state") != BASELINE_NOT_APPLICABLE_GREENFIELD
            or data.get("applicable") is not False
            or data.get("passed") is not None
            or data.get("test_counts") is not None
            or data.get("project_evidence_sha256") != self._triad_project_evidence.get("evidence_sha256")
        ):
            return None
        return data

    @staticmethod
    def _same_content_as(worktree_path: str, head: Optional[str], expected: Optional[str]) -> bool:
        """True when head is the qualified commit, or the private snapshot of it that core.bootstrap makes for every
        worktree: its only parent is the qualified commit and its tree is unchanged."""
        if not head or not expected:
            return False
        if head == expected:
            return True

        def rev(spec):
            r = subprocess.run(["git", "rev-parse", "--verify", spec], cwd=str(worktree_path),
                               capture_output=True, text=True)
            return r.stdout.strip() if r.returncode == 0 else None
        tree = rev(f"{head}^{{tree}}")
        return (rev(f"{head}^1") == expected and rev(f"{head}^2") is None
                and tree is not None and tree == rev(f"{expected}^{{tree}}"))

    def run_baseline_if_required(self, run_id: str, worktree_path: str, logger, provenance_records,
                                 force: bool = False, allow_measure: bool = True):
        if not self._triad_greenfield_baseline and not self._triad_project_evidence:
            return super().run_baseline_if_required(
                run_id, worktree_path, logger, provenance_records, force=force, allow_measure=allow_measure
            )
        if not self._has_greenfield_exemption():
            msg = "GREENFIELD_BASELINE_INVALID: explicit qualification evidence is missing or malformed"
            return None, self._stop(
                run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "GREENFIELD_BASELINE_INVALID"
            )
        existing = None if force else self.baseline_evidence(run_id)
        if existing is not None:
            return 0, None
        if not allow_measure or not self._worktree_is_pristine(worktree_path):
            msg = "GREENFIELD_BASELINE_INVALID: isolated worktree is missing or not pristine before Builder"
            return None, self._stop(
                run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "GREENFIELD_BASELINE_INVALID"
            )

        fresh = assess_project_type(worktree_path, user_explicit_new_project=True)
        expected_head = self._triad_project_evidence.get("head_commit")
        if (
            fresh.get("project_type") != PROJECT_TYPE_GREENFIELD
            or not self._same_content_as(worktree_path, fresh.get("head_commit"), expected_head)
        ):
            msg = "GREENFIELD_BASELINE_INVALID: worktree no longer matches qualified empty-project evidence"
            return None, self._stop(
                run_id, State.HUMAN_REQUIRED, msg, logger, provenance_records, "GREENFIELD_BASELINE_INVALID"
            )

        baseline = dict(self._triad_greenfield_baseline)
        baseline["worktree_path"] = str(worktree_path)
        baseline["worktree_project_evidence"] = fresh
        self._save_json(run_id, "baseline_verification.json", baseline)
        self.sm.log_audit(
            run_id, "ORCHESTRATOR", BASELINE_NOT_APPLICABLE_GREENFIELD,
            json.dumps({"project_evidence_sha256": baseline["project_evidence_sha256"],
                        "head_commit": expected_head}, ensure_ascii=False),
        )
        logger.info("[Baseline Gate] NOT_APPLICABLE_GREENFIELD recorded; this is not a test PASS")
        return 0, None

    def final_evidence(self, run_id: str, order, worktree_path) -> Dict[str, Any]:
        evidence = super().final_evidence(run_id, order, worktree_path)
        if not self._has_greenfield_exemption():
            return evidence
        baseline = self.baseline_evidence(run_id)
        if baseline is None:
            return evidence

        evidence["baseline"] = BASELINE_NOT_APPLICABLE_GREENFIELD
        evidence["greenfield_baseline"] = "PASS"
        evidence["reasons"] = [reason for reason in evidence["reasons"] if reason != "BASELINE_FAILED"]
        # There is no pre-build test count to compare.  Regression is satisfied only by a real successful
        # post-build verification, never by the exemption itself.
        if evidence.get("verification") == "PASS":
            evidence["regression"] = "PASS"
        else:
            evidence["regression"] = "MISSING" if evidence.get("verification") == "MISSING" else "FAIL"
        return evidence
