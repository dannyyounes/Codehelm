# CodeHelm

[![Python tests](https://github.com/dannyyounes/Codehelm/actions/workflows/tests.yml/badge.svg)](https://github.com/dannyyounes/Codehelm/actions/workflows/tests.yml)

**CodeHelm** is an open-source Python CLI (MIT) for orchestrating AI software engineering agents. The installed package version is still `0.1.0`. The notes below describe behavior that is in this tree, including the optional v1.7 quality contract.

## Current behavior

Implemented in this repository:

- Local configuration, SQLite task state, and Git worktree isolation
- Claude Code, Codex, and Gemini CLI adapters
- Single-task lifecycle, dependency-aware dispatch, and a durable SQLite scheduler with bounded attempts, leases, and explicit retry
- Estimated token/USD reservations and an additional-AUD admission budget
- Test, security, and independent-review gates, a documentation agent, a second validation pass, draft GitHub pull requests, and optional Jira Cloud REST
- An optional quality contract, active only when `.codehelm/quality.yaml` exists

Not implemented: direct provider APIs, a container image, automatic merge, and automatic Jira transitions. Structured CLI stdout is recorded when `parse_cli_usage` recognizes it; otherwise the reservation stays an estimate. Unknown USD is not stored as zero. An explicit `jira-transition` command posts one transition id; a dry run does not call Jira. The scheduler still does not transition issues or merge pull requests. Each `quality-debt` row accepts one unresolved finding for the current attempt. It does not waive tests, security, review, architecture, docs, acceptance, or human approval. A debt from another attempt does not count, and a debt whose text does not match a finding is ignored. Test and security commands run in a Linux namespace sandbox. High-risk approval stores the GitHub login and a sha256 digest of the workspace diff. A later change makes that approval not count. It is not a cryptographic signature of the person. A successful agent exit does not complete a story. Budgets remain admission estimates, not guaranteed spending protection and not a provider-side cap.

Sections headed `v0.x` / `v1.x` are milestone notes. If an earlier note says a capability is absent, this summary and any later milestone supersede that sentence.

## Vision

A hybrid orchestrator combines deterministic workflow control with AI planning and coding. It coordinates provider-agnostic agents across planning, implementation, testing, security, independent code review, documentation, and pull requests. Jira is optional.

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

`codehelm init` creates `.codehelm/config.yaml` and a local SQLite state database. It does not create a quality policy and it does not call a provider or Jira. Later commands do, and only when you pass `--execute`.

## Commands

`codehelm --help` lists the commands that exist today: `init`, `status`, `run`, `validate`, `document`, `validate-after-docs`, `pr-create`, worktrees, providers, planning, Jira, durable scheduling, budgets, and the v1.7 quality commands.

`codehelm agents` lists task rows in `.codehelm/state.db`: one line each with id, status, provider, and updated_at. Queued and running tasks come first, then other tasks by updated_at descending. The default limit is 20 and `--limit` accepts 1 to 100. It does not launch an agent. `codehelm resume <task-id>` continues an interrupted durable story on the same attempt. The task id must look like `PREFIX-STORY-An` (for example `PLAN-S1-A1`). Without `--execute` it names the task and does not change dispatch state. With `--execute` it re-claims that interrupted story and carries the same attempt forward. It refuses to relaunch a queued, running, or failed coding agent. `schedule-retry` is still how a new attempt starts. Expired claims are marked with `schedule-recover` and retried explicitly with `schedule-retry`. That retry starts a new attempt; it does not resume an in-memory model session.

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

MIT. See [LICENSE](LICENSE).

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

**Not in this milestone:** direct API implementations, task scheduling, token accounting, structured agent output, automatic Jira operations, and security sandboxing. Scheduling, Jira REST, and estimated budgets were added later. Direct provider APIs and a container sandbox are still absent.

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
CLI exit is **not** story completion or independent code review. This command
still refuses to rerun a task id. Explicit retry of a durable story arrived
in v1.2 and uses a new task id.

**Not in this milestone:** automatic test/security gates, independent review,
PR creation, Jira REST operations, provider APIs, agent crash recovery,
live event streaming, token budgets and container sandboxing. Gates, draft
PRs, Jira REST, durable retry, and estimated budgets were added in later
milestones. Direct provider APIs and a container sandbox are still absent.
Provider CLIs can execute code and incur charges; use trusted workspaces and
provider permission settings.


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
`completed`. Documentation, PR and Jira gates were still pending at this
milestone; see v0.6–v0.9. The v1.7 quality contract, when its policy file
exists, can later replace `awaiting_pr` with `blocked`,
`awaiting_human_approval`, or `ready_for_pr`.

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
A second validation pass, draft PRs, and Jira comments were added in v0.7–v0.9.

Limitations: the checks use working-tree file hashes and do not catch every
possible side effect (such as modifications to ignored files or Git history).
The coding CLI runs with its normal local permissions; use a sandbox for
untrusted code. Documentation that needs no change is a human exception:
`codehelm docs-exception TASK --reason TEXT --approver LOGIN`. With
`--execute`, CodeHelm records it only when `--approver` strips to the
GitHub login from `github_user_login()` and the reason is nonempty. A dry
run does not call GitHub and writes nothing. The exception does not waive
tests, security, review, acceptance, or human approval.

## Post-documentation validation (v0.7)

After `codehelm document` succeeds, run the required test, security and
independent-review gates again:

```bash
codehelm validate-after-docs DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
```

Successful gates transition to `awaiting_pr`, **not** `completed`, when no
quality policy file is present. A failure blocks the task or requests changes.
CLI commands currently accept executable names without arguments. GitHub PR
creation and Jira integration were not in this milestone; see v0.8 and v0.9.
Review is a model verdict, not human merge authorization. When
`.codehelm/quality.yaml` is present, a passing post-documentation run is
scored by the quality contract instead of moving straight to `awaiting_pr`.

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
or transition Jira issues. An explicit `jira-transition` command posts one
transition id; a dry run does not call Jira. The scheduler still does not
transition issues or merge pull requests. Push/PR failures retain
`awaiting_pr` for manual inspection; retries can require checking for an
existing remote PR.

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
codehelm jira-transition DEV-123 --id 31
codehelm jira-transition DEV-123 --id 31 --execute
```

`jira-link-pr` posts the recorded GitHub draft PR URL as a comment on
a Jira issue whose key matches the task ID. It does not change Jira status.

`jira-transition KEY --id TRANSITION_ID` prints a dry-run line that names
the key and the transition id and does not call Jira. With `--execute` it
posts that one transition id. The scheduler still does not transition issues
or merge pull requests.

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
schedule stories. An explicit `jira-transition` command posts one transition
id; a dry run does not call Jira. The scheduler still does not transition
issues or merge pull requests. **Publishing is not idempotent**: re-running can create
duplicate Jira issues. A partial Jira failure may leave some issues
created; inspect Jira before retrying. Live Jira operations have not
been exercised by automated tests.

Later milestones add durable scheduling and bounded execution. Publication
mappings and role assignment are still absent. High-risk approval is the
authenticated GitHub login recorded by `quality-approve`.

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
invocation using `--completed S1,S2` when no quality policy file exists. That
flag is a **manual assertion**, not a verified PR/Jira completion signal. Do
not assert completion until review, documentation, post-doc validation and
required human merge approval are satisfied. When `.codehelm/quality.yaml` is
present, `--completed` is refused. Use `schedule-complete` after the story is
`ready_for_pr`, a draft PR is recorded, and a person has confirmed the merge.
Existing dispatched task IDs are not rerun automatically. This command still
has no automatic cross-branch integration, dependency merge, distributed
locking, or provider quotas. Durable scheduler state and estimated budgets
were added in v1.2–v1.4; this legacy `schedule` path does not use them.

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
`schedule-complete --confirmed-merged --execute` asks `gh pr view` and
completes the story only when that pull request is `MERGED`. An open or
closed-unmerged pull request leaves the story unchanged.

This milestone is durable *batch dispatch*, not a long-running daemon or a
crash-resumable model session. It did not heartbeat claims or reserve
token/USD budgets; v1.3 and v1.4 add those admission controls. There is still
no cross-host liveness check, no automatic retry of a failed story, and no
sandbox. Attempt limits are not monetary spend protection. Each
`schedule-durable --execute` dispatches one wave.

When `.codehelm/quality.yaml` exists, that same claim continues through the
test, security, review, documentation, and post-documentation gates before
the dispatch row is released. The story then becomes `blocked`,
`awaiting_human_approval`, or `ready_for_pr` from the recorded evidence.
A passing test exit code does not attest acceptance criteria. The test
process can verify a criterion by printing a `CODEHELM_ACCEPTANCE` line, or
by writing that line to `.codehelm/acceptance-report.txt` during the run. A
high-risk path does not approve itself. With no policy file, a successful
agent still stops at `awaiting_validation`. An interrupted coding agent is
not relaunched.
`schedule-durable` can resume an interrupted story on the same attempt only
after the agent has already finished, and it skips gates that already passed.
`schedule-retry` still starts a new attempt for a failed, blocked, or
interrupted story. `ready_for_pr` does not unlock dependent stories.

## Budget ledger and safe scheduler checkpoints (v1.3)

The optional budget ledger reserves estimated tokens and USD against explicit
limits and accepts verified usage reconciliation. CLI providers do not reliably
report token usage; **this is not a hard provider-side spending cap**. From
v1.4, `schedule-durable --execute` reserves this ledger in the same SQLite
transaction as the claim. `budget-reserve` remains the manual path. Reconcile
only with verified provider data.

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
admission estimates, not guaranteed spending protection and not a
provider-side cap. Structured CLI stdout is recorded when `parse_cli_usage`
recognizes it; otherwise the reservation stays an estimate. Unknown USD is
not stored as zero. Reconcile externally verified usage using `budget-reconcile`.
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
or ambiguous fields fail closed. When USD is unavailable, the command
prints that USD is unavailable and the reservation is retained.
`--execute` records the token count and does not store USD as zero.
Without `--execute`, nothing is written. A recognized token and USD
payload reconciles only with `--execute`. Only use authentic, trusted
provider metadata, not model-generated JSON or agent output.
CLI output formats differ across versions and may not contain these fields.
Structured CLI stdout is recorded when `parse_cli_usage` recognizes it;
otherwise the reservation stays an estimate. This does not enforce a
provider-side cap.
No changes are made to external GitHub/Jira resources.

## AUD additional-spend budget (v1.6)

The durable scheduler now applies an additional-charge allowance in **AUD**:
A$10 first warning, A$15 critical warning, A$20 monthly pause, A$5
estimated additional spend per Epic, and two concurrent agents by default.
These values are applied to estimated additional charges, **not** existing
subscription fees or model-reported USD estimates.

```bash
codehelm monthly-budget
codehelm budget-approve TASK --epic EPIC --amount 2.50 --approver LOGIN --reason TEXT
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN \
  --provider claude --max-parallel 2 --reserve-tokens 20000 \
  --reserve-usd 1.50 --limit-tokens 100000 --limit-usd 10 \
  --additional-aud-per-task 2.50 --execute
```

The AUD estimate is explicitly supplied per task; **no FX conversion is
assumed**. Use `--additional-aud-per-task 0` for runs expected to incur no
additional charge under an existing subscription. Budget admission and
scheduler claim happen in one SQLite transaction. Reconciliation of actual
additional AUD charges is supported by the ledger API. `budget-approve`
records a GitHub login and a reason for one exact task, epic, and amount. A
dry run does not call GitHub. `schedule-durable` consumes that row only when
the task id, epic, month, and amount match; otherwise it still pauses. It
does not raise the standing cap, and it is not spend protection or billing
reconciliation. Automatic provider billing
reconciliation is not implemented. This is an admission pause, not a hard
provider cap. The legacy `schedule` command does not use these controls.

## Quality contract (v1.7)

The contract is optional. `codehelm init` does not write it. With no
`.codehelm/quality.yaml`, task transitions stay as they were through v1.6:
post-documentation success still ends at `awaiting_pr`.

```yaml
schema_version: 1
max_changed_lines: 400
require_acceptance_criteria: true
require_tests: true
require_security: true
require_architecture: true
require_review: true
require_docs: true
max_retries: 2
human_approval_risks:
  - authentication_changes
  - database_migrations
  - security_sensitive_changes
  - production_infrastructure
```

Omitted keys use those defaults. `architecture_cmd` and `risk_patterns` are
optional and empty when omitted. `schema_version` must be `1`. Unknown keys
or an unknown schema version fail closed. `max_retries` is not a second retry
loop; the enforced attempt cap remains the scheduler's `DispatchBudget`. An
omitted `--max-attempts` uses `policy.max_retries` when the policy file exists.

Evidence is taken from rows the orchestrator writes after it runs a check,
and from `git`. An agent-written evidence JSON file is rejected.

| Check | What is stored | What it is |
| --- | --- | --- |
| Tests and security | Exit code from `command_gate` | A real process result |
| Optional architecture command | Exit code from `command_gate` when architecture review is required and `architecture_cmd` is set | Architecture review is still a model verdict. The command must also exit 0 and does not replace it |
| Independent review and architecture review | `independent_review` verdict (`APPROVED` on the last line, different provider, Git snapshot unchanged) | A model verdict recorded by CodeHelm, not human approval |
| Documentation | Documentation-agent file check, or `docs-exception` (`source=human`, passed, GitHub login, nonempty reason) | A real workspace check, or a human exception on this attempt. A row from another attempt does not count. It does not waive tests, security, review, acceptance, or human approval |
| Diff size and risk paths | `git` numstat plus policy globs, and optional `risk_patterns` | Repository state. `risk_patterns` are optional content regexes over changed files; with none configured, only path globs classify risk |
| Acceptance criteria | `quality-attest`, or a `CODEHELM_ACCEPTANCE` line from the test process | A passing exit code alone verifies nothing |
| Human approval | `quality-approve` stores the GitHub login and a sha256 digest of the workspace diff | A later change makes that approval not count. It is not a cryptographic signature of the person. Later scoring does not call GitHub again |
| Quality debt | `quality-debt` records the finding text and a nonempty reason (`source=human`, passed) | Accepts that exact unresolved finding on the current attempt. A debt from another attempt, or whose text does not match, is ignored. It does not waive the other gates |
| Oversized-change justification | `quality-justify` records the GitHub login and the reason | A dry run does not call GitHub. A row with no identity does not count. Later scoring does not call GitHub again |

A test verifies one criterion only by emitting this line on stdout or stderr, or by creating or changing `.codehelm/acceptance-report.txt` in the workspace while it runs:

```text
CODEHELM_ACCEPTANCE	<exact planned criterion>	passed
```

`failed` records the criterion as not verified. A line left in that file before the test process starts is ignored. Pytest's default capture hides `print`, so a wrapper that runs `pytest -s`, or the report file, is what the single-executable `--test-cmd` can use. `quality-attest` still records an operator attestation. A criterion with neither is unverified. A failed report line cancels a passed report line for that criterion. It does not cancel an operator attestation.

Path globs classify `migrations/**`, `auth/**`, `security/**`, `deploy/**`,
`infra/**`, and `.github/workflows/**` (including nested forms) as high risk.
`risk_patterns` are optional content regexes over changed files. With none
configured, only path globs classify risk. A match on a changed file adds that
risk name even when the path matches no glob. Files outside the change set are
not scanned. With no `risk_patterns`, a risky edit outside those globs is not
detected unless an operator records it.

Architecture review is still a model verdict. When that review is required, an
optional `architecture_cmd` runs first and must also exit 0. It does not
replace the review. Turning architecture review off skips the command.

When the policy file exists, `validate-after-docs` and `schedule-quality --execute`
persist one of three outcomes and use it as the task or dispatch state:

- `blocked`
- `awaiting_human_approval`
- `ready_for_pr`

`pr-create` then requires `ready_for_pr` and evaluates the recorded evidence
again before pushing. A task left at `awaiting_pr` before the policy file
existed is not grandfathered; re-run the post-documentation gates or
`quality-eval --execute`. `schedule-complete` also requires that latest outcome, a recorded pull
request, `--confirmed-merged`, and a `MERGED` result from `gh pr view`.
Evidence is stored per attempt. `schedule-quality`
still refuses an interrupted claim. `schedule-durable` can resume that claim
on the same attempt after the coding agent has finished. `schedule-retry`
starts a new attempt, and the previous attempt's rows do not count.

```bash
codehelm validate PLAN-S1-A1 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
codehelm document PLAN-S1-A1 --provider claude --execute
codehelm quality-attest PLAN-S1-A1 --criterion "Works" --execute
codehelm docs-exception PLAN-S1-A1 --reason "The README already describes this behavior" --approver YOUR_GITHUB_LOGIN --execute
codehelm validate-after-docs PLAN-S1-A1 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
codehelm schedule-quality S1 --plan-file .codehelm/plan.json --prefix PLAN --execute
codehelm quality-approve PLAN-S1-A1 --approver YOUR_GITHUB_LOGIN --execute
codehelm quality-debt PLAN-S1-A1 --finding "the finding text" --reason "why this finding can wait" --execute
```

`schedule-durable --execute` runs the gate commands and model reviews once per
attempt while the claim is held. It does not repeat a stage that already
passed for that attempt. `schedule-quality` does not re-run pytest, bandit,
or the reviewer. It only reads evidence those commands already recorded.
Missing evidence blocks the story. Re-scoring after `quality-attest`,
`quality-approve`, or `quality-debt` does not spend another reviewer call.

`quality-approve --execute` asks `gh api user` for the current login and
records that login only when `--approver` matches it exactly. The row stores
the GitHub login and a sha256 digest of the workspace diff. A dry run does
not call GitHub. Later scoring does not call `gh` again. A later change
makes that approval not count. It is not a cryptographic signature of the person.

`docs-exception --execute` records a passing docs row for the current attempt
when `--approver` strips to the GitHub login and `--reason` is nonempty. A
dry run does not call GitHub. The exception does not waive tests, security,
review, acceptance, or human approval. The documentation agent still fails
when it changes no documentation file.

Test and security commands run through `unshare` with a private user, mount,
pid, and network namespace. `/tmp`, `/home`, and `/root` are covered. The
workspace is bind-mounted back, and credential environment variables are
dropped. If `unshare` is missing or the namespace cannot be created, the gate
fails and the command does not run outside the sandbox. Hosts that block
unprivileged user namespaces, including a default GitHub-hosted runner,
must allow them first. The coding agent and the model reviewer are not in
that namespace. This is not a container image.

Each `quality-debt` row accepts one unresolved finding for the current attempt
when a human records that finding text and a nonempty reason. More than one
debt can be recorded. It does not
waive tests, security, review, architecture, docs, acceptance, or human
approval. A debt from another attempt does not count, and a debt whose text
does not match a finding is ignored. Current limits: no signed approval, and
no container image around the gate commands. A passing test exit code still
does not verify an unnamed acceptance criterion. Architecture review is a
second model prompt, not a separate deterministic checker. Budgets remain
estimates, not spend protection.
