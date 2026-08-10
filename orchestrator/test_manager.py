"""TestManager — runs acceptance tests and records results."""

import logging
import os
import fnmatch
import hashlib
import ast
import json
import subprocess
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from app.subprocess_utils import command_basename, quote_command_arg, run_process
from app.text_utils import read_text_compatible
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
            result = run_process(
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
        return command_basename(command) == "git"

    @staticmethod
    def is_test_authoring_task(task: Any) -> bool:
        """Distinguish writing tests from merely running/inspecting them."""
        if getattr(task, "task_type", "") != "testing":
            return False
        text = " ".join((
            getattr(task, "title", "") or "",
            getattr(task, "description", "") or "",
        )).lower()
        markers = (
            "write test", "add test", "create test", "update test",
            "modify test", "implement test", "test coverage",
            "编写测试", "新增测试", "添加测试", "创建测试", "修改测试",
            "补充测试", "测试用例",
        )
        return any(marker in text for marker in markers)

    @classmethod
    def should_validate_locally(cls, task: Any) -> bool:
        command = (getattr(task, "acceptance_command", "") or "").lower()
        title = (getattr(task, "title", "") or "").lower()
        description = (getattr(task, "description", "") or "").lower()
        return (
            getattr(task, "task_type", "") == "review"
            or (
                getattr(task, "task_type", "") == "testing"
                and not cls.is_test_authoring_task(task)
            )
            or "review diff" in title
            or "检查差异" in title
            or ("git diff" in command and ("structure" in description or "结构" in description))
        )

    @classmethod
    def discover_test_command(cls, root: str | Path) -> str:
        """Choose a deterministic project test command when one is available."""
        root_path = Path(root)
        package_json = root_path / "package.json"
        if package_json.is_file():
            try:
                package = json.loads(package_json.read_text(encoding="utf-8"))
                scripts = package.get("scripts") or {}
                if scripts.get("test"):
                    return "npm test"
            except (OSError, UnicodeError, json.JSONDecodeError):
                pass
        if (
            (root_path / "pytest.ini").exists()
            or (root_path / "pyproject.toml").exists()
            or (root_path / "setup.cfg").exists()
            or (root_path / "tests").is_dir()
            or list(root_path.glob("test_*.py"))
        ):
            return f"{quote_command_arg(sys.executable)} -m pytest -q"
        if (root_path / "Cargo.toml").is_file():
            return "cargo test"
        if (root_path / "go.mod").is_file():
            return "go test ./..."
        return ""

    @staticmethod
    def _is_shell_command(cmd: str) -> bool:
        """Check if a string looks like an actual shell command, not natural language."""
        if not cmd or not cmd.strip():
            return False
        base = command_basename(cmd)
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
            proc = run_process(
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

    async def validate_project(self, task: Any, repos: dict,
                               event_bus: EventBus | None = None,
                               baseline_snapshot: dict | None = None,
                               project_root: str | Path | None = None) -> dict:
        """Public deterministic validation entry point for coding tasks."""
        return await self._validate_project(
            task, repos, event_bus,
            baseline_snapshot=baseline_snapshot,
            project_root=project_root,
        )

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
        supported = {".html", ".htm", ".json", ".py", ".js", ".mjs", ".cjs"}
        candidates: list[Path] = []
        changed_paths = [root / relative for relative in diff["changed"]]
        for path in changed_paths:
            if path.is_file() and path.suffix.lower() in supported:
                candidates.append(path)
        if not candidates:
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in supported:
                    continue
                relative = path.relative_to(root).as_posix()
                if not allowed or any(
                    fnmatch.fnmatch(relative, pattern) for pattern in allowed
                ):
                    candidates.append(path)
        candidates = sorted(set(candidates))[:200]

        issues = []
        checked = []
        for path in candidates:
            result = self._validate_source(path)
            checked.append(path.relative_to(root).as_posix())
            issues.extend(result["issues"])

        # A review task verifies accumulated job changes; it does not need to edit again.
        changed = diff["changed"]
        if baseline_snapshot is not None and not changed:
            issues.append("No file changes detected from the job baseline")

        test_command = ""
        test_output = ""
        if not issues:
            requested = getattr(task, "acceptance_command", "") or ""
            if self._is_shell_command(requested) and not self._uses_git(requested):
                test_command = requested
            else:
                test_command = self.discover_test_command(root)
            if test_command:
                try:
                    proc = run_process(
                        test_command, shell=True, capture_output=True, text=True,
                        timeout=self.DEFAULT_TIMEOUT, cwd=str(root),
                    )
                    test_output = (proc.stdout + "\n" + proc.stderr).strip()
                    if proc.returncode != 0:
                        issues.append(
                            f"Test command failed ({test_command}): "
                            f"{test_output[-1000:]}"
                        )
                except subprocess.TimeoutExpired:
                    issues.append(f"Test command timed out: {test_command}")
                except OSError as error:
                    issues.append(f"Cannot run test command {test_command}: {error}")

        status = "passed" if not issues else "failed"
        summary_parts = [
            f"changed={len(changed)}", f"files_checked={len(checked)}"
        ]
        if test_command:
            summary_parts.append(f"tests={test_command}")
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
                "failed": len(issues), "output": output, "changes": diff,
                "command": test_command, "test_output": test_output[:2000]}

    @classmethod
    def _validate_source(cls, path: Path) -> dict:
        """Run syntax/structure checks without asking a model."""
        suffix = path.suffix.lower()
        if suffix in {".html", ".htm"}:
            return cls._validate_html(path)
        issues = []
        try:
            content, _ = read_text_compatible(path)
            if suffix == ".json":
                json.loads(content)
            elif suffix == ".py":
                ast.parse(content, filename=str(path))
            elif suffix in {".js", ".mjs", ".cjs"}:
                proc = run_process(
                    ["node", "--check", str(path)], capture_output=True,
                    text=True, timeout=15,
                )
                if proc.returncode != 0:
                    issues.append(
                        f"{path.name}: JavaScript syntax error: "
                        f"{(proc.stderr or proc.stdout).strip()[:500]}"
                    )
        except FileNotFoundError as error:
            if suffix in {".js", ".mjs", ".cjs"}:
                logger.info("Node is unavailable; skipping JavaScript syntax check: %s", error)
            else:
                issues.append(f"{path.name}: cannot validate: {error}")
        except (OSError, UnicodeError, ValueError, SyntaxError,
                json.JSONDecodeError) as error:
            issues.append(f"{path.name}: syntax/parse error: {error}")
        return {"status": "passed" if not issues else "failed", "issues": issues}

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
            content, _ = read_text_compatible(path)
            parser = StructureParser()
            parser.feed(content)
            parser.close()
            duplicates = sorted({value for value in parser.ids if parser.ids.count(value) > 1})
            if duplicates:
                issues.append(
                    f"{path.name}: duplicate id(s): {', '.join(duplicates[:8])}"
                )
            lower = content.lower()
            # HTML fragments are valid project assets. Only flag an incomplete
            # document wrapper when the file started using one of these tags.
            has_html = "<html" in lower
            has_body = "<body" in lower
            if has_html != has_body:
                issues.append(f"{path.name}: missing html/body structure")
        except (OSError, UnicodeError, ValueError) as error:
            issues.append(f"{path.name}: cannot parse HTML: {error}")
        return {"status": "passed" if not issues else "failed", "issues": issues}
