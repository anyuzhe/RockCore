"""Policy engine — pure code enforcement of AI constraints."""

import fnmatch
import logging
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

        for task in plan.get("tasks", []):
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
        return errors

    def check_tool_call(self, task: Any, tool_name: str, args: dict) -> bool:
        """Check if a tool call is permitted for this task."""
        if tool_name == "run_command":
            command = args.get("command", "")
            self.check_command(command)

        if tool_name in ("write_file", "apply_patch", "edit_file", "insert_before", "insert_after"):
            path = args.get("path", args.get("file_path", ""))
            protected = getattr(task, "protected_paths", []) or []
            allowed = getattr(task, "allowed_paths", []) or []
            self.check_path(path, protected, allowed)

        return True
