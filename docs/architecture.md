# Architecture

CodeHelm is a local CLI orchestrator. Model calls are limited to planning, implementation, documentation, and review adapters. Scheduling, budgets, and quality decisions are deterministic Python over SQLite.

The package version is `0.1.0`. The behavior below is what the code does now.

## Control plane

- **Planning:** a provider-neutral adapter returns a structured Epic and Stories. The plan is validated locally (IDs, dependencies, cycles) before anything is dispatched.
- **Deterministic engine:** task transitions, the dependency-aware scheduler, attempt and concurrency limits, budget admission, and the quality contract.
- **Adapters:** Claude Code, Codex, and Gemini CLIs behind one provider interface. Direct API adapters are not implemented.
- **State:** SQLite in `.codehelm/state.db`. Task rows, dispatch claims, budget reservations, and quality evidence live outside the model context.
- **Execution:** one Git worktree per coding task. Worktrees are file isolation, not a container sandbox.
- **Integrations:** optional Jira Cloud REST and draft GitHub pull requests via `gh`. Neither is required to run the engine. Creating a pull request does not merge it.

## Task lifecycle

Agent execution persists `queued → running → awaiting_validation` or `failed`. A zero exit code is not story completion.

Explicit follow-up commands then run:

`awaiting_validation → tests → security → independent review → awaiting_documentation → documenting → awaiting_post_docs_validation → tests → security → independent review`

With no quality policy file, success ends at `awaiting_pr`. Failures persist as `blocked` or `changes_requested`. `pr-create` from `awaiting_pr` records `pr_created`. Nothing on this path merges or marks a story complete. `schedule-durable` also stops a successful agent at `awaiting_validation` when the policy file is absent.

With `.codehelm/quality.yaml` present, `schedule-durable` runs those gates inside the claimed attempt and then scores the contract. The manual post-documentation command does the same scoring. The task and the dispatch row become `blocked`, `awaiting_human_approval`, or `ready_for_pr`. `pr-create` is allowed only from `ready_for_pr`, and it evaluates the recorded evidence again before publishing. A failed gate does not launch the later model calls for that attempt.

Story completion in the durable scheduler is a separate step. It requires a recorded pull request, `--confirmed-merged`, and `gh pr view` reporting `state=MERGED` with a merged timestamp. An open or closed pull request does not complete the story. When a quality policy is active it also requires the latest evaluation for that attempt to be `ready_for_pr`. Dependents stay locked until that completion. CodeHelm does not merge the pull request.

## Durable scheduling

`DurableScheduler` stores one row per story. Claims use `BEGIN IMMEDIATE`. Limits:

- per-story attempts (default 2, maximum 5)
- total attempts per prefix (default 20)
- concurrent claims (default 2, maximum 8)
- estimated token and USD reservations in the same transaction as a budgeted claim
- optional additional-AUD monthly admission (warn A$10, critical A$15, pause A$20, epic A$5)

These are admission controls on estimates CodeHelm stores. They are not provider-side spending caps. Live token use is unknown unless a verified usage report is imported.

A claim heartbeat refreshes `claimed_at` during the agent and, when a policy exists, between quality stages. If the lease expires, the story becomes `interrupted`. Recovery never relaunches a coding agent that is still queued, running, or failed. If the agent had already finished, the next `schedule-durable --execute` can resume that same attempt and skip gates that already passed. `schedule-retry` remains the way to start a new task id (`PREFIX-S1-A2`). Resume does not take a second budget reservation.

## Quality contract

Policy file: `.codehelm/quality.yaml` with `schema_version: 1`. If the file is absent, `load_policy` reports the contract inactive and existing commands keep their previous transitions. Omitted keys use defaults (tests, security, architecture review, independent review, documentation, acceptance criteria, 400 changed lines, and human approval for authentication, migrations, security-sensitive paths, and production infrastructure). `architecture_cmd` and `risk_patterns` default to empty. Unknown schema versions and unknown keys fail closed.

`TaskStore.initialize` is the migration. It creates `quality_evidence` and `quality_evaluations` with `CREATE TABLE IF NOT EXISTS`, adds any missing columns, and records state schema version 2 in `schema_migrations`. Existing task and dispatch rows are left in place. `codehelm init` does not write a quality policy.

`validate`, `document`, `validate-after-docs`, and the durable quality wave append evidence. `schedule-quality` does not run those commands again. It and the post-documentation gate call `assess`, which reads only the current attempt:

| Check | Source | How to read it |
| --- | --- | --- |
| Tests, security, optional architecture command | `command_gate` exit code | Process result. Architecture review is still a model verdict. When that review is required, `architecture_cmd` must also exit 0 |
| Independent review, architecture review | `independent_review` (exact `APPROVED` line, different provider, snapshot unchanged) | Model verdict recorded by the orchestrator |
| Documentation | documentation agent file-hash check | Process result |
| Diff size and risk paths | `git` status/diff plus policy globs | Repository state. `risk_patterns` are optional content regexes over changed files; with none configured, only path globs classify risk |
| Acceptance criteria | `quality-attest` (`source=human`) or a `CODEHELM_ACCEPTANCE` line captured from the test process (`source=test_report`) | A passing exit code alone verifies nothing. A report file is used only when that process creates or changes it |
| Human approval | `quality-approve` (`source=human`, identity is the `gh api user` login) | Checked at record time. A typed name that is not that login is refused. Later scoring does not call GitHub again |
| Quality debt | `quality-debt` (`source=human`, detail is the finding, identity is the reason, passed) | Accepts one matching unresolved finding on the current attempt. A debt from another attempt does not count. A debt whose text does not match a finding is ignored. It does not waive tests, security, review, architecture, docs, acceptance, or human approval |
| Oversized justification | stored justification (`source=human`) | Operator attestation, not authenticated |

`record_evidence` rejects any other source, including an agent-supplied JSON document. Outcomes are appended to `quality_evaluations` and copied onto the task and, from `schedule-quality --execute`, the dispatch row.

`schedule-quality` still refuses an interrupted story. Resuming it first re-claims the same attempt; `schedule-retry` starts a new attempt, and only that new attempt's rows count.

High-risk classification uses path globs. `risk_patterns` are optional content regexes over changed files; with none configured, only path globs classify risk. A risky change outside those globs and any configured patterns is not detected unless an operator records a `risk` row. `quality-approve --execute` stores the authenticated GitHub login; it does not sign the diff, and evaluation does not repeat the GitHub call. Test and security commands run in a private user, mount, pid, and network namespace. The workspace stays writable; `/tmp`, `/home`, and `/root` are covered, and there is no unsandboxed fallback. A host that blocks unprivileged user namespaces refuses the gate. The coding agent and the model reviewer stay outside that namespace. Each quality debt accepts one unresolved finding for the current attempt. More than one debt can be recorded. `quality-debt` stores that finding as detail and a nonempty reason as identity (`source=human`, passed). Evaluation drops a finding only when its text matches. A debt from another attempt does not count, and a debt whose text does not match a finding is ignored. The debt does not waive tests, security, review, architecture, docs, acceptance, or human approval. There is no container image. Architecture review reuses the independent-review function with a different prompt; it is still a model verdict. When that review is required, an optional `architecture_cmd` runs first and must also exit 0. It does not replace the review, and turning the review off skips the command. `max_retries` on the policy is not a second retry loop. `DispatchBudget` remains the enforced attempt cap.

## Budgets and provider use

Token and USD figures are reservations plus optional reconciliation of a verified provider JSON report. Missing fields fail closed. AUD controls apply only to an explicitly supplied additional-charge estimate; no exchange rate is assumed. Do not describe any of these as guaranteed spend protection.
