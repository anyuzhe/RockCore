"""Context Manager — prunes context to relevant files for each task for V5."""

import logging
from pathlib import Path
from typing import Any

from app.paths import project_state_dir
from .project_memory import ProjectMemory
from .repo_map import RepoMap
from .instruction_resolver import InstructionResolver

logger = logging.getLogger(__name__)


class ContextManager:
    """Builds task-specific context by pruning the project to relevant files.

    Uses the repository map and project memory to inject only the most
    relevant context for each task, reducing token usage and improving focus.
    """

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.state_dir = project_state_dir(self.project_root)
        self.project_memory = ProjectMemory(
            str(self.project_root), state_dir=self.state_dir
        )
        self.repo_map = RepoMap(
            str(self.project_root), state_dir=self.state_dir
        )
        self.project_surface: dict[str, Any] = {}
        self.instructions = InstructionResolver(self.project_root)

    async def initialize(self):
        """Initialize the context manager and build the repo map."""
        self.repo_map.update()
        logger.info("ContextManager initialized")

    async def switch_project(self, new_root: str):
        """Switch to a different project root for memory/context.
        Called when the engine starts a job on a different project."""
        new_root_path = Path(new_root).resolve()
        if new_root_path == self.project_root:
            # A previous job may have added, removed, or moved files while the
            # selected project stayed the same.
            self.repo_map.update()
            return
        self.project_root = new_root_path
        self.state_dir = project_state_dir(new_root_path)
        self.project_memory = ProjectMemory(
            str(new_root_path), state_dir=self.state_dir
        )
        self.repo_map = RepoMap(
            str(new_root_path), state_dir=self.state_dir
        )
        self.project_surface = {}
        self.instructions = InstructionResolver(self.project_root)
        self.repo_map.update()
        logger.info(f"ContextManager switched to project: {new_root}")

    def set_project_surface(self, surface: dict | None):
        """Set the deterministic runtime surface shared by Planner and Worker."""
        self.project_surface = dict(surface or {})

    async def build_task_context(self, task) -> str:
        """Build optimized context for a specific task."""
        parts = []

        fixed_context = str(
            getattr(task, "_rockcore_fixed_context", "") or ""
        ).strip()
        if fixed_context:
            parts.append(fixed_context)

        instruction_context = self.instructions.render(
            self._instruction_working_directory(task)
        )
        if instruction_context:
            parts.append(instruction_context)

        surface = dict(
            getattr(task, "_rockcore_project_surface", None)
            or self.project_surface
            or {}
        )
        if surface:
            active = list(surface.get("active_files") or [])
            support = list(surface.get("support_files") or [])
            legacy = list(surface.get("legacy_files") or [])
            entrypoints = [
                str(item.get("path"))
                for item in (surface.get("entrypoints") or [])
                if isinstance(item, dict) and item.get("path")
            ]
            lines = ["=== Active Project Surface ==="]
            if entrypoints:
                lines.append("Entrypoints: " + ", ".join(entrypoints[:8]))
            if active:
                lines.append("Runtime files (authoritative):")
                lines.extend(f"- {path}" for path in active[:30])
            if support:
                lines.append("Supporting tests/configuration:")
                lines.extend(f"- {path}" for path in support[:20])
            if legacy:
                lines.append(
                    "Unreferenced/legacy files (do not edit unless the task explicitly targets them):"
                )
                lines.extend(f"- {path}" for path in legacy[:20])
            for ambiguity in (surface.get("ambiguities") or [])[:6]:
                lines.append("Ambiguity: " + str(ambiguity))
            parts.append("\n".join(lines))

        # Project memory follows the active surface so the authoritative runtime
        # cannot be truncated by older, lower-priority repository information.
        memory_summary = self.project_memory.get_context_summary()
        if memory_summary:
            parts.append("=== Project Knowledge ===\n" + memory_summary[:1200])

        if self.repo_map.is_loaded:
            parts.append(
                "=== Repository Map ===\n"
                + self.repo_map.get_context_summary()[:1200]
            )

        # Task-specific file context
        relevant_files = self._find_relevant_files(task, surface=surface)
        if relevant_files:
            parts.append("=== Relevant Files ===\n" + "\n".join(f"- {f}" for f in relevant_files))

        # Fixed session state and repository rules must never disappear because
        # lower-priority repository summaries are large. Each optional section
        # is already bounded independently.
        return "\n\n".join(parts)

    def _instruction_working_directory(self, task) -> Path:
        """Resolve the deepest concrete task path for nested instructions."""
        candidates = []
        for raw in (getattr(task, "allowed_paths", None) or []):
            value = str(raw or "").replace("\\", "/").strip("/")
            if not value or any(char in value for char in "*?["):
                continue
            candidate = (self.project_root / value).resolve()
            if candidate.is_file() or candidate.suffix:
                candidate = candidate.parent
            try:
                candidate.relative_to(self.project_root)
            except ValueError:
                continue
            candidates.append(candidate)
        return max(candidates, key=lambda path: len(path.parts), default=self.project_root)

    def _find_relevant_files(self, task, surface: dict | None = None) -> list[str]:
        """Find files relevant to a task based on task type and description."""
        relevant = set()
        exact_allowed = False

        # Add files from allowed paths
        for pattern in (task.allowed_paths or []):
            if not any(char in pattern for char in "*?["):
                exact_allowed = True
            try:
                for f in self.project_root.rglob(pattern):
                    if f.is_file():
                        relevant.add(f.relative_to(self.project_root).as_posix())
            except Exception as e:
                logger.warning(f"Glob pattern '{pattern}' failed: {e}")

        active_files = list((surface or {}).get("active_files") or [])
        support_files = list((surface or {}).get("support_files") or [])

        # Runtime reachability is authoritative when the task has a broad scope.
        if active_files and not exact_allowed:
            relevant.update(active_files[:24])

        # For coding tasks, add source files only when no active surface exists.
        if task.task_type == "coding" and not exact_allowed:
            if not active_files:
                source_files = self.repo_map.get_category_files("source")
                relevant.update(source_files[:10])  # Limit to 10 source files

        # For testing tasks, add test files
        if task.task_type == "testing":
            if support_files:
                relevant.update(support_files[:16])
            else:
                test_files = self.repo_map.get_category_files("test")
                relevant.update(test_files[:10])

        # For analysis tasks, add all source and config
        if task.task_type == "analysis" and not active_files:
            for cat in ("source", "config"):
                relevant.update(self.repo_map.get_category_files(cat)[:15])

        return sorted(relevant)[:12]

    async def update_after_task(self, task, result: dict):
        """Update project memory and repo map after a task completes."""
        # Append to known issues if task failed
        if result.get("status") == "failed":
            error = result.get("error", "Unknown error")
            self.project_memory.append_memory(
                "known_issues",
                f"Task {task.task_id} ({task.title}): {error}",
            )

        # Update repo map if files changed
        changes = result.get("changes", [])
        if changes:
            self.repo_map.update()

    def get_full_context(self) -> str:
        """Get full project context (for Planner)."""
        parts = []
        instruction_context = self.instructions.render()
        if instruction_context:
            parts.append(instruction_context)
        parts.append(self.project_memory.get_context_summary())
        if self.repo_map.is_loaded:
            parts.append(self.repo_map.get_context_summary())
        if self.project_surface:
            parts.append(
                "Active runtime files: "
                + ", ".join(self.project_surface.get("active_files") or [])
            )
        return "\n\n".join(parts)
