"""Quality contract evaluated from recorded execution evidence.

Agent-written evidence JSON is not accepted. Test, security, review, documentation
and diff inputs come from rows the orchestrator stored after running those checks,
or from git. A passing test exit code does not verify acceptance criteria. A
criterion counts only when the test process the orchestrator ran emits a
CODEHELM_ACCEPTANCE line, or an operator attests it. Human approval and
oversized-change justification stay operator attestations.
"""
from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import subprocess

import yaml

HIGH_RISK = frozenset({
    "authentication_changes",
    "database_migrations",
    "security_sensitive_changes",
    "production_infrastructure",
})

# ** matches across directories; * does not match '/'.
_DEFAULT_RISK_PATHS = (
    ("authentication_changes", ("auth/**", "**/auth/**", "**/authentication/**")),
    ("database_migrations", ("migrations/**", "**/migrations/**", "**/alembic/**")),
    ("security_sensitive_changes", ("security/**", "**/security/**")),
    ("production_infrastructure", ("deploy/**", "**/deploy/**", "**/infra/**",
                                   "**/.github/workflows/**")),
)

POLICY_SCHEMA_VERSION = 1
_POLICY_FIELDS = {
    "max_changed_lines",
    "require_acceptance_criteria",
    "require_tests",
    "require_security",
    "require_architecture",
    "require_review",
    "require_docs",
    "max_retries",
    "human_approval_risks",
    "risk_paths",
}
_ATTEMPT = re.compile(r"-A([1-9][0-9]*)$")
_ACCEPTANCE_LINE = re.compile(r"^CODEHELM_ACCEPTANCE\t([^\t]+)\t(passed|failed)$")
_ACCEPTANCE_REPORT_LIMIT = 65536


def parse_acceptance_report(text: str) -> dict[str, bool]:
    """Parse CODEHELM_ACCEPTANCE lines. A failed line wins over a passed one."""
    found: dict[str, bool] = {}
    if not text:
        return found
    for raw in text.splitlines():
        match = _ACCEPTANCE_LINE.match(raw.strip())
        if not match:
            continue
        criterion = match.group(1).strip()
        if not criterion:
            continue
        ok = match.group(2) == "passed"
        if criterion in found:
            found[criterion] = found[criterion] and ok
        else:
            found[criterion] = ok
    return found


def read_acceptance_report(workspace: Path) -> str | None:
    """Read the workspace report written by a test process. Oversized files are ignored."""
    path = Path(workspace) / ".codehelm" / "acceptance-report.txt"
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > _ACCEPTANCE_REPORT_LIMIT:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def task_attempt(task_id: str) -> int:
    """Attempt encoded in scheduler task ids (PREFIX-S1-A2). Otherwise 1."""
    match = _ATTEMPT.search(task_id)
    return int(match.group(1)) if match else 1


def policy_path_for(database: Path) -> Path:
    return Path(database).parent / "quality.yaml"


def glob_match(pattern: str, path: str) -> bool:
    escaped = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(escaped, path) is not None


@dataclass(frozen=True)
class QualityPolicy:
    """Versioned policy. present=False means the file was absent (legacy lifecycle)."""
    present: bool = True
    schema_version: int | None = POLICY_SCHEMA_VERSION
    max_changed_lines: int = 400
    require_acceptance_criteria: bool = True
    require_tests: bool = True
    require_security: bool = True
    require_architecture: bool = True
    require_review: bool = True
    require_docs: bool = True
    max_retries: int = 2
    human_approval_risks: frozenset[str] = field(default_factory=lambda: frozenset(HIGH_RISK))
    risk_paths: tuple[tuple[str, tuple[str, ...]], ...] = _DEFAULT_RISK_PATHS

    @staticmethod
    def absent() -> "QualityPolicy":
        return QualityPolicy(present=False, schema_version=None)


