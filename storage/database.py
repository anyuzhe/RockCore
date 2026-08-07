"""SQLite database setup and session management."""

import os
from pathlib import Path
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker, scoped_session

from .models import Base


_default_db_path = None


def get_default_db_path():
    global _default_db_path
    if _default_db_path is None:
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
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_jobs_source_job_id "
                "ON jobs (source_job_id)"
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
    return scoped_session(sessionmaker(bind=engine, expire_on_commit=False))
