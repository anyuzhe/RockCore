"""Data access layer for all ORM models."""

import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from .models import (
    Project, Job, Constitution, Plan, Task, AgentRun,
    ToolCall, TestRun, Review, GitSnapshot
)


class ProjectRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, name: str, root_path: str, description: str = "",
               repo_type: str = "git") -> Project:
        project = Project(name=name, root_path=root_path,
                          description=description, repo_type=repo_type)
        self.session.add(project)
        self.session.commit()
        return project

    def get_by_id(self, project_id: int) -> Optional[Project]:
        return self.session.get(Project, project_id)

    def get_by_name(self, name: str) -> Optional[Project]:
        return self.session.query(Project).filter(Project.name == name).first()

    def list_all(self) -> list[Project]:
        return self.session.query(Project).order_by(Project.updated_at.desc()).all()

    def delete(self, project_id: int) -> bool:
        project = self.get_by_id(project_id)
        if project:
            self.session.delete(project)
            self.session.commit()
            return True
        return False


class JobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, job_id: str, project_id: int, user_request: str,
               risk_level: str = "medium",
               source_job_id: str | None = None,
               attachments: list[dict] | None = None) -> Job:
        job = Job(job_id=job_id, project_id=project_id,
                  user_request=user_request, risk_level=risk_level,
                  source_job_id=source_job_id,
                  attachments=list(attachments or []))
        self.session.add(job)
        self.session.commit()
        return job

    def get_by_id(self, job_id: str) -> Optional[Job]:
        return self.session.query(Job).filter(Job.job_id == job_id).first()

    def get_by_pk(self, pk: int) -> Optional[Job]:
        return self.session.get(Job, pk)

    def list_by_project(self, project_id: int) -> list[Job]:
        return self.session.query(Job).filter(
            Job.project_id == project_id
        ).order_by(Job.created_at.desc()).all()

    def list_all(self) -> list[Job]:
        return self.session.query(Job).order_by(Job.created_at.desc()).all()

    def update_status(self, job_id: str, status: str) -> Optional[Job]:
        job = self.get_by_id(job_id)
        if job:
            job.status = status
            job.updated_at = datetime.now(timezone.utc)
            if status in (
                "done", "failed", "cancelled", "interrupted",
                "needs_attention",
            ):
                job.completed_at = datetime.now(timezone.utc)
            self.session.commit()
        return job

    def update_risk_level(self, job_id: str, risk_level: str) -> Optional[Job]:
        job = self.get_by_id(job_id)
        if job:
            job.risk_level = risk_level
            job.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        return job

    def set_failure(self, job_id: str, code: str, reason: str,
                    recovery_hint: str = "") -> Optional[Job]:
        job = self.get_by_id(job_id)
        if job:
            job.failure_code = code or "unknown"
            job.failure_reason = reason or "未知错误"
            job.recovery_hint = recovery_hint or ""
            job.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        return job

    def clear_failure(self, job_id: str) -> Optional[Job]:
        job = self.get_by_id(job_id)
        if job:
            job.failure_code = ""
            job.failure_reason = ""
            job.recovery_hint = ""
            self.session.commit()
        return job

    def update_checkpoint(self, job_id: str, checkpoint: dict) -> Optional[Job]:
        job = self.get_by_id(job_id)
        if job:
            job.last_checkpoint = dict(checkpoint or {})
            job.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        return job

    def add_usage(self, job_id: str, input_tokens: int = 0,
                  cached_input_tokens: int = 0,
                  output_tokens: int = 0, cost: float = 0.0,
                  billable_cost: float = 0.0) -> Optional[Job]:
        job = self.get_by_id(job_id)
        if job:
            job.usage_input_tokens = (job.usage_input_tokens or 0) + max(0, int(input_tokens or 0))
            job.usage_cached_input_tokens = (
                (job.usage_cached_input_tokens or 0)
                + min(
                    max(0, int(cached_input_tokens or 0)),
                    max(0, int(input_tokens or 0)),
                )
            )
            job.usage_output_tokens = (job.usage_output_tokens or 0) + max(0, int(output_tokens or 0))
            job.usage_calls = (job.usage_calls or 0) + 1
            job.usage_cost = round((job.usage_cost or 0.0) + max(0.0, float(cost or 0.0)), 6)
            job.usage_billable_cost = round(
                (job.usage_billable_cost or 0.0)
                + max(0.0, float(billable_cost or 0.0)),
                6,
            )
            job.usage_cost_currency = "CNY"
            job.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        return job


class ConstitutionRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, job_id: int, goal: str, constraints: list,
               acceptance_criteria: list, risk: str = "medium",
               protected_paths: list = None,
               requires_final_review: bool = True,
               raw_output: dict = None) -> Constitution:
        c = Constitution(
            job_id=job_id, goal=goal, constraints=constraints,
            acceptance_criteria=acceptance_criteria, risk=risk,
            protected_paths=protected_paths or [],
            requires_final_review=requires_final_review,
            raw_output=raw_output or {}
        )
        self.session.add(c)
        self.session.commit()
        return c

    def get_by_job(self, job_id: int) -> Optional[Constitution]:
        return self.session.query(Constitution).filter(
            Constitution.job_id == job_id
        ).first()


class PlanRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, job_id: int, summary: str = "",
               raw_output: dict = None) -> Plan:
        plan = Plan(job_id=job_id, summary=summary,
                    raw_output=raw_output or {})
        self.session.add(plan)
        self.session.commit()
        return plan

    def get_by_job(self, job_id: int) -> Optional[Plan]:
        return self.session.query(Plan).filter(
            Plan.job_id == job_id
        ).first()

    def update_validation(self, plan_id: int, validated: bool,
                          errors: list = None) -> Optional[Plan]:
        plan = self.session.get(Plan, plan_id)
        if plan:
            plan.validated = validated
            plan.validation_errors = errors or []
            self.session.commit()
        return plan

    def upsert_repair_round(self, job_id: int, repair_round: dict) -> Optional[Plan]:
        """Persist one review-repair decision inside the job's existing plan."""
        plan = self.get_by_job(job_id)
        if not plan:
            return None
        raw_output = dict(plan.raw_output or {})
        rounds = list(raw_output.get("repair_rounds") or [])
        round_number = repair_round.get("round")
        replacement = dict(repair_round)
        for index, existing in enumerate(rounds):
            if existing.get("round") == round_number:
                rounds[index] = replacement
                break
        else:
            rounds.append(replacement)
        raw_output["repair_rounds"] = rounds
        plan.raw_output = raw_output
        self.session.commit()
        return plan


class TaskRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, task_id: str, job_id: int, title: str,
               task_type: str = "coding", description: str = "",
               allowed_paths: list = None, dependencies: list = None,
               acceptance_command: str = "", order: int = 0) -> Task:
        task = Task(
            task_id=task_id, job_id=job_id, title=title,
            description=description, task_type=task_type,
            allowed_paths=allowed_paths or [],
            dependencies=dependencies or [],
            acceptance_command=acceptance_command,
            order=order
        )
        self.session.add(task)
        self.session.flush()
        # Older databases could reuse a deleted task's integer primary key
        # while leaving its test runs behind. A newly-created task cannot have
        # legitimate test results yet, so remove any collision before commit.
        self.session.query(TestRun).filter(TestRun.task_id == task.id).delete(
            synchronize_session=False
        )
        self.session.commit()
        return task

    def get_by_id(self, task_id: str) -> Optional[Task]:
        return self.session.query(Task).filter(
            Task.task_id == task_id
        ).first()

    def get_by_job_and_id(self, job_id: int, task_id: str) -> Optional[Task]:
        """Resolve a task inside one job; task IDs repeat across jobs."""
        return self.session.query(Task).filter(
            Task.job_id == job_id,
            Task.task_id == task_id,
        ).first()

    def get_by_pk(self, pk: int) -> Optional[Task]:
        return self.session.get(Task, pk)

    def list_by_job(self, job_id: int) -> list[Task]:
        return self.session.query(Task).filter(
            Task.job_id == job_id
        ).order_by(Task.order).all()

    def update_status(self, task_id: str, status: str) -> Optional[Task]:
        task = self.get_by_id(task_id)
        return self._set_status(task, status)

    def update_status_by_pk(self, task_pk: int, status: str) -> Optional[Task]:
        task = self.get_by_pk(task_pk)
        return self._set_status(task, status)

    def update_definition(self, task_pk: int, *, description: str | None = None,
                          allowed_paths: list[str] | None = None,
                          acceptance_command: str | None = None) -> Optional[Task]:
        """Refine an unstarted task from verified prerequisite findings."""
        task = self.get_by_pk(task_pk)
        if task:
            if description is not None:
                task.description = description
            if allowed_paths is not None:
                task.allowed_paths = allowed_paths
            if acceptance_command is not None:
                task.acceptance_command = acceptance_command
            task.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        return task

    def update_result(self, task_pk: int, *, summary: str = "",
                      data: dict | None = None,
                      failure_reason: str = "") -> Optional[Task]:
        task = self.get_by_pk(task_pk)
        if task:
            task.result_summary = (summary or "")[:4000]
            task.result_data = dict(data or {})
            task.failure_reason = (failure_reason or "")[:4000]
            task.updated_at = datetime.now(timezone.utc)
            self.session.commit()
        return task

    def _set_status(self, task: Optional[Task], status: str) -> Optional[Task]:
        if task:
            task.status = status
            task.updated_at = datetime.now(timezone.utc)
            if status in ("done", "failed", "blocked", "cancelled"):
                task.completed_at = datetime.now(timezone.utc)
            self.session.commit()
        return task

    def get_next_pending(self, job_id: int) -> Optional[Task]:
        """Get next pending task whose dependencies are all done."""
        all_tasks = self.list_by_job(job_id)
        for task in all_tasks:
            if task.status != "pending":
                continue
            if not task.dependencies:
                return task
            deps_ok = all(
                any(t.task_id == dep and t.status == "done"
                    for t in all_tasks)
                for dep in task.dependencies
            )
            if deps_ok:
                return task
        return None


class AgentRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, task_id: int, agent_type: str, model_name: str = "") -> AgentRun:
        run = AgentRun(task_id=task_id, agent_type=agent_type,
                       model_name=model_name)
        self.session.add(run)
        self.session.commit()
        return run

    def update_status(self, run_id: int, status: str, **kwargs) -> Optional[AgentRun]:
        run = self.session.get(AgentRun, run_id)
        if run:
            run.status = status
            for k, v in kwargs.items():
                setattr(run, k, v)
            if status == "running" and run.started_at is None:
                run.started_at = datetime.now(timezone.utc)
            if status in ("completed", "failed"):
                run.completed_at = datetime.now(timezone.utc)
            self.session.commit()
        return run

    def list_by_task(self, task_id: int) -> list[AgentRun]:
        return self.session.query(AgentRun).filter(
            AgentRun.task_id == task_id
        ).order_by(AgentRun.created_at.asc()).all()


class ToolCallRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, agent_run_id: int, tool_name: str,
               arguments: dict = None, result_summary: str = "",
               status: str = "success", duration_ms: int = 0) -> ToolCall:
        tc = ToolCall(agent_run_id=agent_run_id, tool_name=tool_name,
                      arguments=arguments or {}, result_summary=result_summary,
                      status=status, duration_ms=duration_ms)
        self.session.add(tc)
        self.session.commit()
        return tc


class TestRunRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, task_id: int, command: str) -> TestRun:
        tr = TestRun(task_id=task_id, command=command)
        self.session.add(tr)
        self.session.commit()
        return tr

    def list_by_task(self, task_id: int) -> list[TestRun]:
        return self.session.query(TestRun).filter(
            TestRun.task_id == task_id
        ).order_by(TestRun.created_at.desc()).all()

    def update_result(self, test_run_id: int, passed: int, failed: int,
                      skipped: int, output: str, duration_ms: int,
                      status: str) -> Optional[TestRun]:
        tr = self.session.get(TestRun, test_run_id)
        if tr:
            tr.passed = passed
            tr.failed = failed
            tr.skipped = skipped
            tr.output = output
            tr.duration_ms = duration_ms
            tr.status = status
            self.session.commit()
        return tr


class ReviewRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, job_id: int, reviewer_type: str = "codex",
               result: str = "pending", severity: str = "medium",
               issues: list = None, constraint_violations: list = None,
               suggested_actions: list = None, summary: str = "") -> Review:
        r = Review(job_id=job_id, reviewer_type=reviewer_type,
                   result=result, severity=severity,
                   issues=issues or [], constraint_violations=constraint_violations or [],
                   suggested_actions=suggested_actions or [], summary=summary)
        self.session.add(r)
        self.session.commit()
        return r

    def list_by_job(self, job_id: int) -> list[Review]:
        return self.session.query(Review).filter(
            Review.job_id == job_id
        ).order_by(Review.created_at.desc()).all()
