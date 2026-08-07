"""DeepSeek Worker Agent — executes individual tasks via tool calls."""

import json
import logging
import math
from typing import Any

from orchestrator.model_router import ModelRouter
from tools.tool_broker import ToolBroker

logger = logging.getLogger(__name__)

WRITE_TOOLS = {"write_file", "apply_patch", "insert_before", "insert_after"}
EXPLORATION_TOOLS = {
    "list_files", "read_file", "search_in_file", "search_code",
    "git_status", "git_diff", "read_log",
}

WORKER_SYSTEM_PROMPT = """You are a code executor, not an investigator.

Your ONLY job is to execute the given task using the available tools.

CRITICAL RULES:
1. You are working in a USER project, NOT in the AI Engineering Studio codebase.
2. Do NOT explore or investigate any "studio" code, databases, or configs.
3. Use ONLY relative paths like "index.html", "src/main.py", etc.
4. For analysis tasks: inspect the project and return a concrete report. Do not
   create or modify files unless the task explicitly requests a report artifact.
5. For coding tasks: use write_file to create files, read_file to check existing ones.
6. For testing tasks: run the test command and report results.
7. For coding tasks, use at most 4 exploratory reads before you start editing.
8. After a successful patch, verify but do NOT re-explore the whole project.
9. NEVER use absolute paths. NEVER access ~/.ai_engineering_studio or similar.
10. For coding tasks, it is better to make changes and hit the turn limit than
    to keep reading and never edit.

Available tools:
- list_files: List files in the project directory
- read_file: Read a file's contents with start/end line pagination
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

        system_prompt = WORKER_SYSTEM_PROMPT + f"""
\n\n## Current Task: {task.task_id}
Title: {task.title}
Description: {task.description}
Type: {task.task_type}
Allowed Paths: {task.allowed_paths or 'all'}
Acceptance Command: {task.acceptance_command or 'none'}
Project Root: {project_root}
{task_memory_context}"""

        task_context = f"""
Task: {task.task_id} - {task.title}
Description: {task.description}
Type: {task.task_type}
"""

        if task.task_type == "analysis":
            task_context += "\nFocus on understanding the codebase. Read files, search for patterns, and report findings."
        elif task.task_type == "coding":
            task_context += "\nRead existing code, then implement the changes. Verify with git_diff."
        elif task.task_type == "testing":
            task_context += "\nWrite tests and verify they pass."
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
        exploration_calls = 0
        has_written = False
        seen_exploration_calls: set[tuple[str, str, bool]] = set()
        progress_warning_sent = False
        finish_warning_sent = False
        premature_completion_count = 0
        force_tool_call = False
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

                response = await self.model_router.chat_with_tools(
                    self.agent_type,
                    system_prompt,
                    messages,
                    tools=self.tool_broker.get_tool_definitions(),
                    provider_override=provider_override,
                    task=task,
                    max_tokens=8192,
                    tool_choice=(
                        "required"
                        if task.task_type == "coding" and force_tool_call and not has_written
                        else "auto"
                    ),
                )

                usage = response.get("usage", {})
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)

                content = response.get("content", "")
                tool_calls = response.get("tool_calls", [])

                if not tool_calls:
                    if task.task_type == "coding" and not has_written:
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
                                "used. The task is not complete. Use write_file, "
                                "apply_patch, insert_before, or insert_after now."
                            ),
                        })
                        # The text reminder alone is not reliable: some models
                        # repeatedly promise to edit without emitting a tool call.
                        # Force tool use at the API level until a write succeeds.
                        force_tool_call = True
                        continue
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
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    exploration_signature = (
                        func_name,
                        json.dumps(args, sort_keys=True, ensure_ascii=False, default=str),
                        has_written,
                    )
                    repeated_exploration = (
                        task.task_type == "coding"
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
                        task.task_type == "coding"
                        and func_name in EXPLORATION_TOOLS
                        and exploration_calls >= (
                            self.max_exploration_turns + (2 if has_written else 0)
                        )
                    ):
                        result = {
                            "status": "rejected",
                            "error": (
                                "Exploration budget exhausted. Use write_file, "
                                "apply_patch, insert_before, or insert_after now."
                            ),
                        }
                        exploration_blocked = True
                    else:
                        result = await self.tool_broker.execute(task, func_name, args)
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
                    tool_calls_made.append({
                        "tool": func_name,
                        "args": args,
                        "result_status": result.get("status", "error"),
                    })

                    # Format result for the model
                    result_str = json.dumps(result, ensure_ascii=False, default=str)[:3000]
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
                            "Do not read or search again. Apply the remaining code "
                            "changes now, or return the final completion response if "
                            "the task is fully implemented."
                        ),
                    })

            else:
                logger.warning(f"Worker: task {task.task_id} reached max turns ({self.max_turns})")
                return {
                    "status": "failed",
                    "error": f"Max turns ({self.max_turns}) reached",
                    "turns": len(tool_calls_made),
                    "tool_calls": tool_calls_made,
                    "input_tokens": total_input,
                    "output_tokens": total_output,
                }

            return {
                "status": "completed",
                "content": final_content,
                "turns": len(tool_calls_made),
                "tool_calls": tool_calls_made,
                "input_tokens": total_input,
                "output_tokens": total_output,
            }

        except Exception as e:
            logger.error(f"Worker: task {task.task_id} failed: {e}")
            return {
                "status": "failed",
                "error": str(e),
                "turns": len(tool_calls_made),
                "tool_calls": tool_calls_made,
            }
