"""Main orchestrator engine — the brain of the AI Engineering Studio."""

import asyncio
import copy
import fnmatch
import json
import logging
import math
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.subprocess_utils import run_process
from app.job_report import JobReportService
MAX_FLASH_RETRY = 2
MAX_REPLAN_RETRY = 1
MAX_REVIEW_REPAIR_ROUNDS = 2


# Complexity is workload size, not merely the technical domain.  Broad words
# such as "API", "payment", "page", or "fix" are deliberately absent: a typo
# on a payment page can be tiny, while a browser game can be substantial.
COMPLEX_WORKLOAD_KEYWORDS = (
    "微服务", "microservice", "分布式", "distributed", "多线程",
    "multithread", "并发架构", "concurrency architecture", "跨平台",
    "cross-platform", "全量迁移", "完整迁移", "data migration",
    "架构重构", "系统重构", "整体重构", "rewrite the system",
)
COMPLEX_SCOPE_MARKERS = (
    "完整系统", "整个系统", "全套", "全部功能", "从零搭建", "从头开发",
    "端到端", "多个模块", "所有模块", "完整游戏", "完整应用",
    "full system", "entire system", "all features", "from scratch",
    "end-to-end", "multiple modules", "complete game", "complete app",
)
OPEN_ENDED_BUILD_MARKERS = (
    "开发一个", "开发一款", "创建一个", "制作一个", "搭建一个", "实现一个",
    "build a", "build an", "create a", "develop a", "implement a",
)
MULTI_BEHAVIOR_MARKERS = (
    "游戏", "小游戏", "系统", "平台", "应用", "工作流", "编辑器", "dashboard",
    "game", "system", "platform", "application", "workflow", "editor",
)
FOCUSED_SIMPLE_MARKERS = (
    "错别字", "拼写", "文案", "按钮文字", "标题文字", "颜色", "字号", "间距",
    "链接地址", "注释", "改名", "重命名", "typo", "spelling", "copy text",
    "button label", "text color", "font size", "spacing", "rename", "comment",
)
EXPLICIT_SMALL_SCOPE_MARKERS = (
    "一个字段", "一个按钮", "一处", "单处", "一行", "单行", "仅修改",
    "只修改", "一个文件", "单个文件", "one field", "one button", "one line",
    "single line", "only change", "single file",
)
SIMPLE_STATIC_ARTIFACT_PATTERN = re.compile(
    r"(?:简单|基础|静态|simple|basic|static).{0,24}"
    r"(?:html|网页|页面|page)"
    r"|(?:html|网页|页面|page).{0,24}(?:简单|基础|静态|simple|basic|static)",
    re.IGNORECASE,
)
DOCUMENT_KEYWORDS = (
    ".pdf", "pdf", "文档", "书籍", "全书", "长文档", "电子书",
    "document", "book", "ebook",
)
LONG_DOCUMENT_KEYWORDS = (
    "整本", "全书", "书籍", "长文档", "电子书", "整份文档",
    "entire book", "full book", "long document", "ebook",
)

# Request intent is independent from risk/complexity.  In particular, a small
# request to inspect or explain a repository is not a coding task merely because
# its subject is source code.  Keep the mutation list deliberately action-led;
# nouns such as "configuration" or "changes" commonly appear in read-only
# questions and must not force a write workflow on their own.
READ_ONLY_INTENT_MARKERS = (
    "只读", "仅查看", "只查看", "看一下", "看下", "看看", "查看",
    "读取", "检查", "查找", "搜索", "定位", "分析", "解释", "说明",
    "介绍", "了解", "盘点", "梳理",
    "列出", "总结", "归纳", "提炼", "审查", "评估", "告诉我",
    "是什么", "为什么", "什么原因", "作用", "干什么", "有哪些",
    "是否", "inspect", "read", "analyze", "analyse", "explain",
    "describe", "review", "summarize", "list", "what is", "why",
)
MUTATING_INTENT_MARKERS = (
    "改一下", "修改", "修复", "优化", "实现", "创建", "新建", "增加", "添加", "删除",
    "移除", "剔除", "调整", "替换", "改成", "改为", "写入", "编辑",
    "更新", "重构", "安装", "合并", "打包", "构建", "开发", "制作",
    "搭建", "提交", "推送", "fix", "optimize", "optimise", "implement",
    "create", "add", "delete", "remove", "adjust", "replace", "edit",
    "modify", "change",
    "update", "refactor", "install", "merge", "package", "build",
    "develop", "commit", "push",
)
EXPLICIT_READ_ONLY_PATTERNS = (
    r"(?:不|无需|不要|不会)(?:创建|修改|改动|编辑|写入|删除)(?:任何)?(?:项目)?(?:文件|代码)?",
    r"(?:do not|don't|without)\s+(?:create|modify|edit|write|change|delete)",
)
REPORT_ARTIFACT_PATTERN = re.compile(
    r"(?:生成|创建|新建|导出|保存|写入|整理成|制作|generate|create|export|save|write)"
    r".{0,24}(?:\.pdf|\.docx|\.pptx?|\.md\b|pdf\s*文件|word\s*文件|"
    r"markdown\s*文件|报告文件|文档文件)",
    re.IGNORECASE,
)
from .event_bus import EventBus
from .state_machine import StateMachine, JobState
from .scheduler import Scheduler
from .policy_engine import PolicyEngine
from .model_router import ModelRouter
from .cost_engine import BudgetExceededError
from .project_resolver import ProjectResolver
from .execution_session import (
    normalize_session, record_substep, record_turn, render_fixed_context,
    update_checklist,
)
from .main_agent import MainAgent
from agents.planner import PlannerOutputTruncatedError
from .test_manager import TestManager
from .merge_manager import MergeManager
from .agent_config import ProjectAgentConfig, load_project_config
from .failure_evals import FailureEvalStore
from .hooks import HookRunner
from .skill_learning import SkillLearningService
from storage.database import create_session_factory
from storage.repositories import (
    ProjectRepository, ExecutionConversationRepository, JobRepository, ConstitutionRepository,
    PlanRepository, TaskRepository, AgentRunRepository,
    ToolCallRepository, TestRunRepository, ReviewRepository
)
from git.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class JobRuntime:
    """Mutable execution services owned by exactly one running Job."""

    job_id: str
    project_root: str
    scheduler: Scheduler
    merge_manager: MergeManager
    test_manager: TestManager
    tool_broker: Any = None
    skill_manager: Any = None
    context_manager: Any = None
    agents: dict[str, Any] = field(default_factory=dict)
    closed: bool = False


