"""Digest of a workspace change. Not a signature of a person.

When ``resolve_diff_base`` finds an integration ref, the value is ``sha256:``
plus the hex sha256 of a canonical payload. The payload's diff is the working
tree versus ``git merge-base <base> HEAD``. A temporary copy of the index
receives ``git add -A`` (``.gitignore`` is honoured) and then
``git diff --cached --binary --no-ext-diff --no-textconv <merge-base>``.
Committed, staged, unstaged, and untracked files are in that diff, so
committing the same content leaves the digest unchanged. Any content edit
changes it. The real index is never modified (``GIT_OPTIONAL_LOCKS=0``). Git
errors yield ``""``. A submodule whose porcelain v2 ``<sub>`` field is
``S<c><m><u>`` with ``M`` (tracked modifications) or ``U`` (untracked
content) also yields ``""``: ``git add -A`` records only the commit pointer.
A clean pointer change (only ``C``) is digested normally; the cached diff
records the gitlink.
An untracked nested repository (a directory that contains ``.git`` and is not
a registered submodule) also yields ``""`` for the same reason.

When no base resolves, the previous payload is kept: stdout of ``git diff
--binary --no-ext-diff --no-textconv HEAD``, then each untracked non-ignored
path from ``git status --porcelain -uall`` in sorted order, followed by that
file's bytes. ``--binary`` keeps two tracked binary edits from sharing one
digest. Lengths keep two different trees from hashing to the same payload.

Approval digests assume an honest local workspace; adversarial tampering
(symlink/TOCTOU races, hostile git filters/attributes) is out of scope by
design.

Git runs without host config or credential environment variables.
"""
from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile


_CACHED_DIFF = [
    "-c", "core.quotePath=true",
    "-c", "diff.noprefix=false",
    "-c", "diff.mnemonicPrefix=false",
    "-c", "diff.relative=false",
    "diff", "--cached", "--binary", "--full-index", "--no-renames", "--no-color",
    "--no-ext-diff", "--no-textconv", "--ignore-submodules=none",
    "--src-prefix=a/", "--dst-prefix=b/",
]


def _git_env() -> dict[str, str]:
    """Environment for git. PATH only; no host tokens or credentials."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        # `git status` refreshes stat cache by rewriting the index unless this is 0.
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _run_git(workspace: Path, args: list[str], env: dict[str, str] | None = None) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), *args],
            capture_output=True,
            shell=False,
            env=_git_env() if env is None else env,
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


def _digest_against_head(workspace: Path) -> str:
    """Previous digest: uncommitted ``git diff HEAD`` plus untracked files."""
    diff = _run_git(workspace, ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"])
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


def _index_file(workspace: Path) -> Path | None:
    raw = _run_git(workspace, ["rev-parse", "--git-path", "index"])
    if not raw:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text or "\n" in text or "\x00" in text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = workspace / path
    if not path.is_file():
        return None
    return path


def _merge_base(workspace: Path, base: str) -> str | None:
    raw = _run_git(workspace, ["merge-base", base, "HEAD"])
    if not raw:
        return None
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    if not text or any(char not in "0123456789abcdef" for char in text):
        return None
    return text


def _dirty_submodule(workspace: Path) -> bool | None:
    """True when a submodule has content ``git add -A`` will not record.

    Porcelain v2 ``<sub>`` is ``N...`` or ``S<c><m><u>``. Only ``M`` (tracked
    modifications) and ``U`` (untracked content) are dirty. A clean pointer
    change (``C`` alone) is not: the cached diff records the gitlink.
    An untracked directory containing ``.git`` is also dirty: it is staged as
    a gitlink, so edits inside it never reach the digest.
    ``None`` means status could not be read.
    """
    status = _run_git(
        workspace,
        [
            "-c", "core.quotePath=true",
            "status", "--porcelain=v2", "--ignore-submodules=none", "--untracked-files=all",
        ],
    )
    if status is None:
        return None
    for line in status.splitlines():
        if line.startswith(b"? "):
            path = _unquote(line[2:])
            if path is None:
                return None
            if path.endswith("/"):
                path = path[:-1]
            if not path:
                return None
            candidate = workspace / path
            try:
                if candidate.is_dir() and (candidate / ".git").exists():
                    return True
            except OSError:
                return None
            continue
        if line[:2] not in (b"1 ", b"2 ", b"u "):
            continue
        fields = line.split(b" ")
        if len(fields) < 3:
            return None
        sub = fields[2]
        if sub in (b"N...", b"S..."):
            continue
        if (
            len(sub) == 4
            and sub[:1] == b"S"
            and sub[1:2] in (b".", b"C")
            and sub[2:3] in (b".", b"M")
            and sub[3:4] in (b".", b"U")
        ):
            if sub[2:3] == b"M" or sub[3:4] == b"U":
                return True
            continue
        return None
    return False


def _digest_against_base(workspace: Path, base: str) -> str:
    """Working tree versus the merge-base, including untracked files."""
    # Fail closed: submodule content and untracked nested repos are not in the gitlink.
    if _dirty_submodule(workspace) is not False:
        return ""
    merge_base = _merge_base(workspace, base)
    index = _index_file(workspace)
    if merge_base is None or index is None:
        return ""
    temp_name = None
    try:
        descriptor, temp_name = tempfile.mkstemp(prefix="codehelm-index-")
        os.close(descriptor)
        shutil.copyfile(index, temp_name)
        env = _git_env()
        env["GIT_INDEX_FILE"] = temp_name
        if _run_git(workspace, ["add", "-A"], env) is None:
            return ""
        diff = _run_git(workspace, [*_CACHED_DIFF, merge_base], env)
        if diff is None:
            return ""
        return "sha256:" + hashlib.sha256(_payload(diff, [])).hexdigest()
    except OSError:
        return ""
    finally:
        if temp_name is not None:
            for name in (temp_name, temp_name + ".lock"):
                try:
                    os.unlink(name)
                except OSError:
                    pass


def diff_digest(workspace: Path | None, base_branch: str = "") -> str:
    """Return ``sha256:`` of the workspace change, or ``""`` when git cannot be read."""
    if workspace is None or not Path(workspace).is_dir():
        return ""
    workspace = Path(workspace)
    try:
        from codehelm.quality.contract import resolve_diff_base
        base = resolve_diff_base(workspace, base_branch or "")
    except (OSError, subprocess.SubprocessError):
        return ""
    if not base:
        return _digest_against_head(workspace)
    return _digest_against_base(workspace, base)
