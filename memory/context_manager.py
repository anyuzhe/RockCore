"""Context Manager — prunes context to relevant files for each task for V5."""

import logging
from pathlib import Path
from typing import Any

from .project_memory import ProjectMemory
from .repo_map import RepoMap

logger = logging.getLogger(__name__)


class ContextManager:
    """Builds task-specific context by pruning the project to relevant files.

    Uses the repository map and project memory to inject only the most
    relevant context for each task, reducing token usage and improving focus.
    """

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.project_memory = ProjectMemory(str(self.project_root))
        self.repo_map = RepoMap(str(self.project_root))

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
        self.project_memory = ProjectMemory(str(new_root_path))
        self.repo_map = RepoMap(str(new_root_path))
        self.repo_map.update()
        logger.info(f"ContextManager switched to project: {new_root}")

    async def build_task_context(self, task) -> str:
        """Build optimized context for a specific task."""
        parts = []

        # Project memory summary
        memory_summary = self.project_memory.get_context_summary()
        if memory_summary:
            parts.append("=== Project Knowledge ===\n" + memory_summary[:1800])

        # Repository map summary
        if self.repo_map.is_loaded:
            parts.append(
                "=== Repository Map ===\n"
                + self.repo_map.get_context_summary()[:1800]
            )

        # Task-specific file context
        relevant_files = self._find_relevant_files(task)
        if relevant_files:
            parts.append("=== Relevant Files ===\n" + "\n".join(f"- {f}" for f in relevant_files))

        return "\n\n".join(parts)[:4000]

    def _find_relevant_files(self, task) -> list[str]:
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
                        relevant.add(str(f.relative_to(self.project_root)))
            except Exception as e:
                logger.warning(f"Glob pattern '{pattern}' failed: {e}")

        # For coding tasks, add source files
        if task.task_type == "coding" and not exact_allowed:
            source_files = self.repo_map.get_category_files("source")
            relevant.update(source_files[:10])  # Limit to 10 source files

        # For testing tasks, add test files
        if task.task_type == "testing":
            test_files = self.repo_map.get_category_files("test")
            relevant.update(test_files[:10])

        # For analysis tasks, add all source and config
        if task.task_type == "analysis":
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
        parts.append(self.project_memory.get_context_summary())
        if self.repo_map.is_loaded:
            parts.append(self.repo_map.get_context_summary())
        return "\n\n".join(parts)
