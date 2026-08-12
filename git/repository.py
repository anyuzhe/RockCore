"""Git repository management for the AI Engineering Studio."""

import logging
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from app.subprocess_utils import run_process

logger = logging.getLogger(__name__)

ROCKCORE_IGNORE_START = "# >>> RockCore managed ignores >>>"
ROCKCORE_IGNORE_END = "# <<< RockCore managed ignores <<<"
ROCKCORE_IGNORE_LINES = (
    "# Python bytecode and tool caches",
    "__pycache__/",
    "*.py[cod]",
    "*$py.class",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".tox/",
    ".nox/",
    ".coverage",
    ".coverage.*",
    "htmlcov/",
    "",
    "# Local dependencies and virtual environments",
    ".venv/",
    "venv/",
    "node_modules/",
    ".npm/",
    ".pnpm-store/",
    ".yarn/cache/",
    "",
    "# Local secrets, logs, OS and editor temporary files",
    ".env",
    ".env.local",
    ".env.*.local",
    "*.pem",
    "*.key",
    "credentials.json",
    "*.log",
    ".DS_Store",
    "Thumbs.db",
    "*.swp",
    "*~",
    "",
    "# RockCore runtime isolation",
    ".ai/worktrees/",
    ".ai/runtime/",
    ".ai/recovery/",
    ".ai/reports/",
)


