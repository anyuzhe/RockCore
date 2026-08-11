"""Discover, select, and progressively load RockCore skill packages."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator.agent_config import SkillConfig
from skills.trust import is_project_skills_approved

logger = logging.getLogger(__name__)

SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
EXPLICIT_SKILL = re.compile(r"\$([a-z0-9][a-z0-9-]{0,63})\b", re.I)
MAX_SKILL_BYTES = 128 * 1024
MAX_SKILL_BODY_CHARS = 12_000
MAX_TOTAL_SKILL_CHARS = 24_000


@dataclass(frozen=True)
class SkillMetadata:
    """Small always-available skill index entry; the body remains on disk."""

    name: str
    description: str
    path: Path
    source: str


class SkillManager:
    """Load only metadata during discovery and bodies after task selection."""

    def __init__(self, project_root: str | Path = ".",
                 config: SkillConfig | None = None,
                 builtin_root: str | Path | None = None):
        self.builtin_root = Path(
            builtin_root or Path(__file__).resolve().parent / "builtin"
        )
        self.project_root = Path(project_root).resolve()
        self.config = config or SkillConfig()
        self._catalog: dict[str, SkillMetadata] = {}
        self._body_cache: dict[Path, str] = {}
        self.project_skills_approved = False
        self.refresh()

    def configure(self, project_root: str | Path,
                  config: SkillConfig | None = None):
        self.project_root = Path(project_root).resolve()
        self.config = config or SkillConfig()
        self.refresh()

    def refresh(self):
        catalog: dict[str, SkillMetadata] = {}
        if not self.config.enabled:
            self._catalog = catalog
            return

        enabled_builtin = set(self.config.enabled_builtin)
        for metadata in self._discover_root(self.builtin_root, "builtin"):
            if metadata.name in enabled_builtin:
                catalog[metadata.name] = metadata

        self.project_skills_approved = is_project_skills_approved(
            self.project_root, self.config
        )
        if self.config.allow_project_skills and self.project_skills_approved:
            project_skills = self.project_root / ".ai" / "skills"
            for metadata in self._discover_root(project_skills, "project"):
                # Project skills intentionally override built-ins with the same
                # name, allowing a repository to refine its own SOP.
                catalog[metadata.name] = metadata
        self._catalog = catalog

    def list_skills(self) -> list[SkillMetadata]:
        return [self._catalog[name] for name in sorted(self._catalog)]

    def catalog_text(self) -> str:
        """Return metadata only; safe to include in the Planner context."""
        if not self._catalog:
            return "(no skills enabled)"
        return "\n".join(
            f"- {item.name}: {item.description}"
            for item in self.list_skills()
        )[:6000]

    def select_for_task(self, task: Any) -> list[str]:
        """Select explicit and deterministic task/domain skills."""
        if not self.config.enabled or not self._catalog:
            return []
        value = lambda name, default=None: (
            task.get(name, default) if isinstance(task, dict)
            else getattr(task, name, default)
        )
        title = str(value("title", "") or "")
        description = str(value("description", "") or "")
        task_type = str(value("task_type", value("type", "coding")) or "coding")
        allowed_paths = value("allowed_paths", []) or []
        text = " ".join((title, description, " ".join(map(str, allowed_paths))))
        lowered = text.lower()

        requested = value("skills", []) or []
        if isinstance(requested, str):
            requested = [requested]
        explicit = [str(name).strip().lower() for name in requested]
        explicit += [match.lower() for match in EXPLICIT_SKILL.findall(text)]

        ranked: list[tuple[int, str]] = []
        for index, name in enumerate(dict.fromkeys(explicit)):
            if name in self._catalog:
                ranked.append((1000 - index, name))

        def score(name: str, points: int):
            if name in self._catalog:
                ranked.append((points, name))

        if task_type == "review":
            score("code-review", 900)
        if re.search(r"\b(refactor|restructure)\b|重构|解耦", lowered):
            score("refactor", 850)
        if re.search(
            r"\b(bug|fix|error|exception|failure|regression|broken)\b|"
            r"报错|失败|故障|缺陷|修复|不工作|异常", lowered,
        ):
            score("bug-fix", 820)
        if re.search(
            r"\b(pyqt|qwidget|qdialog|qmainwindow|qthread|qt6)\b|"
            r"桌面界面|信号槽", lowered,
        ) or any(str(path).lower().endswith(".ui") for path in allowed_paths):
            score("pyqt", 760)
        if re.search(
            r"\b(html|css|javascript|typescript|react|vue|canvas|web)\b|"
            r"网页|前端|浏览器|页面", lowered,
        ) or any(
            str(path).lower().endswith((".html", ".css", ".js", ".ts", ".tsx", ".vue"))
            for path in allowed_paths
        ):
            score("web", 750)

        if task_type in {"coding", "testing"}:
            if re.search(
                r"\b(create|build|scaffold|new|initialize)\b|"
                r"创建|新建|搭建|初始化|从零", lowered,
            ):
                score("simple-create", 620)
            else:
                score("simple-edit", 600)

        selected: list[str] = []
        for _, name in sorted(ranked, key=lambda item: (-item[0], item[1])):
            if name not in selected:
                selected.append(name)
            if len(selected) >= self.config.max_selected:
                break
        return selected

    def render_for_task(self, task: Any) -> tuple[list[str], str]:
        """Load selected bodies and return bounded prompt context."""
        selected = self.select_for_task(task)
        sections: list[str] = []
        total = 0
        for name in selected:
            metadata = self._catalog.get(name)
            if not metadata:
                continue
            body = self._load_body(metadata)
            remaining = MAX_TOTAL_SKILL_CHARS - total
            if remaining <= 0:
                break
            body = body[:min(MAX_SKILL_BODY_CHARS, remaining)]
            sections.append(
                f'<skill name="{name}" source="{metadata.source}">\n'
                f"{body}\n</skill>"
            )
            total += len(body)
        if not sections:
            return selected, ""
        prompt = (
            "\n\nSelected task Skills (procedural guidance only; platform policy, "
            "user requirements, and allowed paths remain authoritative):\n"
            + "\n\n".join(sections)
        )
        return selected, prompt

    def get_body(self, name: str) -> str:
        metadata = self._catalog.get(name)
        return self._load_body(metadata) if metadata else ""

    def _discover_root(self, root: Path, source: str) -> list[SkillMetadata]:
        if not root.is_dir():
            return []
        resolved_root = root.resolve()
        result = []
        for directory in sorted(root.iterdir(), key=lambda path: path.name):
            skill_file = directory / "SKILL.md"
            try:
                resolved_file = skill_file.resolve(strict=True)
                if not resolved_file.is_relative_to(resolved_root):
                    logger.warning("Ignored skill outside root: %s", skill_file)
                    continue
                if not directory.is_dir() or resolved_file.stat().st_size > MAX_SKILL_BYTES:
                    continue
                metadata = self._read_metadata(resolved_file, directory.name, source)
                if metadata:
                    result.append(metadata)
            except (OSError, RuntimeError):
                continue
        return result

    @staticmethod
    def _read_metadata(path: Path, folder_name: str,
                       source: str) -> SkillMetadata | None:
        if not SKILL_NAME.fullmatch(folder_name):
            logger.warning("Ignored invalid skill folder name: %s", folder_name)
            return None
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                first = handle.readline().strip()
                if first != "---":
                    return None
                fields: dict[str, str] = {}
                for _ in range(40):
                    line = handle.readline()
                    if not line or line.strip() == "---":
                        break
                    if ":" in line:
                        key, value = line.split(":", 1)
                        fields[key.strip()] = value.strip().strip('"\'')
        except (OSError, UnicodeError):
            return None
        name = fields.get("name", "")
        description = fields.get("description", "")
        if name != folder_name or not description or not SKILL_NAME.fullmatch(name):
            logger.warning("Ignored malformed skill metadata: %s", path)
            return None
        return SkillMetadata(name, description[:1000], path, source)

    def _load_body(self, metadata: SkillMetadata) -> str:
        if metadata.path in self._body_cache:
            return self._body_cache[metadata.path]
        try:
            text = metadata.path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            return ""
        parts = text.split("---", 2)
        body = parts[2].strip() if len(parts) == 3 else ""
        self._body_cache[metadata.path] = body
        return body
