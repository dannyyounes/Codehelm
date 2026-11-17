# CodeHelm

[![Python tests](https://github.com/dannyyounes/Codehelm/actions/workflows/tests.yml/badge.svg)](https://github.com/dannyyounes/Codehelm/actions/workflows/tests.yml)

**CodeHelm** is an open-source Python CLI (MIT) for orchestrating AI software engineering agents. The installed package version is still `0.1.0`. The notes below describe behavior that is in this tree, including the optional v1.7 quality contract.

## Current behavior

Implemented in this repository:

- Local configuration, SQLite task state, and Git worktree isolation
- Claude Code, Codex, Gemini, and Cursor Agent CLI adapters
- Opt-in HTTP transport for those same three providers, off unless `--transport http` is set
- Single-task lifecycle, dependency-aware dispatch, and a durable SQLite scheduler with bounded attempts, leases, and explicit retry
- Estimated token/USD reservations and an additional-AUD admission budget
- Test, security, and independent-review gates, a documentation agent, a second validation pass, GitHub pull requests (draft unless `--ready` or `pr_ready: true`), and optional Jira Cloud REST
- An optional quality contract, active only when `.codehelm/quality.yaml` exists
- DesignFlow screen and form mockups, inline comments, and an explicit agent revision loop

There is no default exchange rate. `--fetch-aud-rate` reads AUD per USD from frankfurter.app only when set, stores that source and the rate date, and fails closed when the fetch fails or the date is more than five days old. It converts `--additional-usd` unless `--convert-reserve-usd` is also set, in which case it converts `--reserve-usd` and refuses `--additional-usd`. `--convert-reserve-usd` is off by default. `plan-publish --execute` writes each returned story key into the plan. A dry run does not write one, and a plan that already has `jira_key` is refused. HTTP is opt-in and off by default. It requires `CODEHELM_CLAUDE_API_URL`, `CODEHELM_CLAUDE_API_TOKEN`, and `CODEHELM_CLAUDE_MODEL`, or the matching `CODEHELM_CODEX_*` or `CODEHELM_GEMINI_*` variables, and it reads them only when HTTP is selected. The CLI adapters remain the default. HTTP is not a spending cap and does not cover every provider feature. Structured CLI stdout is recorded when `parse_cli_usage` recognizes it; otherwise the reservation stays an estimate. Unknown USD is not stored as zero. An explicit `jira-transition` command posts one transition id; a dry run does not call Jira. The scheduler posts a Jira transition only when `jira_transitions` maps the new state and the story has a `jira_key`. That is off by default, and it stays a dry run unless `jira_transition_execute` is true. A failed post is stored on the task and reported. `schedule-durable` merges a recorded pull request only when `auto_merge` is true, using the same checks as `merge-ready`. That is off by default. The plain `schedule` command does not merge. `merge-ready` merges one recorded pull request only when every gate has passed, required human approval is on that decision, and every GitHub check is success. A dry run does not call GitHub, and a merge does not complete the story. `pr-create` opens a draft pull request unless `--ready` is passed or `pr_ready` is true; `--draft` forces a draft. That command does not merge. Each `quality-debt` row accepts one unresolved finding for the current attempt. It does not waive tests, security, review, architecture, docs, acceptance, or human approval. A debt from another attempt does not count, and a debt whose text does not match a finding is ignored. Test and security commands run in a Linux namespace sandbox. Each namespace gate stages its bind mounts in a new directory under `/dev/shm`, so concurrent gates do not share a stage path when the sandbox process is pid 1. An optional container runner for those commands is off by default and fails closed when selected but unavailable. The gate does not pull an image. A workflow publishes `ghcr.io/dannyyounes/codehelm` on pushes to `main` and on version tags. Pin `gate_image` to the digest that workflow prints. CodeHelm does not fetch that digest and does not fall back to the host. Optional `prepare_cmd` runs on the host in the new worktree before the coding agent, with the operator's environment and network. Failure marks the task `failed` and does not start the agent. The quality contract does not score that result, and `agent-run` does not run the command. When `risk_patterns` is omitted, a small built-in content scanner runs. A configured mapping replaces it, and `{}` turns content scanning off. High-risk approval stores the GitHub login and a sha256 digest of the working tree against the resolved merge-base. That digest is a length-prefixed cached binary diff of a temporary index after `git add -A`, so committed changes and untracked files are included and the real index is not modified. Committing that same content leaves the digest unchanged. A content edit makes that approval not count. A dirty submodule, an untracked nested Git repository, or a missing merge-base yields an empty digest, and approval is refused. When no base resolves, the digest is only the uncommitted diff against HEAD plus untracked files, and committing that work changes it. With `approval_signature` off, that row is not a cryptographic signature. `approval_signature: required` demands an SSH or GPG signature over the digest, checked again when the story is scored. A successful agent exit does not complete a story. Budgets remain admission estimates, not guaranteed spending protection and not a provider-side cap.

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

`codehelm --help` lists the commands that exist today: `init`, `status`, `run`, `validate`, `document`, `validate-after-docs`, `pr-create`, worktrees, providers, planning, Jira, durable scheduling, budgets, the v1.7 quality commands, and `designflow`.

`codehelm agents` lists task rows in `.codehelm/state.db`: one line each with id, status, provider, and updated_at. Queued and running tasks come first, then other tasks by updated_at descending. The default limit is 20 and `--limit` accepts 1 to 100. It does not launch an agent. `codehelm resume <task-id>` continues an interrupted durable story on the same attempt. The task id must look like `PREFIX-STORY-An` (for example `PLAN-S1-A1`). Without `--execute` it names the task and does not change dispatch state. With `--execute` it re-claims that interrupted story and carries the same attempt forward. It refuses to relaunch a queued, running, or failed coding agent. `resume` accepts the same `--agent-timeout` and `--gate-timeout` bounds as `schedule-durable` (see Durable scheduling). `schedule-retry` is still how a new attempt starts. Expired claims are marked with `schedule-recover` and retried explicitly with `schedule-retry`. That retry starts a new attempt; it does not resume an in-memory model session.

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

`.gitignore` excludes bytecode, caches, virtualenvs, and local `.codehelm/` artifacts. `pytest tests/test_repo_hygiene.py` fails if any `*.pyc` file is tracked in git.

Lifecycle timeout defaults and CLI wiring are covered by `tests/test_lifecycle_timeouts.py`. Verify with:

```bash
pytest tests/test_lifecycle_timeouts.py
codehelm validate --help
codehelm document --help
codehelm validate-after-docs --help
codehelm schedule-durable --help
codehelm resume --help
```

Help text lists `--timeout` / `--agent-timeout` / `--gate-timeout` with their documented defaults (300 or 600) and the 1..7200 bound.

## DesignFlow

DesignFlow writes a visual mockup of a screen or form, stores inline comments on that mockup, and sends those comments to an agent for a revised mockup. Ported from firstmate's lavish.

```bash
codehelm designflow draft --title "Login" --brief "Sign in with email" \
  --field "email|Email|email|required|you@example.com" \
  --field "password|Password|password|required" \
  --field "submit|Sign in|button"
codehelm designflow serve login
codehelm designflow comment login --element email --text "Say email address"
codehelm designflow send login
codehelm designflow revise login --provider claude --execute
```

`draft` renders the fields you pass and does not call a provider. `--spec` renders a JSON screen spec the same way. `--execute` asks the provider for that JSON and renders only a spec that validates. `comment` stores one comment on a field uid, or on `screen` for the whole mockup. Comments stay on the mockup until `send`. `send` queues them and does not launch a provider. `revise` without `--execute` prints the queued comments. With `--execute` it sends those comments to the provider, checks the returned JSON, and writes the next revision. A failed response leaves the mockup and the queued comments unchanged.

`serve` listens on `127.0.0.1` only. The page lets you click an element, add a comment, and send the open comments. Sending does not launch a provider. The page names the `revise --execute` command that does. Refresh the page after that command to see the revised mockup.

The agent sees the comments as reviewer evidence about the mockup. That evidence does not authorize repository edits, merges, or any other work. DesignFlow does not schedule stories and does not merge pull requests. Provider output is not written as HTML. The renderer builds the page from the validated spec.

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

Supported initial CLI names: `claude`, `codex`, `gemini`, `cursor`. CodeHelm uses existing CLI authentication and permission policies. `agent-run` does not read `prepare_cmd` and does not create a task worktree. Without `--execute`, it only prints a dry-run message. CLI command syntax can vary by installed version; test compatibility before relying on a provider. Agent processes can modify files or incur usage charges. Run only in trusted or sandboxed workspaces.

The `cursor` provider runs `cursor-agent -p --trust --output-format text --model <model> -- "<prompt>"` in the story worktree. The model comes from `CODEHELM_CURSOR_MODEL` and defaults to `auto`. Authentication is the Cursor CLI's own: `CURSOR_API_KEY` in the environment or `cursor-agent login`. CodeHelm does not read or pass the key. In print mode Cursor Agent can write files; shell commands follow its normal permission policy because CodeHelm does not pass `--force`. `cursor` is a distinct provider name, so `--provider cursor --reviewer codex` satisfies the different-reviewer rule and `cursor` reviewing `cursor` is refused. Structured usage is read from `--output-format json` token counts, with cache read and write tokens added to input; USD stays unknown. There is no HTTP transport for `cursor`.

**Not in this milestone:** direct API implementations, task scheduling, token accounting, structured agent output, automatic Jira operations, and security sandboxing. Scheduling, Jira REST, and estimated budgets were added later. An opt-in HTTP transport for the same three providers was added later and is off by default. The CLI adapters remain the default.

## Opt-in HTTP transport

HTTP is off by default. `run`, `agent-run`, `schedule`, and `schedule-durable` accept `--transport cli` (the default) or `--transport http`. A dry run does not call the network. Selecting HTTP does not fall back to the CLI. A missing or invalid URL, token, model, or response raises an error and does not launch a provider process.

The endpoint, bearer token, and model are read only from these environment variables, and only when HTTP is selected. There is no default host, model, or token.

- Claude: `CODEHELM_CLAUDE_API_URL`, `CODEHELM_CLAUDE_API_TOKEN`, `CODEHELM_CLAUDE_MODEL`
- Codex: `CODEHELM_CODEX_API_URL`, `CODEHELM_CODEX_API_TOKEN`, `CODEHELM_CODEX_MODEL`
- Gemini: `CODEHELM_GEMINI_API_URL`, `CODEHELM_GEMINI_API_TOKEN`, `CODEHELM_GEMINI_MODEL`

The token is sent in the `Authorization: Bearer` header. It is not placed in the URL, the prompt, or the error text. Each request uses one fixed JSON body and does not cover every provider feature. Claude sends `model`, `max_tokens` 1024, and the prompt as the user message, plus the header `anthropic-version: 2023-06-01`. That pinned header is the value this client sends; it is not a promise that the value matches every future account. Codex sends `model` and `input`. Gemini sends the prompt as a user content part and does not copy the model into that JSON or the query string. The operator-supplied URL is posted to as given, and the Gemini token is not added to the query string.

A response is success only for HTTP 200, and only when the documented text field is present (`content[].text`, `output_text`, or `candidates[0].content.parts[].text`). Multiple text parts are concatenated. Any other shape is an error. CodeHelm does not invent a completion from missing text. A usage object that `parse_usage` already understands is attached. A body without one leaves usage unknown. Unknown usage is not stored as zero. This transport is not a spending cap.

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
milestones. The CLI adapters remain the default. HTTP stays off unless `--transport http` is selected.
Provider CLIs can execute code and incur charges; use trusted workspaces and
provider permission settings.


## Quality gates (v0.5)

After `codehelm run` exits successfully, the task is `awaiting_validation`.
Run explicit test and security executables, followed by an independent CLI reviewer:

```bash
codehelm validate DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
codehelm validate DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --timeout 900 --execute
```

`--timeout` (default 300, range 1..7200) is the per-command limit for the test executable, security executable, and independent reviewer. It applies only with `--execute`. Out-of-range values are rejected by the CLI.

**Important:** `--test-cmd` and `--security-cmd` each accept an executable name only
(no arguments). Install/configure those commands in the agent worktree environment
before running validation. `prepare_cmd` in `.codehelm/quality.yaml` is the host
argv that can install those dependencies in the new worktree before the coding
agent. Files it writes remain in that worktree for later validation.
See Quality contract (v1.7). `--test-cmd` and `--security-cmd` stay a
single executable name. For example, `bandit` without configured targets may
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

**Reviewer story context.** A task id shaped `PREFIX-STORY-An`
(example `PLAN-S1-A1`) makes `validate --execute` and
`validate-after-docs --execute` read `.codehelm/plan.json` in the working
directory and prepend the matching story to the reviewer prompt. The id is
three segments: a prefix and a story id, each letters, digits, or
underscores, then `A` and an attempt number that does not start with `0`.
The middle segment must equal a story `id` in that file (`S1`, `S2`, ...).
`DEV-123` does not match, so the commands above keep the original prompt.
A dry run returns before opening the plan. A missing file, a plan that
`load_plan` rejects, or an unknown story id also keeps the original prompt.
That lookup failure leaves the command on the same path as a task with no plan.

The prepended block is the heading `Story`, the title, the whole
description, a blank line, the heading `Acceptance criteria`, and one
`- ` line per criterion. Text about scope or a threat model is included
when it is written in those fields. Headings inside the description are
ordinary description text. There is no plan field and no CLI flag for a
separate scope section. The heading `Declared scope and threat model`
appears only when Python code passes `scope_text` to `independent_review`.
The CLI and the durable wave do not pass that argument.

A block longer than 12000 characters is cut at that length. The cut can
split a line and can drop criteria that fall past it. This instruction,
then the original review prompt, follow the cut in full:

```text
Judge the change against the story and acceptance criteria. Report real defects: correctness, regressions, security issues within the declared scope, and missing tests for the criteria. Do not request hardening for cases the story declares out of scope. If remaining concerns are only out-of-scope or optional improvements, list them as notes and still return APPROVED.
```

Approval is still the reviewer's last line exactly `APPROVED`, a different
provider from the author, and an unchanged Git snapshot. CodeHelm does not
classify findings as in or out of scope. With none of the story fields set,
the prompt is the original code or architecture prompt. `validate` uses the
code prompt. `validate-after-docs` uses the code prompt and, when the
quality policy runs architecture review, the architecture prompt.
When a quality policy file is present, `schedule-durable` and `resume`
pass the claimed story's title, description, and acceptance criteria from
`--plan-file` (default `.codehelm/plan.json`) into every code review and
architecture review in that wave. `schedule-quality` does not call the
reviewer.

```bash
pytest tests/test_quality.py::test_review_prompt_unchanged_without_story_context \
  tests/test_quality.py::test_review_prompt_includes_story_context \
  tests/test_quality.py::test_story_context_is_truncated \
  tests/test_quality.py::test_validate_passes_story_context \
  tests/test_quality.py::test_validate_cli_passes_plan_story \
  tests/test_durable_quality.py::test_carry_story_sends_plan_story_to_reviewer
```

**Security limitation:** This prototype invokes user-selected executables and
AI CLIs directly. The default gate sandbox is a namespace, not a container
image. Concurrent namespace gates each use a new stage directory under
`/dev/shm`. An optional container runner for gate commands is off by default and
fails closed when selected but unavailable. It can name the published GHCR
image, and it still does not pull that image. It does not run the coding agent
or the model reviewer. Run only on trusted code with appropriate permissions.
The reviewer is instructed not to edit files, but the Git snapshot guard is
not a security boundary. AI approval is not human merge authorization.

## Documentation Agent (v0.6)

After a task passes test, security and independent code review gates:

```bash
codehelm document DEV-123 --provider claude --execute
codehelm document DEV-123 --provider claude --timeout 1800 --execute
codehelm task-status DEV-123
```

`--timeout` (default 600, range 1..7200) is the documentation agent process limit. It applies only with `--execute`. Out-of-range values are rejected by the CLI.
The documentation agent examines implementation and tests and updates relevant
README, `docs/`, API, user, or developer documentation. Paths it may change are
`README.md`, anything under `docs/`, paths ending in `.md` or `.rst`, and paths
starting with `CHANGELOG`. Any other edit fails with
`Documentation agent modified non-documentation files`, including when a
documentation path is already in the diff.

`codehelm document` sends the base prompt only. `schedule-durable` and `resume`
append the claimed story's title, description, and acceptance criteria when
they run this agent (`require_docs` defaults to true). That text tells the
agent to respect any documentation scope or line limits stated there and to
make no edit when documentation is already complete. Those limits are prompt
text. Whitespace-only title, description, and criteria add nothing.

A zero exit is checked with content hashes of the union of paths from
`git status --porcelain -uall` before and after the agent. A path that was
dirty before and is clean or missing afterward counts as an agent edit. A
documentation-only edit passes. Its evidence detail is the agent stdout, last
2000 characters, or `Documentation changes detected` when stdout is empty. When
the agent edits nothing, the gate passes with detail `Documentation already
included in the change` if `collect_diff` already lists a documentation path.
That diff is committed changes from the merge-base with the resolved base
through `HEAD`, plus uncommitted and untracked paths. The base comes from
`resolve_diff_base` and `base_branch` in `.codehelm/quality.yaml` beside the
state database. A missing file or an omitted key leaves the name empty, so
only the main/master fallbacks are tried. A named ref that is not present
falls through to those fallbacks. Lookups use only `refs/heads/<name>` and
`refs/remotes/origin/<name>`. When no documentation path is in that diff, the
detail is `No documentation changes detected`. A nonzero agent exit fails
with `Documentation agent failed:` plus the provider stderr, last 1500
characters, and does not consult that diff.

Failure on this command sets `blocked`. For an agent result, the printed
task error is the evidence detail. Success sets
`awaiting_post_docs_validation`. It does not mark the story complete. On the
no-edit check, an invalid policy or a diff that cannot be computed
(`Could not compute the documentation diff against the resolved base`) is
stored on the docs evidence row and the task becomes `blocked`. The printed
task error is `documentation:` plus that exception. A second
validation pass, draft PRs, and Jira comments were added in v0.7–v0.9.

Limitations: the edit check reads paths from `git status --porcelain -uall`
and compares file hashes. A non-documentation path that was dirty before the
agent and is clean or missing afterward counts as an edit and fails the gate.
Ignored files are not listed. A commit that is clean in both snapshots is not
an agent edit; with no porcelain edit, a documentation path already in the
resolved-base diff still passes. The documentation CLI runs with its normal
local permissions; use a sandbox for untrusted code. A change with no
documentation path still needs a human exception:
`codehelm docs-exception TASK --reason TEXT --approver LOGIN`. With
`--execute`, CodeHelm records it only when `--approver` strips to the
GitHub login from `github_user_login()` and the reason is nonempty. A dry
run does not call GitHub and writes nothing. The exception does not waive
tests, security, review, acceptance, or human approval.

Verify with `pytest tests/test_documentation.py`. After a task is
`awaiting_documentation`:

```bash
codehelm document DEV-123 --provider claude --execute
codehelm task-status DEV-123
sqlite3 .codehelm/state.db "SELECT passed, detail FROM quality_evidence WHERE task_id='DEV-123' AND gate='docs' ORDER BY id DESC LIMIT 1;"
```

`document` prints `DEV-123: awaiting_post_docs_validation; ` when the gate
passes. The evidence detail is `Documentation already included in the change`
when the agent exited 0, edited nothing, and the resolved-base diff already
has a documentation path. A miss prints `DEV-123: blocked; No documentation
changes detected`. A source edit, including restoring a dirty source file to
`HEAD`, prints `DEV-123: blocked; Documentation agent modified non-documentation
files`. An invalid `.codehelm/quality.yaml` on that no-edit path prints
`blocked` and a task error of `documentation:` plus the policy error. The
evidence `detail` is that policy error without the prefix. `task-status`
prints the status and does not print the evidence detail.

## Post-documentation validation (v0.7)

After `codehelm document` succeeds, run the required test, security and
independent-review gates again:

```bash
codehelm validate-after-docs DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --execute
codehelm validate-after-docs DEV-123 --reviewer codex --test-cmd pytest --security-cmd bandit --timeout 900 --execute
```

`--timeout` (default 300, range 1..7200) is the per-command limit for the post-documentation test, security, and independent-review steps. It applies only with `--execute`. Out-of-range values are rejected by the CLI.
This command uses the same `.codehelm/plan.json` story lookup as `validate`.
The story is sent to the code review and, when the policy runs it, to the
architecture review.
Successful gates transition to `awaiting_pr`, **not** `completed`, when no
quality policy file is present. A failure blocks the task or requests changes.
CLI commands currently accept executable names without arguments. GitHub PR
creation and Jira integration were not in this milestone; see v0.8 and v0.9.
Review is a model verdict, not human merge authorization. When
`.codehelm/quality.yaml` is present, a passing post-documentation run is
scored by the quality contract instead of moving straight to `awaiting_pr`.

## GitHub pull requests (v0.8)

Requires GitHub CLI `gh` installed and authenticated, a GitHub `origin`
remote, and a clean task worktree with committed changes.

```bash
codehelm pr-create DEV-123 --title "Fix DEV-123" --body "Validated changes"
codehelm pr-create DEV-123 --title "Fix DEV-123" --body "Validated changes" --execute
codehelm pr-create DEV-123 --title "Fix DEV-123" --body "Validated changes" --ready --execute
codehelm pr-create DEV-123 --title "Fix DEV-123" --body "Validated changes" --draft --execute
```

The command is dry-run by default. It loads `.codehelm/quality.yaml` from the
current working directory. `--repo` selects the Git repository and leaves
that policy path unchanged. The dry run prints one of these lines and returns.
It does not check the task, push, or call `gh`:

```text
Dry run. Add --execute to push branch and create a draft PR.
Dry run. Add --execute to push branch and create a ready PR.
```

The pull request is a **draft** unless `--ready` is passed or `pr_ready` is
the boolean `true`. `--draft` forces a draft, including when `--ready` and
`pr_ready: true` are both set. A missing policy file, an omitted key,
`pr_ready: false`, and a null `pr_ready:` stay draft. A non-boolean, including
the string `'true'`, fails load with `pr_ready must be a boolean` and exits 1
before the dry-run line.

With `--execute`, CodeHelm verifies that the task is `awaiting_pr` when no
quality policy file is present, or `ready_for_pr` when
`.codehelm/quality.yaml` is present, checks the worktree and branch, pushes
with `git push -u origin <branch>`, and runs `gh pr create`. A draft command
is `gh pr create --draft --base <base> --head <branch> --title <title>
--body <body>`. A ready pull request uses that same argument list with
`--draft` omitted. CodeHelm does not run `gh pr ready` or any merge command.
Success prints `Draft pull request created: <url>` or `Pull request created:
<url>` and records `pr_created` with that URL. When `--base` is omitted, the
target branch is the policy `base_branch` if that key is set (for example
`base_branch: staging`), otherwise `main`. An explicit `--base` still wins.
Change counting uses the same policy key; see the quality contract section.
It does not approve or transition Jira issues. `merge-ready` is a separate
command and is off unless the quality policy sets `auto_merge: true`. An
explicit `jira-transition` command posts one transition id; a dry run does
not call Jira. The scheduler posts one only when `jira_transitions` maps the
new state, and that post stays a dry run unless `jira_transition_execute` is
true. `pr-create` does not transition Jira.
Push/PR failures retain the same required status (`awaiting_pr` or
`ready_for_pr`) for manual inspection; retries can require checking for an
existing remote PR.

`jira-link-pr` links a `pr_created` task whose stored value starts with
`https://github.com/`. The comment is `CodeHelm draft pull request: <url>`
for a ready pull request and for a draft. `schedule-complete` accepts
`pr_created` and then asks `gh pr view` whether that URL is `MERGED`. A task
that is missing or not `pr_created` raises
`A draft PR must exist before manual completion`.

Verify the choice without GitHub:

```bash
pytest tests/test_github_pr.py
codehelm pr-create --help
codehelm pr-create DEV-123 --title "T" --body "B"
codehelm pr-create DEV-123 --title "T" --body "B" --ready
```

`--help` describes `--execute` as pushing and creating the pull request,
`--ready` as creating a ready pull request instead of a draft, and `--draft`
as forcing a draft even when `pr_ready` or `--ready` is set. With no policy
file, or with `pr_ready` omitted, null, or false, the dry run without flags
prints `create a draft PR.` and `--ready` prints `create a ready PR.` With
`pr_ready: true`, the dry run without flags prints `create a ready PR.` and
`--draft` prints `create a draft PR.`

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

`jira-link-pr` posts `CodeHelm draft pull request: <url>` on the Jira issue
whose key matches the task ID. The task must be `pr_created`, and the stored
value must start with `https://github.com/`. A ready pull request uses that
same comment. It does not change Jira status. Success prints
`Linked draft PR to <task-id>; Jira status unchanged`. A task that is not
`pr_created`, or has an empty stored value, raises `Task must have a recorded
draft PR before linking`. A non-empty value that does not start with
`https://github.com/` raises `Stored PR URL is invalid`.

`jira-transition KEY --id TRANSITION_ID` prints a dry-run line that names
the key and the transition id and does not call Jira. With `--execute` it
posts that one transition id. The scheduler uses that same client only when
`jira_transitions` maps the new dispatch state to a transition id and the
story's `jira_key` is set. Omitting the mapping, or setting it to `false`,
leaves the scheduler off. `jira_transition_execute` defaults to false, so a
mapped state records a dry run and does not call Jira. A missing key or a
Jira error is stored as `jira_transition_failed` and reported. The story
state is not rolled back. `schedule-durable` merges a recorded pull request only when `auto_merge` is true. The plain `schedule` command does not merge, and a merge does not complete the story.

**Limitations:** This is a minimal Jira Cloud REST adapter, not autonomous
epic decomposition, bulk story scheduling, or a bidirectional
sync engine. It does not look up Jira's workflow or invent a transition id. Jira project configurations can differ in supported issue types
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
id; a dry run does not call Jira. The scheduler posts one only when
`jira_transitions` maps the new state and the story has a `jira_key`, and
that post stays a dry run unless `jira_transition_execute` is true. `plan-publish --execute` writes each returned story key into `jira_key` and does not store the epic key. A dry run does not write. A plan that already has a `jira_key` is refused before Jira is called. `schedule-durable` merges a recorded pull request only when `auto_merge` is true. The plain `schedule` command does not.
**Publishing is not idempotent**: re-running can create
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
`ready_for_pr`, a pull request is recorded (`pr_created`), and a person has confirmed the merge.
Existing dispatched task IDs are not rerun automatically. This command still
has no automatic cross-branch integration, dependency merge, distributed
locking, or provider quotas. Durable scheduler state and estimated budgets
were added in v1.2–v1.4; this legacy `schedule` path does not use them.

## Durable scheduling and bounded execution (v1.2)

```bash
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN --max-parallel 2 --max-attempts 2 --max-total-attempts 20 --execute
codehelm schedule-durable --plan-file .codehelm/plan.json --prefix PLAN --reserve-tokens 100 --reserve-usd 0.50 --limit-tokens 1000 --limit-usd 10 --agent-timeout 1500 --gate-timeout 450 --execute
codehelm resume PLAN-S1-A1 --prefix PLAN --plan-file .codehelm/plan.json --agent-timeout 2100 --gate-timeout 700 --execute
codehelm schedule-recover --plan-file .codehelm/plan.json --prefix PLAN
codehelm schedule-retry S1 --plan-file .codehelm/plan.json --prefix PLAN --execute
codehelm schedule-complete S1 --plan-file .codehelm/plan.json --prefix PLAN --confirmed-merged --execute
```

`schedule-durable` and `resume` accept `--agent-timeout` (default 600, range 1..7200) and `--gate-timeout` (default 300, range 1..7200). `--agent-timeout` bounds the coding agent and, when a quality policy continues the claim, the documentation agent. `--gate-timeout` bounds test/security commands and model reviews on that same path. `resume` still does not relaunch a queued, running, or failed coding agent; `--agent-timeout` then applies only if that attempt still runs the documentation agent. Dry runs do not start agents or gates. Out-of-range values are rejected by the CLI. `codehelm run` and `codehelm agent-run` keep their own `--timeout` (default 600, max 3600) and are unchanged by these flags. `prepare_cmd`, when set, uses a fixed 900 second host timeout on the `execute` path and ignores `--agent-timeout`, `--gate-timeout`, and `run --timeout`.
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
When that claim runs the documentation agent and the story has a non-blank
title, description, or acceptance criterion, the prompt includes those fields.
An invalid policy fails `schedule-durable` and `resume` before the agent
starts. If the no-edit diff check raises during that docs stage, the exception
is stored on the docs evidence row and scoring adds `documentation_not_verified`.
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

The AUD estimate is explicitly supplied per task. To convert a USD figure,
pass `--additional-usd`, `--aud-per-usd`, and `--aud-rate-source` together.
Those flags do not fetch a rate, and CodeHelm does not assume one.
`--fetch-aud-rate` is a separate opt-in. It reads AUD per USD from
frankfurter.app, stores that source and the rate date on the reservation,
and fails if the request fails or the date is more than five days old. It
cannot be combined with a supplied rate or a nonzero
`--additional-aud-per-task`. It converts `--additional-usd`. `--convert-reserve-usd`
is a separate opt-in, off by default: with `--fetch-aud-rate` it converts
`--reserve-usd` instead, and it refuses `--additional-usd`. It does nothing
without `--fetch-aud-rate`. The USD reservation itself is unchanged. A dry run of `schedule-durable` does not fetch.
`aud-quote` prints the explicit product and writes nothing.
`aud-quote --fetch-aud-rate` fetches and still writes nothing. Use
`--additional-aud-per-task 0` for runs expected to incur no
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

Omitted keys use those defaults. `architecture_cmd` is optional and empty when
omitted. When `risk_patterns` is omitted or null, a small built-in content
scanner runs. A configured mapping replaces that set. `risk_patterns: {}`
turns content scanning off. `gate_runner` defaults to `namespace`.
`approval_signature` defaults to `off`. `auto_merge` defaults to `false`.
`pr_ready` defaults to `false` when the policy file is missing, the key is
omitted, or the value is null. Set `pr_ready: true` (a YAML boolean) to make
`pr-create` open a ready pull request. A non-boolean, including `'true'`,
fails load with `pr_ready must be a boolean`. `--draft` forces a draft, and
`--ready` opens a ready pull request when the key is false or absent.
`--draft` wins when both flags are passed. Neither path merges or runs
`gh pr ready`. Usage and checks are in the GitHub pull requests section;
`pytest tests/test_github_pr.py` covers the policy and the CLI wiring.
`prepare_cmd` is optional. Omitted, null, or an empty YAML key, the list is
empty and execution does not run a prepare step. A present value uses the same validation as
`architecture_cmd`: a nonempty list of nonempty strings with no NUL
characters. An empty list, a bare string, an empty item, or a non-string
fails policy load with an error that names `prepare_cmd`. The list is an
argv. CodeHelm does not invoke a shell.

With `--execute`, `run`, `schedule`, and `schedule-durable` start the coding
agent through `LifecycleEngine.execute`. `--transport cli` and `--transport http`
both do. When `prepare_cmd` is nonempty, `execute` runs that argv on the host
after the worktree exists and before the provider call. `schedule-durable`
does this only for an attempt that has no task row yet. The working directory
is the new worktree, still the base checkout. The limit is 900 seconds. It
does not use `--timeout`, `--agent-timeout`, or `--gate-timeout`. The process
keeps the operator's environment, including credential variables, and can use
the network, so a step such as `npm ci` or `composer install` can download
dependencies. `gate_runner` does not apply. Gate commands still drop credential
variables inside the namespace or container runner.

Exit 0 records evidence gate `prepare` with source `command_gate`. The agent
then starts in that same worktree and can read files the command wrote. A
relative path the command writes stays in the worktree. The evidence detail
stores the argv and the captured output, cut at 4000 characters. Prepare
output stays on that row. A successful `run` prints the agent output when
the agent produced output, then `Task <id>: awaiting_validation`. A nonzero
exit, a timeout, or a failure to start the process records the same gate as
failed, stores `failed` on the task, and leaves the status path `queued`
then `failed`. The agent does not start. The task exit code is the process
code, or 1 when the process has no exit code. The evidence row keeps a null
exit code in that missing-code case. `run` prints the prepare error on
stderr, cut at 2000 characters, then `Task <id>: failed`, and exits with
that task code. `schedule` prints `<story> -> <task>: failed (<provider>)`
and exits 1. `schedule-durable` prints `<story>: failed`, settles the story
`failed`, and does not run the later quality gates. `assess` and `evaluate`
do not read the row. A missing prepare row does not block `ready_for_pr`.

A dry run does not create the worktree or run the command. `agent-run` and
`worktree-create` do not call `execute`. `execute` refuses a task id that
already has a row, so the same id does not run `prepare_cmd` again.
`schedule-retry` allocates a new task id (`PREFIX-S1-A2`), and that id runs
`prepare_cmd` again. `resume` leaves a failed task failed.

```yaml
schema_version: 1
prepare_cmd:
  - npm
  - ci
```

```bash
pytest tests/test_prepare_cmd.py
```

Operator check, from a Git repository with at least one commit:

```bash
codehelm run --jira DEV-123 --prompt "Fix the failing tests" --provider claude --execute
codehelm task-status DEV-123
```

Success prints `Task DEV-123: awaiting_validation`. `task-status` shows that
status and the worktree path. A relative path the command writes is in that
worktree. A failing command prints the prepare error and `Task DEV-123: failed`.
`task-status` shows `failed` and the same worktree. The coding agent has not
edited it. Use a new task id to run the command again.

`base_branch` is optional. Set it when the integration branch is not
`main` or `master`:

```yaml
schema_version: 1
base_branch: staging
```

When set, change counting uses that name as the preferred integration ref, and
an omitted `pr-create --base` passes the same name to `gh pr create --base`
(`default_pr_base`). Diff resolution for counting tries
`refs/heads/<base_branch>`, then `refs/remotes/origin/<base_branch>`, then
`main`, `master`, `origin/main`, and `origin/master` (same `refs/heads` /
`refs/remotes/origin` form) so a tag with the same name cannot win. Committed
lines are `git merge-base <base> HEAD` through `HEAD` (`merge-base..<HEAD>`
numstat), plus the working tree and untracked files. The approval digest uses that same resolved base, but it is not the numstat. CodeHelm copies the index, runs `git add -A` on the copy (`.gitignore` is honoured), and hashes a length-prefixed cached binary diff of that index against the merge-base. Committed changes and the current working tree, including untracked files, are in that diff, so committing without a further content edit leaves the approval in place. The real index is not modified. A dirty submodule (tracked edits or untracked content), an untracked directory that contains `.git`, or a merge-base that Git cannot compute yields an empty digest, and `quality-approve` records nothing. A clean submodule pointer change is the gitlink commit id only. When no base resolves, the digest falls back to the uncommitted `git diff HEAD` plus untracked file bytes, and committing that work changes the digest so the approval no longer counts. `pr-create` does not
walk that fallback chain; it uses the policy string, or `main` when the key
is absent. Without a resolvable counting base, committed changes can look
empty after commit even though the worktree was dirty before; set
`base_branch` for those repos. A present value must be a nonempty string that
passes `git check-ref-format refs/heads/<name>` (not `--branch`, which expands
checkout shorthands like `@{-1}`). Names equal to `HEAD` or `@`, starting with
`-`, or containing `@{`, are refused. Null, empty, non-string, or invalid
names fail closed when the policy loads. An explicit `pr-create --base` still
wins over the policy default.

Configure and verify:

```bash
# policy load, resolve_diff_base, collect_diff, and pr-create default/override
pytest tests/test_base_branch.py

# operator check (needs a ready_for_pr task and authenticated gh):
# 1) set base_branch: staging in .codehelm/quality.yaml
# 2) commit on a branch based on staging — scoring must still see changed lines
# 3) dry-run first (prints only; does not show the chosen base)
codehelm pr-create DEV-123 --title "T" --body "B"
# 4) omit --base with --execute → draft PR targets staging
# 5) pass --base develop with --execute → draft PR targets develop instead
codehelm pr-create DEV-123 --title "T" --body "B" --execute
codehelm pr-create DEV-123 --title "T" --body "B" --base develop --execute

# approval digest: stable across a content-preserving commit when a base resolves;
# empty for a dirty submodule, an untracked nested repo, or a missing merge-base
pytest tests/test_diff_bound_approval.py
```

`jira_transitions` defaults to off. A nonempty mapping names the only states
the scheduler may post: `awaiting_validation`, `failed`, `blocked`,
`awaiting_human_approval`, `ready_for_pr`, and `completed`. Each value is a
quoted transition id. `jira_transition_execute` defaults to false and is
refused without that mapping. The story needs `jira_key` in the plan.
Publishing a plan does not write that key.
`approval_signers` is a directory of `allowed_signers` and `gpg-public.asc`.
It is required only when `approval_signature` is `required`.
`gate_image` is required only when `gate_runner` is `container`; empty or
missing in that mode fails closed, and any other `gate_runner` value fails
closed. In namespace mode `gate_image` is ignored. For the published image,
pin the digest printed by the GHCR workflow (`ghcr.io/dannyyounes/codehelm`).
CodeHelm does not pull it. `schema_version` must be
`1`. Unknown keys or an unknown schema version fail closed. `max_retries` is
not a second retry loop; the enforced attempt cap remains the scheduler's
`DispatchBudget`. An omitted `--max-attempts` uses `policy.max_retries` when
the policy file exists.

Evidence is taken from rows the orchestrator writes after it runs a check,
and from `git`. An agent-written evidence JSON file is rejected.

| Check | What is stored | What it is |
| --- | --- | --- |
| Tests and security | Exit code from `command_gate` | A real process result |
| Optional architecture command | Exit code from `command_gate` when architecture review is required and `architecture_cmd` is set | Architecture review is still a model verdict. The command must also exit 0 and does not replace it |
| Optional prepare command | Exit code recorded as gate `prepare` with source `command_gate` when `prepare_cmd` is set | Host argv, cwd = the new worktree, after worktree creation and before the coding agent, timeout 900s. The operator's environment, including credential variables, and network are kept. Failure stores task status `failed` (`queued` then `failed`) and does not start the agent. `assess` does not read this row |
| Independent review and architecture review | `independent_review` verdict (`APPROVED` on the last line, different provider, Git snapshot unchanged). A matching story is prepended to that prompt; see Quality gates for the task-id shape and the 12000-character cut | A model verdict recorded by CodeHelm, not human approval |
| Documentation | Documentation-agent file check, an already-included documentation diff, or `docs-exception` (`source=human`, passed, GitHub login, nonempty reason) | A workspace check on this attempt. No new edit still passes when the resolved-base diff already includes a documentation path, with detail `Documentation already included in the change`. A row from another attempt does not count. It does not waive tests, security, review, acceptance, or human approval |
| Diff size and risk paths | `resolve_diff_base` then `collect_diff` (`merge-base..<HEAD` numstat plus working-tree/untracked), policy globs, and `risk_patterns` | Repository state. Optional `base_branch` is preferred before the main/master fallbacks; lookups use only `refs/heads/<name>` and `refs/remotes/origin/<name>` (order: policy base, `origin/<base>`, `main`, `master`, `origin/main`, `origin/master`). When `risk_patterns` is omitted or null, a built-in scanner checks changed files. A configured mapping replaces it. `{}` turns content scanning off. It is a heuristic and does not detect every risky edit. Path globs still apply |
| Acceptance criteria | `quality-attest`, or a `CODEHELM_ACCEPTANCE` line from the test process | A passing exit code alone verifies nothing |
| Human approval | `quality-approve` stores the GitHub login and `diff_digest` for policy `base_branch` (cached binary diff of a temporary index after `git add -A` against the merge-base; `git diff HEAD` plus untracked file bytes when no base resolves) | Committing that same content leaves the approval in place only when a base resolved. A content edit makes that approval not count (`approval_diff_mismatch`, status `blocked`). A commit with no resolved base also changes the digest. An empty digest is not recorded. With `approval_signature` off, that row is not a cryptographic signature. `approval_signature: required` demands an SSH or GPG signature over the digest, checked again when the story is scored. Later scoring does not call GitHub again |
| Quality debt | `quality-debt` records the finding text and a nonempty reason (`source=human`, passed) | Accepts that exact unresolved finding on the current attempt. A debt from another attempt, or whose text does not match, is ignored. It does not waive the other gates |
| Oversized-change justification | `quality-justify` records the GitHub login and the reason | A dry run does not call GitHub. A row with no identity does not count. Later scoring does not call GitHub again |

A test verifies one criterion only by emitting this line on stdout or stderr, or by creating or changing `.codehelm/acceptance-report.txt` in the workspace while it runs:

```text
CODEHELM_ACCEPTANCE	<exact planned criterion>	passed
```

`failed` records the criterion as not verified. A line left in that file before the test process starts is ignored. Pytest's default capture hides `print`, so a wrapper that runs `pytest -s`, or the report file, is what the single-executable `--test-cmd` can use. `quality-attest` still records an operator attestation. When this attempt already has planned criteria, the attested text must be one of them. Without a stored plan, the attestation is still recorded. A criterion with neither is unverified. A failed report line cancels a passed report line for that criterion. It does not cancel an operator attestation.

Path globs classify `migrations/**`, `auth/**`, `security/**`, `deploy/**`,
`infra/**`, and `.github/workflows/**` (including nested forms) as high risk.
When `risk_patterns` is omitted or null, a built-in scanner also checks changed
files for a PEM or OpenSSH private-key header, an `AKIA` access-key id, a
`ghp_` or `github_pat_` token, and a line that drops a table or column. A
configured mapping replaces that set. `risk_patterns: {}` turns content
scanning off. A match is a heuristic risk name, not proof of a secret or a
migration, and the scanner does not detect every risky edit. Path globs still
apply. A match on a changed file adds that risk name even when the path matches
no glob. Files outside the change set are not scanned. A risky edit that
matches neither a path glob nor a content pattern is not detected unless an
operator records it.

Architecture review is still a model verdict. When that review is required, an
optional `architecture_cmd` runs first and must also exit 0. `schedule-durable`
and `validate-after-docs` both run that command before the model review. A
failing command does not launch the model review. The command does not
replace the review. Turning architecture review off skips the command.
The code review and this architecture review receive the story context
described under Quality gates. `schedule-durable` and `resume` take the
title, description, and acceptance criteria from the plan loaded with
`--plan-file`. `validate` and `validate-after-docs` take them from
`.codehelm/plan.json` when the task id is `PREFIX-STORY-An`.

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
codehelm quality-eval PLAN-S1-A1 --execute
codehelm quality-debt PLAN-S1-A1 --finding "the finding text" --reason "why this finding can wait" --execute
```

`schedule-durable --execute` runs the gate commands and model reviews once per
attempt while the claim is held. It does not repeat a stage that already
passed for that attempt. `schedule-quality` does not re-run pytest, bandit,
or the reviewer. It only reads evidence those commands already recorded.
Missing evidence blocks the story. Re-scoring after `quality-attest`,
`quality-approve`, or `quality-debt` does not spend another reviewer call.

`quality-approve --execute` asks `gh api user` for the current login and
records that login only when `--approver` matches it exactly. The task must
already have a workspace directory. The row stores the GitHub login and the
sha256 digest from `diff_digest` using the current policy `base_branch`. A
dry run does not call GitHub and writes nothing. Later scoring does not call
`gh` again. Run `quality-eval --execute` before `pr-create`. With a resolved
base, committing that same content leaves the digest unchanged and the
approval still counts. A content edit makes it not count: `quality-eval`
reports `approval_diff_mismatch` and status `blocked`. The command does not
stage files or rewrite the real index. If the digest is empty — Git cannot
be read, the merge-base cannot be computed, a submodule has uncommitted
content, or an untracked directory contains `.git` — the command exits 1
with `Human approval requires a workspace diff digest` and records nothing.
Scoring then leaves a required approval at `awaiting_human_approval` rather
than `approval_diff_mismatch`. When no base resolves, committing the approved
files changes the digest. Keep the same `base_branch` between approve and
score; a different base is a different digest. With `approval_signature` off,
that row is not a cryptographic signature. `approval_signature: required`
demands an SSH or GPG signature over the digest, checked again when the story
is scored. Verify with `pytest tests/test_diff_bound_approval.py`.

`merge-ready` merges the recorded pull request only when `auto_merge` is `true`, the fresh decision is `ready_for_pr` because human approval was required and is present, and `gh` reports the pull request open with every check `SUCCESS`. A dry run does not call GitHub. `--execute` uses `gh pr merge --merge --match-head-commit` and does not pass `--auto` or `--admin`. If that command leaves the pull request open, including by arming GitHub auto-merge, CodeHelm reports failure and does not turn the flag off. The story stays incomplete until `schedule-complete` sees `MERGED`. `schedule-durable` calls the same merge when `auto_merge` is true and the latest attempt is `pr_created`. A dry run does not call GitHub. The plain `schedule` command does not merge. A failed merge does not turn the flag off and does not complete the story.

The signed bytes are the `sha256:` digest with no added newline. SSH uses `ssh-keygen -Y sign -n codehelm` and an `allowed_signers` file whose principal is the GitHub login. GPG uses a detached signature and `gpg-public.asc`. The key user id must be that login, or an email whose local part is that login. Scoring does not fetch keys and does not open the default GnuPG home. A signature that does not verify does not count.

`docs-exception --execute` records a passing docs row for the current attempt
when `--approver` strips to the GitHub login and `--reason` is nonempty. A
dry run does not call GitHub. The exception does not waive tests, security,
review, acceptance, or human approval. The documentation agent fails
with `No documentation changes detected` when neither the resolved-base diff
nor the agent touched a documentation file. A nonzero agent exit fails with
`Documentation agent failed:` and leaves that diff unused. It passes with
detail `Documentation already included in the change` when the agent exits 0,
makes no new edit, and the resolved-base change already includes one.

Test and security commands run through `unshare` with a private user, mount,
pid, and network namespace. `/tmp`, `/home`, and `/root` are covered. The
workspace is bind-mounted back, and credential environment variables are
dropped. `architecture_cmd` uses this runner when `gate_runner` is
`namespace`. Each bind is staged in a new directory under `/dev/shm`: prefix
`codehelm-ws-` for the workspace, and `codehelm-keep0-`, `codehelm-keep1-`,
and so on for an interpreter directory that would otherwise be covered. The
directory name does not include the process id. `unshare --pid --fork` runs
the sandbox process as pid 1, so concurrent namespace gates do not share one
stage path. CodeHelm
unmounts and removes those directories before the gate command replaces the
process. If that unmount fails, the directory can remain under `/dev/shm`.
`gate_runner: container` does not create them. If `unshare` is missing or the
namespace cannot be created, the gate fails and the command does not run
outside the sandbox. Hosts that block unprivileged user namespaces, including
a default GitHub-hosted runner, must allow them first. The coding agent and
the model reviewer are not in that namespace. `prepare_cmd` runs outside that
sandbox: no shell, the
operator's environment including credential variables, network access, and a
fixed 900 second timeout. This is not a container image.

Verify the stage directories without starting a gate:

```bash
pytest tests/test_gate_sandbox.py::test_stage_directories_are_unique_for_the_same_name_and_pid
```

The test sets `os.getpid` to 1, calls `_stage("ws")` twice, and requires two
different directories whose parent is `/dev/shm`. It then removes them. It
does not run `unshare`. `/dev/shm` must be writable. The rest of
`tests/test_gate_sandbox.py` covers workspace writes, hidden files outside the
workspace, dropped credential variables, blocked network, and a missing
`unshare`.

An optional container runner is off by default. Set `gate_runner: container`
and `gate_image` to an image already present in the local `podman` store, or
`docker` when `podman` is not on `PATH`. The published gate image is
`ghcr.io/dannyyounes/codehelm`. The publish workflow pushes it only on pushes
to `main` and on version tags, and it prints the image digest. Pin
`gate_image` to that digest. This repository does not embed the digest,
because it exists only after that workflow runs. CodeHelm does not pull the
image, does not substitute a tag, and does not fetch the digest. If the
digest is not already local, the gate fails. The runtime is invoked with
`--rm`, `--network=none`, dropped capabilities, `no-new-privileges`,
`--pull=never`, and the workspace mounted. An image name that starts with a
dash or contains whitespace is rejected. The gate command is passed as
arguments, not through a shell, and credential environment variables are
dropped. If the runtime is missing, cannot be executed, or returns nonzero,
the gate fails and does not run the command on the host or through `unshare`.
The image contains Python, pytest, and bandit for those gate commands. The
coding agent and the model reviewer are not run in that container and are not
installed in that image.

Each `quality-debt` row accepts one unresolved finding for the current attempt
when a human records that finding text and a nonempty reason. More than one
debt can be recorded. It does not
waive tests, security, review, architecture, docs, acceptance, or human
approval. A debt from another attempt does not count, and a debt whose text
does not match a finding is ignored. Current limits: signed approval stays off until `approval_signature` is `required`. `auto_merge` stays off until it is set to `true`, and it still does not merge when human approval was not required or when any check is missing, pending, skipped, or failed. The
container runner is optional, off by default, and fails closed when selected
but unavailable. Publishing `ghcr.io/dannyyounes/codehelm` does not make the gate pull it. A passing test exit code still does not verify an unnamed
acceptance criterion. Architecture review is a second model prompt, not a
separate deterministic checker. Budgets remain estimates, not spend protection.
