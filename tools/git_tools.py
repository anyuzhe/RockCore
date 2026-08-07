"""Git operation tools for the AI worker."""

import os
from pathlib import Path
from typing import Any


class GitTools:
    """Git operations limited to the project repository."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    async def git_status(self) -> dict:
        """Get current git status."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "status", "--short"],
                capture_output=True, text=True, cwd=self.project_root
            )
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=self.project_root
            )
            return {
                "status": result.stdout or "(clean)",
                "branch": branch.stdout.strip() if branch.stdout else "unknown",
                "is_clean": not bool(result.stdout.strip()),
            }
        except Exception as e:
            return {"error": str(e), "status": "", "branch": "unknown", "is_clean": True}

    async def git_diff(self, staged: bool = False) -> dict:
        """Get git diff."""
        try:
            import subprocess
            cmd = ["git", "diff", "--cached"] if staged else ["git", "diff"]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    cwd=self.project_root)
            return {
                "diff": result.stdout or "(no changes)",
                "has_changes": bool(result.stdout.strip()),
            }
        except Exception as e:
            return {"error": str(e), "diff": "", "has_changes": False}

    async def create_branch(self, branch_name: str) -> dict:
        """Create a new branch from current HEAD."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "checkout", "-b", branch_name],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"branch": branch_name, "status": "created"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def commit(self, message: str) -> dict:
        """Stage all and commit."""
        try:
            import subprocess
            subprocess.run(["git", "add", "-A"], capture_output=True,
                           cwd=self.project_root)
            result = subprocess.run(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"hash": result.stdout.strip(), "status": "committed"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def checkout(self, branch: str) -> dict:
        """Checkout a branch."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "checkout", branch],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"branch": branch, "status": "checked_out"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    # ── Worktree operations (V4) ────────────────────────────────

    async def create_worktree(self, branch_name: str, worktree_path: str) -> dict:
        """Create a new git worktree with a branch."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "worktree", "add", "-b", branch_name, worktree_path, "HEAD"],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"branch": branch_name, "path": worktree_path, "status": "created"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def remove_worktree(self, worktree_path: str) -> dict:
        """Remove a git worktree."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "worktree", "remove", worktree_path],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                # Force remove if clean failed
                result = subprocess.run(
                    ["git", "worktree", "remove", "--force", worktree_path],
                    capture_output=True, text=True, cwd=self.project_root
                )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"path": worktree_path, "status": "removed"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def merge_branch(self, source_branch: str, target_branch: str = "main") -> dict:
        """Merge source_branch into target_branch."""
        try:
            import subprocess
            # Checkout target
            subprocess.run(
                ["git", "checkout", target_branch],
                capture_output=True, text=True, cwd=self.project_root
            )
            # Merge source
            result = subprocess.run(
                ["git", "merge", source_branch, "--no-edit"],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                # Conflict detected
                return {
                    "status": "conflict",
                    "error": result.stderr,
                    "conflicts": self._detect_conflicts(),
                }
            return {"status": "merged", "branch": source_branch, "into": target_branch}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def delete_branch(self, branch_name: str) -> dict:
        """Delete a local branch."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "branch", "-D", branch_name],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"branch": branch_name, "status": "deleted"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    def _detect_conflicts(self) -> list[str]:
        """Detect files with merge conflicts."""
        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                capture_output=True, text=True, cwd=self.project_root
            )
            return [f.strip() for f in result.stdout.split("\n") if f.strip()]
        except Exception:
            return []