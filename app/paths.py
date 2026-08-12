"""Writable per-user paths for source and packaged desktop builds."""

import hashlib
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path


def application_dir() -> Path:
    """Return the read-only application location, never a data directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundled_git_root() -> Path | None:
    """Return the packaged MinGit root when this build includes it."""
    roots = []
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        roots.append(Path(frozen_root))
    roots.append(application_dir())
    for root in roots:
        candidate = root / "runtime" / "git"
        if (candidate / "cmd" / "git.exe").is_file():
            return candidate
    return None


def configure_bundled_git() -> Path | None:
    """Put packaged MinGit first on PATH without requiring a system install."""
    if sys.platform != "win32":
        return None
    root = bundled_git_root()
    if root is None:
        return None
    directories = [root / "cmd", root / "mingw64" / "bin"]
    current = os.environ.get("PATH", "")
    existing = [item for item in current.split(os.pathsep) if item]
    normalized = {os.path.normcase(os.path.abspath(item)) for item in existing}
    prepend = [
        str(path) for path in directories
        if path.is_dir()
        and os.path.normcase(os.path.abspath(path)) not in normalized
    ]
    if prepend:
        os.environ["PATH"] = os.pathsep.join([*prepend, *existing])
    return root / "cmd" / "git.exe"


def is_writable_directory(path: str | os.PathLike[str], *,
                          create: bool = False) -> bool:
    """Verify writability with a real file probe (``os.access`` is unreliable)."""
    directory = Path(path).expanduser()
    try:
        if directory.exists() and not directory.is_dir():
            return False
        if not directory.exists():
            if not create:
                return False
            directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".rockcore-write-{os.getpid()}-{uuid.uuid4().hex}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
        finally:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        return True
    except (OSError, PermissionError, RuntimeError):
        return False


def _first_writable_directory(candidates: list[Path]) -> Path:
    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(str(candidate.expanduser()))
        if not key or key in seen:
            continue
        seen.add(key)
        if is_writable_directory(candidate, create=True):
            return candidate.expanduser().resolve()
    raise OSError("RockCore could not find a writable per-user data directory")


def app_data_dir() -> Path:
    if sys.platform == "win32":
        candidates = [
            Path(value) / "RockCore"
            for value in (
                os.environ.get("APPDATA", ""),
                os.environ.get("LOCALAPPDATA", ""),
            )
            if value
        ]
        candidates.extend([
            Path.home() / "AppData" / "Roaming" / "RockCore",
            Path(tempfile.gettempdir()) / "RockCore",
        ])
    elif sys.platform == "darwin":
        candidates = [
            Path.home() / "Library" / "Application Support" / "RockCore",
            Path(tempfile.gettempdir()) / "RockCore",
        ]
    else:
        candidates = [
            Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "RockCore",
            Path(tempfile.gettempdir()) / "RockCore",
        ]
    return _first_writable_directory(candidates)


def _legacy_data_dir() -> Path:
    return Path.home() / ".ai_engineering_studio"


def _migrate_legacy_file(filename: str, target: Path):
    """Preserve settings/history from pre-packaging RockCore builds once."""
    legacy = _legacy_data_dir() / filename
    if target.exists() or not legacy.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
    except OSError:
        # A read-only legacy directory should never prevent the app from starting.
        pass


def config_path() -> Path:
    path = app_data_dir() / "config.json"
    _migrate_legacy_file("config.json", path)
    return path


def database_path() -> Path:
    path = app_data_dir() / "studio.db"
    _migrate_legacy_file("studio.db", path)
    return path


def default_workspace_dir() -> Path:
    """Use a writable workspace for first launch and context indexing."""
    return _first_writable_directory([
        Path.home() / "RockCore Projects",
        app_data_dir() / "Projects",
        Path(tempfile.gettempdir()) / "RockCore Projects",
    ])


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_protected_windows_location(path: Path) -> bool:
    if sys.platform != "win32":
        return False
    protected = [
        os.environ.get("ProgramFiles", ""),
        os.environ.get("ProgramFiles(x86)", ""),
        os.environ.get("WINDIR", ""),
    ]
    return any(
        value and _is_within(path, Path(value))
        for value in protected
    )


def resolve_working_dir(configured: str | os.PathLike[str] | None,
                        *, install_dir: str | os.PathLike[str] | None = None) -> Path:
    """Resolve a writable workspace and reject stale installer-directory values."""
    candidate: Path | None = None
    if configured:
        try:
            expanded = os.path.expandvars(os.fspath(configured))
            candidate = Path(expanded).expanduser()
        except (TypeError, ValueError, OSError):
            candidate = None

    if candidate and candidate.is_absolute():
        inside_install = bool(
            install_dir and _is_within(candidate, Path(install_dir))
        )
        if (
            not inside_install
            and not _is_protected_windows_location(candidate)
            and is_writable_directory(candidate, create=True)
        ):
            return candidate.resolve()
    return default_workspace_dir()


def is_usable_project_dir(path: str | os.PathLike[str]) -> bool:
    """Return whether a selected project can safely be modified by RockCore."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() or not candidate.is_dir():
        return False
    if _is_protected_windows_location(candidate):
        return False
    if getattr(sys, "frozen", False) and _is_within(
        candidate, application_dir()
    ):
        return False
    return is_writable_directory(candidate)


