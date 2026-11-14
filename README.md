# CodeHelm

**CodeHelm** is an early-stage, open-source Python CLI for orchestrating AI software engineering agents.

## Vision

A hybrid orchestrator combines deterministic workflow control with AI planning and coding. The planned platform coordinates provider-agnostic agents across Jira planning, implementation, testing, security, independent code review, documentation, and pull requests.

> **Status:** Early scaffold. Git worktree creation, listing and safe removal are implemented. Full orchestration, Jira, direct API adapters and quality gates are **not implemented yet**. A minimal CLI provider adapter is available.

## Quick start

Requires Python 3.13+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
codehelm --help
codehelm init
codehelm status
pytest
```

On Windows, activate with `.venv\Scripts\activate`.

Worktree commands (run from a Git repository containing at least one commit):

```bash
codehelm worktree-create DEV-123
codehelm worktree-list
codehelm worktree-remove DEV-123
```

Worktrees are created in a sibling `<repo>-worktrees` directory. Removal refuses dirty worktrees and preserves branches. This provides file isolation, **not** a security sandbox.

`codehelm init` creates `.codehelm/config.yaml` and a local SQLite state database. The CLI does not yet invoke an AI provider or modify Jira.

## Proposed commands

- `codehelm init` — initialise local configuration and state (implemented)
- `codehelm status` — show scaffold status (implemented)
- `codehelm worktree-create DEV-123` — create an isolated task worktree (implemented)
- `codehelm worktree-list` — list Git worktrees (implemented)
- `codehelm worktree-remove DEV-123` — remove a clean worktree (implemented)
- `codehelm run --jira DEV-123` — future: run a Jira story through the agent lifecycle
- `codehelm agents` — future: inspect active agents
- `codehelm resume <task-id>` — future: resume a checkpointed task

## Design principles

1. Hybrid AI planning and deterministic scheduling.
2. CLI and direct API providers behind one adapter contract.
3. Git worktree isolation for coding tasks; stronger sandboxing for untrusted execution.
4. Minimal, just-in-time context with durable external state.
5. Independent code review required before story completion.
6. Automated test, security, documentation, and acceptance gates.
7. Configurable budgets, bounded retries, observability, and human approval.
8. Jira integration optional: the core engine must also work without Jira.

## MVP milestones

1. Python CLI, configuration and persistent task state
2. CLI/API provider adapters
3. Git worktree manager
4. Agent lifecycle and event-driven supervision
5. Token-efficient context manager and budgets
6. Jira epic/story/bug integration
7. Mandatory review and SDLC quality gates
8. Single Jira story → validated pull request demonstration
9. Incremental API and user documentation automation

## Development

See [Architecture](docs/architecture.md), [Contributing](CONTRIBUTING.md), and [Agent Instructions](AGENTS.md).

## License

License selection pending. **No open-source license has been granted yet**; choose and add a license before public release.

## Provider CLI adapters (v0.3)

Check which supported tools are installed:

```bash
codehelm providers
```

Run an installed coding CLI in an existing worktree:

```bash
codehelm agent-run --provider claude --workspace ../codehelm-worktrees/DEV-123 --prompt "Fix the failing tests" --execute
```

Supported initial CLI names: `claude`, `codex`, `gemini`. CodeHelm uses existing CLI authentication and permission policies. Without `--execute`, it only prints a dry-run message. CLI command syntax can vary by installed version; test compatibility before relying on a provider. Agent processes can modify files or incur usage charges. Run only in trusted or sandboxed workspaces.

**Not yet included:** direct API implementations, task scheduling, token accounting, structured agent output, automatic Jira operations, and security sandboxing.

## Single-task lifecycle (v0.4)

The CLI now connects a provider adapter to a persisted, single-task execution lifecycle.

```bash
codehelm init
codehelm run --jira DEV-123 --prompt "Fix the failing tests" --provider claude --execute
codehelm task-status DEV-123
```

`--jira` currently accepts a Jira-style **task identifier**; it does not fetch Jira issues.
Without `--execute`, the command performs no execution. With it, CodeHelm
creates an isolated worktree, launches the installed provider CLI, and persists
`queued → running → awaiting_validation` or `failed` in SQLite. A successful
CLI exit is **not** story completion or independent code review. Task IDs
cannot be rerun automatically; resume/retry policy is future work.

**Not yet implemented:** automatic test/security gates, independent review,
PR creation, Jira REST operations, provider APIs, agent crash recovery,
live event streaming, token budgets and container sandboxing. Provider CLIs
can execute code and incur charges; use trusted workspaces and provider
permission settings.


## Quality gates (v0.5)

After `codehelm run` exits successfully, the task is `awaiting_validation`.
Run explicit test and security executables, followed by an independent CLI reviewer:

```bash
codehelm validate DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
```

**Important:** `--test-cmd` and `--security-cmd` each accept an executable name only
(no arguments). Install/configure those commands in the agent worktree environment
before running validation. For example, `bandit` without configured targets may
fail; this is intentional fail-closed behavior. A later version will support
structured command arguments and per-project policies.

The reviewer must be a different provider from the coding agent, and must
return a final line exactly `APPROVED`. Review workspace changes invalidate
approval. Test/security failure blocks the task; review rejection yields
`changes_requested`. Successful gates yield `awaiting_documentation`, **not**
`completed`. Documentation, PR and Jira gates are still pending.

**Security limitation:** This prototype invokes user-selected executables and
AI CLIs directly, without a container sandbox. Run only on trusted code with
appropriate permissions. The reviewer is instructed not to edit files, but the
Git snapshot guard is not a security boundary. AI approval is not human merge
authorization.

## Documentation Agent (v0.6)

After a task passes test, security and independent code review gates:

```bash
codehelm document DEV-123 --provider claude --execute
codehelm task-status DEV-123
```

The documentation agent examines implementation and tests, updates relevant
README/docs/API/user/developer documentation, and must produce an actual
documentation-file change. It is not permitted to change non-documentation
files. Failure blocks the task; success advances it to
`awaiting_post_docs_validation`. It **does not** mark the story complete.
A second validation/review pass and PR/Jira integration remain future work.

Limitations: the checks use working-tree file hashes and do not catch every
possible side effect (such as modifications to ignored files or Git history).
The coding CLI runs with its normal local permissions; use a sandbox for
untrusted code. Documentation that needs no change is not yet supported as a
formal, independently approved exception.

## Post-documentation validation (v0.7)

After `codehelm document` succeeds, run the required test, security and
independent-review gates again:

```bash
codehelm validate-after-docs DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
```

Successful gates transition to `awaiting_pr`, **not** `completed`. A failure
blocks the task or requests changes. CLI commands currently accept executable
names without arguments. GitHub PR creation and Jira integration are not yet
implemented. Review is a model verdict, not human merge authorization.

## GitHub draft pull requests (v0.8)

Requires GitHub CLI `gh` installed and authenticated, a GitHub `origin`
remote, and a clean task worktree with committed changes.

```bash
codehelm pr-create DEV-123 --title "Fix DEV-123" --body "Validated changes" --execute
```

The command is dry-run by default. With `--execute`, CodeHelm verifies that
the task is `awaiting_pr`, checks the worktree and branch, pushes the branch
to `origin`, and creates a **draft** GitHub PR via `gh pr create`.
It records `pr_created` and the returned URL. It does not merge, approve,
or transition Jira issues. Push/PR failures retain `awaiting_pr` for
manual inspection; retries can require checking for an existing remote PR.

**Caution:** This prototype checks for a clean tree, not that every desired
change has been committed or that remote branch protections are configured.
Do not use on sensitive repositories without reviewing permissions.

## Jira Cloud integration (v0.9)

Set `CODEHELM_JIRA_URL`, `CODEHELM_JIRA_EMAIL`, and
`CODEHELM_JIRA_API_TOKEN` in your shell environment. Never commit tokens.

```bash
codehelm jira-get DEV-123
codehelm jira-create --project DEV --kind Epic --summary "Authentication" --execute
codehelm jira-create --project DEV --kind Story --summary "Login" --parent DEV-123 --execute
codehelm jira-create --project DEV --kind Bug --summary "Fix crash" --execute
codehelm jira-link-pr DEV-123 --execute
```

`jira-link-pr` posts the recorded GitHub draft PR URL as a comment on
a Jira issue whose key matches the task ID. It does not change Jira status.

**Limitations:** This is a minimal Jira Cloud REST adapter, not autonomous
epic decomposition, bulk story scheduling, transition mapping, or a bidirectional
sync engine. Jira project configurations can differ in supported issue types
and parent relationships. Live Jira credentials and network operations have
not been exercised by the automated tests. Jira remains optional.

## Autonomous planning preview (v1.0)

Generate a structured, validated local plan from a high-level goal:

```bash
codehelm plan --goal "Add secure user login" --provider claude --execute
cat .codehelm/plan.json
```

The planner produces an Epic and up to 30 Stories, each with acceptance
criteria and local story dependencies. Validation rejects invalid JSON,
duplicate IDs, unknown dependencies and cycles. **Review/edit the JSON**
before publication.

```bash
codehelm plan-publish --project DEV --plan-file .codehelm/plan.json --execute
```

Publishing creates the Jira Epic and its Stories in dependency order,
including resolved prerequisite issue keys in story descriptions. It does
not launch agents, create Jira dependency links, transition issues, or
schedule stories. **Publishing is not idempotent**: re-running can create
duplicate Jira issues. A partial Jira failure may leave some issues
created; inspect Jira before retrying. Live Jira operations have not
been exercised by automated tests.

Next: approval checkpoints, durable publication mappings, DAG scheduling,
role assignment and bounded multi-agent execution.

## Dependency-aware multi-agent dispatch (v1.1)

```bash
codehelm schedule --plan-file .codehelm/plan.json --repo . --max-parallel 2
codehelm schedule --plan-file .codehelm/plan.json --repo . --provider claude,codex --max-parallel 2 --execute
```

Dry-run lists dependency-ready Stories. `--execute` launches **one bounded
wave** of up to eight agents, each in its own Git worktree, using the
existing persisted task lifecycle. The command never marks Stories complete
from a successful agent exit: results remain `awaiting_validation`.

Prerequisite Stories must be explicitly attested as completed in a subsequent
invocation using `--completed S1,S2`; this flag is a **manual assertion**,
not a verified PR/Jira completion signal. Do not assert completion until
review, documentation, post-doc validation and required human merge approval
are satisfied. Existing dispatched task IDs are not rerun automatically.
No automatic cross-branch integration, dependency merge, distributed locking,
provider quotas, budget accounting or durable scheduler state yet.

## Durable scheduling and bounded execution (v1.2)

```bash
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN --max-parallel 2 --max-attempts 2 --max-total-attempts 20 --execute
codehelm schedule-recover --plan-file .codehelm/plan.json --prefix PLAN
codehelm schedule-retry S1 --plan-file .codehelm/plan.json --prefix PLAN --execute
codehelm schedule-complete S1 --plan-file .codehelm/plan.json --prefix PLAN --confirmed-merged --execute
```

SQLite records story dispatch states and attempts. Atomic claims prevent
multiple scheduler processes from claiming the same story. Limits cap
concurrent claims, per-story attempts, and total attempts. Each retry uses a
fresh task ID/worktree (`PLAN-S1-A2`) rather than rerunning a prior task.
Expired leases become `interrupted`, requiring process/worktree inspection
and **explicit** retry authorization. A dependent story only unlocks after
a human explicitly confirms its predecessor's PR was merged and marks it
complete; CodeHelm does **not** independently verify GitHub merge status.

This is durable *batch dispatch*, not a long-running daemon or full
crash-resumable agent session. There is no automatic heartbeating,
cross-host process liveness check, auto-retry, token metering, cost accounting,
or sandbox. The budgets constrain *attempts and parallelism*, not tokens
or monetary spend. Each `schedule-durable --execute` dispatches one wave.

## Budget ledger and safe scheduler checkpoints (v1.3)

The optional budget ledger reserves estimated tokens and USD against explicit
limits and accepts verified usage reconciliation. CLI providers do not reliably
report token usage; **this is not a hard provider-side spending cap**. The
existing `schedule-durable` command does not yet consult this ledger before
launching agents. To use it safely, reserve each planned attempt before
manual execution and reconcile only with verified provider data.

```bash
codehelm budget-reserve PLAN-S1-A1 --prefix PLAN --provider claude --tokens 20000 --usd 1.50 --limit-tokens 100000 --limit-usd 10 --execute
codehelm budget-status --prefix PLAN
codehelm budget-reconcile PLAN-S1-A1 --prefix PLAN --tokens 14500 --usd 0.92 --execute
codehelm schedule-checkpoint --plan-file .codehelm/plan.json --prefix PLAN
```

The checkpoint stores a scheduler-state snapshot in SQLite; it does not
checkpoint or restart an in-flight model session. Expired claims still require
manual inspection before retry. Next: atomic budget reservation within
claims, provider usage adapters, heartbeats and checkpoint-driven recovery.

## Budget-gated durable dispatch and heartbeats (v1.4)

`codehelm schedule-durable` now requires an estimated per-task token/USD
reservation and total plan limits. Each dependency-ready story is claimed
**in the same SQLite transaction** that reserves its estimated usage.
Budget exhaustion leaves remaining stories pending. A running agent refreshes
its claim timestamp every 30 seconds (configurable); a stopped scheduler
cannot heartbeat, and expired claims are marked interrupted for inspection.

```bash
codehelm schedule-durable --plan-file .codehelm/plan.json \
  --prefix PLAN --provider claude --max-parallel 2 \
  --reserve-tokens 20000 --reserve-usd 1.50 \
  --limit-tokens 100000 --limit-usd 10.00 --execute
