"""ToolBroker — central security checkpoint for all AI tool execution."""

import copy
import json
import logging
import inspect
import os
import time
import hashlib
from pathlib import Path
from typing import Any

from mcp_runtime.manager import MCPManager
from mcp_runtime.trust import is_project_mcp_approved
from orchestrator.agent_config import MCPConfig, MCPServerConfig
from orchestrator.policy_engine import PolicyEngine
from tools.file_tools import FileTools
from tools.artifact_tools import ArtifactTools
from tools.shell_tools import ShellTools
from tools.search_tools import SearchTools
from tools.git_tools import GitTools
from tools.test_tools import TestTools
from tools.runtime_tools import TaskRuntimeTools
from tools.tool_pipeline import (
    SessionToolMiddleware, ToolExecutionContext, ToolPipeline,
)

logger = logging.getLogger(__name__)

_READ_CACHE_TOOLS = {"read_file", "search_in_file", "search_code"}
_CACHE_INVALIDATING_TOOLS = {
    "write_file",
    "apply_patch",
    "insert_before",
    "insert_after",
    "write_docx",
    "write_pptx",
    "write_pdf",
    "write_temp_file",
    "promote_artifact",
    "run_command",
    "run_tests",
}
_UNSUCCESSFUL_WRITE_STATUSES = {
    "ambiguous",
    "dependency_missing",
    "encoding_error",
    "error",
    "failed",
    "no_match",
    "rejected",
}


