"""Regression tests for strict worktree commit and merge integration."""

import asyncio
import subprocess
from pathlib import Path

import orchestrator.merge_manager as merge_module
import tools.git_tools as git_module
from orchestrator.merge_manager import MergeManager
from orchestrator.engine import Engine
from orchestrator.state_machine import JobState
from git.repository import Repository
from tools.git_tools import GitTools


def _git(project: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )


def _initialize_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    initialized = _git(project, "init", "-b", "main")
    if initialized.returncode != 0:
        assert _git(project, "init").returncode == 0
        assert _git(project, "checkout", "-b", "main").returncode == 0
    (project / "README.md").write_text("baseline\n", encoding="utf-8")
    assert _git(project, "add", "README.md").returncode == 0
    committed = _git(
        project,
        "-c", "user.name=Test",
        "-c", "user.email=test@example.com",
        "commit", "-m", "initial",
    )
    assert committed.returncode == 0
    return project


def test_worktree_merge_requires_and_records_verified_commit(tmp_path):
    project = _initialize_project(tmp_path)
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-VERIFY")
        assert created["status"] == "created"
        worktree = Path(created["path"])
        (worktree / "output.txt").write_text("complete\n", encoding="utf-8")
        return await manager.commit_and_merge("T001", "verified output")

    result = asyncio.run(scenario())

    assert result["status"] == "merged"
    assert result["verified"] is True
    assert result["staged_paths"] == ["output.txt"]
    assert (project / "output.txt").read_text(encoding="utf-8") == "complete\n"
    assert _git(
        project, "merge-base", "--is-ancestor", result["commit"], "main"
    ).returncode == 0
    assert _git(project, "config", "--local", "--get", "user.name").stdout.strip()
    assert _git(project, "config", "--local", "--get", "user.email").stdout.strip()
    assert manager.active_count == 0


def test_runtime_reports_are_excluded_from_task_merge(tmp_path):
    project = _initialize_project(tmp_path)
    report = project / ".ai" / "reports" / "JOB-REPORT.events.jsonl"
    report.parent.mkdir(parents=True)
    report.write_text("main report\n", encoding="utf-8")
    assert _git(project, "add", ".ai/reports/JOB-REPORT.events.jsonl").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "report baseline",
    ).returncode == 0
    manager = MergeManager(str(project), worktrees_dir=str(tmp_path / "worktrees"))

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-REPORT")
        assert created["status"] == "created"
        worktree = Path(created["path"])
        (worktree / "output.txt").write_text("task output\n", encoding="utf-8")
        (worktree / ".ai" / "reports" / "JOB-REPORT.events.jsonl").write_text(
            "task report\n", encoding="utf-8"
        )
        return await manager.commit_and_merge("T001", "produce output")

    result = asyncio.run(scenario())

    assert result["status"] == "merged"
    assert result["staged_paths"] == ["output.txt"]
    assert (project / "output.txt").read_text(encoding="utf-8") == "task output\n"
    assert (project / ".ai" / "reports" / "JOB-REPORT.events.jsonl").read_text(
        encoding="utf-8"
    ) == "main report\n"


def test_untracked_runtime_report_is_removed_from_task_worktree(tmp_path):
    project = _initialize_project(tmp_path)
    report = project / ".ai" / "reports" / "JOB-UNTRACKED.events.jsonl"
    report.parent.mkdir(parents=True)
    report.write_text("main report\n", encoding="utf-8")
    manager = MergeManager(str(project), worktrees_dir=str(tmp_path / "worktrees"))

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-UNTRACKED")
        assert created["status"] == "created"
        worktree = Path(created["path"])
        (worktree / "output.txt").write_text("task output\n", encoding="utf-8")
        copied_report = worktree / ".ai" / "reports" / report.name
        copied_report.parent.mkdir(parents=True, exist_ok=True)
        copied_report.write_text("task report\n", encoding="utf-8")
        result = await manager.commit_and_merge("T001", "produce output")
        return result, worktree

    result, worktree = asyncio.run(scenario())

    assert result["status"] == "merged"
    assert result["staged_paths"] == ["output.txt"]
    assert not worktree.exists()
    assert report.read_text(encoding="utf-8") == "main report\n"


