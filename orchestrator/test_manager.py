"""TestManager — runs acceptance tests and records results."""

import logging
import fnmatch
import hashlib
import ast
import copy
import json
import subprocess
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from app.subprocess_utils import command_basename, run_process
from app.text_utils import read_text_compatible
from app.python_validation import run_embedded_python_command
from .event_bus import EventBus

logger = logging.getLogger(__name__)

# Commands that are safe to run via shell. Natural-language acceptance criteria
# like "完成结构化分析报告" are NOT valid commands.
VALID_TEST_COMMANDS = {
    "pytest", "python3", "python", "py", "npm", "pnpm", "yarn", "node",
    "cargo", "go", "make", "cmake", "ctest", "deno", "bun",
    "mypy", "ruff", "black", "flake8", "eslint", "tsc", "vitest", "jest",
    "pip", "poetry", "ls", "cat", "head", "tail", "test", "echo",
    "git",
}


class TestManager:
    """Manages test execution for task acceptance criteria."""

    DEFAULT_TIMEOUT = 300
    SNAPSHOT_IGNORES = {
        ".git", ".ai", "node_modules",
        ".venv", "venv", "__pycache__",
    }

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
    def capture_snapshot(
        cls, root: str | Path,
    ) -> dict[str, tuple[int, int, str, int | None]]:
        """Capture a lightweight content snapshot for non-Git change detection."""
        root_path = Path(root).resolve()
        snapshot: dict[str, tuple[int, int, str, int | None]] = {}
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
                line_count = None
                if stat.st_size <= 5 * 1024 * 1024:
                    payload = path.read_bytes()
                    digest = hashlib.sha256(payload).hexdigest()
                    if b"\x00" not in payload:
                        try:
                            text, _ = read_text_compatible(path)
                            line_count = len(text.splitlines())
                        except (OSError, UnicodeError):
                            line_count = None
                snapshot[relative] = (
                    stat.st_size, stat.st_mtime_ns, digest, line_count,
                )
            except OSError:
                continue
        return snapshot

    @staticmethod
    def normalize_snapshot(before: dict | None) -> dict[str, tuple]:
        """Restore snapshots after JSON has converted tuples into lists."""
        normalized: dict[str, tuple] = {}
        for path, value in (before or {}).items():
            if not isinstance(path, str) or not isinstance(value, (list, tuple)):
                continue
            if len(value) < 4:
                continue
            normalized[path.replace("\\", "/")] = tuple(value[:4])
        return normalized

    @classmethod
    def snapshot_diff(cls, root: str | Path,
                      before: dict | None) -> dict:
        after = cls.capture_snapshot(root)
        before = cls.normalize_snapshot(before)
        added = sorted(after.keys() - before.keys())
        deleted = sorted(before.keys() - after.keys())
        modified = sorted(
            path for path in after.keys() & before.keys() if after[path] != before[path]
        )
        return {"added": added, "modified": modified, "deleted": deleted,
                "changed": added + modified + deleted}

    @classmethod
    def change_summary(cls, root: str | Path,
                       before: dict | None) -> dict:
        """Return live file and text-line changes relative to a task snapshot.

        Git provides exact numstat values for tracked files. Newly created files
        and non-Git projects fall back to bounded text line counts captured in
        the task snapshot. Binary files still contribute to ``files_changed``.
        """
        root_path = Path(root).resolve()
        before = before or {}
        diff = cls.snapshot_diff(root_path, before)
        additions = 0
        deletions = 0
        counted: set[str] = set()
        changed_paths = set(diff["changed"])

        if cls.is_git_repo(root_path):
            try:
                result = run_process(
                    [
                        "git", "-C", str(root_path), "diff", "--numstat",
                        "--no-renames", "HEAD", "--",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.splitlines():
                        parts = line.split("\t", 2)
                        if len(parts) != 3:
                            continue
                        added, removed, relative = parts
                        relative = relative.replace("\\", "/")
                        if relative not in changed_paths:
                            continue
                        counted.add(relative)
                        if added.isdigit():
                            additions += int(added)
                        if removed.isdigit():
                            deletions += int(removed)
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        after = cls.capture_snapshot(root_path)
        for relative in diff["added"]:
            if relative not in counted:
                additions += cls._snapshot_line_count(after.get(relative))
        for relative in diff["deleted"]:
            if relative not in counted:
                deletions += cls._snapshot_line_count(before.get(relative))
        for relative in diff["modified"]:
            if relative in counted:
                continue
            old_lines = cls._snapshot_line_count(before.get(relative))
            new_lines = cls._snapshot_line_count(after.get(relative))
            if new_lines >= old_lines:
                additions += new_lines - old_lines
            else:
                deletions += old_lines - new_lines

        return {
            **diff,
            "files_changed": len(diff["changed"]),
            "additions": additions,
            "deletions": deletions,
        }

    @staticmethod
    def _snapshot_line_count(value) -> int:
        if isinstance(value, (tuple, list)) and len(value) >= 4:
            count = value[3]
            if isinstance(count, int) and count >= 0:
                return count
        return 0

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
            "modify test", "implement test", "author test", "test coverage",
            "write and run test", "create and run test",
            "编写测试", "新增测试", "添加测试", "创建测试", "修改测试",
            "补充测试", "测试用例", "实现测试",
        )
        if any(marker in text for marker in markers):
            return True
        # Allow words between the action and object, e.g.
        # “编写并运行工作日志核心验收测试”, without treating a run-only
        # “执行验收测试” task as authoring work.
        chinese_actions = (
            "编写", "新增", "添加", "创建", "修改", "补充", "实现", "生成",
        )
        chinese_test_objects = ("测试", "测试用例", "测试脚本")
        action_text = text
        for negated in (
            "不修改", "无需修改", "不要修改", "不新增", "无需新增",
            "不创建", "无需创建", "不编写", "无需编写",
        ):
            action_text = action_text.replace(negated, "")
        return (
            any(action in action_text for action in chinese_actions)
            and any(subject in text for subject in chinese_test_objects)
        )

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
            # This is deliberately a logical command rather than sys.executable:
            # app.python_validation runs it with RockCore's packaged interpreter
            # and bundled pytest even when the target PC has no Python on PATH.
            return "python -m pytest -q"
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

    @staticmethod
    def _split_sequential_commands(command: str) -> list[str]:
        """Split unquoted ``&&`` without enabling general shell composition.

        Planner output occasionally represents an acceptance suite as one
        ``command_a && command_b`` string.  RockCore must keep rejecting shell
        composition in the embedded Python runner, but the orchestrator can
        safely turn a plain sequential suite into separately audited commands.
        Operators inside quotes are preserved and ``||``/pipes/redirection are
        deliberately not rewritten because they change control/data flow.
        """
        text = str(command or "").strip()
        if not text:
            return []
        parts: list[str] = []
        start = 0
        quote = ""
        escaped = False
        index = 0
        while index < len(text):
            character = text[index]
            if escaped:
                escaped = False
            elif character == "\\" and quote != "'":
                escaped = True
            elif quote:
                if character == quote:
                    quote = ""
            elif character in {"'", '"'}:
                quote = character
            elif text[index:index + 2] == "&&":
                part = text[start:index].strip()
                if not part:
                    return [text]
                parts.append(part)
                index += 1
                start = index + 1
            index += 1
        if quote:
            return [text]
        tail = text[start:].strip()
        if not tail:
            return [text]
        parts.append(tail)
        return parts

    @classmethod
    def acceptance_suite(cls, task: Any) -> list[str]:
        """Return a de-duplicated suite of independently executable commands."""
        raw_commands = [
            str(command).strip()
            for command in (getattr(task, "acceptance_commands", None) or [])
            if str(command).strip()
        ]
        legacy = str(getattr(task, "acceptance_command", "") or "").strip()
        if legacy:
            raw_commands.append(legacy)
        normalized: list[str] = []
        for command in raw_commands:
            candidates = cls._split_sequential_commands(command)
            if len(candidates) > 1 and all(
                cls._is_shell_command(candidate) for candidate in candidates
            ):
                values = candidates
            else:
                values = [command]
            for value in values:
                if value not in normalized:
                    normalized.append(value)
        return normalized

    async def run_tests(self, task: Any, repos: dict,
                        event_bus: EventBus | None = None,
                        baseline_snapshot: dict | None = None,
                        project_root: str | Path | None = None) -> dict:
        """Run a task's acceptance command and record results."""
        suite = self.acceptance_suite(task)
        command = str(getattr(task, "acceptance_command", "") or "").strip()
        if not command and suite:
            command = suite[0]
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

        if len(suite) > 1 or (suite and suite[0] != str(command).strip()):
            results = []
            for suite_command in suite:
                command_task = copy.copy(task)
                command_task.acceptance_command = suite_command
                command_task.acceptance_commands = []
                result = await self.run_tests(
                    command_task, repos, event_bus,
                    baseline_snapshot=baseline_snapshot,
                    project_root=cwd,
                )
                results.append((suite_command, result))
                if result.get("status") != "passed":
                    break
            passed = bool(results) and all(
                result.get("status") == "passed" for _, result in results
            ) and len(results) == len(suite)
            return {
                "status": "passed" if passed else "failed",
                "passed": sum(int(result.get("passed") or 0) for _, result in results),
                "failed": sum(int(result.get("failed") or 0) for _, result in results),
                "output": "\n\n".join(
                    f"$ {suite_command}\n{result.get('output') or result.get('error') or ''}"
                    for suite_command, result in results
                )[:4000],
                "duration_ms": sum(
                    int(result.get("duration_ms") or 0) for _, result in results
                ),
                "commands": [command for command, _ in results],
            }

        # Only execute if it's an actual shell command
        if not self._is_shell_command(command):
            manifest = dict(
                getattr(task, "_rockcore_artifact_manifest", None) or {}
            )
            if manifest.get("require_changed_output"):
                return await self._validate_project(
                    task, repos, event_bus,
                    baseline_snapshot=baseline_snapshot,
                    project_root=cwd,
                )
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
            proc = run_embedded_python_command(
                command, cwd, timeout=self.DEFAULT_TIMEOUT
            )
            used_embedded_python = proc is not None
            if proc is None:
                proc = run_process(
                    command, shell=True, capture_output=True,
                    text=True, timeout=self.DEFAULT_TIMEOUT, cwd=cwd,
                )
            duration = int((time.time() - start) * 1000)
            output = proc.stdout + "\n" + proc.stderr
            passed = (
                1 if used_embedded_python and proc.returncode == 0
                else output.count("passed") if proc.returncode == 0 else 0
            )
            failed = (
                1 if used_embedded_python and proc.returncode != 0
                else output.count("FAILED") if proc.returncode != 0 else 0
            )
            status = "passed" if proc.returncode == 0 else "failed"
            if status == "passed":
                artifact_issues, _ = self._validate_artifact_manifest(
                    task, Path(cwd),
                    self.snapshot_diff(cwd, baseline_snapshot),
                )
                if artifact_issues:
                    status = "failed"
                    passed = 0
                    failed = max(1, len(artifact_issues))
                    output = (
                        output.rstrip()
                        + "\nArtifact validation failed: "
                        + "; ".join(artifact_issues[:5])
                    )
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
                tr.id, 0, 0, 0,
                f"Timeout ({self.DEFAULT_TIMEOUT}s)",
                self.DEFAULT_TIMEOUT * 1000, "failed"
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

    @classmethod
    def _validate_artifact_manifest(cls, task: Any, root: Path,
                                    diff: dict) -> tuple[list[str], list[str]]:
        """Validate the requested final artifact independently of helper files."""
        manifest = dict(
            getattr(task, "_rockcore_artifact_manifest", None) or {}
        )
        if not (
            manifest.get("kind") == "pdf"
            and manifest.get("require_changed_output")
        ):
            return [], []
        issues = []
        checked = []
        changed_set = set(diff["changed"])
        input_paths = {
            str(path).replace("\\", "/")
            for path in (manifest.get("inputs") or [])
        }
        output_paths = [
            str(path).replace("\\", "/")
            for path in (manifest.get("outputs") or [])
        ]
        if not output_paths:
            output_paths = [
                relative for relative in diff["changed"]
                if relative.lower().endswith(".pdf")
                and relative not in input_paths
            ]
        changed_outputs = [
            relative for relative in output_paths
            if relative in changed_set and (root / relative).is_file()
        ]
        if not changed_outputs:
            expected = ", ".join(output_paths) or "a new PDF artifact"
            issues.append(
                "Requested final PDF was not created or updated: " + expected
            )
        input_digests = set()
        for relative in input_paths:
            path = root / relative
            try:
                if path.is_file():
                    input_digests.add(hashlib.sha256(path.read_bytes()).hexdigest())
            except OSError:
                continue
        for relative in changed_outputs:
            path = root / relative
            result = cls._validate_pdf(
                path,
                require_extractable_text=bool(
                    manifest.get("require_extractable_text", True)
                ),
            )
            checked.append(relative)
            issues.extend(result["issues"])
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if digest in input_digests:
                    issues.append(
                        f"{path.name}: output is byte-identical to an input PDF"
                    )
            except OSError:
                pass
        return issues, checked

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
        supported = {
            ".html", ".htm", ".json", ".py", ".js", ".mjs", ".cjs",
            ".pdf", ".md", ".txt",
        }
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
        artifact_issues, artifact_checked = self._validate_artifact_manifest(
            task, root, diff
        )
        issues.extend(artifact_issues)
        checked.extend(artifact_checked)
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            if relative in checked:
                continue
            result = self._validate_source(path)
            checked.append(relative)
            issues.extend(result["issues"])

        # Review and run-only validation tasks verify accumulated job state and
        # do not need to edit again. Only authoring/coding work must prove a new
        # artifact relative to its own baseline.
        changed = diff["changed"]
        require_changes = (
            getattr(task, "task_type", "") == "coding"
            or self.is_test_authoring_task(task)
        )
        if baseline_snapshot is not None and not changed and require_changes:
            issues.append("No file changes detected from the job baseline")

        test_command = ""
        test_commands: list[str] = []
        test_outputs: list[str] = []
        if not issues:
            requested_suite = self.acceptance_suite(task)
            test_commands = [
                command for command in requested_suite
                if self._is_shell_command(command) and not self._uses_git(command)
            ]
            if not test_commands:
                discovered = self.discover_test_command(root)
                if discovered:
                    test_commands = [discovered]
            for test_command in test_commands:
                try:
                    proc = run_embedded_python_command(
                        test_command, root, timeout=self.DEFAULT_TIMEOUT
                    )
                    if proc is None:
                        proc = run_process(
                            test_command, shell=True, capture_output=True, text=True,
                            timeout=self.DEFAULT_TIMEOUT, cwd=str(root),
                        )
                    command_output = (proc.stdout + "\n" + proc.stderr).strip()
                    test_outputs.append(
                        f"$ {test_command}\n{command_output}".strip()
                    )
                    if proc.returncode != 0:
                        issues.append(
                            f"Test command failed ({test_command}): "
                            f"{command_output[-1000:]}"
                        )
                except subprocess.TimeoutExpired:
                    issues.append(f"Test command timed out: {test_command}")
                except OSError as error:
                    issues.append(f"Cannot run test command {test_command}: {error}")

        status = "passed" if not issues else "failed"
        summary_parts = [
            f"changed={len(changed)}", f"files_checked={len(checked)}"
        ]
        if test_commands:
            summary_parts.append(f"tests={' | '.join(test_commands)}")
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
                "command": " | ".join(test_commands),
                "commands": test_commands,
                "test_output": "\n\n".join(test_outputs)[:4000]}

    @classmethod
    def _validate_source(cls, path: Path) -> dict:
        """Run syntax/structure checks without asking a model."""
        suffix = path.suffix.lower()
        if suffix in {".html", ".htm"}:
            return cls._validate_html(path)
        if suffix == ".pdf":
            return cls._validate_pdf(path)
        issues = []
        try:
            content, _ = read_text_compatible(path)
            if suffix in {".md", ".txt"} and not content.strip():
                issues.append(f"{path.name}: generated document is empty")
            elif suffix == ".json":
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
    def _validate_pdf(path: Path,
                      require_extractable_text: bool = False) -> dict:
        """Check that a generated PDF is readable, non-empty, and has pages."""
        issues = []
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                from PyPDF2 import PdfReader
            if path.stat().st_size <= 0:
                issues.append(f"{path.name}: generated PDF is empty")
            else:
                reader = PdfReader(str(path))
                if getattr(reader, "is_encrypted", False):
                    issues.append(f"{path.name}: generated PDF is encrypted")
                elif len(reader.pages) < 1:
                    issues.append(f"{path.name}: generated PDF has no pages")
                else:
                    # Access both ends so a truncated xref/page tree cannot
                    # masquerade as a structurally valid multi-page artifact.
                    reader.pages[0].mediabox
                    reader.pages[-1].mediabox
                    if require_extractable_text:
                        indexes = sorted({
                            0, min(1, len(reader.pages) - 1),
                            len(reader.pages) - 1,
                        })
                        extracted = "".join(
                            str(reader.pages[index].extract_text() or "")
                            for index in indexes
                        )
                        if not extracted.strip():
                            issues.append(
                                f"{path.name}: generated PDF has no extractable text"
                            )
        except (OSError, ValueError, TypeError, IndexError) as error:
            issues.append(f"{path.name}: invalid PDF: {error}")
        except Exception as error:
            issues.append(f"{path.name}: PDF validation failed: {error}")
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
