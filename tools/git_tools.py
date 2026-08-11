"""Git operation tools for the AI worker."""

import os
from pathlib import Path
from typing import Any

from app.subprocess_utils import run_process


class GitTools:
    """Git operations limited to the project repository."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    async def git_status(self) -> dict:
        """Get current git status."""
        try:
            result = run_process(
                ["git", "status", "--short"],
                capture_output=True, text=True, cwd=self.project_root
            )
            branch = run_process(
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
            cmd = ["git", "diff", "--cached"] if staged else ["git", "diff"]
            result = run_process(cmd, capture_output=True, text=True,
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
            result = run_process(
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
            stage = run_process(
                ["git", "add", "-A"], capture_output=True, text=True,
                cwd=self.project_root,
            )
            if stage.returncode != 0:
                return {
                    "error": self._process_error(stage),
                    "status": "failed",
                    "phase": "stage",
                }
            result = run_process(
                ["git", "commit", "-m", message],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {
                    "error": self._process_error(result),
                    "status": "failed",
                    "phase": "commit",
                }
            return {"hash": result.stdout.strip(), "status": "committed"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def checkout(self, branch: str) -> dict:
        """Checkout a branch."""
        try:
            result = run_process(
                ["git", "checkout", branch],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {"branch": branch, "status": "checked_out"}
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    # ── Worktree operations (V4) ────────────────────────────────

    async def create_worktree(self, branch_name: str, worktree_path: str,
                              start_point: str = "HEAD") -> dict:
        """Create a new git worktree, optionally from a continuation ref."""
        try:
            result = run_process(
                [
                    "git", "worktree", "add", "-b", branch_name,
                    worktree_path, start_point or "HEAD",
                ],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                return {"error": result.stderr, "status": "failed"}
            return {
                "branch": branch_name, "path": worktree_path,
                "status": "created", "start_point": start_point or "HEAD",
            }
        except Exception as e:
            return {"error": str(e), "status": "failed"}

    async def remove_worktree(self, worktree_path: str) -> dict:
        """Remove a git worktree."""
        try:
            result = run_process(
                ["git", "worktree", "remove", worktree_path],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                # Force remove if clean failed
                result = run_process(
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
            checkout = run_process(
                ["git", "checkout", target_branch],
                capture_output=True, text=True, cwd=self.project_root
            )
            if checkout.returncode != 0:
                return {
                    "status": "failed",
                    "phase": "checkout_target",
                    "error": self._process_error(checkout),
                    "branch": source_branch,
                    "into": target_branch,
                }
            merge_head = run_process(
                ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                capture_output=True, text=True, cwd=self.project_root,
            )
            if merge_head.returncode == 0:
                return {
                    "status": "failed",
                    "phase": "merge_preflight",
                    "error": "Target repository already has an in-progress merge",
                    "branch": source_branch,
                    "into": target_branch,
                }
            result = run_process(
                ["git", "merge", source_branch, "--no-edit"],
                capture_output=True, text=True, cwd=self.project_root
            )
            if result.returncode != 0:
                conflicts = self._detect_conflicts()
                active_merge = run_process(
                    ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"],
                    capture_output=True, text=True, cwd=self.project_root,
                )
                merge_aborted = False
                if active_merge.returncode == 0:
                    abort_result = run_process(
                        ["git", "merge", "--abort"],
                        capture_output=True, text=True, cwd=self.project_root,
                    )
                    merge_aborted = abort_result.returncode == 0
                return {
                    "status": "conflict" if conflicts else "failed",
                    "phase": "merge",
                    "error": self._process_error(result),
                    "conflicts": conflicts,
                    "branch": source_branch,
                    "into": target_branch,
                    "target_merge_aborted": merge_aborted,
                }
            return {
                "status": "merged",
                "branch": source_branch,
                "into": target_branch,
                "output": self._process_error(result),
            }
        except Exception as e:
            return {"error": str(e), "status": "failed", "phase": "merge"}

    async def delete_branch(self, branch_name: str) -> dict:
        """Delete a local branch."""
        try:
            result = run_process(
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
            result = run_process(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                capture_output=True, text=True, cwd=self.project_root
            )
            return [f.strip() for f in result.stdout.split("\n") if f.strip()]
        except Exception:
            return []

    @staticmethod
    def _process_error(result: Any) -> str:
        """Return useful Git output for both stdout-only and stderr failures."""
        stdout = str(getattr(result, "stdout", "") or "").strip()
        stderr = str(getattr(result, "stderr", "") or "").strip()
        return "\n".join(part for part in (stderr, stdout) if part)
