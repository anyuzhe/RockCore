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

    normalized = os.path.normcase(str(root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    fallback = app_data_dir() / "project-state" / digest / ".ai"
    if not is_writable_directory(fallback, create=True):
        raise OSError(f"Project metadata directory is not writable: {root}")
    return fallback
