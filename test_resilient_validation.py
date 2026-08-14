"""Regression coverage for Git bootstrap, local validation, and provider failures."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

from git.repository import ROCKCORE_IGNORE_START, Repository
from orchestrator.engine import Engine
from orchestrator.state_machine import JobState
from orchestrator.test_manager import TestManager


def _task(project, **overrides):
    values = {
        "id": 1,
        "task_id": "T004",
        "title": "Review diff and structure",
        "description": "Check HTML structure",
        "task_type": "review",
        "acceptance_command": "git diff --stat",
        "allowed_paths": ["*.html"],
        "job": SimpleNamespace(project=project),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_repository_bootstrap_creates_safe_initial_commit(tmp_path):
    (tmp_path / "index.html").write_text("<html><body>ok</body></html>")
    (tmp_path / ".env").write_text("SECRET=do-not-commit")

    result = Repository(str(tmp_path)).ensure_initialized()

    assert result["status"] == "initialized"
    assert result["commit"]
    assert Repository(str(tmp_path)).is_repo()
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=tmp_path
    ).stdout.splitlines()
    assert "index.html" in tracked
    assert ".gitignore" in tracked
    assert ".env" not in tracked
    ignore_text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "# >>> RockCore managed ignores >>>" in ignore_text
    assert "__pycache__/" in ignore_text
    assert "node_modules/" in ignore_text
    assert ".ai/runtime/" in ignore_text
    assert ".ai/recovery/" in ignore_text


def test_repository_bootstrap_ignores_generated_code_artifacts(tmp_path):
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "main.cpython-313.pyc").write_bytes(b"generated")
    dependency = tmp_path / "node_modules" / "demo"
    dependency.mkdir(parents=True)
    (dependency / "index.js").write_text("generated", encoding="utf-8")

    result = Repository(str(tmp_path)).ensure_initialized()
    tracked = Repository(str(tmp_path))._run("ls-files").stdout.splitlines()

    assert result["status"] == "initialized"
    assert "main.py" in tracked
    assert not any("__pycache__" in path for path in tracked)
    assert not any("node_modules" in path for path in tracked)


def test_existing_repository_gets_idempotent_managed_ignore_block(tmp_path):
    repository = Repository(str(tmp_path))
    repository._run("init", "-b", "main")
    repository._run("config", "user.name", "RockCore Test")
    repository._run("config", "user.email", "test@rockcore.local")
    (tmp_path / ".gitignore").write_bytes(b"custom-output/\r\n")
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    repository._run("add", "-A")
    repository._run("commit", "-m", "baseline")

    first = repository.ensure_initialized()
    first_bytes = (tmp_path / ".gitignore").read_bytes()
    second = repository.ensure_initialized()

    assert first["status"] == "existing"
    assert first["gitignore_updated"]
    assert not second["gitignore_updated"]
    assert (tmp_path / ".gitignore").read_bytes() == first_bytes
    assert first_bytes.startswith(b"custom-output/\r\n")
    assert first_bytes.count(ROCKCORE_IGNORE_START.encode()) == 1


def test_non_git_git_acceptance_uses_snapshot_and_local_html_validation(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        baseline = TestManager.capture_snapshot(tmp_path)
        (tmp_path / "bracket.html").write_text(
            '<!doctype html><html><body><section id="bracket">ok</section></body></html>'
        )
        task = _task(project)
        result = asyncio.run(engine.test_manager.run_tests(
            task, repos, baseline_snapshot=baseline, project_root=tmp_path
        ))
        assert result["status"] == "passed"
        assert result["changes"]["added"] == ["bracket.html"]
    finally:
        repos["_session"].close()


def test_local_html_validation_reports_duplicate_ids(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        baseline = TestManager.capture_snapshot(tmp_path)
        (tmp_path / "bad.html").write_text(
            '<html><body><div id="team"></div><div id="team"></div></body></html>'
        )
        result = asyncio.run(engine.test_manager.run_tests(
            _task(project), repos, baseline_snapshot=baseline, project_root=tmp_path
        ))
        assert result["status"] == "failed"
        assert "duplicate id(s): team" in result["output"]
    finally:
        repos["_session"].close()


def test_execution_group_runs_every_acceptance_command(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Suite", str(tmp_path))
        baseline = TestManager.capture_snapshot(tmp_path)
        (tmp_path / "one.py").write_text("value = 1\n", encoding="utf-8")
        (tmp_path / "two.py").write_text("value = 2\n", encoding="utf-8")
        task = _task(
            project,
            task_type="coding",
            allowed_paths=["*.py"],
            acceptance_command="python -m py_compile two.py",
            acceptance_commands=[
                "python -m py_compile one.py",
                "python -m py_compile two.py",
            ],
        )

        result = asyncio.run(engine.test_manager.validate_project(
            task, repos, baseline_snapshot=baseline, project_root=tmp_path
        ))

        assert result["status"] == "passed"
        assert result["commands"] == [
            "python -m py_compile one.py",
            "python -m py_compile two.py",
        ]
    finally:
        repos["_session"].close()


def test_provider_balance_error_requests_user_action_without_replanning(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))

        class FailedWorker:
            def __init__(self):
                self.calls = 0

            async def run(self, *args, **kwargs):
                self.calls += 1
                return {"status": "failed", "error": "Error code: 402 - Insufficient Balance"}

        worker = FailedWorker()
        task = SimpleNamespace(task_id="T001", acceptance_command="")
        result = await engine._execute_single_task_with_escalation(
            task, SimpleNamespace(job_id="JOB-1"), {}, worker, str(tmp_path)
        )
        assert result["status"] == "needs_user_action"
        assert result["failure_stage"] == "user_action_required"
        assert "Insufficient Balance" in result["error"]
        assert worker.calls == 1

    asyncio.run(scenario())


def test_internal_validation_failure_is_resumable_without_user_action():
    summary = Engine._execution_failure_summary({
        "T004": {
            "status": "failed",
            "error": "Local validation: Test command failed (python -m pytest)",
            "failure_stage": "validation_continuation",
        },
        "T005": {
            "status": "blocked",
            "error": "Blocked by failed dependencies: T004",
        },
    }, blocked=["T005"])

    assert summary["terminal_status"] == "interrupted"
    assert summary["continuation_tasks"] == ["T004"]
    assert summary["attention_tasks"] == []
    assert "Blocked by" not in summary["reason"]


def test_original_failure_survives_secondary_resume_error(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        job = repos["job"].create("JOB-ROOT-FAILURE", project.id, "build")
        task = repos["task"].create(
            "T004", job.id, "Write tests", task_type="testing"
        )
        first = {
            "status": "needs_continuation",
            "error": "Test command failed: python: command not found",
            "failure_stage": "validation_continuation",
        }
        engine._checkpoint_task(
            repos, job, task, status="interrupted", result=first,
            error=first["error"],
        )
        secondary = {
            "status": "needs_continuation",
            "error": "No file changes detected from the job baseline",
            "failure_stage": "validation_continuation",
        }
        engine._checkpoint_task(
            repos, job, task, status="interrupted", result=secondary,
            error=secondary["error"],
        )
        engine._store_job_failure(repos, job.job_id, secondary["error"])

        repos["_session"].refresh(job)
        assert job.failure_reason == first["error"]
        assert job.last_checkpoint["root_failure"]["reason"] == first["error"]
        assert len(job.last_checkpoint["failure_history"]) == 2
        assert (
            job.last_checkpoint["execution_session"]["recoverable_error"][
                "latest_attempt_reason"
            ] == secondary["error"]
        )
    finally:
        repos["_session"].close()


def test_exhausted_model_candidates_wait_for_configuration_and_resume(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    class Model404Worker:
        max_turns = 8
        max_exploration_turns = 3

        def scoped_to(self, _root):
            return self

        async def run(self, *_args, **_kwargs):
            return {
                "status": "failed",
                "error": (
                    "Error code: 404 - Not found the model kimi-k2.7-code "
                    "or Permission denied (resource_not_found_error)"
                ),
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(project_root))
            job = repos["job"].create("JOB-MODEL-404", project.id, "edit")
            task = repos["task"].create(
                "T001", job.id, "Edit", task_type="coding",
                allowed_paths=["output.py"],
            )
            engine.register_agent("worker", Model404Worker())
            engine.state_machine._states[job.job_id] = JobState.READY

            result = await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(job)
            repos["_session"].refresh(task)
            assert result["status"] == "needs_attention"
            assert job.status == "needs_attention"
            assert task.status == "needs_attention"
            attention = engine.event_bus.get_history(
                "task_needs_user_action"
            )[-1]["data"]
            assert attention["failure_stage"] == "model_configuration"
            assert "模型配置不可用" in attention["reason"]
            assert task.task_id in engine._resumable_task_ids([task])
            session = job.last_checkpoint["execution_session"]
            assert session["recoverable_error"]["task_id"] == "T001"
            assert not engine.event_bus.get_history("task_needs_continuation")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_failed_acceptance_preserves_changes_as_continuation(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    class WritingWorker:
        max_turns = 8
        max_exploration_turns = 3

        def scoped_to(self, root):
            self.root = Path(root)
            return self

        async def run(self, *_args, **_kwargs):
            (self.root / "broken.py").write_text(
                "def broken(:\n", encoding="utf-8"
            )
            return {"status": "completed", "content": "written"}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Demo", str(project_root))
            job = repos["job"].create(
                "JOB-VALIDATION-CONTINUE", project.id, "write code"
            )
            task = repos["task"].create(
                "T001", job.id, "Write code", task_type="coding",
                allowed_paths=["broken.py"],
                acceptance_command="python -m py_compile broken.py",
            )
            engine.register_agent("worker", WritingWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            result = await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert result["status"] == "interrupted"
            assert task.status == "interrupted"
            assert (project_root / "broken.py").exists()
            event = engine.event_bus.get_history(
                "task_needs_continuation"
            )[-1]["data"]
            assert event["failure_stage"] == "validation_continuation"
            assert not engine.event_bus.get_history("task_failed")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_failed_acceptance_gets_one_automatic_focused_repair(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    class RepairingWorker:
        max_turns = 8
        max_exploration_turns = 3
        calls = 0

        def scoped_to(self, root):
            self.root = Path(root)
            return self

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            content = "def ok():\n    return True\n" if self.calls > 1 else (
                "def broken(:\n"
            )
            (self.root / "result.py").write_text(content, encoding="utf-8")
            return {"status": "completed", "content": "written"}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        worker = RepairingWorker()
        try:
            project = repos["project"].create("Demo", str(project_root))
            job = repos["job"].create(
                "JOB-VALIDATION-REPAIR", project.id, "write code"
            )
            task = repos["task"].create(
                "T001", job.id, "Write code", task_type="coding",
                allowed_paths=["result.py"],
                acceptance_command="python -m py_compile result.py",
            )
            engine.register_agent("worker", worker)
            engine.state_machine._states[job.job_id] = JobState.READY

            result = await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert result["status"] == "completed"
            assert task.status == "done"
            assert worker.calls == 2
            assert engine.event_bus.get_history("task_validation_repairing")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_emergency_runs_only_after_repeated_validation_repairs_fail(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    class BrokenWorker:
        max_turns = 8
        max_exploration_turns = 3
        calls = 0

        def scoped_to(self, root):
            self.root = Path(root)
            return self

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            (self.root / "result.py").write_text(
                "def broken(:\n", encoding="utf-8"
            )
            return {"status": "completed", "content": "written"}

    class Emergency:
        calls = 0

        async def run(self, task, _project, **kwargs):
            self.calls += 1
            Path(kwargs["project_root"], "result.py").write_text(
                "def ok():\n    return True\n", encoding="utf-8"
            )
            return {"status": "completed", "fix_success": True}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        worker = BrokenWorker()
        emergency = Emergency()
        try:
            project = repos["project"].create("Demo", str(project_root))
            job = repos["job"].create(
                "JOB-VALIDATION-EMERGENCY", project.id, "write code"
            )
            task = repos["task"].create(
                "T001", job.id, "Write code", task_type="coding",
                allowed_paths=["result.py"],
                acceptance_command="python -m py_compile result.py",
            )
            engine.register_agent("worker", worker)
            engine.register_agent("emergency_coder", emergency)
            engine.state_machine._states[job.job_id] = JobState.READY

            result = await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert result["status"] == "completed"
            assert task.status == "done"
            assert worker.calls == 3
            assert emergency.calls == 1
            assert len(engine.event_bus.get_history(
                "task_validation_repairing"
            )) == 2
            assert len(engine.event_bus.get_history("task_escalating")) == 1
        finally:
            repos["_session"].close()

    asyncio.run(scenario())
