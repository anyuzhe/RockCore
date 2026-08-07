"""Main orchestrator engine — the brain of the AI Engineering Studio."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MAX_FLASH_RETRY = 2
MAX_REPLAN_RETRY = 1


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

    def __init__(self, db_path: str | None = None):
        from storage.database import init_database
        self._engine = init_database(db_path)
        self._session_factory = create_session_factory(self._engine)

        self.event_bus = EventBus()
        self.event_bus.subscribe("model_chat", self._record_model_usage)
        self.state_machine = StateMachine()
        self.scheduler = Scheduler(max_concurrent=3)
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
        output_tokens = max(0, int(data.get("output_tokens") or 0))
        estimated_cost = max(0.0, float(data.get("estimated_cost") or 0.0))
        repos = self._get_repos()
        try:
            repos["job"].add_usage(
                job_id, input_tokens, output_tokens, estimated_cost
            )
            task_id = data.get("task_id")
            if not task_id:
                return
            task = repos["task"].get_by_id(task_id)
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
                output_tokens=output_tokens,
                cost=estimated_cost,
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
        logger.info("Engine started")

    async def stop(self):
        self._running = False
        self.scheduler.stop()
        logger.info("Engine stopped")

    async def _skip_phase(self, job, repos, phase: str):
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
            summary=f"{phase} 已按项目配置禁用")

    async def _skip_review(self, job, repos):
        """Skip reviewer and go straight to DONE."""
        self.state_machine.transition(job.job_id, JobState.REVIEWING)
        self.state_machine.transition(job.job_id, JobState.DONE)
        repos["job"].update_status(job.job_id, "done")
        await self.event_bus.publish("phase_summary",
            phase="reviewer", agent_type="reviewer", status="skipped",
            summary="审核已按项目配置跳过")
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

            today = datetime.now(timezone.utc).strftime("%Y%m%d")
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
            logger.info(f"Job {job_id}: complexity={complexity}")

            # Apply config: set worker turn limits
            worker = self.get_agent("worker")
            saved_turns = None
            if worker:
                saved_turns = worker.max_turns
                worker.max_turns = proj_config.get_worker_turns(complexity)

            # Complexity controls budgets only. Skipping governance/planning is
            # an explicit project-level choice, never an automatic guess.
            if proj_config.mode == "fast":
                await self._run_simple(job, repos, proj_config)
            else:
                # ── Phase 1: Governor ──
                if proj_config.governor.enabled:
                    await self._run_governor(job, repos, proj_config)
                else:
                    await self._skip_phase(job, repos, "governor")
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
                if job.status == "failed":
                    if worker and saved_turns is not None:
                        worker.max_turns = saved_turns
                    return

                # ── Phase 4: Review ──
                if proj_config.reviewer.enabled:
                    await self._run_reviewer(job, repos)
                else:
                    await self._skip_review(job, repos)

            if worker and saved_turns is not None:
                worker.max_turns = saved_turns

            # ── Phase 5: Finalize ──
            if not self._is_cancelled(job.job_id, job, repos):
                await self._finalize(job, repos)

        except Exception as e:
            logger.error(f"Job failed: {job_id}: {e}")
            repos["job"].update_status(job_id, "failed")
            self.state_machine.transition(job_id, JobState.FAILED)
            await self.event_bus.publish("job_failed", job_id=job_id, error=str(e))
        finally:
            self._close_repos(repos)

    async def _run_governor(self, job, repos, proj_config=None):
        repos["job"].update_status(job.job_id, "governing")
        self.state_machine.transition(job.job_id, JobState.GOVERNING)
        await self.event_bus.publish("job_governing", job_id=job.job_id)

        governor = self.get_agent("governor")
        if governor:
            try:
                effective_request = self._request_with_context(job, repos, proj_config)
                constitution = await governor.run(effective_request, job.project)
                repos["constitution"].create(
                    job_id=job.id, **constitution
                )
                risk_cn = {"low": "低", "medium": "中", "high": "高"}.get(constitution.get("risk", "low"), "低")
                await self.event_bus.publish("phase_summary",
                    phase="governor", agent_type="governor", status="success",
                    summary=f"分析了需求：{constitution.get('goal', job.user_request)}，风险等级：{risk_cn}",
                )
            except Exception as e:
                logger.warning(f"Governor failed, using defaults: {e}")
                repos["constitution"].create(
                    job_id=job.id,
                    goal=job.user_request,
                    constraints=[],
                    acceptance_criteria=["All tests pass"],
                    risk=job.risk_level,
                    protected_paths=[],
                    requires_final_review=True,
                    raw_output={"fallback": True, "error": str(e)},
                )
                await self.event_bus.publish(
                    "phase_summary",
                    phase="governor", agent_type="governor", status="fallback",
                    summary="裁决者不可用，已使用保守的默认约束继续执行",
                )
        else:
            repos["constitution"].create(
                job_id=job.id,
                goal=job.user_request,
                constraints=[],
                acceptance_criteria=["All tests pass"],
                risk=job.risk_level,
                protected_paths=[],
                requires_final_review=True,
                raw_output={"fallback": True, "error": "Governor not registered"},
            )
            await self.event_bus.publish(
                "phase_summary",
                phase="governor", agent_type="governor", status="fallback",
                summary="未注册裁决者，已使用保守的默认约束继续执行",
            )

        self.state_machine.transition(job.job_id, JobState.GOVERNED)
        await self.event_bus.publish("job_governed", job_id=job.job_id)

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

    def _create_tasks_from_plan(self, job, repos, plan_data: dict):
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
                order=i,
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
                             complexity: str = "normal"):
        """Execute tasks in parallel using DAG scheduler (V4: worktree isolation)."""
        if self._is_cancelled(job.job_id, job, repos):
            return
        self.state_machine.transition(job.job_id, JobState.EXECUTING)
        repos["job"].update_status(job.job_id, "executing")
        await self.event_bus.publish("job_executing", job_id=job.job_id)
        if self._is_cancelled(job.job_id, job, repos):
            return

        worker = self.get_agent("worker")
        if not worker:
            logger.warning("No worker agent registered")
            return

        # Collect all tasks as dicts for the DAG scheduler
        all_tasks = repos["task"].list_by_job(job.id)
        if not all_tasks:
            return

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

        # Define runner for each task (with worktree isolation)
        async def run_single_task(task_id: str, task_data: dict):
            t = task_data["_db_task"]
            nonlocal repos, job, worker

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
                    await self.event_bus.publish(
                        "task_failed", job_id=job.job_id, task_id=task_id,
                        error=result.get("output", "Local validation failed"),
                    )
                    raise RuntimeError(result.get("output", "Local validation failed"))
                repos["task"].update_status_by_pk(t.id, "done")
                await self.event_bus.publish(
                    "task_done", job_id=job.job_id, task_id=task_id, result=result
                )
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
                        t, repos, self.event_bus, project_root=task_worktree_root
                    )
                    if test_result and test_result.get("status") != "passed":
                        test_passed = False
                        logger.warning(f"Task {task_id} acceptance test failed: {test_result.get('status')}")

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
                    await self.event_bus.publish(
                        "task_done", job_id=job.job_id,
                        task_id=task_id, result=result_payload,
                    )
                else:
                    repos["task"].update_status_by_pk(t.id, "failed")
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
                return
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
                repos["job"].update_status(job.job_id, "failed")
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
                await self.event_bus.publish("phase_summary",
                    phase="execution", agent_type="worker", status="failed",
                    summary=f"任务执行失败：{reason}",
                    details={
                        "done": len(self.scheduler._completed),
                        "failed": len(direct_failures),
                        "blocked": len(blocked),
                    },
                )
                return
        except Exception as e:
            logger.error(f"Execution failed: {e}")
            repos["job"].update_status(job.job_id, "failed")
            self.state_machine.transition(job.job_id, JobState.FAILED)
            await self.event_bus.publish("phase_summary",
                phase="execution", agent_type="worker", status="failed",
                summary=f"执行异常：{str(e)[:100]}",
            )
            return

        self.state_machine.transition(job.job_id, JobState.TESTING)
        await self.event_bus.publish("execution_complete", job_id=job.job_id)
        await self.event_bus.publish("phase_summary",
            phase="execution", agent_type="worker", status="success",
            summary=f"所有任务执行完成",
            details={"done": len(task_dicts), "failed": 0},
        )

    async def _execute_single_task_with_escalation(self, task, job, repos, worker, worktree_root):
        """Execute a single task with L0-L4 escalation (used by parallel runner)."""
        replan_count = 0
        escalation_count = 0
        last_error = ""
        initial_turn_budget = getattr(worker, "max_turns", 16)
        continuation_turn_budget = min(12, max(8, initial_turn_budget // 2))

        for attempt in range(1, MAX_FLASH_RETRY + 2):  # L0 + L1 retry
            try:
                result = await worker.run(task, project_root=worktree_root)
                if result and result.get("status") == "completed":
                    return {"status": "completed", "result": result}

                if result and result.get("error"):
                    last_error = str(result["error"])
                    if self._is_provider_unavailable(last_error):
                        fallback = await self._run_worker_fallback(
                            worker, task, worktree_root, last_error
                        )
                        if fallback:
                            return fallback
                        return {"status": "failed", "error": last_error}
                    if "ended without editing files" in last_error.lower():
                        logger.warning(
                            f"Task {task.task_id}: model ended before editing; "
                            "switching to focused repair"
                        )
                        break

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
                        break
            except Exception as e:
                last_error = str(e)
                logger.warning(f"Task {task.task_id} attempt {attempt} failed: {e}")
                if self._is_provider_unavailable(last_error):
                    logger.warning(
                        f"Task {task.task_id}: provider failure is not retryable "
                        "on the same provider; switching to fallback"
                    )
                    break

        if self._is_provider_unavailable(last_error):
            fallback = await self._run_worker_fallback(
                worker, task, worktree_root, last_error
            )
            if fallback:
                return fallback
            return {"status": "failed", "error": last_error}

        # L0 + L1 failed — escalate
        worker.max_turns = continuation_turn_budget
        await self.event_bus.publish("task_repairing", job_id=job.job_id,
                                      task_id=task.task_id, error=last_error)

        # L2: Replan
        if replan_count < MAX_REPLAN_RETRY:
            replan_count += 1
            await self.event_bus.publish("task_replanning", job_id=job.job_id,
                                          task_id=task.task_id)
            repair_plan = await self._repair_plan(job, task, last_error, repos)
            if repair_plan:
                recovery_context = json.dumps(
                    repair_plan, ensure_ascii=False, default=str
                )
                for attempt in range(1, MAX_FLASH_RETRY + 2):
                    try:
                        result = await worker.run(
                            task,
                            project_root=worktree_root,
                            provider_override=(
                                "kimi" if self.model_router.has_provider("kimi") else None
                            ),
                            recovery_context=recovery_context,
                        )
                        if result and result.get("status") == "completed":
                            return {"status": "completed", "result": result}
                        if result and result.get("error"):
                            last_error = str(result["error"])
                    except Exception as e:
                        last_error = str(e)
                        logger.warning(f"Repair attempt {attempt} failed: {e}")

        # L3: Emergency Coder
        if escalation_count < 1:
            escalation_count += 1
            await self.event_bus.publish("task_escalating", job_id=job.job_id,
                                          task_id=task.task_id)
            emergency_result = await self._escalate_to_emergency(task, job, last_error)
            if emergency_result and emergency_result.get("fix_success"):
                return {"status": "completed", "result": emergency_result}

        # L4: Failed
        return {"status": "failed", "error": last_error}

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
        for provider in ("kimi",):
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
        except Exception as e:
            logger.error(f"Repair planning failed: {e}")
            return None

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

    async def _run_reviewer(self, job, repos):
        if self._is_cancelled(job.job_id, job, repos):
            return
        self.state_machine.transition(job.job_id, JobState.REVIEWING)
        repos["job"].update_status(job.job_id, "reviewing")
        await self.event_bus.publish("job_reviewing", job_id=job.job_id)
        if self._is_cancelled(job.job_id, job, repos):
            return

        review_result = None
        reviewer = self.get_agent("reviewer")
        if reviewer:
            try:
                review_result = await reviewer.run(job)
                repos["review"].create(
                    job_id=job.id,
                    result=review_result.get("result", "pass"),
                    severity=review_result.get("severity", "low"),
                    issues=review_result.get("issues", []),
                    constraint_violations=review_result.get("constraint_violations", []),
                    suggested_actions=review_result.get("suggested_actions", []),
                    summary=review_result.get("summary", ""),
                )
                await self.event_bus.publish("review_complete", job_id=job.job_id,
                                              result=review_result.get("result"))
            except Exception as e:
                logger.warning(f"Reviewer failed: {e}")
                summary = f"审核者不可用：{str(e)[:300]}"
                repos["review"].create(
                    job_id=job.id,
                    result="error",
                    severity="high",
                    issues=[{"problem": summary, "severity": "high"}],
                    summary=summary,
                )
                review_result = {
                    "result": "error",
                    "summary": summary,
                    "issues": [{"problem": summary, "severity": "high"}],
                }
                await self.event_bus.publish(
                    "review_complete", job_id=job.job_id, result="error"
                )

        if self._is_cancelled(job.job_id, job, repos):
            return

        # If reviewer rejected, mark failed, not done
        review_status = review_result.get("result") if review_result else "error"
        is_rejected = review_status in {"reject", "error"}
        if is_rejected:
            logger.warning(f"Job {job.job_id}: review {review_status.upper()}")
            repos["job"].update_status(job.job_id, "failed")
            self.state_machine.transition(job.job_id, JobState.FAILED)
            await self.event_bus.publish("phase_summary",
                phase="reviewer", agent_type="reviewer",
                status="failed" if review_status == "error" else "rejected",
                summary=(
                    f"审核失败：{review_result.get('summary', '')[:200]}"
                    if review_status == "error" else
                    f"审核驳回：{review_result.get('summary', '')[:200]}"
                ),
                details={"issues": review_result.get("issues", []) if review_result else []},
            )
            await self.event_bus.publish("job_failed", job_id=job.job_id,
                                         error=(
                                             "Reviewer unavailable"
                                             if review_status == "error" else "Review rejected"
                                         ))
        else:
            self.state_machine.transition(job.job_id, JobState.DONE)
            repos["job"].update_status(job.job_id, "done")
            await self.event_bus.publish("phase_summary",
                phase="reviewer", agent_type="reviewer", status="success",
                summary="审核通过",
            )
            await self.event_bus.publish("job_done", job_id=job.job_id)

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

        # Get previous job's output files
        prev_tasks = repos["task"].list_by_job(prev_job.id)
        prev_files = set()
        for t in prev_tasks:
            for p in (t.allowed_paths or []):
                # Only include concrete files (not globs)
                if "*" not in p and "." in p:
                    prev_files.add(p)

        if not prev_files:
            # Try to find actual files in the project directory
            import os
            root = job.project.root_path if job.project else "."
            if os.path.isdir(root):
                for f in sorted(os.listdir(root))[:10]:
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp) and not f.startswith("."):
                        prev_files.add(f)

        context = f"""
=== CONTINUATION CONTEXT ===
This is a follow-up to a previous task. Do NOT restart — modify existing work.

Previous request: {prev_job.user_request[:200]}

=== Target Files ===
These files already exist and should be modified:
{chr(10).join('- ' + f for f in sorted(prev_files)[:8] if f)}

=== Your Workflow ===
1. Use search_in_file to locate relevant sections by keyword
2. Use read_file with start/end to read only the relevant range of lines
3. Apply modifications with apply_patch (prefer patches over full rewrites)
4. Verify changes with git_diff
5. Do NOT read entire files from beginning — use targeted search + range reads
6. Prefer editing existing files; create a new file only when the follow-up requires it

=== Context Hints ===
The current request sounds like it wants to add/modify content in the existing files.
Focus on finding the right insertion point using search_in_file, then make targeted edits.
"""
        return context

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

    async def _run_simple(self, job, repos, proj_config: ProjectAgentConfig | None = None):
        """Fast path for simple tasks: skip Governor + Planner, run directly."""
        await self.event_bus.publish("phase_summary",
            phase="governor", agent_type="governor", status="skipped",
            summary="简单任务，直接执行")
        await self.event_bus.publish("phase_summary",
            phase="planner", agent_type="planner", status="skipped",
            summary="简单任务，直接创建单个编码任务")

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

        repos["constitution"].create(
            job_id=job.id, goal=job.user_request, constraints=[],
            acceptance_criteria=[], risk="low", protected_paths=[],
            requires_final_review=False,
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
        if job.status == "failed":
            worker.max_turns = saved_turns
            return

        worker.max_turns = saved_turns
        await self.event_bus.publish("phase_summary",
            phase="reviewer", agent_type="reviewer", status="skipped",
            summary="简单任务，跳过审核")
        self.state_machine.transition(job.job_id, JobState.REVIEWING)
        self.state_machine.transition(job.job_id, JobState.DONE)
        repos["job"].update_status(job.job_id, "done")
        await self.event_bus.publish("job_done", job_id=job.job_id)

    async def _finalize(self, job, repos):
        # Output verification: check if project actually has files
        if job.status == "done" and job.project:
            root = job.project.root_path
            import os
            if os.path.isdir(root):
                all_files = []
                for f in os.listdir(root):
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp) and not f.startswith("."):
                        all_files.append(f)
                logger.info(f"[output_verify] project={root} files={len(all_files)}: {all_files[:5]}")
                if not all_files:
                    logger.error(f"[output_verify] Job {job.job_id}: marked done but project directory is empty!")
                    repos["job"].update_status(job.job_id, "failed")
                    self.state_machine.transition(job.job_id, JobState.FAILED)

        await self.event_bus.publish("job_finished", job_id=job.job_id,
                                     status=job.status)

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