@dataclass
class QualityEvidence:
    story_id: str
    acceptance_criteria: list[str]
    verified_criteria: list[str]
    changed_files: list[str]
    changed_lines: int
    test_exit_code: int | None = None
    security_exit_code: int | None = None
    architecture_approved: bool = False
    reviewer_approved: bool = False
    reviewer_identity: str = ""
    implementer_identity: str = ""
    docs_verified: bool = False
    risks: list[str] = field(default_factory=list)
    human_approved: bool = False
    oversized_justification: str = ""
    unresolved_findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ContractDecision:
    status: str
    failures: tuple[str, ...]
    requires_human: bool

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "failures": list(self.failures),
            "requires_human": self.requires_human,
        }


def decision_error(decision: ContractDecision) -> str:
    if decision.failures:
        return ",".join(decision.failures)[:2000]
    if decision.status == "awaiting_human_approval":
        return "human approval required"
    return ""


def evaluate(policy: QualityPolicy, evidence: QualityEvidence) -> ContractDecision:
    """Fail closed. Human approval is required only after every other check passes."""
    failures: list[str] = []
    if not evidence.story_id.strip():
        failures.append("missing_story_id")
    if policy.require_acceptance_criteria and (
        not evidence.acceptance_criteria
        or not set(evidence.acceptance_criteria) <= set(evidence.verified_criteria)
    ):
        failures.append("unverified_acceptance_criteria")
    if not evidence.changed_files or evidence.changed_lines < 0:
        failures.append("missing_change_evidence")
    if evidence.changed_lines > policy.max_changed_lines and not evidence.oversized_justification.strip():
        failures.append("oversized_change_requires_justification")
    if policy.require_tests and evidence.test_exit_code != 0:
        failures.append("tests_not_verified")
    if policy.require_security and evidence.security_exit_code != 0:
        failures.append("security_scan_not_verified")
    if policy.require_architecture and not evidence.architecture_approved:
        failures.append("architecture_review_missing")
    if policy.require_review and (
        not evidence.reviewer_approved
        or not evidence.reviewer_identity
        or not evidence.implementer_identity
        or evidence.reviewer_identity == evidence.implementer_identity
    ):
        failures.append("independent_review_missing")
    if policy.require_docs and not evidence.docs_verified:
        failures.append("documentation_not_verified")
    if evidence.unresolved_findings:
        failures.append("unresolved_findings")
    needs_human = bool(set(evidence.risks) & policy.human_approval_risks)
    if failures:
        status = "blocked"
    elif needs_human and not evidence.human_approved:
        status = "awaiting_human_approval"
    else:
        status = "ready_for_pr"
    return ContractDecision(status, tuple(failures), needs_human)


def _require_bool(value, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _require_int(value, field_name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{field_name} must be an integer from {low} to {high}")
    return value


def load_policy(path: Path) -> QualityPolicy:
    """Load `.codehelm/quality.yaml`. A missing file leaves the legacy lifecycle in place."""
    path = Path(path)
    if not path.exists():
        return QualityPolicy.absent()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid quality policy: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Quality policy must be a mapping")
    unknown = set(data) - _POLICY_FIELDS - {"schema_version"}
    if unknown:
        raise ValueError(f"Unknown quality policy fields: {sorted(unknown)}")
    version = data.get("schema_version")
    if version != POLICY_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported quality policy schema_version {version!r}; "
            f"expected {POLICY_SCHEMA_VERSION}"
        )
    risks = data.get("human_approval_risks", sorted(HIGH_RISK))
    if not isinstance(risks, list) or not risks or not all(isinstance(item, str) and item.strip() for item in risks):
        raise ValueError("human_approval_risks must be a nonempty list of strings")
    if len(set(risks)) != len(risks):
        raise ValueError("human_approval_risks contains duplicates")
    raw_paths = data.get("risk_paths")
    if raw_paths is None:
        risk_paths = _DEFAULT_RISK_PATHS
    else:
        if not isinstance(raw_paths, dict) or not raw_paths:
            raise ValueError("risk_paths must be a nonempty mapping of risk to glob lists")
        converted = []
        for risk, patterns in raw_paths.items():
            if not isinstance(risk, str) or not risk.strip():
                raise ValueError("risk_paths keys must be nonempty strings")
            if not isinstance(patterns, list) or not patterns or not all(
                isinstance(item, str) and item.strip() for item in patterns
            ):
                raise ValueError(f"risk_paths.{risk} must be a nonempty list of globs")
            converted.append((risk, tuple(patterns)))
        risk_paths = tuple(converted)
    return QualityPolicy(
        present=True,
        schema_version=POLICY_SCHEMA_VERSION,
        max_changed_lines=_require_int(data.get("max_changed_lines", 400), "max_changed_lines", 0, 100000),
        require_acceptance_criteria=_require_bool(
            data.get("require_acceptance_criteria", True), "require_acceptance_criteria"),
        require_tests=_require_bool(data.get("require_tests", True), "require_tests"),
        require_security=_require_bool(data.get("require_security", True), "require_security"),
        require_architecture=_require_bool(data.get("require_architecture", True), "require_architecture"),
        require_review=_require_bool(data.get("require_review", True), "require_review"),
        require_docs=_require_bool(data.get("require_docs", True), "require_docs"),
        max_retries=_require_int(data.get("max_retries", 2), "max_retries", 1, 5),
        human_approval_risks=frozenset(item.strip() for item in risks),
        risk_paths=risk_paths,
    )


def _git(workspace: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(workspace), *args], capture_output=True, text=True)


