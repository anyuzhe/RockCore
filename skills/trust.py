"""User-local approval for repository-supplied Skill instructions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from app.paths import app_data_dir
from orchestrator.agent_config import SkillConfig


def approval_path() -> Path:
    return app_data_dir() / "skill_approvals.json"


def skill_fingerprint(project_root: str | Path, config: SkillConfig) -> str:
    root = Path(project_root).resolve()
    skills_root = root / ".ai" / "skills"
    files = []
    if skills_root.is_dir():
        resolved_skills = skills_root.resolve()
        for path in sorted(skills_root.glob("*/SKILL.md")):
            try:
                resolved = path.resolve(strict=True)
                if resolved.is_relative_to(resolved_skills):
                    files.append({
                        "path": resolved.relative_to(resolved_skills).as_posix(),
                        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
                    })
            except OSError:
                continue
    payload = {
        "project_root": os.path.normcase(str(root)),
        "config": asdict(config),
        "files": files,
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def is_project_skills_approved(project_root: str | Path, config: SkillConfig,
                               store_path: str | Path | None = None) -> bool:
    if not config.allow_project_skills:
        return True
    path = Path(store_path) if store_path else approval_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    root_key = os.path.normcase(str(Path(project_root).resolve()))
    return data.get(root_key) == skill_fingerprint(project_root, config)


def approve_project_skills(project_root: str | Path, config: SkillConfig,
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
    data[root_key] = skill_fingerprint(project_root, config)
    _atomic_write(path, data)


def revoke_project_skills(project_root: str | Path,
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
        prefix="skill-approvals-", suffix=".json", dir=str(path.parent)
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
