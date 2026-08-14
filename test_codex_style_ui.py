"""Regression coverage for the conversation-first desktop workflow."""

import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QLabel, QLineEdit, QMessageBox,
)

from app.ui import main_window as main_window_module
from app.ui import settings_dialog as settings_dialog_module
from app.ui.main_window import MainWindow
from app.ui.project_config_dialog import ProjectConfigDialog
from app.ui.project_panel import ProjectDialog, ProjectPanel
from app.ui.settings_dialog import SettingsDialog
from app.ui.task_panel import TaskPanel
from app.ui.time_utils import (
    as_utc_isoformat,
    format_local_timestamp,
    to_local_datetime,
)
from orchestrator.agent_config import load_project_config
from orchestrator.engine import Engine


_QT_APP = None


def _app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def test_naive_database_time_is_treated_as_utc_then_shown_locally():
    shanghai = timezone(timedelta(hours=8))

    assert format_local_timestamp(
        "2026-08-10T02:38:00",
        local_tz=shanghai,
        include_offset=True,
    ) == "2026-08-10 10:38 UTC+08:00"
    assert to_local_datetime(
        "2026-08-10T02:38:00Z", local_tz=shanghai
    ).hour == 10


def test_database_datetime_serialization_keeps_explicit_utc_marker():
    stored = datetime(2026, 8, 10, 2, 38)

    assert as_utc_isoformat(stored) == "2026-08-10T02:38:00Z"


def test_running_request_can_queue_a_followup():
    _app()
    window = MainWindow(None)
    window._current_project = {"name": "Demo", "root_path": "/tmp/demo"}
    window._running_job_id = "JOB-1"
    window._followup_source_job_id = "JOB-1"
    window.input_text.setPlainText("把标题再缩小一点")

    window._on_submit_request()

    assert window._queued_request == "把标题再缩小一点"
    assert window._queued_source_job_id == "JOB-1"
    assert not window.queue_bar.isHidden()
    assert window.input_text.toPlainText() == ""
    window.close()


def test_running_other_project_does_not_block_new_project(monkeypatch, tmp_path):
    class EngineStub:
        async def stop(self):
            pass

    _app()
    window = MainWindow(EngineStub())
    first = {"name": "First", "root_path": str(tmp_path / "first")}
    second = {"name": "Second", "root_path": str(tmp_path / "second")}
    first_key = window._project_key(first)
    second_key = window._project_key(second)
    window._running_jobs[first_key] = "JOB-FIRST"
    window._current_project = second
    window._sync_project_runtime_state()
    scheduled = []

    def capture(coroutine):
        scheduled.append(coroutine)
        coroutine.close()

    monkeypatch.setattr(main_window_module.asyncio, "ensure_future", capture)
    window.input_text.setPlainText("第二个项目的独立需求")
    window._on_submit_request()

    assert scheduled
    assert second_key in window._starting_projects
    assert second_key not in window._queued_by_project
    assert window._running_jobs[first_key] == "JOB-FIRST"
    window.close()


def test_background_job_events_are_buffered_until_job_is_selected():
    _app()
    window = MainWindow(None)
    window._selected_job_id = "JOB-FIRST"

    window._on_event("phase_summary", {
        "job_id": "JOB-SECOND",
        "phase": "worker",
        "agent_type": "worker",
        "status": "running",
        "summary": "第二个项目正在执行",
    })

    assert len(window._job_event_buffers["JOB-SECOND"]) == 1
    window._selected_job_id = "JOB-SECOND"
    window._replay_buffered_job_events("JOB-SECOND")
    assert "JOB-SECOND" not in window._job_event_buffers
    window.close()


def test_live_stage_updates_expand_inside_the_conversation():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-1",
        "user_request": "创建页面",
        "status": "executing",
        "created_at": "2026-08-07T12:00:00",
    })

    panel.update_stage("worker", "running", "正在修改 index.html")

    worker = panel.stages["worker"]
    assert worker._status == "running"
    assert worker.indicator.is_spinning
    assert panel.job_status_indicator.is_spinning
    assert not worker.output.isHidden()
    assert "正在修改 index.html" in worker.output.toPlainText()
    assert hasattr(panel, "diff_details")
    assert hasattr(panel, "test_details")

    panel.update_stage("worker", "success", "修改完成")
    panel.update_job_status("JOB-1", "done")
    assert not worker.indicator.is_spinning
    assert not panel.job_status_indicator.is_spinning
    panel.close()


def test_historical_workflow_renders_structured_requirement_understanding():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-UNDERSTANDING",
        "user_request": "做一个网页游戏",
        "status": "executing",
        "created_at": "2026-08-13T10:00:00Z",
    }, constitution={
        "goal": "创建可直接打开的网页游戏",
        "constraints": ["不引入外部依赖"],
        "acceptance_criteria": ["浏览器中可以运行"],
        "risk": "low",
        "requires_final_review": False,
        "raw_output": {
            "execution_strategy": "planned",
            "next_action": "先确认入口文件",
        },
    })

    output = panel.stages["user"].output.toPlainText()
    assert "需求：做一个网页游戏" in output
    assert "目标：创建可直接打开的网页游戏" in output
    assert "不引入外部依赖" in output
    assert "浏览器中可以运行" in output
    assert "执行策略：先规划后执行" in output
    assert "下一步：先确认入口文件" in output
    panel.close()


