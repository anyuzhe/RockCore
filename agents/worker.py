"""DeepSeek Worker Agent — executes individual tasks via tool calls."""

import json
import logging
import math
import re
from typing import Any

from orchestrator.model_router import ModelRouter
from tools.tool_broker import ToolBroker

logger = logging.getLogger(__name__)

WRITE_TOOLS = {"write_file", "apply_patch", "insert_before", "insert_after"}
REPORT_TASK_TYPES = {"analysis", "review", "testing"}
ALREADY_SATISFIED_MARKER = "[ALREADY_SATISFIED]"
STATE_VERIFICATION_TOOLS = {
    "read_file", "read_pdf", "search_in_file", "search_code", "git_diff",
}
# Some review-oriented coding tasks intentionally edit files only when a defect
# is found. Keep this narrow so a worker that forgot to edit a normal coding
# task is still caught as a failure.
NO_CHANGE_MARKERS = (
    "仅当发现", "如有问题", "若未发现", "无问题时", "无需修改",
    "没有问题则跳过", "若无问题", "没有缺陷则跳过",
    "only if", "if needed", "skip if no", "no changes required",
    "if no issues", "if there are no issues", "skip when no",
)
EXPLORATION_TOOLS = {
    "list_files", "read_file", "read_pdf", "search_in_file", "search_code",
    "git_status", "git_diff", "read_log",
}
MAX_CONVERSATION_CHARS = 14_000

WORKER_SYSTEM_PROMPT = """You are a code executor, not an investigator.

Your ONLY job is to execute the given task using the available tools.

CRITICAL RULES:
1. The selected project is authoritative. It may itself be RockCore; stay within
   the task's allowed paths and do not access unrelated user or system files.
2. Inspect only the code and configuration needed for the current task.
3. Use ONLY relative paths like "index.html", "src/main.py", etc.
4. For analysis/review tasks: inspect the project and return a concrete report. Do not
   create or modify files unless the task explicitly requests a report artifact.
5. For coding tasks: use write_file to create files, read_file to check existing ones.
6. For testing tasks: run the test command and report results.
7. For coding tasks, use at most 4 exploratory reads before you start editing.
8. After a successful patch, verify but do NOT re-explore the whole project.
9. NEVER use absolute paths. NEVER access ~/.ai_engineering_studio or similar.
10. For coding tasks, it is better to make changes and hit the turn limit than
    to keep reading and never edit.
11. If a coding task's requested state is already present before your first edit,
    inspect the relevant code and verify it. Then return [ALREADY_SATISFIED]
    followed by concrete evidence. Do not make a meaningless rewrite.
12. For PDF input, use read_pdf with page ranges. Never install PDF packages and
    never call pdftotext through run_command. For long documents, process pages
    incrementally and write/update the requested artifact as you go.

Available tools:
- list_files: List files in the project directory
- read_file: Read a file's contents with start/end line pagination
- read_pdf: Extract PDF text with start_page/end_page pagination
- write_file: Write content to a file — USE THIS, do not output code in chat
- apply_patch: Search and replace text in a file
- insert_before / insert_after: Insert text at a specific anchor point
- search_in_file: Search for text within a specific file
- search_code: Search for text across project files
- run_command: Run a shell command
- git_status: Check git status
- git_diff: Show git diff

CRITICAL: NEVER output code, HTML, or file content in your text response.
When creating or modifying code, you MUST use write_file, apply_patch,
insert_before, or insert_after. A coding task is NOT complete until files have
been written to the workspace. An analysis task is complete when its final
response contains a substantive report. Keep other responses brief.
"""


