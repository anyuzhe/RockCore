"""ToolBroker — central security checkpoint for all AI tool execution."""

import logging
import inspect
import os
import time
from typing import Any

from orchestrator.policy_engine import PolicyEngine
from tools.file_tools import FileTools
from tools.shell_tools import ShellTools
from tools.search_tools import SearchTools
from tools.git_tools import GitTools
from tools.test_tools import TestTools

logger = logging.getLogger(__name__)


class ToolBroker:
    """Central security layer: all AI tool calls go through here."""

    def __init__(self, project_root: str | os.PathLike[str] | None,
                 policy_engine: PolicyEngine):
        self.project_root = (
            os.fspath(project_root) if project_root is not None else os.getcwd()
        )
        self.policy = policy_engine
        self.file_tools = FileTools(self.project_root)
        self.shell_tools = ShellTools(self.project_root)
        self.search_tools = SearchTools(self.project_root)
        self.git_tools = GitTools(self.project_root)
        self.test_tools = TestTools(self.project_root)

        self._tool_registry = {
            "list_files": self.file_tools.list_files,
            "read_file": self.file_tools.read_file,
            "read_pdf": self.file_tools.read_pdf,
            "write_file": self.file_tools.write_file,
            "apply_patch": self.file_tools.apply_patch,
            "insert_before": self.file_tools.insert_before,
            "insert_after": self.file_tools.insert_after,
            "search_in_file": self.file_tools.search_in_file,
            "search_code": self.search_tools.search_code,
            "read_log": self.search_tools.read_log,
            "run_command": self.shell_tools.run_command,
            "run_tests": self.test_tools.run_tests,
            "git_status": self.git_tools.git_status,
            "git_diff": self.git_tools.git_diff,
        }

    def get_tool_definitions(self, task_type: str | None = None,
                             test_authoring: bool = False) -> list[dict]:
        """Return only the tools needed by this task type."""
        definitions = [
            {
                "type": "function",
                "function": {
                    "name": "list_files",
                    "description": "List files in a directory",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Directory path relative to project root"}
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "Read a file's contents with pagination support. Returns metadata: total_lines, has_more, next_start.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to project root"},
                            "start": {"type": "integer", "description": "Start line (1-indexed). Omit for beginning."},
                            "end": {"type": "integer", "description": "End line (1-indexed). Omit for end."},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_pdf",
                    "description": (
                        "Extract text from a PDF by page range. Use this instead "
                        "of shell commands or installing PDF packages. Returns "
                        "page_count, has_more, and next_page for pagination; "
                        "reports encrypted or scanned PDFs explicitly."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "PDF path relative to project root",
                            },
                            "start_page": {
                                "type": "integer",
                                "description": "First page to extract (1-indexed)",
                            },
                            "end_page": {
                                "type": "integer",
                                "description": "Last page to extract; max 8 pages per call",
                            },
                            "max_chars": {
                                "type": "integer",
                                "description": "Maximum extracted characters (2000-16000)",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_in_file",
                    "description": "Search for text within a specific file. Returns matching line numbers with context. Use this to locate where to make changes before reading.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to project root"},
                            "text": {"type": "string", "description": "Text to search for (case-insensitive)"},
                            "context": {"type": "integer", "description": "Lines of context around each match (default 3)"},
                        },
                        "required": ["path", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": (
                        "Write content to a file (creates dirs if needed). "
                        "Existing files keep their detected encoding by default."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to project root"},
                            "content": {"type": "string", "description": "File content"},
                            "encoding": {
                                "type": "string",
                                "enum": ["preserve", "utf-8", "utf-8-sig", "utf-16", "gb18030", "gbk", "cp936", "cp1252"],
                                "description": (
                                    "Text encoding. Use preserve (default) for existing files; "
                                    "select utf-8 only when an encoding conversion is intended."
                                ),
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "insert_before",
                    "description": "Insert text before a unique anchor text. Safer than apply_patch for adding new sections.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path"},
                            "anchor": {"type": "string", "description": "Text to insert before (must be unique). Use search_in_file first."},
                            "content": {"type": "string", "description": "Text to insert before the anchor"},
                        },
                        "required": ["path", "anchor", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "insert_after",
                    "description": "Insert text after a unique anchor text. Ideal for appending new entries.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path"},
                            "anchor": {"type": "string", "description": "Text to insert after (must be unique)"},
                            "content": {"type": "string", "description": "Text to insert after the anchor"},
                        },
                        "required": ["path", "anchor", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "apply_patch",
                    "description": "Search and replace text in a file. Returns match_count and reason on failure: 0 matches = whitespace issue; >1 matches = need more context to make unique.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path"},
                            "search": {"type": "string", "description": "Text to find"},
                            "replace": {"type": "string", "description": "Replacement text"},
                        },
                        "required": ["path", "search", "replace"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_code",
                    "description": "Search for a pattern in code files",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "pattern": {"type": "string", "description": "Search pattern (plain text or regex)"},
                            "path": {"type": "string", "description": "Directory to search"},
                            "glob_pattern": {"type": "string", "description": "Optional file glob filter (e.g. *.py)"},
                        },
                        "required": ["pattern"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_command",
                    "description": "Run a shell command (restricted to allowed commands)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Command to run"},
                            "timeout": {"type": "integer", "description": "Timeout in seconds"},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_tests",
                    "description": "Run tests (pytest, etc.)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string", "description": "Test command"},
                            "timeout": {"type": "integer", "description": "Timeout in seconds"},
                        },
                        "required": ["command"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "git_status",
                    "description": "Show current git status",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "git_diff",
                    "description": "Show git diff of uncommitted changes",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "staged": {"type": "boolean", "description": "Show staged diff only"}
                        },
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_log",
                    "description": "Read the last N lines of a log file",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Log file path"},
                            "tail": {"type": "integer", "description": "Number of lines"},
                        },
                        "required": ["path"],
                    },
                },
            },
        ]
        if task_type in {"analysis", "review"}:
            allowed = {
                "list_files", "read_file", "read_pdf", "search_in_file", "search_code",
                "git_status", "git_diff", "read_log",
            }
        elif task_type == "testing" and not test_authoring:
            allowed = {"run_tests", "run_command", "git_diff", "git_status"}
        elif task_type in {"coding", "testing"}:
            allowed = {
                "list_files", "read_file", "read_pdf", "search_in_file", "search_code",
                "write_file", "apply_patch", "insert_before", "insert_after",
                "run_command", "run_tests", "git_status", "git_diff",
            }
        else:
            return definitions
        return [
            definition for definition in definitions
            if definition["function"]["name"] in allowed
        ]

    async def execute(self, task, tool_name: str, args: dict) -> dict:
        """Execute a tool call with policy enforcement."""
        start = time.time()

        handler = self._tool_registry.get(tool_name)
        if not handler:
            return {
                "status": "error",
                "error": f"Unknown tool: {tool_name}",
                "tool": tool_name,
            }

        args, ignored_arguments = self._normalize_tool_arguments(handler, args)
        if ignored_arguments:
            logger.info(
                "Ignored unsupported arguments for %s: %s",
                tool_name, ", ".join(ignored_arguments),
            )

        # 1. Policy check
        try:
            self.policy.check_tool_call(task, tool_name, args)
        except Exception as e:
            logger.warning(f"Tool call rejected: {tool_name} -> {e}")
            return {
                "status": "rejected",
                "error": str(e),
                "tool": tool_name,
                "duration_ms": int((time.time() - start) * 1000),
            }

        # 2. Execute
        try:
            result = await handler(**args)
            duration = int((time.time() - start) * 1000)
            if isinstance(result, dict):
                result["tool"] = tool_name
                result["duration_ms"] = duration
                if ignored_arguments:
                    result["ignored_arguments"] = ignored_arguments
            return result
        except Exception as e:
            logger.error(f"Tool execution error: {tool_name}: {e}")
            return {
                "status": "error",
                "error": str(e),
                "tool": tool_name,
                "duration_ms": int((time.time() - start) * 1000),
            }

    @staticmethod
    def _normalize_tool_arguments(handler, args: dict) -> tuple[dict, list[str]]:
        """Drop provider-added metadata unsupported by the concrete handler."""
        values = dict(args) if isinstance(args, dict) else {}
        try:
            parameters = inspect.signature(handler).parameters.values()
        except (TypeError, ValueError):
            return values, []
        if any(parameter.kind == inspect.Parameter.VAR_KEYWORD
               for parameter in parameters):
            return values, []
        allowed = {
            parameter.name for parameter in parameters
            if parameter.kind in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        }
        ignored = sorted(set(values) - allowed)
        return {key: value for key, value in values.items() if key in allowed}, ignored

    def set_project_root(self, project_root: str | os.PathLike[str] | None):
        """Update the project root (called per-job to match the actual project)."""
        self.project_root = (
            os.fspath(project_root) if project_root is not None else os.getcwd()
        )
        self.file_tools = FileTools(self.project_root)
        self.shell_tools = ShellTools(self.project_root)
        self.search_tools = SearchTools(self.project_root)
        self.git_tools = GitTools(self.project_root)
        self.test_tools = TestTools(self.project_root)
        # Rebuild registry so bound methods point to the new tool instances
        self._tool_registry = {
            "list_files": self.file_tools.list_files,
            "read_file": self.file_tools.read_file,
            "read_pdf": self.file_tools.read_pdf,
            "write_file": self.file_tools.write_file,
            "apply_patch": self.file_tools.apply_patch,
            "insert_before": self.file_tools.insert_before,
            "insert_after": self.file_tools.insert_after,
            "search_in_file": self.file_tools.search_in_file,
            "search_code": self.search_tools.search_code,
            "read_log": self.search_tools.read_log,
            "run_command": self.shell_tools.run_command,
            "run_tests": self.test_tools.run_tests,
            "git_status": self.git_tools.git_status,
            "git_diff": self.git_tools.git_diff,
        }
        logger.info(f"ToolBroker project root updated: {project_root}")

    def get_available_tools(self) -> list[str]:
        return list(self._tool_registry.keys())
