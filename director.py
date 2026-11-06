#!/usr/bin/env python3
import sys
import yaml
import json
from pathlib import Path
from typing import Optional
import click

from core.orchestrator import Orchestrator
from core.state_machine import StateMachine, State

def load_config() -> dict:
    cfg_path = Path(__file__).resolve().parent / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path, "r", encoding="utf-8") as f:
            from core.role_settings import apply
            return apply(yaml.safe_load(f) or {})
    return {}

@click.group()
def cli():
    """AI DEV DIRECTOR v3.1.6 - Local Autonomous Orchestrator (Subscription CLIs)"""
    pass

@cli.command()
@click.option("--project", "-p", required=True, help="Target project root directory")
@click.option("--goal", "-g", required=True, help="User goal/prompt for the run")
@click.option("--head-provider", help="Override HEAD provider (claude, codex, mock)")
def run(project: str, goal: str, head_provider: Optional[str]):
    """Start a new development run."""
    config = load_config()
    if head_provider:
        config.setdefault("head", {})["provider"] = head_provider

    click.echo(f"=== Initiating AI DEV DIRECTOR Run ===")
    click.echo(f"Project: {project}")
    click.echo(f"Goal:    {goal}\n")

    orchestrator = Orchestrator(config=config)
    try:
        summary = orchestrator.run(project_path=project, goal=goal)
        _print_summary(summary)
    except Exception as e:
        click.secho(f"\n[CRITICAL ERROR] Run aborted: {str(e)}", fg="red")
        sys.exit(1)

@cli.command()
@click.option("--run-id", "-r", help="Specific Run ID to inspect")
def status(run_id: Optional[str]):
    """Inspect status of previous runs."""
    sm = StateMachine()
    if run_id:
        run = sm.get_run(run_id)
        if not run:
            click.secho(f"Run {run_id} not found.", fg="red")
            return
        
        click.echo(f"\n=== Details for {run_id} ===")
        click.echo(f"Project:       {run['project_path']}")
        click.echo(f"Goal:          {run['goal']}")
        click.echo(f"Current State: {run['current_state']}")
        click.echo(f"Worktree:      {run['worktree_path']}")
        click.echo(f"Branch:        {run['branch_name']}")
        click.echo(f"Created At:    {run['created_at']}")
        click.echo(f"Updated At:    {run['updated_at']}\n")

        transitions = sm.get_transitions(run_id)
        click.echo("--- State History ---")
        for t in transitions:
            click.echo(f"  [{t['timestamp']}] {t['from_state']} -> {t['to_state']} : {t['details'] or ''}")
    else:
        runs = sm.list_runs()
        if not runs:
            click.echo("No runs recorded in database yet.")
            return

        click.echo(f"\n{'RUN ID':<20} {'STATE':<16} {'CREATED AT':<24} {'GOAL'}")
        click.echo("-" * 80)
        for r in runs:
            goal_preview = (r['goal'][:35] + '...') if len(r['goal']) > 35 else r['goal']
            click.echo(f"{r['run_id']:<20} {r['current_state']:<16} {r['created_at'][:19]:<24} {goal_preview}")

@cli.command()
@click.argument("run_id")
def resume(run_id: str):
    """Resume an interrupted or paused run."""
    config = load_config()
    click.echo(f"=== Resuming AI DEV DIRECTOR Run: {run_id} ===")
    orchestrator = Orchestrator(config=config)
    try:
        summary = orchestrator.resume(run_id)
        _print_summary(summary)
    except Exception as e:
        click.secho(f"\n[RESUME ERROR] {str(e)}", fg="red")
        sys.exit(1)