def test_live_main_agent_event_populates_requirement_understanding():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-LIVE-UNDERSTANDING",
        "user_request": "修复登录页",
        "status": "governing",
        "created_at": "2026-08-13T10:00:00Z",
    }
    window.task_panel.set_workflow(job)
    window._selected_job_id = job["job_id"]
    window._on_event("main_agent_decided", {
        "job_id": job["job_id"],
        "summary": "已定位登录入口",
        "goal": "修复登录页并保留现有认证流程",
        "constraints": ["不修改服务端接口"],
        "acceptance_criteria": ["登录成功和失败提示正常"],
        "risk_reasons": ["涉及用户认证"],
        "protected_paths": ["server/auth/**"],
        "execution_strategy": "planned",
        "next_action": "让策划者拆分验证步骤",
    })

    output = window.task_panel.stages["user"].output.toPlainText()
    assert "修复登录页并保留现有认证流程" in output
    assert "不修改服务端接口" in output
    assert "涉及用户认证" in output
    assert "server/auth/**" in output
    assert "先规划后执行" in output
    window.close()


def test_requirement_understanding_stays_running_until_decision_arrives():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-UNDERSTANDING-STATE",
        "user_request": "创建天气页面",
        "status": "governing",
        "created_at": "2026-08-13T10:00:00Z",
    }
    window.task_panel.set_workflow(job)
    window._selected_job_id = job["job_id"]

    understanding = window.task_panel.stages["user"]
    assert understanding._status == "running"
    assert understanding.indicator.is_spinning
    assert "正在结合项目上下文" in understanding.output.toPlainText()

    window._on_event("main_agent_decided", {
        "job_id": job["job_id"],
        "goal": "创建天气页面并显示当前地区",
        "constraints": ["不暴露定位数据"],
        "acceptance_criteria": ["天气信息可见"],
        "execution_strategy": "planned",
    })

    assert understanding._status == "success"
    assert not understanding.indicator.is_spinning
    assert "创建天气页面并显示当前地区" in understanding.output.toPlainText()
    window.close()


def test_requirement_understanding_fallback_completes_only_on_fallback_event():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-UNDERSTANDING-FALLBACK",
        "user_request": "检查项目",
        "status": "governing",
        "created_at": "2026-08-13T10:00:00Z",
    }
    window.task_panel.set_workflow(job)
    window._selected_job_id = job["job_id"]
    assert window.task_panel.stages["user"]._status == "running"

    window._on_event("main_agent_fallback", {
        "job_id": job["job_id"],
        "summary": "主控不可用，继续规则流程",
        "error": "offline",
    })

    assert window.task_panel.stages["user"]._status == "success"
    assert "确定性规则" in window.task_panel.stages["user"].output.toPlainText()
    window.close()


def test_worker_stage_describes_read_only_analysis_without_file_edits():
    _app()
    panel = TaskPanel()
    panel.set_workflow(
        {
            "job_id": "JOB-READ",
            "user_request": "查看项目结构并说明文件用途",
            "status": "executing",
            "created_at": "2026-08-12T04:00:00Z",
        },
        tasks=[{
            "task_id": "T001",
            "title": "查看项目结构",
            "description": "读取所有文件并形成说明",
            "task_type": "analysis",
            "status": "running",
            "allowed_paths": ["*"],
        }],
    )

    assert panel.stages["worker"].subtitle_label.text() == "项目读取与分析报告"
    panel.close()


def test_job_detail_header_does_not_repeat_the_submitted_request():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-NO-DUPLICATE-TITLE",
        "user_request": "这段需求只需要在对话气泡和左侧列表中显示",
        "status": "executing",
        "created_at": "2026-08-12T02:00:00Z",
    })

    assert panel.workflow_title.isHidden()
    assert "第 1/1 轮" in panel.job_meta_label.text()
    assert "JOB-NO-DUPLICATE-TITLE" in panel.job_meta_label.toolTip()
    assert "这段需求" in panel.user_output.text()

    panel.begin_new_request("Demo", "/tmp/demo")
    assert not panel.workflow_title.isHidden()
    assert panel.workflow_title.text() == "新需求"
    panel.close()


def test_submitted_request_can_copy_the_complete_original_text():
    app = _app()
    panel = TaskPanel()
    original = "第一行完整需求\n第二行包含实现细节" + "。" * 400
    panel.set_workflow({
        "job_id": "JOB-COPY",
        "user_request": original,
        "status": "done",
        "created_at": "2026-08-11T12:00:00Z",
    })

    # Copying must use the stored request even if presentation text changes.
    panel.user_output.setText("界面显示内容")
    panel._copy_original_request()

    assert app.clipboard().text() == original
    assert panel.user_frame.contextMenuPolicy() == (
        panel.user_output.contextMenuPolicy()
    ) == Qt.ContextMenuPolicy.CustomContextMenu
    assert panel.user_output.textInteractionFlags() & (
        Qt.TextInteractionFlag.TextSelectableByMouse
    )
    panel.close()


def test_needs_attention_has_distinct_checkpoint_resume_action():
    _app()
    panel = TaskPanel()
    emitted = []
    panel.attention_resume_requested.connect(emitted.append)
    job = {
        "job_id": "JOB-ATTENTION",
        "user_request": "处理加密文档",
        "status": "needs_attention",
        "failure_reason": "PDF 需要密码",
        "recovery_hint": "请替换为已解密 PDF",
        "created_at": "2026-08-12T01:00:00Z",
    }

    panel.set_workflow(job)

    assert not panel.attention_card.isHidden()
    assert "PDF 需要密码" in panel.attention_reason.text()
    assert "已解密 PDF" in panel.attention_hint.text()
    assert panel.attention_resume_btn.text() == "已处理，继续完成任务"
    assert panel.followup_btn.isHidden()

    panel.attention_resume_btn.click()
    assert [item["job_id"] for item in emitted] == ["JOB-ATTENTION"]
    assert not panel.attention_resume_btn.isEnabled()

    panel.update_job_status("JOB-ATTENTION", "executing")
    assert panel.attention_card.isHidden()
    assert not panel.followup_btn.isHidden()
    assert not panel.followup_btn.isEnabled()
    panel.close()