def test_job_rollback_reverts_only_that_job_and_preserves_later_commit(tmp_path):
    project = _initialize_project(tmp_path)
    (project / "feature.txt").write_text("job output\n", encoding="utf-8")
    assert _git(project, "add", "feature.txt").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "AI JOB-ROLLBACK: T001 - feature",
    ).returncode == 0
    (project / "later.txt").write_text("keep me\n", encoding="utf-8")
    assert _git(project, "add", "later.txt").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "later independent work",
    ).returncode == 0

    result = Repository(str(project)).rollback_job("JOB-ROLLBACK")

    assert result["status"] == "rolled_back"
    assert not (project / "feature.txt").exists()
    assert (project / "later.txt").read_text(encoding="utf-8") == "keep me\n"
    assert _git(project, "status", "--porcelain").stdout == ""


def test_job_rollback_keeps_target_unchanged_when_later_edit_conflicts(tmp_path):
    project = _initialize_project(tmp_path)
    readme = project / "README.md"
    readme.write_text("job version\n", encoding="utf-8")
    assert _git(project, "add", "README.md").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "AI JOB-CONFLICT: T001 - change readme",
    ).returncode == 0
    readme.write_text("later version\n", encoding="utf-8")
    assert _git(project, "add", "README.md").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "later overlapping work",
    ).returncode == 0
    before = _git(project, "rev-parse", "HEAD").stdout.strip()

    result = Repository(str(project)).rollback_job("JOB-CONFLICT")

    assert result["status"] == "failed"
    assert readme.read_text(encoding="utf-8") == "later version\n"
    assert _git(project, "rev-parse", "HEAD").stdout.strip() == before
    assert _git(project, "status", "--porcelain").stdout == ""


def test_job_rollback_preserves_uncommitted_unrelated_user_file(tmp_path):
    project = _initialize_project(tmp_path)
    (project / "feature.txt").write_text("job output\n", encoding="utf-8")
    assert _git(project, "add", "feature.txt").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "AI JOB-DIRTY: T001 - feature",
    ).returncode == 0
    (project / "my-notes.txt").write_text("do not lose\n", encoding="utf-8")

    result = Repository(str(project)).rollback_job("JOB-DIRTY")

    assert result["status"] == "rolled_back"
    assert not (project / "feature.txt").exists()
    assert (project / "my-notes.txt").read_text(encoding="utf-8") == "do not lose\n"
    assert "?? my-notes.txt" in _git(project, "status", "--porcelain").stdout


def test_stale_task_branch_gets_a_unique_worktree_run_suffix(tmp_path):
    project = _initialize_project(tmp_path)
    assert _git(project, "branch", "ai/job-repeat/t001").returncode == 0
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-REPEAT")
        assert created["status"] == "created"
        assert created["collision_recovered"] is True
        assert created["branch"] == "ai/job-repeat/t001-run2"
        assert Path(created["path"]).name == "T001-run2"
        await manager.abort_worktree("T001")

    asyncio.run(scenario())


def test_localized_branch_collision_is_rechecked_without_parsing_error(
    tmp_path, monkeypatch,
):
    project = _initialize_project(tmp_path)
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )
    real_create = manager.git_tools.create_worktree
    calls = []

    async def localized_collision(branch, path, start_point="HEAD"):
        calls.append(branch)
        if len(calls) == 1:
            assert _git(project, "branch", branch).returncode == 0
            return {
                "status": "failed",
                "error": f"致命错误：一个名为 '{branch}' 的分支已经存在",
            }
        return await real_create(branch, path, start_point=start_point)

    monkeypatch.setattr(
        manager.git_tools, "create_worktree", localized_collision
    )

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-LOCALIZED")
        assert created["status"] == "created"
        assert created["branch"] == "ai/job-localized/t001-run2"
        assert created["collision_recovered"] is True
        await manager.abort_worktree("T001")

    asyncio.run(scenario())
    assert calls == [
        "ai/job-localized/t001",
        "ai/job-localized/t001-run2",
    ]


