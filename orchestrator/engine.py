"""Main orchestrator engine — the brain of the AI Engineering Studio."""

import asyncio
import fnmatch
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_FLASH_RETRY = 2
MAX_REPLAN_RETRY = 1
MAX_REVIEW_REPAIR_ROUNDS = 2


# Keywords that signal higher complexity
COMPLEX_KEYWORDS = [
    "数据库", "database", "迁移", "migration", "并发", "concurrent",
    "认证", "auth", "安全", "security", "重构", "refactor",
    "微服务", "microservice", "API", "接口", "支付", "payment",
    "多线程", "multithread", "分布式", "distributed",
]
SIMPLE_KEYWORDS = [
    "html", "HTML", "网页", "页面", "css", "CSS", "静态",
    "修改", "fix", "修复", "bug", "小", "简单", "一个",
    "显示", "展示", "调整", "改一下", "加个",
]

from .event_bus import EventBus
from .state_machine import StateMachine, JobState
from .scheduler import Scheduler
from .policy_engine import PolicyEngine
from .model_router import ModelRouter
from .cost_engine import BudgetExceededError
from .test_manager import TestManager
from .merge_manager import MergeManager
from .agent_config import ProjectAgentConfig, load_project_config
from storage.database import create_session_factory
from storage.repositories import (
    ProjectRepository, JobRepository, ConstitutionRepository,
    PlanRepository, TaskRepository, AgentRunRepository,
    ToolCallRepository, TestRunRepository, ReviewRepository
)
from git.repository import Repository

logger = logging.getLogger(__name__)