def test_interrupted_continue_button_resumes_same_job_immediately():
    _app()
    panel = TaskPanel()
    resumed = []
    followed_up = []
    panel.attention_resume_requested.connect(resumed.append)
    panel.followup_requested.connect(followed_up.append)
    panel.set_workflow({
        "job_id": "JOB-INTERRUPTED",
        "user_request": "完成游戏",
        "status": "interrupted",
        "created_at": "2026-08-12T01:00:00Z",
    })

    assert not panel.followup_btn.isHidden()
    panel.followup_btn.click()

    assert [item["job_id"] for item in resumed] == ["JOB-INTERRUPTED"]
    assert followed_up == []
    panel.close()


def test_worker_stage_shows_direct_attention_reason_before_blocked_dependents():
    _app()
    panel = TaskPanel()
    panel.set_workflow(
        {
            "job_id": "JOB-BALANCE",
            "user_request": "继续实现游戏",
            "status": "needs_attention",
            # Simulate an older aggregate reason racing with the authoritative
            # direct task failure. The card must show what actually stopped work.
            "failure_reason": "NO_PROGRESS: no new evidence",
            "recovery_hint": "已保留任务检查点",
            "created_at": "2026-08-12T01:00:00Z",
        },
        tasks=[
            {
                "task_id": "T004",
                "title": "玩家系统",
                "task_type": "coding",
                "status": "needs_attention",
                "failure_reason": "Error code: 402 - Insufficient Balance",
                "dependencies": [],
            },
            {
                "task_id": "T005",
                "title": "敌人系统",
                "task_type": "coding",
                "status": "blocked",
                "failure_reason": "Blocked by failed dependencies: T004",
                "dependencies": ["T004"],
            },
        ],
    )

    worker = panel.stages["worker"]
    output = worker.output.toPlainText()
    assert worker._status == "needs_attention"
    assert worker.status_label.text() == "需处理"
    assert "当前模型供应商 API 余额不足（HTTP 402）" in output
    assert "等待依赖任务完成后自动继续：T004" in output
    assert "当前模型供应商 API 余额不足（HTTP 402）" in panel.attention_reason.text()
    assert "充值当前模型供应商" in panel.attention_hint.text()
    assert "NO_PROGRESS" not in panel.attention_reason.text()
    panel.close()


def test_workflow_shows_governor_fallback_and_unrepairable_reason():
    _app()
    panel = TaskPanel()
    panel.set_workflow(
        {
            "job_id": "JOB-REPAIR",
            "user_request": "继续修复",
            "status": "failed",
            "created_at": "2026-08-10T02:38:00",
        },
        {
            "goal": "继续修复",
            "constraints": [],
            "acceptance_criteria": [],
            "risk": "medium",
            "requires_final_review": True,
            "raw_output": {
                "fallback": True,
                "error": (
                    "Error code: 429 credit_balance_exhausted: "
                    "You have no credits remaining."
                ),
            },
        },
        {
            "summary": "原始计划",
            "raw_output": {
                "tasks": [],
                "repair_rounds": [{
                    "round": 1,
                    "status": "unrepairable",
                    "repairable": False,
                    "reason": "缺少只能由用户提供的生产签名密钥",
                    "plan": {"tasks": []},
                }],
            },
        },
        [],
        [{
            "result": "reject",
            "severity": "high",
            "summary": "审核未通过",
            "issues": [],
        }],
    )

    governor_output = panel.stages["governor"].output.toPlainText()
    assert "Platform API 账户无可用余额" in governor_output
    assert "不代表 ChatGPT/Codex 用量耗尽" in governor_output
    repair_planner = panel.repair_stages["repair_1_planner"]
    assert "生产签名密钥" in repair_planner.output.toPlainText()
    assert repair_planner._status == "rejected"
    assert "生产签名密钥" in panel.agent_summary.text()
    panel.close()


def test_review_repairs_are_appended_in_conversation_order():
    _app()
    panel = TaskPanel()
    panel.set_workflow(
        {
            "job_id": "JOB-TIMELINE",
            "user_request": "修复页面",
            "status": "done",
            "created_at": "2026-08-10T03:00:00Z",
        },
        {
            "goal": "修复页面",
            "constraints": [],
            "acceptance_criteria": [],
            "risk": "medium",
            "requires_final_review": True,
        },
        {
            "summary": "首次计划",
            "raw_output": {
                "tasks": [{"id": "T001", "title": "首次实现"}],
                "repair_rounds": [
                    {
                        "round": 1,
                        "status": "review_rejected",
                        "reason": "第一轮修复后仍有问题",
                        "plan": {"tasks": [{
                            "id": "R01T001", "title": "第一轮修复",
                        }]},
                    },
                    {
                        "round": 2,
                        "status": "passed",
                        "reason": "第二轮可以修复",
                        "final_review_summary": "第二轮复审通过",
                        "plan": {"tasks": [{
                            "id": "R02T001", "title": "第二轮修复",
                        }]},
                    },
                ],
            },
        },
        [
            {"task_id": "T001", "title": "首次实现", "status": "done"},
            {"task_id": "R01T001", "title": "第一轮修复", "status": "done"},
            {"task_id": "R02T001", "title": "第二轮修复", "status": "done"},
        ],
        [
            {"result": "pass", "summary": "第二轮复审通过", "issues": []},
            {"result": "reject", "summary": "第一轮复审未通过", "issues": []},
            {"result": "reject", "summary": "首次审核未通过", "issues": []},
        ],
    )

    stages = [
        panel.trace_layout.itemAt(index).widget()
        for index in range(panel.trace_layout.count())
    ]
    assert [stage.title_label.text() for stage in stages] == [
        "理解需求", "安全与范围", "执行计划", "工作过程", "验证结果",
        "策划者", "执行者", "审核者",
        "策划者", "执行者", "审核者",
        "审计报告",
    ]
    assert stages[5].subtitle_label.text() == "第 1 轮 · 判断与修复计划"
    assert stages[8].subtitle_label.text() == "第 2 轮 · 判断与修复计划"
    assert "首次审核未通过" in panel.stages["reviewer"].output.toPlainText()
    assert "第一轮复审未通过" in panel.repair_stages[
        "repair_1_reviewer"
    ].output.toPlainText()
    assert panel.repair_stages["repair_2_reviewer"]._status == "success"
    panel.close()