def test_preserved_continuation_releases_active_slot_without_deleting_files(
    tmp_path,
):
    project = _initialize_project(tmp_path)
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        first = await manager.create_task_worktree("T001", "JOB-ONE")
        first_path = Path(first["path"])
        (first_path / "checkpoint.txt").write_text(
            "partial", encoding="utf-8"
        )

        preserved = manager.preserve_worktree("T001")
        second = await manager.create_task_worktree("T001", "JOB-TWO")

        assert preserved["status"] == "preserved"
        assert (first_path / "checkpoint.txt").read_text(
            encoding="utf-8"
        ) == "partial"
        assert second["status"] == "created"
        assert second["path"] != str(first_path)
        await manager.abort_worktree("T001")

    asyncio.run(scenario())


def test_continuation_restores_source_branch_and_uncommitted_checkpoint(tmp_path):
    project = _initialize_project(tmp_path)
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        first = await manager.create_task_worktree("T001", "JOB-SOURCE")
        first_path = Path(first["path"])
        (first_path / "checkpoint.md").write_text(
            "pages 1-80 complete", encoding="utf-8"
        )
        manager.preserve_worktree("T001")

        resumed = await manager.create_task_worktree(
            "T001", "JOB-CONTINUE", source_job_id="JOB-SOURCE"
        )
        resumed_path = Path(resumed["path"])
        assert resumed["resumed_from"] == "ai/job-source/t001"
        assert "checkpoint.md" in resumed["resumed_files"]
        assert (resumed_path / "checkpoint.md").read_text(
            encoding="utf-8"
        ) == "pages 1-80 complete"
        await manager.abort_worktree("T001")

    asyncio.run(scenario())


def test_continuation_can_integrate_already_committed_source_output(tmp_path):
    project = _initialize_project(tmp_path)
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        first = await manager.create_task_worktree("T001", "JOB-SOURCE")
        first_path = Path(first["path"])
        (first_path / "completed.md").write_text("done\n", encoding="utf-8")
        assert _git(first_path, "add", "completed.md").returncode == 0
        assert _git(
            first_path, "-c", "user.name=Test", "-c",
            "user.email=test@example.com", "commit", "-m", "checkpoint",
        ).returncode == 0
        manager.preserve_worktree("T001")

        resumed = await manager.create_task_worktree(
            "T001", "JOB-CONTINUE", source_job_id="JOB-SOURCE"
        )
        assert resumed["resumed_files"] == ["completed.md"]
        merged = await manager.commit_and_merge("T001", "finish continuation")
        assert merged["status"] == "merged"

    asyncio.run(scenario())
    assert (project / "completed.md").read_text(encoding="utf-8") == "done\n"


def test_different_untracked_target_collision_is_backed_up_and_merged(tmp_path):
    project = _initialize_project(tmp_path)
    (project / "output.txt").write_text("user version\n", encoding="utf-8")
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-COLLISION")
        worktree = Path(created["path"])
        (worktree / "output.txt").write_text("worker version\n", encoding="utf-8")
        return await manager.commit_and_merge("T001", "produce output")

    result = asyncio.run(scenario())
    assert result["status"] == "merged"
    assert result["preflight"]["different"] == ["output.txt"]
    assert result["preflight"]["strategy"] == (
        "backup_target_then_apply_task_output"
    )
    assert (project / "output.txt").read_text(encoding="utf-8") == "worker version\n"
    backup = Path(result["preflight"]["backups"][0]["backup"])
    assert backup.read_text(encoding="utf-8") == "user version\n"