def _add_numstat(files: dict[str, int], text: str) -> None:
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[-1]
        if " => " in path:
            path = path.split(" => ", 1)[1].strip()
        try:
            count = (0 if added == "-" else int(added)) + (0 if deleted == "-" else int(deleted))
        except ValueError:
            count = 0
        files[path] = files.get(path, 0) + count


def collect_diff(workspace: Path | None) -> tuple[list[str], int]:
    """Changed files and line counts from git. Missing git data fails closed (-1)."""
    if workspace is None or not workspace.is_dir():
        return [], -1
    head = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode:
        return [], -1
    files: dict[str, int] = {}
    branch = head.stdout.strip()
    if branch not in {"main", "master", "HEAD"}:
        for ref in ("main", "master"):
            if _git(workspace, "rev-parse", "--verify", "--quiet", ref).returncode:
                continue
            if _git(workspace, "merge-base", "--is-ancestor", ref, "HEAD").returncode:
                continue
            if _git(workspace, "rev-parse", ref).stdout.strip() == _git(workspace, "rev-parse", "HEAD").stdout.strip():
                break
            committed = _git(workspace, "diff", "--numstat", f"{ref}...HEAD")
            if committed.returncode:
                return [], -1
            _add_numstat(files, committed.stdout)
            break
    working = _git(workspace, "diff", "--numstat", "HEAD")
    if working.returncode:
        return [], -1
    _add_numstat(files, working.stdout)
    status = _git(workspace, "status", "--porcelain", "-uall")
    if status.returncode:
        return [], -1
    for line in status.stdout.splitlines():
        if not line.startswith("??"):
            continue
        path = line[3:].strip().strip('"')
        file_path = workspace / path
        if not file_path.is_file():
            files.setdefault(path, 0)
            continue
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], -1
        files[path] = files.get(path, 0) + text.count("\n") + (0 if text.endswith("\n") or text == "" else 1)
    names = sorted(files)
    return names, sum(files.values())


def _risks_for(paths: list[str], policy: QualityPolicy) -> list[str]:
    found = []
    for risk, patterns in policy.risk_paths:
        if any(glob_match(pattern, path) for path in paths for pattern in patterns):
            found.append(risk)
    return sorted(set(found))


def _latest(rows, gate: str):
    chosen = None
    for row in rows:
        if row.gate == gate:
            chosen = row
    return chosen