@cli.command()
@click.argument("goal")
@click.option("--project", "-p", default=None, help="Project folder (default: the current folder when it is a git repo)")
@click.option("--profile", type=click.Choice(["quick", "standard", "deep"]), default=None, help="Budget: quick / standard / deep")
@click.option("--no-apply", is_flag=True, help="Keep the result in the worktree instead of applying it to the project")
def do(goal: str, project: Optional[str], profile: Optional[str], no_apply: bool):
    """The compact way: `director do "캡션 검사기 만들어줘"` inside a project folder. Starts the mission through the
    running 8081 companion (so it shows on the stage) or, when no companion is up, runs it right here."""
    import os
    import urllib.request
    import uuid
    target = project or os.getcwd()
    from core import autopilot as _ap
    tag, goal = _ap.split_profile_tag(goal)
    profile = profile or tag or "standard"
    payload = {"request_id": "cli-" + uuid.uuid4().hex[:16], "project_path": str(Path(target).resolve()), "goal": goal,
               "profile": profile, "auto_apply": "off" if no_apply else None}
    token_path = Path(__file__).resolve().parent / "data" / "solomon-bridge.token"
    try:
        req = urllib.request.Request("http://127.0.0.1:8081/bridge/missions", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json", "X-Solomon-Bridge": token_path.read_text(encoding="utf-8").strip()})
        with urllib.request.urlopen(req, timeout=600) as r:
            res = json.loads(r.read().decode("utf-8"))
        click.echo(f"[{res.get('status')}] " + (res.get("mission_id") or res.get("message") or res.get("error") or ""))
        if res.get("mission_id"):
            click.echo("진행 상황: http://127.0.0.1:8081/  (스테이지에서 실시간으로 보입니다)")
        return
    except Exception as exc:
        click.echo(f"8081 companion unavailable ({str(exc)[:120]}); running the mission in this process instead.")
    ctx = click.get_current_context()
    ctx.invoke(mission, project=str(Path(target).resolve()), goal=goal, mission_id=None, max_steps=None, replan=None, profile=profile, no_apply=no_apply)


@cli.command()
@click.option("--project", "-p", required=True, help="Target project root directory")
@click.option("--goal", "-g", required=True, help="What you want built, in your own words")
@click.option("--mission-id", help="Continue an existing mission instead of starting one")
@click.option("--max-steps", type=int, help="Milestone budget for a new mission (default: autonomy.max_steps)")
@click.option("--replan", help="With --mission-id: send the blocked step back to HEAD planning, with this reason")
@click.option("--profile", type=click.Choice(["quick", "standard", "deep"]), default=None, help="Budget profile for a new mission")
@click.option("--no-apply", is_flag=True, help="Keep the result in the worktree instead of applying it to the project.")
def mission(project: str, goal: str, mission_id: Optional[str], max_steps: Optional[int], replan: Optional[str] = None,
            profile: Optional[str] = None, no_apply: bool = False):
    """Run a goal-driven mission: HEAD plans milestones and decides on your behalf until the goal is done."""
    from core import autopilot
    from core.chat_manager import ChatManager
    from core.mission import MissionRunner, MissionStore, latest_deliverable
    from core.bootstrap import prepare_project

    config = load_config()
    if max_steps:
        config.setdefault("autonomy", {})["max_steps"] = max_steps
    if no_apply:
        config.setdefault("autonomy", {})["auto_apply"] = "off"
    project = str(prepare_project(project))
    try:
        from dashboard_server import active_session_id
        sid = active_session_id()
    except Exception:
        sid = None

    def notify(text, run_id=None, role="SYSTEM"):
        click.echo(f"[{role}] {text}\n")
        if sid:
            try:
                ChatManager().add_message(sid, role, text, run_id=run_id)
            except Exception:
                pass

    runner = MissionRunner(lambda: Orchestrator(config=load_config()), MissionStore(), notify)
    if not mission_id:
        orch = Orchestrator(config=config)
        previous = latest_deliverable(project)
        context = orch._read_project_context(previous or project)
        if previous:
            notify(f"이전 미션 결과물에서 이어서 진행: {previous}")
            context["note"] = "Continuing from the latest mission deliverable; the listed files already exist there."
        from core import usage
        usage.set_context(stage="CHAT", run_id=None, mission_id=None)   # the ledger shows what this first HEAD call is
        resp = orch.head.chat(goal, context, [])
        if resp.get("type") != "DEV_TASK":
            click.echo(resp.get("message", ""))
            return
        from schemas.order import OrderSchema
        gate = autopilot.entry_gate(OrderSchema(**resp["order"]), project, orch.head, cwd=project)
        for note in gate["notes"]:
            notify("HEAD 대리 처리: " + note)
        if gate["escalate"]:
            notify(f"사용자 결정 필요: {gate['escalate']}")
            return
        mission_id = runner.create(project, goal, gate["order"], config=config, notes=gate["notes"], profile=profile, source="cli")["mission_id"]
        notify(f"미션 시작 {mission_id}")
    else:
        if no_apply:
            existing = runner.store.load(mission_id)
            existing["auto_apply"] = "off"
            runner.store.save(existing)
        if max_steps:
            existing = runner.store.load(mission_id)
            existing["max_steps"] = max(max_steps, len(existing["steps"]) + 1)
            runner.store.save(existing)
            notify(f"미션 {mission_id} 단계 예산을 {existing['max_steps']}로 조정")
        if replan:
            existing = runner.store.load(mission_id)
            existing["steps"][-1]["operator_replan"] = replan
            runner.store.save(existing)
            notify(f"미션 {mission_id}: 멈춘 단계를 운영자가 재계획으로 돌려보냄 - {replan}")
        runner.reopen(mission_id)
    final = runner.run(mission_id)
    click.echo(f"=== Mission {mission_id}: {final['status']} ===")


def _print_summary(summary: dict):
    run_id = summary.get("run_id", "UNKNOWN")
    final_state = summary.get("final_state", "UNKNOWN")
    files_changed = summary.get("files_changed", 0)
    scope_violations = summary.get("scope_violations", 0)

    # Verification details
    v_data = summary.get("verification") or {}
    test_info = "N/A"
    build_info = "N/A"
    if v_data:
        if v_data.get("build", {}).get("executed"):
            build_info = "PASS" if v_data["build"]["passed"] else "FAIL"
        else:
            build_info = "PASS (skipped)"
        if v_data.get("test", {}).get("executed"):
            test_info = "PASS" if v_data["test"]["passed"] else "FAIL"
        else:
            test_info = "PASS (skipped)"

    # Builder status
    b_data = summary.get("builder") or {}
    builder_status = b_data.get("status", "N/A")

    # Review status
    r_data = summary.get("review") or {}
    review_verdict = r_data.get("verdict", "N/A").upper()

    # Baseline details
    base_data = summary.get("baseline_verification") or {}
    base_test_info = "N/A"
    if base_data:
        b_cnt = base_data.get("test_counts", {}).get("passed", 0)
        base_test_info = f"PASS ({b_cnt} tests)" if base_data.get("passed") else "FAIL"

    head_label = summary.get("head_label") or "HEAD"
    reviewer_label = summary.get("reviewer_label") or "CODEX"
    failover_mode = summary.get("failover_mode", "NORMAL")
    mode_tag = f" [{failover_mode}]" if failover_mode != "NORMAL" else ""

    click.echo("\n" + "=" * 36)
    click.echo(f"{run_id}{mode_tag}\n")
    click.echo(f"HEAD             PASS")
    click.echo(f"BASELINE TEST    {base_test_info}")
    click.echo(f"ANTIGRAVITY      {builder_status}")
    click.echo(f"BUILD            {build_info}")
    click.echo(f"POST TEST        {test_info}")
    click.echo(f"{reviewer_label} REVIEW".ljust(17) + f"{review_verdict}")
    click.echo("")
    click.echo(f"FILES CHANGED    {files_changed}")
    click.echo(f"SCOPE VIOLATION  {scope_violations}")
    click.echo("")
    click.echo("FINAL:")
    if final_state == State.PASS:
        click.secho("READY_FOR_HUMAN_REVIEW (PASS)", fg="green", bold=True)
    elif final_state == State.HUMAN_REQUIRED:
        click.secho("HUMAN_REQUIRED", fg="yellow", bold=True)
    else:
        click.secho(f"FAIL ({final_state})", fg="red", bold=True)
    click.echo("=" * 36 + "\n")

@cli.command()
@click.option("--project", "-p", required=True, help="Target project root directory")
@click.option("--goal", "-g", required=True, help="The research question")
@click.option("--new-project", is_flag=True, help="The folder is an empty new project (no baseline tests to run).")
@click.option("--allow-degraded", is_flag=True, help="Accept running with two of the three AIs if one is unavailable.")
def research(project: str, goal: str, new_project: bool, allow_degraded: bool):
    """Run one TRIAD research cycle: independent research, cross-critique, synthesis, experiment, interpretation, memory.

    This makes about 15-20 subscription CLI calls."""
    from core.bootstrap import prepare_project
    from core.mission import MissionStore, _same_path
    from core.research_orchestrator import ResearchOrchestrator

    project = str(prepare_project(project))
    busy = [m["mission_id"] for m in MissionStore().all()
            if m.get("status") == "RUNNING" and _same_path(m.get("project_path", ""), project)]
    if busy:
        click.secho(f"A mission is running on this project: {', '.join(busy)}. Stop it or wait for it first.", fg="red")
        sys.exit(1)
    orch = ResearchOrchestrator()
    result = orch.start_session(project, goal, cycle_budget=1, allow_degraded=allow_degraded, new_project=new_project)
    session_id = result["session_id"]
    click.echo(f"TRIAD session {session_id}: {result.get('message')}")
    for run_stage in (orch.run_through_synthesis, orch.plan_experiment, orch.execute_experiment,
                      orch.run_result_interpretation, orch.run_interpretation_cross_critique,
                      orch.consolidate_research_memory):
        if result.get("human_required"):
            break
        result = run_stage(session_id)
        click.echo(f"[{result.get('current_stage')}] {result.get('message')}")
    if result.get("human_required"):
        click.secho(f"Stopped for a human decision: {result.get('message')}", fg="yellow")
        sys.exit(1)
    click.secho(f"TRIAD cycle finished: {result.get('current_stage')}", fg="green")


@cli.command("clean-worktrees")
@click.option("--project", "-p", required=True, help="Target project root directory")
@click.option("--dry-run", is_flag=True, default=False, help="List worktrees to remove without deleting them")
@click.option("--purge-reviewed", is_flag=True, default=False, help="Force remove PASS and HUMAN_REQUIRED worktrees")
def clean_worktrees(project: str, dry_run: bool, purge_reviewed: bool):
    """Safely remove completed/terminal worktrees while protecting active runs and branches."""
    import shutil
    import subprocess
    from core.git_manager import GitManager
    from core.state_machine import TERMINAL_STATES

    proj = Path(project).resolve()
    click.echo(f"Inspecting worktrees for project: {proj} (dry_run={dry_run})...")

    sm = StateMachine()
    runs = sm.list_runs()

    # Query terminal runs and active runs for this project
    terminal_worktrees = set()
    active_worktrees = set()

    for r in runs:
        try:
            r_proj = Path(r["project_path"]).resolve()
        except Exception:
            continue
        if r_proj != proj:
            continue

        wt_path = r.get("worktree_path")
        if not wt_path:
            continue
        resolved_wt = str(Path(wt_path).resolve())

        if r["current_state"] in TERMINAL_STATES:
            terminal_worktrees.add((r["run_id"], r["current_state"], resolved_wt))
        else:
            active_worktrees.add(resolved_wt)

    # Get worktrees currently registered in git
    registered_wts = set()
    try:
        raw_list = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(proj),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True
        ).stdout
        for line in raw_list.splitlines():
            if line.startswith("worktree "):
                registered_wts.add(str(Path(line.split(" ", 1)[1].strip()).resolve()))
    except Exception as e:
        click.secho(f"Warning: git worktree list query: {e}", fg="yellow")

    cleaned_count = 0
    for run_id, state, wt in terminal_worktrees:
        # Never remove an active run's worktree
        if wt in active_worktrees:
            click.echo(f"[PROTECTED] Skipping active worktree: {wt}")
            continue

        wt_path_obj = Path(wt)
        wt_exists = wt_path_obj.exists()
        wt_registered = wt in registered_wts

        if not wt_exists and not wt_registered:
            continue

        # Skip PASS and HUMAN_REQUIRED unless --purge-reviewed is True
        if state in ["PASS", "HUMAN_REQUIRED"] and not purge_reviewed:
            click.echo(f"Skipping {run_id} ({state}) by default. Use --purge-reviewed to remove.")
            continue

        # Check for uncommitted changes if worktree directory exists
        has_uncommitted = False
        if wt_exists:
            try:
                status_out = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=wt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8"
                ).stdout
                if status_out.strip():
                    has_uncommitted = True
            except Exception:
                has_uncommitted = True

        if has_uncommitted:
            click.secho(f"SKIPPED_UNREVIEWED_CHANGES: {wt}", fg="yellow")
            continue

        if dry_run:
            click.echo(f"[DRY-RUN] Would remove worktree: {wt} (Run: {run_id}, State: {state})")
            cleaned_count += 1
        else:
            click.echo(f"Removing worktree: {wt} (Run: {run_id}, State: {state})...")
            try:
                GitManager.remove_worktree(str(proj), wt, force=purge_reviewed)
                if wt_path_obj.exists():
                    shutil.rmtree(wt, ignore_errors=True)
                cleaned_count += 1
            except Exception as e:
                click.secho(f"Failed to remove {wt}: {e}", fg="red")

    if not dry_run:
        try:
            res = subprocess.run(["git", "worktree", "prune", "-v"], cwd=str(proj), capture_output=True, text=True, encoding="utf-8")
            if res.stdout.strip():
                click.echo(res.stdout.strip())
        except Exception:
            pass

    action_label = "Identified" if dry_run else "Cleaned"
    click.echo(f"\n{action_label} {cleaned_count} terminal worktree(s). Branches preserved for audit.")

if __name__ == "__main__":
    cli()