def test_job_finished_keeps_sidebar_and_header_on_same_terminal_status():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-1",
        "user_request": "创建页面",
        "status": "done",
        "created_at": "2026-08-10T03:05:00",
    }
    window.project_panel.set_jobs([job])
    window.task_panel.set_workflow(job)
    window._selected_job_id = "JOB-1"

    window._on_event("job_finished", {"job_id": "JOB-1", "status": "failed"})

    sidebar_text = window.project_panel.job_list.item(0).text()
    assert "失败" in sidebar_text
    assert "已完成" not in sidebar_text
    assert window.task_panel.job_status_label.text() == "失败"
    window.close()


def test_worker_progress_card_explains_live_step_and_changes():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-PROGRESS",
        "user_request": "修改页面",
        "status": "executing",
        "created_at": "2026-08-12T10:00:00Z",
    }, tasks=[
        {"task_id": "T001", "title": "创建页面", "status": "done"},
        {"task_id": "T002", "title": "实现交互", "status": "running"},
        {"task_id": "T003", "title": "验收", "status": "pending"},
    ])

    panel.set_worker_progress(
        "T002", task_index=2, task_total=3,
        phase="正在修改文件", path="src/main.js",
        changes={"files_changed": 2, "additions": 30, "deletions": 3},
    )

    text = panel.worker_progress_label.text()
    assert not panel.worker_progress_wrap.isHidden()
    assert "第 2/3 步" in text
    assert "正在修改文件" in text
    assert "src/main.js" in text
    assert "2 个文件已更改" in text
    assert "+30" in text and "-3" in text

    panel.update_task_status("T002", "done")
    assert panel.worker_progress_wrap.isHidden()
    panel.close()


def test_checkpoint_progress_keeps_original_plan_step_numbers():
    _app()
    panel = TaskPanel()
    tasks = [
        {
            "task_id": f"T{index:03d}",
            "title": f"步骤 {index}",
            "status": "done" if index < 4 else (
                "running" if index == 4 else "pending"
            ),
        }
        for index in range(1, 11)
    ]
    panel.set_workflow({
        "job_id": "JOB-RESUME-PROGRESS",
        "user_request": "继续完成",
        "status": "executing",
        "created_at": "2026-08-12T10:00:00Z",
    }, tasks=tasks)

    # Simulate the old subset-relative event emitted while resuming T004-T010.
    panel.set_worker_progress("T004", task_index=1, task_total=7)

    assert "第 4/10 步" in panel.worker_progress_label.text()
    assert "第 1/7 步" not in panel.worker_progress_label.text()
    panel.close()


def test_worker_activity_timeline_updates_started_tool_in_place():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-ACTIVITY",
        "user_request": "修改页面",
        "status": "executing",
        "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{
        "task_id": "T001", "title": "实现页面", "status": "running",
    }])

    activity_id = panel.add_worker_activity(
        "T001", event_kind="tool_started", tool="read_file",
        path="src/main.js", turn=2, status="started",
        arguments={"path": "src/main.js"},
    )
    item = panel.worker_activity.item(activity_id)
    assert item is not None
    assert item.indicator.is_spinning
    assert item.summary.text() == "正在读取项目文件（1 项）"

    completed_id = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="read_file",
        path="src/main.js", turn=2, status="success",
        arguments={"path": "src/main.js"},
        result={"status": "success", "content": "const app = true;"},
        duration_ms=443,
    )

    assert completed_id == activity_id
    assert len(panel.worker_activity._items) == 1
    assert not item.indicator.is_spinning
    assert item.summary.text() == "已读取项目文件（1 项）"
    assert "src/main.js" in item.meta.text()
    assert "0.4s" in item.meta.text()
    assert not item.toggle.isHidden()
    panel.close()


def test_worker_activity_timeline_is_nested_inside_worker_stage():
    _app()
    panel = TaskPanel()

    assert panel.worker_activity.parentWidget() is panel.stages["worker"]
    assert panel.stages["worker"].layout().indexOf(panel.worker_activity) == 1
    assert not hasattr(panel, "processing_time_label")
    assert panel.trace_layout.indexOf(panel.stages["worker"]) >= 0
    panel.close()


def test_worker_activity_groups_consecutive_reads_and_searches_by_task():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-GROUPED-READS",
        "user_request": "了解项目结构",
        "status": "executing",
        "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{
        "task_id": "T001", "title": "读取项目", "status": "running",
    }])

    first_id = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="list_files",
        path="src", turn=1, status="success",
        result={"status": "success", "files": ["src/main.py"]},
    )
    second_id = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="read_file",
        path="src/main.py", turn=2, status="success",
        result={"status": "success", "content": "main()"},
    )
    search_id = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="search_code",
        path="MainWindow", turn=3, status="success",
        result={"status": "success", "matches": []},
    )

    assert first_id == second_id == search_id
    assert len(panel.worker_activity._items) == 1
    item = panel.worker_activity.item(first_id)
    assert item.summary.text() == "已读取项目文件（3 项）"
    assert "src、src/main.py、MainWindow" in item.meta.text()
    assert "src/main.py · read_file" in item.details.toPlainText()
    assert "MainWindow · search_code" in item.details.toPlainText()
    panel.close()


