"""Regression tests for read-only analysis tasks and dependency failures."""

import asyncio
import subprocess
import sys
from types import SimpleNamespace

from app.ui.status_constants import STATUS_STYLE
from orchestrator.engine import Engine
from orchestrator.scheduler import Scheduler
from orchestrator.state_machine import JobState


def test_status_metadata_can_load_without_qt_runtime():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "from app.ui.status_constants import STATUS_STYLE; "
                "assert STATUS_STYLE['blocked']['text'] == '已阻塞'; "
                "assert 'PyQt6' not in sys.modules"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


class _AnalysisWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {
            "status": "completed",
            "content": "The project is empty and has no existing technical stack.",
            "turns": 2,
        }


class _ReadOnlyTraceWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {
            "status": "completed",
            "content": "项目包含 src 与 tests；src 是主代码，tests 是测试目录。",
            "tool_calls": [{
                "tool": "list_files", "args": {}, "result_status": "success",
            }],
            "turns": 1,
        }


class _TextOnlyWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {
            "status": "completed",
            "content": "I did not implement the requested change.",
            "tool_calls": [],
            "turns": 1,
        }


class _NoChangeCodingWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {"status": "completed", "content": "No changes made.", "turns": 0}


class _ConditionalNoChangeCodingWorker:
    def scoped_to(self, _project_root):
        return self

    async def run(self, _task, **_kwargs):
        return {
            "status": "completed",
            "content": "检查完成，未发现需要修复的问题。",
            "no_changes": True,
            "turns": 1,
        }


