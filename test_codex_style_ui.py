"""Regression coverage for the conversation-first desktop workflow."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from app.ui.main_window import MainWindow
from app.ui.project_config_dialog import ProjectConfigDialog
from app.ui.project_panel import ProjectPanel
from app.ui.task_panel import TaskPanel


_QT_APP = None


def _app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


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
    assert not worker.output.isHidden()
    assert "正在修改 index.html" in worker.output.toPlainText()
    assert hasattr(panel, "diff_details")
    assert hasattr(panel, "test_details")


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
