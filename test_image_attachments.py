"""Regression coverage for image selection, paste, persistence, and Codex input."""

import asyncio
import json
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QMimeData
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from app import image_attachments
from app.ui.main_window import MainWindow
from orchestrator.engine import Engine
from providers.codex_provider import CodexProvider


_QT_APP = None


def _app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def _png(path):
    image = QImage(12, 8, QImage.Format.Format_ARGB32)
    image.fill(QColor("#d45f19"))
    assert image.save(str(path), "PNG")
    return path


def test_image_file_is_copied_normalized_and_persisted_with_job(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(image_attachments, "app_data_dir", lambda: tmp_path / "data")
    source = _png(tmp_path / "界面截图.png")
    attachment = image_attachments.store_image_file(source)

    assert attachment["name"] == "界面截图.png"
    assert attachment["path"] != str(source)
    assert image_attachments.normalize_attachments([attachment])[0]["sha256"]

    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        result = asyncio.run(engine.create_job(
            project.id,
            "按截图修改界面",
            str(tmp_path),
            attachments=[attachment],
        ))
        job = repos["job"].get_by_id(result["job_id"])
        assert job.attachments[0]["name"] == "界面截图.png"
        assert "IMAGE ATTACHMENTS" in engine._request_with_context(job, repos)
    finally:
        repos["_session"].close()


def test_job_rejects_an_unmanaged_image_path(tmp_path, monkeypatch):
    monkeypatch.setattr(image_attachments, "app_data_dir", lambda: tmp_path / "data")
    outside = _png(tmp_path / "outside.png")
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    repos = engine._get_repos()
    try:
        project = repos["project"].create("Demo", str(tmp_path))
        with pytest.raises(ValueError, match="用户数据目录"):
            asyncio.run(engine.create_job(
                project.id,
                "检查图片",
                str(tmp_path),
                attachments=[{"name": "outside.png", "path": str(outside)}],
            ))
    finally:
        repos["_session"].close()


def test_composer_accepts_clipboard_image_without_text(tmp_path, monkeypatch):
    _app()
    monkeypatch.setattr(image_attachments, "app_data_dir", lambda: tmp_path / "data")
    window = MainWindow(None)
    window._current_project = {"name": "Demo", "root_path": str(tmp_path)}

    image = QImage(10, 10, QImage.Format.Format_ARGB32)
    image.fill(QColor("#334455"))
    mime = QMimeData()
    mime.setImageData(image)
    window.input_text.insertFromMimeData(mime)

    assert len(window._attachments) == 1
    assert window._attachments[0]["source"] == "clipboard"
    assert window.start_task_btn.isEnabled()
    assert not window.attachment_scroll.isHidden()
    assert window.input_text.toPlainText() == ""
    window.close()


def test_chatgpt_codex_transport_receives_image_argument(tmp_path):
    provider = CodexProvider(
        {},
        auth_path=tmp_path / "auth.json",
        environ={"CODEX_BINARY": sys.executable, "PATH": ""},
        login_status_runner=lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Logged in using ChatGPT", stderr=""
        ),
    )
    image_path = _png(tmp_path / "需求图.png")
    captured = {}

    async def fake_exec(prompt, **kwargs):
        captured.update(kwargs)
        output = "\n".join([
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "完成"},
            }),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ])
        return output, "", 0

    provider._run_codex_exec = fake_exec
    asyncio.run(provider.chat(
        "Inspect the image.",
        [{"role": "user", "content": "按图修改"}],
        agent_type="reviewer",
        project_root=str(tmp_path),
        attachments=[{"path": str(image_path), "name": "需求图.png"}],
    ))

    assert captured["image_paths"] == [str(image_path.resolve())]
