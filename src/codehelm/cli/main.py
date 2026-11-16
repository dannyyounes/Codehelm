from pathlib import Path
import json
import sqlite3

import typer
import yaml
from codehelm.execution.worktrees import WorktreeManager, WorktreeError
from codehelm.providers.adapters import CLIProvider, ProviderError, provider_names
from codehelm.orchestrator.lifecycle import ClaimLost, LifecycleEngine, TaskStore
from codehelm.integrations.github_pr import PullRequestError, github_user_login
from codehelm.integrations.jira import JiraClient, JiraError
from codehelm.orchestrator.scheduler import ready_stories, dispatch_batch
from codehelm.orchestrator.durable import (
    DurableScheduler, DispatchBudget, resolve_max_attempts, parse_resume_task_id,
)
from codehelm.orchestrator.budgets import BudgetLedger, Limits
from codehelm.providers.usage import parse_usage
from codehelm.orchestrator.monthly_budget import (
    MonthlyBudget, MonthlyPolicy, parse_override_amount)
from decimal import Decimal
from codehelm.planning.planner import (generate_plan, load_plan, save_plan,
                                      publish_plan_to_jira, PlanningError)
from codehelm.quality.contract import assess, decision_error, load_policy, task_attempt
from codehelm.quality.diffdigest import diff_digest

app = typer.Typer(help="CodeHelm: autonomous engineering orchestrator (early scaffold).")
STATE_DIR = Path(".codehelm")


@app.command()
def init() -> None:
    """Initialise local configuration and task state."""
    STATE_DIR.mkdir(exist_ok=True)
    config = STATE_DIR / "config.yaml"
    if not config.exists():
        config.write_text(yaml.safe_dump({
            "project": {"name": Path.cwd().name},
            "jira": {"enabled": False},
            "providers": {},
            "limits": {"max_retries": 2},
        }, sort_keys=False), encoding="utf-8")
    TaskStore(STATE_DIR / "state.db").initialize()
    typer.echo(f"Initialised CodeHelm in {STATE_DIR.resolve()}")


@app.command()
def status() -> None:
    """Show local scaffold status."""
    if not (STATE_DIR / "state.db").exists():
        typer.echo("Not initialised. Run: codehelm init")
        raise typer.Exit(code=1)
    with sqlite3.connect(STATE_DIR / "state.db") as db:
        count = db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    typer.echo(f"CodeHelm scaffold ready. Stored tasks: {count}. Agent lifecycle execution available; test/security/review gates available; GitHub draft PR and basic Jira commands available.")


