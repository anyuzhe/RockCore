"""Git repository management for the AI Engineering Studio."""

import logging
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class Repository:
    """Manages a git repository for AI job isolation."""

    def __init__(self, root_path: str):
        self.root_path = Path(root_path).resolve()

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
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

    def ensure_initialized(self) -> dict:
        """Create a local Git baseline for a project that has no repository yet."""
        if not self.root_path.is_dir():
            return {"status": "failed", "error": "Project root is not a directory"}
        if self.is_repo():
            return {
                "status": "existing",
                "branch": self.current_branch(),
                "commit": self.get_commit_hash(),
            }

        try:
            init_result = self._run("init", "-b", "main")
        except OSError as error:
            return {"status": "failed", "error": str(error)}
        if init_result.returncode != 0:
            init_result = self._run("init")
        if init_result.returncode != 0:
            return {"status": "failed", "error": init_result.stderr.strip()}

        # Keep generated data and common credentials out of the automatic baseline
        # without modifying a project's own .gitignore.
        exclude_path = self.root_path / ".git" / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        patterns = [
            ".DS_Store", ".venv/", "venv/", "node_modules/", "__pycache__/",
            "*.pyc", ".env", ".env.*", "*.pem", "*.key", "credentials.json",
            ".ai/worktrees/",
        ]
        missing = [pattern for pattern in patterns if pattern not in existing.splitlines()]
        if missing:
            suffix = "" if not existing or existing.endswith("\n") else "\n"
            exclude_path.write_text(
                existing + suffix + "# RockCore local exclusions\n" + "\n".join(missing) + "\n",
                encoding="utf-8",
            )

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
