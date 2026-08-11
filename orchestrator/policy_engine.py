"""Policy engine — pure code enforcement of AI constraints."""

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

from app.subprocess_utils import command_basename

logger = logging.getLogger(__name__)


class PolicyViolation(Exception):
    def __init__(self, policy: str, message: str):
        self.policy = policy
        self.message = message
        super().__init__(f"[{policy}] {message}")


class PolicyEngine:
    """Enforces constraints via code, not via AI prompts."""

    FORBIDDEN_COMMANDS = {
        "rm", "dd", "mkfs", "format", "shutdown", "reboot",
        "chmod 777", "chown", "sudo", "su", "passwd",
    }

    FORBIDDEN_PATTERNS = [
        "rm -rf /",
        "rm -rf /*",
        ">:*",
        "| sh",
        "| bash",
        "| zsh",
        "`*`",
        "$(*)",
    ]

    ALLOWED_SHELL_COMMANDS = {
        "pytest", "npm", "pnpm", "yarn", "python", "python3",
        "cmake", "ctest", "make", "ruff", "flake8", "black",
        "eslint", "tsc", "vitest", "jest", "mypy",
        "git", "pip", "poetry", "cargo", "go",
        "node", "deno", "bun",
    }

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def check_path(self, path: str, protected_paths: list[str],
                   allowed_paths: list[str] | None = None) -> bool:
        """Check if path is protected or allowed."""
        path = path.replace("\\", "/")
        # Normalize: strip ./ prefix for consistent matching
        if path.startswith("./"):
            path = path[2:]
        # Keep absolute paths as-is for system dir check below

        for pattern in protected_paths:
            if fnmatch.fnmatch(path, pattern) or path.startswith(pattern):
                raise PolicyViolation(
                    "protected_path",
                    f"Path is protected: {path} (pattern: {pattern})"
                )

        if allowed_paths:
            if isinstance(allowed_paths, str):
                import json
                allowed_paths = json.loads(allowed_paths)
            allowed = any(
                fnmatch.fnmatch(path, p) or path.startswith(p)
                for p in allowed_paths
            )
            if not allowed:
                raise PolicyViolation(
                    "allowed_path",
                    f"Path not in allowed set: {path}"
                )

        # Prevent path traversal
        if ".." in path.split("/"):
            raise PolicyViolation(
                "path_traversal",
                f"Path traversal detected: {path}"
            )

        # Prevent absolute paths to system dirs
        system_dirs = ["/etc", "/usr", "/bin", "/sbin", "/dev", "/proc", "/sys"]
        for sd in system_dirs:
            if path.startswith(sd):
                raise PolicyViolation(
                    "system_path",
                    f"System directory access denied: {path}"
                )

        return True

    def check_command(self, command: str) -> bool:
        """Validate shell command against allowed list."""
        command = command.strip()

        for pattern in self.FORBIDDEN_PATTERNS:
            if fnmatch.fnmatch(command, pattern):
                raise PolicyViolation(
                    "forbidden_pattern",
                    f"Command matches forbidden pattern: {command[:100]}"
                )

        base_cmd = command_basename(command)

        if base_cmd in self.FORBIDDEN_COMMANDS:
            raise PolicyViolation(
                "forbidden_command",
                f"Command not allowed: {base_cmd}"
            )

        # Allow if it starts with an allowed command
        return True

    def check_task_plan(self, plan: dict, constitution: dict) -> list[str]:
        """Validate that a plan respects the constitution. Returns list of errors."""
        errors = []
        protected = constitution.get("protected_paths", [])
        tasks = list(plan.get("tasks") or [])
        task_ids = [str(task.get("id") or "").strip() for task in tasks]
        known_ids = {task_id for task_id in task_ids if task_id}

        for index, task_id in enumerate(task_ids, start=1):
            if not task_id:
                errors.append(f"Task at position {index} has no id")
            elif task_ids.count(task_id) > 1:
                message = f"Duplicate task id: {task_id}"
                if message not in errors:
                    errors.append(message)

        task_ref = re.compile(
            r"(?<![A-Za-z0-9_])(?:R\d{2}T\d{3,}|T\d{3,})(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        dependency_graph = {}
        for task in tasks:
            task_id = str(task.get("id") or "").strip()
            dependencies = [
                str(dependency).strip()
                for dependency in (task.get("dependencies") or [])
                if str(dependency).strip()
            ]
            dependency_graph[task_id] = dependencies
            for dependency in dependencies:
                if dependency == task_id:
                    errors.append(f"Task {task_id}: cannot depend on itself")
                elif dependency not in known_ids:
                    errors.append(
                        f"Task {task_id}: dependency references missing task "
                        f"'{dependency}'"
                    )

            for field in ("title", "description"):
                value = str(task.get(field) or "")
                for match in task_ref.finditer(value):
                    referenced = match.group(0)
                    before = value[max(0, match.start() - 24):match.start()].lower()
                    after = value[match.end():match.end() + 24].lower()
                    is_prerequisite_reference = any(
                        marker in before for marker in (
                            "依据", "根据", "依赖", "前置",
                            "after", "following", "based on", "depends on",
                        )
                    ) or any(
                        marker in after for marker in (
                            "报告", "分析结果", "分析结论", "结论", "产出", "完成后",
                            " report", " analysis", " result", " output",
                        )
                    )
                    if not is_prerequisite_reference:
                        continue
                    canonical = next(
                        (item for item in known_ids if item.upper() == referenced.upper()),
                        None,
                    )
                    if canonical is None:
                        errors.append(
                            f"Task {task_id}: {field} references missing task "
                            f"'{referenced}'"
                        )
                    elif canonical == task_id:
                        errors.append(
                            f"Task {task_id}: {field} contains a self-reference "
                            f"'{referenced}'"
                        )

            if task.get("type", "coding") == "coding":
                description = str(task.get("description") or "").lower()
                if "=== continuation context ===" in description:
                    paths = [
                        str(path or "").strip()
                        for path in (task.get("allowed_paths") or [])
                    ]
                    broad_only = not paths or all(
                        not path or path in {"*", "**", "**/*", "./*"}
                        for path in paths
                    )
                    acceptance = str(
                        task.get("acceptance_command") or ""
                    ).strip()
                    title = str(task.get("title") or "").strip().lower()
                    vague_title = title in {
                        "继续", "继续执行", "继续完成", "继续上一个任务",
                        "continue", "continue task", "finish task",
                    }
                    if broad_only and not acceptance:
                        errors.append(
                            f"continuation_quality: Task {task_id} has only a "
                            "wildcard scope and no acceptance condition"
                        )
                    if vague_title and broad_only:
                        errors.append(
                            f"continuation_quality: Task {task_id} does not name "
                            "the remaining output or target files"
                        )
                absolute_read_only = any(marker in description for marker in (
                    "不创建或修改任何项目文件",
                    "不创建或修改项目文件",
                    "不创建或修改任何文件",
                    "不得创建或修改任何项目文件",
                    "do not create or modify any project files",
                    "without creating or modifying any project files",
                ))
                positive_description = description
                for marker in (
                    "不创建或修改任何项目文件",
                    "不创建或修改项目文件",
                    "不创建或修改任何文件",
                    "不得创建或修改任何项目文件",
                    "do not create or modify any project files",
                    "without creating or modifying any project files",
                ):
                    positive_description = positive_description.replace(marker, "")
                requires_edit = any(marker in positive_description for marker in (
                    "创建", "修改", "实现", "写入", "编辑", "搭建",
                    "create", "modify", "implement", "write", "edit", "build",
                ))
                if absolute_read_only and requires_edit:
                    errors.append(
                        f"Task {task_id}: coding instructions conflict between "
                        "read-only and file modification requirements"
                    )

            for path in task.get("allowed_paths", []):
                normalized = str(path or "").replace("\\", "/")
                if Path(normalized).is_absolute():
                    errors.append(
                        f"Task {task.get('id')}: allowed_path must be relative: "
                        f"'{normalized}'"
                    )
                    continue
                if ".." in normalized.split("/"):
                    errors.append(
                        f"Task {task.get('id')}: allowed_path contains traversal: "
                        f"'{normalized}'"
                    )
                    continue
                for pp in protected:
                    if fnmatch.fnmatch(normalized, pp):
                        errors.append(
                            f"Task {task.get('id')}: allowed_path '{normalized}' "
                            f"intersects protected_path '{pp}'"
                        )

        # A cycle leaves every involved task waiting forever. Report one clear
        # error per cycle entry instead of allowing it into the scheduler.
        visited: set[str] = set()
        active: set[str] = set()

        def visit(task_id: str) -> bool:
            if task_id in active:
                return True
            if task_id in visited:
                return False
            active.add(task_id)
            for dependency in dependency_graph.get(task_id, []):
                if dependency in known_ids and visit(dependency):
                    return True
            active.remove(task_id)
            visited.add(task_id)
            return False

        for task_id in known_ids:
            if visit(task_id):
                errors.append(f"Task dependency cycle detected at '{task_id}'")
                break
        return errors

    def check_tool_call(self, task: Any, tool_name: str, args: dict) -> bool:
        """Check if a tool call is permitted for this task."""
        if tool_name == "run_command":
            command = args.get("command", "")
            self.check_command(command)

        if tool_name in (
            "write_file", "apply_patch", "edit_file", "insert_before",
            "insert_after", "write_docx", "write_pptx", "write_pdf",
            "promote_artifact",
        ):
            path = args.get(
                "target_path", args.get("path", args.get("file_path", ""))
            )
            protected = getattr(task, "protected_paths", []) or []
            allowed = getattr(task, "allowed_paths", []) or []
            self.check_path(path, protected, allowed)

        return True