def test_analysis_report_succeeds_without_file_changes(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Empty", str(project_root))
            job = repos["job"].create("JOB-ANALYSIS", project.id, "Inspect project")
            task = repos["task"].create(
                "T001",
                job.id,
                "Inspect project structure",
                task_type="analysis",
                allowed_paths=["*"],
            )
            engine.register_agent("worker", _AnalysisWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert task.status == "done"
            done_events = engine.event_bus.get_history("task_done")
            assert done_events[-1]["data"]["result"]["output"].startswith(
                "The project is empty"
            )
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_read_only_request_intent_is_distinct_from_mutating_requests():
    assert Engine._request_task_type(
        "帮我看下项目下面所有的文件夹和文件，详细说明它们的作用"
    ) == "analysis"
    assert Engine._request_task_type(
        "查看这次修改为什么失败"
    ) == "analysis"
    assert Engine._request_task_type(
        "告诉我这个问题应该怎么修改"
    ) == "analysis"
    assert Engine._request_task_type(
        "检查代码有没有明显问题"
    ) == "analysis"
    assert Engine._request_task_type(
        "分析项目结构，然后修复入口文件"
    ) == "coding"
    assert Engine._request_task_type(
        "帮我查看目录并修改错误的文件名"
    ) == "coding"
    assert Engine._request_task_type(
        "检查代码，如果有问题就修复"
    ) == "coding"
    assert Engine._request_task_type(
        "告诉我为什么失败，然后直接修复"
    ) == "coding"
    assert Engine._request_task_type(
        "分析项目并生成 PDF 报告文件"
    ) == "coding"
    assert Engine._request_task_type("实现文件列表页面") == "coding"
    assert Engine._request_task_type(
        "Inspect the project and modify the configuration"
    ) == "coding"


def test_direct_plan_uses_analysis_for_read_only_request(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        job = repos["job"].create(
            "JOB-DIRECT-ANALYSIS", project.id,
            "查看项目所有文件并解释各自用途",
        )

        plan = engine._direct_plan_data(job, repos)

        assert plan["tasks"][0]["type"] == "analysis"
        assert plan["tasks"][0]["acceptance_command"] == ""
    finally:
        repos["_session"].close()


def test_plan_normalizer_corrects_only_read_only_coding_defaults():
    read_plan = {"tasks": [{
        "id": "T001", "type": "coding",
        "title": "查看项目目录", "description": "解释所有文件的用途",
        "acceptance_command": "git diff --exit-code",
    }]}
    mixed_plan = {"tasks": [{
        "id": "T001", "type": "coding",
        "title": "检查并修复", "description": "先检查问题，然后修复代码",
        "acceptance_command": "pytest",
    }]}

    assert Engine._normalize_plan_task_types(
        read_plan, "查看项目所有文件并解释用途"
    )
    assert read_plan["tasks"][0]["type"] == "analysis"
    assert read_plan["tasks"][0]["acceptance_command"] == ""
    assert not Engine._normalize_plan_task_types(
        mixed_plan, "检查代码，如果有问题就修复"
    )
    assert mixed_plan["tasks"][0]["type"] == "coding"


def test_legacy_read_only_coding_task_is_safely_reclassified(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Legacy", str(project_root))
            request = "查看项目文件和目录，说明它们都是干什么的"
            job = repos["job"].create("JOB-LEGACY-READ", project.id, request)
            task = repos["task"].create(
                "T001", job.id, request, description=request,
                task_type="coding", allowed_paths=["*"],
            )
            engine.register_agent("worker", _ReadOnlyTraceWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert task.status == "done"
            assert task.task_type == "analysis"
            done_result = engine.event_bus.get_history("task_done")[-1]["data"]["result"]
            assert done_result["reclassified_from"] == "coding"
            assert engine.event_bus.get_history("task_reclassified")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_coding_task_text_alone_is_not_reclassified(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Coding", str(project_root))
            job = repos["job"].create(
                "JOB-CODING-GUARD", project.id, "实现文件列表页面"
            )
            task = repos["task"].create(
                "T001", job.id, "实现文件列表页面",
                description="创建页面并列出所有文件", task_type="coding",
                allowed_paths=["index.html"],
            )
            engine.register_agent("worker", _TextOnlyWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert task.status == "failed"
            assert task.task_type == "coding"
            assert not engine.event_bus.get_history("task_reclassified")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_conditional_coding_task_succeeds_without_file_changes(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Empty", str(project_root))
            job = repos["job"].create("JOB-CONDITIONAL", project.id, "Audit project")
            task = repos["task"].create(
                "T001",
                job.id,
                "修复现有问题（如有问题）",
                description="仅当发现问题时修复；若未发现则跳过。",
                task_type="coding",
                allowed_paths=["*"],
            )
            engine.register_agent("worker", _ConditionalNoChangeCodingWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            assert task.status == "done"
            assert not engine.event_bus.get_history("task_failed")
            done_result = engine.event_bus.get_history("task_done")[-1]["data"]["result"]
            assert done_result["no_changes"] is True
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_scheduler_marks_transitive_dependents_as_blocked():
    async def scenario():
        scheduler = Scheduler(max_concurrent=1)
        executed = []

        async def runner(task_id, _task_data):
            executed.append(task_id)
            raise RuntimeError("upstream failed")

        results = await scheduler.run_dag(
            [
                {"task_id": "T001", "dependencies": []},
                {"task_id": "T002", "dependencies": ["T001"]},
                {"task_id": "T003", "dependencies": ["T002"]},
            ],
            runner,
        )

        assert executed == ["T001"]
        assert results["T002"]["status"] == "blocked"
        assert results["T002"]["blocked_by"] == ["T001"]
        assert results["T003"]["status"] == "blocked"
        assert results["T003"]["blocked_by"] == ["T002"]
        assert scheduler._completed == set()
        assert scheduler._failed == {"T001", "T002", "T003"}

    asyncio.run(scenario())


def test_execution_summary_does_not_count_blocked_tasks_as_done(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        project_root = tmp_path / "project"
        project_root.mkdir()
        try:
            project = repos["project"].create("Empty", str(project_root))
            job = repos["job"].create("JOB-BLOCKED", project.id, "Build app")
            first = repos["task"].create(
                "T001", job.id, "Create app", task_type="coding"
            )
            second = repos["task"].create(
                "T002", job.id, "Add behavior", dependencies=["T001"], order=1
            )
            third = repos["task"].create(
                "T003", job.id, "Test app", task_type="testing",
                dependencies=["T002"], order=2,
            )
            engine.register_agent("worker", _NoChangeCodingWorker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job,
                repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            for task in (first, second, third):
                repos["_session"].refresh(task)
            assert first.status == "failed"
            assert second.status == "blocked"
            assert third.status == "blocked"

            summary = engine.event_bus.get_history("phase_summary")[-1]["data"]
            assert summary["details"] == {
                "done": 0,
                "failed": 1,
                "blocked": 2,
            }
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_blocked_tasks_have_a_distinct_user_facing_status():
    assert STATUS_STYLE["blocked"]["text"] == "已阻塞"


def test_execution_reason_prefers_user_action_root_cause_over_other_failures():
    summary = Engine._execution_failure_summary(
        {
            "T001": {"status": "failed", "error": "NO_PROGRESS: no new evidence"},
            "T004": {
                "status": "needs_user_action",
                "error": "Error code: 402 - Insufficient Balance",
            },
            "T005": {
                "status": "blocked",
                "error": "Blocked by failed dependencies: T004",
            },
        },
        blocked=["T005"],
    )

    assert summary["terminal_status"] == "needs_attention"
    assert summary["attention_tasks"] == ["T004"]
    assert summary["direct_failures"] == ["T001", "T004"]
    assert summary["reason"] == "Error code: 402 - Insufficient Balance"


def test_model_configuration_failure_has_an_explicit_status():
    assert STATUS_STYLE["model_configuration_failed"]["text"] == (
        "失败—模型配置不可用"
    )


def test_read_only_report_budget_allows_paginated_cross_file_reads(tmp_path):
    (tmp_path / "game.js").write_text("const state = 'playing';\n" * 450)
    (tmp_path / "index.html").write_text("<button id='restart'>Restart</button>\n" * 180)
    task = SimpleNamespace(
        task_type="analysis",
        allowed_paths=["game.js", "index.html"],
        dependencies=[],
        description="审核游戏结束与重新开始流程",
    )

    budget = Engine._estimate_task_budget(
        task, str(tmp_path), base_turns=8, base_exploration=4, mode="fast"
    )

    assert budget["max_turns"] == 36
    assert budget["exploration_turns"] == 48
