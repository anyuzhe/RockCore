"""SQLAlchemy ORM models for the AI Engineering Studio."""

from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Float, Boolean, DateTime,
    ForeignKey, Enum, JSON, create_engine
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    description = Column(Text, default="")
    root_path = Column(String(1024), nullable=False)
    repo_type = Column(String(64), default="git")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    jobs = relationship("Job", back_populates="project", cascade="all, delete-orphan")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True)
    job_id = Column(String(64), nullable=False, unique=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id"), nullable=False)
    user_request = Column(Text, nullable=False)
    attachments = Column(JSON, default=list, nullable=False)
    # The job whose result this request explicitly continues, if any.
    source_job_id = Column(String(64), nullable=True, index=True)
    status = Column(String(32), default="created", index=True)
    risk_level = Column(String(16), default="medium")
    branch_name = Column(String(255), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    usage_input_tokens = Column(Integer, default=0)
    usage_cached_input_tokens = Column(Integer, default=0)
    usage_output_tokens = Column(Integer, default=0)
    usage_calls = Column(Integer, default=0)
    # RMB API-price-equivalent estimate for every provider call, including
    # calls made through a ChatGPT subscription.
    usage_cost = Column(Float, default=0.0)
    # Estimated cost only for separately billed API transports. Nullable keeps
    # migrated historical rows distinguishable from newly classified usage.
    usage_billable_cost = Column(Float, default=0.0, nullable=True)
    usage_cost_currency = Column(String(8), default="CNY")
    failure_code = Column(String(64), default="")
    failure_reason = Column(Text, default="")
    recovery_hint = Column(Text, default="")
    last_checkpoint = Column(JSON, default=dict)

    project = relationship("Project", back_populates="jobs")
    constitution = relationship("Constitution", uselist=False, back_populates="job",
                                cascade="all, delete-orphan")
    plan = relationship("Plan", uselist=False, back_populates="job",
                        cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="job", cascade="all, delete-orphan")
    reviews = relationship("Review", back_populates="job", cascade="all, delete-orphan")


class Constitution(Base):
    __tablename__ = "constitutions"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True)
    goal = Column(Text, nullable=False)
    constraints = Column(JSON, default=list)
    acceptance_criteria = Column(JSON, default=list)
    risk = Column(String(16), default="medium")
    protected_paths = Column(JSON, default=list)
    requires_final_review = Column(Boolean, default=True)
    raw_output = Column(JSON, default=dict)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    job = relationship("Job", back_populates="constitution")


class Plan(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, unique=True)
    summary = Column(Text, default="")
    raw_output = Column(JSON, default=dict)
    validated = Column(Boolean, default=False)
    validation_errors = Column(JSON, default=list)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    job = relationship("Job", back_populates="plan")
    tasks = relationship("Task", secondary="plan_tasks", viewonly=True)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    task_id = Column(String(64), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    title = Column(String(512), nullable=False)
    description = Column(Text, default="")
    task_type = Column(String(32), default="coding")  # analysis, coding, testing, review, action
    status = Column(String(32), default="pending", index=True)
    allowed_paths = Column(JSON, default=list)
    skills = Column(JSON, default=list)
    dependencies = Column(JSON, default=list)
    acceptance_command = Column(String(1024), default="")
    order = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))
    completed_at = Column(DateTime, nullable=True)
    result_summary = Column(Text, default="")
    result_data = Column(JSON, default=dict)
    failure_reason = Column(Text, default="")

    job = relationship("Job", back_populates="tasks")
    agent_runs = relationship("AgentRun", back_populates="task", cascade="all, delete-orphan")
    test_runs = relationship("TestRun", back_populates="task", cascade="all, delete-orphan")


class PlanTasks(Base):
    __tablename__ = "plan_tasks"
    plan_id = Column(Integer, ForeignKey("plans.id"), primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), primary_key=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    agent_type = Column(String(32), nullable=False)  # governor, planner, worker, reviewer
    model_name = Column(String(64), default="")
    status = Column(String(32), default="pending")
    input_tokens = Column(Integer, default=0)
    cached_input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    # ``cost`` is the RMB equivalent estimate retained for compatibility.
    cost = Column(Float, default=0.0)
    billable_cost = Column(Float, default=0.0, nullable=True)
    cost_currency = Column(String(8), default="CNY")
    billing_mode = Column(String(32), default="api")
    error_message = Column(Text, default="")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    task = relationship("Task", back_populates="agent_runs")
    tool_calls = relationship("ToolCall", back_populates="agent_run",
                              cascade="all, delete-orphan")


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id = Column(Integer, primary_key=True)
    agent_run_id = Column(Integer, ForeignKey("agent_runs.id"), nullable=False)
    tool_name = Column(String(128), nullable=False)
    arguments = Column(JSON, default=dict)
    result_summary = Column(String(1024), default="")
    status = Column(String(32), default="success")  # success, rejected, error
    duration_ms = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    agent_run = relationship("AgentRun", back_populates="tool_calls")


class TestRun(Base):
    __tablename__ = "test_runs"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    command = Column(String(1024), nullable=False)
    passed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    skipped = Column(Integer, default=0)
    output = Column(Text, default="")
    duration_ms = Column(Integer, default=0)
    status = Column(String(32), default="pending")  # pending, running, passed, failed
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    task = relationship("Task", back_populates="test_runs")


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    reviewer_type = Column(String(32), default="codex")
    result = Column(String(32), default="pending")  # pending, pass, reject
    severity = Column(String(16), default="medium")
    issues = Column(JSON, default=list)
    constraint_violations = Column(JSON, default=list)
    suggested_actions = Column(JSON, default=list)
    summary = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    job = relationship("Job", back_populates="reviews")


class GitSnapshot(Base):
    __tablename__ = "git_snapshots"

    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    stage = Column(String(32), nullable=False)  # before, after
    branch = Column(String(255), default="")
    commit_hash = Column(String(64), default="")
    diff_summary = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
