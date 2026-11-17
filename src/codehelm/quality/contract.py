"""Quality contract evaluated from recorded execution evidence.

Agent-written evidence JSON is not accepted. Test, security, review, documentation
and diff inputs come from rows the orchestrator stored after running those checks,
or from git. A passing test exit code does not verify acceptance criteria. A
criterion counts when the latest test report for it on this attempt passed,
or an operator attests it. An earlier failed report does not cancel a later
pass. A passing human attestation still verifies the criterion. Human approval stores the
GitHub login captured when quality-approve ran and a sha256 digest of the
working tree against the resolved merge-base. Committing that same content
leaves the digest unchanged. A content edit makes that approval not count.
When no base resolves, the digest falls back to the uncommitted diff against
HEAD. With approval_signature off, that row is not a cryptographic signature. required
checks an SSH or GPG signature over the current digest. An oversized-change justification
counts only when a passing human row has nonempty detail and a nonempty
identity. Evaluation does not call GitHub again. A quality debt accepts one unresolved finding on the
current attempt when its text matches. It does not waive tests, security,
review, architecture, docs, acceptance, or human approval.
An optional architecture command is a separate command_gate result and does not
replace the model architecture review. An optional prepare_cmd is an argv list
validated like architecture_cmd. It is not a contract gate: execute runs it on
the host before the coding agent. When risk_patterns is omitted or null, a
small built-in content scanner classifies changed files. A configured mapping
replaces that set. An empty mapping turns content scanning off. A match is a
heuristic risk finding, not proof of a secret or a migration. Path globs still
apply. gate_runner selects the namespace sandbox unless it is container.
gate_image is required only then. Any other runner fails closed. In namespace
mode gate_image is ignored.
"""
from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import subprocess

import yaml

from codehelm.quality.diffdigest import diff_digest
from codehelm.quality.signatures import split_signature, verify_approval_signature

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

# Heuristics used when risk_patterns is omitted or null. A nonempty mapping
# replaces this set; {} turns content scanning off. A match is not proof of a
# secret or a migration, and these patterns do not detect every risky edit.
_DEFAULT_RISK_PATTERNS = (
    ("security_sensitive_changes", (
        r"(?m)^-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
        r"\bAKIA[A-Z0-9]{16}\b",
        r"\bghp_[A-Za-z0-9]{36}\b",
        r"\bgithub_pat_[A-Za-z0-9_]{82}\b",
    )),
    ("database_migrations", (
        r"(?i)\bDROP[ \t]+(?:TABLE|COLUMN)\b",
    )),
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
    "architecture_cmd",
    "prepare_cmd",
    "risk_patterns",
    "gate_runner",
    "gate_image",
    "approval_signature",
    "approval_signers",
    "auto_merge",
    "jira_transitions",
    "jira_transition_execute",
    "base_branch",
    "pr_ready",
}
_JIRA_TRANSITION_STATES = frozenset({
    "awaiting_validation", "failed", "blocked",
    "awaiting_human_approval", "ready_for_pr", "completed",
})
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
    architecture_cmd: tuple[str, ...] = ()
    prepare_cmd: tuple[str, ...] = ()
    risk_patterns: tuple[tuple[str, tuple[str, ...]], ...] = ()
    gate_runner: str = "namespace"
    gate_image: str = ""
    approval_signature: str = "off"
    approval_signers: str = ""
    auto_merge: bool = False
    jira_transitions: tuple[tuple[str, str], ...] = ()
    jira_transition_execute: bool = False
    base_branch: str = ""
    pr_ready: bool = False

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
    architecture_check_exit: int | None = None
    architecture_approved: bool = False
    reviewer_approved: bool = False
    reviewer_identity: str = ""
    implementer_identity: str = ""
    docs_verified: bool = False
    risks: list[str] = field(default_factory=list)
    human_approved: bool = False
    approval_mismatch: bool = False
    approval_signature_failed: bool = False
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


