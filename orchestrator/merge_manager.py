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

    def _unstage_input_assets(self, worktree_path: str) -> tuple[bool, str]:
        """Keep pre-existing untracked PDFs/media out of Worker commits."""
        present_assets = [
            path for path in sorted(self._untracked_input_assets)
            if (Path(worktree_path) / path).is_file()
        ]
        if not present_assets:
            return True, ""
        result = run_process(
            [
                "git", "reset", "-q", "HEAD", "--",
                *present_assets,
            ],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if result.returncode != 0:
            error = self._process_output(result)
            logger.warning("Could not unstage source assets in %s: %s",
                           worktree_path, error)
            return False, error
        return True, ""

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
        """Create an isolated worktree, avoiding stale branch/path collisions."""
        if task_id in self._active_worktrees:
            active = self._active_worktrees[task_id]
            return {
                "status": "failed",
                "phase": "worktree_preflight",
                "error": f"Task {task_id} already has an active worktree",
                "path": active.get("path", ""),
                "branch": active.get("branch", ""),
            }

        base_branch = f"ai/{job_id.lower()}/{task_id.lower()}"
        last_result = {}
        for run_number in range(1, 26):
            suffix = "" if run_number == 1 else f"-run{run_number}"
            branch = base_branch + suffix
            path_name = task_id if run_number == 1 else f"{task_id}{suffix}"
            wt_path = str(self.worktrees_base / path_name)
            result = await self.git_tools.create_worktree(branch, wt_path)
            last_result = result
            if result.get("status") == "created":
                self._copy_untracked_input_assets(wt_path)
                self._active_worktrees[task_id] = {
                    "branch": branch,
                    "path": wt_path,
                    "task_id": task_id,
                    "status": "active",
                }
                result["collision_recovered"] = run_number > 1
                result["run_number"] = run_number
                logger.info("Worktree created: %s at %s", branch, wt_path)
                return result
            error = str(result.get("error") or "")
            if not self._is_worktree_collision(error):
                result.setdefault("phase", "worktree_create")
                logger.error("Worktree creation failed for %s: %s", task_id, error)
                return result
            logger.warning(
                "Worktree name collision for %s (%s); trying a unique run suffix",
                task_id, error[:300],
            )

        return {
            "status": "failed",
            "phase": "worktree_create",
            "error": (
                "Could not allocate a unique task worktree after 25 attempts: "
                + str(last_result.get("error") or "unknown Git error")
            ),
        }

    @staticmethod
    def _is_worktree_collision(error: str) -> bool:
        normalized = str(error or "").lower()
        return any(marker in normalized for marker in (
            "already exists",
            "already checked out",
            "already registered worktree",
            "is a missing but already registered worktree",
            "path already exists",
        ))

    def preserve_worktree(self, task_id: str) -> dict:
        """Keep files on disk but release the active slot for a continuation."""
        info = self._active_worktrees.pop(task_id, None)
        if not info:
            return {"status": "not_found", "task_id": task_id}
        info = dict(info)
        info["status"] = "preserved"
        logger.info(
            "Worktree preserved for continuation: %s at %s",
            task_id, info.get("path", ""),
        )
        return info

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
            stage_result = run_process(
                ["git", "add", "-A"],
                capture_output=True, text=True, cwd=wt_path,
            )
            if stage_result.returncode != 0:
                return self._integration_failure(
                    task_id, "stage", self._process_output(stage_result)
                )

            assets_unstaged, unstage_error = self._unstage_input_assets(wt_path)
            if not assets_unstaged:
                return self._integration_failure(
                    task_id, "unstage_input_assets", unstage_error
                )

            staged_result = run_process(
                ["git", "diff", "--cached", "--name-only", "-z"],
                capture_output=True, text=True, cwd=wt_path,
            )
            if staged_result.returncode != 0:
                return self._integration_failure(
                    task_id, "inspect_staged", self._process_output(staged_result)
                )
            staged_paths = [
                item for item in staged_result.stdout.split("\0") if item
            ]
            if not staged_paths:
                return self._integration_failure(
                    task_id,
                    "commit",
                    "No task output remained staged after excluding input assets",
                )

            identity_error = self._ensure_git_identity(wt_path)
            if identity_error:
                return self._integration_failure(
                    task_id, "git_identity", identity_error
                )

            commit_result = run_process(
                ["git", "commit", "-m", commit_message],
                capture_output=True, text=True, cwd=wt_path,
            )
            if commit_result.returncode != 0:
                return self._integration_failure(
                    task_id, "commit", self._process_output(commit_result)
                )

            commit_hash_result = run_process(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, cwd=wt_path,
            )
            if commit_hash_result.returncode != 0:
                return self._integration_failure(
                    task_id, "resolve_commit",
                    self._process_output(commit_hash_result),
                )
            commit_hash = commit_hash_result.stdout.strip()

            merge_result = await self.git_tools.merge_branch(branch, self.target_branch)
            if merge_result.get("status") == "conflict":
                return await self._handle_conflict(task_id, merge_result)
            if merge_result.get("status") != "merged":
                return self._integration_failure(
                    task_id,
                    str(merge_result.get("phase") or "merge"),
                    str(merge_result.get("error") or "Git merge failed"),
                    details=merge_result,
                )

            verify_result = run_process(
                [
                    "git", "merge-base", "--is-ancestor",
                    commit_hash, self.target_branch,
                ],
                capture_output=True, text=True, cwd=self.project_root,
            )
            if verify_result.returncode != 0:
                return self._integration_failure(
                    task_id,
                    "verify_merge",
                    self._process_output(verify_result)
                    or f"Commit {commit_hash} is not reachable from {self.target_branch}",
                    details={"commit": commit_hash},
                )

            cleanup_warnings = []
            remove_result = await self.git_tools.remove_worktree(wt_path)
            if remove_result.get("status") == "removed":
                delete_result = await self.git_tools.delete_branch(branch)
                if delete_result.get("status") != "deleted":
                    cleanup_warnings.append(
                        "Branch cleanup failed: "
                        + str(delete_result.get("error") or branch)
                    )
                wt_info["status"] = "merged"
                self._active_worktrees.pop(task_id, None)
            else:
                wt_info["status"] = "merged_cleanup_pending"
                cleanup_warnings.append(
                    "Worktree cleanup failed: "
                    + str(remove_result.get("error") or wt_path)
                )

            return {
                "status": "merged",
                "task_id": task_id,
                "branch": branch,
                "into": self.target_branch,
                "commit": commit_hash,
                "staged_paths": staged_paths,
                "verified": True,
                "cleanup_warnings": cleanup_warnings,
            }

        except Exception as e:
            logger.error(f"Merge failed for {task_id}: {e}")
            return self._integration_failure(task_id, "unexpected", str(e))

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
            "worktree_path": wt_info.get("path", ""),
            "phase": "merge",
            "conflicts": conflicts,
            "error": merge_result.get("error", ""),
            "target_merge_aborted": merge_result.get("target_merge_aborted", False),
            "preserved": True,
            "message": "Merge conflicts detected — manual resolution required",
        }

    def _ensure_git_identity(self, worktree_path: str) -> str:
        """Ensure packaged Windows installs can create local task commits."""
        defaults = {
            "user.name": "RockCore",
            "user.email": "rockcore@localhost",
        }
        for name, default in defaults.items():
            current = run_process(
                ["git", "config", "--local", "--get", name],
                capture_output=True, text=True, cwd=worktree_path,
            )
            if current.returncode == 0 and current.stdout.strip():
                continue
            configured = run_process(
                ["git", "config", "--local", name, default],
                capture_output=True, text=True, cwd=worktree_path,
            )
            if configured.returncode != 0:
                return self._process_output(configured) or (
                    f"Could not configure local Git {name}"
                )
        return ""

    def _integration_failure(self, task_id: str, phase: str, error: str,
                             *, details: dict | None = None) -> dict:
        """Keep the failed worktree intact and return an actionable diagnosis."""
        wt_info = self._active_worktrees.get(task_id, {})
        wt_info["status"] = "integration_failed"
        result = {
            "status": "failed",
            "task_id": task_id,
            "phase": phase,
            "error": error or "Git integration failed",
            "branch": wt_info.get("branch", ""),
            "worktree_path": wt_info.get("path", ""),
            "preserved": True,
        }
        if details:
            result["details"] = details
        logger.error(
            "Git integration failed for %s during %s; preserved %s: %s",
            task_id, phase, result["worktree_path"], result["error"],
        )
        return result

    @staticmethod
    def _process_output(result) -> str:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        stdout = str(getattr(result, "stdout", "") or "").strip()
        return "\n".join(part for part in (stderr, stdout) if part)

    async def abort_worktree(self, task_id: str) -> dict:
        """Abort and clean up a worktree without merging."""
        wt_info = self._active_worktrees.get(task_id)
        if not wt_info:
            return {"error": f"No active worktree for {task_id}", "status": "failed"}

        await self.git_tools.remove_worktree(wt_info["path"])
        await self.git_tools.delete_branch(wt_info["branch"])
        self._active_worktrees.pop(task_id, None)
        return {"status": "aborted", "task_id": task_id}

    def get_active_worktrees(self) -> list[dict]:
        return list(self._active_worktrees.values())

    @property
    def active_count(self) -> int:
        return len(self._active_worktrees)
