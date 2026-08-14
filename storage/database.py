"""SQLite database setup and session management."""

import os
from pathlib import Path
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from .models import Base


_default_db_path = None


def get_default_db_path():
    global _default_db_path
    if _default_db_path is None:
        try:
            from app.paths import database_path
            _default_db_path = str(database_path())
        except ImportError:
            data_dir = Path.home() / ".ai_engineering_studio"
            data_dir.mkdir(parents=True, exist_ok=True)
            _default_db_path = str(data_dir / "studio.db")
    return _default_db_path


def init_database(db_path: str | None = None):
    """Initialize database, create tables if not exist."""
    if db_path is None:
        db_path = get_default_db_path()
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    _migrate_schema(engine)
    return engine


def _migrate_schema(engine):
    """Apply small additive migrations for databases created by older builds."""
    columns = {column["name"] for column in inspect(engine).get_columns("jobs")}
    if "source_job_id" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE jobs ADD COLUMN source_job_id VARCHAR(64)"
            )

    if "execution_session_id" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE jobs ADD COLUMN execution_session_id VARCHAR(64)"
            )

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE jobs SET execution_session_id = job_id "
            "WHERE execution_session_id IS NULL OR execution_session_id = ''"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_execution_session_id "
            "ON jobs (execution_session_id)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_source_job_id "
            "ON jobs (source_job_id)"
        )

    if "attachments" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE jobs ADD COLUMN attachments JSON NOT NULL DEFAULT '[]'"
            )

    usage_columns = {
        "usage_input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "usage_cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "usage_output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "usage_calls": "INTEGER NOT NULL DEFAULT 0",
        "usage_cost": "FLOAT NOT NULL DEFAULT 0.0",
        # Existing rows cannot be classified reliably after the fact. NULL is
        # rendered as historical/unclassified instead of pretending it was
        # either all billable or all subscription usage.
        "usage_billable_cost": "FLOAT DEFAULT NULL",
    }
    missing_usage = [name for name in usage_columns if name not in columns]
    if missing_usage:
        with engine.begin() as connection:
            for name in missing_usage:
                connection.exec_driver_sql(
                    f"ALTER TABLE jobs ADD COLUMN {name} {usage_columns[name]}"
                )

    # The previous release stored USD-equivalent values. Convert those rows
    # once when introducing the explicit CNY currency marker.
    if "usage_cost_currency" not in columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE jobs ADD COLUMN usage_cost_currency VARCHAR(8)"
            )
            connection.exec_driver_sql(
                "UPDATE jobs SET "
                "usage_cost = COALESCE(usage_cost, 0) * 7.2, "
                "usage_billable_cost = CASE "
                "WHEN usage_billable_cost IS NULL THEN NULL "
                "ELSE usage_billable_cost * 7.2 END, "
                "usage_cost_currency = 'CNY'"
            )

    job_failure_columns = {
        "failure_code": "VARCHAR(64) NOT NULL DEFAULT ''",
        "failure_reason": "TEXT NOT NULL DEFAULT ''",
        "recovery_hint": "TEXT NOT NULL DEFAULT ''",
        "last_checkpoint": "JSON NOT NULL DEFAULT '{}'",
    }
    missing_failure = [
        name for name in job_failure_columns if name not in columns
    ]
    if missing_failure:
        with engine.begin() as connection:
            for name in missing_failure:
                connection.exec_driver_sql(
                    f"ALTER TABLE jobs ADD COLUMN {name} "
                    f"{job_failure_columns[name]}"
                )

    # Backfill only after last_checkpoint has been added to old databases.
    # Some supported legacy fixtures predate even status/timestamp columns, so
    # every optional source expression is selected from the inspected schema.
    migrated_job_columns = {
        column["name"] for column in inspect(engine).get_columns("jobs")
    }
    status_expr = "j.status" if "status" in migrated_job_columns else "'created'"
    state_expr = (
        "COALESCE(j.last_checkpoint, '{}')"
        if "last_checkpoint" in migrated_job_columns else "'{}'"
    )
    created_expr = (
        "j.created_at" if "created_at" in migrated_job_columns
        else "CURRENT_TIMESTAMP"
    )
    updated_expr = (
        "j.updated_at" if "updated_at" in migrated_job_columns
        else created_expr
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO execution_conversations "
            "(session_id, project_id, title, goal, status, state, created_at, updated_at) "
            "SELECT j.execution_session_id, j.project_id, "
            "substr(replace(j.user_request, char(10), ' '), 1, 255), "
            f"j.user_request, {status_expr}, {state_expr}, "
            f"{created_expr}, {updated_expr} FROM jobs j "
            "INNER JOIN ("
            "  SELECT execution_session_id, MIN(id) AS first_id "
            "  FROM jobs GROUP BY execution_session_id"
            ") first_turn ON first_turn.first_id = j.id"
        )

    agent_run_columns = {
        column["name"]
        for column in inspect(engine).get_columns("agent_runs")
    }
    agent_run_usage_columns = {
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cost": "FLOAT NOT NULL DEFAULT 0.0",
        "billable_cost": "FLOAT DEFAULT NULL",
        "billing_mode": "VARCHAR(32) NOT NULL DEFAULT 'unclassified'",
    }
    missing_agent_run_usage = [
        name for name in agent_run_usage_columns
        if name not in agent_run_columns
    ]
    if missing_agent_run_usage:
        with engine.begin() as connection:
            for name in missing_agent_run_usage:
                connection.exec_driver_sql(
                    f"ALTER TABLE agent_runs ADD COLUMN {name} "
                    f"{agent_run_usage_columns[name]}"
                )

    if "cost_currency" not in agent_run_columns:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "ALTER TABLE agent_runs ADD COLUMN cost_currency VARCHAR(8)"
            )
            connection.exec_driver_sql(
                "UPDATE agent_runs SET "
                "cost = COALESCE(cost, 0) * 7.2, "
                "billable_cost = CASE "
                "WHEN billable_cost IS NULL THEN NULL "
                "ELSE billable_cost * 7.2 END, "
                "cost_currency = 'CNY'"
            )

    task_columns = {
        column["name"] for column in inspect(engine).get_columns("tasks")
    }
    task_result_columns = {
        "result_summary": "TEXT NOT NULL DEFAULT ''",
        "result_data": "JSON NOT NULL DEFAULT '{}'",
        "failure_reason": "TEXT NOT NULL DEFAULT ''",
        "skills": "JSON NOT NULL DEFAULT '[]'",
        "context_key": "VARCHAR(255) NOT NULL DEFAULT ''",
        "execution_group_id": "VARCHAR(255) NOT NULL DEFAULT ''",
        "internal_steps": "JSON NOT NULL DEFAULT '[]'",
        "acceptance_commands": "JSON NOT NULL DEFAULT '[]'",
    }
    missing_task_results = [
        name for name in task_result_columns if name not in task_columns
    ]
    if missing_task_results:
        with engine.begin() as connection:
            for name in missing_task_results:
                connection.exec_driver_sql(
                    f"ALTER TABLE tasks ADD COLUMN {name} "
                    f"{task_result_columns[name]}"
                )

    # Old project deletions could leave test runs behind. SQLite may then reuse
    # the deleted task ID, making an unrelated new task display stale results.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM test_runs "
            "WHERE NOT EXISTS (SELECT 1 FROM tasks WHERE tasks.id = test_runs.task_id) "
            "OR EXISTS ("
            "SELECT 1 FROM tasks "
            "WHERE tasks.id = test_runs.task_id "
            "AND test_runs.created_at < tasks.created_at"
            ")"
        )


def create_session_factory(engine):
    # Each repository bundle must own an independent session. A scoped session
    # would return the caller's active session to nested event handlers (for
    # example model-usage persistence), allowing a handler to close or expire
    # ORM objects that the main job lifecycle is still using.
    return sessionmaker(bind=engine, expire_on_commit=False)
