"""DeepSeek Worker Agent — executes individual tasks via tool calls."""

import json
import logging
import math
import re
from typing import Any

from orchestrator.model_router import ModelRouter
from tools.tool_broker import ToolBroker

logger = logging.getLogger(__name__)

WRITE_TOOLS = {
    "write_file", "apply_patch", "insert_before", "insert_after",
    "write_docx", "write_pptx", "write_pdf", "promote_artifact",
}
TEMP_WRITE_TOOLS = {"write_temp_file"}
PAYLOAD_WRITE_TOOLS = WRITE_TOOLS | TEMP_WRITE_TOOLS
REPORT_TASK_TYPES = {"analysis", "review", "testing", "action"}
ALREADY_SATISFIED_MARKER = "[ALREADY_SATISFIED]"
STATE_VERIFICATION_TOOLS = {
    "read_file", "read_pdf", "read_docx", "read_pptx", "search_in_file",
    "search_code", "git_diff",
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
    "list_files", "read_file", "read_pdf", "read_docx", "read_pptx",
    "search_in_file", "search_code", "git_status", "git_diff", "read_log",
}
MAX_CONVERSATION_CHARS = 24_000
MAX_TOOL_CONTENT_CHARS = 12_000
CODING_OUTPUT_TOKENS = 12_288
FOLLOWUP_OUTPUT_TOKENS = 8_192
REPORT_OUTPUT_TOKENS = 6_144

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
7. For coding tasks, start editing once the relevant code is understood. The
   exploration allowance is a soft, task-sized reminder, not a read ban.
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
13. When the requested final artifact is a PDF, create it with write_pdf. Never
    implement a custom PDF/font/TTC parser or generator in project source files.
14. Keep every write_file/insert/apply_patch content payload below 12000
    characters. For a larger file, write a complete small skeleton first, then
    add self-contained sections with multiple focused insert/patch calls. Never
    put an entire large file into one tool call.
15. If a tool payload is reported as truncated, do not resend the same full
    payload. Immediately switch to smaller complete chunks and keep the file
    syntactically valid after each chunk.
16. Intermediate PDF page text, OCR, extracted chunks, notes, and drafts MUST use
    write_temp_file. Only user-requested final artifacts belong in the project.
    Use promote_artifact when a temporary file is ready to become a declared output.

Available tools:
- list_files: List files in the project directory
- read_file: Read a file's contents with start/end line pagination
- read_pdf: Extract PDF text with start_page/end_page pagination
- read_docx / read_pptx: Read Word or PowerPoint content when enabled
- write_file: Write content to a file — USE THIS, do not output code in chat
- write_temp_file / read_temp_file: Store intermediate data outside the project
- promote_artifact: Atomically publish a temporary file as a declared final output
- write_docx / write_pptx / write_pdf: Create enabled binary artifacts
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
been written to the workspace. For artifact tasks, write_docx, write_pptx, or
write_pdf also counts as a workspace write. An analysis task is complete when its final
response contains a substantive report. Keep other responses brief.
"""


class WorkerAgent:
    """DeepSeek Worker: executes tasks using tools."""

    def __init__(self, model_router: ModelRouter, tool_broker: ToolBroker,
                 max_turns: int = 75, max_exploration_turns: int = 48,
                 context_manager=None, skill_manager=None):
        self.model_router = model_router
        self.tool_broker = tool_broker
        self.agent_type = "worker"
        self.max_turns = max_turns
        self.max_exploration_turns = max_exploration_turns
        self.context_manager = context_manager
        self.skill_manager = skill_manager

    def scoped_to(self, project_root: str) -> "WorkerAgent":
        """Create an isolated worker whose tools all target one task workspace."""
        broker = ToolBroker(
            project_root, self.tool_broker.policy,
            mcp_manager=self.tool_broker.mcp_manager,
        )
        return WorkerAgent(
            self.model_router,
            broker,
            max_turns=self.max_turns,
            max_exploration_turns=self.max_exploration_turns,
            context_manager=self.context_manager,
            skill_manager=self.skill_manager,
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

        selected_skills: list[str] = []
        skill_prompt = ""
        if self.skill_manager:
            selected_skills, skill_prompt = self.skill_manager.render_for_task(task)
            task.skills = selected_skills
        system_prompt = WORKER_SYSTEM_PROMPT + skill_prompt

        task_context = f"""