def test_worker_model_progress_is_readable_activity_not_task_dump():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-THOUGHT",
        "user_request": "修复交互",
        "status": "executing",
        "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{
        "task_id": "T001", "title": "修复交互", "status": "running",
    }])

    panel.add_model_output(
        agent_type="worker", provider="deepseek",
        response="我已经定位事件绑定，接下来会修改按钮状态并运行验证。",
        error=None, duration_ms=1200, task_id="T001",
    )

    activities = list(panel.worker_activity._items.values())
    assert activities
    assert "接下来会修改按钮状态" in activities[-1].summary.text()
    assert activities[-1].meta.text() == "本段用时 1.2s"
    assert activities[-1].summary.objectName() == "activityNarrative"
    assert activities[-1].indicator.isHidden()
    assert "模型输出：" not in panel.stages["worker"].output.toPlainText()
    panel.close()


def test_historical_worker_tools_are_hidden_after_job_finishes():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-HISTORY-ACTIVITY",
        "user_request": "修复页面",
        "status": "done",
        "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{
        "task_id": "T001", "title": "修复页面", "status": "done",
        "worker_activities": [{
            "task_id": "T001", "tool": "apply_patch", "path": "index.html",
            "status": "success", "arguments": {"path": "index.html"},
            "result": {"status": "success"}, "duration_ms": 18,
            "created_at": "2026-08-13T10:01:00Z",
        }],
    }])

    assert not panel.worker_activity.has_items
    assert panel.worker_activity.isHidden()
    panel.close()


def test_worker_activity_treats_concrete_tool_success_status_as_complete():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-WRITTEN", "user_request": "写文件",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{"task_id": "T001", "title": "写文件", "status": "running"}])

    activity_id = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="write_file",
        path="index.html", turn=1, status="written",
        result={"status": "written", "path": "index.html"},
    )

    item = panel.worker_activity.item(activity_id)
    assert item.summary.text() == "已编辑 index.html"
    assert not item.indicator.is_spinning
    panel.close()


def test_worker_activity_names_search_query_and_validation_command():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-TARGETS", "user_request": "检查实现",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{"task_id": "T001", "title": "检查", "status": "running"}])

    search_id = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="search_code",
        path="PlayerTank", status="success",
    )
    command_id = panel.add_worker_activity(
        "T001", event_kind="tool_started", tool="run_command",
        path="pytest -q", status="started",
    )

    assert panel.worker_activity.item(search_id).summary.text() == "已读取项目文件（1 项）"
    assert panel.worker_activity.item(command_id).summary.text() == "正在验证项目（1 项）"
    panel.close()


def test_worker_activity_groups_repeated_validation_commands():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-GROUPED-VALIDATION", "user_request": "检查脚本",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{"task_id": "T001", "title": "检查", "status": "running"}])

    first = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="run_command",
        path="node --check js/main.js", turn=1, status="success",
        result={"status": "success", "stdout": "OK"}, duration_ms=40,
    )
    second = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="run_command",
        path="node --check js/player.js", turn=2, status="success",
        result={"status": "success", "stdout": "OK"}, duration_ms=35,
    )
    final = panel.add_validation_activity(
        "T001", event_kind="validation_completed", status="failed",
        output="browser smoke failed",
    )

    assert first == second == final
    assert len(panel.worker_activity._items) == 1
    item = panel.worker_activity.item(first)
    assert item.summary.text() == "项目验证完成（3 项），1 项未通过"
    assert "2 项通过" in item.meta.text()
    assert "node --check js/player.js · run_command" in item.details.toPlainText()
    assert "最终验收 · acceptance" in item.details.toPlainText()
    panel.close()


def test_worker_actions_are_grouped_below_each_narrative_segment():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-NARRATIVE-GROUPS", "user_request": "修复页面",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{"task_id": "T001", "title": "修复", "status": "running"}])

    panel.add_worker_thought("T001", "先检查页面入口。", 100)
    first_read = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="read_file",
        path="index.html", turn=1, status="success",
    )
    panel.add_worker_thought("T001", "入口已确认，接下来修改样式。", 200)
    second_read = panel.add_worker_activity(
        "T001", event_kind="tool_completed", tool="read_file",
        path="style.css", turn=2, status="success",
    )

    assert first_read == "T001-project-read-0-1"
    assert second_read == "T001-project-read-0-2"
    ordered_ids = list(panel.worker_activity._items)
    assert ordered_ids == [
        "T001-narrative-1", first_read,
        "T001-narrative-2", second_read,
    ]
    panel.close()


def test_each_task_uses_its_own_elapsed_time():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-ELAPSED", "user_request": "检查项目",
        "status": "done",
        "created_at": "2026-08-13T10:00:00Z",
        "completed_at": "2026-08-13T10:10:00Z",
    }, tasks=[{
        "task_id": "T001", "title": "分析结构", "status": "done",
        "started_at": "2026-08-13T10:01:00Z",
        "completed_at": "2026-08-13T10:03:21Z",
    }, {
        "task_id": "T002", "title": "修改页面", "status": "done",
        "started_at": "2026-08-13T10:04:00Z",
        "completed_at": "2026-08-13T10:09:05Z",
    }])

    assert not panel.worker_activity.has_items
    assert panel.worker_activity.isHidden()
    assert not panel._task_timer.isActive()
    panel.close()


def test_worker_activity_shows_only_four_recent_and_one_current_action():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-COMPACT-ACTIVITY", "user_request": "修改页面",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{"task_id": "T001", "title": "修改", "status": "running"}])

    for index in range(6):
        panel.add_worker_activity(
            "T001", activity_id=f"done-{index}", event_kind="tool_completed",
            tool="write_file", path=f"file-{index}.txt", status="success",
        )
    panel.add_worker_activity(
        "T001", activity_id="current", event_kind="tool_started",
        tool="run_command", path="pytest -q", status="started",
    )

    visible = [
        activity_id for activity_id, item in panel.worker_activity._items.items()
        if not item.isHidden()
    ]
    assert visible == [
        "done-2", "done-3", "done-4", "done-5",
        "T001-verification-0-0",
    ]
    panel.close()


