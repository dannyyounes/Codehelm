"""Optional container runner for gate commands. Process launch is mocked."""
import subprocess
from unittest.mock import patch

import pytest

from codehelm.orchestrator.lifecycle import LifecycleEngine
from codehelm.quality.contract import load_policy
from codehelm.quality.gates import GateResult, command_gate


def _completed(code: int):
    return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr="")


def test_default_runner_builds_the_unshare_command(tmp_path):
    with patch("codehelm.quality.gates.shutil.which", return_value="/usr/bin/unshare"), \
         patch("codehelm.quality.gates.subprocess.run", return_value=_completed(0)) as run:
        result = command_gate(tmp_path, ["python", "-c", "pass"], image="local/ignored:test")
    assert result.passed and result.exit_code == 0
    argv = run.call_args.args[0]
    assert argv[:11] == [
        "unshare", "--user", "--map-root-user", "--mount", "--propagation", "private",
        "--pid", "--fork", "--net", "--mount-proc", "--",
    ]
    assert argv[-3:] == ["python", "-c", "pass"]
    assert "local/ignored:test" not in argv
    assert run.call_args.kwargs["shell"] is False


def test_container_without_runtime_does_not_launch_or_unshare(tmp_path):
    seen = []

    def which(name, *args, **kwargs):
        seen.append(name)
        return None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run") as run:
        result = command_gate(
            tmp_path, ["python", "-c", "open('ran.txt','w').write('x')"],
            runner="container", image="local/gates:test")
    assert not result.passed
    assert result.exit_code is None
    assert "Container runner unavailable" in result.detail
    assert not run.called
    assert seen == ["podman", "docker"]
    assert "unshare" not in seen
    assert not (tmp_path / "ran.txt").exists()


def test_container_argv_contains_image_network_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekrit")
    monkeypatch.setenv("PATH", "/usr/bin")
    seen = []

    def which(name, *args, **kwargs):
        seen.append(name)
        if name == "podman":
            return "/usr/bin/podman"
        if name == "docker":
            return "/usr/bin/docker"
        return None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run", return_value=_completed(0)) as run:
        result = command_gate(
            tmp_path, ["python", "-c", "pass"],
            runner="container", image="local/gates:test")
    assert result.passed
    assert seen == ["podman"]
    argv = run.call_args.args[0]
    assert argv[0] == "/usr/bin/podman"
    assert "--rm" in argv
    assert "--network=none" in argv
    assert "--cap-drop=ALL" in argv
    assert "--pull=never" in argv
    assert "local/gates:test" in argv
    image_at = argv.index("local/gates:test")
    assert argv[image_at + 1:] == ["python", "-c", "pass"]
    assert "unshare" not in argv
    assert run.call_args.kwargs["shell"] is False
    env = run.call_args.kwargs["env"]
    assert "GITHUB_TOKEN" not in env
    assert "sekrit" not in env.values()
    assert all("GITHUB_TOKEN" not in part and "sekrit" not in part for part in argv)
    mounted = str(tmp_path.resolve())
    assert any(mounted in part for part in argv)


def test_container_does_not_forward_a_secret_environment_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekrit")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "also-sekrit")

    def which(name, *args, **kwargs):
        return "/usr/bin/podman" if name == "podman" else None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run", return_value=_completed(0)) as run:
        command_gate(tmp_path, ["python", "-c", "pass"], runner="container", image="local/gates:test")
    argv = run.call_args.args[0]
    env = run.call_args.kwargs["env"]
    assert "GITHUB_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env
    assert "sekrit" not in env.values() and "also-sekrit" not in env.values()
    assert all("GITHUB_TOKEN" not in part and "sekrit" not in part for part in argv)
    assert all("AWS_SECRET_ACCESS_KEY" not in part and "also-sekrit" not in part for part in argv)


def test_container_uses_docker_when_podman_is_absent(tmp_path):
    def which(name, *args, **kwargs):
        if name == "docker":
            return "/usr/bin/docker"
        return None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run", return_value=_completed(0)) as run:
        result = command_gate(
            tmp_path, ["pytest", "-q"], runner="container", image="local/gates:test")
    assert result.passed
    argv = run.call_args.args[0]
    assert argv[0] == "/usr/bin/docker"
    assert "--network=none" in argv
    assert argv[-2:] == ["pytest", "-q"]
    assert "unshare" not in argv


