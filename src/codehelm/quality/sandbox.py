"""Run one gate command in a private user, mount, pid, and network namespace.

The workspace stays writable. /tmp, /home, and /root are covered, then the
workspace and any executable directory that lived under those paths are
bind-mounted back. Nothing here builds a container image.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys

_COVERED = (Path("/tmp"), Path("/home"), Path("/root"))


def _mount(*args: str) -> None:
    result = subprocess.run(["mount", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout or "mount failed").strip()
        raise SystemExit(detail[:500])


def _under_covered(path: Path) -> bool:
    if any(path == root or root in path.parents for root in _COVERED):
        return True
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in _COVERED)


def _remember(ordered: list[Path], seen: set[Path], path: Path, workspace: Path) -> None:
    """Keep the path the command will open, including a symlink's own name."""
    if path in seen or not _under_covered(path):
        return
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved == workspace or workspace in resolved.parents:
        return
    if path == workspace or workspace in path.parents:
        return
    seen.add(path)
    ordered.append(path)


def _link_chain(path: Path) -> list[Path]:
    """Each path in a symlink chain, including the final target."""
    chain = [path]
    seen: set[Path] = set()
    current = path
    while current.is_symlink() and current not in seen:
        seen.add(current)
        target = Path(os.readlink(current))
        if not target.is_absolute():
            target = current.parent / target
        current = target
        chain.append(current)
    return chain


def _interpreter_roots(executable: Path) -> list[Path]:
    """Directories a command binary needs when they would otherwise be covered."""
    roots: list[Path] = []
    for path in _link_chain(executable):
        roots.append(path.parent)
        if path.parent.name == "bin":
            roots.append(path.parent.parent)
            config = path.parent.parent / "pyvenv.cfg"
            if config.is_file():
                for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("home ="):
                        home = Path(line.split("=", 1)[1].strip())
                        roots.append(home.parent if home.name == "bin" else home)
    return roots


def _keep_dirs(command: list[str], workspace: Path) -> list[Path]:
    """Directories that must stay visible when they sit under a covered path."""
    executable = command[0]
    if os.sep not in executable:
        found = shutil.which(executable)
        if not found:
            return []
        candidate = Path(found)
    else:
        candidate = Path(executable)
    ordered: list[Path] = []
    seen: set[Path] = set()
    for root in _interpreter_roots(candidate):
        _remember(ordered, seen, root, workspace)
    return ordered


def _stage(name: str) -> Path:
    path = Path("/dev/shm") / f"codehelm-{os.getpid()}-{name}"
    path.mkdir(exist_ok=True)
    return path


def main(argv: list[str] | None = None) -> None:
    """Prepare the namespace and replace this process with the gate command."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" not in argv:
        raise SystemExit("Sandbox usage: workspace -- command")
    separator = argv.index("--")
    if separator != 1 or separator + 1 >= len(argv):
        raise SystemExit("Sandbox usage: workspace -- command")
    workspace = Path(argv[0]).resolve()
    command = argv[separator + 1:]
    if not workspace.is_dir() or not command or not command[0]:
        raise SystemExit("Sandbox workspace or command is missing")
    stages: list[Path] = []
    try:
        workspace_stage = _stage("ws")
        stages.append(workspace_stage)
        _mount("--bind", str(workspace), str(workspace_stage))
        kept = []
        for index, directory in enumerate(_keep_dirs(command, workspace)):
            stage = _stage(f"keep{index}")
            stages.append(stage)
            _mount("--bind", str(directory), str(stage))
            kept.append((stage, directory))
        for root in _COVERED:
            if root.is_dir():
                _mount("-t", "tmpfs", "tmpfs", str(root))
        workspace.mkdir(parents=True, exist_ok=True)
        _mount("--bind", str(workspace_stage), str(workspace))
        for stage, directory in kept:
            directory.mkdir(parents=True, exist_ok=True)
            _mount("--bind", str(stage), str(directory))
        for stage in stages:
            subprocess.run(["umount", str(stage)], capture_output=True, check=False)
            try:
                stage.rmdir()
            except OSError:
                pass
        stages.clear()
        temporary = workspace / ".codehelm" / "tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(workspace)
        os.environ["TMPDIR"] = str(temporary)
        os.chdir(workspace)
        os.execvp(command[0], command)
    finally:
        for stage in stages:
            subprocess.run(["umount", str(stage)], capture_output=True, check=False)
            try:
                stage.rmdir()
            except OSError:
                pass
    raise SystemExit("Sandbox could not start the command")


if __name__ == "__main__":
    main()
