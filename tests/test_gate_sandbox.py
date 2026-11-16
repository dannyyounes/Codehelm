"""Gate commands run in a namespace sandbox or not at all."""
import shutil
from unittest.mock import patch

from codehelm.quality.gates import command_gate


def test_command_can_write_its_workspace_and_keeps_its_exit_code(tmp_path):
    wrote = command_gate(tmp_path, ["python", "-c", "open('marker.txt','w').write('in')"])
    assert wrote.passed and wrote.exit_code == 0
    assert (tmp_path / "marker.txt").read_text(encoding="utf-8") == "in"
    failed = command_gate(tmp_path, ["python", "-c", "raise SystemExit(2)"])
    assert not failed.passed and failed.exit_code == 2


def test_sandbox_hides_files_outside_the_workspace(tmp_path):
    secret = tmp_path.parent / "outside-secret.txt"
    secret.write_text("top-secret", encoding="utf-8")
    script = (
        "import pathlib,sys; "
        f"sys.exit(0 if not pathlib.Path({str(secret)!r}).exists() else 1)"
    )
    result = command_gate(tmp_path, ["python", "-c", script])
    assert result.passed and result.exit_code == 0
    assert secret.read_text(encoding="utf-8") == "top-secret"


def test_sandbox_drops_credential_env_and_blocks_network(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "sekrit")
    hidden = command_gate(tmp_path, [
        "python", "-c",
        "import os,sys; sys.exit(0 if 'GITHUB_TOKEN' not in os.environ else 1)",
    ])
    assert hidden.passed and hidden.exit_code == 0
    offline = command_gate(tmp_path, [
        "python", "-c",
        "import socket,sys; s=socket.socket(); s.settimeout(2); "
        "s.connect(('1.1.1.1', 80))",
    ])
    assert not offline.passed


def test_missing_unshare_does_not_run_the_command(tmp_path):
    real_which = shutil.which

    def which(name, *args, **kwargs):
        if name == "unshare":
            return None
        return real_which(name, *args, **kwargs)

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run") as run:
        result = command_gate(tmp_path, ["python", "-c", "open('ran.txt','w').write('x')"])
    assert not result.passed
    assert "Sandbox unavailable" in result.detail
    assert not run.called
    assert not (tmp_path / "ran.txt").exists()