class WorkerAgent:
    """DeepSeek Worker: executes tasks using tools."""

    def __init__(self, model_router: ModelRouter, tool_broker: ToolBroker,
                 max_turns: int = 25, max_exploration_turns: int = 4,
                 context_manager=None):
        self.model_router = model_router
        self.tool_broker = tool_broker
        self.agent_type = "worker"
        self.max_turns = max_turns
        self.max_exploration_turns = max_exploration_turns
        self.context_manager = context_manager

    def scoped_to(self, project_root: str) -> "WorkerAgent":
        """Create an isolated worker whose tools all target one task workspace."""
        broker = ToolBroker(project_root, self.tool_broker.policy)
        return WorkerAgent(
            self.model_router,
            broker,
            max_turns=self.max_turns,
            max_exploration_turns=self.max_exploration_turns,
            context_manager=self.context_manager,
        )

    async def run(self, task, project=None, project_root: str | None = None,
                  provider_override: str | None = None,
                  model_override: str | None = None,
                  recovery_context: str = "") -> dict:
        """Execute a single task using the tool-calling loop."""
        logger.info(f"Worker: executing task {task.task_id}: {task.title}")

        project_root = project_root or (project.root_path if project else ".")

        # Inject task-specific context
        task_memory_context = ""
        if self.context_manager:
            try:
                task_memory_context = await self.context_manager.build_task_context(task)
                if task_memory_context:
                    task_memory_context = f"\n\nRelevant Project Context:\n{task_memory_context}\n"
            except Exception as e:
                logger.warning(f"Context manager failed: {e}")

        system_prompt = WORKER_SYSTEM_PROMPT

        task_context = f"""
Task: {task.task_id} - {task.title}
Description: {task.description}
Type: {task.task_type}
Allowed Paths: {task.allowed_paths or 'all'}
Acceptance Command: {task.acceptance_command or 'none'}
{task_memory_context[:4000]}
"""

        if task.task_type in {"analysis", "review"}:
            task_context += (
                "\nThis is a read-only review task. Inspect only the relevant files, "
                "then return a concrete findings report. Do not modify project files. "
                "After the evidence is sufficient, stop using tools and write the report."
            )
        elif task.task_type == "coding":
            task_context += "\nRead existing code, then implement the changes. Verify with git_diff."
        elif task.task_type == "testing":
            task_context += "\nWrite tests and verify they pass."
        document_text = (
            f"{task.title} {task.description} {task.allowed_paths}"
        ).lower()
        is_document_task = any(marker in document_text for marker in (
            ".pdf", "pdf", "文档", "书籍", "全书", "document", "book",
        ))
        finalization_mode = bool(
            getattr(task, "_rockcore_finalization_mode", False)
        )
        if is_document_task:
            task_context += (
                "\nThis is a document-processing task. Use read_pdf directly "
                "and paginate with next_page. Do not install dependencies or "
                "retry shell-based PDF extraction. For a long source, write "
                "the output incrementally so completed page ranges are preserved."
            )
        if finalization_mode:
            task_context += (
                "\nFINALIZATION MODE: useful artifacts already exist. Do not "
                "restart source reading or broad exploration. Inspect only the "
                "generated outputs, run one focused deterministic check, repair "
                "only a concrete defect if found, then return the final completion "
                "response. Never regenerate a complete document from scratch."
            )
        if recovery_context:
            task_context += (
                "\n\nThis is a focused continuation after an earlier attempt. "
                "Keep existing useful changes, avoid repeating broad exploration, "
                "and finish the task now.\nRecovery guidance:\n"
                + recovery_context[:4000]
            )

        messages = [{"role": "user", "content": task_context}]
        total_input = 0
        total_output = 0
        tool_calls_made = []
        final_content = ""
        no_changes_declared = False
        exploration_calls = 0
        has_written = False
        seen_exploration_calls: set[tuple[str, str, bool]] = set()
        progress_warning_sent = False
        finish_warning_sent = False
        premature_completion_count = 0
        empty_report_count = 0
        force_tool_call = False
        verified_existing_state = False
        pending_document_pages: dict[str, int] = {}
        allow_no_change = self._allows_no_change(task)
        progress_warning_turn = max(1, math.ceil(self.max_turns * 0.70))
        finish_warning_turn = max(1, math.ceil(self.max_turns * 0.85))

        try:
            for turn in range(self.max_turns):
                if turn >= finish_warning_turn and not finish_warning_sent:
                    messages.append({
                        "role": "user",
                        "content": (
                            "You have used 85% of the task budget. Stop broad work, "
                            "complete the required edits, run one focused verification, "
                            "and return the final completion response."
                        ),
                    })
                    finish_warning_sent = True
                elif (
                    turn >= progress_warning_turn
                    and not has_written
                    and not progress_warning_sent
                ):
                    messages.append({
                        "role": "user",
                        "content": (
                            "You have used 70% of the task budget without editing. "
                            "Stop investigating and apply the required change now."
                        ),
                    })
                    progress_warning_sent = True

                messages = self._compact_messages(
                    messages,
                    max_chars=26_000 if is_document_task else MAX_CONVERSATION_CHARS,
                )
                model_kwargs = (
                    {"model": model_override} if model_override else {}
                )
                response = await self.model_router.chat_with_tools(
                    self.agent_type,
                    system_prompt,
                    messages,
                    tools=self._tool_definitions(task),
                    provider_override=provider_override,
                    task=task,
                    attachments=(
                        getattr(getattr(task, "job", None), "attachments", None)
                        or []
                    ),
                    max_tokens=4096,
                    tool_choice=(
                        "required"
                        if task.task_type == "coding" and force_tool_call and not has_written
                        else "auto"
                    ),
                    **model_kwargs,
                )

                if not isinstance(response, dict):
                    return self._failure(
                        "Provider returned an invalid response object",
                        tool_calls_made, total_input, total_output,
                    )

                usage = response.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                total_input += input_tokens if isinstance(input_tokens, (int, float)) else 0
                total_output += output_tokens if isinstance(output_tokens, (int, float)) else 0

                content = response.get("content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False, default=str)
                raw_tool_calls = response.get("tool_calls") or []
                if not isinstance(raw_tool_calls, list):
                    raw_tool_calls = [raw_tool_calls]
                tool_calls = []
                malformed_calls = []
                for index, raw_call in enumerate(raw_tool_calls):
                    call = raw_call if isinstance(raw_call, dict) else {}
                    function = call.get("function") or {}
                    if not isinstance(function, dict) or not function.get("name"):
                        malformed_calls.append(f"tool call {index + 1} has no function name")
                        continue
                    arguments = function.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False, default=str)
                    tool_calls.append({
                        "id": str(call.get("id") or f"worker-{turn}-{index}"),
                        "function": {
                            "name": str(function["name"]),
                            "arguments": arguments,
                        },
                    })

                if malformed_calls:
                    messages.append({
                        "role": "user",
                        "content": (
                            "The provider returned malformed tool calls: "
                            + "; ".join(malformed_calls)
                            + ". Return a valid tool call or a final response."
                        ),
                    })
                    if not tool_calls:
                        continue

                if not tool_calls:
                    if is_document_task and pending_document_pages:
                        unread = ", ".join(
                            f"{path}: start_page={page}"
                            for path, page in sorted(
                                pending_document_pages.items()
                            )
                        )
                        messages.append({
                            "role": "assistant",
                            "content": (content or "")[-1600:],
                        })
                        messages.append({
                            "role": "user",
                            "content": (
                                "The document is not fully read yet. read_pdf "
                                "reported remaining pages. Do not declare the "
                                "task complete. Continue from: " + unread
                                + ". Preserve the existing incremental output."
                            ),
                        })
                        continue
                    if task.task_type == "coding" and not has_written:
                        already_satisfied = (
                            ALREADY_SATISFIED_MARKER in (content or "")
                            and verified_existing_state
                        )
                        if already_satisfied:
                            final_content = content.replace(
                                ALREADY_SATISFIED_MARKER, ""
                            ).strip()
                            no_changes_declared = True
                            messages.append({
                                "role": "assistant", "content": final_content,
                            })
                            break
                        if allow_no_change and (content or "").strip():
                            logger.info(
                                "Worker: task %s completed without changes "
                                "because it is conditional",
                                task.task_id,
                            )
                            final_content = content.strip()
                            no_changes_declared = True
                            messages.append({
                                "role": "assistant", "content": final_content,
                            })
                            break
                        premature_completion_count += 1
                        messages.append({
                            "role": "assistant",
                            "content": (content or "")[-1600:],
                        })
                        if premature_completion_count >= 2:
                            return {
                                "status": "failed",
                                "error": "Coding model ended without editing files",
                                "content": content or "",
                                "turns": len(tool_calls_made),
                                "tool_calls": tool_calls_made,
                                "input_tokens": total_input,
                                "output_tokens": total_output,
                            }
                        messages.append({
                            "role": "user",
                            "content": (
                                "This is a coding task and no editing tool has been "
                                "used. If you verified that the requested state is "
                                "already present, return [ALREADY_SATISFIED] with "
                                "concrete evidence. Otherwise use write_file, "
                                "apply_patch, insert_before, or insert_after now."
                            ),
                        })
                        # The text reminder alone is not reliable: some models
                        # repeatedly promise to edit without emitting a tool call.
                        # Force tool use at the API level until a write succeeds.
                        force_tool_call = True
                        continue
                    if task.task_type in REPORT_TASK_TYPES and not (content or "").strip():
                        empty_report_count += 1
                        messages.append({
                            "role": "user",
                            "content": (
                                "Your response was empty. Return a concise, concrete "
                                "report of the work or findings now."
                            ),
                        })
                        if empty_report_count < 2:
                            continue
                        return self._failure(
                            "Read-only task ended without a report",
                            tool_calls_made, total_input, total_output,
                        )
                    # No more tool calls — task is complete
                    logger.info(f"Worker: task {task.task_id} completed in {turn+1} turns")
                    final_content = content or ""
                    messages.append({"role": "assistant", "content": final_content or "Task complete."})
                    break

                # Process tool calls
                messages.append({
                    "role": "assistant",
                    # Long narrated reasoning is not needed on later turns and
                    # can crowd the actual file/tool context out of the window.
                    "content": (content or "")[-1600:],
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"]["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                })

                exploration_blocked = False
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    argument_error = None
                    try:
                        args = json.loads(tc["function"]["arguments"])
                        if not isinstance(args, dict):
                            argument_error = "Tool arguments must be a JSON object"
                            args = {}
                    except (TypeError, json.JSONDecodeError) as error:
                        argument_error = f"Invalid tool arguments: {error}"
                        args = {}

                    exploration_signature = (
                        func_name,
                        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str),
                        has_written,
                    )
                    repeated_exploration = (
                        task.task_type in {"coding", "analysis", "review"}
                        and func_name in EXPLORATION_TOOLS
                        and exploration_signature in seen_exploration_calls
                    )
                    if repeated_exploration:
                        result = {
                            "status": "rejected",
                            "error": (
                                "This exact read/search was already completed in the "
                                "current phase. Use the existing result and change strategy."
                            ),
                        }
                        exploration_blocked = True
                    elif (
                        task.task_type in {"coding", "analysis", "review"}
                        and func_name in EXPLORATION_TOOLS
                        and exploration_calls >= (
                            self.max_exploration_turns + (2 if has_written else 0)
                        )
                    ):
                        result = {
                            "status": "rejected",
                            "error": (
                            "Exploration budget exhausted. Stop reading and return "
                            "the concrete report now."
                            if task.task_type in {"analysis", "review"}
                            else
                            "Exploration budget exhausted. Use write_file, "
                            "apply_patch, insert_before, or insert_after now."
                            ),
                        }
                        exploration_blocked = True
                    elif argument_error:
                        result = {"status": "error", "error": argument_error}
                    else:
                        try:
                            result = await self.tool_broker.execute(task, func_name, args)
                        except Exception as error:
                            logger.warning(
                                "Worker tool %s failed for %s: %s",
                                func_name, task.task_id, error,
                            )
                            result = {
                                "status": "error",
                                "error": f"Tool {func_name} failed: {error}",
                            }
                        if not isinstance(result, dict):
                            result = {
                                "status": "error",
                                "error": (
                                    f"Tool {func_name} returned an invalid result; "
                                    "expected a JSON object"
                                ),
                            }
                        if (
                            func_name == "read_pdf"
                            and result.get("status") in {
                                "success", "empty_page_range",
                            }
                        ):
                            source_path = str(
                                result.get("path")
                                or args.get("path")
                                or "PDF"
                            )
                            next_page = int(result.get("next_page") or 0)
                            if result.get("has_more") and next_page > 0:
                                pending_document_pages[source_path] = next_page
                            else:
                                pending_document_pages.pop(source_path, None)
                        if result.get("status") in {
                            "password_required", "no_extractable_text",
                        }:
                            tool_calls_made.append({
                                "tool": func_name,
                                "args": args,
                                "result_status": result.get("status"),
                            })
                            reason = str(result.get("error") or "PDF cannot be read")
                            source_path = str(result.get("path") or args.get("path") or "PDF")
                            return self._failure(
                                f"USER_INPUT_REQUIRED: {source_path}: {reason}",
                                tool_calls_made,
                                total_input,
                                total_output,
                            )
                        if (
                            task.task_type == "coding"
                            and func_name in WRITE_TOOLS
                            and result.get("status") == "rejected"
                            and (
                                "[allowed_path]" in str(result.get("error", ""))
                                or "path not in allowed set" in str(
                                    result.get("error", "")
                                ).lower()
                            )
                        ):
                            tool_calls_made.append({
                                "tool": func_name,
                                "args": args,
                                "result_status": "rejected",
                            })
                            return self._failure(
                                str(result.get("error") or "Path not in allowed set"),
                                tool_calls_made,
                                total_input,
                                total_output,
                            )
                        if func_name in EXPLORATION_TOOLS:
                            exploration_calls += 1
                            seen_exploration_calls.add(exploration_signature)
                        if (
                            func_name in WRITE_TOOLS
                            and result.get("status") not in {"error", "rejected"}
                            and not result.get("error")
                        ):
                            has_written = True
                            force_tool_call = False
                        if (
                            func_name in STATE_VERIFICATION_TOOLS
                            and result.get("status") not in {"error", "rejected"}
                            and not result.get("error")
                        ):
                            verified_existing_state = True
                    tool_calls_made.append({
                        "tool": func_name,
                        "args": args,
                        "result_status": result.get("status", "error"),
                    })

                    # Format result for the model
                    result_limit = 18_000 if func_name == "read_pdf" else 1_800
                    result_str = json.dumps(
                        result, ensure_ascii=False, default=str
                    )[:result_limit]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })

                    logger.info(f"Worker: {func_name} -> {result.get('status', 'ok')}")

                if exploration_blocked:
                    messages.append({
                        "role": "user",
                        "content": (
                            "The read-only exploration limit has been reached. "
                            "Do not read or search again. Return the final concrete "
                            "review report now."
                            if task.task_type in {"analysis", "review"}
                            else
                            "The read-only exploration limit has been reached. "
                            "Do not read or search again. Apply the remaining code "
                            "changes now, or return the final completion response if "
                            "the task is fully implemented."
                        ),
                    })

            else:
                logger.warning(f"Worker: task {task.task_id} reached max turns ({self.max_turns})")
                unread_detail = ""
                if pending_document_pages:
                    unread_detail = "; document still has unread pages: " + ", ".join(
                        f"{path} start_page={page}"
                        for path, page in sorted(pending_document_pages.items())
                    )
                return {
                    "status": "failed",
                    "error": (
                        f"Max turns ({self.max_turns}) reached{unread_detail}"
                    ),
                    "turns": len(tool_calls_made),
                    "tool_calls": tool_calls_made,
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                    "document_progress": dict(pending_document_pages),
                    "has_written": has_written,
                }

            result = {
                "status": "completed",
                "content": final_content,
                "turns": len(tool_calls_made),
                "tool_calls": tool_calls_made,
                "input_tokens": total_input,
                "output_tokens": total_output,
            }
            if no_changes_declared:
                result["no_changes"] = True
            if is_document_task:
                result["document_progress"] = dict(pending_document_pages)
            return result

        except Exception as e:
            logger.error(f"Worker: task {task.task_id} failed: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "turns": len(tool_calls_made),
                "tool_calls": tool_calls_made,
            }

    @staticmethod
    def _compact_messages(messages: list[dict],
                          max_chars: int = MAX_CONVERSATION_CHARS) -> list[dict]:
        """Bound cumulative prompt growth while preserving tool-call pairs."""
        def size(message: dict) -> int:
            return len(json.dumps(message, ensure_ascii=False, default=str))

        def trim_text(value: str, limit: int) -> str:
            if len(value) <= limit:
                return value
            side = max(1, (limit - 80) // 2)
            return (
                value[:side]
                + "\n...[compacted; re-read workspace for full content]...\n"
                + value[-side:]
            )

        def compact_history_message(message: dict) -> dict:
            # Round-trip to avoid mutating the caller's conversation objects.
            compacted = json.loads(json.dumps(
                message, ensure_ascii=False, default=str
            ))
            content = compacted.get("content")
            if isinstance(content, str):
                compacted["content"] = trim_text(content, 3_500)
            for tool_call in compacted.get("tool_calls") or []:
                function = tool_call.get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str) and len(arguments) > 1_200:
                    function["arguments"] = json.dumps({
                        "note": "historical tool arguments compacted",
                    })
            return compacted

        if sum(size(message) for message in messages) <= max_chars:
            return messages
        if len(messages) <= 2:
            return messages

        head = compact_history_message(messages[0])
        head_content = head.get("content")
        if isinstance(head_content, str):
            head["content"] = trim_text(
                head_content, max(256, max_chars // 2)
            )
        groups: list[list[dict]] = []
        index = 1
        while index < len(messages):
            message = compact_history_message(messages[index])
            group = [message]
            index += 1
            if message.get("role") == "assistant" and message.get("tool_calls"):
                while index < len(messages) and messages[index].get("role") == "tool":
                    group.append(compact_history_message(messages[index]))
                    index += 1
            groups.append(group)

        note = {
            "role": "user",
            "content": (
                "Earlier tool exchanges were compacted to keep this task within "
                "its context budget. Keep using the established findings and "
                "current workspace state; do not restart broad exploration."
            ),
        }
        remaining = max_chars - size(head) - size(note)
        selected: list[list[dict]] = []
        used = 0
        for group in reversed(groups):
            group_size = sum(size(message) for message in group)
            if group_size > remaining:
                # Never send an oversized or half-paired tool exchange. The
                # compaction note tells the model to re-read authoritative
                # workspace state when the latest tool output was enormous.
                break
            selected.append(group)
            used += group_size
            if used >= remaining:
                break
        selected.reverse()
        compacted = [head, note] + [
            message for group in selected for message in group
        ]
        # JSON framing adds a little overhead beyond content limits. The group
        # selection above accounts for it without ever splitting a tool pair.
        if sum(size(item) for item in compacted) > max_chars:
            head["content"] = trim_text(
                str(head.get("content") or ""), max(64, max_chars // 3)
            )
        return compacted

    def _tool_definitions(self, task) -> list[dict]:
        """Request a compact task-specific tool schema, with legacy fallback."""
        try:
            return self.tool_broker.get_tool_definitions(
                getattr(task, "task_type", None),
                test_authoring=getattr(task, "task_type", "") == "testing",
            )
        except TypeError:
            # Small test doubles and older third-party brokers may expose the
            # original zero-argument method.
            return self.tool_broker.get_tool_definitions()

    @staticmethod
    def _failure(error: str, tool_calls: list[dict], input_tokens: int,
                 output_tokens: int) -> dict:
        return {
            "status": "failed",
            "error": error,
            "turns": len(tool_calls),
            "tool_calls": tool_calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    @staticmethod
    def _allows_no_change(task) -> bool:
        """Return whether the task explicitly permits a successful no-op."""
        text = f"{getattr(task, 'title', '')} {getattr(task, 'description', '')}".lower()
        if any(marker.lower() in text for marker in NO_CHANGE_MARKERS):
            return True
        # Common generated wording uses "如 ... 存在" instead of an explicit
        # "如有问题". Limit the match to defect-oriented terms to avoid
        # treating ordinary examples ("如 React") as conditional tasks.
        return bool(re.search(
            r"如.{0,100}(?:存在|问题|缺陷|不一致|错误|需要修复)", text,
        ))
