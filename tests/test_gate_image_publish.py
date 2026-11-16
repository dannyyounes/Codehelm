"""The gate image is published by workflow. The gate still does not pull it."""
import subprocess
from pathlib import Path
from unittest.mock import patch

from codehelm.quality.gates import command_gate

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "ghcr.io/dannyyounes/codehelm"
BASE = "sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90"


def test_dockerfile_pins_the_base_and_gate_tools():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM python:3.13-slim@{BASE}" in text
    assert 'pip install --no-cache-dir "pytest==9.2.0" "bandit==1.9.4"' in text
    assert "CODEHELM_" not in text
    assert "pull" not in text.lower()


def test_publish_workflow_is_main_and_tags_only():
    text = (ROOT / ".github/workflows/publish-image.yml").read_text(encoding="utf-8")
    assert "pull_request" not in text
    assert "branches: [main]" in text
    assert 'tags: ["v*"]' in text
    assert "packages: write" in text
    assert "docker login ghcr.io" in text
    assert "--password-stdin" in text
    assert "docker pull" not in text
    assert "--pull" not in text
    assert 'tag="${image}:sha-${GITHUB_SHA}"' in text
    assert f"{IMAGE}@sha256:" in text
    assert "CodeHelm does not pull it." in text
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    for document in (readme, architecture):
        assert IMAGE in document
        assert "does not pull" in document
        assert f"{IMAGE}@sha256:" not in document
    assert "automatic Jira transitions" in readme
    assert "fetched or default exchange rate" in readme
    assert "a published container image" not in readme


def test_digest_reference_never_pulls_or_falls_back(tmp_path):
    image = f"{IMAGE}@{BASE}"
    seen = []

    def which(name, *args, **kwargs):
        seen.append(name)
        if name == "podman":
            return "/usr/bin/podman"
        return None

    with patch("codehelm.quality.gates.shutil.which", side_effect=which), \
         patch("codehelm.quality.gates.subprocess.run",
               return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")) as run:
        result = command_gate(tmp_path, ["pytest", "-q"], runner="container", image=image)
    assert result.passed
    assert seen == ["podman"]
    argv = run.call_args.args[0]
    assert argv[0] == "/usr/bin/podman"
    assert "--pull=never" in argv
    assert not any(part == "pull" or part.startswith("--pull=") and part != "--pull=never" for part in argv)
    assert image in argv
    assert argv[argv.index(image) + 1:] == ["pytest", "-q"]
    assert "unshare" not in argv
    assert run.call_args.kwargs["shell"] is False
