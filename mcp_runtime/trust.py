"""User-local approval for project-supplied MCP process configurations."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from app.paths import app_data_dir
from orchestrator.agent_config import MCPConfig


def approval_path() -> Path:
    return app_data_dir() / "mcp_approvals.json"


def config_fingerprint(project_root: str | Path, config: MCPConfig) -> str:
    payload = {
        "project_root": os.path.normcase(str(Path(project_root).resolve())),
        "mcp": asdict(config),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_project_mcp_approved(project_root: str | Path, config: MCPConfig,
                            store_path: str | Path | None = None) -> bool:
    if not config.enabled:
        return True
    path = Path(store_path) if store_path else approval_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    root_key = os.path.normcase(str(Path(project_root).resolve()))
    return data.get(root_key) == config_fingerprint(project_root, config)


def approve_project_mcp(project_root: str | Path, config: MCPConfig,
                        store_path: str | Path | None = None):
    path = Path(store_path) if store_path else approval_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        data = {}
    root_key = os.path.normcase(str(Path(project_root).resolve()))
    data[root_key] = config_fingerprint(project_root, config)
    _atomic_write(path, data)


def revoke_project_mcp(project_root: str | Path,
                       store_path: str | Path | None = None):
    path = Path(store_path) if store_path else approval_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    root_key = os.path.normcase(str(Path(project_root).resolve()))
    if isinstance(data, dict) and data.pop(root_key, None) is not None:
        _atomic_write(path, data)


def _atomic_write(path: Path, data: dict):
    descriptor, temporary = tempfile.mkstemp(
        prefix="mcp-approvals-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass
