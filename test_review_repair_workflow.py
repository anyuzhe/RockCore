"""Regression coverage for review rejection repair cycles."""

import asyncio
from types import SimpleNamespace

from agents.planner import PlannerAgent
from orchestrator.agent_config import ProjectAgentConfig
from orchestrator.engine import Engine
from orchestrator.state_machine import JobState


async def _prepare_review_job(engine, repos, tmp_path,
                              request="Fix the generated tests"):
    project = repos["project"].create("Review Demo", str(tmp_path))
    job_info = await engine.create_job(project.id, request, str(tmp_path))
    job = repos["job"].get_by_id(job_info["job_id"])
    repos["constitution"].create(
        job_id=job.id,
        goal=request,
        constraints=[],
        acceptance_criteria=["Tests pass"],
        protected_paths=[],
    )
    repos["plan"].create(
        job_id=job.id,
        summary="Original implementation",
        raw_output={"summary": "Original implementation", "tasks": []},
    )
    for state in (
        JobState.GOVERNING, JobState.GOVERNED, JobState.PLANNING,
        JobState.PLAN_CHECK, JobState.READY, JobState.EXECUTING,
        JobState.TESTING,
    ):
        engine.state_machine.transition(job.job_id, state)
    repos["job"].update_status(job.job_id, "testing")
    return job


class _SequenceReviewer:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    async def run(self, _job):
        self.calls += 1
        return self.results.pop(0)


class _RepairPlanner:
    def __init__(self, decision):
        self.decision = decision
        self.calls = 0

    async def plan_review_repair(self, *_args, **_kwargs):
        self.calls += 1
        return self.decision


class _PlannerRouter:
    def __init__(self, content):
        self.content = content

    async def chat(self, *_args, **_kwargs):
        return {"content": self.content}


def test_planner_parses_review_repair_decision_and_plan():
    planner = PlannerAgent(_PlannerRouter(
        '{"repairable":true,"reason":"local fix",'
        '"plan":{"summary":"repair","tasks":[{'
        '"title":"Fix parser","allowed_paths":["tests/site.test.js"]}]}}'
    ))
    decision = asyncio.run(planner.plan_review_repair(
        SimpleNamespace(job_id="JOB-1", user_request="Fix tests"),
        {"summary": "Brittle parser", "issues": []},
    ))

    assert decision["repairable"] is True
    assert decision["reason"] == "local fix"
    assert decision["plan"]["tasks"][0]["id"] == "T001"
    assert decision["plan"]["tasks"][0]["type"] == "coding"


def test_default_project_config_enables_review_auto_repair():
    assert ProjectAgentConfig().auto_repair is True
    assert ProjectAgentConfig.standard_preset().auto_repair is True


