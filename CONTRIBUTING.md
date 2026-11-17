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

## Repository hygiene

The root `.gitignore` keeps Python bytecode, pytest and packaging caches, virtualenvs, and local CodeHelm artifacts (`.codehelm/state.db*`, `.codehelm/plan.json`, `.codehelm/config.yaml`, `.codehelm/tmp/`) out of git. Do not force-add `__pycache__/` or `*.pyc` files.

`tests/test_repo_hygiene.py` asserts that no `*.pyc` files are tracked. Verify with:

```bash
pytest tests/test_repo_hygiene.py
git ls-files -- '*.pyc'   # must print nothing
```

Every change must receive independent code review. Do not commit secrets. CodeHelm is licensed under the MIT License.
