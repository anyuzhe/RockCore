"""Regression coverage for the conversation-first desktop workflow."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QMessageBox

from app.ui.main_window import MainWindow
from app.ui.project_config_dialog import ProjectConfigDialog
from app.ui.project_panel import ProjectPanel
from app.ui.settings_dialog import SettingsDialog
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
    ):
        assert field.echoMode() == QLineEdit.EchoMode.Password
        assert button.toolTip() == "显示密钥"
        button.click()
        assert field.echoMode() == QLineEdit.EchoMode.Normal
        assert button.toolTip() == "隐藏密钥"
        button.click()
        assert field.echoMode() == QLineEdit.EchoMode.Password

    dialog.close()


def test_sidebar_uses_rock_innovation_branding():
    _app()
    panel = ProjectPanel()
    labels = panel.findChildren(QLabel)
    assert any(label.text() == "RockCore" for label in labels)
    assert any(label.text() == "岩创科技" for label in labels)
    mark = next(label for label in labels if label.text() == "")
    assert not mark.pixmap().isNull()
    panel.close()