def test_rejected_review_is_replanned_executed_and_reviewed_again(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            job = await _prepare_review_job(engine, repos, tmp_path)
            reviewer = _SequenceReviewer([
                {
                    "result": "reject",
                    "severity": "medium",
                    "summary": "HTML test regexes reject valid tag attributes.",
                    "issues": [{
                        "file": "tests/site.test.js",
                        "line": 66,
                        "problem": "Allow attributes on table tags.",
                        "severity": "medium",
                    }],
                    "suggested_actions": ["Use word-boundary tag patterns."],
                },
                {
                    "result": "pass",
                    "severity": "low",
                    "summary": "The repaired tests now accept valid attributes.",
                    "issues": [],
                },
            ])
            planner = _RepairPlanner({
                "repairable": True,
                "reason": "The findings identify concrete local test changes.",
                "plan": {
                    "summary": "Harden the HTML test parser",
                    "tasks": [{
                        "id": "T001",
                        "title": "Allow attributes in HTML tag patterns",
                        "type": "coding",
                        "description": "Update the brittle regex patterns and verify tests.",
                        "dependencies": [],
                        "allowed_paths": ["tests/site.test.js"],
                        "acceptance_command": "",
                    }],
                },
            })
            engine.register_agent("reviewer", reviewer)
            engine.register_agent("planner", planner)

            execution_calls = []

            async def execute_repair(job_value, repos_value, _baseline=None,
                                     task_ids=None, **_kwargs):
                execution_calls.append(set(task_ids or []))
                engine.state_machine.transition(job_value.job_id, JobState.EXECUTING)
                repos_value["job"].update_status(job_value.job_id, "executing")
                for task in repos_value["task"].list_by_job(job_value.id):
                    if task.task_id in (task_ids or set()):
                        repos_value["task"].update_status_by_pk(task.id, "done")
                engine.state_machine.transition(job_value.job_id, JobState.TESTING)
                repos_value["job"].update_status(job_value.job_id, "testing")
                return {"status": "completed"}

            engine._run_execution = execute_repair
            await engine._run_reviewer(
                job, repos,
                proj_config=ProjectAgentConfig(),
                complexity="normal",
            )

            repos["_session"].refresh(job)
            assert job.status == "done"
            assert engine.state_machine.get_state(job.job_id) == JobState.DONE
            assert reviewer.calls == 2
            assert planner.calls == 1
            assert execution_calls == [{"R01T001"}]
            repair_task = repos["task"].list_by_job(job.id)[0]
            assert repair_task.task_id == "R01T001"
            assert repair_task.status == "done"
            reviews = repos["review"].list_by_job(job.id)
            assert [review.result for review in reviews] == ["pass", "reject"]
            repair_round = repos["plan"].get_by_job(job.id).raw_output["repair_rounds"][0]
            assert repair_round["status"] == "passed"
            assert repair_round["plan"]["tasks"][0]["id"] == "R01T001"
            assert engine.event_bus.get_history("job_done")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_interrupted_job_resumes_same_checkpoint_without_new_job(
        tmp_path, monkeypatch):
    async def scenario():
        project_root = tmp_path / "interrupted-project"
        project_root.mkdir()
        engine = Engine(db_path=str(tmp_path / "interrupted.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Interrupted", str(project_root))
            job = repos["job"].create(
                "JOB-INTERRUPTED", project.id, "继续完成"
            )
            repos["constitution"].create(
                job.id, "继续完成", [], ["验证通过"],
                risk="medium", requires_final_review=False,
            )
            repos["plan"].create(job.id, "原计划", raw_output={"tasks": []})
            done = repos["task"].create("T001", job.id, "已完成")
            interrupted = repos["task"].create(
                "T002", job.id, "中断步骤", dependencies=["T001"]
            )
            blocked = repos["task"].create(
                "T003", job.id, "后续步骤", dependencies=["T002"]
            )
            repos["task"].update_status_by_pk(done.id, "done")
            repos["task"].update_status_by_pk(interrupted.id, "interrupted")
            repos["task"].update_status_by_pk(blocked.id, "blocked")
            repos["job"].update_status(job.job_id, "interrupted")
        finally:
            repos["_session"].close()

        calls = []

        async def fake_execution(job, repos, _baseline, **kwargs):
            calls.append(set(kwargs["task_ids"]))
            for task in repos["task"].list_by_job(job.id):
                if task.task_id in kwargs["task_ids"]:
                    repos["task"].update_status_by_pk(task.id, "done")
            engine.state_machine.transition(job.job_id, JobState.EXECUTING)
            engine.state_machine.transition(job.job_id, JobState.TESTING)
            return {"status": "completed"}

        monkeypatch.setattr(engine, "_run_execution", fake_execution)
        result = await engine.resume_attention_job(
            "JOB-INTERRUPTED", str(project_root)
        )
        check = engine._get_repos()
        try:
            jobs = check["job"].list_by_project(project.id)
            assert result["status"] == "done"
            assert len(jobs) == 1
            assert calls == [{"T002", "T003"}]
        finally:
            check["_session"].close()

    asyncio.run(scenario())


def test_planner_explains_when_rejected_review_cannot_be_repaired(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            job = await _prepare_review_job(engine, repos, tmp_path)
            reviewer = _SequenceReviewer([{
                "result": "reject",
                "severity": "high",
                "summary": "A production credential is required.",
                "issues": [{
                    "problem": "The required signing key is not available.",
                    "severity": "high",
                }],
            }])
            planner = _RepairPlanner({
                "repairable": False,
                "reason": "缺少只能由用户提供的生产签名密钥，无法验证修改结果",
                "plan": {"summary": "", "tasks": []},
            })
            engine.register_agent("reviewer", reviewer)
            engine.register_agent("planner", planner)

            await engine._run_reviewer(
                job, repos,
                proj_config=ProjectAgentConfig(),
                complexity="normal",
            )

            repos["_session"].refresh(job)
            assert job.status == "needs_attention"
            assert engine.state_machine.get_state(job.job_id) == JobState.WAITING_USER
            assert reviewer.calls == 1
            assert planner.calls == 1
            assert repos["task"].list_by_job(job.id) == []
            repair_round = repos["plan"].get_by_job(job.id).raw_output["repair_rounds"][0]
            assert repair_round["status"] == "unrepairable"
            assert "生产签名密钥" in repair_round["reason"]
            attention = engine.event_bus.get_history(
                "job_needs_attention"
            )[-1]["data"]
            assert "生产签名密钥" in attention["reason"]
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_needs_attention_resume_reuses_same_job_and_only_unfinished_tasks(
        tmp_path, monkeypatch):
    async def scenario():
        project_root = tmp_path / "resume-project"
        project_root.mkdir()
        engine = Engine(db_path=str(tmp_path / "resume.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Resume", str(project_root))
            job = repos["job"].create(
                "JOB-RESUME", project.id, "完成剩余实现"
            )
            repos["constitution"].create(
                job.id, "完成剩余实现", [], ["验证通过"],
                risk="medium", requires_final_review=False,
            )
            repos["plan"].create(
                job.id, "原计划", raw_output={"tasks": []}
            )
            done = repos["task"].create(
                "T001", job.id, "已完成分析", task_type="analysis"
            )
            attention = repos["task"].create(
                "T002", job.id, "等待外部操作", dependencies=["T001"]
            )
            blocked = repos["task"].create(
                "T003", job.id, "后续实现", dependencies=["T002"]
            )
            repos["task"].update_status_by_pk(done.id, "done")
            repos["task"].update_status_by_pk(attention.id, "needs_attention")
            repos["task"].update_status_by_pk(blocked.id, "blocked")
            original_baseline = engine.test_manager.capture_snapshot(project_root)
            repos["job"].update_checkpoint(job.job_id, {
                "job_baseline": original_baseline,
            })
            (project_root / "created-before-resume.py").write_text(
                "value = 1\n", encoding="utf-8"
            )
            repos["job"].update_status(job.job_id, "needs_attention")
            engine.state_machine.restore(job.job_id, JobState.WAITING_USER)
        finally:
            repos["_session"].close()

        calls = []

        async def fake_execution(job, repos, _baseline, **kwargs):
            calls.append({
                "job_id": job.job_id,
                "task_ids": set(kwargs["task_ids"]),
                "resume_source_job_id": kwargs["resume_source_job_id"],
                "task_statuses": {
                    task.task_id: task.status
                    for task in repos["task"].list_by_job(job.id)
                },
                "baseline_detects_existing_progress": (
                    [path for path in engine.test_manager.snapshot_diff(
                        project_root, _baseline
                    )["added"] if path == "created-before-resume.py"]
                ),
            })
            for task in repos["task"].list_by_job(job.id):
                if task.task_id in kwargs["task_ids"]:
                    repos["task"].update_status_by_pk(task.id, "done")
            engine.state_machine.transition(job.job_id, JobState.EXECUTING)
            engine.state_machine.transition(job.job_id, JobState.TESTING)
            return {"status": "completed"}

        monkeypatch.setattr(engine, "_run_execution", fake_execution)

        result = await engine.resume_attention_job(
            "JOB-RESUME", str(project_root)
        )

        check = engine._get_repos()
        try:
            jobs = check["job"].list_by_project(project.id)
            resumed = check["job"].get_by_id("JOB-RESUME")
            tasks = check["task"].list_by_job(resumed.id)
            assert result["status"] == "done"
            assert len(jobs) == 1
            assert calls == [{
                "job_id": "JOB-RESUME",
                "task_ids": {"T002", "T003"},
                "resume_source_job_id": "JOB-RESUME",
                "task_statuses": {
                    "T001": "done", "T002": "pending", "T003": "pending",
                },
                "baseline_detects_existing_progress": [
                    "created-before-resume.py"
                ],
            }]
            assert [(task.task_id, task.status) for task in tasks] == [
                ("T001", "done"), ("T002", "done"), ("T003", "done")
            ]
        finally:
            check["_session"].close()

    asyncio.run(scenario())


def test_two_repair_rounds_are_recorded_then_stop_with_clear_reason(tmp_path):
    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            job = await _prepare_review_job(engine, repos, tmp_path)
            rejection = {
                "result": "reject",
                "severity": "medium",
                "summary": "The implementation still violates the review rule.",
                "issues": [{
                    "file": "site/index.html",
                    "problem": "The required behavior is still missing.",
                    "severity": "medium",
                }],
            }
            reviewer = _SequenceReviewer([
                dict(rejection), dict(rejection), dict(rejection),
            ])
            planner = _RepairPlanner({
                "repairable": True,
                "reason": "The finding can be changed locally.",
                "plan": {
                    "summary": "Apply the focused repair",
                    "tasks": [{
                        "id": "T001",
                        "title": "Repair the reviewed behavior",
                        "type": "coding",
                        "description": "Apply and verify the requested repair.",
                        "dependencies": [],
                        "allowed_paths": ["site/index.html"],
                        "acceptance_command": "",
                    }],
                },
            })
            engine.register_agent("reviewer", reviewer)
            engine.register_agent("planner", planner)
            execution_calls = []

            async def execute_repair(job_value, repos_value, _baseline=None,
                                     task_ids=None, **_kwargs):
                execution_calls.append(set(task_ids or []))
                engine.state_machine.transition(
                    job_value.job_id, JobState.EXECUTING
                )
                repos_value["job"].update_status(job_value.job_id, "executing")
                for task in repos_value["task"].list_by_job(job_value.id):
                    if task.task_id in (task_ids or set()):
                        repos_value["task"].update_status_by_pk(task.id, "done")
                engine.state_machine.transition(
                    job_value.job_id, JobState.TESTING
                )
                repos_value["job"].update_status(job_value.job_id, "testing")
                return {"status": "completed"}

            engine._run_execution = execute_repair
            await engine._run_reviewer(
                job, repos,
                proj_config=ProjectAgentConfig(),
                complexity="normal",
            )

            repos["_session"].refresh(job)
            assert job.status == "failed"
            assert engine.state_machine.get_state(job.job_id) == JobState.FAILED
            assert reviewer.calls == 3
            assert planner.calls == 2
            assert execution_calls == [{"R01T001"}, {"R02T001"}]
            rounds = repos["plan"].get_by_job(job.id).raw_output["repair_rounds"]
            assert [item["status"] for item in rounds] == [
                "review_rejected", "review_rejected",
            ]
            assert "第 1 轮修复后审核仍未通过" in rounds[0]["reason"]
            assert "已完成 2 轮自动修复" in job.failure_reason
        finally:
            repos["_session"].close()

    asyncio.run(scenario())