def auto_merge_block(policy: QualityPolicy, decision: ContractDecision) -> str | None:
    """Return why a merge is refused, or None when the local contract allows it.

    Auto-merge stays off unless the policy sets it. A passing score is not enough:
    the decision must be ready_for_pr because human approval was required and recorded.
    """
    if not policy.present or not policy.auto_merge:
        return "auto-merge is off"
    if decision.failures or decision.status == "blocked":
        return "quality gates have not all passed"
    if decision.status != "ready_for_pr" or not decision.requires_human:
        return "required human approval is absent"
    return None


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
    if policy.require_architecture and policy.architecture_cmd and evidence.architecture_check_exit != 0:
        failures.append("architecture_check_not_verified")
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
    if evidence.approval_mismatch and not evidence.human_approved:
        failures.append("approval_diff_mismatch")
    if evidence.approval_signature_failed and not evidence.human_approved:
        failures.append("approval_signature_unverified")
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


def _load_architecture_cmd(value, field_name: str = "architecture_cmd") -> tuple[str, ...]:
    """A present command is a nonempty argv. None is handled by the caller.

    prepare_cmd uses the same rules. Pass its name so the error names that key.
    """
    if (
        not isinstance(value, list) or not value
        or not all(isinstance(item, str) and item and "\x00" not in item for item in value)
    ):
        raise ValueError(
            f"{field_name} must be a nonempty list of nonempty strings with no NUL characters")
    return tuple(value)