class Engine:
    """Central orchestrator that coordinates all agents and tools."""

    def __init__(self, db_path: str | None = None,
                 max_concurrent_workers: int = 3):
        from storage.database import init_database
        self._engine = init_database(db_path)
        self._session_factory = create_session_factory(self._engine)

        self.event_bus = EventBus()
        self.job_reports = JobReportService(self._session_factory)
        self.failure_evals = FailureEvalStore(self._session_factory)
        self.skill_learning = SkillLearningService(self._session_factory)
        self.hook_runner = HookRunner(self.event_bus)
        self.event_bus.subscribe("*", self.job_reports.record_event)
        self.event_bus.subscribe("model_chat", self._record_model_usage)
        self.event_bus.subscribe(
            "worker_tool_completed", self._record_worker_tool_call
        )
        self.event_bus.subscribe(
            "task_budget_checkpoint", self._record_budget_checkpoint
        )
        self.event_bus.subscribe("job_finished", self._generate_job_report)
        self.event_bus.subscribe("job_finished", self._capture_failure_eval)
        self.event_bus.subscribe("job_finished", self._observe_skill_learning)
        self.state_machine = StateMachine()
        self.main_agent = MainAgent(self)
        self._default_scheduler = Scheduler(
            max_concurrent=max(1, int(max_concurrent_workers or 1))
        )
        self.policy_engine = PolicyEngine()
        self.model_router = ModelRouter(event_bus=self.event_bus)

        self._running = False
        self._current_job_id: str | None = None
        self._agents: dict[str, Any] = {}
        self._cancelled_job_ids: set[str] = set()
        self._default_tool_broker: Any = None
        self._default_skill_manager: Any = None
        self._default_test_manager = TestManager()
        self._default_merge_manager: MergeManager | None = None
        self._runtime_context: ContextVar[JobRuntime | None] = ContextVar(
            "rockcore_job_runtime", default=None
        )
        self._job_runtimes: dict[str, JobRuntime] = {}
        self._project_job_locks: dict[str, asyncio.Lock] = {}
        # User guidance for an active requirement is consumed by the same
        # long-lived Worker.  It is deliberately kept outside model history
        # until Worker has closed the current assistant/tool-result batch.
        self._worker_instructions: dict[str, list[dict[str, str]]] = {}
        # Wire state machine to event bus.
        self.state_machine.add_listener(self._on_state_change_sync)

    async def steer_job(self, job_id: str, instruction: str) -> bool:
        """Queue guidance for the active Worker without creating a new Job.

        The queue is Job-scoped so parallel projects cannot consume each
        other's instructions.  Worker drains it only at a provider-protocol
        safe boundary after every tool-call batch has all matching replies.
        """
        job_id = str(job_id or "").strip()
        instruction = str(instruction or "").strip()
        if not job_id or not instruction:
            return False
        session = self._session_factory()
        try:
            job = JobRepository(session).get_by_id(job_id)
            if not job or job.status in {
                "done", "failed", "cancelled", "interrupted",
                "needs_attention", "rolled_back",
            }:
                return False
        finally:
            session.close()
        item = {
            "text": instruction[:8000],
            "created_at": datetime.now().astimezone().isoformat(),
        }
        self._worker_instructions.setdefault(job_id, []).append(item)
        await self.event_bus.publish(
            "worker_instruction_queued", job_id=job_id,
            instruction=item["text"], created_at=item["created_at"],
        )
        return True

    def _drain_worker_instructions(self, job_id: str) -> list[dict[str, str]]:
        """Atomically take pending guidance for exactly one running Job."""
        return self._worker_instructions.pop(str(job_id or ""), [])

    def _active_runtime(self) -> JobRuntime | None:
        return self._runtime_context.get()

    @property
    def scheduler(self) -> Scheduler:
        runtime = self._active_runtime()
        return runtime.scheduler if runtime else self._default_scheduler

    @scheduler.setter
    def scheduler(self, value: Scheduler):
        runtime = self._active_runtime()
        if runtime:
            runtime.scheduler = value
        else:
            self._default_scheduler = value

    @property
    def merge_manager(self) -> MergeManager | None:
        runtime = self._active_runtime()
        return runtime.merge_manager if runtime else self._default_merge_manager

    @merge_manager.setter
    def merge_manager(self, value: MergeManager | None):
        runtime = self._active_runtime()
        if runtime:
            runtime.merge_manager = value
        else:
            self._default_merge_manager = value

    @property
    def test_manager(self) -> TestManager:
        runtime = self._active_runtime()
        return runtime.test_manager if runtime else self._default_test_manager

    @test_manager.setter
    def test_manager(self, value: TestManager):
        runtime = self._active_runtime()
        if runtime:
            runtime.test_manager = value
        else:
            self._default_test_manager = value

    @property
    def tool_broker(self):
        runtime = self._active_runtime()
        return runtime.tool_broker if runtime else self._default_tool_broker

    @tool_broker.setter
    def tool_broker(self, value):
        runtime = self._active_runtime()
        if runtime:
            runtime.tool_broker = value
        else:
            self._default_tool_broker = value

    @property
    def skill_manager(self):
        runtime = self._active_runtime()
        return runtime.skill_manager if runtime else self._default_skill_manager

    @skill_manager.setter
    def skill_manager(self, value):
        runtime = self._active_runtime()
        if runtime:
            runtime.skill_manager = value
        else:
            self._default_skill_manager = value

    def apply_runtime_config(self, config: dict | None):
        """Apply settings that are safe to change without rebuilding agents."""
        config = config or {}
        max_workers = max(1, int(config.get("max_concurrent_workers", 3)))
        self.scheduler.max_concurrent = max_workers
        # Recreate lazily so a previously-created semaphore cannot retain the
        # old concurrency value.
        self.scheduler._semaphore = None
        self.model_router.cost_engine.set_default_budget(
            self.model_router.cost_engine.budget_from_config(
                config.get("budget", {})
            )
        )
        self.model_router.set_provider_map(
            config.get("agent_provider_map", {})
        )
        provider_models = {
            "kimi": (config.get("kimi") or {}).get("model"),
            "deepseek": (config.get("deepseek") or {}).get("model"),
        }
        for provider_name, model in provider_models.items():
            if model and self.model_router.has_provider(provider_name):
                self.model_router.get_provider(provider_name).model = model

    def _get_repos(self):
        session = self._session_factory()
        return {
            "project": ProjectRepository(session),
            "conversation": ExecutionConversationRepository(session),
            "job": JobRepository(session),
            "constitution": ConstitutionRepository(session),
            "plan": PlanRepository(session),
            "task": TaskRepository(session),
            "agent_run": AgentRunRepository(session),
            "tool_call": ToolCallRepository(session),
            "test_run": TestRunRepository(session),
            "review": ReviewRepository(session),
            "_session": session,
        }

    def _close_repos(self, repos: dict):
        repos["_session"].close()

    def register_agent(self, agent_type: str, agent: Any):
        self._agents[agent_type] = agent

    async def _record_model_usage(self, _event_type: str, **data):
        """Persist model usage for jobs and task-level cost reporting."""
        job_id = data.get("job_id")
        if not job_id:
            return
        input_tokens = max(0, int(data.get("input_tokens") or 0))
        cached_input_tokens = min(
            input_tokens,
            max(0, int(data.get("cached_input_tokens") or 0)),
        )
        output_tokens = max(0, int(data.get("output_tokens") or 0))
        estimated_cost = max(0.0, float(data.get("estimated_cost") or 0.0))
        billing_mode = str(data.get("billing_mode") or "api")
        raw_billable_cost = data.get("billable_cost")
        billable_cost = (
            0.0
            if billing_mode == "chatgpt_cli"
            else estimated_cost
            if raw_billable_cost is None
            else max(0.0, float(raw_billable_cost or 0.0))
        )
        repos = self._get_repos()
        try:
            job = repos["job"].add_usage(
                job_id, input_tokens, cached_input_tokens, output_tokens,
                estimated_cost, billable_cost,
            )
            budget_snapshot = data.get("budget")
            if job and isinstance(budget_snapshot, dict):
                checkpoint = dict(job.last_checkpoint or {})
                checkpoint["budget"] = dict(budget_snapshot)
                repos["job"].update_checkpoint(job_id, checkpoint)
            task_id = data.get("task_id")
            if not task_id or not job:
                return
            task = repos["task"].get_by_job_and_id(job.id, task_id)
            if not task:
                return
            run = repos["agent_run"].create(
                task_id=task.id,
                agent_type=data.get("agent_type", "unknown"),
                model_name=data.get("model_name") or data.get("provider", ""),
            )
            repos["agent_run"].update_status(
                run.id,
                "failed" if data.get("error") else "completed",
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                cost=estimated_cost,
                billable_cost=billable_cost,
                cost_currency="CNY",
                billing_mode=billing_mode,
                error_message=str(data.get("error") or ""),
            )
        except Exception as error:
            logger.warning("Could not persist model usage for %s: %s", job_id, error)
        finally:
            self._close_repos(repos)

    async def _record_budget_checkpoint(self, _event_type: str, **data):
        """Persist the 85% progress checkpoint without touching project files."""
        job_id = str(data.get("job_id") or "")
        if not job_id:
            return
        repos = self._get_repos()
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                return
            checkpoint = dict(job.last_checkpoint or {})
            progress = dict(checkpoint.get("budget_progress") or {})
            task_id = str(data.get("task_id") or "")
            if task_id:
                progress[task_id] = {
                    key: data.get(key)
                    for key in (
                        "used_tokens", "task_input_budget", "has_written",
                        "document_progress", "tool_calls",
                    )
                }
            checkpoint["budget_progress"] = progress
            session = normalize_session(
                checkpoint.get("execution_session"),
                session_id=(job.execution_session_id or job.job_id),
                goal=job.user_request,
            )
            session["current_step"] = task_id
            session["next_action"] = "Continue from the saved task checkpoint"
            checkpoint["execution_session"] = session
            checkpoint["budget"] = (
                self.model_router.cost_engine.get_budget_snapshot(job_id)
            )
            repos["job"].update_checkpoint(job_id, checkpoint)
        finally:
            self._close_repos(repos)

    async def _record_worker_tool_call(self, _event_type: str, **data):
        """Attach each Worker tool result to the latest persisted model turn."""
        job_id = str(data.get("job_id") or "")
        task_id = str(data.get("task_id") or "")
        if not job_id or not task_id:
            return
        repos = self._get_repos()
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                return
            checkpoint = dict(job.last_checkpoint or {})
            session = normalize_session(
                checkpoint.get("execution_session"),
                session_id=(job.execution_session_id or job.job_id),
                goal=job.user_request,
            )
            path = str(data.get("path") or "").replace("\\", "/")
            tool_name = str(data.get("tool") or "")
            result_data = dict(data.get("result") or {})
            runtime_by_task = dict(checkpoint.get("worker_runtime") or {})
            runtime = dict(runtime_by_task.get(task_id) or {})
            runtime.update({
                "task_id": task_id,
                "phase": str(data.get("phase") or ""),
                "last_tool": tool_name,
                "last_path": path,
                "last_turn": int(data.get("turn") or 0),
                "last_status": str(data.get("status") or ""),
                "updated_at": datetime.now().astimezone().isoformat(),
                "pending_action": "Continue after the last completed tool result",
            })
            if tool_name in {"read_file", "search_in_file", "search_code", "read_log"}:
                evidence_key = path or tool_name
                session.setdefault("read_evidence", {})[evidence_key] = {
                    "tool": tool_name,
                    "source_version": str(result_data.get("source_version") or ""),
                    "summary": str(
                        result_data.get("count")
                        if result_data.get("count") is not None
                        else result_data.get("total_lines") or "read"
                    )[:200],
                }
                runtime.setdefault("read_evidence", {})[evidence_key] = dict(
                    session["read_evidence"][evidence_key]
                )
            if tool_name:
                record_substep(
                    session,
                    parent_task_id=task_id,
                    key=f"tool-{data.get('turn', 0)}-{tool_name}",
                    title=(
                        f"{tool_name} · {path}" if path else tool_name
                    )[:200],
                    status=str(data.get("status") or "done"),
                    summary=str(
                        result_data.get("summary")
                        or result_data.get("message") or ""
                    )[:800],
                )
            if tool_name in {
                "write_file", "apply_patch", "insert_before", "insert_after",
                "write_docx", "write_pptx", "write_pdf", "promote_artifact",
            } and path:
                session["changed_files"] = list(dict.fromkeys(
                    list(session.get("changed_files") or []) + [path]
                ))[:100]
                runtime["completed_writes"] = list(dict.fromkeys(
                    list(runtime.get("completed_writes") or []) + [path]
                ))[:100]
            runtime["recent_results"] = (
                list(runtime.get("recent_results") or []) + [{
                    "tool": tool_name, "path": path,
                    "status": str(data.get("status") or ""),
                    "summary": str(
                        result_data.get("summary")
                        or result_data.get("message")
                        or result_data.get("error") or ""
                    )[:500],
                }]
            )[-20:]
            runtime_by_task[task_id] = runtime
            checkpoint["worker_runtime"] = runtime_by_task
            session["current_step"] = task_id
            session["next_action"] = "Continue the current task without rediscovering unchanged files"
            checkpoint["execution_session"] = session
            repos["job"].update_checkpoint(job_id, checkpoint)
            task = repos["task"].get_by_job_and_id(job.id, task_id)
            if not task:
                return
            runs = repos["agent_run"].list_by_task(task.id)
            worker_runs = [run for run in runs if run.agent_type == "worker"]
            if not worker_runs:
                return
            result = data.get("result") or {}
            summary = json.dumps(
                result, ensure_ascii=False, default=str,
            )[:1000]
            repos["tool_call"].create(
                worker_runs[-1].id,
                str(data.get("tool") or "unknown"),
                arguments=dict(data.get("arguments") or {}),
                result_summary=summary,
                status=str(data.get("status") or "success"),
                duration_ms=max(0, int(data.get("duration_ms") or 0)),
            )
        except Exception as error:
            logger.warning(
                "Could not persist Worker tool call for %s/%s: %s",
                job_id, task_id, error,
            )
        finally:
            self._close_repos(repos)

    async def _generate_job_report(self, _event_type: str, **data):
        """Build the terminal diagnostic report without blocking the UI loop."""
        job_id = str(data.get("job_id") or "")
        if not job_id:
            return
        try:
            path = await asyncio.to_thread(self.job_reports.generate, job_id)
            # Do not publish another durable event after the report was built:
            # that would make the freshly generated timeline immediately stale.
            await self.event_bus.publish_transient(
                "job_report_ready", job_id=job_id, path=str(path),
            )
        except Exception as error:
            logger.warning("Could not generate Job report for %s: %s", job_id, error)
            await self.event_bus.publish_transient(
                "job_report_failed", job_id=job_id, error=str(error),
            )

    async def generate_job_report(self, job_id: str) -> str:
        """Regenerate one report on demand, including historical Jobs."""
        path = await asyncio.to_thread(self.job_reports.generate, job_id)
        await self.event_bus.publish_transient(
            "job_report_ready", job_id=job_id, path=str(path),
        )
        return str(path)

    async def replay_job_events(self, job_id: str, *, speed: float = 0.0) -> int:
        """Replay a durable Job timeline into the UI without any model call."""
        events = await asyncio.to_thread(self.job_reports.events, job_id)
        await self.event_bus.publish_transient(
            "job_replay_started", job_id=job_id, event_count=len(events),
        )
        previous = None
        count = 0
        for item in events:
            event_type = str(item.get("event") or "")
            data = dict(item.get("data") or {})
            if not event_type:
                continue
            if speed > 0 and previous:
                try:
                    current = datetime.fromisoformat(
                        str(item.get("timestamp") or "").replace("Z", "+00:00")
                    )
                    delay = max(0.0, min(1.5, (current - previous).total_seconds() / speed))
                    if delay:
                        await asyncio.sleep(delay)
                    previous = current
                except (TypeError, ValueError):
                    pass
            elif speed > 0:
                try:
                    previous = datetime.fromisoformat(
                        str(item.get("timestamp") or "").replace("Z", "+00:00")
                    )
                except (TypeError, ValueError):
                    previous = None
            await self.event_bus.publish_transient(
                "job_replay_event", job_id=job_id,
                original_event=event_type, original_data=data,
                timestamp=item.get("timestamp", ""),
            )
            count += 1
        await self.event_bus.publish_transient(
            "job_replay_finished", job_id=job_id, event_count=count,
        )
        return count

    async def _run_project_hooks(self, config: ProjectAgentConfig, event: str,
                                 *, job_id: str, project_root: str,
                                 task_id: str = "") -> list[dict]:
        hooks = getattr(config, "hooks", None)
        commands = list(getattr(hooks, event, []) or []) if hooks else []
        if not hooks or not hooks.enabled or not commands:
            return []
        return await self.hook_runner.run(
            event, job_id=job_id, project_root=project_root,
            task_id=task_id, commands=commands,
        )

    async def _capture_failure_eval(self, _event_type: str, **data):
        job_id = str(data.get("job_id") or "")
        if not job_id:
            return
        case = await asyncio.to_thread(self.failure_evals.capture, job_id)
        if case:
            await self.event_bus.publish_transient(
                "failure_eval_captured", job_id=job_id, case=case,
            )

    async def _observe_skill_learning(self, _event_type: str, **data):
        job_id = str(data.get("job_id") or "")
        if not job_id:
            return
        suggestion = await asyncio.to_thread(self.skill_learning.observe, job_id)
        if suggestion:
            await self.event_bus.publish_transient(
                "skill_suggestion", job_id=job_id, **suggestion,
            )

    def get_agent(self, agent_type: str):
        runtime = self._active_runtime()
        if runtime:
            return runtime.agents.get(agent_type)
        return self._agents.get(agent_type)

    async def _create_job_runtime(self, job_id: str,
                                  project_root: str) -> JobRuntime:
        """Build isolated mutable services for one concurrently running Job."""
        root = str(Path(project_root).resolve())
        context_manager = None
        base_context = next((
            getattr(agent, "context_manager", None)
            for agent in self._agents.values()
            if getattr(agent, "context_manager", None) is not None
        ), None)
        if base_context is not None:
            try:
                from memory.context_manager import ContextManager
                if isinstance(base_context, ContextManager):
                    context_manager = ContextManager(root)
                    await context_manager.initialize()
                else:
                    context_manager = copy.copy(base_context)
            except Exception as error:
                logger.warning(
                    "Could not isolate context manager for %s: %s",
                    job_id, error,
                )

        skill_manager = None
        base_skills = self._default_skill_manager
        if base_skills is not None:
            try:
                from skills.manager import SkillManager
                if isinstance(base_skills, SkillManager):
                    skill_manager = SkillManager(
                        root,
                        config=copy.deepcopy(base_skills.config),
                        builtin_root=base_skills.builtin_root,
                        plugin_config=copy.deepcopy(base_skills.plugin_config),
                    )
                else:
                    skill_manager = copy.copy(base_skills)
            except Exception as error:
                logger.warning(
                    "Could not isolate skill manager for %s: %s", job_id, error
                )

        tool_broker = None
        base_broker = self._default_tool_broker
        if base_broker is not None:
            try:
                from tools.tool_broker import ToolBroker
                if isinstance(base_broker, ToolBroker):
                    tool_broker = ToolBroker(root, base_broker.policy)
                else:
                    tool_broker = copy.copy(base_broker)
                    if hasattr(tool_broker, "set_project_root"):
                        tool_broker.set_project_root(root)
            except Exception as error:
                logger.warning(
                    "Could not isolate tool broker for %s: %s", job_id, error
                )

        runtime = JobRuntime(
            job_id=job_id,
            project_root=root,
            scheduler=Scheduler(self._default_scheduler.max_concurrent),
            merge_manager=MergeManager(root),
            test_manager=TestManager(),
            tool_broker=tool_broker,
            skill_manager=skill_manager,
            context_manager=context_manager,
        )
        for agent_type, template in self._agents.items():
            agent = copy.copy(template)
            if hasattr(agent, "model_router"):
                agent.model_router = self.model_router
            if hasattr(agent, "context_manager"):
                agent.context_manager = context_manager
            if hasattr(agent, "skill_manager"):
                agent.skill_manager = skill_manager
            if hasattr(agent, "tool_broker") and tool_broker is not None:
                agent.tool_broker = tool_broker
            runtime.agents[agent_type] = agent
        return runtime

    async def _close_job_runtime(self, runtime: JobRuntime):
        if runtime.closed:
            return
        runtime.closed = True
        broker = runtime.tool_broker
        if broker and hasattr(broker, "close"):
            try:
                await broker.close()
            except Exception as error:
                logger.warning(
                    "Could not close runtime tools for %s: %s",
                    runtime.job_id, error,
                )

    async def _on_state_change(self, job_id: str, old_state: JobState, new_state: JobState):
        await self.event_bus.publish(
            "job_state_changed",
            job_id=job_id,
            old_state=old_state.name.lower(),
            new_state=new_state.name.lower()
        )

    def _on_state_change_sync(self, job_id: str, old_state: JobState, new_state: JobState):
        """Synchronous wrapper for state machine listener."""
        asyncio.ensure_future(self._on_state_change(job_id, old_state, new_state))

    async def start(self):
        self._running = True
        repos = self._get_repos()
        try:
            terminal = {
                "done", "failed", "cancelled", "interrupted",
                "needs_attention", "rolled_back",
            }
            for job in repos["job"].list_all():
                if job.status in terminal:
                    continue
                reason = (
                    "RockCore 上次退出时该任务仍在运行，已保留已有结果，"
                    "可通过“继续此需求”从检查点恢复。"
                )
                repos["job"].update_status(job.job_id, "interrupted")
                repos["job"].set_failure(
                    job.job_id, "process_interrupted", reason,
                    "继续此需求时会携带已完成步骤、失败原因和文件范围。",
                )
                await self.event_bus.publish(
                    "job_interrupted", job_id=job.job_id, error=reason
                )
        finally:
            self._close_repos(repos)
        # Keep this short metadata scan on the engine thread. SQLite in-memory
        # databases are connection-local, so moving it to a worker thread can
        # open a fresh connection with no schema during tests and embeddings.
        historical_evals = self.failure_evals.sync_historical()
        if historical_evals:
            await self.event_bus.publish_transient(
                "failure_evals_synced", count=len(historical_evals),
            )
        logger.info("Engine started")

    async def stop(self):
        self._running = False
        self._default_scheduler.stop()
        for runtime in list(self._job_runtimes.values()):
            runtime.scheduler.stop()
        for runtime in list(self._job_runtimes.values()):
            await self._close_job_runtime(runtime)
        if (
            self._default_tool_broker
            and hasattr(self._default_tool_broker, "close")
        ):
            await self._default_tool_broker.close()
        logger.info("Engine stopped")

    async def _skip_phase(self, job, repos, phase: str,
                          reason: str = "已按项目配置禁用"):
        """Skip a disabled agent phase with proper state transitions."""
        state_map = {
            "governor": (JobState.GOVERNING, JobState.GOVERNED),
            "planner": (JobState.PLANNING, JobState.PLAN_CHECK, JobState.READY),
        }
        states = state_map.get(phase, ())
        for s in states:
            self.state_machine.transition(job.job_id, s)
        await self.event_bus.publish("phase_summary",
            phase=phase, agent_type=phase, status="skipped",
            summary=f"{phase} {reason}")

    async def _skip_review(self, job, repos,
                           reason: str = "审核已按项目配置跳过"):
        """Skip reviewer and go straight to DONE."""
        self.state_machine.transition(job.job_id, JobState.REVIEWING)
        self.state_machine.transition(job.job_id, JobState.DONE)
        repos["job"].update_status(job.job_id, "done")
        repos["job"].clear_failure(job.job_id)
        await self.event_bus.publish("phase_summary",
            phase="reviewer", agent_type="reviewer", status="skipped",
            summary=reason)
        await self.event_bus.publish("job_done", job_id=job.job_id)

    def _classify_request(self, user_request: str) -> str:
        """Conservatively classify request workload before model routing.

        This rule layer should only claim ``simple`` when scope is explicit.
        Ambiguous creation requests default to ``normal`` so MainAgent can make
        the semantic decision instead of silently bypassing planning.
        """
        request = " ".join(str(user_request or "").split())
        req_lower = request.lower()
        has_complex_workload = any(
            marker.lower() in req_lower for marker in COMPLEX_WORKLOAD_KEYWORDS
        )
        has_complex_scope = any(
            marker.lower() in req_lower for marker in COMPLEX_SCOPE_MARKERS
        )

        # Document work is dominated by source size rather than wording length.
        # Check it before generic phrases such as "创建一个", otherwise a whole
        # book can accidentally receive the small-task budget.
        if self._is_document_request(request):
            if any(marker in req_lower for marker in LONG_DOCUMENT_KEYWORDS):
                return "complex"
            return "complex" if has_complex_workload or has_complex_scope else "normal"
        if has_complex_workload or has_complex_scope:
            return "complex"

        open_ended_build = any(
            marker.lower() in req_lower for marker in OPEN_ENDED_BUILD_MARKERS
        )
        multi_behavior_subject = any(
            marker.lower() in req_lower for marker in MULTI_BEHAVIOR_MARKERS
        )
        focused_change = any(
            marker.lower() in req_lower for marker in FOCUSED_SIMPLE_MARKERS
        )
        explicit_small_scope = any(
            marker.lower() in req_lower for marker in EXPLICIT_SMALL_SCOPE_MARKERS
        )
        simple_static_artifact = bool(
            SIMPLE_STATIC_ARTIFACT_PATTERN.search(request)
        )

        # Short wording is not evidence of a small implementation.  A request
        # such as "build a browser tank game" is open-ended and must receive a
        # semantic MainAgent pass even though it contains "page" and is short.
        if len(request) <= 240 and (
            focused_change or explicit_small_scope or simple_static_artifact
        ) and not (open_ended_build and multi_behavior_subject):
            return "simple"
        return "normal"

    @classmethod
    def _request_task_type(cls, user_request: str) -> str:
        """Return the required deliverable type for a direct user request.

        This intentionally answers a different question from complexity and
        risk classification: whether success is a written report or a changed
        workspace.  Ambiguous requests remain coding tasks so the no-edit guard
        is never weakened for a real implementation request.
        """
        text = " ".join(str(user_request or "").lower().split())
        if not text:
            return "coding"
        if REPORT_ARTIFACT_PATTERN.search(text):
            return "coding"

        explicit_read_only = any(
            re.search(pattern, text, re.IGNORECASE)
            for pattern in EXPLICIT_READ_ONLY_PATTERNS
        )
        positive_text = text
        for pattern in EXPLICIT_READ_ONLY_PATTERNS:
            positive_text = re.sub(
                pattern, " ", positive_text, flags=re.IGNORECASE
            )

        def marker_pattern(markers: tuple[str, ...]) -> str:
            patterns = []
            for marker in sorted(markers, key=len, reverse=True):
                escaped = re.escape(marker)
                patterns.append(
                    rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
                    if marker.isascii() else escaped
                )
            return "|".join(patterns)

        read_pattern = marker_pattern(READ_ONLY_INTENT_MARKERS)
        mutation_pattern = marker_pattern(MUTATING_INTENT_MARKERS)
        has_read_intent = explicit_read_only or bool(re.search(
            read_pattern, text, re.IGNORECASE
        ))
        if not has_read_intent:
            return "coding"

        has_mutation_term = bool(re.search(mutation_pattern, positive_text))
        if not has_mutation_term:
            return "analysis"
        if explicit_read_only:
            return "analysis"

        question_intent = re.search(
            r"为什么|什么原因|是什么|怎么回事|怎么|如何|是否|"
            r"how\s+does|how\s+to|what\s+is|why\b",
            positive_text,
            re.IGNORECASE,
        )
        if question_intent:
            post_question = positive_text[question_intent.end():]
            explicit_action_after_question = re.search(
                rf"(?:并且|并|然后|随后|同时|再|接着|之后|，|,|；|;)"
                rf".{{0,12}}(?:帮我|请|直接|需要|务必|必须)?"
                rf".{{0,8}}(?:{mutation_pattern})",
                post_question,
                re.IGNORECASE,
            )
            if not explicit_action_after_question:
                return "analysis"

        # A read followed by an explicit edit step is a mixed implementation
        # request.  A mutation word used as the subject of a question (for
        # example, "查看这次修改为什么失败") remains read-only.
        edit_after_read = re.search(
            rf"(?:并且|并|然后|随后|同时|再|接着|之后|后再|，|,|；|;|"
            rf"and then|then|and)"
            rf".{{0,16}}(?:{mutation_pattern})",
            positive_text,
            re.IGNORECASE,
        )
        conditional_edit = re.search(
            rf"(?:发现|如果|若|如有|存在|有).{{0,20}}(?:问题|错误|缺陷|不一致)?"
            rf".{{0,12}}(?:就|则|请|需要|要)?\s*(?:{mutation_pattern})",
            positive_text,
            re.IGNORECASE,
        )
        if edit_after_read or conditional_edit:
            return "coding"

        imperative_edit = re.search(
            rf"(?:帮我|请|直接|需要|务必|必须|please)"
            rf".{{0,12}}(?:{mutation_pattern})",
            positive_text,
            re.IGNORECASE,
        )
        if imperative_edit:
            return "coding"

        first_read_match = re.search(read_pattern, positive_text, re.IGNORECASE)
        first_read = first_read_match.start() if first_read_match else -1
        first_mutation = min(
            (match.start() for match in re.finditer(
                mutation_pattern, positive_text, re.IGNORECASE
            )),
            default=-1,
        )
        if first_mutation >= 0 and (first_read < 0 or first_mutation < first_read):
            return "coding"
        return "analysis"

    @classmethod
    def _normalize_plan_task_types(
        cls, plan_data: dict, user_request: str,
    ) -> bool:
        """Correct coding defaults for an unambiguously read-only request."""
        if cls._request_task_type(user_request) != "analysis":
            return False
        changed = False
        for task in plan_data.get("tasks", []):
            task_type = str(task.get("type") or "coding").lower()
            if task_type != "coding":
                continue
            task_text = " ".join((
                str(task.get("title") or ""),
                str(task.get("description") or ""),
            ))
            if cls._request_task_type(task_text) != "analysis":
                continue
            task["type"] = "analysis"
            task["acceptance_command"] = ""
            changed = True
        return changed

    @staticmethod
    def _is_document_request(text: str) -> bool:
        normalized = str(text or "").lower()
        return any(marker in normalized for marker in DOCUMENT_KEYWORDS)

    @staticmethod
    def _pdf_page_count(path: Path) -> int:
        """Read only PDF metadata needed for deterministic budget sizing."""
        try:
            from pypdf import PdfReader

            return max(0, len(PdfReader(str(path)).pages))
        except Exception as error:
            logger.debug("Could not count PDF pages for %s: %s", path, error)
            return 0

    @classmethod
    def _document_task_profile(cls, task, project_root: str,
                               user_request: str = "") -> dict | None:
        """Return a size-aware Worker budget for PDF/document tasks."""
        allowed_paths = list(getattr(task, "allowed_paths", None) or [])
        task_text = " ".join([
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(path) for path in allowed_paths),
        ]).lower()
        if not cls._is_document_request(task_text):
            return None
        text = f"{user_request} {task_text}".lower()

        root = Path(project_root).resolve()
        ignored = {".git", ".ai", ".venv", "venv", "node_modules"}
        pdf_files: set[Path] = set()

        # Prefer Planner-provided paths. If they are broad or omit the source,
        # inspect only a bounded number of project PDFs for workload sizing.
        for pattern in allowed_paths:
            normalized = str(pattern or "").replace("\\", "/").lstrip("./")
            if not normalized:
                continue
            try:
                for candidate in root.glob(normalized):
                    if len(pdf_files) >= 50:
                        break
                    if (
                        candidate.is_file()
                        and candidate.suffix.lower() == ".pdf"
                        and not ignored.intersection(candidate.relative_to(root).parts)
                    ):
                        pdf_files.add(candidate)
            except (OSError, ValueError):
                continue
        if not pdf_files:
            try:
                for candidate in root.rglob("*.pdf"):
                    if len(pdf_files) >= 50:
                        break
                    if (
                        candidate.is_file()
                        and not ignored.intersection(candidate.relative_to(root).parts)
                    ):
                        pdf_files.add(candidate)
            except OSError:
                pass

        output_name_markers = {
            "summary", "output", "result", "final", "condensed",
            "摘要", "总结", "精简", "整理版", "最终版", "输出",
        }
        output_pdfs = {
            path for path in pdf_files
            if len(pdf_files) > 1 and any(
                marker in path.stem.lower() for marker in output_name_markers
            )
        }
        source_pdfs = pdf_files - output_pdfs
        if not source_pdfs:
            source_pdfs = set(pdf_files)
            output_pdfs = set()
        for pattern in allowed_paths:
            normalized = str(pattern or "").replace("\\", "/").lstrip("./")
            if not normalized or "*" in normalized or not normalized.lower().endswith(".pdf"):
                continue
            candidate = (root / normalized).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate not in source_pdfs and (
                not candidate.exists()
                or any(marker in candidate.stem.lower() for marker in output_name_markers)
            ):
                output_pdfs.add(candidate)

        total_bytes = 0
        total_pages = 0
        for path in source_pdfs:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
            total_pages += cls._pdf_page_count(path)

        is_long = any(marker in text for marker in LONG_DOCUMENT_KEYWORDS)
        if total_pages:
            estimated_pages = total_pages
        elif is_long:
            # File size is a weak proxy for text density. A whole-book request
            # must still receive a book-sized budget when metadata is damaged.
            estimated_pages = max(160, math.ceil(total_bytes / 24_000))
        elif total_bytes >= 2 * 1024 * 1024:
            estimated_pages = max(80, math.ceil(total_bytes / 32_000))
        else:
            estimated_pages = max(24, math.ceil(total_bytes / 40_000))

        page_batches = max(1, math.ceil(estimated_pages / 8))
        if estimated_pages > 120 or is_long:
            level = "long"
        elif estimated_pages > 40:
            level = "medium"
        else:
            level = "short"

        # Real runs repeatedly include conversation/tool context. Twelve
        # thousand input tokens per source page plus a 300k workflow reserve is
        # intentionally conservative; the RMB ceiling remains the hard spend
        # protection. The dedicated document tools keep each eight-page batch
        # bounded while still leaving room for incremental writing and checks.
        processing_input_budget = min(
            19_000_000,
            max(400_000, estimated_pages * 12_000 + 300_000),
        )
        finalization_reserve = min(
            1_000_000,
            max(200_000, math.ceil(processing_input_budget * 0.12)),
        )
        input_budget = min(
            20_000_000, processing_input_budget + finalization_reserve
        )
        # A dedicated read_pdf/write_pdf pipeline needs roughly two tool turns
        # per page batch plus verification. Cap one attempt so a broken custom
        # strategy cannot consume hundreds of calls; Token ceilings remain
        # generous and unfinished page ranges are checkpointed for continuation.
        turns = min(192, max(72, 36 + page_batches * 9))
        exploration = min(
            max(36, page_batches * 3 + 36),
            max(36, turns - 36),
        )
        finalization_turns = 36
        api_call_budget = turns + finalization_turns + 72
        output_budget = max(200_000, estimated_pages * 1_500)
        concrete_non_pdf_outputs = [
            str(path).replace("\\", "/").lstrip("./") for path in allowed_paths
            if path and "*" not in str(path)
            and Path(str(path)).suffix.lower() in {
                ".md", ".txt", ".docx", ".pptx", ".html"
            }
        ]
        requires_pdf_output = bool(output_pdfs) or (
            not concrete_non_pdf_outputs
            and "pdf" in text
            and any(marker in text for marker in (
                "生成", "创建", "输出", "整理成", "精简成",
                "generate", "create", "output", "export",
            ))
        )
        return {
            "level": level,
            "input_budget": input_budget,
            "processing_input_budget": processing_input_budget,
            "finalization_reserve": finalization_reserve,
            "finalization_turns": finalization_turns,
            "max_turns": turns,
            "exploration_turns": exploration,
            "api_call_budget": api_call_budget,
            "output_budget": output_budget,
            "pdf_count": len(source_pdfs),
            "pdf_bytes": total_bytes,
            "pdf_pages": total_pages,
            "source_pdfs": [
                path.relative_to(root).as_posix()
                for path in sorted(source_pdfs)
            ],
            "output_pdfs": [
                path.relative_to(root).as_posix()
                for path in sorted(output_pdfs)
            ],
            "final_outputs": sorted(set(concrete_non_pdf_outputs) | {
                path.relative_to(root).as_posix()
                for path in output_pdfs
            }),
            "requires_pdf_output": requires_pdf_output,
            "estimated_pages": estimated_pages,
            "page_batches": page_batches,
            "reason": (
                f"document={level}, source_pdfs={len(source_pdfs)}, "
                f"output_pdfs={len(output_pdfs)}, "
                f"pages={total_pages or f'~{estimated_pages}'}, "
                f"batches={page_batches}, size={total_bytes} bytes"
            ),
        }

    # ── Job Lifecycle ──────────────────────────────────────────

    async def create_job(self, project_id: int, user_request: str,
                         project_root: str, risk_level: str = "medium",
                         source_job_id: str | None = None,
                         attachments: list[dict] | None = None) -> dict:
        from app.image_attachments import normalize_attachments

        repos = self._get_repos()
        try:
            source_job = None
            if source_job_id:
                source_job = repos["job"].get_by_id(source_job_id)
                if not source_job or source_job.project_id != project_id:
                    raise ValueError("The source job does not belong to this project")

            # Job IDs are user-facing, so their calendar date follows the
            # machine's local timezone. Persisted timestamps remain UTC.
            today = datetime.now().astimezone().strftime("%Y%m%d")
            count = len(repos["job"].list_all()) + 1
            job_id_str = f"JOB-{today}-{count:03d}"
            while repos["job"].get_by_id(job_id_str):
                count += 1
                job_id_str = f"JOB-{today}-{count:03d}"

            safe_attachments = normalize_attachments(attachments)
            for attachment in safe_attachments:
                attachment["origin_job_id"] = job_id_str
            if source_job:
                safe_attachments = self._inherit_continuation_attachments(
                    safe_attachments,
                    source_job.job_id,
                    project_id,
                    repos,
                )
            job = repos["job"].create(
                job_id_str, project_id, user_request, risk_level, source_job_id,
                safe_attachments,
                execution_session_id=(
                    (source_job.execution_session_id or source_job.job_id)
                    if source_job
                    else job_id_str
                ),
            )
            inherited_session = (
                dict((source_job.last_checkpoint or {}).get("execution_session") or {})
                if source_job else {}
            )
            session = normalize_session(
                inherited_session,
                session_id=job.execution_session_id,
                goal=(inherited_session.get("goal") or user_request),
            )
            record_turn(
                session, job_id=job_id_str, request=user_request,
                status="created",
            )
            if source_job:
                session["decisions"] = list(session.get("decisions") or []) + [{
                    "kind": "follow_up",
                    "job_id": job_id_str,
                    "request": user_request[:1200],
                }]
                session["current_step"] = "planning"
                session["next_action"] = "Plan only the requested continuation"
            repos["job"].update_checkpoint(job_id_str, {
                "execution_session": session,
            })
            self._cancelled_job_ids.discard(job_id_str)
            self.state_machine.transition(job_id_str, JobState.CREATED)

            # Create git branch
            branch = f"ai/{job_id_str.lower()}"
            await self.event_bus.publish("job_created", job_id=job_id_str,
                                          branch=branch, project_root=project_root)

            return {"job_id": job_id_str, "branch": branch, "pk": job.id}
        finally:
            self._close_repos(repos)

    @staticmethod
    def _continuation_ancestors(source_job_id: str | None, project_id: int,
                                repos, limit: int = 32) -> list:
        """Return the explicit continuation chain, nearest source first."""
        ancestors = []
        seen = set()
        current_id = str(source_job_id or "").strip()
        while current_id and current_id not in seen and len(ancestors) < limit:
            seen.add(current_id)
            current = repos["job"].get_by_id(current_id)
            if not current or current.project_id != project_id:
                break
            ancestors.append(current)
            current_id = str(current.source_job_id or "").strip()
        return ancestors

    def _inherit_continuation_attachments(self, current: list[dict],
                                          source_job_id: str, project_id: int,
                                          repos) -> list[dict]:
        """Merge valid source images into a follow-up without duplicating them."""
        from app.image_attachments import (
            MAX_IMAGE_ATTACHMENTS,
            normalize_attachments,
        )

        merged = [dict(item) for item in current[:MAX_IMAGE_ATTACHMENTS]]
        seen = {
            str(item.get("sha256") or item.get("path") or item.get("id"))
            for item in merged
        }
        for ancestor in self._continuation_ancestors(
            source_job_id, project_id, repos
        ):
            for raw in list(ancestor.attachments or []):
                if len(merged) >= MAX_IMAGE_ATTACHMENTS:
                    return merged
                try:
                    inherited = normalize_attachments([raw])[0]
                except (OSError, ValueError) as error:
                    logger.warning(
                        "Skipping unavailable inherited image from %s: %s",
                        ancestor.job_id,
                        error,
                    )
                    continue
                identity = str(
                    inherited.get("sha256")
                    or inherited.get("path")
                    or inherited.get("id")
                )
                if identity in seen:
                    continue
                seen.add(identity)
                inherited["origin_job_id"] = str(
                    raw.get("origin_job_id") or ancestor.job_id
                )
                inherited["inherited_from_job_id"] = source_job_id
                merged.append(inherited)
        return merged

    async def run_job(self, job_id: str, project_root: str):
        """Run one Job with isolated services; serialize only the same project."""
        project_key = str(Path(project_root).resolve())
        lock = self._project_job_locks.setdefault(project_key, asyncio.Lock())
        async with lock:
            runtime = await self._create_job_runtime(job_id, project_key)
            token = self._runtime_context.set(runtime)
            event_token = (
                self.event_bus.bind_job(job_id)
                if hasattr(self.event_bus, "bind_job") else None
            )
            self._job_runtimes[job_id] = runtime
            try:
                return await self._run_job_pipeline(job_id, project_key)
            finally:
                self._job_runtimes.pop(job_id, None)
                if event_token is not None:
                    self.event_bus.reset_job(event_token)
                self._runtime_context.reset(token)
                await self._close_job_runtime(runtime)

    async def resume_attention_job(self, job_id: str, project_root: str):
        """Resume one persisted interrupted/needs-attention Job checkpoint."""
        project_key = str(Path(project_root).resolve())
        lock = self._project_job_locks.setdefault(project_key, asyncio.Lock())
        async with lock:
            runtime = await self._create_job_runtime(job_id, project_key)
            token = self._runtime_context.set(runtime)
            event_token = (
                self.event_bus.bind_job(job_id)
                if hasattr(self.event_bus, "bind_job") else None
            )
            self._job_runtimes[job_id] = runtime
            try:
                return await self._resume_attention_pipeline(
                    job_id, project_key
                )
            finally:
                self._job_runtimes.pop(job_id, None)
                if event_token is not None:
                    self.event_bus.reset_job(event_token)
                self._runtime_context.reset(token)
                await self._close_job_runtime(runtime)

    async def rollback_job(self, job_id: str, project_root: str) -> dict:
        """Reverse one terminal Job through Git without restarting its workflow."""
        project_key = str(Path(project_root).resolve())
        lock = self._project_job_locks.setdefault(project_key, asyncio.Lock())
        async with lock:
            if any(
                runtime.project_root == project_key
                for runtime in self._job_runtimes.values()
            ):
                return {
                    "status": "failed",
                    "error": "这个项目还有任务在运行，请等待完成后再回退。",
                }
            repos = self._get_repos()
            try:
                job = repos["job"].get_by_id(job_id)
                if not job:
                    return {"status": "failed", "error": f"Job not found: {job_id}"}
                if job.status == "rolled_back":
                    return {"status": "failed", "error": "这次需求已经回退。"}
                await self.event_bus.publish("job_rollback_started", job_id=job_id)
                result = await asyncio.to_thread(
                    Repository(project_key).rollback_job, job_id
                )
                if result.get("status") != "rolled_back":
                    await self.event_bus.publish(
                        "job_rollback_failed", job_id=job_id,
                        error=result.get("error", "Rollback failed"),
                    )
                    return result
                repos["job"].update_status(job_id, "rolled_back")
                repos["job"].clear_failure(job_id)
                repos["job"].update_checkpoint(job_id, {
                    **dict(job.last_checkpoint or {}),
                    "rolled_back_at": datetime.now().astimezone().isoformat(),
                    "rollback_commit": result.get("rollback_commit", ""),
                })
                await self.event_bus.publish(
                    "job_rolled_back", job_id=job_id,
                    rollback_commit=result.get("rollback_commit", ""),
                    reverted_commits=result.get("commits", []),
                )
                return result
            finally:
                self._close_repos(repos)

    def _configure_job_runtime(self, job, project_root: str,
                               proj_config: ProjectAgentConfig) -> None:
        """Restore provider and project context needed by a checkpoint run."""
        self.model_router.set_job_id(job.job_id)
        if self.tool_broker:
            self.tool_broker.set_project_root(project_root)
        profiles = {
            "main_agent": proj_config.governor,
            "main_agent_summary": proj_config.governor,
            "governor": proj_config.governor,
            "planner": proj_config.planner,
            "worker": proj_config.worker,
            "reviewer": proj_config.reviewer,
            "emergency_coder": proj_config.emergency_coder,
        }
        self.model_router.set_job_routing(
            job.job_id,
            provider_map={
                role: profile.provider for role, profile in profiles.items()
                if profile.provider
            },
            model_map={
                role: profile.model for role, profile in profiles.items()
                if profile.model
            },
            reasoning_map={
                role: profile.reasoning_effort
                for role, profile in profiles.items()
                if profile.reasoning_effort
            },
        )

    @staticmethod
    def _resumable_task_ids(tasks: list) -> set[str]:
        """Return the unfinished checkpoint and its downstream dependency chain."""
        candidates = {
            task.task_id for task in tasks
            if task.status in {
                "needs_attention", "interrupted", "blocked", "pending",
                "running",
            }
        }
        roots = {
            task.task_id for task in tasks
            if task.status in {"needs_attention", "interrupted", "running"}
        }
        if not roots:
            roots = set(candidates)
        selected = set(roots)
        changed = True
        while changed:
            changed = False
            for task in tasks:
                if task.task_id not in candidates or task.task_id in selected:
                    continue
                if selected.intersection(task.dependencies or []):
                    selected.add(task.task_id)
                    changed = True
        return selected

    @staticmethod
    def _task_progress_layout(all_job_tasks: list, active_tasks: list,
                              repair_round: int = 0) -> tuple[dict[str, int], int]:
        """Map active tasks to stable positions in the original plan."""
        progress_tasks = active_tasks
        if not repair_round:
            progress_tasks = [
                task for task in all_job_tasks
                if not re.match(r"^R\d+T", str(task.task_id or ""))
            ]
        return (
            {
                task.task_id: index
                for index, task in enumerate(progress_tasks, 1)
            },
            len(progress_tasks),
        )

    async def _resume_attention_pipeline(self, job_id: str,
                                         project_root: str):
        return await self.main_agent.resume_turn(job_id, project_root)

    async def _resume_attention_pipeline_core(self, job_id: str,
                                              project_root: str):
        """Continue the same Job from its persisted phase and task worktrees."""
        repos = self._get_repos()
        job = None
        worker = None
        saved_turns = None
        finalized = False
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                raise ValueError(f"Job not found: {job_id}")
            if job.status not in {"needs_attention", "interrupted"}:
                raise ValueError(
                    f"Job {job_id} has no resumable checkpoint: {job.status}"
                )
            self.main_agent.prepare_turn(job, repos, resumed=True)
            repos["_session"].refresh(job)

            self.state_machine.restore(job_id, JobState.WAITING_USER)
            self._cancelled_job_ids.discard(job_id)
            proj_root = job.project.root_path if job.project else project_root
            proj_config = load_project_config(proj_root)
            self._configure_job_runtime(job, proj_root, proj_config)
            self.model_router.cost_engine.refresh_job_limits(job_id)
            self.model_router.cost_engine.restore_persisted_usage(
                job_id,
                input_tokens=job.usage_input_tokens,
                cached_input_tokens=job.usage_cached_input_tokens,
                output_tokens=job.usage_output_tokens,
                calls=job.usage_calls,
                billable_cost=job.usage_billable_cost,
            )
            if proj_config.governor.enabled and proj_config.mode != "fast":
                recovery_assessment = await self.main_agent.assess_turn(
                    job, repos,
                    fallback_risk=getattr(job, "risk_level", "medium"),
                    resumed=True,
                )
                if recovery_assessment:
                    await self.event_bus.publish(
                        "phase_summary", phase="governor",
                        agent_type="main_agent", status="success",
                        summary=(
                            recovery_assessment.get("summary")
                            or "主控模型已读取检查点并确定恢复路径"
                        ),
                        details={
                            "next_action": recovery_assessment.get(
                                "next_action", ""
                            )
                        },
                    )

            if self.skill_manager:
                self.skill_manager.configure(
                    proj_root, proj_config.skills, proj_config.plugins
                )
            if self.tool_broker and hasattr(self.tool_broker, "configure_mcp"):
                await self.tool_broker.configure_mcp(
                    proj_root, proj_config.mcp,
                    trusted_servers=proj_config.builtin_mcp_servers(
                        job.user_request
                    ),
                )

            for role in ("worker", "planner"):
                agent = self.get_agent(role)
                if agent and getattr(agent, "context_manager", None):
                    await agent.context_manager.switch_project(proj_root)

            repository = Repository(proj_root)
            repository.ensure_initialized()
            self.merge_manager = MergeManager(proj_root)
            baseline = self.test_manager.capture_snapshot(proj_root)
            complexity = self._classify_request(job.user_request)
            job._rockcore_complexity = complexity
            worker = self.get_agent("worker")
            if worker:
                saved_turns = worker.max_turns
                worker.max_turns = proj_config.get_worker_turns(complexity)

            await self.event_bus.publish(
                "job_resuming_from_checkpoint",
                job_id=job_id,
                checkpoint=dict(job.last_checkpoint or {}),
            )
            repos["job"].update_status(job_id, "executing")
            repos["job"].clear_failure(job_id)
            repos["_session"].refresh(job)

            constitution = repos["constitution"].get_by_job(job.id)
            if not constitution:
                precheck = self.model_router.risk_engine.precheck_request(
                    job.user_request, proj_root
                )
                if (
                    proj_config.governor.enabled
                    and (
                        proj_config.mode != "auto"
                        or self._risk_route(precheck.get("level")) == "high"
                    )
                ):
                    await self._run_governor(
                        job, repos, proj_config, fallback_precheck=precheck
                    )
                else:
                    self._create_precheck_constitution(
                        job, repos, precheck["level"]
                    )
                constitution = repos["constitution"].get_by_job(job.id)

            plan = repos["plan"].get_by_job(job.id)
            if not plan:
                await self._run_planner(job, repos, proj_config)
                repos["_session"].refresh(job)
                if job.status in {"failed", "needs_attention", "interrupted"}:
                    return {"status": job.status}

            tasks = repos["task"].list_by_job(job.id)
            resume_ids = self._resumable_task_ids(tasks)
            if resume_ids:
                for task in tasks:
                    if task.task_id in resume_ids:
                        repos["task"].update_status_by_pk(task.id, "pending")
                result = await self._run_execution(
                    job, repos, baseline,
                    proj_config=proj_config,
                    complexity=complexity,
                    task_ids=resume_ids,
                    resume_source_job_id=job_id,
                )
                repos["_session"].refresh(job)
                if result.get("status") != "completed":
                    return result

            # If execution had already completed, the checkpoint belongs to
            # Reviewer (for example a production credential was missing).
            constitution = repos["constitution"].get_by_job(job.id)
            if (
                proj_config.reviewer.enabled
                and (
                    proj_config.mode == "strict"
                    or self._risk_route(
                        getattr(job, "risk_level", "medium")
                    ) == "high"
                )
                and bool(getattr(constitution, "requires_final_review", True))
            ):
                await self._run_reviewer(
                    job, repos, proj_config=proj_config,
                    complexity=complexity,
                )
            else:
                await self._skip_review(
                    job, repos, "从检查点恢复后已通过确定性验证"
                )
            repos["_session"].refresh(job)
            if not self._is_cancelled(job_id, job, repos):
                await self._finalize(job, repos)
                finalized = True
            return {"status": job.status, "job_id": job_id}
        except BudgetExceededError as error:
            repos["job"].update_status(job_id, "needs_attention")
            self._store_job_failure(repos, job_id, str(error))
            self.state_machine.restore(job_id, JobState.WAITING_USER)
            await self.event_bus.publish(
                "job_needs_attention", job_id=job_id,
                reason=str(error), failure_stage="budget_continuation",
            )
            return {"status": "needs_attention", "reason": str(error)}
        except Exception as error:
            logger.exception("Checkpoint resume failed for %s", job_id)
            repos["job"].update_status(job_id, "failed")
            self._store_job_failure(repos, job_id, str(error))
            self.state_machine.restore(job_id, JobState.FAILED)
            await self.event_bus.publish(
                "job_failed", job_id=job_id, error=str(error),
                failure_stage="checkpoint_resume",
            )
            return {"status": "failed", "reason": str(error)}
        finally:
            if worker and saved_turns is not None:
                worker.max_turns = saved_turns
            if job is not None and not finalized:
                try:
                    await self._finalize(job, repos)
                except Exception as error:
                    logger.warning("Could not finalize resumed %s: %s", job_id, error)
            self._close_repos(repos)

    async def _run_job_pipeline(self, job_id: str, project_root: str):
        return await self.main_agent.run_turn(job_id, project_root)

    async def _run_job_pipeline_core(self, job_id: str, project_root: str):
        """Run one user turn under the persistent Main Agent."""
        logger.info(f"Running job: {job_id}")

        repos = self._get_repos()
        job = None
        worker = None
        saved_turns = None
        finalized = False
        try:
            job = repos["job"].get_by_id(job_id)
            if not job:
                raise ValueError(f"Job not found: {job_id}")
            self.main_agent.prepare_turn(job, repos)
            repos["_session"].refresh(job)

            # Track current job ID for chat logging
            self.model_router.set_job_id(job_id)

            # Update tool broker to the actual project root
            if job.project and self.tool_broker:
                self.tool_broker.set_project_root(job.project.root_path)

            # Switch context manager to the user's project
            if job.project:
                worker = self.get_agent("worker")
                if worker and worker.context_manager:
                    await worker.context_manager.switch_project(job.project.root_path)
                planner = self.get_agent("planner")
                if planner and planner.context_manager:
                    await planner.context_manager.switch_project(job.project.root_path)

            # Initialize merge manager for this job
            repository = Repository(project_root)
            repo_state = repository.ensure_initialized()
            if repo_state.get("status") == "failed":
                await self.event_bus.publish(
                    "project_git_unavailable", project_root=project_root,
                    error=repo_state.get("error", "Git initialization failed"),
                )
            elif repo_state.get("status") == "initialized":
                await self.event_bus.publish(
                    "project_git_initialized", project_root=project_root,
                    commit=repo_state.get("commit", ""),
                )
            if repo_state.get("gitignore_updated"):
                await self.event_bus.publish(
                    "project_gitignore_updated",
                    project_root=project_root,
                    summary="已自动维护项目 .gitignore 和本地排除规则",
                )
            unmerged_files = repository.unmerged_files()
            if unmerged_files:
                error = (
                    "RockCore detected an unrecoverable internal Git state: "
                    + ", ".join(unmerged_files[:8])
                )
                repos["job"].update_status(job.job_id, "failed")
                self._store_job_failure(repos, job.job_id, error)
                self.state_machine.transition(job.job_id, JobState.GOVERNING)
                self.state_machine.transition(job.job_id, JobState.FAILED)
                await self.event_bus.publish(
                    "job_failed", job_id=job.job_id, error=error,
                    failure_stage="git_conflict",
                )
                return
            self.merge_manager = MergeManager(project_root)
            job_baseline = self.test_manager.capture_snapshot(project_root)

            # Load project-level AI config
            proj_root = job.project.root_path if job.project else project_root
            proj_config = load_project_config(proj_root)
            logger.info(f"Job {job_id}: mode={proj_config.mode}")
            before_job_hooks = await self._run_project_hooks(
                proj_config, "before_job", job_id=job_id,
                project_root=proj_root,
            )
            if any(item.get("status") != "passed" for item in before_job_hooks):
                raise RuntimeError(
                    "before_job hook failed: "
                    + str(before_job_hooks[-1].get("output") or "unknown error")
                )

            project_surface = await self._resolve_project_surface(
                job, repos, proj_root
            )
            job._rockcore_project_surface = project_surface
            for agent_type in ("planner", "worker"):
                agent = self.get_agent(agent_type)
                context_manager = getattr(agent, "context_manager", None)
                if context_manager and hasattr(
                    context_manager, "set_project_surface"
                ):
                    context_manager.set_project_surface(project_surface)

            if self.skill_manager:
                self.skill_manager.configure(
                    proj_root, proj_config.skills, proj_config.plugins
                )
            mcp_status = {}
            if self.tool_broker and hasattr(self.tool_broker, "configure_mcp"):
                mcp_status = await self.tool_broker.configure_mcp(
                    proj_root, proj_config.mcp,
                    trusted_servers=proj_config.builtin_mcp_servers(
                        job.user_request
                    ),
                )
            await self.event_bus.publish(
                "extensions_ready",
                job_id=job_id,
                skills=(
                    [item.name for item in self.skill_manager.list_skills()]
                    if self.skill_manager else []
                ),
                project_skills_approved=(
                    self.skill_manager.project_skills_approved
                    if self.skill_manager else False
                ),
                mcp=mcp_status,
            )

            # Classify request complexity
            complexity = self._classify_request(job.user_request)
            job._rockcore_complexity = complexity
            logger.info(f"Job {job_id}: complexity={complexity}")

            precheck = self.model_router.risk_engine.precheck_request(
                job.user_request, proj_root
            )
            initial_risk_route = self._risk_route(precheck.get("level"))
            advisor_decision = self.main_agent.decide_advisors(
                mode=proj_config.mode,
                risk_route=initial_risk_route,
                complexity=complexity,
                has_attachments=bool(
                    getattr(job, "attachments", None) or job.source_job_id
                ),
                governor_enabled=proj_config.governor.enabled,
                planner_enabled=proj_config.planner.enabled,
                reviewer_enabled=proj_config.reviewer.enabled,
            )
            await self.event_bus.publish(
                "main_agent_routed", job_id=job_id,
                governor=advisor_decision.governor,
                planner=advisor_decision.planner,
                reviewer=advisor_decision.reviewer,
                summary=advisor_decision.reason,
            )

            profiles = {
                "main_agent": proj_config.governor,
                "main_agent_summary": proj_config.governor,
                "governor": proj_config.governor,
                "planner": proj_config.planner,
                "worker": proj_config.worker,
                "reviewer": proj_config.reviewer,
                "emergency_coder": proj_config.emergency_coder,
            }
            self.model_router.set_job_routing(
                job_id,
                provider_map={
                    role: profile.provider for role, profile in profiles.items()
                    if profile.provider
                },
                model_map={
                    role: profile.model for role, profile in profiles.items()
                    if profile.model
                },
                reasoning_map={
                    role: profile.reasoning_effort
                    for role, profile in profiles.items()
                    if profile.reasoning_effort
                },
            )

            governor_completed = False
            main_agent_assessment = None
            if proj_config.mode == "fast":
                # Text-only fast mode opts out of governance. Image requests
                # still need one vision-capable pass so a text Worker receives
                # the actual requirement instead of an attachment filename.
                if getattr(job, "attachments", None) and self.get_agent("governor"):
                    risk_assessment = await self._run_governor(
                        job, repos, proj_config, fallback_precheck=precheck
                    )
                    governor_completed = True
                else:
                    risk_assessment = {
                        "risk": precheck["level"],
                        "risk_score": precheck["score"],
                        "risk_reasons": precheck["reasons"],
                        "source": "fast_mode_rules",
                    }
                workflow_route = "low"
            else:
                # Auto mode uses deterministic risk/permission preflight. A
                # model Governor is reserved for high-risk ambiguity; standard,
                # strict and custom modes retain their explicit configuration.
                use_model_governor = advisor_decision.governor
                if use_model_governor:
                    repos["job"].update_status(job.job_id, "governing")
                    self.state_machine.transition(job.job_id, JobState.GOVERNING)
                    await self.event_bus.publish(
                        "job_governing", job_id=job.job_id
                    )
                    main_agent_assessment = await self.main_agent.assess_turn(
                        job, repos, fallback_risk=precheck["level"]
                    )
                    if main_agent_assessment:
                        self._persist_main_agent_constitution(
                            job, repos, main_agent_assessment
                        )
                        risk_assessment = main_agent_assessment
                        self.state_machine.transition(
                            job.job_id, JobState.GOVERNED
                        )
                        await self.event_bus.publish(
                            "job_governed", job_id=job.job_id
                        )
                        governor_completed = True
                    else:
                        risk_assessment = await self._run_governor(
                            job, repos, proj_config,
                            fallback_precheck=precheck,
                            phase_started=True,
                        )
                        governor_completed = True
                else:
                    await self._skip_phase(
                        job, repos, "governor",
                        "已由确定性风险与权限预检完成"
                        if proj_config.governor.enabled else "已按项目配置禁用"
                    )
                    self._create_precheck_constitution(
                        job, repos, precheck["level"]
                    )
                    risk_assessment = {
                        "risk": precheck["level"],
                        "risk_score": precheck["score"],
                        "risk_reasons": precheck["reasons"],
                        "source": "deterministic_precheck",
                    }
                    governor_completed = True

                workflow_route = (
                    self._risk_route(risk_assessment.get("risk"))
                    if proj_config.mode == "auto"
                    else "configured"
                )

            # Risk and workload size are different. A low-risk screenshot can
            # still describe a large document task. Reclassify from Governor's
            # visual observations before deciding to bypass the Planner.
            if getattr(job, "attachments", None) and governor_completed:
                governed_scope = self._governed_attachment_scope(job, repos)
                if governed_scope:
                    governed_complexity = self._classify_request(governed_scope)
                    complexity_rank = {"simple": 0, "normal": 1, "complex": 2}
                    if (
                        complexity_rank[governed_complexity]
                        > complexity_rank[complexity]
                    ):
                        complexity = governed_complexity
                        job._rockcore_complexity = complexity
                    if workflow_route == "low" and complexity != "simple":
                        workflow_route = "medium"

            assessed_risk = self._normalized_risk_level(
                risk_assessment.get("risk"), precheck["level"]
            )
            final_advisor_decision = self.main_agent.decide_advisors(
                mode=proj_config.mode,
                risk_route=self._risk_route(assessed_risk),
                complexity=complexity,
                has_attachments=bool(
                    getattr(job, "attachments", None) or job.source_job_id
                ),
                governor_enabled=proj_config.governor.enabled,
                planner_enabled=proj_config.planner.enabled,
                reviewer_enabled=proj_config.reviewer.enabled,
            )
            advisor_decision = final_advisor_decision
            if main_agent_assessment:
                advisor_decision = type(final_advisor_decision)(
                    governor=True,
                    planner=(
                        proj_config.planner.enabled
                        and bool(main_agent_assessment.get("use_planner"))
                    ),
                    reviewer=(
                        proj_config.reviewer.enabled
                        and (
                            bool(main_agent_assessment.get("use_reviewer"))
                            or assessed_risk == "high"
                            or proj_config.mode == "strict"
                        )
                    ),
                    reason="由主控模型根据当前会话选择顾问",
                )
                await self.event_bus.publish(
                    "phase_summary",
                    phase="governor", agent_type="main_agent",
                    status="success",
                    summary=(
                        main_agent_assessment.get("summary")
                        or "主控模型已理解当前需求并选择执行路径"
                    ),
                    details={
                        "strategy": main_agent_assessment.get(
                            "execution_strategy"
                        ),
                        "next_action": main_agent_assessment.get("next_action"),
                    },
                )
            self.main_agent.record_advisor_decision(
                job, repos, final_advisor_decision
            )
            repos["_session"].refresh(job)
            # Workload complexity, not only safety risk, determines whether an
            # explicit plan is useful. The Main Agent remains the owner either
            # way; Planner is an optional advisor for non-trivial turns.
            if (
                workflow_route == "low"
                and advisor_decision.planner
                and risk_assessment.get("source") != "governor"
            ):
                workflow_route = "medium"
            job._rockcore_workflow_route = workflow_route
            repos["job"].update_risk_level(job_id, assessed_risk)
            job.risk_level = assessed_risk
            await self.event_bus.publish(
                "governor_risk_assessed",
                job_id=job_id,
                risk_level=assessed_risk,
                risk_score=risk_assessment.get("risk_score", precheck["score"]),
                workflow_route=workflow_route,
                reasons=risk_assessment.get("risk_reasons", []),
                source=risk_assessment.get("source", "governor"),
                fallback_score=precheck["score"],
                fallback_reasons=precheck["reasons"],
                has_tests=precheck["has_tests"],
            )
            logger.info(
                "Job %s: risk source=%s level=%s route=%s",
                job_id, risk_assessment.get("source", "governor"),
                assessed_risk, workflow_route,
            )

            # Apply config: set worker turn limits
            worker = self.get_agent("worker")
            if worker:
                saved_turns = worker.max_turns
                worker.max_turns = proj_config.get_worker_turns(complexity)

            if workflow_route == "low":
                await self._run_simple(
                    job, repos, proj_config,
                    governor_completed=governor_completed,
                )
            else:
                auto_medium = (
                    proj_config.mode == "auto" and workflow_route == "medium"
                )
                if self._is_cancelled(job.job_id, job, repos):
                    if worker and saved_turns is not None:
                        worker.max_turns = saved_turns
                    return

                # ── Phase 2: Planner ──
                if advisor_decision.planner:
                    await self._run_planner(job, repos, proj_config)
                    if job.status in {"failed", "needs_attention", "interrupted"}:
                        if worker and saved_turns is not None:
                            worker.max_turns = saved_turns
                        return
                else:
                    await self._skip_phase(job, repos, "planner")
                    self._create_direct_plan(job, repos, proj_config)
                # The request classifier only sees the user's short prompt. The
                # Planner has much better scope information, so let a broad plan
                # promote the runtime budget before Worker execution starts.
                complexity = getattr(job, "_rockcore_complexity", complexity)
                if worker:
                    worker.max_turns = proj_config.get_worker_turns(complexity)
                if self._is_cancelled(job.job_id, job, repos):
                    if worker and saved_turns is not None:
                        worker.max_turns = saved_turns
                    return

                # ── Phase 3: Execute tasks ──
                await self._run_execution(
                    job, repos, job_baseline,
                    proj_config=proj_config,
                    complexity=complexity,
                )
                repos["_session"].refresh(job)
                if job.status == "cancelled":
                    if worker and saved_turns is not None:
                        worker.max_turns = saved_turns
                    return
                if job.status in {"failed", "interrupted", "needs_attention"}:
                    if worker and saved_turns is not None:
                        worker.max_turns = saved_turns
                    return

                # ── Phase 4: Review ──
                use_model_reviewer = advisor_decision.reviewer
                if use_model_reviewer:
                    await self._run_reviewer(
                        job, repos,
                        proj_config=proj_config,
                        complexity=complexity,
                    )
                else:
                    await self._skip_review(
                        job, repos,
                        "确定性验证已通过；当前风险无需额外模型审核"
                        if proj_config.reviewer.enabled else "审核已按项目配置跳过",
                    )

            if worker and saved_turns is not None:
                worker.max_turns = saved_turns

            # ── Phase 5: Finalize ──
            if not self._is_cancelled(job.job_id, job, repos):
                await self._finalize(job, repos)
                finalized = True

        except BudgetExceededError as e:
            logger.warning("Job paused for budget continuation: %s: %s", job_id, e)
            repos["job"].update_status(job_id, "needs_attention")
            self._store_job_failure(repos, job_id, str(e))
            self.state_machine.transition(job_id, JobState.WAITING_USER)
            await self.event_bus.publish(
                "job_needs_attention",
                job_id=job_id,
                reason=str(e),
                failure_stage="budget_continuation",
                budget=self.model_router.cost_engine.get_budget_snapshot(job_id),
            )
        except Exception as e:
            logger.error(f"Job failed: {job_id}: {e}")
            repos["job"].update_status(job_id, "failed")
            self._store_job_failure(repos, job_id, str(e))
            self.state_machine.transition(job_id, JobState.FAILED)
            await self.event_bus.publish("job_failed", job_id=job_id, error=str(e))
        finally:
            if worker and saved_turns is not None:
                worker.max_turns = saved_turns
            if job is not None and not finalized:
                try:
                    await self._finalize(job, repos)
                except Exception as error:
                    logger.warning("Could not finalize %s: %s", job_id, error)
            self._close_repos(repos)

    def _create_precheck_constitution(self, job, repos, risk_level: str):
        """Persist deterministic conservative bounds when Governor is skipped."""
        if repos["constitution"].get_by_job(job.id):
            return
        normalized_risk = "high" if risk_level == "critical" else risk_level
        inherited = self._inherited_image_understanding(job, repos)
        repos["constitution"].create(
            job_id=job.id,
            goal=job.user_request,
            constraints=["只修改完成当前需求所必需的文件"],
            acceptance_criteria=["确定性验证通过", "需求中的可观察结果已实现"],
            risk=normalized_risk or "medium",
            protected_paths=[],
            requires_final_review=normalized_risk == "high",
            raw_output={
                "source": "deterministic_precheck",
                "image_observations": inherited["observations"],
                "inherited_image_goals": inherited["goals"],
            },
        )

    def _persist_main_agent_constitution(self, job, repos,
                                         assessment: dict) -> None:
        """Persist the model owner's bounded decision for existing phases."""
        if repos["constitution"].get_by_job(job.id):
            return
        inherited = self._inherited_image_understanding(job, repos)
        observations = self._dedupe_text_values(
            inherited["observations"]
            + list(assessment.get("image_observations") or []),
            limit=16,
        )
        repos["constitution"].create(
            job_id=job.id,
            goal=assessment.get("goal") or job.user_request,
            constraints=list(assessment.get("constraints") or []),
            acceptance_criteria=list(
                assessment.get("acceptance_criteria")
                or ["确定性验证通过"]
            ),
            risk=assessment.get("risk", "medium"),
            protected_paths=list(assessment.get("protected_paths") or []),
            requires_final_review=bool(
                assessment.get("use_reviewer")
                or assessment.get("risk") == "high"
            ),
            raw_output={
                **dict(assessment),
                "source": "main_agent",
                "image_observations": observations,
                "inherited_image_goals": inherited["goals"],
            },
        )
        risk_cn = {
            "low": "低", "medium": "中", "high": "高",
        }.get(assessment.get("risk"), "中")
        # The event is published by the caller to preserve async ordering.
        logger.info(
            "Main Agent understood %s: risk=%s strategy=%s",
            job.job_id, risk_cn, assessment.get("execution_strategy"),
        )

    @staticmethod
    def _normalized_risk_level(value, fallback: str = "medium") -> str:
        level = str(value or "").lower()
        if level == "critical":
            return "high"
        if level in {"low", "medium", "high"}:
            return level
        fallback_level = str(fallback or "medium").lower()
        return "high" if fallback_level == "critical" else (
            fallback_level
            if fallback_level in {"low", "medium", "high"}
            else "medium"
        )

    @classmethod
    def _risk_route(cls, risk_level) -> str:
        return cls._normalized_risk_level(risk_level)

    async def _run_governor(self, job, repos, proj_config=None,
                            fallback_precheck: dict | None = None,
                            phase_started: bool = False) -> dict:
        """Have Governor classify risk and persist the resulting constitution."""
        if not phase_started:
            repos["job"].update_status(job.job_id, "governing")
            self.state_machine.transition(job.job_id, JobState.GOVERNING)
            await self.event_bus.publish("job_governing", job_id=job.job_id)

        precheck = fallback_precheck or {}
        fallback_risk = self._normalized_risk_level(
            precheck.get("level"), getattr(job, "risk_level", "medium")
        )
        assessment = {
            "risk": fallback_risk,
            "risk_score": int(precheck.get("score", 50) or 50),
            "risk_reasons": list(precheck.get("reasons") or [
                "裁决者不可用，使用规则兜底"
            ]),
            "source": "rules_fallback",
        }
        inherited = self._inherited_image_understanding(job, repos)
        governor = self.get_agent("governor")
        if governor:
            try:
                effective_request = self._request_with_context(job, repos, proj_config)
                constitution = await governor.run(
                    effective_request, job.project,
                    attachments=getattr(job, "attachments", None) or [],
                )
                risk = self._normalized_risk_level(
                    constitution.get("risk"), fallback_risk
                )
                try:
                    risk_score = max(
                        0, min(100, int(constitution.get("risk_score", 50)))
                    )
                except (TypeError, ValueError, OverflowError):
                    risk_score = 50
                risk_reasons = constitution.get("risk_reasons")
                if not isinstance(risk_reasons, list):
                    risk_reasons = [str(risk_reasons)] if risk_reasons else []
                risk_reasons = [
                    str(reason)[:300] for reason in risk_reasons
                    if str(reason).strip()
                ][:6] or ["裁决者按需求影响范围完成风险评估"]
                current_observations = constitution.get("image_observations") or []
                if not isinstance(current_observations, list):
                    current_observations = [current_observations]
                image_observations = self._dedupe_text_values(
                    inherited["observations"] + current_observations,
                    limit=12,
                )
                repos["constitution"].create(
                    job_id=job.id,
                    goal=constitution.get("goal", job.user_request),
                    constraints=list(constitution.get("constraints") or []),
                    acceptance_criteria=list(
                        constitution.get("acceptance_criteria")
                        or ["All tests pass"]
                    ),
                    risk=risk,
                    protected_paths=list(
                        constitution.get("protected_paths") or []
                    ),
                    requires_final_review=bool(
                        constitution.get("requires_final_review", True)
                        or risk == "high"
                    ),
                    raw_output={
                        "source": "governor",
                        "risk_score": risk_score,
                        "risk_reasons": risk_reasons,
                        "image_observations": image_observations,
                        "inherited_image_goals": inherited["goals"],
                    },
                )
                assessment = {
                    "risk": risk,
                    "risk_score": risk_score,
                    "risk_reasons": risk_reasons,
                    "source": "governor",
                }
                risk_cn = {"low": "低", "medium": "中", "high": "高"}[risk]
                await self.event_bus.publish("phase_summary",
                    phase="governor", agent_type="governor", status="success",
                    summary=(
                        f"分析了需求：{constitution.get('goal', job.user_request)}，"
                        f"风险等级：{risk_cn}（{risk_score} 分）"
                    ),
                    details={"risk_reasons": risk_reasons},
                )
            except Exception as e:
                logger.warning(f"Governor failed, using defaults: {e}")
                failure_reason = self._friendly_provider_error(str(e))
                repos["constitution"].create(
                    job_id=job.id,
                    goal=job.user_request,
                    constraints=[],
                    acceptance_criteria=["All tests pass"],
                    risk=fallback_risk,
                    protected_paths=[],
                    requires_final_review=True,
                    raw_output={
                        "fallback": True, "error": str(e),
                        "source": "rules_fallback",
                        "risk_score": assessment["risk_score"],
                        "risk_reasons": assessment["risk_reasons"],
                        "image_observations": inherited["observations"],
                        "inherited_image_goals": inherited["goals"],
                    },
                )
                await self.event_bus.publish(
                    "phase_summary",
                    phase="governor", agent_type="governor", status="fallback",
                    summary=(
                        "裁决者不可用，已使用规则预检作为风险兜底继续执行。"
                        f"原因：{failure_reason}"
                    ),
                    details={"error": failure_reason},
                )
        else:
            repos["constitution"].create(
                job_id=job.id,
                goal=job.user_request,
                constraints=[],
                acceptance_criteria=["All tests pass"],
                risk=fallback_risk,
                protected_paths=[],
                requires_final_review=True,
                raw_output={
                    "fallback": True,
                    "error": "Governor not registered",
                    "source": "rules_fallback",
                    "risk_score": assessment["risk_score"],
                    "risk_reasons": assessment["risk_reasons"],
                    "image_observations": inherited["observations"],
                    "inherited_image_goals": inherited["goals"],
                },
            )
            await self.event_bus.publish(
                "phase_summary",
                phase="governor", agent_type="governor", status="fallback",
                summary="未注册裁决者，已使用规则预检作为风险兜底继续执行",
            )

        self.state_machine.transition(job.job_id, JobState.GOVERNED)
        await self.event_bus.publish("job_governed", job_id=job.job_id)
        return assessment

    async def _run_planner(self, job, repos, proj_config=None):
        self.state_machine.transition(job.job_id, JobState.PLANNING)
        repos["job"].update_status(job.job_id, "planning")
        await self.event_bus.publish("job_planning", job_id=job.job_id)

        planner = self.get_agent("planner")
        constitution = repos["constitution"].get_by_job(job.id)

        used_fallback = planner is None
        if planner:
            continuation_context = self._continuation_context(job, repos, proj_config)
            try:
                plan_data = await planner.run(
                    job, constitution, continuation_context=continuation_context
                )
            except PlannerOutputTruncatedError as error:
                await self._fail_truncated_plan(job, repos, error)
                return
        else:
            plan_data = self._direct_plan_data(job, repos, proj_config)

        if not plan_data.get("tasks"):
            used_fallback = True
            plan_data = self._direct_plan_data(job, repos, proj_config)

        self._ground_plan_in_project_surface(
            plan_data, getattr(job, "_rockcore_project_surface", None)
        )

        task_types_corrected = self._normalize_plan_task_types(
            plan_data, job.user_request
        )
        if task_types_corrected:
            await self.event_bus.publish(
                "plan_task_types_corrected",
                job_id=job.job_id,
                task_type="analysis",
                reason="纯查看需求以分析报告作为交付物，不要求修改项目文件",
            )

        initial_complexity = getattr(job, "_rockcore_complexity", "normal")
        effective_complexity = self._promote_complexity_from_plan(
            initial_complexity, plan_data
        )
        job._rockcore_complexity = effective_complexity
        if effective_complexity != initial_complexity:
            logger.info(
                "Job %s: planner promoted complexity %s -> %s",
                job.job_id, initial_complexity, effective_complexity,
            )
            await self.event_bus.publish(
                "plan_complexity_promoted",
                job_id=job.job_id,
                previous_complexity=initial_complexity,
                complexity=effective_complexity,
                reason="策划步骤数量或文件范围超过简单任务阈值",
            )

        self._optimize_plan(plan_data, effective_complexity)
        self._merge_shared_context_tasks(
            plan_data, getattr(job, "_rockcore_project_surface", None)
        )
        self._serialize_overlapping_tasks(
            plan_data, getattr(job, "_rockcore_project_surface", None)
        )
        self._prune_transitive_dependencies(plan_data)
        self._assign_plan_skills(plan_data)

        protected_paths = (
            constitution.protected_paths if constitution else []
        )
        preliminary_errors = self.policy_engine.check_task_plan(
            plan_data, {"protected_paths": protected_paths}
        )
        preliminary_errors.extend(
            self._plan_granularity_errors(plan_data, job.user_request)
        )
        quality_errors = [
            error for error in preliminary_errors
            if (
                str(error).startswith((
                    "continuation_quality:", "granularity_quality:",
                ))
                or "execution-stage plans may contain at most 8" in str(error)
            )
        ]
        if planner and quality_errors:
            await self.event_bus.publish(
                "plan_replanning", job_id=job.job_id,
                reason="plan_quality", errors=quality_errors,
            )
            rejection = (
                continuation_context
                + "\n\n=== PLAN QUALITY REJECTION ===\n"
                + "\n".join(f"- {error}" for error in quality_errors)
                + "\nCreate a concrete continuation plan. Name the remaining "
                  "artifact/files, preserve completed work, and include a "
                  "deterministic acceptance command or a concrete file scope."
                  " Return no more than 8 coherent execution stages. Group tightly "
                  "coupled same-file behavior, while covering every requirement."
            )
            try:
                revised = await planner.run(
                    job, constitution, continuation_context=rejection[:12000]
                )
            except PlannerOutputTruncatedError as error:
                await self._fail_truncated_plan(job, repos, error)
                return
            if revised.get("tasks"):
                plan_data = revised
                self._ground_plan_in_project_surface(
                    plan_data, getattr(job, "_rockcore_project_surface", None)
                )
                self._optimize_plan(plan_data, effective_complexity)
                self._merge_shared_context_tasks(
                    plan_data, getattr(job, "_rockcore_project_surface", None)
                )
                self._serialize_overlapping_tasks(
                    plan_data, getattr(job, "_rockcore_project_surface", None)
                )
                self._prune_transitive_dependencies(plan_data)
                self._assign_plan_skills(plan_data)
            else:
                plan_data = self._direct_plan_data(job, repos, proj_config)
                self._assign_plan_skills(plan_data)

        # Validate plan against constitution
        self.state_machine.transition(job.job_id, JobState.PLAN_CHECK)
        errors = self.policy_engine.check_task_plan(
            plan_data, {"protected_paths": protected_paths}
        )
        if errors and not any(
            marker in str(error).lower()
            for error in errors
            for marker in (
                "protected_path", "contains traversal", "must be relative"
            )
        ):
            original_errors = list(errors)
            fallback_plan = self._direct_plan_data(job, repos, proj_config)
            self._assign_plan_skills(fallback_plan)
            fallback_errors = self.policy_engine.check_task_plan(
                fallback_plan, {"protected_paths": protected_paths}
            )
            if not fallback_errors:
                used_fallback = True
                plan_data = fallback_plan
                errors = []
                await self.event_bus.publish(
                    "plan_recovered",
                    job_id=job.job_id,
                    errors=original_errors,
                    strategy="direct_single_task",
                )

        plan = repos["plan"].create(
            job_id=job.id,
            summary=plan_data.get("summary", ""),
            raw_output=plan_data,
        )
        repos["plan"].update_validation(
            plan.id, validated=len(errors) == 0, errors=errors
        )
        checkpoint = dict(getattr(job, "last_checkpoint", None) or {})
        session = normalize_session(
            checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=(constitution.goal if constitution else job.user_request),
        )
        session["acceptance_criteria"] = list(
            constitution.acceptance_criteria if constitution else []
        )
        session["constraints"] = list(
            constitution.constraints if constitution else []
        )
        session["checklist"] = [{
            "id": str(task.get("id") or ""),
            "title": str(task.get("title") or ""),
            "status": "pending",
            "summary": "",
        } for task in plan_data.get("tasks", [])]
        session["current_step"] = (
            session["checklist"][0]["id"] if session["checklist"] else ""
        )
        session["next_action"] = "Execute the first pending checklist item"
        checkpoint["execution_session"] = session
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        job.last_checkpoint = checkpoint

        if errors:
            logger.warning(f"Plan validation failed: {errors}")
            repos["job"].update_status(job.job_id, "failed")
            self._store_job_failure(
                repos, job.job_id, f"Plan validation failed: {errors[0]}"
            )
            self.state_machine.transition(job.job_id, JobState.FAILED)
            await self.event_bus.publish("plan_rejected", job_id=job.job_id, errors=errors)
            await self.event_bus.publish("phase_summary",
                phase="planner", agent_type="planner", status="rejected",
                summary=f"计划需要调整：{errors[0][:80] if errors else '未知错误'}",
                details={"errors": errors},
            )
            await self.event_bus.publish(
                "job_failed", job_id=job.job_id,
                error=f"计划生成失败：{errors[0]}",
                failure_stage="plan_validation",
            )
            return

        self._create_tasks_from_plan(job, repos, plan_data)

        self.state_machine.transition(job.job_id, JobState.READY)
        await self.event_bus.publish("plan_ready", job_id=job.job_id)

        num_tasks = len(plan_data.get("tasks", []))
        await self.event_bus.publish("phase_summary",
            phase="planner", agent_type="planner", status="success",
            summary=(
                f"策划者未返回可执行步骤，已生成安全的单步计划，共 {num_tasks} 个任务"
                if used_fallback else
                f"制定了执行计划：{plan_data.get('summary', '')}，拆分为 {num_tasks} 个任务"
            ),
            details={"tasks": plan_data.get("tasks", [])},
        )

    async def _fail_truncated_plan(self, job, repos, error: Exception) -> None:
        """Pause a provider-side plan truncation without fabricating tasks."""
        message = str(error)
        logger.error("Planner output truncated for %s: %s", job.job_id, message)
        checkpoint = dict(job.last_checkpoint or {})
        checkpoint.update({
            "phase": "planner",
            "next_action": "Retry the Planner response from this phase",
            "planner_error": message,
        })
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        repos["job"].update_status(job.job_id, "interrupted")
        self._store_job_failure(repos, job.job_id, message)
        self.state_machine.transition(job.job_id, JobState.WAITING_USER)
        await self.event_bus.publish(
            "phase_summary",
            phase="planner", agent_type="planner", status="interrupted",
            summary="策划者输出被服务端截断；已保存阶段，可继续重试完整响应",
            details={"error": message},
        )
        await self.event_bus.publish(
            "job_interrupted", job_id=job.job_id, reason=message,
            failure_stage="planner_output_truncated",
        )

    def _direct_plan_data(self, job, repos, proj_config=None) -> dict:
        """Build one executable task when planning is explicitly unavailable."""
        description = self._request_with_context(job, repos, proj_config)
        task_type = self._request_task_type(job.user_request)
        title = self._effective_task_title(job, repos)
        surface = dict(getattr(job, "_rockcore_project_surface", None) or {})
        allowed_paths = list(surface.get("active_files") or []) or ["*"]
        if getattr(job, "source_job_id", None):
            source = repos["job"].get_by_id(job.source_job_id)
            if source:
                title = f"继续完成：{source.user_request[:48]}"
                concrete = []
                for previous in repos["task"].list_by_job(source.id):
                    if previous.status == "done":
                        continue
                    concrete.extend(
                        str(path) for path in (previous.allowed_paths or [])
                        if path and "*" not in str(path)
                    )
                    data = dict(previous.result_data or {})
                    changes = data.get("changes") or {}
                    concrete.extend(changes.get("changed") or [])
                if concrete:
                    allowed_paths = list(dict.fromkeys(concrete))[:40]
                else:
                    root = Path(job.project.root_path if job.project else ".")
                    try:
                        existing = [
                            path.relative_to(root).as_posix()
                            for path in root.rglob("*")
                            if path.is_file()
                            and not {".git", ".ai"}.intersection(
                                path.relative_to(root).parts
                            )
                        ][:40]
                    except OSError:
                        existing = []
                    if existing:
                        allowed_paths = existing
        return {
            "summary": f"单步执行：{job.user_request[:100]}",
            "tasks": [{
                "id": "T001",
                "title": title,
                "type": task_type,
                "description": description,
                "dependencies": [],
                "allowed_paths": allowed_paths,
                "acceptance_command": "",
            }],
        }

    async def _resolve_project_surface(self, job, repos, project_root: str) -> dict:
        """Resolve and persist the runtime surface before any model plans work."""
        await self.event_bus.publish(
            "project_resolving", job_id=job.job_id,
            project_root=project_root,
        )
        try:
            surface = await asyncio.to_thread(
                ProjectResolver(project_root).resolve
            )
        except Exception as error:
            logger.warning("Project resolver failed for %s: %s", job.job_id, error)
            surface = {
                "version": 1,
                "entrypoints": [],
                "active_files": [],
                "support_files": [],
                "legacy_files": [],
                "duplicate_symbols": [],
                "commands": {},
                "ambiguities": [f"项目静态解析不可用：{error}"],
                "confidence": 0.0,
                "file_count": 0,
            }
        checkpoint = dict(getattr(job, "last_checkpoint", None) or {})
        checkpoint["project_surface"] = surface
        checkpoint["updated_at"] = datetime.now().astimezone().isoformat()
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        job.last_checkpoint = checkpoint
        await self.event_bus.publish(
            "project_resolved",
            job_id=job.job_id,
            entrypoints=surface.get("entrypoints") or [],
            active_files=surface.get("active_files") or [],
            legacy_files=surface.get("legacy_files") or [],
            ambiguities=surface.get("ambiguities") or [],
            commands=surface.get("commands") or {},
            confidence=surface.get("confidence", 0.0),
        )
        return surface

    @classmethod
    def _ground_plan_in_project_surface(cls, plan_data: dict,
                                        surface: dict | None) -> bool:
        """Replace model-wide globs with the files reachable from real entries."""
        active = [
            str(path).replace("\\", "/").lstrip("./")
            for path in ((surface or {}).get("active_files") or [])
            if str(path).strip()
        ]
        if not active:
            return False
        changed = False
        for task in plan_data.get("tasks") or []:
            if str(task.get("type") or "coding") == "action":
                continue
            paths = [str(path) for path in (task.get("allowed_paths") or [])]
            broad = not paths or any(
                cls._is_project_wide_plan_path(path) for path in paths
            )
            if not broad:
                continue
            concrete = [
                path.replace("\\", "/").lstrip("./")
                for path in paths if not cls._is_project_wide_plan_path(path)
            ]
            narrowed = list(dict.fromkeys(active + concrete))[:80]
            if narrowed != paths:
                task["allowed_paths"] = narrowed
                changed = True
        return changed

    @staticmethod
    def _is_project_wide_plan_path(path: str) -> bool:
        normalized = str(path or "").replace("\\", "/").strip()
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.rstrip("/")
        return normalized in {"", ".", "*", "**", "**/*", "**/**"}

    def _create_tasks_from_plan(self, job, repos, plan_data: dict,
                                order_offset: int = 0):
        for i, task_data in enumerate(plan_data.get("tasks", [])):
            selected_skills = (
                self.skill_manager.select_for_task(task_data)
                if self.skill_manager else []
            )
            task_data["skills"] = selected_skills
            repos["task"].create(
                task_id=task_data.get("id", f"T{i+1:03d}"),
                job_id=job.id,
                title=task_data.get("title", ""),
                task_type=task_data.get("type", "coding"),
                description=task_data.get("description", ""),
                allowed_paths=self._normalize_paths(
                    task_data.get("allowed_paths", []),
                    job.project.root_path if job.project else ".",
                ),
                dependencies=task_data.get("dependencies", []),
                acceptance_command=task_data.get("acceptance_command", ""),
                order=order_offset + i,
                skills=selected_skills,
            )

    def _assign_plan_skills(self, plan_data: dict):
        """Normalize model suggestions against the discovered Skill catalog."""
        for task_data in plan_data.get("tasks", []):
            selected = (
                self.skill_manager.select_for_task(task_data)
                if self.skill_manager else []
            )
            task_text = " ".join((
                str(task_data.get("title") or ""),
                str(task_data.get("description") or ""),
                " ".join(map(str, task_data.get("allowed_paths") or [])),
            ))
            if self._is_document_request(task_text) and "pdf" not in selected:
                if self.skill_manager and any(
                    item.name == "pdf" for item in self.skill_manager.list_skills()
                ):
                    selected.insert(0, "pdf")
            task_data["skills"] = selected

    @staticmethod
    def _plan_granularity_errors(plan_data: dict,
                                 user_request: str = "") -> list[str]:
        """Flag obvious umbrella tasks so Planner gets one focused replan."""
        errors: list[str] = []
        umbrella_markers = (
            "remaining systems", "all remaining", "complete everything",
            "implement all", "finish all", "所有剩余", "全部系统",
            "完成全部", "实现所有", "其余全部",
        )
        range_pattern = re.compile(
            r"(?:zone|level|scene|page|区域|关卡|场景|页面|阶段)\s*"
            r"(\d{1,2})\s*[-–—~至]\s*(\d{1,2})",
            re.IGNORECASE,
        )
        for task in list(plan_data.get("tasks") or []):
            if str(task.get("type") or "coding") not in {"coding", "action"}:
                continue
            task_id = str(task.get("id") or "?")
            text = " ".join((
                str(task.get("title") or ""),
                str(task.get("description") or ""),
            ))
            normalized = text.lower()
            # The shared-context optimizer deliberately turns several focused
            # stages into one long-lived Worker task with an internal checklist.
            # Do not ask Planner to split that task again: doing so would recreate
            # the repeated context loading this optimizer exists to prevent.
            continuous_worker = (
                "以下步骤共享核心文件和运行状态" in text
                and "内部步骤" in text
            )
            if continuous_worker:
                continue
            if any(marker in normalized for marker in umbrella_markers):
                errors.append(
                    f"granularity_quality:{task_id} is an umbrella task; "
                    "split independent features into separately verifiable tasks"
                )
                continue
            for match in range_pattern.finditer(text):
                start, end = int(match.group(1)), int(match.group(2))
                if abs(end - start) + 1 >= 3:
                    errors.append(
                        f"granularity_quality:{task_id} combines {match.group(0)}; "
                        "split the regions/scenes into focused implementation tasks"
                    )
                    break
        return errors

    @classmethod
    def _optimize_plan(cls, plan_data: dict, complexity: str = "normal"):
        """Collapse only small plans and keep task text referentially sound."""
        if complexity != "simple" or not cls._can_collapse_simple_plan(plan_data):
            return False

        original = copy.deepcopy(plan_data)
        tasks = copy.deepcopy(plan_data.get("tasks") or [])
        coding = [task for task in tasks if task.get("type", "coding") == "coding"]
        if not coding:
            return False

        analysis = [task for task in tasks if task.get("type") == "analysis"]
        primary = coding[0]
        collapsed_ids = {
            str(task.get("id")) for task in analysis + coding if task.get("id")
        }
        replacement_id = str(primary.get("id") or "T001")
        reference_map = {
            task_id.upper(): replacement_id for task_id in collapsed_ids
        }
        analysis_ids = {
            str(task.get("id")).upper() for task in analysis if task.get("id")
        }

        preflight = []
        for task in analysis:
            text = cls._analysis_as_preflight(task.get("description"))
            text = cls._rewrite_task_references(
                text, reference_map, replacement_id, analysis_ids
            )
            if text and text not in preflight:
                preflight.append(text)

        implementation = []
        for task in coding:
            text = cls._rewrite_task_references(
                task.get("description"), reference_map,
                replacement_id, analysis_ids,
            )
            if text and text not in implementation:
                implementation.append(text)

        description_sections = []
        if preflight:
            description_sections.append(
                "【本任务前置检查】\n" + "\n".join(preflight)
            )
        if implementation:
            if preflight:
                implementation.insert(
                    0,
                    "完成前置检查后，必须继续执行以下实现要求并产生所需文件修改。",
                )
            description_sections.append(
                "【实现要求】\n" + "\n".join(implementation)
            )

        primary["description"] = "\n\n".join(description_sections)
        primary["title"] = str(primary.get("title") or "完成目标修改")
        primary["allowed_paths"] = list(dict.fromkeys(
            path for task in analysis + coding
            for path in (task.get("allowed_paths") or [])
        )) or ["*"]
        primary["dependencies"] = []
        primary["acceptance_command"] = next((
            str(task.get("acceptance_command") or "")
            for task in coding if task.get("acceptance_command")
        ), "")

        retained = [primary]
        validation_kept = False
        for task in tasks:
            if task in analysis or task in coding or task.get("type") == "review":
                continue
            if task.get("type") == "testing":
                text = (
                    f"{task.get('title', '')} {task.get('description', '')}"
                ).lower()
                authoring = any(marker in text for marker in (
                    "write test", "add test", "create test", "update test",
                    "编写测试", "新增测试", "添加测试", "补充测试", "测试用例",
                ))
                if validation_kept and not authoring:
                    continue
                validation_kept = validation_kept or not authoring
            dependencies = []
            for dependency in task.get("dependencies") or []:
                mapped = (
                    replacement_id
                    if str(dependency) in collapsed_ids
                    else dependency
                )
                if mapped != task.get("id") and mapped not in dependencies:
                    dependencies.append(mapped)
            if not dependencies and task.get("type") == "testing":
                dependencies = [replacement_id]
            task["dependencies"] = dependencies
            for field in ("title", "description"):
                task[field] = cls._rewrite_task_references(
                    task.get(field), reference_map,
                    str(task.get("id") or ""), analysis_ids,
                )
            retained.append(task)

        plan_data["tasks"] = retained
        plan_data["summary"] = cls._rewrite_task_references(
            str(plan_data.get("summary") or ""),
            reference_map, "", analysis_ids,
        ) + f"（简单任务已收敛为 {len(retained)} 个步骤）"

        # A deterministic optimizer must never make a model plan less valid.
        # If an unusual text pattern still leaves a dangling/self reference,
        # restore the original tasks instead of persisting a poisoned plan.
        integrity_errors = PolicyEngine().check_task_plan(plan_data, {})
        if integrity_errors:
            logger.warning(
                "Simple-plan optimization rolled back: %s",
                integrity_errors[0],
            )
            plan_data.clear()
            plan_data.update(original)
            return False
        return True

    @staticmethod
    def _is_broad_plan_path(path: str) -> bool:
        normalized = str(path or "").replace("\\", "/").strip().lstrip("./")
        return not normalized or any(marker in normalized for marker in ("*", "?", "["))

    @classmethod
    def _can_collapse_simple_plan(cls, plan_data: dict) -> bool:
        """Return whether merging tasks is smaller and safer than preserving them."""
        tasks = list(plan_data.get("tasks") or [])
        coding = [task for task in tasks if task.get("type", "coding") == "coding"]
        analysis = [task for task in tasks if task.get("type") == "analysis"]
        if not coding or len(coding) > 2 or len(analysis) > 1 or len(tasks) > 5:
            return False

        collapsed = analysis + coding
        collapsed_ids = {
            str(task.get("id")) for task in collapsed if task.get("id")
        }
        if any(
            str(dependency) not in collapsed_ids
            for task in collapsed
            for dependency in (task.get("dependencies") or [])
        ):
            return False

        paths = list(dict.fromkeys(
            str(path) for task in collapsed
            for path in (task.get("allowed_paths") or []) if str(path).strip()
        ))
        if not paths or len(paths) > 3 or any(cls._is_broad_plan_path(path) for path in paths):
            return False

        description_size = sum(
            len(str(task.get("description") or "")) for task in collapsed
        )
        if description_size > 1800:
            return False

        commands = {
            str(task.get("acceptance_command") or "").strip()
            for task in coding if str(task.get("acceptance_command") or "").strip()
        }
        return len(commands) <= 1

    @classmethod
    def _merge_shared_context_tasks(cls, plan_data: dict,
                                    surface: dict | None = None) -> bool:
        """Merge implementation stages that would reload the same core files.

        A Planner task maps to one fresh Worker conversation.  Repeated coding
        stages over the same runtime files therefore cost more context without
        providing isolation.  Merge those stages deterministically, while
        preserving analysis-only deliverables, action boundaries, independent
        scopes, and conflicting acceptance commands.
        """
        original = copy.deepcopy(plan_data)
        tasks = copy.deepcopy(list(plan_data.get("tasks") or []))
        if len(tasks) < 2:
            return False

        task_by_id = {
            str(task.get("id") or ""): task for task in tasks if task.get("id")
        }
        order = {
            str(task.get("id") or ""): index for index, task in enumerate(tasks)
        }
        active_files = {
            str(path).replace("\\", "/").lstrip("./")
            for path in ((surface or {}).get("active_files") or [])
            if str(path).strip()
        }
        support_files = {
            str(path).replace("\\", "/").lstrip("./")
            for path in ((surface or {}).get("support_files") or [])
            if str(path).strip()
        }
        runtime_groups = [
            {
                str(path).replace("\\", "/").lstrip("./")
                for path in (group.get("files") or [])
                if str(path).strip()
            }
            for group in ((surface or {}).get("runtime_groups") or [])
            if isinstance(group, dict)
        ]
        if not runtime_groups and active_files:
            # Backward compatibility for checkpoints and tests created before
            # ProjectResolver recorded one closure per runtime entrypoint.
            entrypoints = list((surface or {}).get("entrypoints") or [])
            if len(entrypoints) <= 1:
                runtime_groups = [set(active_files)]

        parent = {task_id: task_id for task_id in task_by_id}
        group_commands = {
            task_id: ({str(task.get("acceptance_command") or "").strip()}
                      if str(task.get("acceptance_command") or "").strip()
                      else set())
            for task_id, task in task_by_id.items()
        }

        def find(task_id: str) -> str:
            while parent[task_id] != task_id:
                parent[task_id] = parent[parent[task_id]]
                task_id = parent[task_id]
            return task_id

        def union(left_id: str, right_id: str) -> bool:
            left_root, right_root = find(left_id), find(right_id)
            if left_root == right_root:
                return True
            merged_commands = (
                group_commands[left_root] | group_commands[right_root]
            )
            if len(merged_commands) > 1:
                return False
            if order[left_root] <= order[right_root]:
                parent[right_root] = left_root
                group_commands[left_root] = merged_commands
            else:
                parent[left_root] = right_root
                group_commands[right_root] = merged_commands
            return True

        def anchors(task: dict) -> set[str]:
            result: set[str] = set()
            support_names = {
                "package.json", "package-lock.json", "pnpm-lock.yaml",
                "yarn.lock", "pyproject.toml", "pytest.ini", "readme.md",
                "tsconfig.json", "vite.config.js", "vite.config.ts",
            }
            for raw_path in task.get("allowed_paths") or []:
                path = str(raw_path or "").replace("\\", "/").lstrip("./")
                if not path or cls._is_project_wide_plan_path(path):
                    continue
                if path in support_files or Path(path).name.lower() in support_names:
                    continue
                if any(character in path for character in "*?["):
                    matches = {
                        candidate for candidate in active_files
                        if fnmatch.fnmatch(candidate, path)
                    }
                    # A broad source glob is not a stable context boundary unless
                    # deterministic project resolution narrows it to a small set.
                    if 0 < len(matches) <= 12:
                        result.update(matches)
                    continue
                result.add(path)
            return result

        anchors_by_id = {
            task_id: anchors(task) for task_id, task in task_by_id.items()
        }

        context_keys = {
            task_id: re.sub(
                r"[^a-z0-9_.:/-]+", "-",
                str(task.get("context_key") or "").strip().lower(),
            ).strip("-")
            for task_id, task in task_by_id.items()
        }

        def depends_on(task: dict, target_id: str,
                       seen: set[str] | None = None) -> bool:
            """Return whether task belongs to target's serial execution chain."""
            seen = set(seen or ())
            task_id = str(task.get("id") or "")
            if not task_id or task_id in seen:
                return False
            seen.add(task_id)
            for dependency in task.get("dependencies") or []:
                dependency_id = str(dependency)
                if dependency_id == target_id:
                    return True
                dependency_task = task_by_id.get(dependency_id)
                if dependency_task and depends_on(
                    dependency_task, target_id, seen
                ):
                    return True
            return False

        def runtime_anchors(task_id: str) -> set[str]:
            return anchors_by_id[task_id].intersection(active_files)

        def shares_runtime_group(left_id: str, right_id: str) -> bool:
            left_runtime = runtime_anchors(left_id)
            right_runtime = runtime_anchors(right_id)
            return bool(
                left_runtime and right_runtime and any(
                    left_runtime.intersection(group)
                    and right_runtime.intersection(group)
                    for group in runtime_groups
                )
            )

        for left_index, left in enumerate(tasks):
            left_id = str(left.get("id") or "")
            left_type = str(left.get("type") or "coding")
            if left_type not in {"analysis", "coding"} or not left_id:
                continue
            for right in tasks[left_index + 1:]:
                right_id = str(right.get("id") or "")
                right_type = str(right.get("type") or "coding")
                if right_type not in {"analysis", "coding"} or not right_id:
                    continue
                if left_type == right_type == "analysis":
                    continue
                shared = anchors_by_id[left_id].intersection(
                    anchors_by_id[right_id]
                )
                same_declared_context = bool(
                    context_keys[left_id]
                    and context_keys[left_id] == context_keys[right_id]
                )
                serial_runtime_context = bool(
                    shares_runtime_group(left_id, right_id)
                    and (
                        depends_on(right, left_id)
                        or depends_on(left, right_id)
                    )
                )
                if not (
                    shared or same_declared_context or serial_runtime_context
                ):
                    continue
                # A prerequisite analysis can become the first part of the same
                # conversation.  Unrelated analysis reports remain standalone.
                if "analysis" in {left_type, right_type}:
                    analysis_id = left_id if left_type == "analysis" else right_id
                    coding = right if right_type == "coding" else left
                    if analysis_id not in {
                        str(item) for item in (coding.get("dependencies") or [])
                    }:
                        continue
                # A matching context_key is Planner's explicit declaration that
                # these stages need the same loaded code/runtime reasoning. When
                # it is absent, the deterministic fallback joins a serial chain
                # inside the resolved runtime closure, while acceptance-command
                # compatibility still protects genuinely separate runtimes.
                union(left_id, right_id)

        groups: dict[str, list[dict]] = {}
        for task in tasks:
            task_id = str(task.get("id") or "")
            if task_id:
                groups.setdefault(find(task_id), []).append(task)
        merge_groups = [
            sorted(group, key=lambda item: order[str(item.get("id"))])
            for group in groups.values()
            if len(group) > 1 and any(
                str(item.get("type") or "coding") == "coding" for item in group
            )
        ]
        if not merge_groups:
            return False

        old_to_primary: dict[str, str] = {}
        merged_by_primary: dict[str, dict] = {}
        removed_ids: set[str] = set()
        for group in merge_groups:
            coding_members = [
                item for item in group
                if str(item.get("type") or "coding") == "coding"
            ]
            primary = coding_members[0]
            primary_id = str(primary.get("id"))
            group_ids = {str(item.get("id")) for item in group}
            for task_id in group_ids:
                old_to_primary[task_id.upper()] = primary_id
                if task_id != primary_id:
                    removed_ids.add(task_id)

            sections = []
            for step_number, item in enumerate(group, start=1):
                title = str(item.get("title") or item.get("id") or "执行步骤")
                description = str(item.get("description") or "").strip()
                if str(item.get("type") or "coding") == "analysis":
                    description = cls._analysis_as_preflight(description)
                sections.append(
                    f"【内部步骤 {step_number} · {title}】\n{description}".strip()
                )
            primary["title"] = (
                str(primary.get("title") or "完成共享上下文实现")
                + "（连续执行）"
            )
            primary["description"] = (
                "以下步骤共享核心文件和运行状态，必须在同一 Worker 会话中"
                "连续完成；完成前置检查后继续实现，不要在步骤之间重新扫描项目。\n\n"
                + "\n\n".join(sections)
            )
            primary["type"] = "coding"
            primary["allowed_paths"] = list(dict.fromkeys(
                str(path) for item in group
                for path in (item.get("allowed_paths") or []) if str(path).strip()
            ))
            primary["skills"] = list(dict.fromkeys(
                str(skill) for item in group
                for skill in (item.get("skills") or []) if str(skill).strip()
            ))[:3]
            primary["acceptance_command"] = next((
                str(item.get("acceptance_command") or "").strip()
                for item in reversed(group)
                if str(item.get("acceptance_command") or "").strip()
            ), "")
            primary["dependencies"] = list(dict.fromkeys(
                str(dependency) for item in group
                for dependency in (item.get("dependencies") or [])
                if str(dependency) not in group_ids
            ))
            merged_by_primary[primary_id] = primary

        retained: list[dict] = []
        emitted: set[str] = set()
        for task in tasks:
            task_id = str(task.get("id") or "")
            mapped_id = old_to_primary.get(task_id.upper(), task_id)
            if task_id in removed_ids:
                continue
            if mapped_id in merged_by_primary:
                if mapped_id in emitted:
                    continue
                task = merged_by_primary[mapped_id]
                emitted.add(mapped_id)
            task["dependencies"] = list(dict.fromkeys(
                old_to_primary.get(str(dependency).upper(), str(dependency))
                for dependency in (task.get("dependencies") or [])
                if old_to_primary.get(str(dependency).upper(), str(dependency))
                != str(task.get("id") or "")
            ))
            for field in ("title", "description"):
                task[field] = cls._rewrite_task_references(
                    task.get(field), old_to_primary,
                    str(task.get("id") or ""), set(),
                )
            retained.append(task)

        plan_data["tasks"] = retained
        plan_data["summary"] = cls._rewrite_task_references(
            str(plan_data.get("summary") or ""), old_to_primary, "", set()
        ) + f"（共享上下文步骤已合并为 {len(retained)} 个 Worker 任务）"
        if PolicyEngine().check_task_plan(plan_data, {}):
            plan_data.clear()
            plan_data.update(original)
            return False
        return True

    @classmethod
    def _promote_complexity_from_plan(cls, complexity: str,
                                      plan_data: dict) -> str:
        """Correct an optimistic prompt-only classification using plan scope."""
        rank = {"simple": 0, "normal": 1, "complex": 2}
        current = complexity if complexity in rank else "normal"
        tasks = list(plan_data.get("tasks") or [])
        coding = [task for task in tasks if task.get("type", "coding") == "coding"]
        relevant = [
            task for task in tasks
            if task.get("type", "coding") in {"analysis", "coding", "testing"}
        ]
        description_size = sum(
            len(str(task.get("description") or "")) for task in relevant
        )
        broad_paths = sum(
            1 for task in relevant for path in (task.get("allowed_paths") or [])
            if cls._is_broad_plan_path(path)
        )

        target = current
        if (
            len(coding) >= 5
            or len(relevant) >= 8
            or description_size >= 7000
            or (len(coding) >= 4 and broad_paths)
        ):
            target = "complex"
        elif (
            len(coding) >= 3
            or description_size >= 2200
            or (len(coding) >= 2 and broad_paths)
        ):
            target = "normal"
        return target if rank[target] > rank[current] else current

    @staticmethod
    def _analysis_as_preflight(description) -> str:
        """Keep analysis useful without carrying a global no-edit prohibition."""
        text = str(description or "").strip()
        replacements = {
            "只读分析": "检查",
            "产出书面分析报告": "形成供本任务使用的检查结论",
            "不创建或修改任何项目文件": "",
            "不创建或修改项目文件": "",
            "不创建或修改任何文件": "",
            "不得创建或修改任何项目文件": "",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        text = re.sub(
            r"(?i)\b(?:do not|don't|without)\s+"
            r"(?:create|creating|modify|modifying|edit|editing|write|writing)"
            r"[^.;。；]*(?:[.;。；]|$)",
            "",
            text,
        )
        return re.sub(r"[，,]\s*[。.]", "。", text).strip(" ，,;；")

    @staticmethod
    def _rewrite_task_references(text, reference_map: dict[str, str],
                                 current_id: str,
                                 analysis_ids: set[str]) -> str:
        """Rewrite references to removed task IDs, including prose references."""
        value = str(text or "")
        for task_id in analysis_ids:
            escaped = re.escape(task_id)
            value = re.sub(
                rf"(?:依据|根据)\s*[`*]*{escaped}[`*]*\s*"
                rf"(?:的?\s*(?:报告|分析结果|分析结论|结论))?",
                "依据上述前置检查结论",
                value,
                flags=re.IGNORECASE,
            )
            value = re.sub(
                rf"(?i)\b(?:after|following|based\s+on)\s+[`*]*{escaped}[`*]*"
                rf"(?:'s)?(?:\s+(?:report|analysis|findings))?",
                "after the preflight checks above",
                value,
            )

        task_ref = re.compile(
            r"(?<![A-Za-z0-9_])(?:R\d{2}T\d{3,}|T\d{3,})(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )

        def replace(match):
            original = match.group(0)
            replacement = reference_map.get(original.upper())
            if not replacement:
                return original
            if current_id and replacement.upper() == current_id.upper():
                return "本任务前置步骤"
            return replacement

        return task_ref.sub(replace, value)

    @classmethod
    def _serialize_overlapping_tasks(cls, plan_data: dict,
                                     surface: dict | None = None):
        """Serialize task pairs unless their file/runtime scopes are disjoint."""
        tasks = plan_data.get("tasks", [])
        active = {
            str(path).replace("\\", "/").lstrip("./")
            for path in ((surface or {}).get("active_files") or [])
        }
        for index, task in enumerate(tasks):
            dependencies = list(task.get("dependencies") or [])
            current_paths = task.get("allowed_paths") or []
            for previous in tasks[:index]:
                previous_id = previous.get("id")
                if not previous_id or previous_id in dependencies:
                    continue
                previous_paths = previous.get("allowed_paths") or []
                broad_or_unknown = (
                    not current_paths or not previous_paths
                    or any(cls._is_broad_plan_path(path) for path in current_paths)
                    or any(cls._is_broad_plan_path(path) for path in previous_paths)
                )
                analysis_feeds_implementation = (
                    str(previous.get("type") or "") in {"analysis", "review"}
                    and str(task.get("type") or "coding") in {
                        "coding", "testing", "action",
                    }
                )
                current_concrete = {
                    str(path).replace("\\", "/").lstrip("./")
                    for path in current_paths
                    if not cls._is_broad_plan_path(path)
                }
                previous_concrete = {
                    str(path).replace("\\", "/").lstrip("./")
                    for path in previous_paths
                    if not cls._is_broad_plan_path(path)
                }
                shared_runtime_closure = bool(
                    active
                    and current_concrete.intersection(active)
                    and previous_concrete.intersection(active)
                )
                path_overlap = any(
                    cls._path_patterns_overlap(left, right)
                    for left in current_paths
                    for right in previous_paths
                )
                if (
                    broad_or_unknown or analysis_feeds_implementation
                    or shared_runtime_closure or path_overlap
                ):
                    dependencies.append(previous_id)
            task["dependencies"] = dependencies

    @staticmethod
    def _prune_transitive_dependencies(plan_data: dict):
        """Keep only direct DAG prerequisites after path serialization."""
        tasks = plan_data.get("tasks", [])
        dependencies_by_id = {
            task.get("id"): list(task.get("dependencies") or [])
            for task in tasks if task.get("id")
        }

        def reaches(start: str, target: str, seen: set[str] | None = None) -> bool:
            if start == target:
                return True
            seen = set() if seen is None else seen
            if start in seen:
                return False
            seen.add(start)
            return any(
                dependency == target or reaches(dependency, target, set(seen))
                for dependency in dependencies_by_id.get(start, [])
            )

        for task in tasks:
            dependencies = list(dict.fromkeys(task.get("dependencies") or []))
            task["dependencies"] = [
                dependency
                for dependency in dependencies
                if not any(
                    other != dependency and reaches(other, dependency)
                    for other in dependencies
                )
            ]

    @staticmethod
    def _path_patterns_overlap(left: str, right: str) -> bool:
        import fnmatch

        left = (left or "").replace("\\", "/")
        right = (right or "").replace("\\", "/")
        if left.startswith("./"):
            left = left[2:]
        if right.startswith("./"):
            right = right[2:]
        if not left or not right:
            return False
        if left in {"*", "**", "**/*"} or right in {"*", "**", "**/*"}:
            return True
        if left == right or fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left):
            return True
        left_prefix = left.split("*")[0].rstrip("/")
        right_prefix = right.split("*")[0].rstrip("/")
        return bool(
            left_prefix and right_prefix
            and (left_prefix.startswith(right_prefix + "/")
                 or right_prefix.startswith(left_prefix + "/"))
        )

    @staticmethod
    def _estimate_task_budget(task, project_root: str, base_turns: int,
                              base_exploration: int, mode: str = "auto") -> dict:
        """Size a Worker budget from the task and the files it will actually touch."""
        root = Path(project_root).resolve()
        files: set[Path] = set()
        ignored_parts = {".git", ".ai", "node_modules", ".venv", "venv"}
        for pattern in task.allowed_paths or []:
            normalized = (pattern or "").replace("\\", "/").lstrip("./")
            if not normalized:
                continue
            try:
                candidates = root.glob(normalized)
                for candidate in candidates:
                    if len(files) >= 30:
                        break
                    if (
                        candidate.is_file()
                        and not ignored_parts.intersection(candidate.relative_to(root).parts)
                    ):
                        files.add(candidate)
            except (OSError, ValueError):
                continue

        text_suffixes = {
            ".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".scss",
            ".vue", ".json", ".md", ".toml", ".yaml", ".yml", ".java",
            ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
        }
        total_lines = 0
        for path in files:
            try:
                if path.suffix.lower() in text_suffixes and path.stat().st_size <= 2 * 1024 * 1024:
                    with path.open("r", encoding="utf-8", errors="replace") as handle:
                        total_lines += sum(1 for _ in handle)
            except OSError:
                continue

        task_type = (task.task_type or "coding").lower()
        dependency_count = len(task.dependencies or [])
        description = task.description or ""
        behavior_count = sum(description.count(mark) for mark in ("；", ";", "。", "."))
        reasons = [f"base={base_turns}"]

        if task_type in {"analysis", "review"}:
            # Read-only reports need a final model turn after search/read calls.
            # A fast preset may set the coding budget to 8, which is too short
            # for a two-file audit and previously caused false dependency failure.
            turns = max(base_turns, 36 if total_lines >= 600 else 30)
            minimum_exploration = (
                48 if total_lines >= 400 or len(files) >= 3 else 36
            )
            exploration = max(base_exploration, minimum_exploration)
            reasons.append("read-only report")
        elif task_type in {"testing", "review"}:
            turns = min(base_turns, 54 if len(files) > 2 else 42)
            exploration = min(max(base_exploration, 9), max(9, turns // 2))
            reasons.append("validation task")
        else:
            turns = base_turns
            # Coding tasks commonly need several paginated reads across HTML,
            # CSS, and JS before a safe edit. This is only a convergence reminder,
            # so begin with enough room for a real cross-file inspection.
            exploration = max(base_exploration, 36)
            if total_lines >= 400:
                turns += 18
                exploration += 6
                reasons.append(f"existing_code={total_lines} lines")
            if total_lines >= 1000:
                turns += 18
                exploration += 3
                reasons.append("large codebase slice")
            if len(files) >= 2:
                turns += 9
                reasons.append(f"files={len(files)}")
            if len(files) >= 5:
                turns += 9
                exploration += 3
            if dependency_count >= 4:
                turns += 9
                reasons.append(f"dependencies={dependency_count}")
            if behavior_count >= 4 or len(description) >= 320:
                turns += 9
                reasons.append("multiple behaviors")

        cap = 60 if mode == "fast" else 150
        turns = max(18, min(cap, turns))
        # This value counts individual tool operations, not model turns. Parallel
        # reads and pagination can legitimately use several operations in one turn,
        # so keep the reminder generous and never turn it into a hard read limit.
        exploration = max(18, min(120, exploration, max(18, turns * 2)))
        estimated_input_per_turn = min(
            40_000,
            10_000
            + min(20_000, total_lines * 8)
            + min(5_000, len(files) * 1_000)
            + min(5_000, len(description) * 8),
        )
        processing_input_budget = max(
            300_000, turns * estimated_input_per_turn
        )
        finalization_reserve = max(
            180_000, math.ceil(processing_input_budget * 0.20)
        )
        input_budget = min(
            20_000_000, processing_input_budget + finalization_reserve
        )
        return {
            "max_turns": turns,
            "exploration_turns": exploration,
            "estimated_input_per_turn": estimated_input_per_turn,
            "processing_input_budget": processing_input_budget,
            "finalization_reserve": finalization_reserve,
            "input_budget": input_budget,
            "max_auto_input_budget": 50_000_000,
            "output_budget": max(120_000, turns * 4_000),
            "api_call_budget": turns + 16,
            "existing_files": len(files),
            "total_lines": total_lines,
            "reason": ", ".join(reasons),
        }

    def _create_direct_plan(self, job, repos, proj_config=None):
        """Persist a direct task for a deliberately disabled Planner phase."""
        plan_data = self._direct_plan_data(job, repos, proj_config)
        self._assign_plan_skills(plan_data)
        repos["plan"].create(
            job_id=job.id,
            summary=plan_data["summary"],
            raw_output=plan_data,
        )
        self._create_tasks_from_plan(job, repos, plan_data)

    async def _run_execution(self, job, repos, job_baseline: dict | None = None,
                             proj_config: ProjectAgentConfig | None = None,
                             complexity: str = "normal",
                             task_ids: set[str] | None = None,
                             repair_round: int = 0,
                             resume_source_job_id: str = "") -> dict:
        """Execute tasks in parallel using DAG scheduler (V4: worktree isolation)."""
        if self._is_cancelled(job.job_id, job, repos):
            return {"status": "cancelled", "reason": "任务已由用户停止"}
        self.state_machine.transition(job.job_id, JobState.EXECUTING)
        repos["job"].update_status(job.job_id, "executing")
        await self.event_bus.publish(
            "job_executing", job_id=job.job_id,
            repair_round=repair_round,
        )
        if self._is_cancelled(job.job_id, job, repos):
            return {"status": "cancelled", "reason": "任务已由用户停止"}

        worker = self.get_agent("worker")
        if not worker:
            logger.warning("No worker agent registered")
            return {"status": "failed", "reason": "未注册执行者"}

        # Collect all tasks as dicts for the DAG scheduler
        all_job_tasks = repos["task"].list_by_job(job.id)
        all_tasks = all_job_tasks
        if task_ids is not None:
            all_tasks = [task for task in all_tasks if task.task_id in task_ids]
        if not all_tasks:
            return {"status": "failed", "reason": "没有可执行的任务"}

        # Reserve the complete plan before parallel tasks start. Workers cannot
        # consume the Reviewer and first repair round's finishing capacity.
        document_root = job.project.root_path if job.project else "."
        base_turns = (
            proj_config.get_worker_turns(complexity)
            if proj_config else getattr(worker, "max_turns", 72)
        )
        base_exploration = (
            proj_config.get_exploration_turns(complexity)
            if proj_config else getattr(worker, "max_exploration_turns", 12)
        )
        mode = proj_config.mode if proj_config else "auto"
        planned_task_budgets: dict[str, dict] = {}
        document_task_count = 0
        for planned_task in all_tasks:
            profile = self._document_task_profile(
                planned_task, document_root,
                getattr(job, "user_request", ""),
            )
            if profile:
                document_task_count += 1
                planned_task_budgets[planned_task.task_id] = {
                    **profile, "document_profile": profile,
                    "max_auto_input_budget": (
                        self.model_router.cost_engine.get_budget(
                            job.job_id
                        ).max_auto_input_tokens
                    ),
                }
            else:
                planned_task_budgets[planned_task.task_id] = (
                    self._estimate_task_budget(
                        planned_task, document_root, base_turns,
                        base_exploration, mode,
                    )
                )

        combined_input_budget = sum(
            int(profile.get("input_budget", 0) or 0)
            for profile in planned_task_budgets.values()
        )
        combined_api_budget = sum(
            int(profile.get("api_call_budget", 0) or 0)
            for profile in planned_task_budgets.values()
        )
        combined_output_budget = sum(
            int(profile.get("output_budget", 0) or 0)
            for profile in planned_task_budgets.values()
        )
        workflow_budget = self.model_router.cost_engine.reserve_workflow_budget(
            job.job_id,
            combined_input_budget,
            required_api_calls=combined_api_budget,
            required_output_tokens=combined_output_budget,
            reservation_name=f"execution:{repair_round}",
        )
        await self.event_bus.publish(
            "workflow_budget_reserved",
            job_id=job.job_id,
            tasks=len(planned_task_budgets),
            document_tasks=document_task_count,
            task_input_budget=combined_input_budget,
            job_input_budget=workflow_budget.max_input_tokens,
            job_total_budget=workflow_budget.max_total_tokens,
            job_api_call_budget=workflow_budget.max_api_calls,
            budget=self.model_router.cost_engine.get_budget_snapshot(job.job_id),
        )

        selected_task_ids = {task.task_id for task in all_tasks}
        # Keep progress anchored to the original plan during a checkpoint
        # continuation.  A subset such as T004-T010 must still be shown as
        # steps 4-10 of 10, not renumbered to 1-7.
        task_position_by_id, task_total = self._task_progress_layout(
            all_job_tasks, all_tasks, repair_round
        )
        task_dicts = []
        for t in all_tasks:
            task_dicts.append({
                "task_id": t.task_id,
                "title": t.title,
                "description": t.description,
                "type": t.task_type,
                # Dependencies already completed before this checkpoint are
                # satisfied; keep only dependencies participating in this run.
                "dependencies": [
                    dependency for dependency in (t.dependencies or [])
                    if dependency in selected_task_ids
                ],
                "allowed_paths": t.allowed_paths or [],
                "skills": t.skills or [],
                "acceptance_command": t.acceptance_command or "",
                "_db_task": t,
            })
        task_data_by_id = {item["task_id"]: item for item in task_dicts}
        completed_task_results: dict[str, dict] = {}

        # Define runner for each task (with worktree isolation)
        async def run_single_task(task_id: str, task_data: dict):
            t = task_data["_db_task"]
            nonlocal repos, job, worker

            # Event handlers persist checkpoints through their own SQLAlchemy
            # sessions. Refresh here so a later checklist item always receives
            # the newest fixed context instead of the Job object captured when
            # the execution phase started.
            repos["_session"].expire(job, ["last_checkpoint"])
            latest_checkpoint = dict(job.last_checkpoint or {})
            latest_session = normalize_session(
                latest_checkpoint.get("execution_session"),
                session_id=(job.execution_session_id or job.job_id),
                goal=job.user_request,
            )
            t.status = "running"
            update_checklist(latest_session, all_job_tasks)
            latest_session["current_step"] = task_id
            latest_session["next_action"] = f"Execute {task_id}: {t.title}"
            latest_checkpoint["execution_session"] = latest_session
            repos["job"].update_checkpoint(job.job_id, latest_checkpoint)
            job.last_checkpoint = latest_checkpoint

            project_surface = dict(
                getattr(job, "_rockcore_project_surface", None)
                or (getattr(job, "last_checkpoint", None) or {}).get(
                    "project_surface"
                )
                or {}
            )
            t._rockcore_project_surface = project_surface
            t._rockcore_fixed_context = render_fixed_context(latest_session)
            shared_context = self._execution_continuation_context(
                job, t, completed_task_results
            )
            if shared_context:
                existing_context = str(
                    getattr(t, "_rockcore_initial_recovery_context", "") or ""
                )
                t._rockcore_initial_recovery_context = "\n\n".join(
                    item for item in (existing_context, shared_context) if item
                )

            analysis_reports = self._collect_analysis_dependency_reports(
                task_id, task_data_by_id, completed_task_results
            )
            if analysis_reports:
                await self._ground_task_in_analysis(
                    t, job, repos, analysis_reports
                )
                task_data["description"] = t.description
                task_data["allowed_paths"] = t.allowed_paths or []
                task_data["acceptance_command"] = t.acceptance_command or ""

            if self.test_manager.should_validate_locally(t):
                repos["task"].update_status_by_pk(t.id, "running")
                await self.event_bus.publish(
                    "task_running", job_id=job.job_id, task_id=task_id, title=t.title
                )
                result = await self.test_manager.run_tests(
                    t, repos, self.event_bus,
                    baseline_snapshot=job_baseline,
                    project_root=job.project.root_path if job.project else ".",
                )
                if result.get("status") != "passed":
                    reason = str(
                        result.get("output")
                        or result.get("error")
                        or "Local validation did not pass"
                    )
                    continuation = {
                        "status": "needs_continuation",
                        "error": reason,
                        "failure_stage": "validation_continuation",
                        "checkpoint": {"validation": result},
                    }
                    repos["task"].update_status_by_pk(t.id, "interrupted")
                    self._checkpoint_task(
                        repos, job, t, status="interrupted",
                        result=continuation, error=reason,
                    )
                    await self.event_bus.publish(
                        "task_needs_continuation", job_id=job.job_id,
                        task_id=task_id, reason=reason,
                        failure_stage="validation_continuation",
                        checkpoint=continuation["checkpoint"],
                    )
                    return continuation
                repos["task"].update_status_by_pk(t.id, "done")
                self._checkpoint_task(
                    repos, job, t, status="done", result=result
                )
                await self.event_bus.publish(
                    "task_done", job_id=job.job_id, task_id=task_id, result=result
                )
                completed_task_results[task_id] = result
                return result

            # Read-only tasks already receive a non-mutating tool schema. Avoid
            # paying for Git worktree creation/snapshot integration when their
            # required deliverable is an in-app report rather than repository
            # changes.
            is_read_only_task = t.task_type in {"analysis", "review"}
            has_worktree = False
            if self.merge_manager and not is_read_only_task:
                try:
                    wt_result = await self.merge_manager.create_task_worktree(
                        task_id, job.job_id,
                        source_job_id=(
                            resume_source_job_id
                            or str(job.source_job_id or "")
                        ),
                    )
                except TypeError:
                    wt_result = await self.merge_manager.create_task_worktree(
                        task_id, job.job_id
                    )
                if wt_result.get("status") != "created":
                    phase = str(wt_result.get("phase") or "worktree_create")
                    detail = str(
                        wt_result.get("error") or "Unknown Git worktree error"
                    )
                    error = f"Git isolation failed during {phase}: {detail}"
                    logger.warning("Task %s: %s", task_id, error)
                    needs_user = self._is_user_action_required(error)
                    outcome = {
                        "status": (
                            "needs_user_action" if needs_user else "failed"
                        ),
                        "error": error,
                        "failure_stage": "worktree_create",
                    }
                    if needs_user:
                        outcome["checkpoint"] = {"integration": wt_result}
                    task_status = "needs_attention" if needs_user else "failed"
                    repos["task"].update_status_by_pk(t.id, task_status)
                    self._checkpoint_task(
                        repos, job, t, status=task_status,
                        result=outcome, error=error,
                    )
                    await self.event_bus.publish(
                        (
                            "task_needs_user_action"
                            if needs_user else "task_failed"
                        ),
                        job_id=job.job_id,
                        task_id=task_id, reason=error, error=error,
                        failure_stage="worktree_create",
                        checkpoint=outcome.get("checkpoint", {}),
                    )
                    return outcome
                else:
                    has_worktree = True
                    task_worktree_root = wt_result.get("path", job.project.root_path if job.project else ".")
            else:
                task_worktree_root = job.project.root_path if job.project else "."
            task_baseline = self.test_manager.capture_snapshot(task_worktree_root)
            # Files restored from a previous preserved worktree are unfinished
            # task output, not a new baseline. Keep them visible to change
            # detection so they are validated and integrated even when the
            # continuation only needs to verify and finalize them.
            resumed_paths = (
                wt_result.get("resumed_files") or []
            ) if has_worktree else []
            for resumed_path in resumed_paths:
                task_baseline.pop(str(resumed_path).replace("\\", "/"), None)
            task_worker = worker.scoped_to(task_worktree_root)
            live_change_summary = {
                "changed": [], "files_changed": 0,
                "additions": 0, "deletions": 0,
            }
            after_write_hook_failure: dict = {}

            async def publish_worker_progress(progress: dict):
                nonlocal live_change_summary, after_write_hook_failure
                phase = str(progress.get("phase") or "正在执行")
                if phase in {"正在修改文件", "正在执行验证"}:
                    live_change_summary = self.test_manager.change_summary(
                        task_worktree_root, task_baseline
                    )
                await self.event_bus.publish(
                    "task_progress",
                    job_id=job.job_id,
                    task_id=task_id,
                    task_index=task_position_by_id.get(task_id, 1),
                    task_total=task_total,
                    phase=phase,
                    tool=progress.get("tool", ""),
                    path=progress.get("path", ""),
                    turn=progress.get("turn", 0),
                    max_turns=progress.get("max_turns", 0),
                    changes=live_change_summary,
                )
                event_kind = progress.get("event_kind")
                if event_kind == "tool_started":
                    await self.event_bus.publish(
                        "worker_tool_started",
                        job_id=job.job_id,
                        task_id=task_id,
                        task_index=task_position_by_id.get(task_id, 1),
                        task_total=task_total,
                        phase=phase,
                        tool=progress.get("tool", ""),
                        path=progress.get("path", ""),
                        turn=progress.get("turn", 0),
                        max_turns=progress.get("max_turns", 0),
                        status="started",
                        arguments=progress.get("arguments") or {},
                    )
                elif event_kind == "tool_completed":
                    await self.event_bus.publish(
                        "worker_tool_completed",
                        job_id=job.job_id,
                        task_id=task_id,
                        task_index=task_position_by_id.get(task_id, 1),
                        task_total=task_total,
                        phase=phase,
                        tool=progress.get("tool", ""),
                        path=progress.get("path", ""),
                        turn=progress.get("turn", 0),
                        max_turns=progress.get("max_turns", 0),
                        status=progress.get("status", ""),
                        arguments=progress.get("arguments") or {},
                        result=progress.get("result") or {},
                        duration_ms=progress.get("duration_ms", 0),
                    )
                    if (
                        progress.get("tool") in {
                            "write_file", "apply_patch", "insert_before",
                            "insert_after", "write_docx", "write_pptx",
                            "write_pdf", "promote_artifact",
                        }
                        and str(progress.get("status") or "")
                        not in {"error", "rejected", "failed"}
                    ):
                        hook_results = await self._run_project_hooks(
                            proj_config, "after_write", job_id=job.job_id,
                            project_root=task_worktree_root, task_id=task_id,
                        )
                        failed_hook = next((
                            item for item in hook_results
                            if item.get("status") != "passed"
                        ), None)
                        if failed_hook:
                            after_write_hook_failure = dict(failed_hook)

            t._rockcore_progress_callback = publish_worker_progress
            t._rockcore_instruction_source = (
                lambda job_id=job.job_id: self._drain_worker_instructions(job_id)
            )
            base_exploration = (
                proj_config.get_exploration_turns(complexity)
                if proj_config else getattr(task_worker, "max_exploration_turns", 12)
            )
            budget = dict(
                planned_task_budgets.get(task_id)
                or self._estimate_task_budget(
                    t,
                    task_worktree_root,
                    getattr(task_worker, "max_turns", 72),
                    base_exploration,
                    proj_config.mode if proj_config else "auto",
                )
            )
            task_worker.max_turns = budget["max_turns"]
            task_worker.max_exploration_turns = budget["exploration_turns"]
            document_profile = budget.get("document_profile")
            if document_profile:
                task_worker.max_turns = max(
                    task_worker.max_turns, document_profile["max_turns"]
                )
                task_worker.max_exploration_turns = max(
                    task_worker.max_exploration_turns,
                    document_profile["exploration_turns"],
                )
                t._rockcore_input_budget = document_profile["input_budget"]
                t._rockcore_document_profile = dict(document_profile)
                t._rockcore_artifact_manifest = {
                    "kind": "pdf",
                    "inputs": list(document_profile.get("source_pdfs") or []),
                    "outputs": list(document_profile.get("output_pdfs") or []),
                    "require_changed_output": bool(
                        document_profile.get("requires_pdf_output")
                    ),
                    "require_extractable_text": True,
                    "final_outputs": list(
                        document_profile.get("final_outputs") or []
                    ),
                }
                document_job_budget = (
                    self.model_router.cost_engine.reserve_document_budget(
                        job.job_id,
                        t._rockcore_input_budget,
                        required_api_calls=document_profile["api_call_budget"],
                        required_output_tokens=document_profile["output_budget"],
                    )
                )
                await self.event_bus.publish(
                    "document_budget_reserved",
                    job_id=job.job_id,
                    task_id=task_id,
                    document_level=document_profile["level"],
                    task_input_budget=t._rockcore_input_budget,
                    job_input_budget=document_job_budget.max_input_tokens,
                    job_total_budget=document_job_budget.max_total_tokens,
                    job_api_call_budget=document_job_budget.max_api_calls,
                    pdf_count=document_profile["pdf_count"],
                    pdf_bytes=document_profile["pdf_bytes"],
                    pdf_pages=document_profile["pdf_pages"],
                    estimated_pages=document_profile["estimated_pages"],
                    page_batches=document_profile["page_batches"],
                )
            else:
                t._rockcore_document_profile = None
                t._rockcore_artifact_manifest = None
                t._rockcore_input_budget = int(budget["input_budget"])
            concrete_task_outputs = [
                str(path).replace("\\", "/").lstrip("./")
                for path in (t.allowed_paths or [])
                if path and "*" not in str(path)
            ]
            artifact_manifest = dict(
                getattr(t, "_rockcore_artifact_manifest", None) or {}
            )
            final_outputs = (
                list(artifact_manifest.get("final_outputs") or [])
                if document_profile else concrete_task_outputs
            )
            runtime_broker = getattr(task_worker, "tool_broker", None)
            if runtime_broker and hasattr(runtime_broker, "configure_task_runtime"):
                runtime_checkpoint = runtime_broker.configure_task_runtime(
                    job.project.root_path if job.project else task_worktree_root,
                    job.job_id,
                    task_id,
                    final_outputs=final_outputs,
                    input_paths=list(artifact_manifest.get("inputs") or []),
                    require_declared_outputs=bool(document_profile),
                    source_job_id=(
                        resume_source_job_id
                        or str(job.source_job_id or "")
                    ),
                )
                t._rockcore_runtime_checkpoint = runtime_checkpoint
            t._rockcore_max_auto_input_budget = int(
                self.model_router.cost_engine.get_budget(
                    job.job_id
                ).max_auto_input_tokens
            )
            t._rockcore_finalization_reserve = int(
                budget.get("finalization_reserve", 180_000)
            )
            t._rockcore_retry_count = (
                proj_config.worker.retry_count if proj_config else MAX_FLASH_RETRY
            )
            t._rockcore_emergency_after_failures = (
                proj_config.worker.emergency_after_failures
                if proj_config else 3
            )
            t._rockcore_fallback_provider = (
                proj_config.worker.fallback_provider
                if proj_config else "kimi"
            )
            t._rockcore_fallback_model = (
                proj_config.worker.fallback_model
                if proj_config else "kimi-k2.7-code"
            )
            t._rockcore_emergency_enabled = (
                proj_config.emergency_coder.enabled if proj_config else True
            )
            t._rockcore_auto_repair = (
                proj_config.auto_repair if proj_config else True
            )
            logger.info(
                f"Task {task_id} budget: turns={task_worker.max_turns}, "
                f"exploration={task_worker.max_exploration_turns} "
                f"({budget['reason']})"
            )

            repos["task"].update_status_by_pk(t.id, "running")
            await self.event_bus.publish(
                "task_running", job_id=job.job_id,
                task_id=task_id, title=t.title,
                task_index=task_position_by_id.get(task_id, 1),
                task_total=task_total,
                task_type=t.task_type,
                max_turns=task_worker.max_turns,
                exploration_limit=task_worker.max_exploration_turns,
                input_token_budget=t._rockcore_input_budget,
                budget_reason=budget["reason"],
                skills=t.skills or [],
            )

            # A preserved continuation may already contain everything the task
            # needed before an external stop (for example provider balance).
            # Validate that artifact first; do not restart the model merely to
            # rediscover or rewrite existing checkpoint files.
            result, resumed_validation = await self._validate_resumed_artifact(
                t, job, repos, task_worktree_root, task_baseline, resumed_paths,
            )

            if result is None:
                # The checkpoint was absent or incomplete. Continue this same
                # task with focused context, rather than presenting it as a new
                # plan step or rerunning completed upstream tasks.
                if resumed_validation:
                    resumed_context = (
                        "A saved checkpoint artifact was validated before retry. "
                        "Reuse the saved reads and edit only the concrete failing "
                        "parts; do not scan the project again.\nValidation:\n"
                        + str(
                            resumed_validation.get("output")
                            or resumed_validation.get("error") or ""
                        )[:4000]
                        + "\nSaved checkpoint evidence:\n"
                        + self._worker_retry_evidence(
                            dict(t.result_data or {})
                        )
                    )
                    t._rockcore_initial_recovery_context = "\n\n".join(
                        item for item in (
                            str(getattr(
                                t, "_rockcore_initial_recovery_context", ""
                            ) or ""),
                            resumed_context,
                        ) if item
                    )
                result = await self._execute_single_task_with_escalation(
                    t, job, repos, task_worker, task_worktree_root,
                    baseline_snapshot=task_baseline,
                )
            integration_result = None
            runtime_relocation: list[dict] = []
            if runtime_broker and hasattr(
                runtime_broker, "relocate_task_intermediates"
            ):
                pre_relocation_changes = self.test_manager.snapshot_diff(
                    task_worktree_root, task_baseline
                )
                runtime_relocation = runtime_broker.relocate_task_intermediates(
                    pre_relocation_changes.get("added") or []
                )
                t._rockcore_runtime_checkpoint = (
                    runtime_broker.task_runtime_checkpoint()
                )
                if runtime_relocation:
                    await self.event_bus.publish(
                        "task_intermediates_relocated",
                        job_id=job.job_id,
                        task_id=task_id,
                        files=runtime_relocation,
                    )

            if result and result.get("status") == "needs_user_action":
                reason = str(result.get("error") or "需要用户完成必要操作")
                repos["task"].update_status_by_pk(t.id, "needs_attention")
                self._checkpoint_task(
                    repos, job, t, status="needs_attention", result=result,
                    error=reason,
                )
                await self.event_bus.publish(
                    "task_needs_user_action",
                    job_id=job.job_id,
                    task_id=task_id,
                    reason=reason,
                    failure_stage=result.get(
                        "failure_stage", "user_action_required"
                    ),
                    checkpoint=result.get("checkpoint", {}),
                    worktree_path=(
                        task_worktree_root if has_worktree else ""
                    ),
                )
                if has_worktree:
                    preserve = getattr(
                        self.merge_manager, "preserve_worktree", None
                    )
                    if callable(preserve):
                        preserve(task_id)
                return result

            if result and result.get("status") == "needs_continuation":
                reason = str(result.get("error") or "任务已保存，等待继续")
                # A worker turn/budget limit is actionable by the user: they
                # can increase the project budget, add credits, or switch the
                # provider. Keep the checkpoint visible as an attention item
                # so the existing resume button continues this exact task.
                requires_confirmation = self._checkpoint_requires_confirmation(
                    result
                )
                task_status = (
                    "needs_attention" if requires_confirmation else "interrupted"
                )
                repos["task"].update_status_by_pk(t.id, task_status)
                self._checkpoint_task(
                    repos, job, t, status=task_status, result=result,
                    error=reason,
                )
                await self.event_bus.publish(
                    "task_needs_user_action"
                    if requires_confirmation else "task_needs_continuation",
                    job_id=job.job_id,
                    task_id=task_id,
                    reason=reason,
                    failure_stage=result.get(
                        "failure_stage", "budget_continuation"
                    ),
                    checkpoint=result.get("checkpoint", {}),
                    worktree_path=(
                        task_worktree_root if has_worktree else ""
                    ),
                )
                # Preserve the isolated worktree and its checkpoint. A follow-up
                # job receives a unique worktree and the saved continuation data.
                if has_worktree:
                    preserve = getattr(
                        self.merge_manager, "preserve_worktree", None
                    )
                    if callable(preserve):
                        preserve(task_id)
                if requires_confirmation:
                    return {
                        **result,
                        "status": "needs_user_action",
                        "failure_stage": result.get(
                            "failure_stage", "budget_continuation"
                        ),
                    }
                return result

            if result and result.get("status") in {
                "completed", "pending_validation",
            }:
                recovered_for_validation = (
                    result.get("status") == "pending_validation"
                )
                recovered_from_checkpoint = bool(result.get("resumed_artifact"))
                # Coding tasks must edit files. Read-only report tasks instead
                # succeed when they return a substantive report.
                has_file_changes = await self._check_file_changes(
                    task_worktree_root, task_baseline
                )
                task_changes = self.test_manager.snapshot_diff(
                    task_worktree_root, task_baseline
                )
                worker_result = result.get("result") or {}
                task_output = str(
                    worker_result.get("content")
                    or worker_result.get("output")
                    or ""
                ).strip()
                declared_no_changes = bool(worker_result.get("no_changes"))
                reclassified_read_only = False
                if (
                    t.task_type == "coding"
                    and not has_file_changes
                    and not declared_no_changes
                    and task_output
                    and self._request_task_type(job.user_request) == "analysis"
                    and self._request_task_type(
                        f"{t.title} {t.description}"
                    ) == "analysis"
                    and self._read_only_tool_trace(worker_result)
                ):
                    # Last-resort protection for old plans/checkpoints created
                    # before intent classification existed.  This remains
                    # deliberately strict: a non-empty answer alone cannot turn
                    # an unfinished coding task into a success.
                    t.task_type = "analysis"
                    task_data["type"] = "analysis"
                    repos["task"].update_definition(
                        t.id, task_type="analysis", acceptance_command=""
                    )
                    reclassified_read_only = True
                    await self.event_bus.publish(
                        "task_reclassified",
                        job_id=job.job_id,
                        task_id=task_id,
                        previous_type="coding",
                        task_type="analysis",
                        reason=(
                            "原始需求为纯查看，且执行过程只有读取操作并已形成报告"
                        ),
                    )
                missing_required_output = (
                    t.task_type == "coding"
                    and not has_file_changes
                    and not declared_no_changes
                ) or (
                    t.task_type in {"analysis", "review"}
                    and not has_file_changes
                    and not task_output
                ) or (
                    t.task_type == "action"
                    and not bool(worker_result.get("external_action"))
                )
                if missing_required_output:
                    error = (
                        "Coding task produced no file changes"
                        if t.task_type == "coding"
                        else (
                            "External action task produced no external change"
                            if t.task_type == "action"
                            else "Analysis task produced no report"
                        )
                    )
                    logger.warning(
                        f"Task {task_id}: {t.task_type} task completed without "
                        "its required output"
                    )
                    failure = {
                        "status": "failed",
                        "error": error,
                        "failure_stage": "missing_required_output",
                    }
                    repos["task"].update_status_by_pk(t.id, "failed")
                    self._checkpoint_task(
                        repos, job, t, status="failed",
                        result=failure, error=error,
                    )
                    await self.event_bus.publish(
                        "task_failed", job_id=job.job_id,
                        task_id=task_id, error=error,
                        failure_stage="missing_required_output",
                    )
                    if has_worktree:
                        await self.merge_manager.abort_worktree(task_id)
                    return failure

                if (
                    recovered_for_validation
                    and not result.get("pending_event_published")
                ):
                    await self.event_bus.publish(
                        "task_pending_validation",
                        job_id=job.job_id,
                        task_id=task_id,
                        reason=result.get("error", "文档产物已生成，转入确定性验收"),
                        changes=task_changes,
                    )

                # Run acceptance test BEFORE marking done
                test_result = resumed_validation
                test_passed = bool(
                    resumed_validation
                    and resumed_validation.get("status") == "passed"
                ) if recovered_from_checkpoint else True
                if after_write_hook_failure:
                    test_passed = False
                    test_result = {
                        "status": "failed",
                        "output": after_write_hook_failure.get(
                            "output", "after_write hook failed"
                        ),
                        "hook": "after_write",
                    }
                elif recovered_from_checkpoint:
                    pass
                elif t.acceptance_command:
                    before_test_hooks = await self._run_project_hooks(
                        proj_config, "before_test", job_id=job.job_id,
                        project_root=task_worktree_root, task_id=task_id,
                    )
                    if any(item.get("status") != "passed" for item in before_test_hooks):
                        test_result = {
                            "status": "failed",
                            "output": before_test_hooks[-1].get("output", "Hook failed"),
                            "hook": "before_test",
                        }
                    else:
                        test_result = await self.test_manager.run_tests(
                            t, repos, self.event_bus,
                            baseline_snapshot=task_baseline,
                            project_root=task_worktree_root,
                        )
                    if test_result and test_result.get("status") != "passed":
                        test_passed = False
                        logger.warning(f"Task {task_id} acceptance test failed: {test_result.get('status')}")
                elif (
                    t.task_type == "coding"
                    and (proj_config is None or proj_config.auto_validation)
                    and has_file_changes
                ):
                    before_test_hooks = await self._run_project_hooks(
                        proj_config, "before_test", job_id=job.job_id,
                        project_root=task_worktree_root, task_id=task_id,
                    )
                    if any(item.get("status") != "passed" for item in before_test_hooks):
                        test_result = {
                            "status": "failed",
                            "output": before_test_hooks[-1].get("output", "Hook failed"),
                            "hook": "before_test",
                        }
                    else:
                        test_result = await self.test_manager.validate_project(
                            t, repos, self.event_bus,
                            baseline_snapshot=task_baseline,
                            project_root=task_worktree_root,
                        )
                    if test_result.get("status") != "passed":
                        test_passed = False
                        logger.warning(
                            "Task %s deterministic validation failed: %s",
                            task_id, test_result.get("output", "failed"),
                        )

                if not test_passed and t.task_type in {"coding", "testing"}:
                    validation_detail = str(
                        (test_result or {}).get("output")
                        or (test_result or {}).get("error")
                        or "Validation failed"
                    )
                    # Keep focused repairs on the configured Worker. Emergency
                    # is a quality escalation only after two repair/validation
                    # rounds have both failed; it is never a response to repeated
                    # reads or other strategy-level stalls.
                    validation_repair_attempts = max(2, min(3, int(getattr(
                        t, "_rockcore_emergency_after_failures", 3
                    )) - 1))
                    for validation_attempt in range(
                        1, validation_repair_attempts + 1
                    ):
                        await self.event_bus.publish(
                            "task_validation_repairing",
                            job_id=job.job_id,
                            task_id=task_id,
                            validation=validation_detail[:1200],
                            attempt=validation_attempt,
                            max_attempts=validation_repair_attempts,
                        )
                        repair_result = await task_worker.run(
                            t,
                            project_root=task_worktree_root,
                            recovery_context=(
                                "Deterministic acceptance validation failed after "
                                "the implementation. Repair only the concrete "
                                "failure below, preserve useful changes, and avoid "
                                "repeating completed reads.\nValidation output:\n"
                                + validation_detail[:2400]
                                + "\nPrevious Worker evidence:\n"
                                + str(getattr(
                                    t, "_rockcore_retry_evidence", ""
                                ))[:5000]
                            ),
                        )
                        if isinstance(repair_result, dict):
                            evidence = self._worker_retry_evidence(repair_result)
                            if evidence:
                                t._rockcore_retry_evidence = evidence
                        if (
                            not isinstance(repair_result, dict)
                            or repair_result.get("status") != "completed"
                        ):
                            validation_detail = str(
                                (repair_result or {}).get("error")
                                if isinstance(repair_result, dict)
                                else "Focused validation repair did not complete"
                            )
                            continue

                        result = {"status": "completed", "result": repair_result}
                        worker_result = repair_result
                        task_output = str(
                            repair_result.get("content")
                            or repair_result.get("output")
                            or task_output
                        ).strip()
                        has_file_changes = await self._check_file_changes(
                            task_worktree_root, task_baseline
                        )
                        task_changes = self.test_manager.snapshot_diff(
                            task_worktree_root, task_baseline
                        )
                        if t.acceptance_command:
                            test_result = await self.test_manager.run_tests(
                                t, repos, self.event_bus,
                                baseline_snapshot=task_baseline,
                                project_root=task_worktree_root,
                            )
                        else:
                            test_result = await self.test_manager.validate_project(
                                t, repos, self.event_bus,
                                baseline_snapshot=task_baseline,
                                project_root=task_worktree_root,
                            )
                        test_passed = test_result.get("status") == "passed"
                        if test_passed:
                            break
                        validation_detail = str(
                            test_result.get("output")
                            or test_result.get("error")
                            or "Validation failed after focused repair"
                        )

                    if (
                        not test_passed
                        and bool(getattr(t, "_rockcore_emergency_enabled", True))
                        and self.get_agent("emergency_coder")
                    ):
                        await self.event_bus.publish(
                            "task_escalating", job_id=job.job_id,
                            task_id=task_id, reason="validation_repair_exhausted",
                            validation=validation_detail[:1200],
                        )
                        emergency_result = await self._escalate_to_emergency(
                            t, job, validation_detail, task_worktree_root
                        )
                        if emergency_result and emergency_result.get("fix_success"):
                            has_file_changes = await self._check_file_changes(
                                task_worktree_root, task_baseline
                            )
                            task_changes = self.test_manager.snapshot_diff(
                                task_worktree_root, task_baseline
                            )
                            if t.acceptance_command:
                                test_result = await self.test_manager.run_tests(
                                    t, repos, self.event_bus,
                                    baseline_snapshot=task_baseline,
                                    project_root=task_worktree_root,
                                )
                            else:
                                test_result = await self.test_manager.validate_project(
                                    t, repos, self.event_bus,
                                    baseline_snapshot=task_baseline,
                                    project_root=task_worktree_root,
                                )
                            test_passed = test_result.get("status") == "passed"

                if test_passed:
                    # Merge worktree back
                    if has_worktree and has_file_changes:
                        before_commit_hooks = await self._run_project_hooks(
                            proj_config, "before_commit", job_id=job.job_id,
                            project_root=task_worktree_root, task_id=task_id,
                        )
                        if any(
                            item.get("status") != "passed"
                            for item in before_commit_hooks
                        ):
                            detail = str(
                                before_commit_hooks[-1].get("output")
                                or "before_commit hook failed"
                            )
                            failure = {
                                "status": "failed", "error": detail,
                                "failure_stage": "before_commit_hook",
                                "checkpoint": {"changes": task_changes},
                            }
                            repos["task"].update_status_by_pk(t.id, "failed")
                            self._checkpoint_task(
                                repos, job, t, status="failed",
                                result=failure, error=detail,
                            )
                            return failure
                        merge_msg = f"AI {job.job_id}: {task_id} - {t.title}"
                        merge_result = await self.merge_manager.commit_and_merge(task_id, merge_msg)
                        integration_result = merge_result
                        if merge_result.get("status") == "pending_merge":
                            detail = str(
                                merge_result.get("error")
                                or "目标目录存在同名文件，等待显式合并"
                            )
                            internal_failure = {
                                "status": "failed",
                                "error": detail,
                                "failure_stage": "merge_preflight",
                                "checkpoint": {
                                    "changes": task_changes,
                                    "integration": merge_result,
                                },
                            }
                            repos["task"].update_status_by_pk(t.id, "failed")
                            self._checkpoint_task(
                                repos, job, t, status="failed",
                                result=internal_failure, error=detail,
                            )
                            await self.event_bus.publish(
                                "task_failed",
                                job_id=job.job_id, task_id=task_id,
                                error=detail, failure_stage="merge_preflight",
                                checkpoint=internal_failure["checkpoint"],
                                worktree_path=merge_result.get("worktree_path", ""),
                            )
                            preserve = getattr(
                                self.merge_manager, "preserve_worktree", None
                            )
                            if callable(preserve):
                                preserve(task_id)
                            return internal_failure
                        if merge_result.get("status") != "merged":
                            conflicts = merge_result.get("conflicts") or []
                            phase = str(merge_result.get("phase") or "merge")
                            detail = (
                                merge_result.get("error")
                                or merge_result.get("message")
                                or "Task changes could not be integrated"
                            )
                            if conflicts:
                                detail = f"Merge conflict: {', '.join(conflicts)}"
                            preserved_path = str(
                                merge_result.get("worktree_path") or ""
                            )
                            error = f"Git integration failed during {phase}: {detail}"
                            if merge_result.get("preserved") and preserved_path:
                                error += f"; worktree preserved at {preserved_path}"
                                internal_failure = {
                                    "status": "failed",
                                    "error": error,
                                    "failure_stage": "git_integration",
                                    "checkpoint": {
                                        "changes": task_changes,
                                        "integration": merge_result,
                                    },
                                }
                                repos["task"].update_status_by_pk(t.id, "failed")
                                self._checkpoint_task(
                                    repos, job, t, status="failed",
                                    result=internal_failure, error=error,
                                )
                                await self.event_bus.publish(
                                    "task_failed",
                                    job_id=job.job_id, task_id=task_id,
                                    error=error,
                                    failure_stage="git_integration",
                                    checkpoint=internal_failure["checkpoint"],
                                    worktree_path=preserved_path,
                                )
                                preserve = getattr(
                                    self.merge_manager, "preserve_worktree", None
                                )
                                if callable(preserve):
                                    preserve(task_id)
                                return internal_failure
                            repos["task"].update_status_by_pk(t.id, "failed")
                            self._checkpoint_task(
                                repos, job, t, status="failed",
                                result=merge_result, error=error,
                            )
                            await self.event_bus.publish(
                                "task_failed", job_id=job.job_id, task_id=task_id,
                                error=error, failure_stage="git_integration",
                                integration=merge_result,
                            )
                            raise RuntimeError(error)
                    elif has_worktree:
                        # A successful read-only analysis has nothing to merge,
                        # but its temporary worktree still needs to be removed.
                        await self.merge_manager.abort_worktree(task_id)
                    runtime_cleanup = {"status": "not_configured"}
                    if runtime_broker and hasattr(
                        runtime_broker, "cleanup_task_runtime"
                    ):
                        runtime_cleanup = runtime_broker.cleanup_task_runtime()
                        if runtime_cleanup.get("status") not in {
                            "cleaned", "already_clean", "not_configured",
                        }:
                            logger.warning(
                                "Task %s runtime cleanup failed: %s",
                                task_id, runtime_cleanup,
                            )
                    t._rockcore_runtime_checkpoint = {}
                    repos["task"].update_status_by_pk(t.id, "done")
                    result_payload = dict(result)
                    result_payload["status"] = "completed"
                    result_payload["changes"] = task_changes
                    result_payload["runtime"] = {
                        "cleanup": runtime_cleanup,
                        "relocated_intermediates": runtime_relocation,
                    }
                    if recovered_from_checkpoint:
                        result_payload["resumed_from_checkpoint"] = True
                        result_payload["completion_note"] = (
                            "已复用中断前的任务产物并通过确定性验收，未重复调用模型"
                        )
                    elif recovered_for_validation:
                        result_payload["recovered_from_budget"] = True
                        result_payload["completion_note"] = (
                            "模型预算在收尾阶段耗尽；现有产物已通过确定性验收"
                        )
                    if integration_result:
                        result_payload["integration"] = integration_result
                    if declared_no_changes:
                        result_payload["no_changes"] = True
                    if reclassified_read_only:
                        result_payload["reclassified_from"] = "coding"
                    if task_output:
                        result_payload["output"] = task_output
                    self._checkpoint_task(
                        repos, job, t, status="done", result=result_payload
                    )
                    task_context_manager = getattr(
                        task_worker, "context_manager", None
                    )
                    if task_context_manager:
                        await task_context_manager.update_after_task(
                            t, result_payload
                        )
                    await self.event_bus.publish(
                        "task_done", job_id=job.job_id,
                        task_id=task_id, result=result_payload,
                    )
                    completed_task_results[task_id] = result_payload
                    return result_payload
                else:
                    validation_detail = str(
                        (test_result or {}).get("output")
                        or (test_result or {}).get("error")
                        or "Acceptance validation did not pass"
                    )
                    continuation = {
                        "status": "needs_continuation",
                        "error": (
                            "Generated changes did not pass validation; the "
                            "worktree and checkpoint were preserved for a "
                            "focused repair. " + validation_detail[:1200]
                        ),
                        "failure_stage": "validation_continuation",
                        "checkpoint": {
                            "changes": task_changes,
                            "validation": test_result or {"status": "failed"},
                            "recovered_from_model_limit": recovered_for_validation,
                        },
                    }
                    repos["task"].update_status_by_pk(t.id, "interrupted")
                    self._checkpoint_task(
                        repos, job, t, status="interrupted",
                        result=continuation,
                        error=continuation["error"],
                    )
                    await self.event_bus.publish(
                        "task_needs_continuation", job_id=job.job_id,
                        task_id=task_id,
                        reason=continuation["error"],
                        failure_stage="validation_continuation",
                        checkpoint=continuation["checkpoint"],
                        worktree_path=(
                            task_worktree_root if has_worktree else ""
                        ),
                    )
                    if has_worktree:
                        preserve = getattr(
                            self.merge_manager, "preserve_worktree", None
                        )
                        if callable(preserve):
                            preserve(task_id)
                    return continuation
            else:
                failure_stage = (
                    result.get("failure_stage", "") if result else ""
                )
                task_failure_status = (
                    "model_configuration_failed"
                    if failure_stage == "model_configuration" else "failed"
                )
                repos["task"].update_status_by_pk(t.id, task_failure_status)
                await self.event_bus.publish("task_failed", job_id=job.job_id,
                                              task_id=task_id,
                                              error=result.get("error", "Unknown") if result else "Unknown",
                                              failure_stage=failure_stage)
                if has_worktree:
                    await self.merge_manager.abort_worktree(task_id)
                error = result.get("error", "Unknown") if result else "Unknown"
                self._checkpoint_task(
                    repos, job, t, status=task_failure_status, result=result,
                    error=error,
                )
                raise RuntimeError(error)

        # Run through DAG scheduler
        try:
            results = await self.scheduler.run_dag(task_dicts, run_single_task)
            repos["_session"].refresh(job)
            if job.status == "cancelled" or self.scheduler.is_stopped:
                await self.event_bus.publish("phase_summary",
                    phase="execution", agent_type="worker", status="cancelled",
                    summary="任务已由用户停止",
                    details={"done": len(self.scheduler._completed), "failed": 0},
                )
                return {"status": "cancelled", "reason": "任务已由用户停止"}
            blocked = [
                tid for tid, result in results.items()
                if isinstance(result, dict) and result.get("status") == "blocked"
            ]
            for task_id in blocked:
                task_data = next(
                    (item for item in task_dicts if item["task_id"] == task_id),
                    None,
                )
                if not task_data:
                    continue
                blocked_result = results[task_id]
                repos["task"].update_status_by_pk(
                    task_data["_db_task"].id, "blocked"
                )
                self._checkpoint_task(
                    repos, job, task_data["_db_task"], status="blocked",
                    result=blocked_result,
                    error=blocked_result.get("error", "Dependency failed"),
                )
                await self.event_bus.publish(
                    "task_blocked",
                    job_id=job.job_id,
                    task_id=task_id,
                    error=blocked_result.get("error", "Dependency failed"),
                    blocked_by=blocked_result.get("blocked_by", []),
                )

            failure_summary = self._execution_failure_summary(results, blocked)
            failed = failure_summary["failed"]
            if failed:
                logger.error(f"Tasks failed: {failed}")
                continuation_tasks = failure_summary["continuation_tasks"]
                attention_tasks = failure_summary["attention_tasks"]
                terminal_status = failure_summary["terminal_status"]
                repos["job"].update_status(job.job_id, terminal_status)
                self.state_machine.transition(
                    job.job_id,
                    JobState.WAITING_USER
                    if terminal_status in {"needs_attention", "interrupted"}
                    else JobState.FAILED,
                )
                direct_failures = failure_summary["direct_failures"]
                reason = failure_summary["reason"]
                self._store_job_failure(repos, job.job_id, reason)
                if terminal_status == "needs_attention":
                    failure_code, recovery_hint = self._failure_details(reason)
                    await self.event_bus.publish(
                        "job_needs_attention",
                        job_id=job.job_id,
                        reason=reason,
                        recovery_hint=recovery_hint,
                        failure_code=failure_code,
                        failure_stage="execution_user_action_required",
                        task_ids=attention_tasks,
                    )
                await self.event_bus.publish("phase_summary",
                    phase="execution", agent_type="worker",
                    status=(
                        "needs_attention" if attention_tasks else (
                            "interrupted" if continuation_tasks else "failed"
                        )
                    ),
                    summary=(
                        f"等待用户处理后继续：{reason}"
                        if attention_tasks else
                        f"已保存执行进度，等待继续：{reason}"
                        if continuation_tasks
                        else f"任务执行失败：{reason}"
                    ),
                    details={
                        "done": len(self.scheduler._completed),
                        "failed": len(direct_failures),
                        "blocked": len(blocked),
                        **(
                            {"needs_continuation": continuation_tasks}
                            if continuation_tasks else {}
                        ),
                        **(
                            {"needs_user_action": attention_tasks}
                            if attention_tasks else {}
                        ),
                    },
                )
                return {
                    "status": terminal_status,
                    "reason": reason,
                    "needs_continuation": continuation_tasks,
                    "needs_user_action": attention_tasks,
                }
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            repos["job"].update_status(job.job_id, "failed")
            self._store_job_failure(repos, job.job_id, str(e))
            self.state_machine.transition(job.job_id, JobState.FAILED)
            await self.event_bus.publish("phase_summary",
                phase="execution", agent_type="worker", status="failed",
                summary=f"执行异常：{str(e)[:100]}",
            )
            return {"status": "failed", "reason": str(e)}

        self.state_machine.transition(job.job_id, JobState.TESTING)
        await self.event_bus.publish("execution_complete", job_id=job.job_id)
        await self.event_bus.publish("phase_summary",
            phase="execution", agent_type="worker", status="success",
            summary=f"所有任务执行完成",
            details={"done": len(task_dicts), "failed": 0},
        )
        return {"status": "completed", "task_ids": sorted(task_ids or [])}

    @staticmethod
    def _collect_analysis_dependency_reports(
        task_id: str,
        task_data_by_id: dict[str, dict],
        completed_results: dict[str, dict],
    ) -> dict[str, str]:
        """Collect reports from all completed analysis ancestors of a task."""
        reports: dict[str, str] = {}
        visited: set[str] = set()

        def visit(current_id: str):
            if current_id in visited:
                return
            visited.add(current_id)
            current = task_data_by_id.get(current_id) or {}
            for dependency_id in current.get("dependencies") or []:
                dependency = task_data_by_id.get(dependency_id) or {}
                result = completed_results.get(dependency_id) or {}
                if dependency.get("type") == "analysis":
                    output = str(
                        result.get("output")
                        or (result.get("result") or {}).get("content")
                        or ""
                    ).strip()
                    if output:
                        reports[dependency_id] = output
                visit(dependency_id)

        visit(task_id)
        return reports

    async def _ground_task_in_analysis(self, task, job, repos,
                                       reports: dict[str, str]):
        """Feed prerequisite findings into a task and repair guessed paths."""
        if not reports or task.task_type == "analysis":
            return

        report_sections = [
            f"[{task_id}]\n{report[:5000]}"
            for task_id, report in sorted(reports.items())
        ]
        report_text = "\n\n".join(report_sections)
        marker = "\n\n=== Verified prerequisite analysis ===\n"
        description = task.description or ""
        if marker.strip() not in description:
            description = description + marker + report_text

        project_root = job.project.root_path if job.project else "."
        project_files = self._project_output_files(project_root)
        basename_counts: dict[str, int] = {}
        for relative_path in project_files:
            basename = Path(relative_path).name
            basename_counts[basename] = basename_counts.get(basename, 0) + 1
        referenced_files = [
            relative_path
            for relative_path in project_files
            if (
                relative_path in report_text
                or (
                    basename_counts.get(Path(relative_path).name) == 1
                    and Path(relative_path).name in report_text
                )
            )
        ]

        current_paths = list(task.allowed_paths or [])
        current_matches = any(
            fnmatch.fnmatch(relative_path, pattern)
            for relative_path in project_files
            for pattern in current_paths
        )
        refined_paths = current_paths
        if referenced_files and not current_matches:
            # The original paths were speculative and match no real project
            # files. Replace them with exact files proven by the prerequisite.
            refined_paths = sorted(set(referenced_files))

        constitution = repos["constitution"].get_by_job(job.id)
        validation_errors = self.policy_engine.check_task_plan(
            {
                "tasks": [{
                    "id": task.task_id,
                    "allowed_paths": refined_paths,
                }],
            },
            {
                "protected_paths": (
                    constitution.protected_paths if constitution else []
                ),
            },
        )
        if validation_errors:
            logger.warning(
                "Task %s analysis refinement rejected: %s",
                task.task_id, validation_errors,
            )
            # The report itself is still safe and useful context even when a
            # proposed path intersects a protected area.
            repos["task"].update_definition(
                task.id,
                description=description,
                allowed_paths=current_paths,
            )
            task.description = description
            await self.event_bus.publish(
                "task_refinement_rejected",
                job_id=job.job_id,
                task_id=task.task_id,
                errors=validation_errors,
            )
            return

        repos["task"].update_definition(
            task.id,
            description=description,
            allowed_paths=refined_paths,
        )
        task.description = description
        task.allowed_paths = refined_paths
        await self.event_bus.publish(
            "task_refined",
            job_id=job.job_id,
            task_id=task.task_id,
            analysis_tasks=sorted(reports),
            allowed_paths=refined_paths,
            paths_changed=refined_paths != current_paths,
        )

    async def _validate_resumed_artifact(
        self, task, job, repos, project_root: str,
        baseline_snapshot: dict, resumed_paths: list[str],
    ) -> tuple[dict | None, dict | None]:
        """Accept a preserved checkpoint without another model call when valid."""
        if not resumed_paths or task.task_type not in {"coding", "testing"}:
            return None, None
        await self.event_bus.publish(
            "task_pending_validation",
            job_id=job.job_id,
            task_id=task.task_id,
            reason="已恢复原任务产物，先验收后决定是否继续调用模型",
            resumed_files=resumed_paths,
        )
        if task.acceptance_command:
            validation = await self.test_manager.run_tests(
                task, repos, self.event_bus,
                baseline_snapshot=baseline_snapshot,
                project_root=project_root,
            )
        else:
            validation = await self.test_manager.validate_project(
                task, repos, self.event_bus,
                baseline_snapshot=baseline_snapshot,
                project_root=project_root,
            )
        if validation.get("status") != "passed":
            return None, validation
        return ({
            "status": "pending_validation",
            "completion_note": "恢复的任务产物已通过确定性验收",
            "failure_stage": "checkpoint_artifact_validation",
            "pending_event_published": True,
            "resumed_artifact": True,
        }, validation)

    async def _execute_single_task_with_escalation(
        self, task, job, repos, worker, worktree_root,
        baseline_snapshot: dict | None = None,
    ):
        """Retry one Worker; switch provider only for provider-level failures."""
        last_error = ""
        repair_guidance = str(
            getattr(task, "_rockcore_initial_recovery_context", "") or ""
        )
        retry_evidence = self._worker_retry_evidence(
            dict(getattr(task, "result_data", None) or {})
        )
        initial_turn_budget = getattr(worker, "max_turns", 16)
        document_profile = getattr(task, "_rockcore_document_profile", None)
        continuation_turn_budget = (
            min(96, max(48, initial_turn_budget // 2))
            if document_profile else
            min(36, max(24, initial_turn_budget // 2))
        )
        document_budget_extensions = 0
        configured_attempts = max(
            1, int(getattr(task, "_rockcore_retry_count", MAX_FLASH_RETRY)) + 1
        )
        emergency_after = max(1, min(6, int(getattr(
            task, "_rockcore_emergency_after_failures", 3
        ))))
        primary_attempts = min(configured_attempts, emergency_after)
        budget_finalization_retry = False
        document_continuations = 0

        async def extend_after_budget(error: str, attempt: int) -> bool:
            nonlocal document_budget_extensions, budget_finalization_retry
            nonlocal repair_guidance
            if not document_profile or document_budget_extensions >= 2:
                return False
            if attempt > primary_attempts:
                return False
            if attempt >= primary_attempts and not await self._check_file_changes(
                worktree_root, baseline_snapshot
            ):
                return False
            extended = await self._extend_document_budget_for_retry(
                task, job, worker, error, document_budget_extensions + 1,
            )
            if not extended:
                return False
            document_budget_extensions += 1
            repair_guidance = (
                "RockCore enlarged the document Token/call safety ceiling. "
                "Do not repeat source reading. Validate the existing artifacts, "
                "make only focused corrections, and return the final response."
            )
            if attempt >= primary_attempts:
                budget_finalization_retry = True
                self._enter_document_finalization_mode(task, worker)
            return True

        async def pending_validation_or_failure(
            error: str,
            *,
            failure_stage: str = "budget_finalization",
            budget_exhausted: bool = True,
        ) -> dict:
            if await self._check_file_changes(
                worktree_root, baseline_snapshot
            ):
                await self.event_bus.publish(
                    "task_pending_validation",
                    job_id=job.job_id,
                    task_id=getattr(task, "task_id", ""),
                    reason=error,
                )
                return {
                    "status": "pending_validation",
                    "error": error,
                    "failure_stage": failure_stage,
                    "budget_exhausted": budget_exhausted,
                    "pending_event_published": True,
                    "document_progress": getattr(
                        task, "_rockcore_document_progress", {}
                    ),
                }
            document_progress = dict(
                getattr(task, "_rockcore_document_progress", {}) or {}
            )
            if document_progress:
                return {
                    "status": "needs_continuation",
                    "error": error,
                    "failure_stage": "budget_continuation",
                    "checkpoint": {
                        "document_progress": document_progress,
                        "reason": error,
                    },
                }
            if self._is_user_action_required(error):
                return {
                    "status": "needs_user_action",
                    "error": error,
                    "failure_stage": "user_action_required",
                    "checkpoint": {"reason": error},
                }
            # Exhausting a configurable RockCore budget is recoverable even
            # when no file was written yet. Pause for confirmation instead of
            # turning a resource limit into a terminal coding failure.
            if self._is_budget_error(error):
                return {
                    "status": "needs_user_action",
                    "error": error,
                    "failure_stage": "budget_continuation",
                    "checkpoint": {"reason": error},
                }
            return {
                "status": "failed",
                "error": error,
                "failure_stage": "budget_exhausted_without_progress",
            }

        for attempt in range(1, primary_attempts + 2):
            if attempt > primary_attempts and not budget_finalization_retry:
                break
            if document_profile:
                preflight = await self._prepare_document_attempt_budget(
                    task, job, worker, attempt, primary_attempts,
                    document_budget_extensions, worktree_root,
                    baseline_snapshot,
                )
                if preflight.get("extended"):
                    document_budget_extensions += 1
            try:
                recovery_context = ""
                if attempt > 1:
                    recovery_context = (
                        f"Primary Worker attempt {attempt - 1} failed. "
                        f"Error: {last_error[:1800]}\n"
                        "Inspect the existing changes and the exact failure; do not "
                        "repeat broad exploration. Apply a focused fix and verify it."
                    )
                    if attempt >= 3:
                        recovery_context += (
                            " This is the final focused Worker strategy attempt. "
                            "Re-read the acceptance command and relevant final files."
                        )
                    if repair_guidance:
                        recovery_context += "\nPlanner guidance:\n" + repair_guidance
                    if retry_evidence:
                        recovery_context += (
                            "\nReusable evidence from the previous attempt "
                            "(do not re-read these unchanged sources):\n"
                            + retry_evidence
                        )
                elif repair_guidance:
                    recovery_context = repair_guidance
                result = await worker.run(
                    task,
                    project_root=worktree_root,
                    recovery_context=recovery_context,
                )
                if isinstance(result, dict):
                    current_evidence = self._worker_retry_evidence(result)
                    if current_evidence:
                        retry_evidence = current_evidence
                        task._rockcore_retry_evidence = current_evidence
                # Worker already persisted a continuation checkpoint (most
                # commonly because it reached max turns). Do not spend more
                # retries rereading the project; the execution layer will
                # surface this as an actionable pause and preserve the task.
                if result and result.get("status") == "needs_continuation":
                    return result
                if result and result.get("status") == "completed":
                    return {"status": "completed", "result": result}

                if result and result.get("error"):
                    last_error = str(result["error"])
                    if self._is_user_input_required(last_error):
                        return {
                            "status": "needs_user_action",
                            "error": last_error,
                            "failure_stage": "user_input_required",
                            "checkpoint": {"reason": last_error},
                        }
                    if (
                        self._is_user_action_required(last_error)
                        and not self._is_provider_unavailable(last_error)
                    ):
                        return {
                            "status": "needs_user_action",
                            "error": last_error,
                            "failure_stage": "user_action_required",
                            "checkpoint": {"reason": last_error},
                        }
                    if self._is_budget_error(last_error):
                        if await extend_after_budget(last_error, attempt):
                            continue
                        return await pending_validation_or_failure(last_error)
                    if self._is_task_path_mismatch(last_error):
                        logger.warning(
                            "Task %s has an invalid allowed-path plan; "
                            "requesting a focused path correction",
                            task.task_id,
                        )
                        if attempt < primary_attempts:
                            try:
                                repair_plan = await self._repair_plan(
                                    job, task, last_error, repos
                                )
                            except BudgetExceededError as error:
                                return await pending_validation_or_failure(
                                    str(error)
                                )
                            if repair_plan:
                                await self._apply_repair_plan_paths(
                                    task, job, repos, repair_plan
                                )
                                repair_guidance = json.dumps(
                                    repair_plan, ensure_ascii=False, default=str
                                )[:3000]
                        continue
                    if self._is_provider_capability_error(last_error):
                        break
                    if self._is_provider_unavailable(last_error):
                        break
                    if self._is_stalled_worker_error(last_error):
                        logger.warning(
                            "Task %s hit a strategy stall; keeping the same "
                            "provider and validating any existing artifact",
                            task.task_id,
                        )
                        if await self._check_file_changes(
                            worktree_root, baseline_snapshot
                        ):
                            return await pending_validation_or_failure(
                                last_error,
                                failure_stage="strategy_stall_validation",
                                budget_exhausted=False,
                            )
                        repair_guidance = (
                            "The previous attempt repeated an ineffective tool "
                            "strategy. Keep the current provider. Reuse the "
                            "existing read results, avoid broad exploration, and "
                            "make the smallest concrete change required."
                        )
                        continue
                    if "ended without editing files" in last_error.lower():
                        logger.warning(
                            f"Task {task.task_id}: model ended before editing; "
                            "switching to focused repair"
                        )
                        continue

                # A turn limit means the model did not explicitly finish. Keep
                # partial edits for a continuation, but never auto-pass merely
                # because some file changed.
                if result and "Max turns" in str(result.get("error", "")):
                    document_progress = dict(
                        result.get("document_progress") or {}
                    )
                    task._rockcore_document_progress = document_progress
                    has_changes = await self._check_file_changes(
                        worktree_root, baseline_snapshot
                    )
                    if has_changes:
                        progress_detail = ""
                        if document_progress:
                            progress_detail = "; continue document from " + ", ".join(
                                f"{path} page {page}"
                                for path, page in sorted(document_progress.items())
                            )
                        last_error = (
                            "Max turns reached: partial changes require completion"
                            + progress_detail
                        )
                        artifact_manifest = dict(
                            getattr(task, "_rockcore_artifact_manifest", None) or {}
                        )
                        validate_artifact_first = bool(
                            artifact_manifest.get("require_changed_output")
                        )
                        if validate_artifact_first and (
                            not document_progress or document_continuations >= 1
                        ):
                            await self.event_bus.publish(
                                "task_pending_validation",
                                job_id=job.job_id,
                                task_id=task.task_id,
                                reason=last_error,
                            )
                            return {
                                "status": "pending_validation",
                                "error": last_error,
                                "failure_stage": "turn_limit_validation",
                                "pending_event_published": True,
                                "document_progress": document_progress,
                            }
                        if document_progress:
                            document_continuations += 1
                        logger.warning(
                            f"Task {task.task_id}: max turns with unread pages; "
                            "allowing one bounded continuation"
                        )
                        await self.event_bus.publish(
                            "task_continuing",
                            job_id=job.job_id,
                            task_id=task.task_id,
                            reason=last_error,
                            attempt=attempt + 1,
                            max_turns=continuation_turn_budget,
                            document_progress=document_progress,
                        )
                        worker.max_turns = continuation_turn_budget
                        continue
                    else:
                        last_error = "Max turns reached: no changes detected"
                        logger.warning(
                            f"Task {task.task_id}: max turns with no changes; "
                            "pausing for user confirmation"
                        )
                        return {
                            "status": "needs_user_action",
                            "error": last_error,
                            "failure_stage": "turn_limit_continuation",
                            "checkpoint": {
                                "reason": last_error,
                                "retry_evidence": retry_evidence,
                                "document_progress": document_progress,
                            },
                        }
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Task {task.task_id} attempt {attempt} failed: {e}")
                if self._is_user_input_required(last_error):
                    return {
                        "status": "needs_user_action",
                        "error": last_error,
                        "failure_stage": "user_input_required",
                        "checkpoint": {"reason": last_error},
                    }
                if (
                    self._is_user_action_required(last_error)
                    and not self._is_provider_unavailable(last_error)
                ):
                    return {
                        "status": "needs_user_action",
                        "error": last_error,
                        "failure_stage": "user_action_required",
                        "checkpoint": {"reason": last_error},
                    }
                if self._is_budget_error(last_error):
                    if await extend_after_budget(last_error, attempt):
                        continue
                    return await pending_validation_or_failure(last_error)
                if self._is_provider_capability_error(last_error):
                    logger.warning(
                        f"Task {task.task_id}: provider capability mismatch; "
                        "switching to fallback"
                    )
                    break
                if self._is_provider_unavailable(last_error):
                    logger.warning(
                        f"Task {task.task_id}: provider failure is not retryable "
                        "on the same provider; switching to fallback"
                    )
                    break
                if self._is_stalled_worker_error(last_error):
                    if await self._check_file_changes(
                        worktree_root, baseline_snapshot
                    ):
                        return await pending_validation_or_failure(
                            last_error,
                            failure_stage="strategy_stall_validation",
                            budget_exhausted=False,
                        )
                    repair_guidance = (
                        "The previous attempt repeated an ineffective tool "
                        "strategy. Keep the current provider and use a focused "
                        "alternative without repeating completed reads."
                    )
                    continue

        if (
            "max turns" in last_error.lower()
            and await self._check_file_changes(worktree_root, baseline_snapshot)
        ):
            await self.event_bus.publish(
                "task_pending_validation",
                job_id=job.job_id,
                task_id=getattr(task, "task_id", ""),
                reason=last_error,
            )
            return {
                "status": "pending_validation",
                "error": last_error,
                "failure_stage": "budget_finalization",
                "pending_event_published": True,
                "document_progress": getattr(
                    task, "_rockcore_document_progress", {}
                ),
            }

        # Existing output is more trustworthy than a model's terminal wording.
        # Validate it before changing provider or invoking another model.
        if await self._check_file_changes(worktree_root, baseline_snapshot):
            await self.event_bus.publish(
                "task_pending_validation",
                job_id=job.job_id,
                task_id=getattr(task, "task_id", ""),
                reason=last_error or "模型未正常结束，先验收现有产物",
            )
            return {
                "status": "pending_validation",
                "error": last_error,
                "failure_stage": "artifact_recovery",
                "pending_event_published": True,
                "document_progress": getattr(
                    task, "_rockcore_document_progress", {}
                ),
            }

        model_configuration_terminal = self._is_model_configuration_error(last_error)
        if (
            self._is_provider_capability_error(last_error)
            or self._is_provider_unavailable(last_error)
        ):
            fallback = await self._run_worker_fallback(
                worker, task, worktree_root, last_error
            )
            if fallback:
                if fallback.get("status") == "completed":
                    return fallback
                model_configuration_terminal = bool(
                    fallback.get("all_models_unavailable")
                )
                last_error = fallback.get("error", last_error)

            # A failed fallback may still have produced a valid artifact.
            if await self._check_file_changes(
                worktree_root, baseline_snapshot
            ):
                await self.event_bus.publish(
                    "task_pending_validation",
                    job_id=job.job_id,
                    task_id=getattr(task, "task_id", ""),
                    reason=last_error or "备用模型未正常结束，先验收现有产物",
                )
                return {
                    "status": "pending_validation",
                    "error": last_error,
                    "failure_stage": "artifact_recovery",
                    "pending_event_published": True,
                    "document_progress": getattr(
                        task, "_rockcore_document_progress", {}
                    ),
                }

        # User-resolvable provider/budget failures must remain resumable even
        # when automatic repair is disabled for this project.
        if model_configuration_terminal:
            return {
                "status": "needs_user_action",
                "error": f"模型配置不可用：{last_error}",
                "failure_stage": "model_configuration",
                "checkpoint": {
                    "reason": last_error,
                    "next_action": "Choose an available model and resume this task",
                },
            }
        if self._is_user_action_required(last_error):
            return {
                "status": "needs_user_action",
                "error": last_error,
                "failure_stage": "user_action_required",
                "checkpoint": {"reason": last_error},
            }
        if self._is_budget_error(last_error):
            return {
                "status": "needs_user_action",
                "error": last_error,
                "failure_stage": "budget_continuation",
                "checkpoint": {"reason": last_error},
            }
        if not bool(getattr(task, "_rockcore_auto_repair", True)):
            return {"status": "failed", "error": last_error}

        # The configured primary Worker strategy threshold has been reached.
        # Emergency is deliberately not used here: it is reserved for code that
        # repeatedly fails deterministic validation after focused repair.
        await self.event_bus.publish("task_repairing", job_id=job.job_id,
                                      task_id=task.task_id, error=last_error,
                                      attempts=primary_attempts)
        checkpoint_progress = dict(
            getattr(task, "_rockcore_document_progress", {}) or {}
        )
        if (
            checkpoint_progress
            and (
                self._is_budget_error(last_error)
                or "max turns" in last_error.lower()
                or self._is_provider_unavailable(last_error)
                or self._is_stalled_worker_error(last_error)
            )
        ):
            return {
                "status": "needs_continuation",
                "error": last_error,
                "failure_stage": "checkpoint_continuation",
                "checkpoint": {
                    "reason": last_error,
                    "document_progress": checkpoint_progress,
                },
            }

        # No file change or structured progress exists here. Provider, budget,
        # and turn-limit failures are recoverable after user confirmation.
        if (
            self._is_budget_error(last_error)
            or "max turns" in last_error.lower()
        ):
            return {
                "status": "needs_user_action",
                "error": last_error,
                "failure_stage": (
                    "budget_continuation"
                    if self._is_budget_error(last_error)
                    else "turn_limit_continuation"
                ),
                "checkpoint": {"reason": last_error},
            }

        if self._is_transient_provider_error(last_error):
            return {
                "status": "needs_continuation",
                "error": last_error,
                "failure_stage": "provider_interruption",
                "checkpoint": {
                    "reason": last_error,
                    "next_action": "Resume this task when the provider is available",
                },
            }

        # Non-budget/model failures with no usable artifact remain failures.
        return {"status": "failed", "error": last_error}

    @staticmethod
    def _worker_retry_evidence(result: dict | None) -> str:
        """Compact reusable file/search evidence for retries and continuations."""
        if not isinstance(result, dict):
            return ""
        nested = result.get("result")
        if isinstance(nested, dict):
            result = nested
        rows: list[str] = []
        stored_evidence = str(result.get("retry_evidence") or "").strip()
        if stored_evidence:
            rows.append(stored_evidence[:7000])
        seen = set()
        for call in list(result.get("tool_calls") or [])[-40:]:
            if not isinstance(call, dict):
                continue
            name = str(call.get("tool") or call.get("name") or "")
            args = call.get("args") or call.get("arguments") or {}
            if not isinstance(args, dict):
                args = {"value": str(args)}
            signature = (
                name,
                json.dumps(args, ensure_ascii=False, sort_keys=True, default=str),
            )
            if signature in seen:
                continue
            seen.add(signature)
            if name not in {
                "read_file", "search_code", "list_files", "read_pdf",
                "git_diff", "git_status", "run_tests", "run_command",
                "apply_patch", "write_file", "insert_before", "insert_after",
            }:
                continue
            summary = str(call.get("result_summary") or "")[:700]
            rows.append(
                f"- {name}({signature[1][:800]}) -> "
                f"{call.get('result_status', call.get('status', ''))}"
                + (f"; {summary}" if summary else "")
            )
        progress = result.get("document_progress") or {}
        if progress:
            rows.append(
                "- saved document progress: "
                + json.dumps(progress, ensure_ascii=False, default=str)[:1200]
            )
        content = str(
            result.get("content") or result.get("output")
            or result.get("summary") or ""
        ).strip()
        if content:
            rows.append("- previous conclusion: " + content[-1600:])
        return "\n".join(rows[-18:])[:9000]

    @staticmethod
    def _is_budget_error(error: str) -> bool:
        normalized = (error or "").lower()
        return (
            "rockcore job budget exceeded" in normalized
            or "total tokens exceeded" in normalized
            or "input tokens exceeded" in normalized
            or "output tokens exceeded" in normalized
            or "api calls exceeded" in normalized
            or "cost exceeded" in normalized
        )

    @classmethod
    def _checkpoint_requires_confirmation(cls, result: dict | None) -> bool:
        """Whether a saved continuation needs an explicit user confirmation.

        Turn limits and configurable/token/provider budgets are not code
        failures. The user may raise the limit, add credits, or change the
        project provider before resuming the same task checkpoint.
        """
        if not isinstance(result, dict):
            return False
        error = str(result.get("error") or "")
        stage = str(result.get("failure_stage") or "").lower()
        return (
            "max turns" in error.lower()
            or cls._is_budget_error(error)
            or "turn_limit" in stage
            or "budget" in stage
            or cls._is_user_action_required(error)
        )

    @staticmethod
    def _enter_document_finalization_mode(task, worker) -> None:
        """Constrain a document continuation to artifact checks and completion."""
        profile = getattr(task, "_rockcore_document_profile", None) or {}
        turns = max(12, int(profile.get("finalization_turns", 36) or 36))
        task._rockcore_finalization_mode = True
        worker.max_turns = min(max(1, int(worker.max_turns)), turns)
        if hasattr(worker, "max_exploration_turns"):
            worker.max_exploration_turns = min(
                max(1, int(worker.max_exploration_turns)), 9
            )

    async def _prepare_document_attempt_budget(
        self, task, job, worker, attempt: int, primary_attempts: int,
        extension_count: int, worktree_root: str,
        baseline_snapshot: dict | None = None,
    ) -> dict:
        """Forecast continuation cost and preserve a deterministic finish reserve."""
        profile = getattr(task, "_rockcore_document_profile", None) or {}
        if not profile:
            return {"extended": False, "finalization": False}

        usage = self.model_router.cost_engine.get_task_usage(
            job.job_id, getattr(task, "task_id", "")
        )
        calls = int(usage.get("calls", 0) or 0)
        finalization_candidate = attempt >= primary_attempts and calls > 0
        finalization = (
            finalization_candidate
            and await self._check_file_changes(worktree_root, baseline_snapshot)
        )
        if finalization:
            self._enter_document_finalization_mode(task, worker)
            if not getattr(task, "_rockcore_finalization_announced", False):
                task._rockcore_finalization_announced = True
                await self.event_bus.publish(
                    "document_finalization_started",
                    job_id=job.job_id,
                    task_id=getattr(task, "task_id", ""),
                    max_turns=worker.max_turns,
                    reason="预留预算已切换为产物验收与收尾",
                )

        if not calls:
            return {"extended": False, "finalization": finalization}

        hard_limit = max(
            0, int(getattr(task, "_rockcore_input_budget", 0) or 0)
        )
        used = int(usage.get("effective_input_tokens", 0) or 0)
        remaining = max(0, hard_limit - used)
        average = max(
            8_000, int(usage.get("average_input_tokens", 0) or 0)
        )
        predicted = average * max(1, int(worker.max_turns))
        reserve = (
            0 if finalization
            else max(0, int(profile.get("finalization_reserve", 0) or 0))
        )
        required = predicted + reserve
        extended = False
        if remaining < required and extension_count < 2:
            reason = (
                "Forecasted document continuation requires "
                f"{required} input tokens but only {remaining} remain"
            )
            extended = await self._extend_document_budget_for_retry(
                task, job, worker, reason, extension_count + 1,
            )
            if finalization:
                self._enter_document_finalization_mode(task, worker)

        await self.event_bus.publish(
            "document_budget_forecast",
            job_id=job.job_id,
            task_id=getattr(task, "task_id", ""),
            attempt=attempt,
            used_input_tokens=used,
            remaining_input_tokens=remaining,
            predicted_input_tokens=predicted,
            finalization_reserve=reserve,
            finalization=finalization,
            extended=extended,
        )
        return {"extended": extended, "finalization": finalization}

    async def _extend_document_budget_for_retry(
        self, task, job, worker, error: str, extension_round: int
    ) -> bool:
        """Increase non-monetary document ceilings instead of wasting progress."""
        normalized = str(error or "").lower()
        profile = getattr(task, "_rockcore_document_profile", None)
        if not profile or any(marker in normalized for marker in (
            "billable api cost exceeded", "cost exceeded",
        )):
            return False

        current = max(0, int(getattr(task, "_rockcore_input_budget", 0) or 0))
        if current <= 0:
            return False
        extension = max(600_000, current // 2)
        enlarged = min(
            int(getattr(
                task, "_rockcore_max_auto_input_budget", 50_000_000
            ) or 50_000_000),
            current + extension,
        )
        if enlarged <= current:
            return False

        task._rockcore_input_budget = enlarged
        continuation_turns = min(
            96, max(48, int(profile.get("max_turns", 144)) // 2)
        )
        worker.max_turns = max(worker.max_turns, continuation_turns)
        required_calls = (
            int(profile.get("api_call_budget", 0) or 0)
            + continuation_turns * extension_round
        )
        job_budget = self.model_router.cost_engine.reserve_document_budget(
            job.job_id,
            enlarged,
            required_api_calls=required_calls,
            required_output_tokens=int(profile.get("output_budget", 0) or 0),
        )
        await self.event_bus.publish(
            "document_budget_extended",
            job_id=job.job_id,
            task_id=getattr(task, "task_id", ""),
            extension_round=extension_round,
            previous_task_input_budget=current,
            task_input_budget=enlarged,
            max_turns=worker.max_turns,
            job_input_budget=job_budget.max_input_tokens,
            job_total_budget=job_budget.max_total_tokens,
            job_api_call_budget=job_budget.max_api_calls,
            reason=str(error or "")[:300],
        )
        logger.warning(
            "Extended document budget for %s after %s: input %s -> %s",
            getattr(task, "task_id", ""), error, current, enlarged,
        )
        return True

    @staticmethod
    def _is_user_input_required(error: str) -> bool:
        return "user_input_required:" in str(error or "").lower()

    @staticmethod
    def _is_task_path_mismatch(error: str) -> bool:
        normalized = (error or "").lower()
        return (
            "[allowed_path]" in normalized
            or "path not in allowed set" in normalized
        )

    @staticmethod
    def _is_stalled_worker_error(error: str) -> bool:
        """Stop retrying a strategy that has produced no useful progress."""
        normalized = str(error or "").lower()
        return (
            "no_progress:" in normalized
            or "repeated_tool_failure:" in normalized
            or "tool_payload_truncated:" in normalized
        )

    @staticmethod
    def _read_only_tool_trace(worker_result: dict) -> bool:
        """Return whether a Worker result contains evidence but no mutation."""
        calls = list(worker_result.get("tool_calls") or [])
        if not calls:
            return False
        write_tools = {
            "write_file", "apply_patch", "insert_before", "insert_after",
            "write_docx", "write_pptx", "write_pdf", "promote_artifact",
            "write_temp_file",
        }
        read_tools = {
            "list_files", "read_file", "read_pdf", "read_docx", "read_pptx",
            "search_in_file", "search_code", "git_status", "git_diff",
            "read_log",
        }
        names = {
            str(call.get("tool") or call.get("name") or "")
            for call in calls if isinstance(call, dict)
        }
        return bool(names & read_tools) and not bool(names & write_tools)

    @staticmethod
    def _is_provider_capability_error(error: str) -> bool:
        """Return whether a provider rejected a requested model capability."""
        normalized = (error or "").lower()
        markers = (
            "thinking mode does not support this tool_choice",
            "does not support this tool_choice",
            "unsupported tool_choice",
        )
        return any(marker in normalized for marker in markers)

    @classmethod
    def _execution_failure_summary(
        cls, results: dict, blocked: list[str] | None = None,
    ) -> dict:
        """Choose a Job reason from the direct failure matching its status.

        Dependency-blocked tasks describe consequences, not root causes.  When
        several independent tasks stop differently, a user-action failure must
        provide the reason for a ``needs_attention`` Job instead of being paired
        with an unrelated strategy-stall or validation message.
        """
        blocked_set = set(blocked or [])
        failed = [
            task_id for task_id, result in results.items()
            if isinstance(result, dict) and (
                result.get("status") in {
                    "failed", "blocked", "needs_continuation",
                    "needs_user_action",
                }
                or (
                    bool(str(result.get("error") or "").strip())
                    and result.get("status") != "completed"
                )
            )
        ]
        direct_failures = [
            task_id for task_id in failed if task_id not in blocked_set
        ]

        def error_for(task_id: str) -> str:
            result = results.get(task_id)
            return str(result.get("error", "")) if isinstance(result, dict) else ""

        continuation_tasks = [
            task_id for task_id in direct_failures
            if results[task_id].get("status") == "needs_continuation"
        ]
        attention_tasks = [
            task_id for task_id in direct_failures
            if results[task_id].get("status") == "needs_user_action"
            or cls._is_user_action_required(error_for(task_id))
        ]
        if attention_tasks:
            terminal_status = "needs_attention"
            reason_tasks = attention_tasks
        elif continuation_tasks:
            terminal_status = "interrupted"
            reason_tasks = continuation_tasks
        else:
            terminal_status = "failed"
            reason_tasks = direct_failures or failed
        reasons = [error_for(task_id) for task_id in reason_tasks]
        reason = next((value for value in reasons if value.strip()), "未知错误")[:1000]
        return {
            "failed": failed,
            "direct_failures": direct_failures,
            "continuation_tasks": continuation_tasks,
            "attention_tasks": attention_tasks,
            "terminal_status": terminal_status,
            "reason": reason,
        }

    @staticmethod
    def _is_provider_unavailable(error: str) -> bool:
        """Return true only for failures that justify changing providers.

        Strategy/tool failures, generic server errors and malformed model output
        stay on the configured provider. Automatic switching is reserved for a
        timed-out request, throttling/overload, an unavailable model endpoint,
        or authentication/permission failure.
        """
        normalized = (error or "").lower()
        markers = (
            "error code: 401", "status code: 401",
            "error code: 403", "status code: 403", "invalid api key",
            "authentication", "authorization", "missing credentials",
            "credentials were not found", "credentials unavailable",
            "timed out", "timeout", "rate limit", "too many requests",
            "error code: 429", "status code: 429", "overloaded",
            "not found the model", "model not found",
            "resource_not_found_error", "permission denied",
            "error code: 404", "status code: 404",
        )
        return any(marker in normalized for marker in markers)

    @staticmethod
    def _is_model_configuration_error(error: str) -> bool:
        """Return whether every attempted request rejected the model identity."""
        normalized = str(error or "").lower()
        markers = (
            "not found the model", "model not found",
            "resource_not_found_error", "unknown model",
            "model does not exist", "model is not available",
        )
        return any(marker in normalized for marker in markers) or (
            "permission denied" in normalized
            and ("model" in normalized or "404" in normalized)
        )

    @staticmethod
    def _is_transient_provider_error(error: str) -> bool:
        normalized = str(error or "").lower()
        return any(marker in normalized for marker in (
            "timed out", "timeout", "rate limit", "too many requests",
            "overloaded", "service unavailable", "connection error",
            "context length", "context window", "maximum context",
        ))

    @classmethod
    def _is_user_action_required(cls, error: str) -> bool:
        """Reserve needs_attention for an action only the user can perform."""
        normalized = str(error or "").lower()
        if cls._is_model_configuration_error(error):
            return True
        # Git/worktree mechanics are RockCore implementation details. They may
        # fail the run and preserve a checkpoint, but must never require a user
        # who does not know Git to repair repository internals.
        git_markers = (
            "git isolation failed", "git integration", "merge conflict",
            "unresolved git", "internal git", ".git/index.lock",
            "index.lock", "repository lock", "worktree",
        )
        if any(marker in normalized for marker in git_markers):
            return False
        if cls._is_user_input_required(error):
            return True
        markers = (
            "invalid api key", "authentication", "authorization required",
            "missing credentials", "credentials were not found",
            "credentials unavailable",
            "approval required", "insufficient balance", "insufficient_balance",
            "insufficient_quota", "quota exceeded", "billing",
            "billable api hard cost", "cost limit would be exceeded",
            "permission denied", "access denied",
            "context length", "context window", "maximum context",
            "需要用户", "只能由用户", "用户提供", "需要授权",
        )
        return any(marker in normalized for marker in markers)

    async def _run_worker_fallback(self, worker, task, worktree_root: str,
                                   original_error: str) -> dict | None:
        """Try one tool-capable alternate worker when the primary provider is unavailable."""
        fallback_errors = []
        configured_provider = str(
            getattr(task, "_rockcore_fallback_provider", "kimi") or "kimi"
        )
        configured_model = str(
            getattr(task, "_rockcore_fallback_model", "kimi-k2.7-code")
            or "kimi-k2.7-code"
        )
        for provider in (configured_provider,):
            if not self.model_router.has_provider(provider):
                continue
            await self.event_bus.publish(
                "task_provider_fallback", task_id=task.task_id,
                from_provider="deepseek", to_provider=provider,
                reason=original_error[:200],
            )
            try:
                result = await worker.run(
                    task,
                    project_root=worktree_root,
                    provider_override=provider,
                    model_override=configured_model,
                    recovery_context=(
                        "The primary worker provider failed before completing this task. "
                        "Use the existing project state and finish the task with focused "
                        "tool calls; do not repeat broad exploration."
                    ),
                )
            except Exception as error:
                fallback_errors.append(f"{provider}: {error}")
                continue
            if result and result.get("status") == "completed":
                return {"status": "completed", "result": result,
                        "fallback_provider": provider}
            if result and result.get("error"):
                fallback_errors.append(f"{provider}: {result['error']}")
        if fallback_errors:
            logger.warning("Worker fallback attempts failed: %s", "; ".join(fallback_errors))
            all_model_errors = all(
                self._is_model_configuration_error(error)
                for error in [original_error, *fallback_errors]
            )
            return {
                "status": "failed",
                "error": (
                    f"Primary worker provider failed: {original_error}; "
                    f"fallback attempts failed: {'; '.join(fallback_errors)}"
                ),
                "all_models_unavailable": all_model_errors,
            }
        return None

    async def _repair_plan(self, job, failed_task, error, repos) -> dict | None:
        """L2: Use Kimi Planner to create a repair plan for the failed task."""
        planner = self.get_agent("planner")
        if not planner:
            return None

        try:
            repair_context = {
                "failed_task_id": failed_task.task_id,
                "failed_task_title": failed_task.title,
                "error": error,
                "original_description": failed_task.description,
            }
            repair_plan = await planner.repair_plan(job, repair_context)
            return repair_plan
        except BudgetExceededError:
            raise
        except Exception as e:
            logger.error(f"Repair planning failed: {e}")
            return None

    async def _apply_repair_plan_paths(self, task, job, repos,
                                       repair_plan: dict) -> bool:
        """Apply safe path corrections from a focused repair plan."""
        repair_tasks = repair_plan.get("tasks") or []
        proposed_paths = [
            path
            for repair_task in repair_tasks
            for path in (repair_task.get("allowed_paths") or [])
        ]
        if not proposed_paths:
            return False

        project_root = job.project.root_path if job.project else "."
        normalized_paths = self._normalize_paths(proposed_paths, project_root)
        combined_paths = list(dict.fromkeys([
            *(task.allowed_paths or []),
            *normalized_paths,
        ]))
        constitution = repos["constitution"].get_by_job(job.id)
        errors = self.policy_engine.check_task_plan(
            {"tasks": [{
                "id": task.task_id,
                "allowed_paths": combined_paths,
            }]},
            {"protected_paths": (
                constitution.protected_paths if constitution else []
            )},
        )
        if errors:
            logger.warning(
                "Repair plan paths rejected for %s: %s", task.task_id, errors
            )
            await self.event_bus.publish(
                "task_refinement_rejected",
                job_id=job.job_id,
                task_id=task.task_id,
                errors=errors,
            )
            return False

        repos["task"].update_definition(
            task.id, allowed_paths=combined_paths
        )
        task.allowed_paths = combined_paths
        await self.event_bus.publish(
            "task_refined",
            job_id=job.job_id,
            task_id=task.task_id,
            allowed_paths=combined_paths,
            paths_changed=True,
            source="repair_plan",
        )
        return True

    async def _escalate_to_emergency(
        self, task, job, error, worktree_root: str
    ) -> dict | None:
        """Run L3 repair inside the same isolated task worktree."""
        emergency = self.get_agent("emergency_coder")
        if not emergency:
            return None

        task._rockcore_recovery_context = json.dumps({
            "job_request": getattr(job, "user_request", ""),
            "task": {
                "id": getattr(task, "task_id", ""),
                "title": getattr(task, "title", ""),
                "description": getattr(task, "description", ""),
                "allowed_paths": getattr(task, "allowed_paths", []) or [],
                "acceptance_command": getattr(task, "acceptance_command", ""),
            },
            "previous_validation_error": str(error)[:5000],
            "saved_result": dict(getattr(task, "result_data", None) or {}),
            "retry_evidence": str(
                getattr(task, "_rockcore_retry_evidence", "") or ""
            ),
        }, ensure_ascii=False, default=str)[:16000]

        try:
            result = await emergency.run(
                task,
                job.project,
                previous_error=error,
                project_root=worktree_root,
            )
            return result
        except Exception as e:
            logger.error(f"Emergency coder failed: {e}")
            return None

    async def _run_reviewer(self, job, repos,
                            proj_config: ProjectAgentConfig | None = None,
                            complexity: str = "normal"):
        """Review, plan actionable rework, execute it, and review again."""
        repair_round = 0

        while True:
            if self._is_cancelled(job.job_id, job, repos):
                return
            self.state_machine.transition(job.job_id, JobState.REVIEWING)
            repos["job"].update_status(job.job_id, "reviewing")
            self.model_router.cost_engine.release_workflow_reservations(
                job.job_id
            )
            await self.event_bus.publish(
                "job_reviewing", job_id=job.job_id,
                repair_round=repair_round,
            )
            review_budget = self.model_router.cost_engine.reserve_review_budget(
                job.job_id, repair_round
            )
            await self.event_bus.publish(
                "budget_reserved",
                job_id=job.job_id,
                phase="review",
                round=repair_round,
                max_input_tokens=review_budget.max_input_tokens,
                max_cost_cny=review_budget.max_cost_cny,
            )
            if self._is_cancelled(job.job_id, job, repos):
                return

            reviewer = self.get_agent("reviewer")
            if not reviewer:
                summary = "审核者不可用：未注册审核者"
                review_result = {
                    "result": "error",
                    "severity": "high",
                    "summary": summary,
                    "issues": [{"problem": summary, "severity": "high"}],
                    "constraint_violations": [],
                    "suggested_actions": [],
                }
            else:
                try:
                    job._rockcore_review_context = self._review_context(
                        job, repos
                    )
                    review_result = await reviewer.run(job)
                except Exception as error:
                    logger.warning("Reviewer failed: %s", error)
                    summary = f"审核者不可用：{str(error)[:300]}"
                    review_result = {
                        "result": "error",
                        "severity": "high",
                        "summary": summary,
                        "issues": [{"problem": summary, "severity": "high"}],
                        "constraint_violations": [],
                        "suggested_actions": [],
                    }

            review_result.setdefault("result", "error")
            review_result.setdefault("severity", "medium")
            review_result.setdefault("summary", "")
            review_result.setdefault("issues", [])
            review_result.setdefault("constraint_violations", [])
            review_result.setdefault("suggested_actions", [])
            repos["review"].create(
                job_id=job.id,
                result=review_result["result"],
                severity=review_result["severity"],
                issues=review_result["issues"],
                constraint_violations=review_result["constraint_violations"],
                suggested_actions=review_result["suggested_actions"],
                summary=review_result["summary"],
            )
            await self.event_bus.publish(
                "review_complete", job_id=job.job_id,
                result=review_result["result"],
                summary=review_result["summary"],
                issues=review_result["issues"],
                repair_round=repair_round,
            )

            if self._is_cancelled(job.job_id, job, repos):
                return

            review_status = review_result["result"]
            if review_status == "pass":
                if repair_round:
                    plan = repos["plan"].get_by_job(job.id)
                    rounds = list((plan.raw_output or {}).get("repair_rounds") or []) if plan else []
                    if rounds:
                        last_round = dict(rounds[-1])
                        last_round.update({
                            "status": "passed",
                            "final_review_summary": review_result["summary"],
                        })
                        repos["plan"].upsert_repair_round(job.id, last_round)
                self.state_machine.transition(job.job_id, JobState.DONE)
                repos["job"].update_status(job.job_id, "done")
                repos["job"].clear_failure(job.job_id)
                await self.event_bus.publish(
                    "phase_summary",
                    phase="reviewer", agent_type="reviewer", status="success",
                    summary=(
                        f"修复后审核通过：{review_result['summary'][:200]}"
                        if repair_round else "审核通过"
                    ),
                    repair_round=repair_round,
                )
                await self.event_bus.publish("job_done", job_id=job.job_id)
                return review_result

            logger.warning("Job %s: review %s", job.job_id, review_status.upper())
            await self.event_bus.publish(
                "phase_summary",
                phase="reviewer", agent_type="reviewer",
                status="failed" if review_status == "error" else "rejected",
                summary=(
                    f"审核失败：{review_result['summary'][:200]}"
                    if review_status == "error" else
                    f"审核未通过，正在交由策划者判断能否修复："
                    f"{review_result['summary'][:200]}"
                ),
                details={"issues": review_result["issues"]},
                repair_round=repair_round,
            )

            # An unavailable reviewer produced no trustworthy findings to repair.
            if review_status == "error":
                reason = (
                    "审核者未能完成审核，没有有效审核意见可供策划者生成修复计划。"
                    f"{review_result['summary']}"
                )
                if repair_round:
                    self._update_latest_repair_round(
                        repos, job.id,
                        status="review_error", reason=reason,
                    )
                await self._finish_review_failure(
                    job, repos, review_result, reason, status="failed",
                    repair_round=repair_round,
                )
                return review_result

            if repair_round >= MAX_REVIEW_REPAIR_ROUNDS:
                reason = (
                    f"已完成 {repair_round} 轮自动修复，但审核仍未通过："
                    f"{review_result['summary']}"
                )
                self._update_latest_repair_round(
                    repos, job.id,
                    status="review_rejected", reason=reason,
                )
                await self._finish_review_failure(
                    job, repos, review_result, reason, status="rejected",
                    repair_round=repair_round,
                )
                return review_result

            if repair_round:
                reason = (
                    f"第 {repair_round} 轮修复后审核仍未通过："
                    f"{review_result['summary']}"
                )
                self._update_latest_repair_round(
                    repos, job.id,
                    status="review_rejected",
                    reason=reason,
                    final_review_summary=review_result["summary"],
                )

            repair_round += 1
            repair_outcome = await self._attempt_review_repair(
                job, repos, review_result,
                round_number=repair_round,
                proj_config=proj_config,
                complexity=complexity,
            )
            if repair_outcome.get("status") != "completed":
                if self._is_cancelled(job.job_id, job, repos):
                    return review_result
                reason = repair_outcome.get("reason") or "策划者未能完成审核修复"
                outcome_status = repair_outcome.get("status", "")
                await self._finish_review_failure(
                    job, repos, review_result, reason, status="rejected",
                    repair_round=repair_round,
                    agent_type=(
                        "worker" if outcome_status == "execution_failed"
                        else "planner"
                    ),
                    requires_user_action=(
                        True if outcome_status == "needs_user_action" else None
                    ),
                )
                return review_result

    async def _attempt_review_repair(self, job, repos, review_result: dict,
                                     round_number: int,
                                     proj_config: ProjectAgentConfig | None,
                                     complexity: str) -> dict:
        """Ask the Planner whether a rejection is repairable, then execute its plan."""
        planner = self.get_agent("planner")
        precondition_reason = ""
        if proj_config is not None and not proj_config.auto_repair:
            precondition_reason = (
                "项目配置已关闭“失败自动修复”，因此审核未通过后不能自动修改"
            )
        elif proj_config is not None and not proj_config.planner.enabled:
            precondition_reason = (
                "项目配置未启用策划者，无法判断审核问题能否完成修改"
            )
        elif not planner or not hasattr(planner, "plan_review_repair"):
            precondition_reason = "未注册支持审核修复判断的策划者"

        if precondition_reason:
            self.state_machine.transition(job.job_id, JobState.REWORK)
            repos["job"].update_status(job.job_id, "rework")
            repair_record = {
                "round": round_number,
                "review_summary": review_result.get("summary", ""),
                "review_issues": review_result.get("issues", []),
                "repairable": False,
                "requires_user_action": True,
                "reason": precondition_reason,
                "status": "unrepairable",
                "plan": {"summary": "", "tasks": []},
            }
            repos["plan"].upsert_repair_round(job.id, repair_record)
            await self.event_bus.publish(
                "phase_summary",
                phase="planner", agent_type="planner", status="rejected",
                summary=f"无法进入自动修复：{precondition_reason}",
                details={"reason": precondition_reason, "repair_round": round_number},
                repair_round=round_number,
            )
            return {
                "status": "needs_user_action",
                "reason": precondition_reason,
            }

        self.state_machine.transition(job.job_id, JobState.REWORK)
        repos["job"].update_status(job.job_id, "rework")
        repair_budget = self.model_router.cost_engine.reserve_repair_budget(
            job.job_id, round_number
        )
        await self.event_bus.publish(
            "review_repair_assessing",
            job_id=job.job_id,
            repair_round=round_number,
            review_summary=review_result.get("summary", ""),
        )
        await self.event_bus.publish(
            "budget_reserved",
            job_id=job.job_id,
            phase="review_repair",
            round=round_number,
            max_input_tokens=repair_budget.max_input_tokens,
                max_cost_cny=repair_budget.max_cost_cny,
        )

        constitution = repos["constitution"].get_by_job(job.id)
        try:
            decision = await planner.plan_review_repair(
                job, review_result, constitution=constitution,
                round_number=round_number,
            )
        except BudgetExceededError as error:
            reason = f"自动修复未启动：{error}"
            repair_record = {
                "round": round_number,
                "review_summary": review_result.get("summary", ""),
                "review_issues": review_result.get("issues", []),
                "repairable": False,
                "reason": reason,
                "status": "assessment_failed",
                "plan": {"summary": "", "tasks": []},
            }
            repos["plan"].upsert_repair_round(job.id, repair_record)
            await self.event_bus.publish(
                "review_repair_failed",
                job_id=job.job_id,
                repair_round=round_number,
                reason=reason,
            )
            return {"status": "assessment_failed", "reason": reason}
        if not isinstance(decision, dict):
            decision = {
                "repairable": False,
                "reason": "策划者没有返回有效的可修复性判断",
                "plan": {"summary": "", "tasks": []},
                "assessment_error": True,
            }
        reason = str(decision.get("reason") or "").strip()
        repair_record = {
            "round": round_number,
            "review_summary": review_result.get("summary", ""),
            "review_issues": review_result.get("issues", []),
            "repairable": bool(decision.get("repairable")),
            "requires_user_action": bool(
                decision.get("requires_user_action")
            ),
            "reason": reason,
            "status": "assessed",
            "plan": decision.get("plan") or {},
        }

        if not decision.get("repairable"):
            repair_record["status"] = (
                "assessment_failed" if decision.get("assessment_error")
                else "unrepairable"
            )
            repos["plan"].upsert_repair_round(job.id, repair_record)
            await self.event_bus.publish(
                "phase_summary",
                phase="planner", agent_type="planner",
                status="failed" if decision.get("assessment_error") else "rejected",
                summary=f"策划者判断无法自动完成修改：{reason}",
                details={"reason": reason, "repair_round": round_number},
                repair_round=round_number,
            )
            return {
                "status": (
                    "needs_user_action"
                    if decision.get("requires_user_action")
                    else repair_record["status"]
                ),
                "reason": reason,
            }

        plan_data = self._namespace_repair_plan(
            decision.get("plan") or {}, round_number
        )
        self._serialize_overlapping_tasks(
            plan_data, getattr(job, "_rockcore_project_surface", None)
        )
        self._prune_transitive_dependencies(plan_data)
        self._assign_plan_skills(plan_data)
        repair_record["plan"] = plan_data

        self.state_machine.transition(job.job_id, JobState.PLANNING)
        repos["job"].update_status(job.job_id, "planning")
        await self.event_bus.publish(
            "job_planning", job_id=job.job_id,
            repair_round=round_number,
        )
        self.state_machine.transition(job.job_id, JobState.PLAN_CHECK)
        errors = self.policy_engine.check_task_plan(
            plan_data,
            {"protected_paths": constitution.protected_paths if constitution else []},
        )
        if errors:
            plan_reason = f"修复计划未通过约束检查：{errors[0]}"
            repair_record.update({
                "status": "plan_rejected",
                "reason": plan_reason,
                "validation_errors": errors,
            })
            repos["plan"].upsert_repair_round(job.id, repair_record)
            await self.event_bus.publish(
                "phase_summary",
                phase="planner", agent_type="planner", status="rejected",
                summary=plan_reason,
                details={"errors": errors},
                repair_round=round_number,
            )
            return {"status": "plan_rejected", "reason": plan_reason}

        existing_tasks = repos["task"].list_by_job(job.id)
        self._create_tasks_from_plan(
            job, repos, plan_data, order_offset=len(existing_tasks)
        )
        repair_task_ids = {
            task.get("id") for task in plan_data.get("tasks", []) if task.get("id")
        }
        repair_record["status"] = "planned"
        repos["plan"].upsert_repair_round(job.id, repair_record)
        self.state_machine.transition(job.job_id, JobState.READY)
        await self.event_bus.publish(
            "plan_ready", job_id=job.job_id,
            repair_round=round_number,
            task_ids=sorted(repair_task_ids),
        )
        await self.event_bus.publish(
            "phase_summary",
            phase="planner", agent_type="planner", status="success",
            summary=f"策划者判断可以修复：{reason}",
            details={"tasks": plan_data.get("tasks", [])},
            repair_round=round_number,
        )

        project_root = job.project.root_path if job.project else "."
        repair_baseline = self.test_manager.capture_snapshot(project_root)
        repair_record["status"] = "executing"
        repos["plan"].upsert_repair_round(job.id, repair_record)
        execution_result = await self._run_execution(
            job, repos, repair_baseline,
            proj_config=proj_config,
            complexity=complexity,
            task_ids=repair_task_ids,
            repair_round=round_number,
        )
        repos["_session"].refresh(job)
        if not isinstance(execution_result, dict):
            execution_result = {
                "status": "failed",
                "reason": "修复执行没有返回有效结果",
            }
        if execution_result.get("status") != "completed":
            execution_reason = (
                execution_result.get("reason")
                or "修复任务执行后没有达到完成状态"
            )
            repair_record.update({
                "status": "execution_failed",
                "reason": execution_reason,
            })
            repos["plan"].upsert_repair_round(job.id, repair_record)
            await self.event_bus.publish(
                "review_repair_failed", job_id=job.job_id,
                repair_round=round_number,
                reason=execution_reason,
            )
            return {"status": "execution_failed", "reason": execution_reason}

        repair_record["status"] = "executed"
        repos["plan"].upsert_repair_round(job.id, repair_record)
        await self.event_bus.publish(
            "review_repair_executed", job_id=job.job_id,
            repair_round=round_number,
            task_ids=sorted(repair_task_ids),
        )
        return {"status": "completed", "task_ids": sorted(repair_task_ids)}

    async def _finish_review_failure(self, job, repos, review_result: dict,
                                     reason: str, status: str,
                                     repair_round: int = 0,
                                     agent_type: str = "reviewer",
                                     requires_user_action: bool | None = None):
        """Persist and publish a review failure with an actionable explanation."""
        if self._is_cancelled(job.job_id, job, repos):
            return

        if requires_user_action is None:
            requires_user_action = self._is_user_action_required(reason)
        terminal_status = "needs_attention" if requires_user_action else "failed"
        repos["job"].update_status(job.job_id, terminal_status)
        self._store_job_failure(repos, job.job_id, reason)
        target_state = (
            JobState.WAITING_USER if requires_user_action else JobState.FAILED
        )
        if self.state_machine.get_state(job.job_id) != target_state:
            if not self.state_machine.transition(job.job_id, target_state):
                logger.warning(
                    "Could not transition %s to %s from %s",
                    job.job_id,
                    target_state.name,
                    self.state_machine.get_state(job.job_id).name,
                )
        await self.event_bus.publish(
            "phase_summary",
            phase="reviewer", agent_type=agent_type, status=status,
            summary=f"审核修复未完成：{reason[:300]}",
            details={
                "issues": review_result.get("issues", []),
                "reason": reason,
            },
            repair_round=repair_round,
        )
        await self.event_bus.publish(
            "job_needs_attention" if requires_user_action else "job_failed",
            job_id=job.job_id,
            **({"reason": reason} if requires_user_action else {"error": reason}),
            failure_stage="review_user_action_required"
            if requires_user_action else "review_repair_incomplete",
            repair_round=repair_round,
        )

    @staticmethod
    def _review_context(job, repos) -> str:
        """Build the complete, bounded workflow record used by Reviewer."""
        constitution = repos["constitution"].get_by_job(job.id)
        plan = repos["plan"].get_by_job(job.id)
        tasks = repos["task"].list_by_job(job.id)
        payload = {
            "original_request": job.user_request,
            "risk_level": getattr(job, "risk_level", "medium"),
            "constitution": ({
                "goal": constitution.goal,
                "constraints": constitution.constraints or [],
                "acceptance_criteria": constitution.acceptance_criteria or [],
                "protected_paths": constitution.protected_paths or [],
                "risk": constitution.risk,
            } if constitution else {}),
            "plan_summary": plan.summary if plan else "",
            "tasks": [],
        }
        for task in tasks:
            test_runs = repos["test_run"].list_by_task(task.id)
            payload["tasks"].append({
                "id": task.task_id,
                "title": task.title,
                "description": task.description,
                "status": task.status,
                "allowed_paths": task.allowed_paths or [],
                "acceptance_command": task.acceptance_command or "",
                "result_summary": (task.result_summary or "")[:1600],
                "failure_reason": (task.failure_reason or "")[:1200],
                "tests": [{
                    "command": test.command,
                    "status": test.status,
                    "output": (test.output or "")[-1600:],
                } for test in test_runs[-4:]],
            })
        return json.dumps(payload, ensure_ascii=False, default=str)[:16000]

    @staticmethod
    def _update_latest_repair_round(repos, job_id: int, **updates):
        plan = repos["plan"].get_by_job(job_id)
        rounds = list((plan.raw_output or {}).get("repair_rounds") or []) if plan else []
        if not rounds:
            return
        latest = dict(rounds[-1])
        latest.update(updates)
        repos["plan"].upsert_repair_round(job_id, latest)

    @classmethod
    def _namespace_repair_plan(cls, plan_data: dict,
                               round_number: int) -> dict:
        """Give repair tasks unique IDs and rewrite their prose references."""
        tasks = copy.deepcopy(plan_data.get("tasks") or [])
        id_map = {}
        for index, task in enumerate(tasks):
            old_id = str(task.get("id") or f"T{index + 1:03d}")
            new_id = f"R{round_number:02d}T{index + 1:03d}"
            id_map[old_id] = new_id
            task["id"] = new_id
            task.setdefault("title", f"审核修复步骤 {index + 1}")
            task.setdefault("type", "coding")
            task.setdefault("description", task["title"])
            task.setdefault("allowed_paths", [])
            task.setdefault("skills", [])
            task.setdefault("acceptance_command", "")

        reference_map = {
            old_id.upper(): new_id for old_id, new_id in id_map.items()
        }
        for task in tasks:
            task["dependencies"] = [
                id_map[dependency]
                for dependency in (task.get("dependencies") or [])
                if dependency in id_map
            ]
            for field in ("title", "description"):
                task[field] = cls._rewrite_task_references(
                    task.get(field), reference_map,
                    str(task.get("id") or ""), set(),
                )
        return {
            "summary": cls._rewrite_task_references(
                plan_data.get("summary", "审核修复计划"),
                reference_map, "", set(),
            ),
            "tasks": tasks,
        }

    @staticmethod
    def _friendly_provider_error(error: str) -> str:
        """Translate common provider failures into concise user-facing reasons."""
        normalized = (error or "").lower()
        if "insufficient balance" in normalized or "insufficient_balance" in normalized:
            return "当前模型供应商 API 余额不足（HTTP 402）"
        if Engine._is_model_configuration_error(error):
            return "模型配置不可用；已尝试同供应商候选模型和可用备用供应商"
        if "credit_balance_exhausted" in normalized or "no credits remaining" in normalized:
            return (
                "Platform API 账户无可用余额，或认证通道配置错误"
                "（这不代表 ChatGPT/Codex 用量耗尽）"
            )
        if "insufficient_quota" in normalized:
            return (
                "Platform API 配额不足（HTTP 429，insufficient_quota；"
                "与 ChatGPT/Codex 订阅用量无关）"
            )
        if "rate limit" in normalized or "too many requests" in normalized:
            return "模型服务触发速率限制（HTTP 429）"
        if "timed out" in normalized or "timeout" in normalized:
            return "模型请求超时"
        return (error or "未知错误")[:300]

    @classmethod
    def _failure_details(cls, error: str) -> tuple[str, str]:
        normalized = (error or "").lower()
        if cls._is_model_configuration_error(error):
            return (
                "model_configuration",
                "模型配置不可用。请在设置中选择供应商当前开放的模型 ID；"
                "保存后点击继续，将恢复同一个执行任务。",
            )
        if cls._is_user_input_required(error):
            if "password" in normalized or "encrypted" in normalized:
                return (
                    "pdf_password_required",
                    "请放入解除密码保护的 PDF，然后点击“继续此需求”；不会重复消耗模型重试。",
                )
            if "ocr" in normalized or "extractable text" in normalized:
                return (
                    "pdf_ocr_required",
                    "请提供可搜索文本版或完成 OCR 的 PDF，然后点击"
                    "“已处理，继续完成任务”。",
                )
            return (
                "user_input_required",
                "补充所需文件或信息后点击“已处理，继续完成任务”，"
                "将从当前检查点恢复。",
            )
        if any(marker in normalized for marker in (
            "insufficient balance", "insufficient_balance",
            "error code: 402", "status code: 402",
        )):
            return (
                "provider_balance",
                "请充值当前模型供应商的 API 余额，或在项目 AI 配置中改用"
                "有可用额度的执行模型；保存后点击“已处理，继续完成任务”，"
                "将从当前任务检查点恢复。",
            )
        if "max turns" in normalized or "turn_limit" in normalized:
            return (
                "worker_turn_limit",
                "本次读取或验证达到 Worker 轮次上限。可在项目设置中提高执行轮次，"
                "或改用更适合的模型，然后点击“已处理，继续完成任务”，"
                "将从当前检查点恢复。",
            )
        if cls._is_budget_error(error):
            return (
                "budget_exceeded",
                "长文档 Token/调用次数预算会自动扩容两次；若仍失败，请检查"
                "可计费 API 成本上限或异常超大输入。已保留完成步骤。"
                "ChatGPT 登录的等价估算成本不会触发人民币 API 预算。",
            )
        if cls._is_provider_capability_error(error):
            return (
                "provider_capability",
                "该供应商已熔断；继续时会切换到兼容工具调用的供应商。",
            )
        if any(marker in normalized for marker in (
            "401", "402", "403", "api key", "authentication",
            "insufficient balance", "quota", "billing", "credentials",
        )):
            return (
                "provider_credentials",
                "检查供应商登录、API Key、余额或配额；已有文件修改不会丢失。",
            )
        if "timeout" in normalized or "timed out" in normalized:
            return (
                "provider_timeout",
                "继续时会优先使用其他可用供应商，并从最近检查点开始。",
            )
        if "acceptance" in normalized or "validation" in normalized or "test" in normalized:
            return (
                "validation_failed",
                "修复验收输出中列出的具体问题后，仅重跑未通过的步骤。",
            )
        if "merge conflict" in normalized:
            return (
                "merge_conflict",
                "RockCore 已保留任务产物和恢复副本；这是内部集成失败，"
                "不会要求你执行 Git 命令。",
            )
        if "without editing" in normalized or "no file changes" in normalized:
            return (
                "no_effective_edit",
                "重新策划时会锁定目标文件并要求先修改、后验证。",
            )
        return (
            "execution_failed",
            "已保留任务检查点；恢复时只重做失败和受阻步骤。",
        )

    def _store_job_failure(self, repos, job_id: str, error: str):
        code, hint = self._failure_details(error)
        repos["job"].set_failure(job_id, code, str(error)[:4000], hint)

    def _checkpoint_task(self, repos, job, task, *, status: str,
                         result: dict | None = None, error: str = ""):
        """Persist resumable task evidence after every terminal task result."""
        payload = json.loads(json.dumps(
            result or {}, ensure_ascii=False, default=str
        ))
        retry_evidence = str(
            getattr(task, "_rockcore_retry_evidence", "") or ""
        ).strip()
        if retry_evidence:
            payload["retry_evidence"] = retry_evidence[:9000]
        summary = str(
            payload.get("output") or payload.get("content")
            or payload.get("reason") or error or status
        )[:4000]
        repos["task"].update_result(
            task.id, summary=summary, data=payload,
            failure_reason=error if status != "done" else "",
        )
        tasks = repos["task"].list_by_job(job.id)
        existing_checkpoint = dict(
            getattr(job, "last_checkpoint", None) or {}
        )
        existing_checkpoint.update({
            "updated_at": datetime.now().astimezone().isoformat(),
            "tasks": [{
                "task_id": item.task_id,
                "status": item.status,
                "summary": (item.result_summary or "")[:1000],
                "failure_reason": (item.failure_reason or "")[:1000],
                "allowed_paths": item.allowed_paths or [],
                "skills": item.skills or [],
            } for item in tasks],
        })
        existing_checkpoint["execution_context"] = {
            "completed_tasks": [{
                "task_id": item.task_id,
                "title": item.title,
                "summary": (item.result_summary or "")[:1200],
                "changed_files": list(dict.fromkeys(
                    str(path) for path in (
                        ((item.result_data or {}).get("changes") or {}).get(
                            "changed"
                        ) or []
                    )
                ))[:30],
            } for item in tasks if item.status == "done"],
            "next_task": next((
                item.task_id for item in tasks
                if item.status in {"pending", "blocked", "interrupted"}
            ), ""),
        }
        session = normalize_session(
            existing_checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=job.user_request,
        )
        update_checklist(session, tasks)
        changes = payload.get("changes") or {}
        session["changed_files"] = list(dict.fromkeys(
            list(session.get("changed_files") or [])
            + [str(path) for path in (changes.get("changed") or [])]
        ))[:100]
        validation = payload.get("validation") or payload.get("test_result")
        if validation:
            session["validation"] = list(session.get("validation") or []) + [{
                "task_id": task.task_id,
                "status": str(validation.get("status") or ""),
                "output": str(
                    validation.get("output") or validation.get("error") or ""
                )[:1600],
            }]
        if status in {"needs_attention", "interrupted", "failed"}:
            session["recoverable_error"] = {
                "task_id": task.task_id,
                "status": status,
                "reason": str(error or payload.get("error") or "")[:1600],
                "next_action": (
                    "Resolve the user-actionable condition and resume this same task"
                    if status == "needs_attention"
                    else "Resume this same task from its saved checkpoint"
                    if status == "interrupted"
                    else "Inspect the internal failure and run automatic repair"
                ),
            }
        elif status == "done":
            session["recoverable_error"] = {}
        existing_checkpoint["execution_session"] = session
        runtime_checkpoint = dict(
            getattr(task, "_rockcore_runtime_checkpoint", None) or {}
        )
        task_runtimes = dict(existing_checkpoint.get("task_runtimes") or {})
        if runtime_checkpoint and status != "done":
            task_runtimes[task.task_id] = runtime_checkpoint
        else:
            task_runtimes.pop(task.task_id, None)
        if task_runtimes:
            existing_checkpoint["task_runtimes"] = task_runtimes
        else:
            existing_checkpoint.pop("task_runtimes", None)
        existing_checkpoint["budget"] = (
            self.model_router.cost_engine.get_budget_snapshot(job.job_id)
        )
        repos["job"].update_checkpoint(job.job_id, existing_checkpoint)

    @staticmethod
    def _execution_continuation_context(job, task,
                                        completed_results: dict[str, dict]) -> str:
        """Carry compact project decisions forward without replaying chat history."""
        checkpoint = dict(getattr(job, "last_checkpoint", None) or {})
        surface = dict(
            getattr(job, "_rockcore_project_surface", None)
            or checkpoint.get("project_surface")
            or {}
        )
        completed_by_id: dict[str, dict] = {}
        for item in (
            (checkpoint.get("execution_context") or {}).get("completed_tasks")
            or []
        ):
            if isinstance(item, dict) and item.get("task_id"):
                completed_by_id[str(item["task_id"])] = dict(item)
        for task_id, result in completed_results.items():
            if not isinstance(result, dict):
                continue
            changes = result.get("changes") or {}
            completed_by_id[str(task_id)] = {
                "task_id": str(task_id),
                "summary": str(
                    result.get("output") or result.get("content")
                    or result.get("completion_note") or "completed"
                )[:1200],
                "changed_files": list(dict.fromkeys(
                    str(path) for path in (changes.get("changed") or [])
                ))[:30],
            }
        payload = {
            "current_task": getattr(task, "task_id", ""),
            "entrypoints": [
                item.get("path") for item in (surface.get("entrypoints") or [])
                if isinstance(item, dict) and item.get("path")
            ],
            "active_files": list(surface.get("active_files") or [])[:60],
            "support_files": list(surface.get("support_files") or [])[:40],
            "legacy_files": list(surface.get("legacy_files") or [])[:30],
            "commands": dict(surface.get("commands") or {}),
            "ambiguities": list(surface.get("ambiguities") or [])[:8],
            "completed_tasks": list(completed_by_id.values())[-8:],
            "worker_runtime": dict(
                (checkpoint.get("worker_runtime") or {}).get(
                    getattr(task, "task_id", ""),
                ) or {}
            ),
        }
        if not any(payload[key] for key in (
            "entrypoints", "active_files", "completed_tasks", "ambiguities",
            "worker_runtime",
        )):
            return ""
        return (
            "=== SHARED EXECUTION CHECKPOINT ===\n"
            "This is authoritative state from deterministic project resolution and "
            "completed stages. Continue from it; do not rediscover unchanged files. "
            "Files under legacy_files are outside the current runtime unless this "
            "task explicitly resolves an ambiguity.\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )

    async def _check_file_changes(self, project_root: str,
                                  baseline_snapshot: dict | None = None) -> bool:
        """Check if any files have been modified/created in the working directory."""
        # A task-local snapshot is authoritative when available. In particular,
        # isolated worktrees may contain copied, untracked source PDFs before
        # execution; plain ``git status`` must not misclassify those inputs as
        # Worker-generated artifacts.
        if baseline_snapshot is not None:
            return bool(
                self.test_manager.snapshot_diff(
                    project_root, baseline_snapshot
                )["changed"]
            )

        # Try git first
        try:
            result = run_process(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, cwd=project_root,
                timeout=5,
            )
            if result.returncode == 0:
                changed = [f for f in result.stdout.split("\n") if f.strip()]
                if changed:
                    logger.info(f"File changes detected: {changed}")
                    return True

            result2 = run_process(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True, cwd=project_root,
                timeout=5,
            )
            if result2.returncode == 0:
                new_files = [f[3:] for f in result2.stdout.split("\n") if f.strip() and f[0] == "?"]
                if new_files:
                    logger.info(f"New files detected: {new_files}")
                    return True
        except Exception:
            pass

        return False

    @staticmethod
    def _normalize_paths(paths: list[str], project_root: str) -> list[str]:
        """Convert absolute paths to relative when they fall within project_root."""
        root = Path(project_root).resolve()
        result = []
        for p in paths:
            pp = Path(p)
            if pp.is_absolute():
                try:
                    result.append(pp.resolve().relative_to(root).as_posix())
                except ValueError:
                    result.append(p)
            else:
                result.append(p)
        return result

    @staticmethod
    def _dedupe_text_values(values, limit: int = 12) -> list[str]:
        """Normalize inherited model observations while preserving order."""
        result = []
        seen = set()
        for value in values or []:
            text = str(value).strip()[:500]
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            result.append(text)
            if len(result) >= limit:
                break
        return result

    def _inherited_image_understanding(self, job, repos) -> dict[str, list[str]]:
        """Collect structured image facts from the explicit continuation chain."""
        goals = []
        observations = []
        for ancestor in self._continuation_ancestors(
            getattr(job, "source_job_id", None), job.project_id, repos
        ):
            constitution = repos["constitution"].get_by_job(ancestor.id)
            if not constitution:
                continue
            raw = dict(constitution.raw_output or {})
            ancestor_observations = raw.get("image_observations") or []
            if not isinstance(ancestor_observations, list):
                ancestor_observations = [ancestor_observations]
            if not ancestor_observations:
                continue
            goals.append(constitution.goal)
            observations.extend(ancestor_observations)
        return {
            "goals": self._dedupe_text_values(goals, limit=8),
            "observations": self._dedupe_text_values(observations, limit=12),
        }

    def _build_continuation_context(self, job, repos) -> str:
        """Build continuation context only from an explicit source job."""
        prev_job = None
        if job.source_job_id:
            candidate = repos["job"].get_by_id(job.source_job_id)
            if candidate and candidate.project_id == job.project_id:
                prev_job = candidate

        if not prev_job:
            return ""

        prev_tasks = repos["task"].list_by_job(prev_job.id)
        prev_files = set()
        task_lines = []
        test_lines = []
        for previous_task in prev_tasks:
            for p in (previous_task.allowed_paths or []):
                # Only include concrete files (not globs)
                if "*" not in p and "." in p:
                    prev_files.add(p)
            summary = (
                previous_task.result_summary
                or previous_task.failure_reason
                or "无已保存摘要"
            )[:800]
            task_lines.append(
                f"- {previous_task.task_id} [{previous_task.status}]: {summary}"
            )
            for test_run in repos["test_run"].list_by_task(previous_task.id)[:1]:
                test_lines.append(
                    f"- {previous_task.task_id}: {test_run.status} · "
                    f"{test_run.command} · {(test_run.output or '')[:500]}"
                )

        if not prev_files:
            # Try to find actual files in the project directory
            import os
            root = job.project.root_path if job.project else "."
            if os.path.isdir(root):
                for f in sorted(os.listdir(root))[:10]:
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp) and not f.startswith("."):
                        prev_files.add(f)

        reviews = repos["review"].list_by_job(prev_job.id)
        review_text = ""
        if reviews:
            latest = reviews[0]
            review_text = (
                f"Latest review: {latest.result} / {latest.severity} · "
                f"{(latest.summary or '')[:1000]}"
            )

        image_understanding = self._inherited_image_understanding(job, repos)
        image_lines = [
            *(f"Goal: {goal}" for goal in image_understanding["goals"]),
            *(
                f"- {observation}"
                for observation in image_understanding["observations"]
            ),
        ]

        context = f"""
=== CONTINUATION CONTEXT ===
This follows an explicit earlier job. Preserve existing useful work.
Do NOT repeat tasks marked done. Replan only failed, blocked, or newly requested work.

Previous job: {prev_job.job_id} [{prev_job.status}]
Previous request: {prev_job.user_request[:500]}
Failure code: {getattr(prev_job, 'failure_code', '') or 'none'}
Failure reason: {(getattr(prev_job, 'failure_reason', '') or 'none')[:1200]}
Recovery hint: {(getattr(prev_job, 'recovery_hint', '') or 'none')[:800]}

=== Task Checkpoint ===
{chr(10).join(task_lines) or '- No task checkpoint'}

=== Latest Test Evidence ===
{chr(10).join(test_lines) or '- No saved test result'}

=== Review Evidence ===
{review_text or 'No saved review'}

=== Inherited Image Understanding ===
{chr(10).join(image_lines) or 'No inherited image observations'}

=== Target Files ===
Prefer these existing files when relevant:
{chr(10).join('- ' + f for f in sorted(prev_files)[:8] if f)}

=== Your Workflow ===
1. Verify the current workspace state with targeted search/read.
2. Reuse completed work; do not recreate or undo it.
3. Address only failed/blocked checkpoints and the new request.
4. Apply a focused patch, then run deterministic validation.
"""
        return context[:9000]

    def _continuation_context(self, job, repos, proj_config=None) -> str:
        if proj_config is not None and not proj_config.continuation_context:
            return ""
        return self._build_continuation_context(job, repos)

    def _is_cancelled(self, job_id: str, job=None, repos=None) -> bool:
        if job_id in self._cancelled_job_ids:
            return True
        if job is not None and repos is not None:
            repos["_session"].refresh(job)
            return job.status == "cancelled"
        return False

    def _request_with_context(self, job, repos, proj_config=None) -> str:
        from app.image_attachments import attachment_context

        context = self._continuation_context(job, repos, proj_config)
        request = job.user_request + attachment_context(
            getattr(job, "attachments", None)
        )
        governed_scope = self._governed_attachment_scope(job, repos)
        if governed_scope:
            request += (
                "\n\n=== GOVERNOR IMAGE UNDERSTANDING ===\n"
                + governed_scope
                + "\nUse these extracted image requirements as task context."
            )
        return request if not context else f"{request}\n{context}"

    @staticmethod
    def _governed_attachment_scope(job, repos) -> str:
        """Return concrete image requirements extracted by the Governor."""
        if not getattr(job, "attachments", None):
            return ""
        constitution = repos["constitution"].get_by_job(job.id)
        if not constitution:
            return ""
        raw = dict(constitution.raw_output or {})
        observations = [
            str(item).strip() for item in raw.get("image_observations", [])
            if str(item).strip()
        ][:12]
        if not observations:
            return ""
        lines = []
        goal = str(constitution.goal or "").strip()
        if goal:
            lines.append(f"Goal: {goal}")
        lines.extend(f"- {item}" for item in observations)
        return "\n".join(lines)

    def _effective_task_title(self, job, repos) -> str:
        """Use the visually resolved goal instead of an attachment placeholder."""
        if self._governed_attachment_scope(job, repos):
            constitution = repos["constitution"].get_by_job(job.id)
            goal = str(getattr(constitution, "goal", "") or "").strip()
            if goal:
                return goal[:60]
        return str(job.user_request or "")[:60]

    async def _run_simple(self, job, repos,
                          proj_config: ProjectAgentConfig | None = None,
                          governor_completed: bool = False):
        """Run one focused task after Governor classifies low risk."""
        if not governor_completed:
            await self.event_bus.publish("phase_summary",
                phase="governor", agent_type="governor", status="skipped",
                summary="快速模式：跳过裁决者并直接执行")
        await self.event_bus.publish("phase_summary",
            phase="planner", agent_type="planner", status="skipped",
            summary="无需模型策划，直接创建单个聚焦任务")

        if not governor_completed:
            self.state_machine.transition(job.job_id, JobState.GOVERNING)
            self.state_machine.transition(job.job_id, JobState.GOVERNED)
        self.state_machine.transition(job.job_id, JobState.PLANNING)
        self.state_machine.transition(job.job_id, JobState.PLAN_CHECK)
        self.state_machine.transition(job.job_id, JobState.READY)

        if self._is_cancelled(job.job_id, job, repos):
            return

        has_continuation = bool(
            self._continuation_context(job, repos, proj_config)
        )
        description = self._request_with_context(job, repos, proj_config)
        task_type = self._request_task_type(job.user_request)

        normalized_risk = (
            "high" if job.risk_level == "critical" else job.risk_level
        )
        if not repos["constitution"].get_by_job(job.id):
            repos["constitution"].create(
                job_id=job.id, goal=job.user_request, constraints=[],
                acceptance_criteria=["确定性验证通过"],
                risk=normalized_risk or "low", protected_paths=[],
                requires_final_review=False,
                raw_output={"source": "fast_mode_rules"},
            )
        direct_task = {
            "title": self._effective_task_title(job, repos),
            "description": description,
            "type": task_type,
            "allowed_paths": list((
                getattr(job, "_rockcore_project_surface", None) or {}
            ).get("active_files") or []) or ["*"],
            "skills": [],
        }
        selected_skills = (
            self.skill_manager.select_for_task(direct_task)
            if self.skill_manager else []
        )
        direct_task["skills"] = selected_skills
        repos["plan"].create(
            job_id=job.id,
            summary=direct_task["title"],
            raw_output={"summary": direct_task["title"], "tasks": [direct_task]},
        )
        repos["task"].create(
            task_id="T001", job_id=job.id, title=direct_task["title"],
            task_type=task_type, description=description,
            allowed_paths=direct_task["allowed_paths"],
            dependencies=[], acceptance_command="", order=0,
            skills=selected_skills,
        )

        worker = self.get_agent("worker")
        if not worker:
            return
        saved_turns = worker.max_turns

        # Use config turn limit, with extra for continuation
        cfg = proj_config or ProjectAgentConfig()
        if has_continuation:
            worker.max_turns = cfg.get_worker_turns("simple") + cfg.worker.patch_recovery_turns
        else:
            worker.max_turns = cfg.get_worker_turns("simple")

        baseline = self.test_manager.capture_snapshot(
            job.project.root_path if job.project else "."
        )
        await self._run_execution(
            job, repos, baseline, proj_config=cfg, complexity="simple"
        )
        repos["_session"].refresh(job)
        if job.status == "cancelled":
            worker.max_turns = saved_turns
            return
        if job.status in {"failed", "interrupted", "needs_attention"}:
            worker.max_turns = saved_turns
            return

        worker.max_turns = saved_turns
        await self.event_bus.publish("phase_summary",
            phase="reviewer", agent_type="reviewer", status="skipped",
            summary="低风险任务已通过确定性验证，跳过模型审核")
        self.state_machine.transition(job.job_id, JobState.REVIEWING)
        self.state_machine.transition(job.job_id, JobState.DONE)
        repos["job"].update_status(job.job_id, "done")
        repos["job"].clear_failure(job.job_id)
        await self.event_bus.publish("job_done", job_id=job.job_id)

    @staticmethod
    def _project_output_files(project_root: str) -> list[str]:
        """Return user-facing project files, including files in subdirectories."""
        root = Path(project_root)
        if not root.is_dir():
            return []

        ignored_dirs = {".git", ".ai", "__pycache__"}
        output_files: list[str] = []
        for path in root.rglob("*"):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part in ignored_dirs or part.startswith(".") for part in relative.parts[:-1]):
                continue
            if not path.is_file() or path.name.startswith("."):
                continue
            output_files.append(relative.as_posix())
        return sorted(output_files)

    async def _finalize(self, job, repos):
        """Publish one authoritative terminal status after diagnostic checks."""
        repos["_session"].refresh(job)
        tasks = repos["task"].list_by_job(job.id)
        public_summary = next((
            str(task.result_summary or "") for task in reversed(tasks)
            if str(task.result_summary or "").strip()
        ), "")
        checkpoint = dict(job.last_checkpoint or {})
        session = normalize_session(
            checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=job.user_request,
        )
        record_turn(
            session, job_id=job.job_id, request=job.user_request,
            status=job.status, summary=public_summary,
        )
        session["conversation_summary"] = (
            public_summary or session.get("conversation_summary") or ""
        )[:4000]
        checkpoint["execution_session"] = session
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        repos["_session"].refresh(job)
        if (
            checkpoint.get("main_agent_assessment")
            and job.status in {
                "done", "failed", "cancelled", "interrupted",
                "needs_attention", "rolled_back",
            }
        ):
            model_summary = await self.main_agent.summarize_turn(job, repos)
            if model_summary:
                repos["_session"].refresh(job)
        if job.status == "done" and job.project:
            root = job.project.root_path
            output_files = self._project_output_files(root)
            logger.info(
                "[output_verify] project=%s files=%s: %s",
                root, len(output_files), output_files[:5],
            )
            if not output_files:
                # Coding tasks already enforce and merge their required output,
                # while analysis-only jobs may legitimately produce no file.
                # A diagnostic check must not overwrite a reviewed DONE state.
                logger.warning(
                    "[output_verify] Job %s has no user-facing project files; "
                    "keeping reviewed terminal status %s",
                    job.job_id, job.status,
                )
                await self.event_bus.publish(
                    "output_verification_warning",
                    job_id=job.job_id,
                    project_root=root,
                    warning="项目中未检测到用户文件；已保留审核后的完成状态",
                )

        if job.project:
            config = load_project_config(job.project.root_path)
            await self._run_project_hooks(
                config, "after_job", job_id=job.job_id,
                project_root=job.project.root_path,
            )
        await self.event_bus.publish(
            "job_finished", job_id=job.job_id, status=job.status
        )

    async def pause_job(self, job_id: str):
        runtime = self._job_runtimes.get(job_id)
        scheduler = runtime.scheduler if runtime else self._default_scheduler
        scheduler.pause()
        await self.event_bus.publish("job_paused", job_id=job_id)

    async def resume_job(self, job_id: str):
        runtime = self._job_runtimes.get(job_id)
        scheduler = runtime.scheduler if runtime else self._default_scheduler
        pending = scheduler.resume()
        await self.event_bus.publish("job_resumed", job_id=job_id,
                                      pending_count=len(pending))

    async def cancel_job(self, job_id: str):
        repos = self._get_repos()
        try:
            self._cancelled_job_ids.add(job_id)
            runtime = self._job_runtimes.get(job_id)
            scheduler = runtime.scheduler if runtime else self._default_scheduler
            scheduler.stop()
            self.state_machine.transition(job_id, JobState.CANCELLED)
            repos["job"].update_status(job_id, "cancelled")
            await self.event_bus.publish("job_cancelled", job_id=job_id)
        finally:
            self._close_repos(repos)