Task: {task.task_id} - {task.title}
Description: {task.description}
Type: {task.task_type}
Allowed Paths: {task.allowed_paths or 'all'}
Acceptance Command: {task.acceptance_command or 'none'}
Selected Skills: {', '.join(selected_skills) or 'none'}
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
                "the output incrementally so completed page ranges are preserved. "
                "If the final artifact is PDF, call write_pdf directly and verify "
                "that PDF with read_pdf. Do not create a custom font, TTC, cmap, "
                "or PDF parser/generator script."
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
        runtime_tools = getattr(self.tool_broker, "runtime_tools", None)
        if runtime_tools is not None:
            declared_outputs = sorted(runtime_tools.final_outputs)
            task_context += (
                "\nA private task runtime is available. Put every intermediate "
                "PDF page extract, TXT chunk, OCR result, note, and draft there "
                "with write_temp_file. Do not create helper files in the project "
                "root. Final project outputs are: "
                + (", ".join(declared_outputs) if declared_outputs else "those explicitly requested by this task")
                + "."
            )

        messages = [{"role": "user", "content": task_context}]
        total_input = 0
        total_output = 0
        tool_calls_made = []
        final_content = ""
        no_changes_declared = False
        exploration_calls = 0
        has_written = False
        exploration_call_counts: dict[tuple[str, str, bool], int] = {}
        progress_warning_sent = False
        finish_warning_sent = False
        token_compaction_sent = False
        token_checkpoint_sent = False
        token_finalization_sent = False
        budget_finalization_mode = finalization_mode
        premature_completion_count = 0
        empty_report_count = 0
        force_tool_call = False
        external_action_completed = False
        external_action_signatures: set[tuple[str, str]] = set()
        verified_existing_state = False
        pending_document_pages: dict[str, int] = {}
        repeated_errors: dict[str, int] = {}
        truncated_tool_failures = 0
        exploration_warning_sent = False
        no_progress_turns = 0
        stall_warning_sent = False
        allow_no_change = self._allows_no_change(task)
        progress_warning_turn = max(1, math.ceil(self.max_turns * 0.70))
        finish_warning_turn = max(1, math.ceil(self.max_turns * 0.85))

        tool_definitions = self._tool_definitions(task)
        required_arguments = {
            str(item.get("function", {}).get("name") or ""): set(
                item.get("function", {}).get("parameters", {}).get("required") or []
            )
            for item in tool_definitions
            if isinstance(item, dict)
        }

        try:
            for turn in range(self.max_turns):
                job_id = str(
                    getattr(self.model_router, "_current_job_id", "") or "unknown"
                )
                cost_engine = getattr(self.model_router, "cost_engine", None)
                task_usage = (
                    cost_engine.get_task_usage(job_id, task.task_id)
                    if cost_engine is not None
                    else {}
                )
                task_limit = max(
                    1, int(getattr(task, "_rockcore_input_budget", 0) or 1)
                )
                effective_input = int(
                    task_usage.get("effective_input_tokens", 0) or 0
                )
                token_ratio = effective_input / task_limit
                if token_ratio >= 0.92 and not token_finalization_sent:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Token usage reached 92% of the current soft task "
                            "allocation. The router may expand this soft limit. "
                            "Keep work focused and preserve progress, but continue "
                            "the necessary reads, edits, and verification needed to "
                            "finish correctly."
                        ),
                    })
                    token_finalization_sent = True
                    if self.model_router.event_bus:
                        await self.model_router.event_bus.publish(
                            "task_budget_pressure",
                            job_id=job_id,
                            task_id=task.task_id,
                            used_tokens=effective_input,
                            task_input_budget=task_limit,
                            hard_blocked=False,
                        )
                elif token_ratio >= 0.85 and not token_checkpoint_sent:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Token usage reached 85%. Treat all files already "
                            "written as the checkpoint. Do not restart any completed "
                            "analysis; finish only the remaining concrete work."
                        ),
                    })
                    token_checkpoint_sent = True
                    if self.model_router.event_bus:
                        await self.model_router.event_bus.publish(
                            "task_budget_checkpoint",
                            job_id=job_id,
                            task_id=task.task_id,
                            used_tokens=effective_input,
                            task_input_budget=task_limit,
                            has_written=has_written,
                            document_progress=dict(pending_document_pages),
                            tool_calls=len(tool_calls_made),
                        )
                elif token_ratio >= 0.70 and not token_compaction_sent:
                    messages.append({
                        "role": "user",
                        "content": (
                            "Token usage reached 70%. Reuse the evidence already "
                            "collected, avoid broad exploration, and keep the "
                            "remaining context compact."
                        ),
                    })
                    token_compaction_sent = True
                    if self.model_router.event_bus:
                        await self.model_router.event_bus.publish(
                            "task_budget_compacting",
                            job_id=job_id,
                            task_id=task.task_id,
                            used_tokens=effective_input,
                            task_input_budget=task_limit,
                        )
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

                compact_limit = (
                    12_000 if token_ratio >= 0.92
                    else 16_000 if token_ratio >= 0.85
                    else 20_000 if token_ratio >= 0.70
                    else 32_000 if is_document_task
                    else MAX_CONVERSATION_CHARS
                )
                # Provider tool protocols require every assistant tool-call batch
                # to be followed immediately by all matching tool results. Repair
                # legacy/interrupted histories before compaction, then validate the
                # compacted payload before it leaves RockCore.
                messages = self._repair_tool_message_sequence(messages)
                messages = self._compact_messages(messages, max_chars=compact_limit)
                integrity_errors = self._tool_message_integrity_errors(messages)
                if integrity_errors:
                    logger.warning(
                        "Worker repaired invalid tool history before provider call: %s",
                        "; ".join(integrity_errors),
                    )
                    messages = self._repair_tool_message_sequence(messages)
                    remaining_errors = self._tool_message_integrity_errors(messages)
                    if remaining_errors:
                        raise RuntimeError(
                            "RockCore could not build a valid tool message sequence: "
                            + "; ".join(remaining_errors)
                        )
                model_kwargs = (
                    {"model": model_override} if model_override else {}
                )
                if task.task_type in {"coding", "action", "testing"}:
                    max_output_tokens = (
                        CODING_OUTPUT_TOKENS
                        if not (has_written or external_action_completed)
                        else FOLLOWUP_OUTPUT_TOKENS
                    )
                else:
                    max_output_tokens = REPORT_OUTPUT_TOKENS
                response = await self.model_router.chat_with_tools(
                    self.agent_type,
                    system_prompt,
                    messages,
                    tools=tool_definitions,
                    provider_override=provider_override,
                    task=task,
                    attachments=(
                        getattr(getattr(task, "job", None), "attachments", None)
                        or []
                    ),
                    max_tokens=max_output_tokens,
                    tool_choice=(
                        "required"
                        if (
                            task.task_type in {"coding", "action"}
                            and force_tool_call
                            and not (
                                has_written or external_action_completed
                            )
                        )
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
                finish_reason = str(response.get("finish_reason") or "").lower()
                for index, raw_call in enumerate(raw_tool_calls):
                    call = raw_call if isinstance(raw_call, dict) else {}
                    function = call.get("function") or {}
                    if not isinstance(function, dict) or not function.get("name"):
                        malformed_calls.append(f"tool call {index + 1} has no function name")
                        continue
                    arguments = function.get("arguments", "{}")
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False, default=str)
                    argument_error = None
                    parsed_arguments = {}
                    try:
                        parsed_arguments = json.loads(arguments)
                        if not isinstance(parsed_arguments, dict):
                            argument_error = "Tool arguments must be a JSON object"
                            parsed_arguments = {}
                    except (TypeError, json.JSONDecodeError) as error:
                        argument_error = f"Invalid tool arguments: {error}"
                    arguments_truncated = self._arguments_were_truncated(
                        str(function["name"]), arguments, argument_error,
                        finish_reason,
                    )
                    history_arguments = arguments
                    if argument_error:
                        # Keep provider history protocol-valid and compact. Feeding
                        # a giant, malformed JSON fragment back to the next turn
                        # encourages the model to repeat the same broken payload.
                        history_arguments = json.dumps({
                            "_rockcore_recovery": (
                                "tool payload was truncated"
                                if arguments_truncated
                                else "tool arguments were invalid"
                            )
                        })
                    tool_calls.append({
                        "id": str(call.get("id") or f"worker-{turn}-{index}"),
                        "function": {
                            "name": str(function["name"]),
                            "arguments": history_arguments,
                        },
                        "parsed_arguments": parsed_arguments,
                        "argument_error": argument_error,
                        "arguments_truncated": arguments_truncated,
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
                    if task.task_type == "action" and not external_action_completed:
                        premature_completion_count += 1
                        messages.append({
                            "role": "assistant",
                            "content": (content or "")[-1600:],
                        })
                        if premature_completion_count >= 2:
                            return self._failure(
                                "External action task ended without calling a "
                                "mutating MCP tool",
                                tool_calls_made, total_input, total_output,
                            )
                        messages.append({
                            "role": "user",
                            "content": (
                                "This action task has not changed the external "
                                "system. Call the configured mutating MCP tool now."
                            ),
                        })
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
                meaningful_progress = False
                batch_notices: list[str] = []
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    argument_error = tc.get("argument_error")
                    args = dict(tc.get("parsed_arguments") or {})
                    missing = sorted(
                        name for name in required_arguments.get(func_name, set())
                        if name not in args or args.get(name) is None
                    )
                    if not argument_error and missing:
                        argument_error = (
                            "Missing required tool argument(s): "
                            + ", ".join(missing)
                        )

                    exploration_signature = (
                        func_name,
                        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str),
                        has_written,
                    )
                    is_read_only_mcp = bool(getattr(
                        self.tool_broker, "is_read_only_mcp_tool",
                        lambda _name: False,
                    )(func_name))
                    is_mutating_mcp = bool(getattr(
                        self.tool_broker, "is_mutating_mcp_tool",
                        lambda _name: False,
                    )(func_name))
                    is_exploration = (
                        func_name in EXPLORATION_TOOLS or is_read_only_mcp
                    )
                    external_signature = (
                        func_name,
                        json.dumps(
                            args, sort_keys=True, ensure_ascii=False, default=str
                        ),
                    )
                    prior_exploration_calls = exploration_call_counts.get(
                        exploration_signature, 0
                    )
                    repeated_exploration = (
                        task.task_type in {"coding", "analysis", "review", "action"}
                        and is_exploration
                        # Re-reading a file can be legitimate after truncated
                        # output or a concurrent edit. Treat it as a strategy
                        # smell first and reject only after a generous allowance.
                        and prior_exploration_calls >= 6
                    )
                    artifact_manifest = dict(
                        getattr(task, "_rockcore_artifact_manifest", None) or {}
                    )
                    document_requires_pdf = bool(
                        artifact_manifest.get("kind") == "pdf"
                        and artifact_manifest.get("require_changed_output")
                    )
                    requested_path = str(args.get("path") or "").lower()
                    command = str(args.get("command") or "").lower()
                    custom_document_code = (
                        document_requires_pdf
                        and (
                            (
                                func_name in WRITE_TOOLS
                                and func_name != "write_pdf"
                                and requested_path.endswith((
                                    ".py", ".js", ".ts", ".c", ".cpp"
                                ))
                            )
                            or (
                                func_name == "run_command"
                                and any(marker in command for marker in (
                                    "pdftotext", "pip install", "pypdf",
                                    "reportlab", "fonttools", "ttc", "cmap",
                                ))
                            )
                        )
                    )
                    if custom_document_code:
                        result = {
                            "status": "rejected",
                            "error": (
                                "Dedicated PDF pipeline required. Use read_pdf and "
                                "write_pdf directly; do not build/install a custom "
                                "PDF or font parser/generator."
                            ),
                        }
                    elif (
                        budget_finalization_mode
                        and is_exploration
                    ):
                        result = {
                            "status": "rejected",
                            "error": (
                                "Token finalization mode is active. Use the "
                                "existing findings and finish without more reading."
                            ),
                        }
                        exploration_blocked = True
                    elif repeated_exploration:
                        result = {
                            "status": "rejected",
                            "error": (
                                "This exact read/search has already been completed "
                                "six times in the current phase. Reuse the existing "
                                "results or change the read range/search strategy."
                            ),
                        }
                    elif (
                        is_mutating_mcp
                        and external_signature in external_action_signatures
                    ):
                        result = {
                            "status": "rejected",
                            "error": (
                                "This external action already succeeded in this "
                                "task. Do not repeat it; return the final result."
                            ),
                        }
                    elif argument_error:
                        result = {
                            "status": "error",
                            "error": argument_error,
                            "error_code": (
                                "tool_arguments_truncated"
                                if tc.get("arguments_truncated")
                                else "invalid_tool_arguments"
                            ),
                        }
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
                        if is_exploration:
                            exploration_calls += 1
                            exploration_call_counts[exploration_signature] = (
                                prior_exploration_calls + 1
                            )
                            meaningful_progress = True
                            if prior_exploration_calls + 1 == 4:
                                batch_notices.append(
                                    "The same read/search has now been executed four "
                                    "times. This is still allowed, but reuse the "
                                    "existing result unless a changed file, different "
                                    "range, or truncated output makes another read "
                                    "necessary."
                                )
                            if (
                                exploration_calls >= self.max_exploration_turns
                                and not exploration_warning_sent
                            ):
                                batch_notices.append(
                                    "The suggested exploration allowance has been "
                                    f"reached ({exploration_calls} operations). It is "
                                    "a soft threshold, not a ban: continue any new, "
                                    "necessary paginated read, but avoid repeating "
                                    "searches and move to the concrete edit or report "
                                    "when the evidence is sufficient."
                                )
                                exploration_warning_sent = True
                        if (
                            is_mutating_mcp
                            and result.get("status") not in {"error", "rejected"}
                            and not result.get("error")
                        ):
                            external_action_completed = True
                            external_action_signatures.add(external_signature)
                            force_tool_call = False
                        if (
                            func_name in WRITE_TOOLS
                            and not result.get("redirected_to_runtime")
                            and result.get("status") not in {"error", "rejected"}
                            and not result.get("error")
                        ):
                            has_written = True
                            force_tool_call = False
                            meaningful_progress = True
                        elif (
                            (
                                func_name in TEMP_WRITE_TOOLS
                                or result.get("redirected_to_runtime")
                            )
                            and result.get("status") not in {"error", "rejected"}
                            and not result.get("error")
                        ):
                            meaningful_progress = True
                        if (
                            func_name in STATE_VERIFICATION_TOOLS
                            and result.get("status") not in {"error", "rejected"}
                            and not result.get("error")
                        ):
                            verified_existing_state = True
                            meaningful_progress = True
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

                    if result.get("error_code") == "tool_arguments_truncated":
                        truncated_tool_failures += 1
                        force_tool_call = True
                        batch_notices.append(
                            "The previous write-tool JSON was cut off before it "
                            "could run. Do not resend the whole file. Send a "
                            "complete payload under 12000 characters now: write "
                            "a small valid skeleton first, then use separate "
                            "insert_before/insert_after/apply_patch calls for "
                            "the remaining sections."
                        )
                        if truncated_tool_failures >= 3:
                            return self._failure(
                                "TOOL_PAYLOAD_TRUNCATED: the provider repeatedly "
                                "returned an oversized incomplete write payload",
                                tool_calls_made, total_input, total_output,
                            )
                    elif result.get("status") in {"error", "rejected"} or result.get("error"):
                        signature = re.sub(
                            r"\d+", "#", str(result.get("error") or result.get("status"))
                        ).lower()[:300]
                        repeated_errors[signature] = repeated_errors.get(signature, 0) + 1
                        count = repeated_errors[signature]
                        if count == 3:
                            batch_notices.append(
                                "The same tool strategy has failed three times. "
                                "This is a strategy warning, not a provider failure: "
                                "reuse existing evidence or choose a different "
                                "built-in tool before retrying."
                            )
                        elif count >= 6:
                            return self._failure(
                                "REPEATED_TOOL_FAILURE: the same tool strategy "
                                f"failed {count} times: {signature}",
                                tool_calls_made, total_input, total_output,
                            )

                # Do not insert user guidance between an assistant tool_calls
                # message and its tool replies. OpenAI-compatible providers reject
                # that sequence with HTTP 400. Deliver all guidance once the whole
                # parallel batch has been closed.
                if batch_notices:
                    messages.append({
                        "role": "user",
                        "content": "\n\n".join(dict.fromkeys(batch_notices)),
                    })

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

                if meaningful_progress:
                    no_progress_turns = 0
                else:
                    no_progress_turns += 1
                    if no_progress_turns >= 4 and not stall_warning_sent:
                        messages.append({
                            "role": "user",
                            "content": (
                                "No measurable progress was made in four turns. "
                                "Change strategy now: use the dedicated built-in "
                                "tool, make the smallest concrete edit, or finish "
                                "with the current verified artifact."
                            ),
                        })
                        stall_warning_sent = True
                    if no_progress_turns >= 8:
                        return self._failure(
                            "NO_PROGRESS: eight consecutive turns produced no "
                            "new evidence, file change, or artifact progress",
                            tool_calls_made, total_input, total_output,
                        )

            else:
                logger.warning(f"Worker: task {task.task_id} reached max turns ({self.max_turns})")
                unread_detail = ""
                if pending_document_pages:
                    unread_detail = "; document still has unread pages: " + ", ".join(
                        f"{path} start_page={page}"
                        for path, page in sorted(pending_document_pages.items())
                    )
                return {
                    "status": "needs_continuation",
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
                "skills": selected_skills,
                "external_action": external_action_completed,
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
    def _tool_message_integrity_errors(messages: list[dict]) -> list[str]:
        """Return protocol errors for assistant tool-call/result message groups."""
        errors: list[str] = []
        consumed_tool_indexes: set[int] = set()
        for index, message in enumerate(messages):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            expected = [
                str(call.get("id") or "")
                for call in message.get("tool_calls") or []
            ]
            if not all(expected) or len(expected) != len(set(expected)):
                errors.append(f"assistant message {index} has invalid tool-call ids")
                continue
            actual: list[str] = []
            cursor = index + 1
            while cursor < len(messages) and messages[cursor].get("role") == "tool":
                actual.append(str(messages[cursor].get("tool_call_id") or ""))
                consumed_tool_indexes.add(cursor)
                cursor += 1
            if actual != expected:
                errors.append(
                    f"assistant message {index} expects {expected!r}, got {actual!r}"
                )
        for index, message in enumerate(messages):
            if message.get("role") == "tool" and index not in consumed_tool_indexes:
                errors.append(f"orphan tool message {index}")
        return errors

    @staticmethod
    def _repair_tool_message_sequence(messages: list[dict]) -> list[dict]:
        """Close/reorder tool-result batches without discarding later guidance.

        Older runs may contain a user warning between parallel tool results. Move
        such warnings after the complete batch. If an interrupted history really
        lost a result, synthesize an explicit unavailable result so the provider
        can accept the history and the model can safely repeat that read if needed.
        """
        repaired: list[dict] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            tool_calls = (
                message.get("tool_calls")
                if message.get("role") == "assistant"
                else None
            )
            if not tool_calls:
                if message.get("role") != "tool":
                    repaired.append(message)
                else:
                    logger.warning(
                        "Worker dropped orphan tool result %s while repairing history",
                        message.get("tool_call_id"),
                    )
                index += 1
                continue

            repaired.append(message)
            expected_ids = [str(call.get("id") or "") for call in tool_calls]
            expected_set = set(expected_ids)
            results: dict[str, dict] = {}
            deferred: list[dict] = []
            cursor = index + 1
            while cursor < len(messages):
                candidate = messages[cursor]
                if candidate.get("role") == "assistant":
                    break
                if candidate.get("role") == "tool":
                    tool_call_id = str(candidate.get("tool_call_id") or "")
                    if tool_call_id in expected_set and tool_call_id not in results:
                        results[tool_call_id] = candidate
                    else:
                        logger.warning(
                            "Worker dropped unmatched/duplicate tool result %s",
                            tool_call_id,
                        )
                else:
                    deferred.append(candidate)
                cursor += 1

            for tool_call_id in expected_ids:
                result = results.get(tool_call_id)
                if result is None:
                    logger.warning(
                        "Worker synthesized missing tool result %s", tool_call_id,
                    )
                    result = {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": json.dumps({
                            "status": "error",
                            "error": (
                                "Historical tool result was unavailable after "
                                "conversation recovery; repeat only if still needed."
                            ),
                        }),
                    }
                repaired.append(result)
            repaired.extend(deferred)
            index = cursor
        return repaired

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
                skills=list(getattr(task, "skills", []) or []),
            )
        except TypeError:
            # Small test doubles and older third-party brokers may expose the
            # original zero-argument method.
            return self.tool_broker.get_tool_definitions()

    @staticmethod
    def _arguments_were_truncated(
        function_name: str,
        arguments: str,
        argument_error: str | None,
        finish_reason: str,
    ) -> bool:
        """Distinguish an output-limit cut-off from ordinary bad arguments."""
        if function_name not in PAYLOAD_WRITE_TOOLS or not argument_error:
            return False
        if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
            return True
        normalized = argument_error.lower()
        truncation_markers = (
            "unterminated string",
            "expecting ',' delimiter",
            "expecting property name enclosed in double quotes",
            "expecting value",
        )
        return (
            len(arguments) >= MAX_TOOL_CONTENT_CHARS // 2
            and any(marker in normalized for marker in truncation_markers)
        )

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