def _compile_risk_patterns(
    patterns: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compile each regex now. Stored patterns stay readable source strings."""
    for risk, items in patterns:
        for item in items:
            try:
                re.compile(item)
            except re.error as exc:
                raise ValueError(f"risk_patterns.{risk} has an invalid regex: {exc}") from exc
    return patterns


def _load_approval_signature(data: dict) -> tuple[str, str]:
    """off keeps the login-and-digest approval. required demands a signature."""
    mode = data.get("approval_signature", "off")
    if mode is None or mode is False:
        mode = "off"
    if type(mode) is not str or mode not in {"off", "required"}:
        raise ValueError("approval_signature must be off or required")
    signers = data.get("approval_signers", "")
    if signers is None:
        signers = ""
    if type(signers) is not str or "\x00" in signers or any(part == ".." for part in Path(signers).parts):
        raise ValueError("approval_signers must be a path without parent traversal")
    signers = signers.strip()
    if mode == "required" and not signers:
        raise ValueError("approval_signers is required when approval_signature is required")
    return mode, signers


def _signers_directory(policy: QualityPolicy, policy_dir: Path) -> Path | None:
    raw = policy.approval_signers.strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = policy_dir / path
    try:
        return path.resolve()
    except OSError:
        return None


def _load_gate_runner(data: dict) -> tuple[str, str]:
    """namespace is the default. container requires a nonempty local image name."""
    if "gate_runner" not in data or data["gate_runner"] is None:
        runner = "namespace"
    else:
        runner = data["gate_runner"]
    if type(runner) is not str or runner not in {"namespace", "container"}:
        raise ValueError("gate_runner must be namespace or container")
    if runner == "namespace":
        return "namespace", ""
    image = data.get("gate_image")
    if type(image) is not str or not image.strip() or "\x00" in image:
        raise ValueError("gate_image is required when gate_runner is container")
    image = image.strip()
    if image.startswith("-") or any(character.isspace() for character in image):
        raise ValueError("gate_image must be a local image name, not a runtime flag")
    return "container", image


def _load_risk_patterns(value) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Compile a present mapping now. {} turns content scanning off.

    A non-mapping, including an empty list, fails the policy load. An invalid
    regex fails closed. A nonempty mapping replaces the built-in set.
    """
    if not isinstance(value, dict):
        raise ValueError("risk_patterns must be a mapping of risk to regex lists")
    if not value:
        return ()
    converted = []
    for risk, patterns in value.items():
        if not isinstance(risk, str) or not risk.strip():
            raise ValueError("risk_patterns keys must be nonempty strings")
        if not isinstance(patterns, list) or not patterns or not all(
            isinstance(item, str) and item.strip() for item in patterns
        ):
            raise ValueError(f"risk_patterns.{risk} must be a nonempty list of regex strings")
        converted.append((risk, tuple(patterns)))
    return _compile_risk_patterns(tuple(converted))


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
    if "architecture_cmd" not in data or data["architecture_cmd"] is None:
        architecture_cmd = ()
    else:
        architecture_cmd = _load_architecture_cmd(data["architecture_cmd"])
    if "prepare_cmd" not in data or data["prepare_cmd"] is None:
        prepare_cmd = ()
    else:
        prepare_cmd = _load_architecture_cmd(data["prepare_cmd"], "prepare_cmd")
    if "risk_patterns" not in data or data["risk_patterns"] is None:
        risk_patterns = _compile_risk_patterns(_DEFAULT_RISK_PATTERNS)
    else:
        risk_patterns = _load_risk_patterns(data["risk_patterns"])
    gate_runner, gate_image = _load_gate_runner(data)
    approval_signature, approval_signers = _load_approval_signature(data)
    if "auto_merge" not in data or data["auto_merge"] is None:
        auto_merge = False
    else:
        auto_merge = _require_bool(data["auto_merge"], "auto_merge")
    if "pr_ready" not in data or data["pr_ready"] is None:
        pr_ready = False
    else:
        pr_ready = _require_bool(data["pr_ready"], "pr_ready")
    jira_transitions = _load_jira_transitions(data)
    if "jira_transition_execute" not in data or data["jira_transition_execute"] is None:
        jira_transition_execute = False
    else:
        jira_transition_execute = _require_bool(
            data["jira_transition_execute"], "jira_transition_execute")
    if jira_transition_execute and not jira_transitions:
        raise ValueError("jira_transition_execute requires jira_transitions")
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
        architecture_cmd=architecture_cmd,
        prepare_cmd=prepare_cmd,
        risk_patterns=risk_patterns,
        gate_runner=gate_runner,
        gate_image=gate_image,
        approval_signature=approval_signature,
        approval_signers=approval_signers,
        auto_merge=auto_merge,
        jira_transitions=jira_transitions,
        jira_transition_execute=jira_transition_execute,
        base_branch=_load_base_branch(data),
        pr_ready=pr_ready,
    )


def _load_base_branch(data: dict) -> str:
    """Optional integration branch for diff counting and pr-create defaulting.

    An absent key keeps main/master fallbacks. An explicit null or empty value
    fails closed. A present value must pass ``git check-ref-format refs/heads/<name>``
    (not ``--branch``, which expands checkout shorthands). Names equal to
    ``HEAD`` or ``@``, or containing ``@{``, are refused even when check-ref-format
    would accept them.
    """
    if "base_branch" not in data:
        return ""
    value = data["base_branch"]
    if value is None:
        raise ValueError("base_branch must be a nonempty string")
    if type(value) is not str:
        raise ValueError("base_branch must be a string")
    value = value.strip()
    if not value or "\x00" in value:
        raise ValueError("base_branch must be a nonempty string")
    # refs/heads/<name> accepts some names --branch would refuse (leading '-',
    # HEAD). Reject those literals here; do not use --branch (expands @{-1}).
    if value in {"HEAD", "@"} or value.startswith("-") or "@{" in value:
        raise ValueError("base_branch must be a valid branch name")
    check = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{value}"],
        capture_output=True, text=True)
    if check.returncode != 0:
        raise ValueError("base_branch must be a valid branch name")
    return value


def default_pr_base(policy: QualityPolicy) -> str:
    """Target branch for pr-create when --base is omitted."""
    if policy.present and policy.base_branch:
        return policy.base_branch
    return "main"


