"""Base-branch-aware change counting and pr-create default base."""
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codehelm.cli.main import app
from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.quality.contract import (
    ContractDecision, QualityPolicy, collect_diff, default_pr_base, load_policy,
    resolve_diff_base,
)


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def make_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir()
    git(path, "init", "-b", branch)
    git(path, "config", "user.email", "test@example.org")
    git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", "initial")
    return path


def rev_parse(repo: Path, ref: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", ref],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_base_branch_absent_keeps_default(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\nmax_changed_lines: 50\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.base_branch == ""
    assert default_pr_base(policy) == "main"
    assert default_pr_base(QualityPolicy.absent()) == "main"


def test_base_branch_accepted_and_defaults_pr_base(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\nbase_branch: staging\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.base_branch == "staging"
    assert default_pr_base(policy) == "staging"


@pytest.mark.parametrize("raw,match", [
    ("base_branch: null\n", "nonempty"),
    ("base_branch: ''\n", "nonempty"),
    ("base_branch: '  '\n", "nonempty"),
    ("base_branch: 1\n", "string"),
    ("base_branch: true\n", "string"),
    ("base_branch: '-bad'\n", "valid"),
    ("base_branch: 'has space'\n", "valid"),
    ("base_branch: 'a..b'\n", "valid"),
    ("base_branch: HEAD\n", "valid"),
    ("base_branch: '@'\n", "valid"),
    ("base_branch: 'staging.'\n", "valid"),
    ("base_branch: '@{-1}'\n", "valid"),
])
def test_base_branch_validation_fails_closed(tmp_path, raw, match):
    path = tmp_path / "quality.yaml"
    path.write_text(f"schema_version: 1\n{raw}", encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_policy(path)


def test_base_branch_rejects_at_brace_even_with_checkout_history(tmp_path):
    """``git check-ref-format --branch @{-1}`` expands; literal refs/heads must not."""
    repo = make_repo(tmp_path / "repo")
    git(repo, "checkout", "-b", "other")
    git(repo, "checkout", "main")
    expanded = subprocess.run(
        ["git", "-C", str(repo), "check-ref-format", "--branch", "@{-1}"],
        capture_output=True, text=True)
    assert expanded.returncode == 0 and expanded.stdout.strip() == "other"
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\nbase_branch: '@{-1}'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="valid"):
        load_policy(path)


def test_resolve_diff_base_prefers_policy_over_main(tmp_path):
    repo = make_repo(tmp_path / "repo", "main")
    git(repo, "checkout", "-b", "staging")
    (repo / "staging.txt").write_text("staging\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "staging tip")
    git(repo, "checkout", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature tip")
    assert resolve_diff_base(repo, "staging") == "refs/heads/staging"
    assert resolve_diff_base(repo, "") == "refs/heads/main"


def test_resolve_diff_base_uses_origin_policy_then_fallbacks(tmp_path):
    repo = make_repo(tmp_path / "repo", "main")
    git(repo, "checkout", "-b", "staging")
    (repo / "staging.txt").write_text("staging\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "staging tip")
    staging_sha = rev_parse(repo, "HEAD")
    git(repo, "checkout", "-b", "feature")
    git(repo, "branch", "-D", "staging")
    git(repo, "update-ref", "refs/remotes/origin/staging", staging_sha)
    assert resolve_diff_base(repo, "staging") == "refs/remotes/origin/staging"
    git(repo, "update-ref", "-d", "refs/remotes/origin/staging")
    assert resolve_diff_base(repo, "staging") == "refs/heads/main"
    main_sha = rev_parse(repo, "main")
    git(repo, "branch", "-D", "main")
    git(repo, "update-ref", "refs/remotes/origin/main", main_sha)
    assert resolve_diff_base(repo, "staging") == "refs/remotes/origin/main"
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    git(repo, "update-ref", "refs/remotes/origin/master", main_sha)
    assert resolve_diff_base(repo, "") == "refs/remotes/origin/master"


def test_resolve_diff_base_ignores_tag_collision(tmp_path):
    """A tag named like the branch must not override refs/heads/<name>."""
    repo = make_repo(tmp_path / "repo", "main")
    git(repo, "checkout", "-b", "staging")
    (repo / "staging.txt").write_text("staging\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "staging tip")
    staging_sha = rev_parse(repo, "HEAD")
    git(repo, "checkout", "main")
    (repo / "main-only.txt").write_text("main\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "main tip")
    git(repo, "tag", "staging")
    git(repo, "branch", "-D", "staging")
    # Tag exists; no local or remote staging branch — fall through to main.
    assert resolve_diff_base(repo, "staging") == "refs/heads/main"
    git(repo, "update-ref", "refs/heads/staging", staging_sha)
    assert resolve_diff_base(repo, "staging") == "refs/heads/staging"
    assert rev_parse(repo, "refs/heads/staging") == staging_sha
    assert rev_parse(repo, "refs/tags/staging") != staging_sha


def test_collect_diff_counts_committed_changes_against_non_main_base(tmp_path):
    """After commit on a staging-based branch, change evidence must remain."""
    repo = make_repo(tmp_path / "repo", "staging")
    git(repo, "checkout", "-b", "codehelm/DEV-1")
    (repo / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "feature")
    # No local main/master: pre-S2-style lookup finds nothing after commit.
    absent_files, absent_lines = collect_diff(repo, "")
    assert absent_files == [] and absent_lines == 0
    files, lines = collect_diff(repo, "staging")
    assert "feature.py" in files
    assert lines >= 1


def test_collect_diff_without_base_branch_still_counts_against_main(tmp_path):
    repo = make_repo(tmp_path / "repo", "main")
    git(repo, "checkout", "-b", "feature")
    (repo / "change.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "change")
    files, lines = collect_diff(repo, "")
    assert files == ["change.txt"]
    assert lines == 1
    (repo / "change.txt").write_text("one\ntwo\n", encoding="utf-8")
    (repo / "extra.txt").write_text("new\n", encoding="utf-8")
    files, lines = collect_diff(repo, "")
    assert files == ["change.txt", "extra.txt"]
    assert lines >= 2


def test_pr_create_defaults_to_policy_base_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text(
        "schema_version: 1\nbase_branch: staging\n", encoding="utf-8")
    engine = LifecycleEngine(repo, state / "state.db")
    engine.store.transition("DEV-1", "ready_for_pr", provider="claude", workspace=str(repo))
    ready = ContractDecision("ready_for_pr", (), False)
    with patch("codehelm.orchestrator.lifecycle.assess", return_value=ready), \
         patch("codehelm.orchestrator.lifecycle.publish_pr",
               return_value=type("PR", (), {"url": "https://github.com/o/r/pull/1"})()) as publish:
        result = CliRunner().invoke(
            app, ["pr-create", "DEV-1", "--title", "T", "--body", "B", "--repo", str(repo),
                  "--execute"])
    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["base"] == "staging"


def test_pr_create_defaults_to_main_without_policy_base(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    engine = LifecycleEngine(repo, state / "state.db")
    engine.store.transition("DEV-1", "ready_for_pr", provider="claude", workspace=str(repo))
    ready = ContractDecision("ready_for_pr", (), False)
    with patch("codehelm.orchestrator.lifecycle.assess", return_value=ready), \
         patch("codehelm.orchestrator.lifecycle.publish_pr",
               return_value=type("PR", (), {"url": "https://github.com/o/r/pull/2"})()) as publish:
        result = CliRunner().invoke(
            app, ["pr-create", "DEV-1", "--title", "T", "--body", "B", "--repo", str(repo),
                  "--execute"])
    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["base"] == "main"


def test_pr_create_explicit_base_overrides_policy(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = make_repo(tmp_path / "repo")
    state = tmp_path / ".codehelm"
    state.mkdir()
    (state / "quality.yaml").write_text(
        "schema_version: 1\nbase_branch: staging\n", encoding="utf-8")
    engine = LifecycleEngine(repo, state / "state.db")
    engine.store.transition("DEV-1", "ready_for_pr", provider="claude", workspace=str(repo))
    ready = ContractDecision("ready_for_pr", (), False)
    with patch("codehelm.orchestrator.lifecycle.assess", return_value=ready), \
         patch("codehelm.orchestrator.lifecycle.publish_pr",
               return_value=type("PR", (), {"url": "https://github.com/o/r/pull/3"})()) as publish:
        result = CliRunner().invoke(
            app, ["pr-create", "DEV-1", "--title", "T", "--body", "B", "--repo", str(repo),
                  "--base", "develop", "--execute"])
    assert result.exit_code == 0, result.output
    assert publish.call_args.kwargs["base"] == "develop"
