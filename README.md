# CodeHelm Quality Contract v1.7 — prototype

This is an **additive prototype**, not yet integrated into CodeHelm's durable scheduler.
Copy `codehelm_quality/` into the repository root, and the test file into `tests/`.
Run `python -m pytest -q tests/test_contract.py` and evaluate an evidence JSON file with
`python -m codehelm_quality path/to/evidence.json`.

The checker is intentionally deterministic and fails closed for missing evidence.
**Important:** the JSON is a *claim*, not proof. The orchestrator must populate it
from trusted test process exit codes, security scan results, Git diffs, and reviewer
identities; it must not accept an agent-written evidence JSON as authoritative.

Pending integration: policy YAML parsing, authenticated human approvals, tamper-resistant
artifact references, automatic evidence collection, quality debt register, and scheduler
state transitions. No GitHub repository changes were made by this package.