def _load_jira_transitions(data: dict) -> tuple[tuple[str, str], ...]:
    """Off unless the operator maps scheduler states to transition ids.

    False and null stay off. A boolean true or an empty mapping is refused
    because this loader does not invent transition ids.
    """
    if "jira_transitions" not in data or data["jira_transitions"] is None or data["jira_transitions"] is False:
        return ()
    raw = data["jira_transitions"]
    if raw is True or not isinstance(raw, dict) or not raw:
        raise ValueError(
            "jira_transitions must be false or a nonempty mapping of state to transition id")
    converted = []
    for state, transition_id in raw.items():
        if state not in _JIRA_TRANSITION_STATES:
            raise ValueError(f"jira_transitions state {state!r} is not a scheduler outcome")
        if not isinstance(transition_id, str) or not transition_id or any(
            not (character.isascii() and (character.isalnum() or character == "-"))
            for character in transition_id
        ):
            raise ValueError(f"jira_transitions.{state} must be a transition id")
        converted.append((state, transition_id))
    return tuple(converted)


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


def resolve_diff_base(workspace: Path, base_branch: str = "") -> str | None:
    """First existing ref for change counting.

    Order: policy base_branch, origin/<base_branch>, main, master, origin/main,
    origin/master. Lookups use refs/heads/<name> and refs/remotes/origin/<name>
    so a tag with the same name cannot win. An empty base_branch skips the
    policy entries.
    """
    candidates: list[str] = []
    name = (base_branch or "").strip()
    if name:
        candidates.append(f"refs/heads/{name}")
        candidates.append(f"refs/remotes/origin/{name}")
    candidates.extend([
        "refs/heads/main",
        "refs/heads/master",
        "refs/remotes/origin/main",
        "refs/remotes/origin/master",
    ])
    seen: set[str] = set()
    for ref in candidates:
        if ref in seen:
            continue
        seen.add(ref)
        if _git(workspace, "rev-parse", "--verify", "--quiet", ref).returncode:
            continue
        return ref
    return None


