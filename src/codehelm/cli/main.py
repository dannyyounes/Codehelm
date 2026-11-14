from pathlib import Path
import sqlite3

import typer
import yaml
from codehelm.execution.worktrees import WorktreeManager, WorktreeError
from codehelm.providers.adapters import CLIProvider, ProviderError, provider_names
from codehelm.orchestrator.lifecycle import LifecycleEngine, TaskStore
from codehelm.integrations.github_pr import PullRequestError
from codehelm.integrations.jira import JiraClient, JiraError
from codehelm.orchestrator.scheduler import ready_stories, dispatch_batch
from codehelm.orchestrator.durable import DurableScheduler, DispatchBudget
from codehelm.orchestrator.budgets import BudgetLedger, Limits
from codehelm.providers.usage import parse_usage
from codehelm.orchestrator.monthly_budget import MonthlyBudget, MonthlyPolicy
from decimal import Decimal
from codehelm.planning.planner import (generate_plan, load_plan, save_plan,
                                      publish_plan_to_jira, PlanningError)

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
    max_attempts: int = typer.Option(2, min=1, max=5),
    max_total_attempts: int = typer.Option(20, min=1, max=100),
    repo: Path = typer.Option(Path(".")),
    reserve_tokens: int = typer.Option(..., min=1, help="Estimated tokens per task"),
    reserve_usd: str = typer.Option(..., help="Estimated USD per task"),
    limit_tokens: int = typer.Option(..., min=1, help="Total plan token budget"),
    limit_usd: str = typer.Option(..., help="Total plan USD budget"),
    heartbeat_seconds: int = typer.Option(30, min=5, max=300),
    additional_aud_per_task: str = typer.Option("0", help="Estimated additional AUD charge per task; subscription-only=0"),
    execute: bool = typer.Option(False),
) -> None:
    """Atomically claim one ready wave, with durable attempt and concurrency limits."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, Thread
    try:
        plan_obj = load_plan(plan_file)
        scheduler = DurableScheduler(STATE_DIR / "state.db", plan_obj, prefix)
        budget = DispatchBudget(max_attempts, max_total_attempts, max_parallel)
        if not execute:
            typer.echo(f"Durable state: {scheduler.snapshot()}")
            typer.echo("Dry run. Add --execute to claim and launch one wave.")
            return
        limits = Limits(limit_tokens, Decimal(limit_usd))
        chosen = scheduler.claim_budgeted(budget, provider, reserve_tokens,
                                          Decimal(reserve_usd), limits,
                                          MonthlyPolicy(), Decimal(additional_aud_per_task))
        engine = LifecycleEngine(repo, STATE_DIR / "state.db")
        stories = {story.id: story for story in plan_obj.stories}
        def launch(sid: str):
            story = stories[sid]
            attempt = scheduler.snapshot()[sid][1]
            task_id = f"{prefix}-{sid}-A{attempt}"
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
                result = engine.execute(task_id, prompt, provider)
                state = "failed" if result.exit_code else "awaiting_validation"
                scheduler.finish(sid, state, result.error)
                return sid, state
            except Exception as exc:
                try:
                    scheduler.finish(sid, "failed", str(exc))
                except ValueError:
                    pass
                return sid, "failed"
            finally:
                stop.set()
                heartbeat_thread.join(timeout=2)
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            results = list(pool.map(launch, chosen))
        for sid, status in results:
            typer.echo(f"{sid}: {status}")
        if not chosen:
            typer.echo("No stories eligible under dependencies and budget.")
    except (ValueError, PlanningError, OSError, ArithmeticError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule-complete")
def schedule_complete(
    story_id: str,
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    confirmed_merged: bool = typer.Option(False, help="Human confirms PR merged and gates satisfied"),
    execute: bool = typer.Option(False),
) -> None:
    """Manually unlock dependencies only after a verified merged PR."""
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


@app.command("schedule-retry")
def schedule_retry(
    story_id: str,
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    prefix: str = typer.Option("PLAN"),
    max_attempts: int = typer.Option(2, min=1, max=5),
    execute: bool = typer.Option(False),
) -> None:
    """Authorize a retry after manually inspecting a failed/interrupted attempt."""
    if not execute:
        typer.echo("Dry run. Add --execute to authorize a retry.")
        return
    try:
        scheduler = DurableScheduler(STATE_DIR / "state.db", load_plan(plan_file), prefix)
        scheduler.retry(story_id, DispatchBudget(max_attempts=max_attempts))
        typer.echo(f"{story_id}: pending retry")
    except (ValueError, PlanningError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


@app.command("schedule")
def schedule(
    plan_file: Path = typer.Option(Path(".codehelm/plan.json")),
    completed: str = typer.Option("", help="Comma-separated explicitly completed local story IDs"),
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


if __name__ == "__main__":
    app()