def test_terminal_status_clears_live_worker_activity():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-HIDE-ACTIVITY", "user_request": "修改页面",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }, tasks=[{"task_id": "T001", "title": "修改", "status": "running"}])
    panel.add_worker_activity(
        "T001", activity_id="current", event_kind="tool_started",
        tool="write_file", path="index.html", status="started",
    )
    assert panel.worker_activity.has_items
    assert not panel.worker_activity.isHidden()

    panel.update_job_status("JOB-HIDE-ACTIVITY", "done")

    assert not panel.worker_activity.has_items
    assert panel.worker_activity.isHidden()
    panel.close()


def test_terminal_result_leads_with_evidence_and_offers_event_replay():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-EVIDENCE", "user_request": "修改页面",
        "status": "done", "report_path": "/tmp/JOB-EVIDENCE.pdf",
    }, tasks=[{
        "task_id": "T001", "title": "修改页面", "status": "done",
        "result_data": {
            "changes": {"changed": ["index.html", "style.css"]},
            "integration": {"commit": "abc1234"},
        },
        "test_results": [{"command": "pytest -q", "status": "passed"}],
    }])

    assert not panel.evidence_frame.isHidden()
    assert "index.html" in panel.evidence_text.text()
    assert "pytest -q" in panel.evidence_text.text()
    assert "abc1234" in panel.evidence_text.text()
    assert not panel.replay_btn.isHidden()
    assert panel.replay_btn.text() == "重放过程"
    panel.close()


def test_live_task_timer_starts_and_finishes_on_task_events():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-LIVE-TASK-TIMER", "user_request": "修改页面",
        "status": "executing", "created_at": "2026-08-13T10:00:00Z",
    }
    window.task_panel.set_workflow(job, tasks=[{
        "task_id": "T001", "title": "修改页面", "status": "pending",
    }])
    window._selected_job_id = job["job_id"]

    window._on_event("task_running", {
        "job_id": job["job_id"], "task_id": "T001",
        "title": "修改页面", "task_index": 1, "task_total": 1,
    })
    item = window.task_panel.worker_activity.item("T001-task")
    assert "T001 · 已处理" in item.meta.text()
    assert window.task_panel._task_timings["T001"]["running"]

    window._on_event("task_done", {
        "job_id": job["job_id"], "task_id": "T001", "result": {},
    })
    assert not window.task_panel._task_timings["T001"]["running"]
    assert not window.task_panel._task_timer.isActive()
    assert "T001 · 已处理" in item.meta.text()
    window.close()


def test_successful_model_fallback_and_terminal_statuses_are_explicit():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-FALLBACK",
        "user_request": "修改页面",
        "status": "executing",
        "created_at": "2026-08-11T10:00:00",
    }
    window.project_panel.set_jobs([job])
    window.task_panel.set_workflow(job)
    window._selected_job_id = "JOB-FALLBACK"

    window._on_event("task_model_fallback_succeeded", {
        "job_id": "JOB-FALLBACK",
        "task_id": "T001",
        "from_model": "kimi-k2.7-code",
        "to_model": "kimi-k2.6",
    })
    output = window.task_panel.stages["worker"].output.toPlainText()
    assert "已自动降级" in output
    assert "任务继续执行" in output

    window._on_event("job_finished", {
        "job_id": "JOB-FALLBACK", "status": "interrupted",
    })
    assert window.task_panel.job_status_label.text() == "待继续"
    assert "待继续" in window.project_panel.job_list.item(0).text()
    assert "待继续" in window.status_label.text()
    window.close()


def test_main_window_routes_tool_and_validation_events_to_activity_timeline():
    _app()
    window = MainWindow(None)
    job = {
        "job_id": "JOB-EVENT-ACTIVITY",
        "user_request": "修复页面",
        "status": "executing",
        "created_at": "2026-08-13T10:00:00Z",
    }
    window.task_panel.set_workflow(job, tasks=[{
        "task_id": "T001", "title": "修复页面", "status": "running",
    }])
    window._selected_job_id = job["job_id"]

    window._on_event("worker_tool_started", {
        "job_id": job["job_id"], "task_id": "T001", "tool": "read_file",
        "path": "index.html", "turn": 1, "status": "started",
        "arguments": {"path": "index.html"},
    })
    started = next(iter(window.task_panel.worker_activity._items.values()))
    assert started.indicator.is_spinning

    window._on_event("worker_tool_completed", {
        "job_id": job["job_id"], "task_id": "T001", "tool": "read_file",
        "path": "index.html", "turn": 1, "status": "success",
        "arguments": {"path": "index.html"},
        "result": {"status": "success"}, "duration_ms": 25,
    })
    assert not started.indicator.is_spinning
    assert started.summary.text() == "已读取项目文件（1 项）"

    window._on_event("test_running", {
        "job_id": job["job_id"], "task_id": "T001", "command": "pytest -q",
    })
    validation = window.task_panel.worker_activity.item("T001-verification-0-0")
    assert validation.indicator.is_spinning
    window._on_event("test_result", {
        "job_id": job["job_id"], "task_id": "T001", "status": "passed",
        "output": "1 passed",
    })
    assert not validation.indicator.is_spinning
    assert validation.summary.text() == "项目验证通过（1 项）"
    window.close()


def test_switching_from_fast_to_auto_restores_the_full_pipeline(tmp_path):
    _app()
    dialog = ProjectConfigDialog(str(tmp_path))
    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("fast"))
    assert not dialog._agent_widgets["governor"]["enabled"].isChecked()

    dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData("auto"))

    assert dialog._agent_widgets["governor"]["enabled"].isChecked()
    assert dialog._agent_widgets["planner"]["enabled"].isChecked()
    assert dialog._agent_widgets["reviewer"]["enabled"].isChecked()

    dialog._agent_widgets["governor"]["enabled"].click()
    assert dialog.mode_combo.currentData() == "custom"
    dialog.close()