def test_container_runtime_failure_does_not_fall_back(tmp_path):
    def which(name, *args, **kwargs):
        return "/usr/bin/podman" if name == "podman" else None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run", side_effect=OSError("exec failed")) as run:
        result = command_gate(
            tmp_path, ["python", "-c", "pass"], runner="container", image="local/gates:test")
    assert not result.passed
    assert run.call_count == 1
    assert run.call_args.args[0][0] == "/usr/bin/podman"
    assert "unshare" not in run.call_args.args[0]


def test_container_nonzero_exit_fails(tmp_path):
    def which(name, *args, **kwargs):
        return "/usr/bin/podman" if name == "podman" else None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run", return_value=_completed(3)) as run:
        result = command_gate(
            tmp_path, ["python", "-c", "pass"], runner="container", image="local/gates:test")
    assert not result.passed and result.exit_code == 3
    assert run.call_count == 1


def test_invalid_gate_runner_is_rejected(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\ngate_runner: host\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gate_runner"):
        load_policy(path)


def test_gate_image_cannot_be_a_runtime_flag(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text(
        "schema_version: 1\ngate_runner: container\ngate_image: '--privileged'\n",
        encoding="utf-8")
    with pytest.raises(ValueError, match="gate_image"):
        load_policy(path)
    with patch("codehelm.quality.gates.subprocess.run") as run:
        result = command_gate(
            tmp_path, ["python", "-c", "pass"], runner="container", image="--privileged")
    assert not result.passed
    assert "local image name" in result.detail
    assert not run.called


def test_container_without_image_is_rejected(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\ngate_runner: container\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gate_image"):
        load_policy(path)
    path.write_text(
        "schema_version: 1\ngate_runner: container\ngate_image: '   '\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gate_image"):
        load_policy(path)


def test_omitted_runner_stays_namespace_and_ignores_image(tmp_path):
    path = tmp_path / "quality.yaml"
    path.write_text("schema_version: 1\n", encoding="utf-8")
    policy = load_policy(path)
    assert policy.gate_runner == "namespace" and policy.gate_image == ""
    path.write_text(
        "schema_version: 1\ngate_runner: namespace\ngate_image: local/ignored:test\n",
        encoding="utf-8")
    ignored = load_policy(path)
    assert ignored.gate_runner == "namespace" and ignored.gate_image == ""
    path.write_text(
        "schema_version: 1\ngate_runner: container\ngate_image: local/gates:test\n",
        encoding="utf-8")
    selected = load_policy(path)
    assert selected.gate_runner == "container" and selected.gate_image == "local/gates:test"


def _git_repo(path):
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    return path


def test_absent_policy_does_not_request_a_container(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition(
        "DEV-9", "awaiting_validation", provider="claude", workspace=str(repo))
    passed = GateResult(True, "APPROVED", 0)
    with patch("codehelm.orchestrator.lifecycle.command_gate", return_value=passed) as gate, \
         patch("codehelm.orchestrator.lifecycle.independent_review", return_value=passed):
        assert engine.validate("DEV-9", ["pytest"], ["bandit"], "codex")
    assert gate.call_count == 2
    for call in gate.call_args_list:
        assert call.kwargs["runner"] == "namespace"
        assert call.kwargs["image"] == ""


def test_container_policy_is_threaded_into_command_gate(tmp_path):
    repo = _git_repo(tmp_path / "repo")
    (tmp_path / "quality.yaml").write_text(
        "schema_version: 1\ngate_runner: container\ngate_image: local/gates:test\n",
        encoding="utf-8")
    engine = LifecycleEngine(repo, tmp_path / "state.db")
    engine.store.transition(
        "DEV-9", "awaiting_validation", provider="claude", workspace=str(repo))
    passed = GateResult(True, "APPROVED", 0)
    with patch("codehelm.orchestrator.lifecycle.command_gate", return_value=passed) as gate, \
         patch("codehelm.orchestrator.lifecycle.independent_review", return_value=passed):
        assert engine.validate("DEV-9", ["pytest"], ["bandit"], "codex")
    commands = []
    for call in gate.call_args_list:
        assert call.kwargs["runner"] == "container"
        assert call.kwargs["image"] == "local/gates:test"
        commands.append(call.args[1])
    assert commands == [["pytest"], ["bandit"]]
