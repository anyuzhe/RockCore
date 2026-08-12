import asyncio
import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pypdf import PdfReader
from PyQt6.QtWidgets import QApplication

from app.job_report import JobReportService
from app.ui.task_panel import TaskPanel
from orchestrator.engine import Engine
from storage.repositories import ProjectRepository, JobRepository, TaskRepository


_QT_APP = None


def _app():
    global _QT_APP
    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def _seed_job(tmp_path: Path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    engine = Engine(str(tmp_path / "studio.db"))
    session = engine._session_factory()
    project = ProjectRepository(session).create("Report Demo", str(project_root))
    job = JobRepository(session).create(
        "JOB-REPORT-001", project.id,
        "创建一个页面，并记录每一步的执行细节。",
    )
    task = TaskRepository(session).create(
        "T001", job.id, "创建页面", description="修改 index.html 并验收",
        allowed_paths=["index.html"], acceptance_command="node --check game.js",
    )
    session.close()
    return engine, project_root, task


def test_job_report_persists_events_redacts_secrets_and_generates_pdf(tmp_path):
    engine, project_root, _task = _seed_job(tmp_path)

    async def scenario():
        await engine.event_bus.publish(
            "model_chat", job_id="JOB-REPORT-001", task_id="T001",
            agent_type="worker", provider="deepseek", model_name="v4-pro",
            system_prompt="internal rules", messages=[{"role": "user"}],
            response="已创建页面。", duration_ms=1250,
            input_tokens=100, cached_input_tokens=20, output_tokens=30,
            estimated_cost=0.01, billable_cost=0.01, billing_mode="api",
        )
        await engine.event_bus.publish(
            "worker_tool_completed", job_id="JOB-REPORT-001", task_id="T001",
            tool="write_file", path="index.html", turn=1,
            status="success", duration_ms=12,
            arguments={"path": "index.html", "api_key": "secret-value"},
            result={"status": "written", "path": "index.html"},
        )
        await engine.event_bus.publish(
            "job_finished", job_id="JOB-REPORT-001", status="done",
        )

    asyncio.run(scenario())
    report_path = engine.job_reports.report_path(
        "JOB-REPORT-001", existing_only=True,
    )
    assert report_path == project_root / ".ai" / "reports" / "JOB-REPORT-001.pdf"
    assert report_path.is_file() and report_path.stat().st_size > 1000
    assert len(PdfReader(str(report_path)).pages) >= 1

    event_path = report_path.with_suffix(".events.jsonl")
    log_text = event_path.read_text(encoding="utf-8")
    assert "secret-value" not in log_text
    assert "[已脱敏]" in log_text
    model_event = next(
        json.loads(line) for line in log_text.splitlines()
        if '"event":"model_chat"' in line
    )
    assert "system_prompt" not in model_event["data"]
    assert model_event["data"]["prompt_message_count"] == 1


def test_historical_terminal_job_can_generate_report_without_event_log(tmp_path):
    engine, project_root, _task = _seed_job(tmp_path)
    path = engine.job_reports.generate("JOB-REPORT-001")

    assert path == project_root / ".ai" / "reports" / "JOB-REPORT-001.pdf"
    assert path.is_file()
    assert len(PdfReader(str(path)).pages) >= 1


def test_terminal_job_exposes_report_button_and_signal():
    _app()
    panel = TaskPanel()
    emitted = []
    panel.report_requested.connect(emitted.append)
    panel.set_workflow({
        "job_id": "JOB-REPORT-UI",
        "user_request": "生成报告",
        "status": "done",
        "created_at": "2026-08-12T01:00:00Z",
    })

    assert not panel.report_btn.isHidden()
    assert panel.report_btn.isEnabled()
    panel.report_btn.click()
    assert emitted and emitted[0]["job_id"] == "JOB-REPORT-UI"
    assert panel.report_btn.text() == "正在生成…"
    panel.set_report_state(path="/tmp/report.pdf", available=True)
    assert panel.report_btn.text() == "查看报告"
    panel.close()


def test_sanitizer_redacts_common_tokens():
    sanitized = JobReportService._sanitize({
        "authorization": "Bearer abcdefghijklmnop",
        "note": "token sk-abcdefghijklmnop",
    })

    assert sanitized["authorization"] == "[已脱敏]"
    assert "sk-abcdefghijklmnop" not in sanitized["note"]
