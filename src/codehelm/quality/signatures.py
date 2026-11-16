"""Verify an SSH or GPG signature over a diff digest.

The signed bytes are the exact ``sha256:`` digest ASCII, with no added
newline. SSH uses ``ssh-keygen -Y verify`` in the ``codehelm`` namespace.
GPG imports only ``gpg-public.asc`` from the signers directory into a
temporary home. Neither command uses a keyserver or the default GnuPG home.
A missing tool, key, or identity binding does not verify.
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile

_NAMESPACE = "codehelm"
_KINDS = frozenset({"ssh", "gpg"})
_MAX_SIGNATURE = 8000
_UID_SPLIT = re.compile(r"[\s<>]+")


def signature_blob(kind: str, text: str) -> str:
    """Store a verified signature with its kind. Reject anything else."""
    if kind not in _KINDS:
        raise ValueError("Approval signature kind must be ssh or gpg")
    if not isinstance(text, str) or not text.strip() or "\x00" in text:
        raise ValueError("Approval signature is empty or not text")
    if len(text) > _MAX_SIGNATURE:
        raise ValueError("Approval signature is too large")
    return f"{kind}\n{text}"


def split_signature(stored: str) -> tuple[str, str] | None:
    """Return kind and signature text, or None when the row is unsigned."""
    if not stored or not stored.strip():
        return None
    kind, separator, text = stored.partition("\n")
    if separator != "\n" or kind not in _KINDS or not text.strip() or "\x00" in text:
        return None
    return kind, text


def _tool_env(home: Path | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
    }
    if home is not None:
        env["GNUPGHOME"] = str(home)
    return env


def _run(argv: list[str], *, message: bytes | None = None, home: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        input=message,
        capture_output=True,
        shell=False,
        env=_tool_env(home),
        timeout=30,
        check=False,
    )


def _uid_binds(identity: str, userid: str) -> bool:
    """The GitHub login must be the user id or the local part of its email."""
    pieces = [part for part in _UID_SPLIT.split(userid) if part]
    local_parts = [part.split("@", 1)[0] for part in pieces if "@" in part]
    return identity in pieces or identity in local_parts


def _gpg_verified(status: str, identity: str) -> bool:
    if any(token in status for token in ("BADSIG", "ERRSIG", "EXPSIG", "REVKEYSIG", "NO_PUBKEY")):
        return False
    matched = False
    for line in status.splitlines():
        if not line.startswith("[GNUPG:] GOODSIG "):
            continue
        rest = line[len("[GNUPG:] GOODSIG "):]
        _key, separator, userid = rest.partition(" ")
        if separator and _uid_binds(identity, userid):
            matched = True
    return matched


def verify_approval_signature(stored: str, digest: str, identity: str, signers: Path | None) -> bool:
    """Re-check one stored signature against the current digest and signers."""
    parsed = split_signature(stored)
    if parsed is None or not digest.startswith("sha256:") or not identity or identity.startswith("-"):
        return False
    if any(character.isspace() or character == "\x00" for character in identity):
        return False
    if signers is None or not signers.is_dir():
        return False
    kind, text = parsed
    if len(text) > _MAX_SIGNATURE:
        return False
    message = digest.encode("ascii")
    try:
        with tempfile.TemporaryDirectory(prefix="codehelm-sig-", dir="/tmp") as temporary:
            home = Path(temporary)
            home.chmod(0o700)
            signature_path = home / "approval.sig"
            message_path = home / "digest.txt"
            signature_path.write_text(text, encoding="utf-8")
            message_path.write_bytes(message)
            if kind == "ssh":
                allowed = signers / "allowed_signers"
                tool = shutil.which("ssh-keygen")
                if tool is None or not allowed.is_file():
                    return False
                result = _run(
                    [tool, "-Y", "verify", "-f", str(allowed), "-I", identity,
                     "-n", _NAMESPACE, "-s", str(signature_path)],
                    message=message_path.read_bytes(),
                )
                return result.returncode == 0
            public_key = signers / "gpg-public.asc"
            tool = shutil.which("gpg")
            if tool is None or not public_key.is_file():
                return False
            imported = _run(
                [tool, "--batch", "--no-default-keyring", "--homedir", str(home),
                 "--import", str(public_key)],
                home=home,
            )
            if imported.returncode != 0:
                return False
            checked = _run(
                [tool, "--batch", "--status-fd", "1", "--no-default-keyring",
                 "--homedir", str(home), "--trust-model", "always",
                 "--keyserver-options", "no-auto-key-retrieve",
                 "--verify", str(signature_path), str(message_path)],
                home=home,
            )
            status = checked.stdout.decode("utf-8", errors="replace")
            return checked.returncode == 0 and _gpg_verified(status, identity)
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return False