def assemble_evidence(store, task_id: str, attempt: int, policy: QualityPolicy,
                      workspace: Path | None, acceptance_criteria, story_id: str,
                      implementer: str) -> QualityEvidence:
    """Build evidence from the state layer and git. Ignores other attempts."""
    if acceptance_criteria is not None:
        criteria = [item.strip() for item in acceptance_criteria]
    else:
        criteria = [
            row.detail for row in store.evidence_for(task_id, attempt)
            if row.gate == "acceptance_planned" and row.source == "plan" and row.detail.strip()
        ]
    rows = store.evidence_for(task_id, attempt)
    human_verified = {
        row.detail.strip() for row in rows
        if row.gate == "acceptance" and row.source == "human" and row.passed and row.detail.strip()
    }
    report_passed = {
        row.detail.strip() for row in rows
        if row.gate == "acceptance" and row.source == "test_report" and row.passed and row.detail.strip()
    }
    report_failed = {
        row.detail.strip() for row in rows
        if row.gate == "acceptance" and row.source == "test_report" and not row.passed and row.detail.strip()
    }
    verified = sorted(human_verified | (report_passed - report_failed))
    findings = [
        row.detail for row in rows
        if row.gate == "finding" and row.source in {"independent_review", "human"}
        and not row.passed and row.detail.strip()
    ]
    justification = "\n".join(
        row.detail.strip() for row in rows
        if row.gate == "oversized_justification" and row.source == "human" and row.detail.strip()
    )
    human_approved = any(
        row.gate == "human_approval" and row.source == "human" and row.passed and row.identity.strip()
        for row in rows
    )
    recorded_risks = [
        row.detail.strip() for row in rows
        if row.gate == "risk" and row.source == "human" and row.passed and row.detail.strip()
    ]
    tests = _latest(rows, "tests")
    security = _latest(rows, "security")
    review = _latest(rows, "review")
    architecture = _latest(rows, "architecture")
    docs = _latest(rows, "docs")
    changed_files, changed_lines = collect_diff(workspace)
    implementer_identity = (implementer or "").strip()
    reviewer_identity = ""
    reviewer_approved = False
    if review and review.source == "independent_review":
        reviewer_identity = review.identity.strip()
        reviewer_approved = bool(review.passed and reviewer_identity and reviewer_identity != implementer_identity)
    architecture_approved = bool(
        architecture and architecture.source == "independent_review" and architecture.passed
        and architecture.identity.strip() and architecture.identity.strip() != implementer_identity
    )
    def exit_of(row) -> int | None:
        if row is None or row.source != "command_gate":
            return None
        return row.exit_code
    return QualityEvidence(
        story_id=story_id,
        acceptance_criteria=criteria,
        verified_criteria=verified,
        changed_files=changed_files,
        changed_lines=changed_lines,
        test_exit_code=exit_of(tests),
        security_exit_code=exit_of(security),
        architecture_approved=architecture_approved,
        reviewer_approved=reviewer_approved,
        reviewer_identity=reviewer_identity,
        implementer_identity=implementer_identity,
        docs_verified=bool(docs and docs.source == "documentation_agent" and docs.passed),
        risks=sorted(set(recorded_risks) | set(_risks_for(changed_files, policy))),
        human_approved=human_approved,
        oversized_justification=justification,
        unresolved_findings=findings,
    )


def assess(store, task_id: str, *, attempt: int, policy: QualityPolicy,
           workspace: Path | None, acceptance_criteria, story_id: str,
           implementer: str, persist: bool = True) -> ContractDecision:
    """Evaluate one attempt. persist=False previews without writing an evaluation row."""
    if not policy.present:
        raise ValueError("Quality policy is not active")
    if acceptance_criteria is not None and persist:
        store.replace_plan_criteria(task_id, attempt, list(acceptance_criteria))
    evidence = assemble_evidence(
        store, task_id, attempt, policy, workspace, acceptance_criteria, story_id, implementer)
    decision = evaluate(policy, evidence)
    if persist:
        store.record_evaluation(
            task_id, attempt, story_id, policy.schema_version, decision.status,
            json.dumps(list(decision.failures)), decision.requires_human)
    return decision