class ToolBroker:
    """Central security layer: all AI tool calls go through here."""

    def __init__(self, project_root: str | os.PathLike[str] | None,
                 policy_engine: PolicyEngine,
                 mcp_manager: MCPManager | None = None):
        self.project_root = (
            os.fspath(project_root) if project_root is not None else os.getcwd()
        )
        self.policy = policy_engine
        self.mcp_manager = mcp_manager
        self.file_tools = FileTools(self.project_root)
        self.artifact_tools = ArtifactTools(self.project_root)
        self.shell_tools = ShellTools(self.project_root)
        self.search_tools = SearchTools(self.project_root)
        self.git_tools = GitTools(self.project_root)
        self.test_tools = TestTools(self.project_root)
        self.runtime_tools: TaskRuntimeTools | None = None
        # A broker is scoped to one worker task. Keep exact repeated reads in
        # memory and invalidate them whenever that task successfully writes.
        self._read_cache: dict[tuple[str, str], dict] = {}
        self._file_observations: dict[str, str] = {}
        self._pipeline = ToolPipeline()
        self.session_runtime = None

        self._tool_registry = {
            "list_files": self.file_tools.list_files,
            "read_file": self.file_tools.read_file,
            "read_pdf": self.file_tools.read_pdf,
            "read_docx": self.artifact_tools.read_docx,
            "write_docx": self.artifact_tools.write_docx,
            "read_pptx": self.artifact_tools.read_pptx,
            "write_pptx": self.artifact_tools.write_pptx,
            "write_pdf": self.artifact_tools.write_pdf,
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
                             test_authoring: bool = False,
                             skills: list[str] | None = None) -> list[dict]:
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
                        "Existing files keep their detected encoding by default. "
                        "Keep content under 12000 characters; for larger files "
                        "write a valid skeleton and add sections with insert tools."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path relative to project root"},
                            "content": {
                                "type": "string",
                                "maxLength": 12000,
                                "description": "Complete file content, at most 12000 characters",
                            },
                            "encoding": {
                                "type": "string",
                                "enum": ["preserve", "utf-8", "utf-8-sig", "utf-16", "gb18030", "gbk", "cp936", "cp1252"],
                                "description": (
                                    "Text encoding. Use preserve (default) for existing files; "
                                    "select utf-8 only when an encoding conversion is intended."
                                ),
                            },
                            "purpose": {
                                "type": "string",
                                "enum": ["final", "intermediate"],
                                "description": (
                                    "Use final only for a user-requested project artifact; "
                                    "intermediate is redirected to the private task runtime."
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
                    "description": "Insert up to 12000 characters before a unique anchor text. Safer than apply_patch for adding new sections.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path"},
                            "anchor": {"type": "string", "description": "Text to insert before (must be unique). Use search_in_file first."},
                            "content": {
                                "type": "string", "maxLength": 12000,
                                "description": "Text to insert before the anchor",
                            },
                        },
                        "required": ["path", "anchor", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "insert_after",
                    "description": "Insert up to 12000 characters after a unique anchor text. Ideal for appending new entries.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path"},
                            "anchor": {"type": "string", "description": "Text to insert after (must be unique)"},
                            "content": {
                                "type": "string", "maxLength": 12000,
                                "description": "Text to insert after the anchor",
                            },
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
                            "replace": {
                                "type": "string", "maxLength": 12000,
                                "description": "Replacement text, at most 12000 characters",
                            },
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
        if self.runtime_tools is not None:
            definitions.extend(self._runtime_tool_definitions())
        selected_skills = set(skills or [])
        artifact_definitions = self._artifact_definitions()
        if "documents" in selected_skills:
            definitions.extend(
                item for item in artifact_definitions
                if item["function"]["name"] in {"read_docx", "write_docx"}
            )
        if "presentations" in selected_skills:
            definitions.extend(
                item for item in artifact_definitions
                if item["function"]["name"] in {"read_pptx", "write_pptx"}
            )
        if "pdf" in selected_skills:
            definitions.extend(
                item for item in artifact_definitions
                if item["function"]["name"] == "write_pdf"
            )
        if task_type in {"analysis", "review"}:
            allowed = {
                "list_files", "read_file", "read_pdf", "search_in_file", "search_code",
                "read_docx", "read_pptx",
                "git_status", "git_diff", "read_log",
            }
        elif task_type == "testing" and not test_authoring:
            allowed = {"run_tests", "run_command", "git_diff", "git_status"}
        elif task_type in {"coding", "testing"}:
            allowed = {
                "list_files", "read_file", "read_pdf", "search_in_file", "search_code",
                "read_docx", "read_pptx", "write_docx", "write_pptx", "write_pdf",
                "write_file", "apply_patch", "insert_before", "insert_after",
                "write_temp_file", "read_temp_file", "list_temp_files",
                "promote_artifact",
                "run_command", "run_tests", "git_status", "git_diff",
            }
        elif task_type == "action":
            allowed = {
                "list_files", "read_file", "read_pdf", "search_in_file",
                "search_code", "git_status", "git_diff", "read_log",
            }
        else:
            allowed = None
        local_definitions = (
            definitions if allowed is None else [
                definition for definition in definitions
                if definition["function"]["name"] in allowed
            ]
        )
        if self.mcp_manager:
            local_definitions += self.mcp_manager.tool_definitions(task_type)
        return local_definitions

    def set_session_runtime(self, session_runtime) -> None:
        self.session_runtime = session_runtime
        self._pipeline.clear()
        if session_runtime is not None:
            self._pipeline.add(SessionToolMiddleware(session_runtime))

    async def execute(self, task, tool_name: str, args: dict) -> dict:
        context = ToolExecutionContext(
            task=task,
            tool_name=str(tool_name),
            arguments=dict(args or {}),
            metadata={"mutating": bool(
                tool_name in _CACHE_INVALIDATING_TOOLS
                or self.is_mutating_mcp_tool(str(tool_name))
            )},
        )
        return await self._pipeline.execute(
            context,
            lambda: self._execute_impl(task, tool_name, dict(args or {})),
        )

    async def _execute_impl(self, task, tool_name: str, args: dict) -> dict:
        """Execute a tool call with policy enforcement."""
        start = time.time()

        requested_tool_name = tool_name
        routed_to_runtime = False
        explicit_final_requested = False
        handler = None
        if self.runtime_tools is not None:
            path = str((args or {}).get("path") or "")
            purpose = str((args or {}).get("purpose") or "")
            if (
                tool_name in {"write_file", "apply_patch", "insert_before", "insert_after"}
                and self.runtime_tools.is_protected_input(path)
            ):
                return {
                    "status": "rejected",
                    "error": f"Refusing to modify a declared document input: {path}",
                    "tool": tool_name,
                    "duration_ms": int((time.time() - start) * 1000),
                }
            explicit_final_requested = (
                tool_name == "write_file"
                and purpose.strip().lower() == "final"
            )
            if (
                tool_name == "write_file"
                and self.runtime_tools.should_route_intermediate(
                    task, path, purpose
                )
            ):
                tool_name = "write_temp_file"
                handler = self.runtime_tools.write_temp_file
                routed_to_runtime = True
            elif (
                tool_name == "read_file"
                and self.runtime_tools.has_temp_file(path)
                and not (self.file_tools.project_root / path).is_file()
            ):
                tool_name = "read_temp_file"
                handler = self.runtime_tools.read_temp_file
                routed_to_runtime = True
            elif (
                tool_name in {"apply_patch", "insert_before", "insert_after"}
                and self.runtime_tools.has_temp_file(path)
            ):
                handler = {
                    "apply_patch": self.runtime_tools.apply_temp_patch,
                    "insert_before": self.runtime_tools.insert_temp_before,
                    "insert_after": self.runtime_tools.insert_temp_after,
                }[tool_name]
                tool_name = "write_temp_file"
                routed_to_runtime = True
        handler = handler or self._tool_registry.get(tool_name)
        is_mcp = bool(
            self.mcp_manager and self.mcp_manager.has_tool(tool_name)
        )
        if not handler and not is_mcp:
            return {
                "status": "error",
                "error": f"Unknown tool: {tool_name}",
                "tool": tool_name,
            }

        if handler:
            args, ignored_arguments = self._normalize_tool_arguments(handler, args)
        else:
            args = dict(args) if isinstance(args, dict) else {}
            ignored_arguments = []
        observed_version = ""
        observed_path = ""
        if (
            requested_tool_name in {
                "write_file", "apply_patch", "insert_before", "insert_after",
            }
            and not routed_to_runtime
        ):
            observed_path = self._normalize_observation_path(
                str(args.get("path") or "")
            )
            observed_version = self._file_observations.get(observed_path, "")
            if observed_version:
                args["expected_version"] = observed_version
        if ignored_arguments:
            logger.info(
                "Ignored unsupported arguments for %s: %s",
                tool_name, ", ".join(ignored_arguments),
            )

        # 1. Policy check
        try:
            self.policy.check_tool_call(task, tool_name, args)
            if (
                is_mcp
                and getattr(task, "task_type", "") in {
                    "analysis", "review", "testing",
                }
                and not self.mcp_manager.tool_is_read_only(tool_name)
            ):
                raise PermissionError(
                    "Read-only tasks cannot call a mutating MCP tool"
                )
        except Exception as e:
            logger.warning(f"Tool call rejected: {tool_name} -> {e}")
            return {
                "status": "rejected",
                "error": str(e),
                "tool": tool_name,
                "duration_ms": int((time.time() - start) * 1000),
            }

        cache_key = self._read_cache_key(requested_tool_name, args)
        if cache_key is not None and cache_key in self._read_cache:
            entry = self._read_cache[cache_key]
            if self._cache_entry_is_current(entry):
                result = copy.deepcopy(entry["result"])
                result["tool"] = requested_tool_name
                result["duration_ms"] = 0
                result["cache_hit"] = True
                if routed_to_runtime:
                    result["redirected_to_runtime"] = True
                if ignored_arguments:
                    result["ignored_arguments"] = ignored_arguments
                return result
            self._read_cache.pop(cache_key, None)

        # 2. Execute
        try:
            result = (
                await self.mcp_manager.call_tool(tool_name, args)
                if is_mcp else await handler(**args)
            )
            duration = int((time.time() - start) * 1000)
            if isinstance(result, dict):
                mutating_mcp_succeeded = (
                    is_mcp
                    and not self.mcp_manager.tool_is_read_only(tool_name)
                    and self._write_succeeded(result)
                )
                command_may_have_written = requested_tool_name in {
                    "run_command", "run_tests",
                }
                if mutating_mcp_succeeded or command_may_have_written:
                    self._read_cache.clear()
                elif (
                    requested_tool_name in _CACHE_INVALIDATING_TOOLS
                    or tool_name in _CACHE_INVALIDATING_TOOLS
                ) and self._write_succeeded(result):
                    changed_path = str(
                        result.get("path") or args.get("path") or ""
                    )
                    if changed_path:
                        self._invalidate_read_cache_for_path(changed_path)
                    else:
                        self._read_cache.clear()
                elif cache_key is not None and self._result_is_cacheable(result):
                    dependency = self._cache_dependency(
                        requested_tool_name, args
                    )
                    self._read_cache[cache_key] = {
                        "result": copy.deepcopy(result),
                        "dependency": dependency,
                        "version": self._dependency_version(dependency),
                    }
                result["tool"] = requested_tool_name
                result["duration_ms"] = duration
                if routed_to_runtime:
                    result["redirected_to_runtime"] = True
                elif (
                    explicit_final_requested
                    and self.runtime_tools is not None
                    and result.get("status") not in {"error", "rejected"}
                    and not result.get("error")
                ):
                    self.runtime_tools.mark_explicit_final(
                        str((args or {}).get("path") or "")
                    )
                if ignored_arguments:
                    result["ignored_arguments"] = ignored_arguments
                self._remember_file_observation(
                    requested_tool_name, args, result
                )
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
    def _read_cache_key(tool_name: str, args: dict) -> tuple[str, str] | None:
        if tool_name not in _READ_CACHE_TOOLS:
            return None
        serialized = json.dumps(
            args, sort_keys=True, ensure_ascii=False, default=str,
            separators=(",", ":"),
        )
        return tool_name, serialized

    def _normalize_observation_path(self, raw_path: str) -> str:
        try:
            resolved = (Path(self.project_root) / raw_path).resolve()
            resolved.relative_to(Path(self.project_root).resolve())
            return resolved.as_posix()
        except (OSError, ValueError):
            return ""

    def _remember_file_observation(self, tool_name: str, args: dict,
                                   result: dict) -> None:
        if tool_name not in {
            "read_file", "search_in_file", "write_file", "apply_patch",
            "insert_before", "insert_after",
        }:
            return
        path = self._normalize_observation_path(
            str(result.get("path") or args.get("path") or "")
        )
        version = str(result.get("source_version") or "")
        if path and version:
            self._file_observations[path] = version

    def _cache_dependency(self, tool_name: str, args: dict) -> dict:
        raw_path = str(args.get("path") or ".")
        try:
            resolved = (Path(self.project_root) / raw_path).resolve()
            resolved.relative_to(Path(self.project_root).resolve())
        except (OSError, ValueError):
            resolved = Path(self.project_root).resolve()
        return {
            "kind": "tree" if tool_name == "search_code" else "file",
            "path": str(resolved),
            "glob_pattern": str(args.get("glob_pattern") or ""),
        }

    @staticmethod
    def _dependency_version(dependency: dict) -> str:
        path = Path(str(dependency.get("path") or ""))
        try:
            if dependency.get("kind") == "file":
                stat = path.stat()
                return f"{stat.st_mtime_ns}:{stat.st_size}"
            entries = []
            glob_pattern = str(dependency.get("glob_pattern") or "")
            for root, dirs, files in os.walk(path):
                dirs[:] = [
                    name for name in dirs
                    if not name.startswith(".") and name not in {
                        "node_modules", "__pycache__", ".venv", "venv",
                        "env", "dist", "build",
                    }
                ]
                for name in files:
                    if glob_pattern:
                        from fnmatch import fnmatch
                        if not fnmatch(name, glob_pattern):
                            continue
                    candidate = Path(root) / name
                    stat = candidate.stat()
                    entries.append(
                        f"{candidate.relative_to(path).as_posix()}:"
                        f"{stat.st_mtime_ns}:{stat.st_size}"
                    )
            return hashlib.sha256(
                "\n".join(sorted(entries)).encode("utf-8")
            ).hexdigest()
        except OSError:
            return "missing"

    def _cache_entry_is_current(self, entry: dict) -> bool:
        dependency = dict(entry.get("dependency") or {})
        return bool(dependency) and entry.get("version") == (
            self._dependency_version(dependency)
        )

    def _invalidate_read_cache_for_path(self, raw_path: str):
        try:
            changed = (Path(self.project_root) / raw_path).resolve()
            changed.relative_to(Path(self.project_root).resolve())
        except (OSError, ValueError):
            self._read_cache.clear()
            return
        stale = []
        for key, entry in self._read_cache.items():
            dependency = dict(entry.get("dependency") or {})
            dependency_path = Path(str(dependency.get("path") or ""))
            if dependency.get("kind") == "tree":
                try:
                    changed.relative_to(dependency_path)
                except ValueError:
                    continue
                stale.append(key)
            elif dependency_path == changed:
                stale.append(key)
        for key in stale:
            self._read_cache.pop(key, None)

    @staticmethod
    def _result_is_cacheable(result: dict) -> bool:
        return not result.get("error") and str(
            result.get("status") or ""
        ).lower() not in _UNSUCCESSFUL_WRITE_STATUSES

    @staticmethod
    def _write_succeeded(result: dict) -> bool:
        return ToolBroker._result_is_cacheable(result)

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
        self._read_cache.clear()
        self._file_observations.clear()
        self.project_root = (
            os.fspath(project_root) if project_root is not None else os.getcwd()
        )
        self.file_tools = FileTools(self.project_root)
        self.artifact_tools = ArtifactTools(self.project_root)
        self.shell_tools = ShellTools(self.project_root)
        self.search_tools = SearchTools(self.project_root)
        self.git_tools = GitTools(self.project_root)
        self.test_tools = TestTools(self.project_root)
        self.runtime_tools = None
        # Rebuild registry so bound methods point to the new tool instances
        self._tool_registry = {
            "list_files": self.file_tools.list_files,
            "read_file": self.file_tools.read_file,
            "read_pdf": self.file_tools.read_pdf,
            "read_docx": self.artifact_tools.read_docx,
            "write_docx": self.artifact_tools.write_docx,
            "read_pptx": self.artifact_tools.read_pptx,
            "write_pptx": self.artifact_tools.write_pptx,
            "write_pdf": self.artifact_tools.write_pdf,
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

    def configure_task_runtime(
        self,
        state_root: str | os.PathLike[str],
        job_id: str,
        task_id: str,
        final_outputs: list[str] | None = None,
        input_paths: list[str] | None = None,
        require_declared_outputs: bool = False,
        source_job_id: str = "",
    ) -> dict:
        """Attach a private scratch directory to this task-scoped broker."""
        self._read_cache.clear()
        self.runtime_tools = TaskRuntimeTools(
            self.project_root,
            state_root,
            job_id,
            task_id,
            final_outputs=final_outputs,
            input_paths=input_paths,
            require_declared_outputs=require_declared_outputs,
            source_job_id=source_job_id,
        )
        self._tool_registry.update({
            "write_temp_file": self.runtime_tools.write_temp_file,
            "read_temp_file": self.runtime_tools.read_temp_file,
            "list_temp_files": self.runtime_tools.list_temp_files,
            "promote_artifact": self.runtime_tools.promote_artifact,
        })
        self.shell_tools.set_temp_directory(self.runtime_tools.root)
        return self.runtime_tools.checkpoint()

    def relocate_task_intermediates(self, added_paths: list[str]) -> list[dict]:
        if self.runtime_tools is None:
            return []
        return self.runtime_tools.relocate_project_intermediates(added_paths)

    def task_runtime_checkpoint(self) -> dict:
        return self.runtime_tools.checkpoint() if self.runtime_tools else {}

    def cleanup_task_runtime(self) -> dict:
        return (
            self.runtime_tools.cleanup()
            if self.runtime_tools else {"status": "not_configured"}
        )

    @staticmethod
    def _runtime_tool_definitions() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "write_temp_file",
                    "description": (
                        "Write an intermediate task file outside the project tree. "
                        "Use for PDF page text, extracted chunks, OCR, notes, drafts, "
                        "and other helper data that must not appear in Git or project root."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Task-local temporary path"},
                            "content": {"type": "string", "maxLength": 12000},
                            "encoding": {
                                "type": "string",
                                "enum": ["preserve", "utf-8", "utf-8-sig", "utf-16", "gb18030", "gbk", "cp936", "cp1252"],
                            },
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_temp_file",
                    "description": "Read a task-local intermediate file with line pagination.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_temp_files",
                    "description": "List files in this task's private intermediate workspace.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "promote_artifact",
                    "description": (
                        "Atomically copy a completed temporary file to a declared final "
                        "project output path. The temporary source remains until success cleanup."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "temp_path": {"type": "string"},
                            "target_path": {"type": "string"},
                            "overwrite": {"type": "boolean"},
                        },
                        "required": ["temp_path", "target_path"],
                    },
                },
            },
        ]

    async def configure_mcp(self, project_root: str | os.PathLike[str],
                            config: MCPConfig | None = None,
                            trusted_servers: list[MCPServerConfig] | None = None,
                            ) -> dict[str, dict]:
        """Configure external tools without affecting the local tool registry."""
        if self.mcp_manager is None:
            self.mcp_manager = MCPManager(project_root)
        project_config = config or MCPConfig()
        project_approved = not project_config.enabled or is_project_mcp_approved(
            project_root, project_config
        )
        servers: list[MCPServerConfig] = list(trusted_servers or [])
        if project_config.enabled and project_approved:
            servers.extend(project_config.servers)
        unique_servers = {
            server.name: server for server in servers if server.name
        }
        effective = MCPConfig(
            enabled=bool(unique_servers),
            servers=list(unique_servers.values()),
        )
        statuses = await self.mcp_manager.configure(project_root, effective)
        if project_config.enabled and not project_approved:
            statuses["policy"] = {
                "status": "approval_required",
                "tools": 0,
                "error": (
                    "项目 MCP 配置尚未由本机用户批准；请在项目设置的 "
                    "MCP 页确认并保存"
                ),
            }
        return statuses

    async def close(self):
        if self.mcp_manager:
            await self.mcp_manager.close()

    def is_read_only_mcp_tool(self, tool_name: str) -> bool:
        return bool(
            self.mcp_manager
            and self.mcp_manager.has_tool(tool_name)
            and self.mcp_manager.tool_is_read_only(tool_name)
        )

    def is_mutating_mcp_tool(self, tool_name: str) -> bool:
        return bool(
            self.mcp_manager
            and self.mcp_manager.has_tool(tool_name)
            and not self.mcp_manager.tool_is_read_only(tool_name)
        )

    def get_available_tools(self) -> list[str]:
        names = list(self._tool_registry.keys())
        if self.mcp_manager:
            names.extend(
                definition["function"]["name"]
                for definition in self.mcp_manager.tool_definitions()
            )
        return names

    @staticmethod
    def _artifact_definitions() -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "read_docx",
                    "description": "Read Word .docx text and tables in paginated blocks.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_block": {"type": "integer"},
                            "max_blocks": {"type": "integer"},
                            "max_chars": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_docx",
                    "description": "Create a local Word .docx from Markdown-like text.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "read_pptx",
                    "description": "Read PowerPoint slide text with pagination.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_slide": {"type": "integer"},
                            "max_slides": {"type": "integer"},
                            "max_chars": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_pptx",
                    "description": "Create a local PowerPoint .pptx from structured slides.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "title": {"type": "string"},
                            "subtitle": {"type": "string"},
                            "slides": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "bullets": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["title", "bullets"],
                                },
                            },
                        },
                        "required": ["path", "slides"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "write_pdf",
                    "description": "Create a local PDF with Chinese text support.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "title": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["path", "content"],
                    },
                },
            },
        ]
