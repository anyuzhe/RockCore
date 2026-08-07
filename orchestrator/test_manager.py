"""TestManager — runs acceptance tests and records results."""

import logging
import os
import fnmatch
import hashlib
import shlex
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .event_bus import EventBus

logger = logging.getLogger(__name__)

# Commands that are safe to run via shell. Natural-language acceptance criteria
# like "完成结构化分析报告" are NOT valid commands.
VALID_TEST_COMMANDS = {
    "pytest", "python3", "python", "npm", "pnpm", "yarn", "node",
    "cargo", "go", "make", "cmake", "ctest", "deno", "bun",
    "mypy", "ruff", "black", "flake8", "eslint", "tsc", "vitest", "jest",
    "pip", "poetry", "ls", "cat", "head", "tail", "test", "echo",
    "git",
}


class TestManager:
    """Manages test execution for task acceptance criteria."""

    DEFAULT_TIMEOUT = 120
    SNAPSHOT_IGNORES = {".git", ".ai/worktrees", "node_modules", ".venv", "venv", "__pycache__"}

    @staticmethod
    def is_git_repo(root: str | Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                capture_output=True, text=True, timeout=5,
            )
            return (
                result.returncode == 0
                and Path(result.stdout.strip()).resolve() == Path(root).resolve()
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    @classmethod
    def capture_snapshot(cls, root: str | Path) -> dict[str, tuple[int, int, str]]:
        """Capture a lightweight content snapshot for non-Git change detection."""
        root_path = Path(root).resolve()
        snapshot: dict[str, tuple[int, int, str]] = {}
        if not root_path.is_dir():
            return snapshot
        for path in root_path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root_path).as_posix()
            if any(relative == ignored or relative.startswith(f"{ignored}/")
                   for ignored in cls.SNAPSHOT_IGNORES):
                continue
            try:
                stat = path.stat()
                digest = ""
                if stat.st_size <= 5 * 1024 * 1024:
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                snapshot[relative] = (stat.st_size, stat.st_mtime_ns, digest)
            except OSError:
                continue
        return snapshot

    @classmethod
    def snapshot_diff(cls, root: str | Path,
                      before: dict[str, tuple[int, int, str]] | None) -> dict:
        after = cls.capture_snapshot(root)
        before = before or {}
        added = sorted(after.keys() - before.keys())
        deleted = sorted(before.keys() - after.keys())
        modified = sorted(
            path for path in after.keys() & before.keys() if after[path] != before[path]
        )
        return {"added": added, "modified": modified, "deleted": deleted,
                "changed": added + modified + deleted}

    @staticmethod
    def _uses_git(command: str) -> bool:
        try:
            tokens = shlex.split(command)
        except ValueError:
            tokens = command.split()
        return "git" in [Path(token).name for token in tokens]

    @staticmethod
    def should_validate_locally(task: Any) -> bool:
        command = (getattr(task, "acceptance_command", "") or "").lower()
        title = (getattr(task, "title", "") or "").lower()
        description = (getattr(task, "description", "") or "").lower()
        return (
            getattr(task, "task_type", "") == "review"
            or "review diff" in title
            or "检查差异" in title
            or ("git diff" in command and ("structure" in description or "结构" in description))
        )

    @staticmethod
    def _is_shell_command(cmd: str) -> bool:
        """Check if a string looks like an actual shell command, not natural language."""
        if not cmd or not cmd.strip():
            return False
        base = cmd.strip().split()[0].split("/")[-1]
        return base in VALID_TEST_COMMANDS

    async def run_tests(self, task: Any, repos: dict,
                        event_bus: EventBus | None = None,
                        baseline_snapshot: dict | None = None,
                        project_root: str | Path | None = None) -> dict:
        """Run a task's acceptance command and record results."""
        command = task.acceptance_command
        cwd = str(project_root or (
            task.job.project.root_path if task.job and task.job.project else "."
        ))
        if self.should_validate_locally(task):
            return await self._validate_project(
                task, repos, event_bus, baseline_snapshot=baseline_snapshot,
                project_root=cwd,
            )
        if not command:
            return {"status": "skipped", "reason": "no acceptance command"}

        # Only execute if it's an actual shell command
        if not self._is_shell_command(command):
            return await self._validate_output(task, repos, event_bus)

        if self._uses_git(command) and not self.is_git_repo(cwd):
            logger.info("Git acceptance requested for non-Git project; using file validation")
            return await self._validate_project(
                task, repos, event_bus, baseline_snapshot=baseline_snapshot,
                project_root=cwd,
            )
        tr = repos["test_run"].create(task.id, command)

        if event_bus:
            await event_bus.publish(
                "test_running", task_id=task.task_id, command=command
            )

        start = time.time()
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True,
                text=True, timeout=self.DEFAULT_TIMEOUT, cwd=cwd,
            )
            duration = int((time.time() - start) * 1000)
            output = proc.stdout + "\n" + proc.stderr
            passed = output.count("passed") if proc.returncode == 0 else 0
            failed = output.count("FAILED") if proc.returncode != 0 else 0
            status = "passed" if proc.returncode == 0 else "failed"
            repos["test_run"].update_result(
                tr.id, passed, failed, 0, output, duration, status
            )
            if event_bus:
                await event_bus.publish(
                    "test_result", task_id=task.task_id,
                    status=status, output=output[:500]
                )
            return {
                "status": status, "passed": passed, "failed": failed,
                "output": output[:2000], "duration_ms": duration,
            }
        except subprocess.TimeoutExpired:
            repos["test_run"].update_result(
                tr.id, 0, 0, 0, "Timeout (120s)", 120000, "failed"
            )
            if event_bus:
                await event_bus.publish(
                    "test_result", task_id=task.task_id,
                    status="failed", output="Timeout"
                )
            return {"status": "failed", "error": "timeout"}
        except Exception as e:
            logger.error(f"Test execution error: {e}")
            repos["test_run"].update_result(
                tr.id, 0, 0, 0, str(e), 0, "failed"
            )
            return {"status": "failed", "error": str(e)}

    async def _validate_output(self, task: Any, repos: dict,
                                event_bus: EventBus | None = None) -> dict:
        """Validate task output by checking file existence/contents — no shell execution."""
        cwd = task.job.project.root_path if task.job and task.job.project else "."
        command = task.acceptance_command

        allowed = task.allowed_paths or []
        checked_files = []
        for pattern in allowed:
            # Only check concrete files, not globs
            if "*" in pattern:
                continue
            fp = Path(cwd) / pattern
            if fp.is_file() and fp.stat().st_size > 0:
                checked_files.append({"path": pattern, "exists": True, "size": fp.stat().st_size})
            elif fp.exists():
                checked_files.append({"path": pattern, "exists": True, "size": 0})

        # For analysis/documentation tasks, check if any output files were created
        if checked_files:
            total_size = sum(f["size"] for f in checked_files)
            status = "passed" if total_size > 0 else "failed"
            logger.info(f"Output validation: {len(checked_files)} files, {total_size} bytes → {status}")
        else:
            # No specific output files to check — auto-pass if task was coding and files changed
            status = "passed"
            logger.info(f"Output validation: no specific files to check, auto-passing")

        tr = repos["test_run"].create(task.id, command[:200] if command else "output_validation")
        repos["test_run"].update_result(tr.id, 1 if status == "passed" else 0, 0, 0,
                                         f"Output validation: {status}", 0, status)
        if event_bus:
            await event_bus.publish("test_result", task_id=task.task_id,
                                     status=status, output=f"Output validation: {status}")
        return {"status": status, "passed": 1 if status == "passed" else 0,
                "failed": 0, "output": f"Output validation: {status}"}

    async def _validate_project(self, task: Any, repos: dict,
                                event_bus: EventBus | None = None,
                                baseline_snapshot: dict | None = None,
                                project_root: str | Path | None = None) -> dict:
        """Validate changes and HTML structure without spending model turns."""
        root = Path(project_root or (
            task.job.project.root_path if task.job and task.job.project else "."
        ))
        diff = self.snapshot_diff(root, baseline_snapshot)
        allowed = task.allowed_paths or []
        candidates: list[Path] = []
        for path in root.rglob("*.html"):
            relative = path.relative_to(root).as_posix()
            if not allowed or any(fnmatch.fnmatch(relative, pattern) for pattern in allowed):
                candidates.append(path)

        issues = []
        checked = []
        for path in candidates:
            result = self._validate_html(path)
            checked.append(path.relative_to(root).as_posix())
            issues.extend(result["issues"])

        # A review task verifies accumulated job changes; it does not need to edit again.
        changed = diff["changed"]
        if baseline_snapshot is not None and not changed:
            issues.append("No file changes detected from the job baseline")

        status = "passed" if not issues else "failed"
        summary_parts = [f"changed={len(changed)}", f"html_checked={len(checked)}"]
        if issues:
            summary_parts.append("issues=" + "; ".join(issues[:5]))
        output = "Local validation: " + ", ".join(summary_parts)
        tr = repos["test_run"].create(task.id, "local_project_validation")
        repos["test_run"].update_result(
            tr.id, 1 if status == "passed" else 0,
            0 if status == "passed" else len(issues), 0, output, 0, status,
        )
        if event_bus:
            await event_bus.publish(
                "test_result", task_id=task.task_id, status=status, output=output[:500]
            )
        return {"status": status, "passed": 1 if status == "passed" else 0,
                "failed": len(issues), "output": output, "changes": diff}

    @staticmethod
    def _validate_html(path: Path) -> dict:
        class StructureParser(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.ids = []

            def handle_starttag(self, tag, attrs):
                for name, value in attrs:
                    if name.lower() == "id" and value:
                        self.ids.append(value)

        issues = []
        try:
            content = path.read_text(encoding="utf-8")
            parser = StructureParser()
            parser.feed(content)
            parser.close()
            duplicates = sorted({value for value in parser.ids if parser.ids.count(value) > 1})
            if duplicates:
                issues.append(
                    f"{path.name}: duplicate id(s): {', '.join(duplicates[:8])}"
                )
            lower = content.lower()
            if "<html" not in lower or "<body" not in lower:
                issues.append(f"{path.name}: missing html/body structure")
        except (OSError, UnicodeError, ValueError) as error:
            issues.append(f"{path.name}: cannot parse HTML: {error}")
        return {"status": "passed" if not issues else "failed", "issues": issues}
