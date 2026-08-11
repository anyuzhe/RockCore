"""Regression coverage for the conversation-first desktop workflow."""

import json
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QMessageBox

from app.ui.main_window import MainWindow
from app.ui.project_config_dialog import ProjectConfigDialog
from app.ui.project_panel import ProjectPanel
from app.ui.settings_dialog import SettingsDialog
from app.ui.task_panel import TaskPanel
from app.ui.time_utils import (
    as_utc_isoformat,
    format_local_timestamp,
    to_local_datetime,
)


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
        "已接收需求", "裁决者", "策划者", "执行者", "审核者",
        "策划者", "执行者", "审核者",
        "策划者", "执行者", "审核者",
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


def test_project_settings_expose_skills_and_mcp_capability_tabs(tmp_path):
    _app()
    dialog = ProjectConfigDialog(str(tmp_path))
    tab_titles = [
        dialog.tabs.tabText(index) for index in range(dialog.tabs.count())
    ]

    assert "Skills" in tab_titles
    assert "MCP" in tab_titles
    assert dialog.skills_enabled_cb.isChecked()
    assert dialog.project_skills_cb.isChecked()
    assert dialog.max_skills_spin.value() == 3
    assert dialog.skill_list.count() == 7
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

    assert settings.kimi_model.findData("kimi-k2.7") >= 0
    assert settings.ds_model.findData("deepseek-v4-pro") >= 0
    assert settings.max_cost.prefix() == "¥"
    assert settings.max_tokens.value() == 5_000_000
    assert settings.max_auto_tokens.value() == 50_000_000
    assert settings.max_api_calls.value() == 500
    assert settings.max_auto_api_calls.value() == 5_000
    assert settings.cached_input_weight.value() == 15
    assert project._agent_widgets["planner"]["model"].findData(
        "kimi-k2.7"
    ) >= 0
    assert project.worker_model.findData("deepseek-v4-pro") >= 0
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
    assert project.worker_fallback_model.currentData() == "kimi-k2.7"
    settings.close()
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
