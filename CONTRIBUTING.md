# Contributing

CodeHelm is an early scaffold. Open an issue before large changes, keep PRs focused, and add tests for new behavior.

Run `pytest` after installing the development dependencies.

Lifecycle CLI timeout options (`validate` / `document` / `validate-after-docs` `--timeout`, and `schedule-durable` / `resume` `--agent-timeout` / `--gate-timeout`) are covered by `tests/test_lifecycle_timeouts.py`. Verify with `pytest tests/test_lifecycle_timeouts.py` and the matching `codehelm <command> --help` output.

Reviewer story context (plan title, description, and acceptance criteria prepended to `independent_review`) is covered by `tests/test_quality.py` and `tests/test_durable_quality.py`. Verify with:

```bash
pytest tests/test_quality.py::test_review_prompt_unchanged_without_story_context \
  tests/test_quality.py::test_review_prompt_includes_story_context \
  tests/test_quality.py::test_story_context_is_truncated \
  tests/test_quality.py::test_validate_passes_story_context \
  tests/test_quality.py::test_validate_cli_passes_plan_story \
  tests/test_durable_quality.py::test_carry_story_sends_plan_story_to_reviewer
```

Diff-bound human approval is covered by `tests/test_diff_bound_approval.py`: the digest stays stable when the same content is committed against a resolved merge-base, a content edit or a commit with no resolved base invalidates it, and a dirty submodule, an untracked nested repository, or a missing merge-base yields an empty digest that `quality-approve` will not record. Verify with `pytest tests/test_diff_bound_approval.py`.

Ready versus draft pull requests are covered by `tests/test_github_pr.py`. `pr-create` reads `.codehelm/quality.yaml` from the current working directory. The pull request stays a draft unless `--ready` is passed or `pr_ready` is boolean `true`. `--draft` wins over both. A missing file, an omitted key, null, and `false` stay draft. A non-boolean fails policy load with `pr_ready must be a boolean`. A dry run prints `create a draft PR.` or `create a ready PR.` and does not push. `--execute` passes that choice as `draft` to `publish_pr`. `publish_pr` defaults `draft=True`, adds `gh pr create --draft` only when the flag is true, and does not merge. `LifecycleEngine.create_pull_request` also defaults `draft=True` and does not read `pr_ready`. Verify with:

```bash
pytest tests/test_github_pr.py
codehelm pr-create --help
```
Documentation-agent story context, documentation-path checks, and an already-included documentation diff are covered by `tests/test_documentation.py`. Verify with `pytest tests/test_documentation.py`.

Resuming a quality-blocked story on the same attempt is covered by `tests/test_blocked_resume.py`. `codehelm resume --execute` re-claims dispatch state `interrupted` or `blocked`, does not launch the coding agent, and does not allocate a new attempt. A stage runs again only when the latest row in that visit did not pass. A later pass supersedes an earlier failure, including when another gate's row is in between. A passing human docs exception skips the documentation agent; post-docs visits still run when their passing rows are from before that docs evidence. Scoring uses the latest row per gate and the latest acceptance `test_report` per criterion. A passing human attestation still verifies a criterion. Task status `queued`, `running`, `failed`, or `pr_created` is refused, and so is a `failed` dispatch. Verify with:

```bash
pytest tests/test_blocked_resume.py
codehelm resume --help
```

Namespace gate stage directories are covered by `tests/test_gate_sandbox.py`. Each bind is a new directory under `/dev/shm` (`codehelm-ws-` for the workspace; `codehelm-keep0-`, `codehelm-keep1-`, and so on for an interpreter directory that would be covered). The name does not include the process id. `unshare --pid --fork` runs the sandbox as pid 1, so concurrent namespace gates do not share a stage path. CodeHelm unmounts and removes the directories before the gate command replaces the process. If that unmount fails, the directory can remain under `/dev/shm`. The container runner does not create them. Verify with:

```bash
pytest tests/test_gate_sandbox.py::test_stage_directories_are_unique_for_the_same_name_and_pid
```

The test sets `os.getpid` to 1, calls `_stage("ws")` twice, and requires two different directories whose parent is `/dev/shm`. It then removes them. It does not run `unshare`. `/dev/shm` must be writable.

## Repository hygiene

The root `.gitignore` keeps Python bytecode, pytest and packaging caches, virtualenvs, and local CodeHelm artifacts (`.codehelm/state.db*`, `.codehelm/plan.json`, `.codehelm/config.yaml`, `.codehelm/tmp/`) out of git. Do not force-add `__pycache__/` or `*.pyc` files.

`tests/test_repo_hygiene.py` asserts that no `*.pyc` files are tracked. Verify with:

```bash
pytest tests/test_repo_hygiene.py
git ls-files -- '*.pyc'   # must print nothing
```

Every change must receive independent code review. Do not commit secrets. CodeHelm is licensed under the MIT License.
