"""Persistent per-Job execution history and diagnostic PDF reports."""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.paths import project_state_dir
from app.ui.time_utils import format_local_timestamp
from storage.models import Job


logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {
    "done", "failed", "cancelled", "interrupted", "needs_attention", "rolled_back",
}
_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "password", "passwd",
    "access_token", "refresh_token", "client_secret", "credential",
    "credentials", "cookie", "cookies",
}
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)sk-[A-Za-z0-9_-]{8,}"),
)


class JobReportService:
    """Record Job events and build a readable, restart-safe PDF report."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._paths: dict[str, tuple[Path, Path]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(job_id, threading.Lock())

    @staticmethod
    def _safe_job_name(job_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", str(job_id or "job"))

    def _resolve_paths(self, job_id: str) -> tuple[Path, Path]:
        cached = self._paths.get(job_id)
        if cached:
            return cached
        session = self._session_factory()
        try:
            job = session.query(Job).filter(Job.job_id == job_id).first()
            if not job or not job.project:
                raise LookupError(f"Job not found: {job_id}")
            report_dir = project_state_dir(job.project.root_path) / "reports"
            report_dir.mkdir(parents=True, exist_ok=True)
            safe_name = self._safe_job_name(job_id)
            paths = (
                report_dir / f"{safe_name}.pdf",
                report_dir / f"{safe_name}.events.jsonl",
            )
            self._paths[job_id] = paths
            return paths
        finally:
            session.close()

    def report_path(self, job_id: str, *, existing_only: bool = False) -> Path | None:
        try:
            path, _ = self._resolve_paths(job_id)
        except (LookupError, OSError):
            return None
        return path if not existing_only or path.is_file() else None

    def events(self, job_id: str) -> list[dict[str, Any]]:
        """Return the durable event recording used by reports and replay."""
        try:
            _, event_path = self._resolve_paths(job_id)
        except (LookupError, OSError):
            return []
        return self._load_events(event_path)

    async def record_event(self, event_type: str, **data):
        """Append a sanitized event without depending on the transient UI queue."""
        job_id = str(data.get("job_id") or "").strip()
        if not job_id:
            return
        try:
            _, event_path = self._resolve_paths(job_id)
            normalized = dict(data)
            if event_type == "model_chat":
                messages = normalized.pop("messages", None) or []
                system_prompt = str(normalized.pop("system_prompt", "") or "")
                normalized["prompt_message_count"] = len(messages)
                normalized["system_prompt_chars"] = len(system_prompt)
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": str(event_type),
                "data": self._sanitize(normalized),
            }
            encoded = json.dumps(
                entry, ensure_ascii=False, separators=(",", ":"), default=str,
            )
            with self._lock_for(job_id):
                with event_path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(encoded + "\n")
        except Exception as error:
            # A diagnostic recorder must never turn a valid Job into a failure.
            logger.warning("Could not persist report event for %s: %s", job_id, error)

    @classmethod
    def _sanitize(cls, value: Any, *, key: str = "", depth: int = 0) -> Any:
        if key.lower() in _SENSITIVE_KEYS:
            return "[已脱敏]"
        if depth > 8:
            return "[嵌套内容过深，已省略]"
        if isinstance(value, dict):
            return {
                str(item_key): cls._sanitize(
                    item_value, key=str(item_key), depth=depth + 1,
                )
                for item_key, item_value in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [cls._sanitize(item, depth=depth + 1) for item in value]
        if isinstance(value, str):
            text = value
            for pattern in _SECRET_PATTERNS:
                text = pattern.sub(
                    lambda match: (
                        match.group(1) + "[已脱敏]"
                        if match.lastindex else "[已脱敏]"
                    ),
                    text,
                )
            limit = 30_000
            if len(text) > limit:
                text = (
                    text[:limit]
                    + f"\n[内容共 {len(text):,} 字符，事件存档保留前 {limit:,} 字符]"
                )
            return text
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return cls._sanitize(str(value), depth=depth + 1)

    def generate(self, job_id: str) -> Path:
        """Generate or refresh the full PDF report for one persisted Job."""
        report_path, event_path = self._resolve_paths(job_id)
        temp_path = report_path.with_suffix(".tmp.pdf")
        with self._lock_for(job_id):
            snapshot = self._sanitize(self._load_snapshot(job_id))
            events = self._load_events(event_path)
            try:
                self._build_pdf(temp_path, snapshot, events)
                os.replace(temp_path, report_path)
            finally:
                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass
        return report_path

    def _load_snapshot(self, job_id: str) -> dict[str, Any]:
        session = self._session_factory()
        try:
            job = session.query(Job).filter(Job.job_id == job_id).first()
            if not job:
                raise LookupError(f"Job not found: {job_id}")
            tasks = []
            for task in sorted(job.tasks, key=lambda item: item.order):
                runs = []
                for run in sorted(task.agent_runs, key=lambda item: item.created_at):
                    runs.append({
                        "agent_type": run.agent_type,
                        "model_name": run.model_name,
                        "status": run.status,
                        "input_tokens": run.input_tokens,
                        "cached_input_tokens": run.cached_input_tokens,
                        "output_tokens": run.output_tokens,
                        "cost": run.cost,
                        "billable_cost": run.billable_cost,
                        "billing_mode": run.billing_mode,
                        "error_message": run.error_message,
                        "started_at": run.started_at,
                        "completed_at": run.completed_at,
                        "created_at": run.created_at,
                        "tool_calls": [{
                            "tool_name": call.tool_name,
                            "arguments": call.arguments or {},
                            "result_summary": call.result_summary or "",
                            "status": call.status,
                            "duration_ms": call.duration_ms,
                            "created_at": call.created_at,
                        } for call in sorted(
                            run.tool_calls, key=lambda item: item.created_at,
                        )],
                    })
                tasks.append({
                    "task_id": task.task_id,
                    "title": task.title,
                    "description": task.description,
                    "task_type": task.task_type,
                    "status": task.status,
                    "allowed_paths": task.allowed_paths or [],
                    "skills": task.skills or [],
                    "dependencies": task.dependencies or [],
                    "acceptance_command": task.acceptance_command or "",
                    "created_at": task.created_at,
                    "updated_at": task.updated_at,
                    "completed_at": task.completed_at,
                    "result_summary": task.result_summary or "",
                    "result_data": task.result_data or {},
                    "failure_reason": task.failure_reason or "",
                    "agent_runs": runs,
                    "test_runs": [{
                        "command": test.command,
                        "status": test.status,
                        "passed": test.passed,
                        "failed": test.failed,
                        "skipped": test.skipped,
                        "output": test.output or "",
                        "duration_ms": test.duration_ms,
                        "created_at": test.created_at,
                    } for test in sorted(
                        task.test_runs, key=lambda item: item.created_at,
                    )],
                })
            constitution = job.constitution
            plan = job.plan
            return {
                "job": {
                    "job_id": job.job_id,
                    "project_name": job.project.name if job.project else "",
                    "project_root": job.project.root_path if job.project else "",
                    "user_request": job.user_request,
                    "attachments": job.attachments or [],
                    "source_job_id": job.source_job_id or "",
                    "status": job.status,
                    "risk_level": job.risk_level,
                    "branch_name": job.branch_name or "",
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "completed_at": job.completed_at,
                    "usage_input_tokens": job.usage_input_tokens,
                    "usage_cached_input_tokens": job.usage_cached_input_tokens,
                    "usage_output_tokens": job.usage_output_tokens,
                    "usage_calls": job.usage_calls,
                    "usage_cost": job.usage_cost,
                    "usage_billable_cost": job.usage_billable_cost,
                    "failure_code": job.failure_code or "",
                    "failure_reason": job.failure_reason or "",
                    "recovery_hint": job.recovery_hint or "",
                    "last_checkpoint": job.last_checkpoint or {},
                },
                "constitution": ({
                    "goal": constitution.goal,
                    "constraints": constitution.constraints or [],
                    "acceptance_criteria": constitution.acceptance_criteria or [],
                    "risk": constitution.risk,
                    "protected_paths": constitution.protected_paths or [],
                    "requires_final_review": constitution.requires_final_review,
                    "raw_output": constitution.raw_output or {},
                } if constitution else None),
                "plan": ({
                    "summary": plan.summary,
                    "validated": plan.validated,
                    "validation_errors": plan.validation_errors or [],
                    "raw_output": plan.raw_output or {},
                } if plan else None),
                "tasks": tasks,
                "reviews": [{
                    "reviewer_type": review.reviewer_type,
                    "result": review.result,
                    "severity": review.severity,
                    "issues": review.issues or [],
                    "constraint_violations": review.constraint_violations or [],
                    "suggested_actions": review.suggested_actions or [],
                    "summary": review.summary or "",
                    "created_at": review.created_at,
                } for review in sorted(
                    job.reviews, key=lambda item: item.created_at,
                )],
            }
        finally:
            session.close()

    @staticmethod
    def _load_events(path: Path) -> list[dict]:
        if not path.is_file():
            return []
        events: list[dict] = []
        try:
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        item = json.loads(line)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if isinstance(item, dict):
                        events.append(item)
        except OSError as error:
            logger.warning("Could not read Job event log %s: %s", path, error)
        return events

    @staticmethod
    def _duration(start: Any, end: Any) -> str:
        if not start or not end:
            return "-"
        try:
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            seconds = max(0, int((end - start).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return "-"
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分 {seconds} 秒"
        if minutes:
            return f"{minutes} 分 {seconds} 秒"
        return f"{seconds} 秒"

    @staticmethod
    def _status(value: str) -> str:
        return {
            "created": "已创建", "governing": "裁决中", "planning": "策划中",
            "executing": "执行中", "reviewing": "审核中", "running": "运行中",
            "pending": "等待中", "done": "已完成", "completed": "已完成",
            "passed": "已通过", "pass": "已通过", "failed": "失败",
            "reject": "未通过", "rejected": "未通过", "blocked": "已阻塞",
            "cancelled": "已停止", "interrupted": "待继续",
            "needs_attention": "需用户处理", "rolled_back": "已回退",
            "success": "成功",
            "error": "错误", "skipped": "已跳过",
        }.get(str(value or ""), str(value or "-"))

    def _build_pdf(self, path: Path, snapshot: dict, events: list[dict]):
        try:
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_CENTER, TA_LEFT
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import mm
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont
            from reportlab.platypus import (
                PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                TableStyle,
            )
        except ImportError as error:
            raise RuntimeError("生成任务报告需要 reportlab") from error

        font_name = "STSong-Light"
        try:
            pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        except Exception:
            font_name = "Helvetica"
        sample = getSampleStyleSheet()
        normal = ParagraphStyle(
            "RockCoreReportBody", parent=sample["BodyText"],
            fontName=font_name, fontSize=9.2, leading=14.5,
            textColor=colors.HexColor("#282623"), wordWrap="CJK",
            spaceAfter=2 * mm,
        )
        compact = ParagraphStyle(
            "RockCoreReportCompact", parent=normal, fontSize=8.1,
            leading=12.2, spaceAfter=1 * mm,
        )
        title_style = ParagraphStyle(
            "RockCoreReportTitle", parent=normal, fontSize=22, leading=29,
            alignment=TA_CENTER, textColor=colors.HexColor("#22201D"),
            spaceAfter=5 * mm,
        )
        h1 = ParagraphStyle(
            "RockCoreReportH1", parent=normal, fontSize=15, leading=21,
            textColor=colors.HexColor("#C95216"), spaceBefore=5 * mm,
            spaceAfter=3 * mm,
        )
        h2 = ParagraphStyle(
            "RockCoreReportH2", parent=normal, fontSize=11.5, leading=17,
            textColor=colors.HexColor("#33312E"), spaceBefore=3 * mm,
            spaceAfter=2 * mm,
        )
        label_style = ParagraphStyle(
            "RockCoreReportLabel", parent=compact,
            textColor=colors.HexColor("#6A655F"),
        )
        code_style = ParagraphStyle(
            "RockCoreReportCode", parent=compact,
            backColor=colors.HexColor("#F6F3EF"),
            borderColor=colors.HexColor("#E1DAD1"), borderWidth=0.4,
            borderPadding=5, alignment=TA_LEFT,
        )

        job = snapshot["job"]
        created = job.get("created_at")
        completed = job.get("completed_at") or job.get("updated_at")
        story: list[Any] = [
            Spacer(1, 12 * mm),
            Paragraph("RockCore 任务执行报告", title_style),
            Paragraph(html.escape(job["job_id"]), h2),
            Spacer(1, 4 * mm),
        ]

        def paragraph(value: Any, style=normal):
            text = str(value if value not in (None, "") else "-")
            return Paragraph(html.escape(text).replace("\n", "<br/>"), style)

        def kv_table(rows: list[tuple[str, Any]]):
            data = [[paragraph(label, label_style), paragraph(value, compact)] for label, value in rows]
            table = Table(data, colWidths=[32 * mm, 140 * mm], hAlign="LEFT")
            table.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#E5DFD8")),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)

        def add_text(title: str, value: Any, *, code: bool = False):
            if value in (None, "", [], {}):
                return
            if title:
                story.append(Paragraph(html.escape(title), h2))
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, indent=2, default=str)
            text = str(value)
            # Smaller paragraphs are more robust than one enormous flowable.
            for start in range(0, len(text) or 1, 6_000):
                story.append(paragraph(text[start:start + 6_000], code_style if code else normal))

        kv_table([
            ("项目", job.get("project_name")),
            ("状态", self._status(job.get("status"))),
            ("风险等级", job.get("risk_level") or "-"),
            ("开始时间", format_local_timestamp(created, include_offset=True)),
            ("结束时间", format_local_timestamp(completed, include_offset=True)),
            ("总运行时间", self._duration(created, completed)),
            ("承接任务", job.get("source_job_id") or "无"),
            ("报告生成", format_local_timestamp(
                datetime.now(timezone.utc), include_offset=True,
            )),
        ])
        story.append(Spacer(1, 5 * mm))
        story.append(Paragraph("一、原始需求", h1))
        add_text("需求内容", job.get("user_request"))
        if job.get("attachments"):
            add_text("附件", job.get("attachments"), code=True)
        if job.get("failure_reason"):
            add_text("失败或中断说明", {
                "failure_code": job.get("failure_code"),
                "reason": job.get("failure_reason"),
                "recovery_hint": job.get("recovery_hint"),
            }, code=True)

        story.append(Paragraph("二、用量与成本", h1))
        kv_table([
            ("模型调用", f"{int(job.get('usage_calls') or 0):,} 次"),
            ("输入 Token", f"{int(job.get('usage_input_tokens') or 0):,}"),
            ("其中缓存输入", f"{int(job.get('usage_cached_input_tokens') or 0):,}"),
            ("输出 Token", f"{int(job.get('usage_output_tokens') or 0):,}"),
            ("等价估算", f"¥{float(job.get('usage_cost') or 0):.4f}"),
            ("可计费 API", (
                "历史数据未分类" if job.get("usage_billable_cost") is None
                else f"¥{float(job.get('usage_billable_cost') or 0):.4f}"
            )),
        ])

        story.append(Paragraph("三、裁决与策划", h1))
        phase_pairs = (
            ("需求理解与路由", "job_governing", {"job_governed"}),
            ("策划者", "job_planning", {"plan_ready", "plan_rejected"}),
            ("执行者", "job_executing", {"execution_complete"}),
            ("审核者", "job_reviewing", {"review_complete", "job_done"}),
        )
        phase_rows: list[tuple[str, Any]] = []
        for label, start_event, end_events in phase_pairs:
            start_item = next(
                (item for item in events if item.get("event") == start_event), None,
            )
            end_item = next((
                item for item in events
                if start_item
                and item.get("event") in end_events
                and str(item.get("timestamp") or "")
                >= str(start_item.get("timestamp") or "")
            ), None)
            phase_rows.append((label, self._duration(
                (start_item or {}).get("timestamp"),
                (end_item or {}).get("timestamp"),
            )))
        if events:
            kv_table(phase_rows)
        constitution = snapshot.get("constitution")
        if constitution:
            add_text("裁决目标", constitution.get("goal"))
            add_text("约束", constitution.get("constraints"), code=True)
            add_text("验收标准", constitution.get("acceptance_criteria"), code=True)
            add_text("裁决原始结果", constitution.get("raw_output"), code=True)
        else:
            add_text("裁决", "本任务未保存独立裁决记录。")
        plan = snapshot.get("plan")
        if plan:
            add_text("策划摘要", plan.get("summary"))
            add_text("策划原始结果", plan.get("raw_output"), code=True)
            if plan.get("validation_errors"):
                add_text("计划校验问题", plan.get("validation_errors"), code=True)
        else:
            add_text("策划", "本任务未保存独立策划记录。")

        story.append(PageBreak())
        story.append(Paragraph("四、执行任务明细", h1))
        for index, task in enumerate(snapshot.get("tasks") or [], 1):
            story.append(Paragraph(
                html.escape(f"{index}. {task['task_id']} - {task['title']}"), h2,
            ))
            task_events = [
                item for item in events
                if str((item.get("data") or {}).get("task_id") or "")
                == str(task.get("task_id") or "")
            ]
            task_started = next((
                item for item in task_events
                if item.get("event") == "task_running"
            ), None)
            task_ended = next((
                item for item in task_events
                if item.get("event") in {
                    "task_done", "task_failed", "task_needs_continuation",
                    "task_needs_user_action",
                }
            ), None)
            kv_table([
                ("类型 / 状态", f"{task.get('task_type')} / {self._status(task.get('status'))}"),
                ("实际执行耗时", self._duration(
                    (task_started or {}).get("timestamp"),
                    (task_ended or {}).get("timestamp"),
                )),
                ("排队至结束历时", self._duration(
                    task.get("created_at"), task.get("completed_at") or task.get("updated_at"),
                )),
                ("依赖", ", ".join(map(str, task.get("dependencies") or [])) or "无"),
                ("文件范围", ", ".join(map(str, task.get("allowed_paths") or [])) or "未限定"),
                ("Skills", ", ".join(map(str, task.get("skills") or [])) or "无"),
                ("验收命令", task.get("acceptance_command") or "无"),
            ])
            add_text("任务说明", task.get("description"))
            add_text("执行结果", task.get("result_data") or task.get("result_summary"), code=True)
            add_text("失败原因", task.get("failure_reason"))
            for test_index, test in enumerate(task.get("test_runs") or [], 1):
                add_text(f"验收 {test_index} - {self._status(test.get('status'))}", {
                    "command": test.get("command"),
                    "duration_ms": test.get("duration_ms"),
                    "passed": test.get("passed"),
                    "failed": test.get("failed"),
                    "skipped": test.get("skipped"),
                    "output": test.get("output"),
                }, code=True)

        model_events = [item for item in events if item.get("event") == "model_chat"]
        story.append(PageBreak())
        story.append(Paragraph("五、模型逐次执行记录", h1))
        if model_events:
            for index, item in enumerate(model_events, 1):
                data = item.get("data") or {}
                heading = (
                    f"{index}. {str(data.get('agent_type') or '?').upper()} - "
                    f"{data.get('provider') or '?'} / {data.get('model_name') or '-'}"
                )
                story.append(Paragraph(html.escape(heading), h2))
                kv_table([
                    ("时间", format_local_timestamp(item.get("timestamp"), include_offset=True)),
                    ("任务", data.get("task_id") or "阶段级调用"),
                    ("耗时", f"{int(data.get('duration_ms') or 0) / 1000:.1f} 秒"),
                    ("Token", (
                        f"输入 {int(data.get('input_tokens') or 0):,}"
                        f"（缓存 {int(data.get('cached_input_tokens') or 0):,}） / "
                        f"输出 {int(data.get('output_tokens') or 0):,}"
                    )),
                    ("成本", (
                        f"等价 ¥{float(data.get('estimated_cost') or 0):.4f} / "
                        f"可计费 API ¥{float(data.get('billable_cost') or 0):.4f}"
                    )),
                    ("上下文规模", (
                        f"系统提示 {int(data.get('system_prompt_chars') or 0):,} 字符，"
                        f"消息 {int(data.get('prompt_message_count') or 0):,} 条"
                    )),
                ])
                add_text(
                    "错误" if data.get("error") else "模型输出",
                    data.get("error") or data.get("response") or "（无文本输出）",
                    code=True,
                )
        else:
            add_text(
                "历史说明",
                "该任务产生于逐次事件持久化功能启用前。下方保留数据库中的模型调用元数据。",
            )
            for task in snapshot.get("tasks") or []:
                for run in task.get("agent_runs") or []:
                    add_text(
                        f"{task['task_id']} - {run.get('agent_type')} - {run.get('model_name')}",
                        {
                            key: run.get(key) for key in (
                                "status", "input_tokens", "cached_input_tokens",
                                "output_tokens", "cost", "billable_cost",
                                "billing_mode", "error_message", "created_at",
                                "completed_at",
                            )
                        },
                        code=True,
                    )

        tool_events = [
            item for item in events if item.get("event") == "worker_tool_completed"
        ]
        story.append(PageBreak())
        story.append(Paragraph("六、执行者工具调用明细", h1))
        if tool_events:
            for index, item in enumerate(tool_events, 1):
                data = item.get("data") or {}
                story.append(Paragraph(html.escape(
                    f"{index}. {data.get('task_id') or '?'} - {data.get('tool') or '?'} - "
                    f"{self._status(data.get('status'))}"
                ), h2))
                kv_table([
                    ("时间", format_local_timestamp(item.get("timestamp"), include_offset=True)),
                    ("轮次", data.get("turn") or "-"),
                    ("耗时", f"{int(data.get('duration_ms') or 0)} ms"),
                    ("目标", data.get("path") or "-"),
                ])
                add_text("调用参数", data.get("arguments"), code=True)
                add_text("工具结果", data.get("result"), code=True)
        else:
            stored_calls = [
                (task, call)
                for task in snapshot.get("tasks") or []
                for run in task.get("agent_runs") or []
                for call in run.get("tool_calls") or []
            ]
            if not stored_calls:
                add_text("工具调用", "没有可用的逐次工具调用记录。")
            for index, (task, call) in enumerate(stored_calls, 1):
                add_text(
                    f"{index}. {task['task_id']} - {call.get('tool_name')}",
                    call,
                    code=True,
                )

        story.append(Paragraph("七、审核记录", h1))
        if snapshot.get("reviews"):
            for index, review in enumerate(snapshot["reviews"], 1):
                add_text(
                    f"审核 {index} - {self._status(review.get('result'))}",
                    review,
                    code=True,
                )
        else:
            add_text("审核", "本任务未保存独立审核记录。")

        story.append(PageBreak())
        story.append(Paragraph("八、完整事件时间线", h1))
        event_names = {
            "job_created": "任务创建", "job_governing": "开始裁决",
            "job_governed": "裁决完成", "job_planning": "开始策划",
            "plan_ready": "计划就绪", "job_executing": "开始执行",
            "task_running": "执行步骤开始", "task_progress": "执行进度",
            "worker_tool_completed": "工具调用完成", "task_done": "执行步骤完成",
            "task_failed": "执行步骤失败", "test_started": "开始验收",
            "test_result": "验收结果", "job_reviewing": "开始审核",
            "review_complete": "审核完成", "task_repairing": "开始修复",
            "job_done": "任务完成", "job_failed": "任务失败",
            "job_cancelled": "任务停止", "job_finished": "任务结束",
            "job_needs_attention": "等待用户处理", "model_chat": "模型调用",
            "job_rolled_back": "需求已回退", "job_rollback_started": "开始回退",
            "job_rollback_failed": "回退未完成",
            "phase_summary": "阶段摘要",
        }
        timeline_keys = (
            "task_id", "agent_type", "provider", "model_name", "phase",
            "status", "summary", "reason", "error", "command", "turn",
            "max_turns", "duration_ms", "path", "failure_stage",
        )
        for index, item in enumerate(events, 1):
            data = item.get("data") or {}
            details = {
                key: data.get(key) for key in timeline_keys
                if data.get(key) not in (None, "", [], {})
            }
            if item.get("event") == "model_chat":
                details.pop("error", None)
                details["result"] = "失败" if data.get("error") else "成功"
            if item.get("event") == "worker_tool_completed":
                details.pop("path", None)
            timestamp = format_local_timestamp(
                item.get("timestamp"), fmt="%Y-%m-%d %H:%M:%S",
                include_offset=True,
            )
            add_text(
                f"{index}. {timestamp} - "
                f"{event_names.get(item.get('event'), item.get('event') or '事件')}",
                (
                    "；".join(
                        f"{key}={value}" for key, value in details.items()
                    )
                    if details else "无附加字段"
                ),
                code=True,
            )

        def decorate(canvas, document):
            canvas.saveState()
            canvas.setFont(font_name, 7.5)
            canvas.setFillColor(colors.HexColor("#817A72"))
            canvas.drawString(18 * mm, 10 * mm, f"RockCore - {job['job_id']}")
            canvas.drawRightString(
                A4[0] - 18 * mm, 10 * mm, f"第 {document.page} 页",
            )
            canvas.restoreState()

        document = SimpleDocTemplate(
            str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
            topMargin=16 * mm, bottomMargin=17 * mm,
            title=f"RockCore {job['job_id']} 任务执行报告",
            author="RockCore",
            subject="任务执行、模型调用、工具调用与验收诊断记录",
        )
        document.build(story, onFirstPage=decorate, onLaterPages=decorate)


def is_report_ready_status(status: str) -> bool:
    return str(status or "") in _TERMINAL_STATUSES