```

The legacy `schedule` command is still an experimental **unbudgeted**
dispatch path; do not use it for cost-sensitive work. Reservations are
conservative estimates, not live metering or hard CLI/provider spending
limits. Reconcile externally verified usage using `budget-reconcile`.
The heartbeat is a process liveness signal, not proof that a model is
progressing. There is no automatic restart of interrupted work and no
provider-side token cap. Never treat an expired lease as permission to
relaunch a potentially still-running subprocess.

## Structured usage import (v1.5)

Provider usage can be imported from a **verified JSON usage report**:

```bash
codehelm usage-import PLAN-S1-A1 --provider claude \
  --usage-file provider-usage.json --prefix PLAN
codehelm usage-import PLAN-S1-A1 --provider claude \
  --usage-file provider-usage.json --prefix PLAN --execute
```

Recognizes explicit `usage.input_tokens`, `usage.output_tokens`, and
optional `total_cost_usd` fields in a structured JSON object. Missing
or ambiguous fields fail closed; if USD is unavailable, no reconciliation
occurs and the original reservation is retained. Only use authentic,
trusted provider metadata, not model-generated JSON or agent output.
CLI output formats differ across versions and may not contain these fields.
**This is not automatic usage capture** and does not enforce live hard caps.
No changes are made to external GitHub/Jira resources.

## AUD additional-spend budget (v1.6)

The durable scheduler now applies an additional-charge allowance in **AUD**:
A$10 first warning, A$15 critical warning, A$20 monthly pause, A$5
estimated additional spend per Epic, and two concurrent agents by default.
These values are applied to estimated additional charges, **not** existing
subscription fees or model-reported USD estimates.

```bash
codehelm monthly-budget
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN \
  --provider claude --max-parallel 2 --reserve-tokens 20000 \
  --reserve-usd 1.50 --limit-tokens 100000 --limit-usd 10 \
  --additional-aud-per-task 2.50 --execute
```

The AUD estimate is explicitly supplied per task; **no FX conversion is
assumed**. Use `--additional-aud-per-task 0` for runs expected to incur no
additional charge under an existing subscription. Budget admission and
scheduler claim happen in one SQLite transaction. Reconciliation of actual
additional AUD charges is supported by the ledger API, but automatic provider
billing reconciliation and an interactive approval/override workflow are
not yet implemented. This is an admission pause, not a hard provider cap.
The legacy `schedule` command does not use these controls.
