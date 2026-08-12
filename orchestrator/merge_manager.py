"""Merge Manager — Git Worktree lifecycle and conflict resolution for V4."""

import asyncio
import hashlib
import logging
import os
import shutil
import uuid
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
        self._untracked_baseline = self._snapshot_untracked_files()
        self._untracked_input_assets = {
            path for path in self._untracked_baseline
            if Path(path).suffix.lower() in self.INPUT_ASSET_SUFFIXES
        }
        self._active_worktrees: dict[str, dict] = {}
        self._merge_lock = asyncio.Lock()

    @staticmethod
    def _file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _snapshot_untracked_files(self) -> dict[str, str]:
        """Record pre-existing user files so unchanged copies are never outputs."""
        try:
            result = run_process(
                ["git", "ls-files", "--others", "--exclude-standard", "-z"],
                capture_output=True, text=True, cwd=self.project_root,
            )
            if result.returncode != 0:
                return {}
            snapshot = {}
            for relative in (path for path in result.stdout.split("\0") if path):
                if len(snapshot) >= 500:
                    break
                source = (self.project_root / relative).resolve()
                try:
                    source.relative_to(self.project_root)
                    if (
                        not source.is_file() or source.is_symlink()
                        or source.stat().st_size > 128 * 1024 * 1024
                    ):
                        continue
                    snapshot[relative] = self._file_digest(source)
                except (OSError, ValueError):
                    continue
            return snapshot
        except (OSError, ValueError):
            return {}

    def _unstage_unchanged_preserved_files(
        self, worktree_path: str, preserved_paths: set[str],
    ) -> tuple[bool, str]:
        """Unstage only preserved user files whose bytes are still unchanged."""
        unchanged = []
        for relative in sorted(preserved_paths):
            path = Path(worktree_path) / relative
            expected = self._untracked_baseline.get(relative)
            try:
                if path.is_file() and expected and self._file_digest(path) == expected:
                    unchanged.append(relative)
            except OSError:
                continue
        if not unchanged:
            return True, ""
        result = run_process(
            [
                "git", "reset", "-q", "HEAD", "--",
                *unchanged,
            ],
            capture_output=True, text=True, cwd=worktree_path,
        )
        if result.returncode != 0:
            error = self._process_output(result)
            logger.warning("Could not unstage source assets in %s: %s",
                           worktree_path, error)
            return False, error
        return True, ""

    def _copy_preserved_files(self, worktree_path: str,
                              include_all: bool = False) -> set[str]:
        """Make inputs/checkpoint artifacts available without overwriting history."""
        worktree = Path(worktree_path).resolve()
        candidates = (
            set(self._untracked_baseline) if include_all
            else set(self._untracked_input_assets)
        )
        copied = set()
        for relative in sorted(candidates):
            source = (self.project_root / relative).resolve()
            destination = (worktree / relative).resolve()
            try:
                source.relative_to(self.project_root)
                destination.relative_to(worktree)
            except ValueError:
                logger.warning("Skipped unsafe input asset path: %s", relative)
                continue
            if (
                not source.is_file() or source.is_symlink()
                or destination.exists()
            ):
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied.add(relative)
            except OSError as error:
                logger.warning(
                    "Could not copy input asset %s into worktree: %s",
                    relative, error,
                )
        return copied

    def _resolve_continuation_state(self, source_job_id: str,
                                    task_id: str = "") -> dict:
        """Locate the newest branch/worktree belonging to the source job."""
        source = str(source_job_id or "").strip().lower()
        if not source:
            return {}
        result = run_process(
            [
                "git", "for-each-ref", "--sort=-committerdate",
                "--format=%(refname:short)", f"refs/heads/ai/{source}/*",
            ],
            capture_output=True, text=True, cwd=self.project_root,
        )
        branches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not branches:
            return {}
        worktrees = run_process(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=self.project_root,
        )
        current_path = ""
        worktree_by_branch = {}
        for line in worktrees.stdout.splitlines():
            if line.startswith("worktree "):
                current_path = line[9:].strip()
            elif line.startswith("branch refs/heads/"):
                worktree_by_branch[line[18:].strip()] = current_path
        task_segment = f"/{str(task_id or '').strip().lower()}"
        matching = [
            branch for branch in branches
            if task_segment and (
                branch.endswith(task_segment)
                or f"{task_segment}-run" in branch
            )
        ]
        candidates = matching or branches
        branch = candidates[0]
        for candidate in candidates:
            candidate_path = worktree_by_branch.get(candidate, "")
            if not candidate_path:
                continue
            dirty = run_process(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=candidate_path,
            )
            if dirty.returncode == 0 and dirty.stdout.strip():
                branch = candidate
                break
        state = {
            "ref": branch, "branch": branch,
            "worktree_path": worktree_by_branch.get(branch, ""),
        }
        return state

    def _overlay_continuation_changes(self, source_path: str,
                                      destination_path: str) -> list[str]:
        """Copy modified/untracked checkpoint files from a preserved worktree."""
        if not source_path:
            return []
        source_root = Path(source_path).resolve()
        destination_root = Path(destination_path).resolve()
        if not source_root.is_dir() or source_root == destination_root:
            return []
        result = run_process(
            ["git", "ls-files", "-m", "-o", "--exclude-standard", "-z"],
            capture_output=True, text=True, cwd=source_root,
        )
        copied = []
        for relative in (item for item in result.stdout.split("\0") if item):
            source = (source_root / relative).resolve()
            destination = (destination_root / relative).resolve()
            try:
                source.relative_to(source_root)
                destination.relative_to(destination_root)
            except ValueError:
                continue
            if not source.is_file() or source.is_symlink():
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                copied.append(relative)
            except OSError as error:
                logger.warning("Could not restore checkpoint file %s: %s", relative, error)
        return copied

    def _continuation_committed_files(self, start_point: str) -> list[str]:
        """List source-branch outputs not yet present on the target branch."""
        if not start_point or start_point == "HEAD":
            return []
        result = run_process(
            [
                "git", "diff", "--name-only", "-z",
                self.target_branch, start_point,
            ],
            capture_output=True, text=True, cwd=self.project_root,
        )
        if result.returncode != 0:
            return []
        return [item for item in result.stdout.split("\0") if item]

    async def create_task_worktree(self, task_id: str, job_id: str,
                                   source_job_id: str = "") -> dict:
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

        continuation = self._resolve_continuation_state(source_job_id, task_id)
        start_point = continuation.get("ref") or "HEAD"
        base_branch = f"ai/{job_id.lower()}/{task_id.lower()}"
        last_result = {}
        for run_number in range(1, 26):
            suffix = "" if run_number == 1 else f"-run{run_number}"
            branch = base_branch + suffix
            path_name = task_id if run_number == 1 else f"{task_id}{suffix}"
            wt_path = str(self.worktrees_base / path_name)
            if self._worktree_slot_conflicts(branch, wt_path):
                last_result = {
                    "status": "failed",
                    "error": f"Branch or worktree slot already exists: {branch}",
                }
                logger.warning(
                    "Worktree slot already occupied for %s; trying run suffix %s",
                    task_id, run_number + 1,
                )
                continue
            try:
                result = await self.git_tools.create_worktree(
                    branch, wt_path, start_point=start_point
                )
            except TypeError:
                # Compatibility for small test doubles and third-party Git adapters.
                result = await self.git_tools.create_worktree(branch, wt_path)
            last_result = result
            if result.get("status") == "created":
                preserved = self._copy_preserved_files(
                    wt_path, include_all=bool(source_job_id)
                )
                resumed_files = self._overlay_continuation_changes(
                    continuation.get("worktree_path", ""), wt_path
                )
                resumed_files = list(dict.fromkeys(
                    self._continuation_committed_files(start_point)
                    + resumed_files
                ))
                self._active_worktrees[task_id] = {
                    "branch": branch,
                    "path": wt_path,
                    "task_id": task_id,
                    "status": "active",
                    "preserved_paths": preserved,
                    "resumed_from": continuation.get("branch", ""),
                    "resumed_files": resumed_files,
                }
                result["collision_recovered"] = run_number > 1
                result["run_number"] = run_number
                result["resumed_from"] = continuation.get("branch", "")
                result["resumed_files"] = resumed_files
                logger.info("Worktree created: %s at %s", branch, wt_path)
                return result
            error = str(result.get("error") or "")
            if not (
                self._worktree_slot_conflicts(branch, wt_path)
                or self._is_worktree_collision(error)
            ):
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

    def _worktree_slot_conflicts(self, branch: str, worktree_path: str) -> bool:
        """Detect occupied branches/paths without depending on localized errors."""
        branch_ref = f"refs/heads/{branch}"
        try:
            branch_result = run_process(
                ["git", "show-ref", "--verify", "--quiet", branch_ref],
                capture_output=True, cwd=self.project_root,
            )
            if branch_result.returncode == 0:
                return True
        except OSError:
            pass
        if os.path.lexists(worktree_path):
            return True

        try:
            listed = run_process(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, cwd=self.project_root,
            )
        except OSError:
            return False
        if listed.returncode != 0:
            return False
        expected_path = os.path.normcase(os.path.normpath(str(
            Path(worktree_path).expanduser().resolve()
        )))
        for line in listed.stdout.splitlines():
            if line == f"branch {branch_ref}":
                return True
            if not line.startswith("worktree "):
                continue
            registered = os.path.normcase(os.path.normpath(str(
                Path(line[9:].strip()).expanduser().resolve()
            )))
            if registered == expected_path:
                return True
        return False

    @staticmethod
    def _is_worktree_collision(error: str) -> bool:
        normalized = str(error or "").lower()
        return any(marker in normalized for marker in (
            "already exists",
            "already checked out",
            "already registered worktree",
            "is a missing but already registered worktree",
            "path already exists",
            "已经存在",
            "已存在",
            "已被检出",
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

            assets_unstaged, unstage_error = self._unstage_unchanged_preserved_files(
                wt_path, set(wt_info.get("preserved_paths") or set())
            )
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
            reuse_existing_commit = False
            if not staged_paths:
                staged_paths = self._continuation_committed_files(branch)
                reuse_existing_commit = bool(staged_paths)
                if not reuse_existing_commit:
                    return self._integration_failure(
                        task_id,
                        "commit",
                        "No task output remained staged after excluding input assets",
                    )

            if not reuse_existing_commit:
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

            preflight = self._preflight_untracked_collisions(
                task_id, staged_paths, wt_path
            )
            if preflight.get("status") == "pending_merge":
                wt_info["status"] = "pending_merge"
                return preflight
            if preflight.get("status") == "failed":
                return self._integration_failure(
                    task_id, "merge_preflight",
                    str(preflight.get("error") or "Merge preflight failed"),
                    details=preflight,
                )

            merge_result = await self.git_tools.merge_branch(branch, self.target_branch)
            if merge_result.get("status") == "conflict":
                self._restore_preflight_backups(preflight)
                return await self._handle_conflict(task_id, merge_result)
            if merge_result.get("status") != "merged":
                self._restore_preflight_backups(preflight)
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
                "preflight": preflight,
            }

        except Exception as e:
            logger.error(f"Merge failed for {task_id}: {e}")
            if "preflight" in locals():
                self._restore_preflight_backups(preflight)
            return self._integration_failure(task_id, "unexpected", str(e))

    def _preflight_untracked_collisions(self, task_id: str,
                                        staged_paths: list[str],
                                        worktree_path: str) -> dict:
        """Resolve target collisions without asking the user to operate Git."""
        result = run_process(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True, text=True, cwd=self.project_root,
        )
        if result.returncode != 0:
            return {"status": "failed", "error": self._process_output(result)}
        target_untracked = {item for item in result.stdout.split("\0") if item}
        collisions = sorted(set(staged_paths).intersection(target_untracked))
        if not collisions:
            return {"status": "clear", "identical": [], "different": []}
        identical = []
        different = []
        for relative in collisions:
            target = self.project_root / relative
            source = Path(worktree_path) / relative
            try:
                if (
                    target.is_file() and source.is_file()
                    and self._file_digest(target) == self._file_digest(source)
                ):
                    identical.append(relative)
                else:
                    different.append(relative)
            except OSError:
                different.append(relative)
        backups = []
        if different:
            backup_result = self._backup_and_clear_untracked_collisions(
                task_id, different
            )
            if backup_result.get("status") != "resolved":
                return backup_result
            backups = list(backup_result.get("backups") or [])
        if identical:
            identity_error = self._ensure_git_identity(str(self.project_root))
            if identity_error:
                self._restore_preflight_backups({"backups": backups})
                return {"status": "failed", "error": identity_error}
            stage = run_process(
                ["git", "add", "--", *identical],
                capture_output=True, text=True, cwd=self.project_root,
            )
            if stage.returncode != 0:
                self._restore_preflight_backups({"backups": backups})
                return {
                    "status": "failed", "error": self._process_output(stage)
                }
            commit = run_process(
                [
                    "git", "commit", "--only", "-m",
                    f"RockCore preflight: adopt identical outputs for {task_id}",
                    "--", *identical,
                ],
                capture_output=True, text=True, cwd=self.project_root,
            )
            if commit.returncode != 0:
                self._restore_preflight_backups({"backups": backups})
                return {
                    "status": "failed", "error": self._process_output(commit)
                }
        return {
            "status": "resolved",
            "identical": identical,
            "different": different,
            "backups": backups,
            "strategy": "backup_target_then_apply_task_output",
        }

    def _backup_and_clear_untracked_collisions(
        self, task_id: str, relative_paths: list[str]
    ) -> dict:
        """Back up user-visible files before task output takes their place."""
        safe_task_id = "".join(
            char if char.isalnum() or char in "-_" else "_"
            for char in str(task_id or "task")
        )
        recovery_root = (
            self.worktrees_base.parent / "recovery" / safe_task_id
            / uuid.uuid4().hex
        ).resolve()
        backups = []
        try:
            for relative in relative_paths:
                source = (self.project_root / relative).resolve()
                backup = (recovery_root / relative).resolve()
                source.relative_to(self.project_root)
                backup.relative_to(recovery_root)
                if not source.is_file() or source.is_symlink():
                    raise OSError(
                        f"Cannot safely preserve target collision: {relative}"
                    )
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, backup)
                if self._file_digest(source) != self._file_digest(backup):
                    raise OSError(f"Backup verification failed: {relative}")
                source.unlink()
                backups.append({
                    "path": relative,
                    "backup": str(backup),
                })
        except (OSError, ValueError) as error:
            self._restore_preflight_backups({"backups": backups})
            return {
                "status": "failed",
                "phase": "merge_preflight_backup",
                "task_id": task_id,
                "worktree_path": self._active_worktrees.get(
                    task_id, {}
                ).get("path", ""),
                "preserved": True,
                "conflicts": relative_paths,
                "error": str(error),
                "backups": backups,
            }
        return {
            "status": "resolved",
            "backups": backups,
            "recovery_root": str(recovery_root),
        }

    def _restore_preflight_backups(self, preflight: dict) -> list[str]:
        """Restore originals when integration stopped before producing a file."""
        restored = []
        for record in preflight.get("backups") or []:
            relative = str(record.get("path") or "")
            backup_value = str(record.get("backup") or "")
            if not relative or not backup_value:
                continue
            target = (self.project_root / relative).resolve()
            backup = Path(backup_value).resolve()
            try:
                target.relative_to(self.project_root)
                if target.exists() or not backup.is_file():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
                restored.append(relative)
            except (OSError, ValueError):
                logger.exception("Could not restore Git preflight backup: %s", relative)
        return restored

    async def _handle_conflict(self, task_id: str, merge_result: dict) -> dict:
        """Report an internal merge failure while preserving task output."""
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
            "message": (
                "RockCore could not automatically integrate concurrent changes; "
                "task output was preserved for internal recovery"
            ),
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