def test_untracked_collision_backup_is_restored_if_merge_fails(
    tmp_path, monkeypatch,
):
    project = _initialize_project(tmp_path)
    (project / "output.txt").write_text("user version\n", encoding="utf-8")
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def fail_merge(*_args, **_kwargs):
        return {
            "status": "failed",
            "phase": "merge",
            "error": "simulated internal merge failure",
        }

    monkeypatch.setattr(manager.git_tools, "merge_branch", fail_merge)

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-RESTORE")
        worktree = Path(created["path"])
        (worktree / "output.txt").write_text(
            "worker version\n", encoding="utf-8"
        )
        return await manager.commit_and_merge("T001", "produce output")

    result = asyncio.run(scenario())
    assert result["status"] == "failed"
    assert (project / "output.txt").read_text(encoding="utf-8") == "user version\n"


def test_identical_untracked_target_collision_is_resolved_and_merged(tmp_path):
    project = _initialize_project(tmp_path)
    (project / "output.txt").write_text("same\n", encoding="utf-8")
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-IDENTICAL")
        worktree = Path(created["path"])
        (worktree / "output.txt").write_text("same\n", encoding="utf-8")
        return await manager.commit_and_merge("T001", "produce output")

    result = asyncio.run(scenario())
    assert result["status"] == "merged"
    assert result["preflight"]["status"] == "resolved"
    assert result["preflight"]["identical"] == ["output.txt"]


