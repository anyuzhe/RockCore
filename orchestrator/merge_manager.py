"""Merge Manager — Git Worktree lifecycle and conflict resolution for V4."""

import asyncio
import logging
import shutil
from pathlib import Path

from app.subprocess_utils import run_process
from tools.git_tools import GitTools

logger = logging.getLogger(__name__)


class MergeManager:
    """Manages git worktree lifecycle for parallel workers.

    Creates isolated worktrees per task, monitors for completion,
    auto-merges back to main, and handles conflicts.
    """

    INPUT_ASSET_SUFFIXES = {
        ".pdf", ".epub", ".mobi", ".doc", ".docx", ".ppt", ".pptx",
        ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".gif", ".webp",
        ".mp3", ".wav", ".mp4", ".mov", ".zip", ".7z", ".rar",
    }

    def __init__(self, project_root: str, worktrees_dir: str | None = None):
        self.project_root = Path(project_root).resolve()
        self.worktrees_base = Path(worktrees_dir or self.project_root / ".ai" / "worktrees")
        self.worktrees_base.mkdir(parents=True, exist_ok=True)
        self.git_tools = GitTools(str(self.project_root))
        try:
            current = run_process(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, cwd=self.project_root,
            )
            self.target_branch = current.stdout.strip() if current.returncode == 0 else "main"
        except OSError:
            self.target_branch = "main"
        self._untracked_input_assets = self._find_untracked_input_assets()
        self._active_worktrees: dict[str, dict] = {}
        self._merge_lock = asyncio.Lock()

    def _find_untracked_input_assets(self) -> set[str]:
        """Remember user source assets that must not become task outputs."""
        try:
            result = run_process(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                capture_output=True, text=True, cwd=self.project_root,
            )
            if result.returncode != 0:
                return set()
            return {
                path for path in result.stdout.split("\0") if path
                and Path(path).suffix.lower() in self.INPUT_ASSET_SUFFIXES
            }
        except (OSError, ValueError):
            return set()

    def _unstage_input_assets(self, worktree_path: str) -> None:
        """Keep pre-existing untracked PDFs/media out of Worker commits."""
        present_assets = [
            path for path in sorted(self._untracked_input_assets)
            if (Path(worktree_path) / path).is_file()
        ]
        if not present_assets:
            return
        result = run_process(
            [
                "git", "reset", "-q", "HEAD", "--",
                *present_assets,
            ],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if result.returncode != 0:
            logger.warning(
                "Could not unstage source assets in %s: %s",
                worktree_path, result.stderr.strip(),
            )

    def _copy_untracked_input_assets(self, worktree_path: str) -> None:
        """Make local source documents readable inside an isolated worktree."""
        worktree = Path(worktree_path).resolve()
        for relative in sorted(self._untracked_input_assets):
            source = (self.project_root / relative).resolve()
            destination = (worktree / relative).resolve()
            try:
                source.relative_to(self.project_root)
                destination.relative_to(worktree)
            except ValueError:
                logger.warning("Skipped unsafe input asset path: %s", relative)
                continue
            if not source.is_file() or source.is_symlink():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            except OSError as error:
                logger.warning(
                    "Could not copy input asset %s into worktree: %s",
                    relative, error,
                )

    async def create_task_worktree(self, task_id: str, job_id: str) -> dict:
        """Create an isolated worktree for a task."""
        branch = f"ai/{job_id.lower()}/{task_id.lower()}"
        wt_path = str(self.worktrees_base / task_id)

        result = await self.git_tools.create_worktree(branch, wt_path)
        if result.get("status") == "created":
            self._copy_untracked_input_assets(wt_path)
            self._active_worktrees[task_id] = {
                "branch": branch,
                "path": wt_path,
                "task_id": task_id,
                "status": "active",
            }
            logger.info(f"Worktree created: {branch} at {wt_path}")
        return result

    async def commit_and_merge(self, task_id: str, commit_message: str) -> dict:
        """Commit changes in worktree and merge back to main."""
        async with self._merge_lock:
            return await self._commit_and_merge_locked(task_id, commit_message)

    async def _commit_and_merge_locked(self, task_id: str,
                                       commit_message: str) -> dict:
        """Serialize mutations of the shared target branch."""
        wt_info = self._active_worktrees.get(task_id)
        if not wt_info:
            return {"error": f"No active worktree for task {task_id}", "status": "failed"}

        wt_path = wt_info["path"]
        branch = wt_info["branch"]

        try:
            # Stage and commit in worktree
            run_process(
                ["git", "add", "-A"],
                capture_output=True, text=True, cwd=wt_path,
            )
            self._unstage_input_assets(wt_path)
            commit_result = run_process(
                ["git", "commit", "-m", commit_message],
                capture_output=True, text=True, cwd=wt_path,
            )
            if commit_result.returncode != 0 and "nothing to commit" not in commit_result.stderr:
                if "nothing to commit" not in commit_result.stdout:
                    logger.warning(f"Commit warning for {task_id}: {commit_result.stderr}")

            # Merge back to main repo
            merge_result = await self.git_tools.merge_branch(branch, self.target_branch)
            if merge_result.get("status") == "conflict":
                return await self._handle_conflict(task_id, merge_result)

            # Clean up worktree
            await self.git_tools.remove_worktree(wt_path)
            await self.git_tools.delete_branch(branch)
            wt_info["status"] = "merged"
            self._active_worktrees.pop(task_id, None)

            return {"status": "merged", "task_id": task_id, "branch": branch}

        except Exception as e:
            logger.error(f"Merge failed for {task_id}: {e}")
            return {"error": str(e), "status": "failed", "task_id": task_id}

    async def _handle_conflict(self, task_id: str, merge_result: dict) -> dict:
        """Handle merge conflicts — flag for user resolution."""
        wt_info = self._active_worktrees.get(task_id, {})
        conflicts = merge_result.get("conflicts", [])
        logger.warning(f"Merge conflict in task {task_id}: {conflicts}")

        wt_info["status"] = "conflict"
        return {
            "status": "conflict",
            "task_id": task_id,
            "branch": wt_info.get("branch", ""),
            "conflicts": conflicts,
            "message": "Merge conflicts detected — manual resolution required",
        }

    async def abort_worktree(self, task_id: str) -> dict:
        """Abort and clean up a worktree without merging."""
        wt_info = self._active_worktrees.get(task_id)
        if not wt_info:
            return {"error": f"No active worktree for {task_id}", "status": "failed"}

        try:
            # Abort any in-progress merge
            run_process(
                ["git", "merge", "--abort"],
                capture_output=True, text=True, cwd=self.project_root,
            )
        except Exception:
            pass

        await self.git_tools.remove_worktree(wt_info["path"])
        await self.git_tools.delete_branch(wt_info["branch"])
        self._active_worktrees.pop(task_id, None)
        return {"status": "aborted", "task_id": task_id}

    def get_active_worktrees(self) -> list[dict]:
        return list(self._active_worktrees.values())

    @property
    def active_count(self) -> int:
        return len(self._active_worktrees)
