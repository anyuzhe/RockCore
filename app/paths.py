"""Writable per-user paths for source and packaged desktop builds."""

import os
import shutil
import sys
from pathlib import Path


def app_data_dir() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home()))
        path = root / "RockCore"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "RockCore"
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "RockCore"
    path.mkdir(parents=True, exist_ok=True)
    return path


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
    path = Path.home() / "RockCore Projects"
    path.mkdir(parents=True, exist_ok=True)
    return path