def project_state_dir(project_root: str | os.PathLike[str]) -> Path:
    """Store project metadata beside the project, with a per-user fallback."""
    root = Path(project_root).expanduser().resolve()
    preferred = root / ".ai"
    if is_writable_directory(preferred, create=True):
        return preferred

    _, fallback = project_state_paths(root)
    if not is_writable_directory(fallback, create=True):
        raise OSError(
            "Project metadata directory is not writable: "
            f"{Path(project_root).expanduser()}"
        )
    return fallback


class ProjectStateCleanupError(OSError):
    """Raised when a project's generated state cannot be safely removed."""


def project_state_paths(
    project_root: str | os.PathLike[str],
) -> tuple[Path, Path]:
    """Return local and per-user state locations without creating either."""
    root = Path(project_root).expanduser().resolve()
    preferred = root / ".ai"
    normalized = os.path.normcase(str(root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    fallback = app_data_dir() / "project-state" / digest / ".ai"
    return preferred, fallback


def _is_reparse_point(path: Path) -> bool:
    """Detect Windows junctions without requiring Python 3.12."""
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def _remove_registered_project_worktrees(root: Path, state_dir: Path):
    """Unregister linked worktrees before deleting their generated directory."""
    worktrees_root = state_dir / "worktrees"
    if (
        not os.path.lexists(worktrees_root)
        or worktrees_root.is_symlink()
        or _is_reparse_point(worktrees_root)
        or not os.path.lexists(root / ".git")
    ):
        return

    from app.subprocess_utils import run_process

    listed = run_process(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        cwd=root,
    )
    if listed.returncode != 0:
        raise ProjectStateCleanupError(
            "无法读取项目 Git worktree："
            + (listed.stderr.strip() or f"exit {listed.returncode}")
        )
    registered = [
        Path(line[9:].strip()).expanduser()
        for line in listed.stdout.splitlines()
        if line.startswith("worktree ") and line[9:].strip()
    ]
    for worktree in registered:
        try:
            resolved = worktree.resolve()
        except OSError:
            resolved = worktree.absolute()
        if resolved == root or not _is_within(resolved, worktrees_root):
            continue
        removed = run_process(
            ["git", "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            cwd=root,
        )
        if removed.returncode != 0:
            raise ProjectStateCleanupError(
                f"无法注销 Git worktree {worktree}："
                + (removed.stderr.strip() or f"exit {removed.returncode}")
            )


def _remove_state_path(target: Path) -> bool:
    """Remove one exact state target without traversing top-level links."""
    if not os.path.lexists(target):
        return False
    if target.is_symlink():
        target.unlink()
    elif _is_reparse_point(target):
        if target.is_dir():
            os.rmdir(target)
        else:
            target.unlink()
    elif target.is_dir():
        if os.path.ismount(target):
            raise ProjectStateCleanupError(
                f"拒绝删除挂载点形式的项目状态目录：{target}"
            )
        shutil.rmtree(target)
    else:
        target.unlink()
    return True


def remove_project_state(
    project_root: str | os.PathLike[str],
) -> list[Path]:
    """Delete all RockCore state for a removed project, never project files."""
    root = Path(project_root).expanduser().resolve()
    local_state, fallback_state = project_state_paths(root)
    removed: list[Path] = []
    current_target = local_state
    try:
        _remove_registered_project_worktrees(root, local_state)
        for target in (local_state, fallback_state):
            current_target = target
            if target.name != ".ai":
                raise ProjectStateCleanupError(
                    f"拒绝清理非 .ai 路径：{target}"
                )
            if _remove_state_path(target):
                removed.append(target)
    except ProjectStateCleanupError:
        raise
    except OSError as error:
        raise ProjectStateCleanupError(
            f"无法删除项目状态目录 {current_target}：{error}"
        ) from error
    return removed