class Engine:
    """Central orchestrator that coordinates all agents and tools."""

    def __init__(self, db_path: str | None = None,
                 max_concurrent_workers: int = 3):
        from storage.database import init_database
        self._engine = init_database(db_path)
        self._session_factory = create_session_factory(self._engine)

        self.event_bus = EventBus()
        self.event_bus.subscribe("model_chat", self._record_model_usage)
        self.state_machine = StateMachine()
        self.scheduler = Scheduler(
            max_concurrent=max(1, int(max_concurrent_workers or 1))
        )
        self.policy_engine = PolicyEngine()
        self.model_router = ModelRouter(event_bus=self.event_bus)

        self._running = False
        self._current_job_id: str | None = None
        self._agents: dict[str, Any] = {}
        self._cancelled_job_ids: set[str] = set()
        self.tool_broker: Any = None
        self.test_manager = TestManager()
        self.merge_manager: MergeManager | None = None

        # Wire state machine to event bus
        self.state_machine.add_listener(self._on_state_change_sync)

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

    def get_agent(self, agent_type: str):
        return self._agents.get(agent_type)

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
                "needs_attention",
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
        logger.info("Engine started")

    async def stop(self):
        self._running = False
        self.scheduler.stop()
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
        """Classify a user request as simple, normal, or complex."""
        req_lower = user_request.lower()
        has_complex = any(kw.lower() in req_lower for kw in COMPLEX_KEYWORDS)
        has_simple = any(kw in user_request or kw.lower() in req_lower for kw in SIMPLE_KEYWORDS)

        if has_complex:
            return "complex"
        if has_simple and len(user_request) < 200:
            return "simple"
        return "normal"

    # ── Job Lifecycle ──────────────────────────────────────────

    async def create_job(self, project_id: int, user_request: str,
                         project_root: str, risk_level: str = "medium",
                         source_job_id: str | None = None) -> dict:
        repos = self._get_repos()
        try:
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

            job = repos["job"].create(
                job_id_str, project_id, user_request, risk_level, source_job_id
            )
            self._cancelled_job_ids.discard(job_id_str)
            self.state_machine.transition(job_id_str, JobState.CREATED)

            # Create git branch
            branch = f"ai/{job_id_str.lower()}"
            await self.event_bus.publish("job_created", job_id=job_id_str,
                                          branch=branch, project_root=project_root)

            return {"job_id": job_id_str, "branch": branch, "pk": job.id}
        finally:
            self._close_repos(repos)

    async def run_job(self, job_id: str, project_root: str):
        """Run the full job lifecycle: Governor → Planner → Worker → Test → Reviewer."""
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
            unmerged_files = repository.unmerged_files()
            if unmerged_files:
                error = (
                    "Project has unresolved Git conflicts: "
                    + ", ".join(unmerged_files[:8])
                )
                repos["job"].update_status(job.job_id, "failed")
                self._store_job_failure(repos, job.job_id, error)
                self.state_machine.transition(job.job_id, JobState.GOVERNING)
                self.state_machine.transition(job.job_id, JobState.FAILED)
                await self.event_bus.publish(
                    "job_failed", job_id=job.job_id, error=error
                )
                return
            self.merge_manager = MergeManager(project_root)
            job_baseline = self.test_manager.capture_snapshot(project_root)

            # Load project-level AI config
            proj_root = job.project.root_path if job.project else project_root
            proj_config = load_project_config(proj_root)
            logger.info(f"Job {job_id}: mode={proj_config.mode}")

            # Classify request complexity
            complexity = self._classify_request(job.user_request)
            job._rockcore_complexity = complexity
            logger.info(f"Job {job_id}: complexity={complexity}")

            precheck = self.model_router.risk_engine.precheck_request(
                job.user_request, proj_root
            )

            profiles = {
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
            if proj_config.mode == "fast":
                # Fast mode explicitly opts out of model governance.
                risk_assessment = {
                    "risk": precheck["level"],
                    "risk_score": precheck["score"],
                    "risk_reasons": precheck["reasons"],
                    "source": "fast_mode_rules",
                }
                workflow_route = "low"
            else:
                if proj_config.governor.enabled:
                    risk_assessment = await self._run_governor(
                        job, repos, proj_config, fallback_precheck=precheck
                    )
                    governor_completed = True
                else:
                    await self._skip_phase(
                        job, repos, "governor", "已按项目配置禁用"
                    )
                    self._create_precheck_constitution(
                        job, repos, precheck["level"]
                    )
                    risk_assessment = {
                        "risk": precheck["level"],
                        "risk_score": precheck["score"],
                        "risk_reasons": precheck["reasons"],
                        "source": "rules_fallback",
                    }
                    governor_completed = True

                workflow_route = (
                    self._risk_route(risk_assessment.get("risk"))
                    if proj_config.mode == "auto"
                    else "configured"
                )

            assessed_risk = self._normalized_risk_level(
                risk_assessment.get("risk"), precheck["level"]
            )
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
                if proj_config.planner.enabled:
                    await self._run_planner(job, repos, proj_config)
                    if job.status == "failed":
                        if worker and saved_turns is not None:
                            worker.max_turns = saved_turns
                        return
                else:
                    await self._skip_phase(job, repos, "planner")
                    self._create_direct_plan(job, repos, proj_config)
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
                if job.status in {"failed", "needs_attention"}:
                    if worker and saved_turns is not None:
                        worker.max_turns = saved_turns
                    return

                # ── Phase 4: Review ──
                if proj_config.reviewer.enabled and not auto_medium:
                    await self._run_reviewer(
                        job, repos,
                        proj_config=proj_config,
                        complexity=complexity,
                    )
                else:
                    await self._skip_review(
                        job, repos,
                        "中风险任务已通过确定性验证，跳过模型审核"
                        if auto_medium else "审核已按项目配置跳过",
                    )

            if worker and saved_turns is not None:
                worker.max_turns = saved_turns

            # ── Phase 5: Finalize ──
            if not self._is_cancelled(job.job_id, job, repos):
                await self._finalize(job, repos)
                finalized = True

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

    @staticmethod
    def _create_precheck_constitution(job, repos, risk_level: str):
        """Persist deterministic conservative bounds when Governor is skipped."""
        if repos["constitution"].get_by_job(job.id):
            return
        normalized_risk = "high" if risk_level == "critical" else risk_level
        repos["constitution"].create(
            job_id=job.id,
            goal=job.user_request,
            constraints=["只修改完成当前需求所必需的文件"],
            acceptance_criteria=["确定性验证通过", "需求中的可观察结果已实现"],
            risk=normalized_risk or "medium",
            protected_paths=[],
            requires_final_review=normalized_risk == "high",
            raw_output={"source": "deterministic_precheck"},
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
                            fallback_precheck: dict | None = None) -> dict:
        """Have Governor classify risk and persist the resulting constitution."""
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
        governor = self.get_agent("governor")
        if governor:
            try:
                effective_request = self._request_with_context(job, repos, proj_config)
                constitution = await governor.run(effective_request, job.project)
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
            plan_data = await planner.run(
                job, constitution, continuation_context=continuation_context
            )
        else:
            plan_data = self._direct_plan_data(job, repos, proj_config)

        if not plan_data.get("tasks"):
            used_fallback = True
            plan_data = self._direct_plan_data(job, repos, proj_config)

        self._optimize_plan(
            plan_data, getattr(job, "_rockcore_complexity", "normal")
        )
        self._serialize_overlapping_tasks(plan_data)
        self._prune_transitive_dependencies(plan_data)

        plan = repos["plan"].create(
            job_id=job.id,
            summary=plan_data.get("summary", ""),
            raw_output=plan_data,
        )

        # Validate plan against constitution
        self.state_machine.transition(job.job_id, JobState.PLAN_CHECK)
        errors = self.policy_engine.check_task_plan(
            plan_data, {"protected_paths": constitution.protected_paths if constitution else []}
        )
        repos["plan"].update_validation(plan.id, validated=len(errors) == 0, errors=errors)

        if errors:
            logger.error(f"Plan validation failed: {errors}")
            repos["job"].update_status(job.job_id, "failed")
            self._store_job_failure(
                repos, job.job_id, f"Plan validation failed: {errors[0]}"
            )
            self.state_machine.transition(job.job_id, JobState.FAILED)
            await self.event_bus.publish("plan_rejected", job_id=job.job_id, errors=errors)
            await self.event_bus.publish("phase_summary",
                phase="planner", agent_type="planner", status="rejected",
                summary=f"计划验证失败：{errors[0][:80] if errors else '未知错误'}",
                details={"errors": errors},
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

    def _direct_plan_data(self, job, repos, proj_config=None) -> dict:
        """Build one executable task when planning is explicitly unavailable."""
        description = self._request_with_context(job, repos, proj_config)
        return {
            "summary": f"单步执行：{job.user_request[:100]}",
            "tasks": [{
                "id": "T001",
                "title": job.user_request[:60],
                "type": "coding",
                "description": description,
                "dependencies": [],
                "allowed_paths": ["*"],
                "acceptance_command": "",
            }],
        }

    def _create_tasks_from_plan(self, job, repos, plan_data: dict,
                                order_offset: int = 0):
        for i, task_data in enumerate(plan_data.get("tasks", [])):
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
            )

    @classmethod
    def _optimize_plan(cls, plan_data: dict, complexity: str = "normal"):
        """Collapse model-heavy ceremony for small, targeted changes."""
        if complexity != "simple":
            return
        tasks = [dict(task) for task in (plan_data.get("tasks") or [])]
        coding = [task for task in tasks if task.get("type", "coding") == "coding"]
        if not coding:
            return

        analysis = [task for task in tasks if task.get("type") == "analysis"]
        primary = coding[0]
        descriptions = []
        for task in analysis + coding:
            text = str(task.get("description") or "").strip()
            if text and text not in descriptions:
                descriptions.append(text)
        primary["description"] = "\n\n".join(descriptions)
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

        collapsed_ids = {
            task.get("id") for task in analysis + coding if task.get("id")
        }
        replacement_id = primary.get("id") or "T001"
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
                mapped = replacement_id if dependency in collapsed_ids else dependency
                if mapped != task.get("id") and mapped not in dependencies:
                    dependencies.append(mapped)
            if not dependencies and task.get("type") == "testing":
                dependencies = [replacement_id]
            task["dependencies"] = dependencies
            retained.append(task)

        plan_data["tasks"] = retained
        plan_data["summary"] = (
            str(plan_data.get("summary") or "")
            + f"（简单任务已收敛为 {len(retained)} 个步骤）"
        )

    @classmethod
    def _serialize_overlapping_tasks(cls, plan_data: dict):
        """Prevent concurrently-ready tasks from editing the same paths."""
        tasks = plan_data.get("tasks", [])
        for index, task in enumerate(tasks):
            dependencies = list(task.get("dependencies") or [])
            current_paths = task.get("allowed_paths") or []
            for previous in tasks[:index]:
                previous_id = previous.get("id")
                if not previous_id or previous_id in dependencies:
                    continue
                previous_paths = previous.get("allowed_paths") or []
                if any(
                    cls._path_patterns_overlap(left, right)
                    for left in current_paths
                    for right in previous_paths
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
            turns = max(base_turns, 12 if total_lines >= 600 else 10)
            exploration = min(max(base_exploration, 4), max(4, turns - 2))
            reasons.append("read-only report")
        elif task_type in {"testing", "review"}:
            turns = min(base_turns, 18 if len(files) > 2 else 14)
            exploration = min(max(base_exploration, 3), max(3, turns // 2))
            reasons.append("validation task")
        else:
            turns = base_turns
            exploration = base_exploration
            if total_lines >= 400:
                turns += 6
                exploration += 2
                reasons.append(f"existing_code={total_lines} lines")
            if total_lines >= 1000:
                turns += 6
                exploration += 1
                reasons.append("large codebase slice")
            if len(files) >= 2:
                turns += 3
                reasons.append(f"files={len(files)}")
            if len(files) >= 5:
                turns += 3
                exploration += 1
            if dependency_count >= 4:
                turns += 3
                reasons.append(f"dependencies={dependency_count}")
            if behavior_count >= 4 or len(description) >= 320:
                turns += 3
                reasons.append("multiple behaviors")

        cap = 20 if mode == "fast" else 50
        turns = max(6, min(cap, turns))
        exploration = max(2, min(12, exploration, max(2, turns // 2)))
        return {
            "max_turns": turns,
            "exploration_turns": exploration,
            "existing_files": len(files),
            "total_lines": total_lines,
            "reason": ", ".join(reasons),
        }

    def _create_direct_plan(self, job, repos, proj_config=None):
        """Persist a direct task for a deliberately disabled Planner phase."""
        plan_data = self._direct_plan_data(job, repos, proj_config)
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
                             repair_round: int = 0) -> dict:
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
        all_tasks = repos["task"].list_by_job(job.id)
        if task_ids is not None:
            all_tasks = [task for task in all_tasks if task.task_id in task_ids]
        if not all_tasks:
            return {"status": "failed", "reason": "没有可执行的任务"}

        task_dicts = []
        for t in all_tasks:
            task_dicts.append({
                "task_id": t.task_id,
                "title": t.title,
                "description": t.description,
                "type": t.task_type,
                "dependencies": t.dependencies or [],
                "allowed_paths": t.allowed_paths or [],
                "acceptance_command": t.acceptance_command or "",
                "_db_task": t,
            })
        task_data_by_id = {item["task_id"]: item for item in task_dicts}
        completed_task_results: dict[str, dict] = {}

        # Define runner for each task (with worktree isolation)
        async def run_single_task(task_id: str, task_data: dict):
            t = task_data["_db_task"]
            nonlocal repos, job, worker

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
                    repos["task"].update_status_by_pk(t.id, "failed")
                    self._checkpoint_task(
                        repos, job, t, status="failed", result=result,
                        error=result.get("output", "Local validation failed"),
                    )
                    await self.event_bus.publish(
                        "task_failed", job_id=job.job_id, task_id=task_id,
                        error=result.get("output", "Local validation failed"),
                    )
                    raise RuntimeError(result.get("output", "Local validation failed"))
                repos["task"].update_status_by_pk(t.id, "done")
                self._checkpoint_task(
                    repos, job, t, status="done", result=result
                )
                await self.event_bus.publish(
                    "task_done", job_id=job.job_id, task_id=task_id, result=result
                )
                completed_task_results[task_id] = result
                return result

            # Create worktree for this task
            has_worktree = False
            if self.merge_manager:
                wt_result = await self.merge_manager.create_task_worktree(task_id, job.job_id)
                if wt_result.get("status") != "created":
                    logger.warning(f"Worktree creation failed for {task_id}, running in-place")
                    task_worktree_root = job.project.root_path if job.project else "."
                else:
                    has_worktree = True
                    task_worktree_root = wt_result.get("path", job.project.root_path if job.project else ".")
            else:
                task_worktree_root = job.project.root_path if job.project else "."
            task_baseline = self.test_manager.capture_snapshot(task_worktree_root)
            task_worker = worker.scoped_to(task_worktree_root)
            base_exploration = (
                proj_config.get_exploration_turns(complexity)
                if proj_config else getattr(task_worker, "max_exploration_turns", 4)
            )
            budget = self._estimate_task_budget(
                t,
                task_worktree_root,
                getattr(task_worker, "max_turns", 24),
                base_exploration,
                proj_config.mode if proj_config else "auto",
            )
            task_worker.max_turns = budget["max_turns"]
            task_worker.max_exploration_turns = budget["exploration_turns"]
            if complexity == "simple":
                task_worker.max_turns = min(task_worker.max_turns, 18)
            task_input_limits = {
                "simple": 120_000,
                "normal": 220_000,
                "complex": 320_000,
            }
            t._rockcore_input_budget = task_input_limits.get(
                complexity, 220_000
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
                if proj_config else "kimi-k2.7"
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
                max_turns=task_worker.max_turns,
                exploration_limit=task_worker.max_exploration_turns,
                budget_reason=budget["reason"],
            )

            # L0-L3: Attempt with escalation
            result = await self._execute_single_task_with_escalation(
                t, job, repos, task_worker, task_worktree_root
            )

            if result and result.get("status") == "completed":
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
                missing_required_output = (
                    t.task_type == "coding"
                    and not has_file_changes
                    and not declared_no_changes
                ) or (
                    t.task_type in {"analysis", "review"}
                    and not has_file_changes
                    and not task_output
                )
                if missing_required_output:
                    error = (
                        "Coding task produced no file changes"
                        if t.task_type == "coding"
                        else "Analysis task produced no report"
                    )
                    logger.error(
                        f"Task {task_id}: {t.task_type} task completed without "
                        "its required output — marking failed"
                    )
                    repos["task"].update_status_by_pk(t.id, "failed")
                    self._checkpoint_task(
                        repos, job, t, status="failed", result=result,
                        error=error,
                    )
                    await self.event_bus.publish("task_failed", job_id=job.job_id,
                                                  task_id=task_id,
                                                  error=error)
                    if has_worktree:
                        await self.merge_manager.abort_worktree(task_id)
                    raise RuntimeError(f"Task {task_id} failed: {error}")

                # Run acceptance test BEFORE marking done
                test_passed = True
                if t.acceptance_command:
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

                if test_passed:
                    # Merge worktree back
                    if has_worktree and has_file_changes:
                        merge_msg = f"AI {job.job_id}: {task_id} - {t.title}"
                        merge_result = await self.merge_manager.commit_and_merge(task_id, merge_msg)
                        if merge_result.get("status") != "merged":
                            conflicts = merge_result.get("conflicts") or []
                            error = merge_result.get("error") or merge_result.get("message")
                            if conflicts:
                                error = f"Merge conflict: {', '.join(conflicts)}"
                            error = error or "Task changes could not be merged"
                            repos["task"].update_status_by_pk(t.id, "failed")
                            self._checkpoint_task(
                                repos, job, t, status="failed",
                                result=merge_result, error=error,
                            )
                            await self.event_bus.publish(
                                "task_failed", job_id=job.job_id, task_id=task_id,
                                error=error,
                            )
                            await self.merge_manager.abort_worktree(task_id)
                            raise RuntimeError(error)
                    elif has_worktree:
                        # A successful read-only analysis has nothing to merge,
                        # but its temporary worktree still needs to be removed.
                        await self.merge_manager.abort_worktree(task_id)
                    repos["task"].update_status_by_pk(t.id, "done")
                    result_payload = dict(result)
                    result_payload["changes"] = task_changes
                    if declared_no_changes:
                        result_payload["no_changes"] = True
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
                    repos["task"].update_status_by_pk(t.id, "failed")
                    self._checkpoint_task(
                        repos, job, t, status="failed", result=result,
                        error="Acceptance test failed",
                    )
                    await self.event_bus.publish("task_failed", job_id=job.job_id,
                                                  task_id=task_id,
                                                  error="Acceptance test failed")
                    if has_worktree:
                        await self.merge_manager.abort_worktree(task_id)
                    raise RuntimeError(f"Task {task_id} failed: acceptance test did not pass")
            else:
                repos["task"].update_status_by_pk(t.id, "failed")
                await self.event_bus.publish("task_failed", job_id=job.job_id,
                                              task_id=task_id,
                                              error=result.get("error", "Unknown") if result else "Unknown")
                if has_worktree:
                    await self.merge_manager.abort_worktree(task_id)
                error = result.get("error", "Unknown") if result else "Unknown"
                self._checkpoint_task(
                    repos, job, t, status="failed", result=result,
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

            failed = [
                tid for tid, result in results.items()
                if isinstance(result, dict) and "error" in result
            ]
            if failed:
                logger.error(f"Tasks failed: {failed}")
                terminal_status = (
                    "needs_attention" if self.scheduler._completed else "failed"
                )
                repos["job"].update_status(job.job_id, terminal_status)
                self.state_machine.transition(job.job_id, JobState.FAILED)
                direct_failures = [tid for tid in failed if tid not in blocked]
                failure_messages = [
                    str(results[tid].get("error", "")) for tid in failed
                    if isinstance(results.get(tid), dict)
                ]
                if direct_failures:
                    failure_messages = [
                        str(results[tid].get("error", ""))
                        for tid in direct_failures
                    ]
                reason = failure_messages[0][:160] if failure_messages else "未知错误"
                self._store_job_failure(repos, job.job_id, reason)
                await self.event_bus.publish("phase_summary",
                    phase="execution", agent_type="worker", status="failed",
                    summary=f"任务执行失败：{reason}",
                    details={
                        "done": len(self.scheduler._completed),
                        "failed": len(direct_failures),
                        "blocked": len(blocked),
                    },
                )
                return {"status": "failed", "reason": reason}
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

    async def _execute_single_task_with_escalation(self, task, job, repos, worker, worktree_root):
        """Retry the primary Worker, then escalate once to Emergency."""
        escalation_count = 0
        last_error = ""
        repair_guidance = ""
        initial_turn_budget = getattr(worker, "max_turns", 16)
        continuation_turn_budget = min(12, max(8, initial_turn_budget // 2))
        configured_attempts = max(
            1, int(getattr(task, "_rockcore_retry_count", MAX_FLASH_RETRY)) + 1
        )
        emergency_after = max(1, min(6, int(getattr(
            task, "_rockcore_emergency_after_failures", 3
        ))))
        primary_attempts = min(configured_attempts, emergency_after)

        for attempt in range(1, primary_attempts + 1):
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
                            " This is the final Worker attempt before Emergency. "
                            "Re-read the acceptance command and relevant final files."
                        )
                    if repair_guidance:
                        recovery_context += "\nPlanner guidance:\n" + repair_guidance
                result = await worker.run(
                    task,
                    project_root=worktree_root,
                    recovery_context=recovery_context,
                )
                if result and result.get("status") == "completed":
                    return {"status": "completed", "result": result}

                if result and result.get("error"):
                    last_error = str(result["error"])
                    if self._is_budget_error(last_error):
                        return {"status": "failed", "error": last_error}
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
                                return {"status": "failed", "error": str(error)}
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
                    has_changes = await self._check_file_changes(worktree_root)
                    if has_changes:
                        last_error = (
                            "Max turns reached: partial changes require completion"
                        )
                        logger.warning(
                            f"Task {task.task_id}: max turns with partial changes; "
                            "continuing in the same worktree"
                        )
                        await self.event_bus.publish(
                            "task_continuing",
                            job_id=job.job_id,
                            task_id=task.task_id,
                            reason=last_error,
                            attempt=attempt + 1,
                            max_turns=continuation_turn_budget,
                        )
                        worker.max_turns = continuation_turn_budget
                        continue
                    else:
                        last_error = "Max turns reached: no changes detected"
                        logger.warning(
                            f"Task {task.task_id}: max turns with no changes; "
                            "switching to focused repair"
                        )
                        continue
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Task {task.task_id} attempt {attempt} failed: {e}")
                if self._is_budget_error(last_error):
                    return {"status": "failed", "error": last_error}
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
                last_error = fallback.get("error", last_error)

        if not bool(getattr(task, "_rockcore_auto_repair", True)):
            return {"status": "failed", "error": last_error}

        # The configured primary Worker failure threshold has been reached.
        await self.event_bus.publish("task_repairing", job_id=job.job_id,
                                      task_id=task.task_id, error=last_error,
                                      attempts=primary_attempts)

        # Emergency Coder: one quality-first attempt after the Worker threshold.
        if (
            bool(getattr(task, "_rockcore_emergency_enabled", True))
            and escalation_count < 1
        ):
            escalation_count += 1
            await self.event_bus.publish("task_escalating", job_id=job.job_id,
                                          task_id=task.task_id)
            emergency_result = await self._escalate_to_emergency(task, job, last_error)
            if emergency_result and emergency_result.get("fix_success"):
                return {"status": "completed", "result": emergency_result}

        # L4: Failed
        return {"status": "failed", "error": last_error}

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

    @staticmethod
    def _is_task_path_mismatch(error: str) -> bool:
        normalized = (error or "").lower()
        return (
            "[allowed_path]" in normalized
            or "path not in allowed set" in normalized
        )

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

    @staticmethod
    def _is_provider_unavailable(error: str) -> bool:
        normalized = (error or "").lower()
        markers = (
            "insufficient balance", "insufficient_balance", "error code: 402",
            "status code: 402", "error code: 401", "status code: 401",
            "error code: 403", "status code: 403", "invalid api key",
            "authentication", "quota exceeded", "billing",
            "connection error", "connection reset", "network error",
            "timed out", "timeout", "rate limit", "too many requests",
            "temporarily unavailable", "service unavailable", "server error",
            "status code: 500", "status code: 502", "status code: 503",
            "status code: 504", "error code: 500", "error code: 502",
            "error code: 503", "error code: 504",
            "invalid response", "malformed response", "expected a json object",
            "missing credentials", "credentials were not found", "api key",
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
            getattr(task, "_rockcore_fallback_model", "kimi-k2.7")
            or "kimi-k2.7"
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
            return {
                "status": "failed",
                "error": (
                    f"Primary worker provider failed: {original_error}; "
                    f"fallback attempts failed: {'; '.join(fallback_errors)}"
                ),
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

    async def _escalate_to_emergency(self, task, job, error) -> dict | None:
        """L3: Codex Emergency Coder with workspace_write access."""
        emergency = self.get_agent("emergency_coder")
        if not emergency:
            return None

        try:
            result = await emergency.run(task, job.project, previous_error=error)
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
                "status": "unrepairable",
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
            return {"status": repair_record["status"], "reason": reason}

        plan_data = self._namespace_repair_plan(
            decision.get("plan") or {}, round_number
        )
        self._serialize_overlapping_tasks(plan_data)
        self._prune_transitive_dependencies(plan_data)
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
                                     agent_type: str = "reviewer"):
        """Persist and publish a review failure with an actionable explanation."""
        if self._is_cancelled(job.job_id, job, repos):
            return
        has_completed_work = any(
            task.status == "done"
            for task in repos["task"].list_by_job(job.id)
        )
        repos["job"].update_status(
            job.job_id,
            "needs_attention" if has_completed_work else "failed",
        )
        self._store_job_failure(repos, job.job_id, reason)
        if self.state_machine.get_state(job.job_id) != JobState.FAILED:
            if not self.state_machine.transition(job.job_id, JobState.FAILED):
                logger.warning(
                    "Could not transition %s to FAILED from %s",
                    job.job_id,
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
            "job_failed", job_id=job.job_id, error=reason
        )

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
        """Give repair tasks unique IDs and retain only internal dependencies."""
        tasks = [dict(task) for task in (plan_data.get("tasks") or [])]
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
            task.setdefault("acceptance_command", "")

        for task in tasks:
            task["dependencies"] = [
                id_map[dependency]
                for dependency in (task.get("dependencies") or [])
                if dependency in id_map
            ]
        return {
            "summary": plan_data.get("summary", "审核修复计划"),
            "tasks": tasks,
        }

    @staticmethod
    def _friendly_provider_error(error: str) -> str:
        """Translate common provider failures into concise user-facing reasons."""
        normalized = (error or "").lower()
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
        if cls._is_budget_error(error):
            return (
                "budget_exceeded",
                "已保留完成步骤；检查 Token、调用次数或可计费 API 成本上限。"
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
                "先解决列出的冲突文件，再从失败步骤继续。",
            )
        if "without editing" in normalized or "no file changes" in normalized:
            return (
                "no_effective_edit",
                "重新策划时会锁定目标文件并要求先修改、后验证。",
            )
        return (
            "execution_failed",
            "已保留任务检查点；可继续此需求，只重做失败和受阻步骤。",
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
        summary = str(
            payload.get("output") or payload.get("content")
            or payload.get("reason") or error or status
        )[:4000]
        repos["task"].update_result(
            task.id, summary=summary, data=payload,
            failure_reason=error if status != "done" else "",
        )
        tasks = repos["task"].list_by_job(job.id)
        repos["job"].update_checkpoint(job.job_id, {
            "updated_at": datetime.now().astimezone().isoformat(),
            "tasks": [{
                "task_id": item.task_id,
                "status": item.status,
                "summary": (item.result_summary or "")[:1000],
                "failure_reason": (item.failure_reason or "")[:1000],
                "allowed_paths": item.allowed_paths or [],
            } for item in tasks],
        })

    async def _check_file_changes(self, project_root: str,
                                  baseline_snapshot: dict | None = None) -> bool:
        """Check if any files have been modified/created in the working directory."""
        import os, subprocess

        # Try git first
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True, cwd=project_root,
                timeout=5,
            )
            if result.returncode == 0:
                changed = [f for f in result.stdout.split("\n") if f.strip()]
                if changed:
                    logger.info(f"File changes detected: {changed}")
                    return True

            result2 = subprocess.run(
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

        # For non-Git projects, compare content snapshots rather than merely
        # treating any pre-existing file as a change.
        if baseline_snapshot is not None:
            return bool(
                self.test_manager.snapshot_diff(project_root, baseline_snapshot)["changed"]
            )

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
        context = self._continuation_context(job, repos, proj_config)
        return job.user_request if not context else f"{job.user_request}\n{context}"

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

        # Build continuation context (if enabled in config)
        cont_context = self._continuation_context(job, repos, proj_config)
        description = job.user_request
        if cont_context:
            description = job.user_request + "\n" + cont_context

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
        repos["plan"].create(job_id=job.id, summary=job.user_request, raw_output={})
        repos["task"].create(
            task_id="T001", job_id=job.id, title=job.user_request[:60],
            task_type="coding", description=description, allowed_paths=["*"],
            dependencies=[], acceptance_command="", order=0,
        )

        worker = self.get_agent("worker")
        if not worker:
            return
        saved_turns = worker.max_turns

        # Use config turn limit, with extra for continuation
        cfg = proj_config or ProjectAgentConfig()
        if cont_context:
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
        if job.status in {"failed", "needs_attention"}:
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

        await self.event_bus.publish(
            "job_finished", job_id=job.job_id, status=job.status
        )

    async def pause_job(self, job_id: str):
        self.scheduler.pause()
        await self.event_bus.publish("job_paused", job_id=job_id)

    async def resume_job(self, job_id: str):
        pending = self.scheduler.resume()
        await self.event_bus.publish("job_resumed", job_id=job_id,
                                      pending_count=len(pending))

    async def cancel_job(self, job_id: str):
        repos = self._get_repos()
        try:
            self._cancelled_job_ids.add(job_id)
            self.scheduler.stop()
            self.state_machine.transition(job_id, JobState.CANCELLED)
            repos["job"].update_status(job_id, "cancelled")
            await self.event_bus.publish("job_cancelled", job_id=job_id)
        finally:
            self._close_repos(repos)
