"""Digest of a workspace change. Not a signature of a person.

The value is ``sha256:`` plus the hex sha256 of a canonical payload: the exact
stdout of ``git diff HEAD``, then each untracked non-ignored path from
``git status --porcelain -uall`` in sorted order. Each untracked path is
followed by that file's bytes. Lengths keep two different trees from hashing
to the same payload. Host secrets are not taken from the environment.
"""
from pathlib import Path
import hashlib
import os
import subprocess


def _git_env() -> dict[str, str]:
    """Environment for git. PATH only; no host tokens or credentials."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run_git(workspace: Path, args: list[str]) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            shell=False,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _unquote(raw: bytes) -> str | None:
    """Decode one porcelain path. Quoted paths use git's C-style escapes."""
    if raw.startswith(b'"'):
        if len(raw) < 2 or not raw.endswith(b'"'):
            return None
        body = raw[1:-1]
        out = bytearray()
        index = 0
        simple = {
            ord("a"): 0x07, ord("b"): 0x08, ord("t"): 0x09, ord("n"): 0x0A,
            ord("v"): 0x0B, ord("f"): 0x0C, ord("r"): 0x0D,
            ord("\\"): 0x5C, ord('"'): 0x22,
        }
        while index < len(body):
            byte = body[index]
            if byte != 0x5C:
                out.append(byte)
                index += 1
                continue
            if index + 1 >= len(body):
                return None
            nxt = body[index + 1]
            if nxt in simple:
                out.append(simple[nxt])
                index += 2
                continue
            if nxt not in b"01234567":
                return None
            octal = bytearray()
            cursor = index + 1
            while cursor < len(body) and len(octal) < 3 and body[cursor] in b"01234567":
                octal.append(body[cursor])
                cursor += 1
            try:
                value = int(octal.decode("ascii"), 8)
            except ValueError:
                return None
            if value > 255:
                return None
            out.append(value)
            index = cursor
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not text or "\x00" in text or text.startswith("/") or text.startswith("\\"):
        return None
    if ".." in Path(text).parts:
        return None
    return text


def _untracked(status: bytes) -> list[str] | None:
    paths: list[str] = []
    for line in status.splitlines():
        if not line.startswith(b"?? "):
            continue
        path = _unquote(line[3:])
        if path is None:
            return None
        paths.append(path)
    return sorted(set(paths))


def _file_bytes(workspace: Path, relative: str) -> bytes | None:
    """Read one untracked file inside the workspace. Do not follow escapes."""
    candidate = workspace / relative
    try:
        if candidate.is_symlink():
            return os.readlink(candidate).encode("utf-8", errors="surrogateescape")
        root = workspace.resolve()
        resolved = candidate.resolve()
        resolved.relative_to(root)
        if not resolved.is_file():
            return None
        return resolved.read_bytes()
    except (OSError, ValueError):
        return None


def _payload(diff: bytes, files: list[tuple[str, bytes]]) -> bytes:
    parts = [f"diff {len(diff)}\n".encode("ascii"), diff]
    for path, data in files:
        encoded = path.encode("utf-8")
        parts.append(f"file {len(encoded)} {len(data)}\n".encode("ascii"))
        parts.append(encoded)
        parts.append(b"\n")
        parts.append(data)
    return b"".join(parts)


def diff_digest(workspace: Path | None) -> str:
    """Return ``sha256:`` of the workspace change, or ``""`` when git cannot be read."""
    if workspace is None or not Path(workspace).is_dir():
        return ""
    workspace = Path(workspace)
    diff = _run_git(workspace, ["diff", "HEAD"])
    if diff is None:
        return ""
    status = _run_git(workspace, ["status", "--porcelain", "-uall"])
    if status is None:
        return ""
    paths = _untracked(status)
    if paths is None:
        return ""
    files: list[tuple[str, bytes]] = []
    for path in paths:
        data = _file_bytes(workspace, path)
        if data is None:
            return ""
        files.append((path, data))
    return "sha256:" + hashlib.sha256(_payload(diff, files)).hexdigest()
