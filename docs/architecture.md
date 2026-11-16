# Architecture

CodeHelm is a local CLI orchestrator. Model calls are limited to planning, implementation, documentation, and review adapters. Scheduling, budgets, and quality decisions are deterministic Python over SQLite.

The package version is `0.1.0`. The behavior below is what the code does now.

## Control plane

- **Planning:** a provider-neutral adapter returns a structured Epic and Stories. The plan is validated locally (IDs, dependencies, cycles) before anything is dispatched.
- **Deterministic engine:** task transitions, the dependency-aware scheduler, attempt and concurrency limits, budget admission, and the quality contract.
- **Adapters:** Claude Code, Codex, and Gemini CLIs behind one provider interface. The default transport is the CLI. An opt-in HTTP transport for the same three names is off unless `transport="http"`. It has no default host, model, or token, does not fall back to the CLI, and is not a spending cap.
- **State:** SQLite in `.codehelm/state.db`. Task rows, dispatch claims, budget reservations, and quality evidence live outside the model context.
- **Execution:** one Git worktree per coding task. Worktrees are file isolation, not a container sandbox.
- **Integrations:** optional Jira Cloud REST and draft GitHub pull requests via `gh`. Neither is required to run the engine. Creating a pull request does not merge it. `merge-ready` is off unless `auto_merge: true`, and even then it merges only an open pull request whose gates passed, whose decision required human approval, and whose checks are all success. It does not pass `--auto` or `--admin`, and it does not complete the story. An explicit `jira-transition` command posts one transition id; a dry run does not call Jira. The scheduler still does not transition issues or merge pull requests.

## Task lifecycle

Agent execution persists `queued → running → awaiting_validation` or `failed`. A zero exit code is not story completion.

Explicit follow-up commands then run:

`awaiting_validation → tests → security → independent review → awaiting_documentation → documenting → awaiting_post_docs_validation → tests → security → independent review`

With no quality policy file, success ends at `awaiting_pr`. Failures persist as `blocked` or `changes_requested`. `pr-create` from `awaiting_pr` records `pr_created`. Nothing on this path merges or marks a story complete. `schedule-durable` also stops a successful agent at `awaiting_validation` when the policy file is absent.

With `.codehelm/quality.yaml` present, `schedule-durable` runs those gates inside the claimed attempt and then scores the contract. The manual post-documentation command does the same scoring. The task and the dispatch row become `blocked`, `awaiting_human_approval`, or `ready_for_pr`. `pr-create` is allowed only from `ready_for_pr`, and it evaluates the recorded evidence again before publishing. A failed gate does not launch the later model calls for that attempt.

Story completion in the durable scheduler is a separate step. It requires a recorded pull request, `--confirmed-merged`, and `gh pr view` reporting `state=MERGED` with a merged timestamp. An open or closed pull request does not complete the story. When a quality policy is active it also requires the latest evaluation for that attempt to be `ready_for_pr`. Dependents stay locked until that completion. The scheduler does not merge the pull request. `merge-ready --execute` can merge one recorded pull request when `auto_merge` is true, every gate passed, required human approval is present, and every GitHub check is success. A result that is not yet `MERGED` is a failure. That command does not mark the story complete. The durable scheduler does not transition Jira issues. An explicit `jira-transition` command posts one transition id; a dry run does not call Jira.

## Durable scheduling

`DurableScheduler` stores one row per story. Claims use `BEGIN IMMEDIATE`. Limits:

- per-story attempts (default 2, maximum 5)
- total attempts per prefix (default 20)
- concurrent claims (default 2, maximum 8)
- estimated token and USD reservations in the same transaction as a budgeted claim
- optional additional-AUD monthly admission (warn A$10, critical A$15, pause A$20, epic A$5)

These are admission controls on estimates CodeHelm stores. They are not provider-side spending caps. Live token use is unknown unless a verified usage report is imported, structured CLI stdout is recognized by `parse_cli_usage`, or the opt-in HTTP transport returns a usage object `parse_usage` understands. Unknown usage is not stored as zero. HTTP is off by default and is not a spending cap.

A claim heartbeat refreshes `claimed_at` during the agent and, when a policy exists, between quality stages. If the lease expires, the story becomes `interrupted`. Recovery never relaunches a coding agent that is still queued, running, or failed. If the agent had already finished, the next `schedule-durable --execute` can resume that same attempt and skip gates that already passed. `schedule-retry` remains the way to start a new task id (`PREFIX-S1-A2`). Resume does not take a second budget reservation.

## Quality contract

Policy file: `.codehelm/quality.yaml` with `schema_version: 1`. If the file is absent, `load_policy` reports the contract inactive and existing commands keep their previous transitions. Omitted keys use defaults (tests, security, architecture review, independent review, documentation, acceptance criteria, 400 changed lines, and human approval for authentication, migrations, security-sensitive paths, and production infrastructure). `architecture_cmd` defaults to empty. When `risk_patterns` is omitted or null, a small built-in content scanner runs. A configured mapping replaces it, and `{}` turns content scanning off. `gate_runner` defaults to `namespace`. `gate_image` is required only when `gate_runner` is `container`; an empty or missing image in that mode fails closed. In namespace mode `gate_image` is ignored. Unknown schema versions and unknown keys fail closed.