@app.command()
def run(
    jira: str = typer.Option(..., help="Task ID; Jira retrieval is a separate command"),
    prompt: str = typer.Option(..., help="Task instructions"),
    provider: str = typer.Option("claude", help="Installed CLI provider"),
    repo: Path = typer.Option(Path("."), help="Git repository"),
    timeout: int = typer.Option(600, min=1, max=3600),
    execute: bool = typer.Option(False, help="Explicitly permit agent execution"),
) -> None:
    """Create worktree, execute agent and persist state."""
    if not execute:
        typer.echo("Dry run: no workspace created or agent launched. Add --execute.")
        return
    try:
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        result = engine.execute(jira, prompt, provider, timeout)
    except (ValueError, WorktreeError, ProviderError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.output:
        typer.echo(result.output)
    if result.error:
        typer.echo(result.error, err=True)
    typer.echo(f"Task {jira}: {'awaiting_validation' if result.exit_code == 0 else 'failed'}")
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


@app.command("validate")
def validate(
    task_id: str,
    reviewer: str = typer.Option(..., help="Independent CLI provider"),
    test_cmd: str = typer.Option(..., help="Executable for tests, e.g. pytest"),
    security_cmd: str = typer.Option(..., help="Executable for security scan, e.g. bandit"),
    repo: Path = typer.Option(Path("."), help="Original Git repository"),
    execute: bool = typer.Option(False, help="Explicitly permit checks and review agent"),
) -> None:
    """Run configured test, security and independent review gates."""
    if not execute:
        typer.echo("Dry run. Add --execute to run checks and reviewer.")
        return
    try:
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        passed = engine.validate(task_id, [test_cmd], [security_cmd], reviewer)
    except (ValueError, WorktreeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    state = engine.store.get(task_id)
    typer.echo(f"{task_id}: {state.status}; {state.error or ''}")
    if not passed:
        raise typer.Exit(code=1)


@app.command("document")
def document(
    task_id: str,
    provider: str = typer.Option(..., help="Documentation agent CLI provider"),
    repo: Path = typer.Option(Path("."), help="Original Git repository"),
    execute: bool = typer.Option(False, help="Explicitly permit documentation agent execution"),
) -> None:
    """Update task documentation after successful independent code review."""
    if not execute:
        typer.echo("Dry run. Add --execute to launch the documentation agent.")
        return
    try:
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        passed = engine.document(task_id, provider)
    except (ValueError, WorktreeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    task = engine.store.get(task_id)
    typer.echo(f"{task_id}: {task.status}; {task.error or ''}")
    if not passed:
        raise typer.Exit(code=1)


@app.command("validate-after-docs")
def validate_after_docs(
    task_id: str,
    reviewer: str = typer.Option(..., help="Independent CLI provider"),
    test_cmd: str = typer.Option(..., help="Executable for tests"),
    security_cmd: str = typer.Option(..., help="Executable for security scan"),
    repo: Path = typer.Option(Path("."), help="Original Git repository"),
    execute: bool = typer.Option(False, help="Explicitly permit checks and review agent"),
) -> None:
    """Re-run tests, security and independent review after documentation."""
    if not execute:
        typer.echo("Dry run. Add --execute to run post-documentation gates.")
        return
    try:
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        passed = engine.validate_after_docs(task_id, [test_cmd], [security_cmd], reviewer)
    except (ValueError, WorktreeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    task = engine.store.get(task_id)
    typer.echo(f"{task_id}: {task.status}; {task.error or ''}")
    if not passed:
        raise typer.Exit(code=1)


@app.command("pr-create")
def pr_create(
    task_id: str,
    title: str = typer.Option(..., help="Pull request title"),
    body: str = typer.Option(..., help="Pull request description"),
    base: str = typer.Option("main", help="Target branch"),
    repo: Path = typer.Option(Path("."), help="Original Git repository"),
    execute: bool = typer.Option(False, help="Explicitly push branch and create draft PR"),
) -> None:
    """Create a draft GitHub PR after all implemented gates pass."""
    if not execute:
        typer.echo("Dry run. Add --execute to push branch and create a draft PR.")
        return
    try:
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        url = engine.create_pull_request(task_id, title, body, base)
    except (ValueError, WorktreeError, PullRequestError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Draft pull request created: {url}")


@app.command("monthly-budget")
def monthly_budget_status(month: str = typer.Option(None)) -> None:
    """Show additional AUD charge allowance, excluding existing subscriptions."""
    typer.echo(str(MonthlyBudget(STATE_DIR / "state.db").status(month)))


@app.command("budget-approve")
def budget_approve(
    task_id: str,
    epic: str = typer.Option(..., help="Epic this one-shot override applies to"),
    amount: str = typer.Option(..., help="Exact additional AUD amount to authorize once"),
    approver: str = typer.Option(..., help="Must exactly match the authenticated GitHub login"),
    reason: str = typer.Option(..., help="Why this one reservation may pass the cap"),
    execute: bool = typer.Option(False, help="Record the override after checking the GitHub login"),
) -> None:
    """Record a GitHub login and a reason for one exact task, epic, and amount."""
    try:
        parsed = parse_override_amount(amount)
        if not reason.strip():
            raise ValueError("Budget override reason is required")
        if not task_id.strip() or not epic.strip():
            raise ValueError("Invalid budget override")
        if not execute:
            typer.echo(
                "Dry run. Add --execute to record a one-shot budget override "
                "for this task, epic, and amount. GitHub is not called.")
            return
        login = github_user_login()
        if approver.strip() != login:
            raise PullRequestError(
                f"Approver {approver.strip()!r} does not match authenticated GitHub login {login!r}")
        MonthlyBudget(STATE_DIR / "state.db").record_override(
            task_id, epic, parsed, login, reason.strip())
    except (ValueError, ArithmeticError, PullRequestError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Recorded budget override by {login} for {task_id}.")


@app.command("usage-import")
def usage_import(
    task_id: str,
    provider: str = typer.Option(..., help="claude, codex, or gemini"),
    usage_file: Path = typer.Option(..., help="Verified structured provider usage JSON"),
    prefix: str = typer.Option("PLAN"),
    execute: bool = typer.Option(False, help="Record verified token and USD usage"),
) -> None:
    """Import provider-reported usage; never fabricate missing USD cost."""
    try:
        usage = parse_usage(provider, usage_file.read_text(encoding="utf-8"))
        if usage is None:
            raise ValueError("Unrecognized or incomplete provider usage JSON")
        if usage.usd is None:
            typer.echo(f"Tokens: {usage.tokens}; USD cost unavailable. Reservation retained.")
            return
        if not execute:
            typer.echo(f"Verified JSON reports {usage.tokens} tokens and USD {usage.usd}. "
                       "Add --execute to reconcile.")
            return
        BudgetLedger(STATE_DIR / "state.db").reconcile(
            prefix, task_id, usage.tokens, usage.usd)
        typer.echo(f"Reconciled {task_id}: {usage.tokens} tokens, USD {usage.usd}")
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("budget-status")
def budget_status(prefix: str = typer.Option("PLAN")) -> None:
    """Show reserved-or-reconciled usage; not a live provider bill."""
    ledger = BudgetLedger(STATE_DIR / "state.db")
    typer.echo(str(ledger.report(prefix)))


@app.command("budget-reserve")
def budget_reserve(
    task_id: str,
    prefix: str = typer.Option("PLAN"),
    provider: str = typer.Option("claude"),
    tokens: int = typer.Option(..., min=1),
    usd: str = typer.Option(...),
    limit_tokens: int = typer.Option(..., min=1),
    limit_usd: str = typer.Option(...),
    execute: bool = typer.Option(False),
) -> None:
    """Reserve estimated usage; does not enforce a provider-side cap."""
    if not execute:
        typer.echo("Dry run. Add --execute to reserve budget.")
        return
    try:
        ledger = BudgetLedger(STATE_DIR / "state.db")
        ledger.reserve(prefix, task_id, provider, tokens, Decimal(usd),
                       Limits(limit_tokens, Decimal(limit_usd)))
        typer.echo(f"Reserved {tokens} tokens and USD {usd} for {task_id}")
    except (ValueError, ArithmeticError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("budget-reconcile")
def budget_reconcile(
    task_id: str,
    prefix: str = typer.Option("PLAN"),
    tokens: int = typer.Option(..., min=0),
    usd: str = typer.Option(...),
    execute: bool = typer.Option(False),
) -> None:
    """Record externally verified provider usage, never a guessed zero."""
    if not execute:
        typer.echo("Dry run. Add --execute after verifying usage.")
        return
    try:
        BudgetLedger(STATE_DIR / "state.db").reconcile(prefix, task_id, tokens, Decimal(usd))
        typer.echo(f"Reconciled {task_id}")
    except (ValueError, ArithmeticError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule-durable")
def schedule_durable(
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    provider: str = typer.Option("claude"),
    max_parallel: int = typer.Option(2, min=1, max=8),
    max_attempts: int | None = typer.Option(None, min=1, max=5),
    max_total_attempts: int = typer.Option(20, min=1, max=100),
    repo: Path = typer.Option(Path(".")),
    reserve_tokens: int = typer.Option(..., min=1, help="Estimated tokens per task"),
    reserve_usd: str = typer.Option(..., help="Estimated USD per task"),
    limit_tokens: int = typer.Option(..., min=1, help="Total plan token budget"),
    limit_usd: str = typer.Option(..., help="Total plan USD budget"),
    heartbeat_seconds: int = typer.Option(30, min=5, max=300),
    additional_aud_per_task: str = typer.Option("0", help="Estimated additional AUD charge per task; subscription-only=0"),
    reviewer: str = typer.Option("", help="Independent reviewer used when a quality policy exists"),
    test_cmd: str = typer.Option("pytest", help="Test executable used when a quality policy exists"),
    security_cmd: str = typer.Option("bandit", help="Security executable used when a quality policy exists"),
    execute: bool = typer.Option(False),
) -> None:
    """Claim one ready wave. A quality policy continues that claim through the gates."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Thread
    try:
        plan_obj = load_plan(plan_file)
        scheduler = DurableScheduler(STATE_DIR / "state.db", plan_obj, prefix)
        policy = load_policy(STATE_DIR / "quality.yaml")
        budget = DispatchBudget(
            resolve_max_attempts(policy, max_attempts), max_total_attempts, max_parallel)
        if not execute:
            typer.echo(f"Durable state: {scheduler.snapshot()}")
            typer.echo("Dry run. Add --execute to claim and launch one wave.")
            if policy.present:
                typer.echo("Quality policy active: execution continues through quality gates.")
            return
        if policy.present and (policy.require_review or policy.require_architecture):
            if not reviewer or reviewer == provider:
                raise ValueError("Quality policy requires --reviewer set to a different provider")
        limits = Limits(limit_tokens, Decimal(limit_usd))
        resumed = scheduler.claim_resumable(budget)
        chosen = scheduler.claim_budgeted(budget, provider, reserve_tokens,
                                          Decimal(reserve_usd), limits,
                                          MonthlyPolicy(), Decimal(additional_aud_per_task))
        wave = resumed + [sid for sid in chosen if sid not in resumed]
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        stories = {story.id: story for story in plan_obj.stories}
        def launch(sid: str):
            story = stories[sid]
            prompt = (f"Implement {sid}: {story.title}\\n{story.description}\\n"
                      "Acceptance criteria:\\n" + "\\n".join(story.acceptance_criteria))
            stop = Event()
            def keep_alive():
                while not stop.wait(heartbeat_seconds):
                    if not scheduler.heartbeat(sid):
                        break
            heartbeat_thread = Thread(target=keep_alive, daemon=True)
            heartbeat_thread.start()
            try:
                status = scheduler.carry_story(
                    sid, engine, prompt=prompt, provider=provider,
                    test_command=[test_cmd], security_command=[security_cmd],
                    reviewer=reviewer, docs_provider=provider,
                    heartbeat=lambda: scheduler.heartbeat(sid))
                return sid, status
            except ClaimLost:
                return sid, "claimed"
            except Exception as exc:
                try:
                    scheduler.settle(sid, "failed", str(exc))
                except ValueError:
                    pass
                return sid, "failed"
            finally:
                stop.set()
                heartbeat_thread.join(timeout=2)
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            results = list(pool.map(launch, wave))
        for sid, status in results:
            typer.echo(f"{sid}: {status}")
        if not wave:
            typer.echo("No stories eligible under dependencies and budget.")
    except (ValueError, PlanningError, OSError, ArithmeticError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule-complete")
def schedule_complete(
    story_id: str,
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    confirmed_merged: bool = typer.Option(False, help="Operator asks to complete after GitHub reports the PR merged"),
    execute: bool = typer.Option(False),
) -> None:
    """Unlock dependencies only when gh reports the recorded pull request merged."""
    if not execute or not confirmed_merged:
        typer.echo("No change. Requires --confirmed-merged --execute after verifying the PR.")
        return
    try:
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        scheduler.complete(story_id, human_verified_merge=True)
        typer.echo(f"{story_id}: completed; dependent stories may now be scheduled.")
    except (ValueError, PlanningError, OSError, ArithmeticError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule-checkpoint")
def schedule_checkpoint(
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
) -> None:
    """Persist a scheduler state snapshot without launching agents."""
    try:
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        state = scheduler.checkpoint()
        typer.echo(f"Checkpoint saved for {prefix}: {len(state)} stories")
    except (ValueError, PlanningError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule-recover")
def schedule_recover(
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    lease_seconds: int = typer.Option(900, min=60, max=86400),
) -> None:
    """Flag expired claims for manual inspection; does not relaunch agents."""
    try:
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        count = scheduler.recover_expired(DispatchBudget(lease_seconds=lease_seconds))
        typer.echo(f"Marked {count} expired claims interrupted. Inspect before retry.")
    except (ValueError, PlanningError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("agents")
def agents(
    limit: int = typer.Option(20, min=1, max=100, help="Maximum number of tasks to list"),
) -> None:
    """List stored tasks. Does not launch an agent or contact the network."""
    rows = TaskStore(STATE_DIR / "state.db").list_recent(limit)
    if not rows:
        typer.echo("No tasks are recorded.")
        return
    for task_id, status, provider, updated_at in rows:
        typer.echo(f"{task_id} {status} {provider or '-'} {updated_at}")


@app.command("resume")
def resume(
    task_id: str = typer.Argument(..., help="Stored task id shaped PREFIX-STORY-An"),
    plan_file: Path = typer.Option(Path(".codehelm/plan.json"), help="Plan used by the durable scheduler"),
    prefix: str = typer.Option("", help="Must match the prefix parsed from the task id"),
    reviewer: str = typer.Option("", help="Independent reviewer used when a quality policy exists"),
    test_cmd: str = typer.Option("pytest", help="Test executable used when a quality policy exists"),
    security_cmd: str = typer.Option("bandit", help="Security executable used when a quality policy exists"),
    repo: Path = typer.Option(Path("."), help="Git repository"),
    execute: bool = typer.Option(False, help="Re-claim the interrupted story and carry the same attempt"),
) -> None:
    """Continue one interrupted story on the same attempt. Does not relaunch a coding agent."""
    try:
        parsed_prefix, story_id, attempt = parse_resume_task_id(task_id)
        if not execute:
            typer.echo(
                f"Dry run: resume {task_id} on the same attempt. "
                "Dispatch state unchanged and no coding agent launched.")
            return
        if not prefix:
            raise ValueError("resume --execute requires --prefix")
        if prefix != parsed_prefix:
            raise ValueError(f"Prefix must equal the prefix in {task_id}")
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        task = scheduler.store.get(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        if task.status in {"queued", "running", "failed"}:
            raise ValueError(f"Coding agent will not be relaunched for {task_id}")
        snapshot = scheduler.snapshot()
        if story_id not in snapshot or snapshot[story_id][0] != "interrupted":
            raise ValueError(
                f"Story {story_id} is not interrupted. schedule-retry starts a new attempt")
        if snapshot[story_id][1] != attempt:
            raise ValueError("Task id does not match the interrupted attempt")
        policy = load_policy(STATE_DIR / "quality.yaml")
        provider = task.provider or ""
        if policy.present and (policy.require_review or policy.require_architecture):
            if not reviewer or reviewer == provider:
                raise ValueError("Quality policy requires --reviewer set to a different provider")
        scheduler.reclaim_interrupted(story_id)
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        story = next(item for item in scheduler.plan.stories if item.id == story_id)
        prompt = (f"Implement {story_id}: {story.title}\\n{story.description}\\n"
                  "Acceptance criteria:\\n" + "\\n".join(story.acceptance_criteria))
        status = scheduler.carry_story(
            story_id, engine, prompt=prompt, provider=provider,
            test_command=[test_cmd], security_command=[security_cmd],
            reviewer=reviewer, docs_provider=provider,
            heartbeat=lambda: scheduler.heartbeat(story_id))
        typer.echo(f"{story_id}: {status}")
    except (ValueError, PlanningError, ClaimLost, WorktreeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule-retry")
def schedule_retry(
    story_id: str,
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    max_attempts: int | None = typer.Option(None, min=1, max=5),
    execute: bool = typer.Option(False),
) -> None:
    """Authorize a retry after manually inspecting a failed/interrupted attempt."""
    if not execute:
        typer.echo("Dry run. Add --execute to authorize a retry.")
        return
    try:
        policy = load_policy(STATE_DIR / "quality.yaml")
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        scheduler.retry(
            story_id, DispatchBudget(max_attempts=resolve_max_attempts(policy, max_attempts)))
        typer.echo(f"{story_id}: pending retry")
    except (ValueError, PlanningError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule")
def schedule(
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    completed: str = typer.Option("", help="Comma-separated story IDs; refused when a quality policy exists"),
    provider: str = typer.Option("claude", help="Comma-separated CLI providers"),
    prefix: str = typer.Option("PLAN", help="Task ID prefix"),
    max_parallel: int = typer.Option(2, min=1, max=8),
    repo: Path = typer.Option(Path("."), help="Git repository"),
    execute: bool = typer.Option(False, help="Explicitly launch dependency-ready coding agents"),
) -> None:
    """Preview or dispatch one ready wave; no automatic dependency completion."""
    try:
        plan_obj = load_plan(plan_file)
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        done = {v.strip() for v in completed.split(",") if v.strip()}
        if done and load_policy(STATE_DIR / "quality.yaml").present:
            raise ValueError(
                "schedule --completed is refused while a quality policy exists; "
                "use schedule-complete after ready_for_pr")
        dispatched = {story.id for story in plan_obj.stories
                      if engine.store.get(f"{prefix}-{story.id}") is not None}
        pending = ready_stories(plan_obj, done, dispatched)
        if not execute:
            typer.echo("Ready stories: " + (", ".join(s.id for s in pending) or "(none)"))
            typer.echo("Dry run. Add --execute to launch up to max-parallel agents.")
            return
        results = dispatch_batch(plan_obj, done, dispatched,
                                 [v.strip() for v in provider.split(",")],
                                 prefix, max_parallel, engine.execute)
        for item in results:
            typer.echo(f"{item.story_id} -> {item.task_id}: {item.status} ({item.provider})")
        if not results:
            typer.echo("No dependency-ready stories to dispatch.")
        if any(item.status == "failed" for item in results):
            raise typer.Exit(code=1)
    except (PlanningError, ValueError, WorktreeError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("plan")
def plan(
    goal: str = typer.Option(..., help="High-level engineering objective"),
    provider: str = typer.Option("claude", help="Planning CLI provider"),
    repo: Path = typer.Option(Path("."), help="Repository context"),
    output: Path = typer.Option(Path(".codehelm/plan.json"), help="Local plan JSON"),
    execute: bool = typer.Option(False, help="Explicitly run the planning agent"),
) -> None:
    """Generate and validate an Epic/Stories plan locally; no Jira writes."""
    if not execute:
        typer.echo("Dry run. Add --execute to invoke the planner and save a local plan.")
        return
    try:
        result = generate_plan(goal, provider, repo)
        save_plan(result, output)
    except (PlanningError, ProviderError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Saved plan: {output} ({len(result.stories)} stories). Review before publishing.")


@app.command("plan-publish")
def plan_publish(
    project: str = typer.Option(..., help="Jira project key"),
    plan_file: Path = typer.Option(Path(".codehelm/plan.json"), help="Reviewed plan JSON"),
    execute: bool = typer.Option(False, help="Explicitly create Jira Epic and Stories"),
) -> None:
    """Publish a reviewed plan to Jira; external writes require --execute."""
    if not execute:
        typer.echo("Dry run. Add --execute to create Jira issues.")
        return
    try:
        plan_obj = load_plan(plan_file)
        mapping = publish_plan_to_jira(plan_obj, project, JiraClient.from_env())
    except (PlanningError, JiraError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for local_id, jira_key in mapping.items():
        typer.echo(f"{local_id}: {jira_key}")
    typer.echo("Published Jira issues. No agents launched and no Jira statuses changed.")


@app.command("jira-get")
def jira_get(key: str) -> None:
    """Fetch a Jira issue using environment-based credentials."""
    try:
        issue = JiraClient.from_env().issue(key)
        typer.echo(f"{issue.get('key')}: {issue.get('fields', {}).get('summary', '')}")
    except JiraError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("jira-create")
def jira_create(
    project: str = typer.Option(...),
    kind: str = typer.Option(..., help="Epic, Story or Bug"),
    summary: str = typer.Option(...),
    description: str = typer.Option(""),
    parent: str = typer.Option("", help="Parent epic key for a Story"),
    execute: bool = typer.Option(False, help="Explicitly create a Jira issue"),
) -> None:
    """Create an Epic, Story or Bug, optionally beneath an Epic."""
    if not execute:
        typer.echo("Dry run. Add --execute to create an issue in Jira.")
        return
    try:
        issue = JiraClient.from_env().create_issue(
            project, summary, kind, description, parent or None)
        typer.echo(f"Created Jira issue: {issue.get('key', '<no key returned>')}")
    except JiraError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("jira-link-pr")
def jira_link_pr(
    task_id: str,
    execute: bool = typer.Option(False, help="Explicitly post a Jira comment"),
) -> None:
    """Post the previously recorded draft PR URL to its Jira issue."""
    if not execute:
        typer.echo("Dry run. Add --execute to post a PR link to Jira.")
        return
    try:
        store = TaskStore(STATE_DIR / "state.db")
        store.initialize()
        task = store.get(task_id)
        if task is None or task.status != "pr_created" or not task.error:
            raise JiraError("Task must have a recorded draft PR before linking")
        if not task.error.startswith("https://github.com/"):
            raise JiraError("Stored PR URL is invalid")
        JiraClient.from_env().comment(task_id, "CodeHelm draft pull request: " + task.error)
        typer.echo(f"Linked draft PR to {task_id}; Jira status unchanged")
    except (JiraError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("jira-transition")
def jira_transition(
    key: str,
    transition_id: str = typer.Option(..., "--id", help="Jira transition id"),
    execute: bool = typer.Option(False, help="Explicitly post one Jira transition"),
) -> None:
    """Post one Jira transition id. The scheduler does not call this."""
    if not execute:
        typer.echo(
            f"Dry run: would post transition {transition_id} on {key}. Jira is not called.")
        return
    try:
        JiraClient.from_env().transition(key, transition_id)
        typer.echo(f"Posted Jira transition {transition_id} on {key}")
    except JiraError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("task-status")
def task_status(task_id: str) -> None:
    """Inspect persisted task state."""
    db = STATE_DIR / "state.db"
    if not db.exists():
        typer.echo("No task database. Run: codehelm init")
        raise typer.Exit(code=1)
    store = TaskStore(db)
    store.initialize()
    task = store.get(task_id)
    if task is None:
        typer.echo(f"Unknown task: {task_id}")
        raise typer.Exit(code=1)
    typer.echo(f"{task.id}: {task.status} | provider={task.provider} | workspace={task.workspace}")


@app.command("worktree-create")
def worktree_create(task_id: str, repo: Path = typer.Option(Path("."), help="Git repository")) -> None:
    """Create an isolated Git worktree for a task."""
    try:
        wt = WorktreeManager(repo).create(task_id)
        typer.echo(f"Created {wt.path} on {wt.branch}")
    except WorktreeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("worktree-list")
def worktree_list(repo: Path = typer.Option(Path("."), help="Git repository")) -> None:
    """List registered Git worktrees."""
    try:
        typer.echo(WorktreeManager(repo).list())
    except WorktreeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("worktree-remove")
def worktree_remove(task_id: str, repo: Path = typer.Option(Path("."), help="Git repository")) -> None:
    """Remove a clean worktree, preserving its branch."""
    try:
        WorktreeManager(repo).remove(task_id)
        typer.echo(f"Removed worktree for {task_id}; branch preserved")
    except WorktreeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("providers")
def providers() -> None:
    """Report which supported coding CLIs are installed."""
    for name in provider_names():
        typer.echo(f"{name}: {'installed' if CLIProvider(name).available() else 'not found'}")


@app.command("agent-run")
def agent_run(
    provider: str = typer.Option(..., help="claude, codex, or gemini"),
    prompt: str = typer.Option(..., help="Task instruction"),
    workspace: Path = typer.Option(..., help="Existing workspace directory"),
    timeout: int = typer.Option(600, min=1, max=3600),
    execute: bool = typer.Option(False, help="Explicitly allow running the installed agent CLI"),
) -> None:
    """Run an installed coding CLI in a specified workspace."""
    if not execute:
        typer.echo("Dry run only. Add --execute to launch the agent CLI.")
        return
    try:
        result = CLIProvider(provider).execute(prompt, workspace, timeout)
    except ProviderError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.output:
        typer.echo(result.output)
    if result.error:
        typer.echo(result.error, err=True)
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


@app.command("schedule-quality")
def schedule_quality(
    story_id: str,
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    execute: bool = typer.Option(False, help="Persist the contract outcome and dispatch transition"),
) -> None:
    """Evaluate recorded evidence for one story. Does not re-run gate commands."""
    try:
        policy = load_policy(STATE_DIR / "quality.yaml")
        if not policy.present:
            typer.echo("No .codehelm/quality.yaml; quality contract inactive. Dispatch state unchanged.")
            return
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        if not execute:
            snapshot = scheduler.snapshot()
            if story_id not in snapshot:
                raise ValueError("Unknown story")
            _state, attempts = snapshot[story_id]
            task_id = f"{prefix}-{story_id}-A{attempts}"
            task = scheduler.store.get(task_id)
            story = next((item for item in scheduler.plan.stories if item.id == story_id), None)
            if story is None:
                raise ValueError("Unknown story")
            decision = assess(
                scheduler.store, task_id, attempt=attempts, policy=policy,
                workspace=Path(task.workspace) if task and task.workspace else None,
                acceptance_criteria=story.acceptance_criteria, story_id=story.id,
                implementer=(task.provider or "") if task else "", persist=False)
            typer.echo(json.dumps(decision.to_dict()))
            typer.echo("Dry run. Add --execute to record the outcome and transition the story.")
            return
        status = scheduler.apply_quality(story_id)
    except (ValueError, PlanningError, OSError, KeyError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{story_id}: {status}")
    if status != "ready_for_pr":
        raise typer.Exit(code=1)


@app.command("quality-eval")
def quality_eval(
    task_id: str,
    execute: bool = typer.Option(False, help="Persist the contract outcome on the task"),
) -> None:
    """Evaluate recorded evidence for one task. Ignored when no quality policy exists."""
    try:
        policy = load_policy(STATE_DIR / "quality.yaml")
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not policy.present:
        typer.echo("No .codehelm/quality.yaml; quality contract inactive. Legacy lifecycle unchanged.")
        return
    store = TaskStore(STATE_DIR / "state.db")
    store.initialize()
    task = store.get(task_id)
    if task is None:
        typer.echo(f"Unknown task: {task_id}")
        raise typer.Exit(code=1)
    attempt = task_attempt(task_id)
    decision = assess(
        store, task_id, attempt=attempt, policy=policy,
        workspace=Path(task.workspace) if task.workspace else None,
        acceptance_criteria=None, story_id=task_id, implementer=task.provider or "",
        persist=execute)
    typer.echo(json.dumps(decision.to_dict()))
    if execute:
        store.transition(task_id, decision.status, error=decision_error(decision) or None)
        typer.echo(f"{task_id}: {decision.status}")
    else:
        typer.echo("Dry run. Add --execute to record the outcome on the task.")
    if decision.status != "ready_for_pr":
        raise typer.Exit(code=1)


@app.command("quality-approve")
def quality_approve(
    task_id: str,
    approver: str = typer.Option(..., help="Must exactly match the authenticated GitHub login"),
    execute: bool = typer.Option(False, help="Record approval as the authenticated GitHub user"),
) -> None:
    """Record high-risk approval as the GitHub login and a digest of the workspace diff."""
    if not execute:
        typer.echo("Dry run. Add --execute to record approval as the authenticated GitHub login.")
        return
    try:
        login = github_user_login()
        if approver.strip() != login:
            raise PullRequestError(
                f"Approver {approver.strip()!r} does not match authenticated GitHub login {login!r}")
        store = TaskStore(STATE_DIR / "state.db")
        store.initialize()
        task = store.get(task_id)
        workspace = Path(task.workspace) if task and task.workspace else None
        digest = diff_digest(workspace) if workspace is not None and workspace.is_dir() else ""
        if workspace is None or not workspace.is_dir() or not digest:
            raise ValueError("Human approval requires a workspace diff digest")
        store.record_evidence(task_id, "human_approval", "human", passed=True,
                              attempt=task_attempt(task_id), identity=login, detail=digest)
    except (ValueError, OSError, PullRequestError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Recorded approval by {login} for {task_id}. Re-evaluate before advancing.")


@app.command("quality-justify")
def quality_justify(
    task_id: str,
    reason: str = typer.Option(..., help="Why the oversized change is accepted"),
    approver: str = typer.Option(..., help="Must exactly match the authenticated GitHub login"),
    execute: bool = typer.Option(False, help="Record the justification as the authenticated GitHub user"),
) -> None:
    """Record an oversized-change justification as the GitHub login and the reason."""
    if not execute:
        typer.echo(
            "Dry run. Add --execute to record the oversized-change justification "
            "as the authenticated GitHub login.")
        return
    try:
        stripped = (reason or "").strip()
        if not stripped:
            raise ValueError("Oversized justification requires a reason")
        login = github_user_login()
        if approver.strip() != login:
            raise PullRequestError(
                f"Approver {approver.strip()!r} does not match authenticated GitHub login {login!r}")
        store = TaskStore(STATE_DIR / "state.db")
        store.initialize()
        store.record_evidence(
            task_id, "oversized_justification", "human", passed=True,
            attempt=task_attempt(task_id), detail=stripped, identity=login)
    except (ValueError, OSError, PullRequestError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Recorded oversized-change justification by {login} for {task_id}. "
        "Re-evaluate before advancing.")


@app.command("docs-exception")
def docs_exception(
    task_id: str,
    reason: str = typer.Option(..., help="Why documentation needs no change"),
    approver: str = typer.Option(..., help="Must exactly match the authenticated GitHub login"),
    execute: bool = typer.Option(False, help="Record the exception as the authenticated GitHub user"),
) -> None:
    """Record a human documentation exception for this attempt. Other gates stay in force."""
    if not execute:
        typer.echo("Dry run. Add --execute to record a documentation exception as the authenticated GitHub login.")
        return
    try:
        cleaned = (reason or "").strip()
        if not cleaned:
            raise ValueError("Documentation exception requires a nonempty reason")
        login = github_user_login()
        if approver.strip() != login:
            raise PullRequestError(
                f"Approver {approver.strip()!r} does not match authenticated GitHub login {login!r}")
        store = TaskStore(STATE_DIR / "state.db")
        store.initialize()
        store.record_evidence(
            task_id, "docs", "human", passed=True, attempt=task_attempt(task_id),
            detail=cleaned, identity=login)
    except (ValueError, OSError, PullRequestError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Recorded documentation exception by {login} for {task_id}. Re-evaluate before advancing.")


@app.command("quality-debt")
def quality_debt(
    task_id: str,
    finding: str = typer.Option(..., help="Unresolved finding text this debt accepts"),
    reason: str = typer.Option(..., help="Why this one finding is accepted as debt"),
    execute: bool = typer.Option(False, help="Record the quality debt for the current attempt"),
) -> None:
    """Accept one unresolved finding for the current attempt. Other gates stay in force."""
    if not execute:
        typer.echo("Dry run. Add --execute to record the quality debt.")
        return
    try:
        store = TaskStore(STATE_DIR / "state.db")
        store.initialize()
        store.record_evidence(task_id, "quality_debt", "human", passed=True,
                              attempt=task_attempt(task_id), detail=finding, identity=reason)
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Recorded quality debt for {task_id}. Re-evaluate before advancing.")


@app.command("quality-attest")
def quality_attest(
    task_id: str,
    criterion: str = typer.Option(..., help="Planned acceptance criterion being attested"),
    execute: bool = typer.Option(False, help="Record an operator acceptance attestation"),
) -> None:
    """Record that an operator attests one acceptance criterion. Tests do not imply this."""
    if not execute:
        typer.echo("Dry run. Add --execute to record the attestation.")
        return
    try:
        store = TaskStore(STATE_DIR / "state.db")
        store.initialize()
        store.record_evidence(task_id, "acceptance", "human", passed=True,
                              attempt=task_attempt(task_id), detail=criterion)
    except (ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Recorded acceptance attestation for {task_id}. Re-evaluate before advancing.")


if __name__ == "__main__":
    app()