def collect_diff(workspace: Path | None, base_branch: str = "") -> tuple[list[str], int]:
    """Changed files and line counts from git. Missing git data fails closed (-1).

    Committed changes are ``merge-base(base, HEAD)..HEAD``. Working-tree and
    untracked paths are added on top. ``base_branch`` selects the policy base;
    resolution follows ``resolve_diff_base``.
    """
    if workspace is None or not workspace.is_dir():
        return [], -1
    head = _git(workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode:
        return [], -1
    files: dict[str, int] = {}
    base = resolve_diff_base(workspace, base_branch)
    if base is not None:
        merge = _git(workspace, "merge-base", base, "HEAD")
        if merge.returncode:
            return [], -1
        merge_base = merge.stdout.strip()
        if merge_base:
            head_sha = _git(workspace, "rev-parse", "HEAD")
            if head_sha.returncode:
                return [], -1
            if head_sha.stdout.strip() != merge_base:
                committed = _git(workspace, "diff", "--numstat", f"{merge_base}..HEAD")
                if committed.returncode:
                    return [], -1
                _add_numstat(files, committed.stdout)
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


def _changed_file_text(workspace: Path, relative: str) -> str | None:
    """Read one changed path that resolves inside the workspace."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None
    raw = Path(relative)
    if raw.is_absolute():
        return None
    root = workspace.resolve()
    try:
        resolved = (root / raw).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    try:
        return resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _risks_for(paths: list[str], policy: QualityPolicy, workspace: Path | None = None) -> list[str]:
    found = []
    for risk, patterns in policy.risk_paths:
        if any(glob_match(pattern, path) for path in paths for pattern in patterns):
            found.append(risk)
    if workspace is not None and workspace.is_dir() and policy.risk_patterns and paths:
        compiled = [
            (risk, tuple(re.compile(pattern) for pattern in patterns))
            for risk, patterns in policy.risk_patterns
        ]
        for path in paths:
            text = _changed_file_text(workspace, path)
            if text is None:
                continue
            for risk, patterns in compiled:
                if any(pattern.search(text) is not None for pattern in patterns):
                    found.append(risk)
    return sorted(set(found))


def _latest(rows, gate: str):
    """Latest row for this gate on the attempt. An earlier row does not count."""
    chosen = None
    for row in rows:
        if row.gate == gate:
            chosen = row
    return chosen


def _verified_acceptance(rows) -> list[str]:
    """Criteria verified on this attempt.

    The latest test_report row for each criterion is the one that counts.
    A passing human attestation still verifies that criterion.
    """
    human: set[str] = set()
    reports: dict[str, bool] = {}
    for row in rows:
        if row.gate != "acceptance":
            continue
        name = row.detail.strip()
        if not name:
            continue
        if row.source == "human" and row.passed:
            human.add(name)
        elif row.source == "test_report":
            reports[name] = bool(row.passed)
    return sorted(human | {name for name, passed in reports.items() if passed})


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
    verified = _verified_acceptance(rows)
    accepted_debts = {
        row.detail.strip() for row in rows
        if row.gate == "quality_debt" and row.source == "human" and row.passed
        and row.detail.strip() and row.identity.strip()
    }
    findings = [
        row.detail for row in rows
        if row.gate == "finding" and row.source in {"independent_review", "human"}
        and not row.passed and row.detail.strip()
        and row.detail.strip() not in accepted_debts
    ]
    justification = "\n".join(
        row.detail.strip() for row in rows
        if row.gate == "oversized_justification" and row.source == "human" and row.passed
        and row.detail.strip() and row.identity.strip()
    )
    current = diff_digest(workspace, policy.base_branch) if workspace else ""
    approvals = [
        row for row in rows
        if row.gate == "human_approval" and row.source == "human" and row.passed
    ]
    signers = _signers_directory(policy, policy_path_for(store.database).parent)
    human_approved = False
    signature_failed = False
    for row in approvals:
        digest = row.detail.strip()
        identity = row.identity.strip()
        if not current or not identity or digest != current:
            continue
        stored = row.signature or ""
        if split_signature(stored) is None:
            if policy.approval_signature == "required":
                signature_failed = True
            else:
                human_approved = True
            continue
        if verify_approval_signature(stored, current, identity, signers):
            human_approved = True
        else:
            signature_failed = True
    approval_mismatch = bool(current) and any(
        row.detail.strip() and row.detail.strip() != current for row in approvals
    )
    approval_signature_failed = signature_failed and not human_approved
    recorded_risks = [
        row.detail.strip() for row in rows
        if row.gate == "risk" and row.source == "human" and row.passed and row.detail.strip()
    ]
    tests = _latest(rows, "tests")
    security = _latest(rows, "security")
    architecture_check = _latest(rows, "architecture_check")
    review = _latest(rows, "review")
    architecture = _latest(rows, "architecture")
    docs = _latest(rows, "docs")
    changed_files, changed_lines = collect_diff(workspace, policy.base_branch)
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
        architecture_check_exit=exit_of(architecture_check),
        architecture_approved=architecture_approved,
        reviewer_approved=reviewer_approved,
        reviewer_identity=reviewer_identity,
        implementer_identity=implementer_identity,
        docs_verified=bool(
            docs and (
                (docs.source == "documentation_agent" and docs.passed)
                or (
                    docs.source == "human" and docs.passed
                    and bool(docs.detail.strip()) and bool(docs.identity.strip())
                )
            )
        ),
        risks=sorted(set(recorded_risks) | set(_risks_for(changed_files, policy, workspace))),
        human_approved=human_approved,
        approval_mismatch=approval_mismatch,
        approval_signature_failed=approval_signature_failed,
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