def test_sidebar_settings_and_context_project_removal(monkeypatch):
    _app()
    panel = ProjectPanel()
    panel.set_projects([{"name": "Demo", "root_path": "/tmp/demo"}])

    settings_opened = []
    removed = []
    panel.settings_requested.connect(lambda: settings_opened.append(True))
    panel.project_deleted.connect(removed.append)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    panel.settings_btn.click()
    panel._delete_project(panel.project_list.item(0))

    assert settings_opened == [True]
    assert removed == ["Demo"]
    assert not hasattr(panel, "delete_btn")


def test_sidebar_primary_action_creates_a_project():
    _app()
    panel = ProjectPanel()

    assert panel.new_project_btn.text() == "＋  新项目"
    assert panel.new_project_btn.toolTip() == "添加本地项目"
    assert not hasattr(panel, "new_request_btn")
    assert not hasattr(panel, "add_project_btn")
    panel.close()


def test_project_folder_selection_fills_empty_name_without_overwriting(
    monkeypatch, tmp_path,
):
    _app()
    selected = tmp_path / "天气工具"
    selected.mkdir()
    monkeypatch.setattr(
        "app.ui.project_panel.QFileDialog.getExistingDirectory",
        lambda *_args, **_kwargs: str(selected),
    )
    dialog = ProjectDialog()

    dialog._browse()
    assert dialog.path_input.text() == str(selected)
    assert dialog.name_input.text() == "天气工具"

    dialog.name_input.setText("我的自定义名称")
    dialog._browse()
    assert dialog.name_input.text() == "我的自定义名称"
    dialog.close()


def test_creating_project_persists_current_global_model_defaults(tmp_path):
    _app()
    project_root = tmp_path / "new-project"
    project_root.mkdir()
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    window = MainWindow(engine)
    window._config = {
        "agent_provider_map": {
            "governor": "codex", "planner": "deepseek",
            "worker": "kimi", "reviewer": "codex",
            "emergency_coder": "codex",
        },
        "codex": {"model": "gpt-5.6-sol"},
        "deepseek": {"model": "deepseek-v4-pro"},
        "kimi": {"model": "kimi-k2.7-code"},
    }

    window._create_project({
        "name": "Inherited", "root_path": str(project_root),
        "description": "",
    })
    config = load_project_config(str(project_root))

    assert config.planner.provider == "deepseek"
    assert config.planner.model == "deepseek-v4-pro"
    assert config.worker.provider == "kimi"
    assert config.worker.model == "kimi-k2.7-code"
    assert (project_root / ".ai" / "agents.json").exists()
    window._poll_timer.stop()
    window.engine = None
    window.close()


def test_api_key_fields_have_reveal_buttons():
    _app()
    dialog = SettingsDialog()

    for field, button in (
        (dialog.kimi_api_key, dialog.kimi_api_key_reveal),
        (dialog.ds_api_key, dialog.ds_api_key_reveal),
        (dialog.codex_api_key, dialog.codex_api_key_reveal),
    ):
        assert field.echoMode() == QLineEdit.EchoMode.Password
        assert button.toolTip() == "显示密钥"
        button.click()
        assert field.echoMode() == QLineEdit.EchoMode.Normal
        assert button.toolTip() == "隐藏密钥"
        button.click()
        assert field.echoMode() == QLineEdit.EchoMode.Password

    dialog.close()


def test_settings_no_longer_exposes_model_scoring():
    _app()
    dialog = SettingsDialog()

    tab_titles = [
        dialog.tabs.tabText(index) for index in range(dialog.tabs.count())
    ]
    assert "模型评分" not in tab_titles
    assert not hasattr(dialog, "scoring_text")
    dialog.close()


def test_old_default_rmb_limit_migrates_to_ten_without_overwriting_custom(
    monkeypatch, tmp_path,
):
    config_file = tmp_path / "config.json"
    monkeypatch.setattr(settings_dialog_module, "CONFIG_PATH", config_file)
    base = {
        "workflow_defaults_version": 3,
        "pricing_currency_version": 1,
        "budget_policy_version": 2,
        "budget": {"max_cost_cny": 3.60},
    }
    config_file.write_text(json.dumps(base), encoding="utf-8")

    migrated = settings_dialog_module.load_config()
    assert migrated["budget"]["max_cost_cny"] == 10.00
    assert migrated["budget_policy_version"] == 3

    base["budget"]["max_cost_cny"] = 8.00
    config_file.write_text(json.dumps(base), encoding="utf-8")
    custom = settings_dialog_module.load_config()
    assert custom["budget"]["max_cost_cny"] == 8.00


def test_project_settings_expose_plugins_skills_and_mcp_capability_tabs(tmp_path):
    _app()
    dialog = ProjectConfigDialog(str(tmp_path))
    tab_titles = [
        dialog.tabs.tabText(index) for index in range(dialog.tabs.count())
    ]

    assert "Skills" in tab_titles
    assert "常用插件" in tab_titles
    assert "MCP" in tab_titles
    assert dialog.skills_enabled_cb.isChecked()
    assert dialog.project_skills_cb.isChecked()
    assert dialog.max_skills_spin.value() == 3
    assert dialog.skill_list.count() == 7
    assert dialog.plugins_enabled_cb.isChecked()
    assert dialog.plugin_list.count() == 5
    assert not dialog.mcp_enabled_cb.isChecked()
    assert json.loads(dialog.mcp_servers_text.toPlainText()) == []
    dialog.close()


def test_usage_header_separates_equivalent_and_billable_api_cost():
    text = TaskPanel._format_usage({
        "input_tokens": 180_863,
        "output_tokens": 9_159,
        "calls": 9,
        "cost": 1.6269,
        "billable_cost": 0.0213,
    }, "总用量")

    assert "等价估算 ¥1.6269" in text
    assert "可计费 API ¥0.0213" in text


