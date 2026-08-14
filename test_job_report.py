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
    metadata = json.loads(
        report_path.with_suffix(".report.json").read_text(encoding="utf-8")
    )
    assert metadata["format_version"] == JobReportService.REPORT_FORMAT_VERSION
    assert len(PdfReader(str(report_path)).pages) >= 1
    report_events = engine.event_bus.get_history()
    assert any(event["type"] == "job_report_started" for event in report_events)
    assert any(event["type"] == "job_report_ready" for event in report_events)

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


def test_legacy_report_is_regenerated_once_for_current_format(tmp_path):
    engine, project_root, _task = _seed_job(tmp_path)
    expected = project_root / ".ai" / "reports" / "JOB-REPORT-001.pdf"
    expected.parent.mkdir(parents=True, exist_ok=True)
    expected.write_bytes(b"%PDF-1.4\nlegacy report")

    assert engine.job_reports.report_path(
        "JOB-REPORT-001", existing_only=True,
    ) is None

    generated = engine.job_reports.generate("JOB-REPORT-001")

    assert generated == expected
    assert engine.job_reports.report_path(
        "JOB-REPORT-001", existing_only=True,
    ) == expected


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
    assert panel.stages["report"]._status == "running"
    panel.set_report_state(path="/tmp/report.pdf", available=True)
    assert panel.report_btn.text() == "查看报告"
    assert panel.stages["report"]._status == "success"
    assert "/tmp/report.pdf" in panel.stages["report"].output.toPlainText()
    panel.close()


def test_report_failure_is_visible_in_workflow_and_can_be_retried():
    _app()
    panel = TaskPanel()
    panel.set_workflow({
        "job_id": "JOB-REPORT-FAILED",
        "user_request": "生成报告",
        "status": "done",
        "created_at": "2026-08-13T01:00:00Z",
    })

    panel.set_report_state(available=True, error="PDF renderer unavailable")

    assert panel.stages["report"]._status == "failed"
    assert "PDF renderer unavailable" in panel.stages["report"].output.toPlainText()
    assert panel.report_btn.isEnabled()
    panel.close()


def test_sanitizer_redacts_common_tokens():
    sanitized = JobReportService._sanitize({
        "authorization": "Bearer abcdefghijklmnop",
        "note": "token sk-abcdefghijklmnop",
    })

    assert sanitized["authorization"] == "[已脱敏]"
    assert "sk-abcdefghijklmnop" not in sanitized["note"]


def test_report_usage_breakdown_groups_each_model_and_reconciles_job_totals():
    snapshot = {"job": {
        "usage_input_tokens": 1_500,
        "usage_cached_input_tokens": 500,
        "usage_output_tokens": 250,
        "usage_calls": 3,
        "usage_cost": 0.42,
        "usage_billable_cost": 0.12,
    }, "tasks": []}
    events = [{
        "event": "model_chat",
        "data": {
            "agent_type": "main_agent", "provider": "codex",
            "model_name": "gpt-5.6-sol", "billing_mode": "chatgpt_cli",
            "input_tokens": 500, "cached_input_tokens": 200,
            "output_tokens": 50, "estimated_cost": 0.30,
            "billable_cost": 0.0,
        },
    }, {
        "event": "model_chat",
        "data": {
            "agent_type": "worker", "provider": "deepseek",
            "model_name": "deepseek-v4-pro", "billing_mode": "api",
            "input_tokens": 800, "cached_input_tokens": 250,
            "output_tokens": 150, "estimated_cost": 0.08,
            "billable_cost": 0.08,
        },
    }]

    records, source = JobReportService._usage_records(snapshot, events)
    rows = JobReportService._model_usage_breakdown(records)

    assert source == "逐次模型事件"
    assert sum(row["input_tokens"] for row in rows) == 1_500
    assert sum(row["cached_input_tokens"] for row in rows) == 500
    assert sum(row["output_tokens"] for row in rows) == 250
    assert sum(row["calls"] for row in rows) == 3
    residual = next(row for row in rows if row["model_name"] == "历史未归属")
    assert residual["input_tokens"] == 200
    assert residual["output_tokens"] == 50
    assert residual["calls"] == 1
    deepseek = next(row for row in rows if row["model_name"] == "deepseek-v4-pro")
    assert deepseek["agent_types"] == {"worker"}
    assert deepseek["billable_cost"] == 0.08


def test_single_agent_comparison_uses_main_model_and_explicit_token_reduction():
    records = [{
        "agent_type": "main_agent", "provider": "codex",
        "model_name": "gpt-5.6-sol", "billing_mode": "chatgpt_cli",
        "input_tokens": 1_000, "cached_input_tokens": 500,
        "output_tokens": 100, "calls": 1, "cost": 0.0,
        "billable_cost": 0.0,
    }, {
        "agent_type": "worker", "provider": "deepseek",
        "model_name": "deepseek-v4-pro", "billing_mode": "api",
        "input_tokens": 4_000, "cached_input_tokens": 2_000,
        "output_tokens": 500, "calls": 4, "cost": 0.0,
        "billable_cost": 0.0,
    }]
    job = {
        "usage_input_tokens": 5_000, "usage_output_tokens": 600,
        "usage_cost": 1.25, "usage_billable_cost": 0.20,
    }

    result = JobReportService._single_agent_comparison(records, job)
    expected = result["scenarios"]["expected"]

    assert result["model_name"] == "gpt-5.6-sol"
    assert result["billing_mode"] == "chatgpt_cli"
    assert expected["input_tokens"] == 3_530
    assert expected["cached_input_tokens"] == 1_765
    assert expected["output_tokens"] == 475
    assert expected["calls"] == 3
    assert expected["total_tokens"] == 4_005
    assert round(expected["token_saving_percent"], 1) == 28.5
    assert expected["cost"] > 0
    assert expected["billable_cost"] == 0.0
    assert (
        result["scenarios"]["optimistic"]["total_tokens"]
        < expected["total_tokens"]
        < result["scenarios"]["conservative"]["total_tokens"]
    )
