# Contributing

CodeHelm is an early scaffold. Open an issue before large changes, keep PRs focused, and add tests for new behavior.

Run `pytest` after installing the development dependencies.

Lifecycle CLI timeout options (`validate` / `document` / `validate-after-docs` `--timeout`, and `schedule-durable` / `resume` `--agent-timeout` / `--gate-timeout`) are covered by `tests/test_lifecycle_timeouts.py`. Verify with `pytest tests/test_lifecycle_timeouts.py` and the matching `codehelm <command> --help` output.

## Repository hygiene

The root `.gitignore` keeps Python bytecode, pytest and packaging caches, virtualenvs, and local CodeHelm artifacts (`.codehelm/state.db*`, `.codehelm/plan.json`, `.codehelm/config.yaml`, `.codehelm/tmp/`) out of git. Do not force-add `__pycache__/` or `*.pyc` files.

`tests/test_repo_hygiene.py` asserts that no `*.pyc` files are tracked. Verify with:

```bash
pytest tests/test_repo_hygiene.py
git ls-files -- '*.pyc'   # must print nothing
```

Every change must receive independent code review. Do not commit secrets. CodeHelm is licensed under the MIT License.