def test_historical_usage_does_not_guess_billable_cost():
    text = TaskPanel._format_usage({
        "input_tokens": 10,
        "output_tokens": 2,
        "calls": 1,
        "cost": 0.1,
        "billable_cost": None,
    })

    assert "历史记录未区分" in text


def test_new_kimi_and_deepseek_models_appear_in_global_and_project_settings(tmp_path):
    _app()
    settings = SettingsDialog()
    project = ProjectConfigDialog(str(tmp_path))

    assert settings.kimi_model.findData("kimi-k2.7-code") >= 0
    assert settings.ds_model.findData("deepseek-v4-pro") >= 0
    assert settings.ds_model.currentData() == "deepseek-v4-pro"
    assert settings.max_cost.prefix() == "¥"
    assert settings.max_tokens.value() == 5_000_000
    assert settings.max_auto_tokens.value() == 50_000_000
    assert settings.max_api_calls.value() == 500
    assert settings.max_auto_api_calls.value() == 5_000
    assert settings.cached_input_weight.value() == 15
    assert project._agent_widgets["planner"]["model"].findData(
        "kimi-k2.7-code"
    ) >= 0
    assert project.worker_model.findData("deepseek-v4-pro") >= 0
    assert project.worker_model.currentData() == "deepseek-v4-pro"
    assert project._agent_widgets["governor"]["model"].findData(
        "gpt-5.6-sol"
    ) >= 0
    assert project._agent_widgets["governor"]["reasoning"].findData(
        "high"
    ) >= 0
    assert project._agent_widgets["emergency_coder"]["reasoning"].findData(
        "max"
    ) >= 0
    assert project.worker_emergency_after.value() == 3
    assert project.worker_fallback_model.currentData() == "kimi-k2.7-code"
    settings.close()
    project.close()


def test_project_agent_combos_keep_visible_text_on_native_dark_palette(tmp_path):
    _app()
    project = ProjectConfigDialog(str(tmp_path))

    combos = project.findChildren(QComboBox)
    assert combos
    for combo in combos:
        assert combo.count() > 0
        assert combo.currentText()
        assert combo.minimumHeight() >= 32
        assert combo.palette().color(QPalette.ColorRole.ButtonText) == QColor(
            "#25231f"
        )
        assert combo.palette().color(QPalette.ColorRole.Base) == QColor("#ffffff")
        assert "color: #25231f" in combo.styleSheet()

    project.close()


def test_usage_header_shows_live_budget_breakdown():
    text = TaskPanel._format_usage({
        "input_tokens": 100,
        "output_tokens": 20,
        "calls": 1,
        "cost": 0.01,
        "billable_cost": 0.005,
        "budget": {
            "used_tokens": 90,
            "reserved_tokens": 30,
            "remaining_tokens": 4_999_880,
            "max_auto_tokens": 50_000_000,
            "billable_cost": 0.005,
            "hard_cost_limit_cny": 3.6,
        },
    })

    assert "有效已用 90" in text
    assert "已预留 30" in text
    assert "剩余 4,999,880" in text
    assert "最高自动扩容 50,000,000" in text
    assert "人民币硬上限 ¥0.0050/¥3.60" in text


def test_sidebar_uses_rock_innovation_branding():
    _app()
    panel = ProjectPanel()
    labels = panel.findChildren(QLabel)
    assert any(label.text() == "RockCore" for label in labels)
    assert any(label.text() == "岩创科技" for label in labels)
    mark = next(label for label in labels if label.text() == "")
    assert not mark.pixmap().isNull()
    panel.close()


def test_sidebar_groups_followup_jobs_as_one_conversation():
    _app()
    panel = ProjectPanel()
    panel.set_jobs([
        {
            "job_id": "JOB-2", "execution_session_id": "SESSION-1",
            "user_request": "缩小标题", "status": "done",
            "created_at": "2026-08-13T02:00:00Z",
        },
        {
            "job_id": "JOB-1", "execution_session_id": "SESSION-1",
            "user_request": "创建页面", "status": "done",
            "created_at": "2026-08-13T01:00:00Z",
        },
    ])

    assert panel.job_list.count() == 1
    assert "2 轮" in panel.job_list.item(0).text()
    assert "JOB-" not in panel.job_list.item(0).text()
    assert "JOB-2" in panel.job_list.item(0).toolTip()
    panel.close()


def test_conversation_panel_shows_previous_turns_and_hides_internal_ids():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-2", "execution_session_id": "SESSION-1",
        "user_request": "缩小标题", "status": "done",
        "turn_number": 2, "turn_total": 2,
        "created_at": "2026-08-13T02:00:00Z",
    })
    panel.set_conversation(
        {"session_id": "SESSION-1", "title": "创建页面"},
        [
            {"user_request": "创建页面", "status": "done", "summary": "已创建"},
            {"user_request": "缩小标题", "status": "done", "summary": "已调整"},
        ],
    )

    assert not panel.conversation_history.isHidden()
    assert "创建页面" in panel.conversation_history_text.text()
    assert "JOB-2" not in panel.job_meta_label.text()
    assert "JOB-2" in panel.job_meta_label.toolTip()
    assert panel.workflow_title.text() == "创建页面"
    panel.close()


def test_primary_worker_steps_hide_internal_task_ids():
    _app()
    panel = TaskPanel()
    panel.set_workflow(
        {
            "job_id": "JOB-1", "user_request": "修改页面",
            "status": "executing", "created_at": "2026-08-13T02:00:00Z",
        },
        tasks=[{
            "task_id": "T001", "title": "调整标题",
            "task_type": "coding", "status": "running",
        }],
    )

    output = panel.stages["worker"].output.toPlainText()
    assert "步骤 1" in output
    assert "T001" not in output
    assert "T001" in panel.stages["worker"].toolTip()
    panel.close()
