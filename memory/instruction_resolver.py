"""Resolve Codex-style layered repository instructions."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


DEFAULT_MAX_BYTES = 32 * 1024


@dataclass(frozen=True)
class InstructionSource:
    path: Path
    content: str


class InstructionResolver:
    """Load one AGENTS instruction file per directory, root to workdir.

    ``AGENTS.override.md`` wins over ``AGENTS.md`` inside the same directory.
    Later (more specific) files are appended last so their rules take
    precedence. Repository instructions are data supplied by the project; the
    platform/user policy and tool permissions remain authoritative.
    """

    def __init__(self, project_root: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES,
                 codex_home: str | Path | None = None):
        self.project_root = Path(project_root).resolve()
        self.max_bytes = max(1024, int(max_bytes))
        configured_home = codex_home or os.environ.get("CODEX_HOME")
        self.codex_home = Path(
            configured_home or (Path.home() / ".codex")
        ).expanduser().resolve()

    def resolve(self, working_directory: str | Path | None = None) -> list[InstructionSource]:
        target = Path(working_directory or self.project_root).resolve()
        try:
            target.relative_to(self.project_root)
        except ValueError:
            target = self.project_root
        if target.is_file():
            target = target.parent

        directories = [self.project_root]
        if target != self.project_root:
            relative = target.relative_to(self.project_root)
            cursor = self.project_root
            for part in relative.parts:
                cursor = cursor / part
                directories.append(cursor)

        sources: list[InstructionSource] = []
        used = 0
        global_source = self._select(self.codex_home)
        if global_source is not None:
            loaded = self._load(global_source, self.max_bytes)
            if loaded is not None:
                sources.append(loaded)
                used += len(loaded.content.encode("utf-8"))
        for directory in directories:
            selected = self._select(directory)
            if selected is None:
                continue
            remaining = self.max_bytes - used
            if remaining <= 0:
                break
            loaded = self._load(selected, remaining)
            if loaded is None:
                continue
            sources.append(loaded)
            used += len(loaded.content.encode("utf-8"))
        return sources

    @staticmethod
    def _select(directory: Path) -> Path | None:
        return next((
            candidate for candidate in (
                directory / "AGENTS.override.md",
                directory / "AGENTS.md",
            ) if candidate.is_file()
        ), None)

    @staticmethod
    def _load(path: Path, remaining: int) -> InstructionSource | None:
        try:
            payload = path.read_bytes()
        except OSError:
            return None
        if not payload.strip() or remaining <= 0:
            return None
        payload = payload[:remaining]
        content = payload.decode("utf-8-sig", errors="replace").strip()
        return InstructionSource(path, content) if content else None

    def render(self, working_directory: str | Path | None = None) -> str:
        sources = self.resolve(working_directory)
        if not sources:
            return ""
        sections = []
        for source in sources:
            try:
                label = source.path.relative_to(self.project_root).as_posix()
                heading = "Repository Instructions"
            except ValueError:
                label = source.path.name
                heading = "Global Instructions"
            sections.append(f"=== {heading}: {label} ===\n{source.content}")
        return "\n\n".join(sections)