def test_commit_failure_preserves_worktree_and_never_merges(
    tmp_path, monkeypatch,
):
    project = _initialize_project(tmp_path)
    manager = MergeManager(
        str(project), worktrees_dir=str(tmp_path / "worktrees")
    )

    async def create():
        created = await manager.create_task_worktree("T002", "JOB-PRESERVE")
        assert created["status"] == "created"
        worktree = Path(created["path"])
        (worktree / "valuable.txt").write_text("keep me\n", encoding="utf-8")
        return worktree

    worktree = asyncio.run(create())
    real_run = merge_module.run_process

    def fail_commit(command, **kwargs):
        if command[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(
                command, 128, stdout="", stderr="simulated commit failure"
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(merge_module, "run_process", fail_commit)
    result = asyncio.run(manager.commit_and_merge("T002", "must fail"))

    assert result["status"] == "failed"
    assert result["phase"] == "commit"
    assert result["preserved"] is True
    assert Path(result["worktree_path"]) == worktree
    assert (worktree / "valuable.txt").read_text(encoding="utf-8") == "keep me\n"
    assert not (project / "valuable.txt").exists()
    assert manager.get_active_worktrees()[0]["status"] == "integration_failed"

    monkeypatch.setattr(merge_module, "run_process", real_run)
    asyncio.run(manager.abort_worktree("T002"))


def test_merge_stops_when_target_checkout_fails(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="target branch is locked"
        )

    monkeypatch.setattr(git_module, "run_process", fake_run)
    result = asyncio.run(GitTools(str(tmp_path)).merge_branch("ai/task", "main"))

    assert result["status"] == "failed"
    assert result["phase"] == "checkout_target"
    assert "locked" in result["error"]
    assert calls == [["git", "checkout", "main"]]


def test_non_conflict_merge_error_is_not_misreported_as_conflict(
    tmp_path, monkeypatch,
):
    def fake_run(command, **kwargs):
        if command == ["git", "checkout", "main"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command == ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"]:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
        if command == ["git", "merge", "ai/task", "--no-edit"]:
            return subprocess.CompletedProcess(
                command, 128, stdout="", stderr="refusing unrelated histories"
            )
        if command == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected Git command: {command}")

    monkeypatch.setattr(git_module, "run_process", fake_run)
    result = asyncio.run(GitTools(str(tmp_path)).merge_branch("ai/task", "main"))

    assert result["status"] == "failed"
    assert result["phase"] == "merge"
    assert result["conflicts"] == []
    assert "unrelated histories" in result["error"]


def test_existing_user_merge_is_never_aborted(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["git", "checkout", "main"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command == ["git", "rev-parse", "-q", "--verify", "MERGE_HEAD"]:
            return subprocess.CompletedProcess(
                command, 0, stdout="existing-merge\n", stderr=""
            )
        raise AssertionError(f"Unexpected Git command: {command}")

    monkeypatch.setattr(git_module, "run_process", fake_run)
    result = asyncio.run(GitTools(str(tmp_path)).merge_branch("ai/task", "main"))

    assert result["status"] == "failed"
    assert result["phase"] == "merge_preflight"
    assert ["git", "merge", "--abort"] not in calls


def test_branch_content_conflict_is_resolved_without_user_git_action(tmp_path):
    project = _initialize_project(tmp_path)
    shared = project / "shared.txt"
    shared.write_text("baseline\n", encoding="utf-8")
    assert _git(project, "add", "shared.txt").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c",
        "user.email=test@example.com", "commit", "-m", "shared baseline",
    ).returncode == 0
    assert _git(project, "branch", "ai/task").returncode == 0

    shared.write_text("target version\n", encoding="utf-8")
    assert _git(project, "add", "shared.txt").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c",
        "user.email=test@example.com", "commit", "-m", "target change",
    ).returncode == 0

    assert _git(project, "checkout", "ai/task").returncode == 0
    shared.write_text("task version\n", encoding="utf-8")
    assert _git(project, "add", "shared.txt").returncode == 0
    assert _git(
        project, "-c", "user.name=Test", "-c",
        "user.email=test@example.com", "commit", "-m", "task change",
    ).returncode == 0
    assert _git(project, "checkout", "main").returncode == 0

    result = asyncio.run(GitTools(str(project)).merge_branch("ai/task", "main"))

    assert result["status"] == "merged"
    assert result["auto_resolved"] is True
    assert result["resolved_conflicts"] == ["shared.txt"]
    assert shared.read_text(encoding="utf-8") == "task version\n"


def test_engine_treats_git_integration_as_internal_failure(tmp_path):
    project = tmp_path / "engine-project"
    project.mkdir()

    class WritingWorker:
        def __init__(self, root=None):
            self.root = Path(root) if root else None

        def scoped_to(self, root):
            return WritingWorker(root)

        async def run(self, _task, **_kwargs):
            (self.root / "result.txt").write_text("valuable\n", encoding="utf-8")
            return {"status": "completed", "content": "implemented"}

    class FailingIntegrationManager:
        def __init__(self):
            self.abort_calls = 0

        async def create_task_worktree(self, _task_id, _job_id):
            return {"status": "created", "path": str(project)}

        async def commit_and_merge(self, task_id, _message):
            return {
                "status": "failed",
                "task_id": task_id,
                "phase": "commit",
                "error": "simulated identity failure",
                "worktree_path": str(project),
                "preserved": True,
            }

        async def abort_worktree(self, _task_id):
            self.abort_calls += 1
            return {"status": "aborted"}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        integration = FailingIntegrationManager()
        engine.merge_manager = integration
        try:
            project_row = repos["project"].create("Test", str(project))
            job = repos["job"].create("JOB-GIT-FAIL", project_row.id, "Build")
            task = repos["task"].create(
                "T001", job.id, "Write result", task_type="coding",
                allowed_paths=["result.txt"],
            )
            engine.register_agent("worker", WritingWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project),
            )

            repos["_session"].refresh(task)
            failure = engine.event_bus.get_history("task_failed")[-1]["data"]
            assert task.status == "failed"
            assert failure["failure_stage"] == "git_integration"
            assert "during commit" in failure["error"]
            assert "worktree preserved" in failure["error"]
            assert integration.abort_calls == 0
            assert (project / "result.txt").read_text(encoding="utf-8") == "valuable\n"
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_git_errors_never_require_user_action():
    for error in (
        "Merge conflict: src/main.py",
        "Project has unresolved Git conflict",
        "fatal: Unable to create .git/index.lock",
        "repository lock is held",
    ):
        assert Engine._is_user_action_required(error) is False
