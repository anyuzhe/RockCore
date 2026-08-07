"""Regression coverage for Git bootstrap, local validation, and provider failures."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from git.repository import Repository
from orchestrator.engine import Engine
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
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, cwd=tmp_path
    ).stdout.splitlines()
    assert "index.html" in tracked
    assert ".env" not in tracked


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


def test_provider_balance_error_fails_fast_without_replanning(tmp_path):
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
        assert result["status"] == "failed"
        assert "Insufficient Balance" in result["error"]
        assert worker.calls == 1

    asyncio.run(scenario())