class Repository:
    """Manages a git repository for AI job isolation."""

    def __init__(self, root_path: str):
        self.root_path = Path(root_path).resolve()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return run_process(
            ["git"] + list(args),
            capture_output=True, text=True, cwd=self.root_path,
        )

    def is_repo(self) -> bool:
        try:
            result = self._run("rev-parse", "--show-toplevel")
        except OSError:
            return False
        if result.returncode != 0:
            return False
        try:
            return Path(result.stdout.strip()).resolve() == self.root_path
        except (OSError, ValueError):
            return False

    @staticmethod
    def _managed_ignore_block(newline: bytes) -> bytes:
        lines = (
            ROCKCORE_IGNORE_START,
            *ROCKCORE_IGNORE_LINES,
            ROCKCORE_IGNORE_END,
            "",
        )
        return newline.join(line.encode("utf-8") for line in lines)

    @staticmethod
    def _update_managed_ignore_file(path: Path) -> tuple[bool, str]:
        """Atomically add/update RockCore's block while preserving user bytes."""
        if path.is_symlink():
            return False, f"Refusing to replace symlinked ignore file: {path}"
        try:
            existing = path.read_bytes() if path.exists() else b""
            newline = (
                b"\r\n"
                if existing.count(b"\r\n") > existing.count(b"\n") / 2
                else b"\n"
            )
            block = Repository._managed_ignore_block(newline)
            matcher = re.compile(
                rb"(?ms)^" + re.escape(ROCKCORE_IGNORE_START.encode())
                + rb"\r?\n.*?^" + re.escape(ROCKCORE_IGNORE_END.encode())
                + rb"(?:\r?\n)?"
            )
            match = matcher.search(existing)
            if match:
                updated = existing[:match.start()] + block + existing[match.end():]
            else:
                prefix = existing
                if prefix and not prefix.endswith((b"\n", b"\r")):
                    prefix += newline
                if prefix and not prefix.endswith(newline * 2):
                    prefix += newline
                updated = prefix + block
            if updated == existing:
                return False, ""

            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(
                f".{path.name}.rockcore-{os.getpid()}-{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary.write_bytes(updated)
                os.replace(temporary, path)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return True, ""
        except OSError as error:
            return False, str(error)

    def ensure_ignore_rules(self) -> dict:
        """Maintain shared .gitignore rules for new and existing repositories."""
        common_dir_result = self._run("rev-parse", "--git-common-dir")
        if common_dir_result.returncode != 0:
            return {
                "updated": False,
                "error": common_dir_result.stderr.strip()
                or "Unable to locate the Git metadata directory",
            }
        common_dir = Path(common_dir_result.stdout.strip())
        if not common_dir.is_absolute():
            common_dir = (self.root_path / common_dir).resolve()
        targets = (
            self.root_path / ".gitignore",
            common_dir / "info" / "exclude",
        )
        changed = []
        for target in targets:
            updated, error = self._update_managed_ignore_file(target)
            if error:
                return {"updated": bool(changed), "error": error}
            if updated:
                changed.append(str(target))
        return {"updated": bool(changed), "paths": changed, "error": ""}

    def ensure_initialized(self) -> dict:
        """Create a local Git baseline for a project that has no repository yet."""
        if not self.root_path.is_dir():
            return {"status": "failed", "error": "Project root is not a directory"}
        existing_repository = self.is_repo()
        if existing_repository:
            ignore_state = self.ensure_ignore_rules()
            if ignore_state.get("error"):
                return {"status": "failed", "error": ignore_state["error"]}
            return {
                "status": "existing",
                "branch": self.current_branch(),
                "commit": self.get_commit_hash(),
                "gitignore_updated": ignore_state["updated"],
            }

        try:
            init_result = self._run("init", "-b", "main")
        except OSError as error:
            return {"status": "failed", "error": str(error)}
        if init_result.returncode != 0:
            init_result = self._run("init")
        if init_result.returncode != 0:
            return {"status": "failed", "error": init_result.stderr.strip()}

        ignore_state = self.ensure_ignore_rules()
        if ignore_state.get("error"):
            return {"status": "failed", "error": ignore_state["error"]}

        stage_result = self._run("add", "-A")
        if stage_result.returncode != 0:
            return {"status": "initialized", "error": stage_result.stderr.strip()}

        # Local-only identity keeps bootstrap independent from global Git settings.
        if self._run("config", "user.name").returncode != 0:
            self._run("config", "user.name", "RockCore")
        if self._run("config", "user.email").returncode != 0:
            self._run("config", "user.email", "rockcore@localhost")

        commit_result = self._run("commit", "--allow-empty", "-m", "Initial project state")
        if commit_result.returncode != 0:
            combined = f"{commit_result.stdout}\n{commit_result.stderr}".lower()
            if "nothing to commit" not in combined:
                return {"status": "initialized", "error": commit_result.stderr.strip()}

        return {
            "status": "initialized",
            "branch": self.current_branch(),
            "commit": self.get_commit_hash(),
            "gitignore_updated": ignore_state["updated"],
        }

    def current_branch(self) -> str:
        result = self._run("rev-parse", "--abbrev-ref", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else "unknown"

    def create_branch(self, branch_name: str, base: str = "main") -> dict:
        """Create a new branch for an AI job."""
        result = self._run("checkout", "-b", branch_name, base)
        if result.returncode != 0:
            # Try master
            result = self._run("checkout", "-b", branch_name, "master")
        if result.returncode != 0:
            # Just create from current HEAD
            result = self._run("checkout", "-b", branch_name)
        return {
            "branch": branch_name,
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else "",
        }

    def stage_all(self) -> dict:
        result = self._run("add", "-A")
        return {
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else "",
        }

    def commit(self, message: str) -> dict:
        result = self._run("commit", "-m", message)
        return {
            "success": result.returncode == 0,
            "hash": result.stdout.strip() if result.returncode == 0 else "",
            "error": result.stderr if result.returncode != 0 else "",
        }

    def diff(self, base: str = "HEAD") -> str:
        result = self._run("diff", base)
        return result.stdout

    def changed_files(self) -> list[str]:
        result = self._run("diff", "--name-only", "HEAD")
        if result.stdout.strip():
            return result.stdout.strip().split("\n")
        return []

    def checkout(self, branch: str) -> dict:
        result = self._run("checkout", branch)
        return {
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else "",
        }

    def stash(self) -> dict:
        result = self._run("stash")
        return {
            "success": result.returncode == 0,
            "error": result.stderr if result.returncode != 0 else "",
        }

    def is_clean(self) -> bool:
        result = self._run("status", "--porcelain")
        return not bool(result.stdout.strip())

    def job_commits(self, job_id: str) -> list[str]:
        """Return task commits created for one RockCore Job, oldest first."""
        result = self._run(
            "log", "--reverse", "--format=%H", "--fixed-strings",
            f"--grep=AI {job_id}:",
        )
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def _discard_job_worktrees(self, job_id: str) -> list[str]:
        """Delete isolated checkpoints owned by a Job after explicit rollback."""
        prefix = f"ai/{str(job_id or '').strip().lower()}/"
        listed = self._run("worktree", "list", "--porcelain")
        blocks: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in listed.stdout.splitlines() + [""]:
            if not line.strip():
                if current:
                    blocks.append(current)
                    current = {}
                continue
            if line.startswith("worktree "):
                current["path"] = line[9:].strip()
            elif line.startswith("branch refs/heads/"):
                current["branch"] = line[18:].strip()

        removed = []
        branches = set()
        for item in blocks:
            branch = item.get("branch", "")
            path = item.get("path", "")
            if not branch.startswith(prefix) or not path:
                continue
            result = self._run("worktree", "remove", "--force", path)
            if result.returncode == 0:
                removed.append(path)
                branches.add(branch)
        refs = self._run(
            "for-each-ref", "--format=%(refname:short)",
            f"refs/heads/{prefix}",
        )
        branches.update(
            line.strip() for line in refs.stdout.splitlines() if line.strip()
        )
        for branch in sorted(branches):
            self._run("branch", "-D", branch)
        self._run("worktree", "prune")
        return removed

    def rollback_job(self, job_id: str) -> dict:
        """Safely reverse a Job on a temporary branch, then merge the inverse.

        The project branch is never hard-reset. Later commits remain intact and
        conflicts are detected in the temporary worktree before the target is
        touched.
        """
        if not self.is_repo():
            state = self.ensure_initialized()
            if state.get("status") == "failed":
                return {
                    "status": "failed",
                    "error": state.get("error", "Git unavailable"),
                }
        if self.unmerged_files():
            return {
                "status": "failed",
                "error": "项目正在进行内部合并，RockCore 暂时不能安全回退。",
            }
        if self._run("config", "user.name").returncode != 0:
            self._run("config", "user.name", "RockCore")
        if self._run("config", "user.email").returncode != 0:
            self._run("config", "user.email", "rockcore@localhost")

        commits = self.job_commits(job_id)
        if not commits:
            removed = self._discard_job_worktrees(job_id)
            return {
                "status": "rolled_back", "commits": [], "removed_worktrees": removed,
                "message": "该需求没有已合并的代码变更，已清理其中断检查点。",
            }

        token = uuid.uuid4().hex[:10]
        safe_job = re.sub(r"[^a-z0-9._-]+", "-", job_id.lower()).strip("-")
        branch = f"ai/rollback/{safe_job}-{token}"
        rollback_root = self.root_path / ".ai" / "recovery" / f"rollback-{token}"
        rollback_root.parent.mkdir(parents=True, exist_ok=True)
        added = self._run(
            "worktree", "add", "-b", branch, str(rollback_root), "HEAD"
        )
        if added.returncode != 0:
            return {
                "status": "failed",
                "error": added.stderr.strip() or "无法创建安全回退检查点。",
            }

        rollback_ready = False
        try:
            for commit_hash in reversed(commits):
                result = run_process(
                    ["git", "revert", "--no-edit", commit_hash],
                    capture_output=True, text=True, cwd=rollback_root,
                )
                if result.returncode != 0:
                    run_process(
                        ["git", "revert", "--abort"],
                        capture_output=True, text=True, cwd=rollback_root,
                    )
                    return {
                        "status": "failed",
                        "error": (
                            "无法安全回退：这次需求与后续修改有重叠。"
                            "RockCore 已保留现有代码，未做任何破坏性操作。"
                        ),
                        "detail": (result.stderr or result.stdout).strip()[:2000],
                    }
            rollback_ready = True
        finally:
            self._run("worktree", "remove", "--force", str(rollback_root))
            if not rollback_ready:
                self._run("branch", "-D", branch)

        merged = self._run(
            "merge", "--no-ff", "-m", f"RockCore rollback {job_id}", branch
        )
        if merged.returncode != 0:
            self._run("merge", "--abort")
            self._run("branch", "-D", branch)
            return {
                "status": "failed",
                "error": "回退结果无法安全合入当前项目；现有代码已原样保留。",
                "detail": (merged.stderr or merged.stdout).strip()[:2000],
            }
        self._run("branch", "-D", branch)
        removed = self._discard_job_worktrees(job_id)
        return {
            "status": "rolled_back",
            "commits": commits,
            "rollback_commit": self.get_commit_hash(),
            "removed_worktrees": removed,
        }

    def unmerged_files(self) -> list[str]:
        """Return paths left in an unresolved Git merge."""
        result = self._run("diff", "--name-only", "--diff-filter=U")
        if result.returncode != 0 or not result.stdout.strip():
            return []
        return [path for path in result.stdout.splitlines() if path.strip()]

    def get_commit_hash(self, ref: str = "HEAD") -> str:
        result = self._run("rev-parse", "--short", ref)
        return result.stdout.strip() if result.returncode == 0 else ""

    def snapshot(self) -> dict:
        """Take a snapshot of current state."""
        return {
            "branch": self.current_branch(),
            "commit": self.get_commit_hash(),
            "is_clean": self.is_clean(),
            "changed_files": self.changed_files(),
        }