`TaskStore.initialize` is the migration. It creates `quality_evidence` and `quality_evaluations` with `CREATE TABLE IF NOT EXISTS`, adds any missing columns, and records state schema version 3 in `schema_migrations`. Version 3 adds `quality_evidence.signature`. Existing task and dispatch rows are left in place. `codehelm init` does not write a quality policy.

`validate`, `document`, `docs-exception`, `validate-after-docs`, and the durable quality wave append evidence. `schedule-quality` does not run those commands again. It and the post-documentation gate call `assess`, which reads only the current attempt:

| Check | Source | How to read it |
| --- | --- | --- |
| Tests, security, optional architecture command | `command_gate` exit code | Process result. Architecture review is still a model verdict. When that review is required, `architecture_cmd` must also exit 0 |
| Independent review, architecture review | `independent_review` (exact `APPROVED` line, different provider, snapshot unchanged) | Model verdict recorded by the orchestrator |
| Documentation | documentation agent file-hash check, or `docs-exception` (`source=human`, GitHub login, nonempty reason, passed) | Process result, or a human exception for this attempt. A dry run does not call GitHub. It does not waive tests, security, review, acceptance, or human approval |
| Diff size and risk paths | `git` status/diff plus policy globs | Repository state. When `risk_patterns` is omitted or null, a built-in scanner checks changed files. A configured mapping replaces it. `{}` turns content scanning off. It is a heuristic and does not detect every risky edit. Path globs still apply |
| Acceptance criteria | `quality-attest` (`source=human`) or a `CODEHELM_ACCEPTANCE` line captured from the test process (`source=test_report`) | A passing exit code alone verifies nothing. A report file is used only when that process creates or changes it |
| Human approval | `quality-approve` (`source=human`, identity is the `gh api user` login, detail is a sha256 digest of the workspace diff, optional `signature`) | A typed name that is not that login is refused. A later change makes that approval not count. With `approval_signature` off, the row is not a cryptographic signature. `required` checks an SSH or GPG signature over the current digest at scoring time. Later scoring does not call GitHub again |
| Quality debt | `quality-debt` (`source=human`, detail is the finding, identity is the reason, passed) | Accepts one matching unresolved finding on the current attempt. A debt from another attempt does not count. A debt whose text does not match a finding is ignored. It does not waive tests, security, review, architecture, docs, acceptance, or human approval |
| Oversized justification | `quality-justify` records the GitHub login and the reason (`source=human`, identity is the login, detail is the reason) | A dry run does not call GitHub. A row with no identity does not count. Later scoring does not call GitHub again |

`record_evidence` rejects any other source, including an agent-supplied JSON document. Outcomes are appended to `quality_evaluations` and copied onto the task and, from `schedule-quality --execute`, the dispatch row.

`schedule-quality` still refuses an interrupted story. Resuming it first re-claims the same attempt; `schedule-retry` starts a new attempt, and only that new attempt's rows count.

High-risk classification uses path globs. When `risk_patterns` is omitted or null, a built-in scanner also checks changed files for a PEM or OpenSSH private-key header, an `AKIA` access-key id, a `ghp_` or `github_pat_` token, and a line that drops a table or column. A configured mapping replaces that set. `risk_patterns: {}` turns content scanning off. A match is a heuristic, not proof of a secret or a migration, and the scanner does not detect every risky edit. Path globs still apply. A risky change outside those globs and the active content patterns is not detected unless an operator records a `risk` row. `quality-approve --execute` stores the GitHub login and a sha256 digest of the workspace diff. A later change makes that approval not count. With `approval_signature` off, that row is not a cryptographic signature. `approval_signature: required` demands an SSH or GPG signature over the digest, checked again when the story is scored. Evaluation does not repeat the GitHub call. Test and security commands run in a private user, mount, pid, and network namespace. The workspace stays writable; `/tmp`, `/home`, and `/root` are covered, and there is no unsandboxed fallback. A host that blocks unprivileged user namespaces refuses the gate. The coding agent and the model reviewer stay outside that namespace. Each quality debt accepts one unresolved finding for the current attempt. More than one debt can be recorded. `quality-debt` stores that finding as detail and a nonempty reason as identity (`source=human`, passed). Evaluation drops a finding only when its text matches. A debt from another attempt does not count, and a debt whose text does not match a finding is ignored. The debt does not waive tests, security, review, architecture, docs, acceptance, or human approval. The namespace sandbox is not a container image. An optional container runner is off by default. When `gate_runner` is `container` and the local runtime or `gate_image` is unavailable, the gate fails closed and the command does not run on the host or through the namespace sandbox. That runner does not pull an image, and it does not run the coding agent or the model reviewer. Architecture review reuses the independent-review function with a different prompt; it is still a model verdict. When that review is required, an optional `architecture_cmd` runs first and must also exit 0. It does not replace the review, and turning the review off skips the command. `max_retries` on the policy is not a second retry loop. `DispatchBudget` remains the enforced attempt cap. An omitted `--max-attempts` uses `policy.max_retries` when the policy file exists.

## Budgets and provider use

Token and USD figures are reservations plus optional reconciliation of a verified provider JSON report. Missing fields fail closed. AUD controls apply to an explicitly supplied additional-charge estimate, or to a USD amount multiplied by an operator-supplied AUD-per-USD rate and a source label. CodeHelm does not fetch a rate and does not assume one. Do not describe any of these as guaranteed spend protection.
