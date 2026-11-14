# Architecture — draft

## Control plane
- **AI planning:** task decomposition, acceptance criteria, unexpected blocker analysis.
- **Deterministic engine:** state transitions, dependency graph, budgets, retries, permissions, event-driven monitoring.
- **Adapters:** CLI and API providers with explicit capabilities and authentication methods.
- **State:** SQLite initially; PostgreSQL later. State lives outside model context.
- **Context:** provider-specific minimal instruction files and just-in-time source retrieval.
- **Execution:** one Git worktree per coding task; container sandbox when executing untrusted code.
- **Integrations:** optional Jira epics, stories, bugs and progress updates.

## Proposed lifecycle
`created → planned → queued → running → validating → reviewing → documenting → pr_ready → completed`

Failure states: `blocked`, `failed`, `cancelled`. All transitions must be persisted and auditable.

## Review and safety
Independent review is mandatory for every completed story. Deterministic tests and security scans are separate gates. PR creation does not imply permission to merge. Human approval is required for destructive actions and production deployment.

## Efficiency
Avoid model calls for polling, linting, tests, and Jira writes. Measure token usage by provider, task and epic. Keep retry limits and use structured compact outputs.

## Scope
This is a design document. Only CLI initialization and status are currently implemented.

## Implemented v0.5 gates
Explicit test command → explicit security command → different-provider AI review.
Persist all transitions and stop at `awaiting_documentation` on approval.
No automatic merging, PR creation or story completion.
