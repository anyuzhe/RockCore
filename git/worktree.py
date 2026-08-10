"""Git worktree management for parallel task execution."""

import logging
from pathlib import Path
from typing import Any

from app.subprocess_utils import run_process

logger = logging.getLogger(__name__)


class WorktreeManager:
    """Manages git worktrees for parallel AI task execution."""

    def __init__(self, repo_path: str, worktrees_dir: str | None = None):
        self.repo_path = Path(repo_path).resolve()
        self.worktrees_dir = Path(worktrees_dir or self.repo_path / ".ai" / "worktrees")

    def create(self, branch: str, base: str = "main") -> dict:
        """Create a new worktree for a task branch."""
        worktree_path = self.worktrees_dir / branch
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Create branch first
        result = run_process(
            ["git", "branch", branch, base],
            capture_output=True, text=True, cwd=self.repo_path,
        )
        if result.returncode != 0 and "already exists" not in result.stderr:
            logger.warning(f"Branch creation: {result.stderr}")

        # Create worktree
        result = run_process(
            ["git", "worktree", "add", str(worktree_path), branch],
            capture_output=True, text=True, cwd=self.repo_path,
        )
        return {
            "path": str(worktree_path),
            "branch": branch,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else "",
        }

    def remove(self, branch: str) -> dict:
        """Remove a worktree and its branch."""
        worktree_path = self.worktrees_dir / branch

        run_process(
            ["git", "worktree", "remove", str(worktree_path)],
            capture_output=True, cwd=self.repo_path,
        )
        result = run_process(
            ["git", "branch", "-D", branch],
            capture_output=True, text=True, cwd=self.repo_path,
        )
        return {
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else "",
        }

    def list_worktrees(self) -> list[dict]:
        result = run_process(
            ["git", "worktree", "list"],
            capture_output=True, text=True, cwd=self.repo_path,
        )
        worktrees = []
        for line in result.stdout.strip().split("\n"):
            if line:
                parts = line.split()
                worktrees.append({
                    "path": parts[0] if len(parts) > 0 else "",
                    "branch": parts[1] if len(parts) > 1 else "",
                    "commit": parts[2] if len(parts) > 2 else "",
                })
        return worktrees
