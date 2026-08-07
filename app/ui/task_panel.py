"""Codex-style requirement conversation and inline execution trace."""

import json
from datetime import datetime

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QTextCursor
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


STATUS_STYLE = {
    "done": {"icon": "✓", "color": "#55a86b", "text": "已完成"},
    "success": {"icon": "✓", "color": "#55a86b", "text": "已完成"},
    "passed": {"icon": "✓", "color": "#55a86b", "text": "已通过"},
    "failed": {"icon": "!", "color": "#d96868", "text": "失败"},
    "blocked": {"icon": "−", "color": "#8f8f98", "text": "已阻塞"},
    "rejected": {"icon": "!", "color": "#d9914f", "text": "未通过"},
    "fallback": {"icon": "!", "color": "#d9914f", "text": "已降级"},
    "cancelled": {"icon": "×", "color": "#8f8f98", "text": "已停止"},
    "skipped": {"icon": "−", "color": "#8f8f98", "text": "已跳过"},
    "executing": {"icon": "●", "color": "#d4a94f", "text": "执行中"},
    "reviewing": {"icon": "●", "color": "#d4a94f", "text": "审核中"},
    "governing": {"icon": "●", "color": "#d4a94f", "text": "分析中"},
    "planning": {"icon": "●", "color": "#d4a94f", "text": "规划中"},
    "running": {"icon": "●", "color": "#d4a94f", "text": "运行中"},
    "created": {"icon": "○", "color": "#8f8f98", "text": "等待中"},
    "pending": {"icon": "○", "color": "#74747e", "text": "等待中"},
    "idle": {"icon": "○", "color": "#74747e", "text": "等待中"},
}

ACTIVE_STATUSES = {"executing", "reviewing", "governing", "planning", "running"}


class StatusIndicator(QWidget):
    """Static status glyph that becomes a rotating ring while work is active."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon = "○"
        self._color = QColor("#74747e")
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._advance)
        self.setFixedSize(17, 17)

    @property
    def is_spinning(self) -> bool:
        return self._timer.isActive()

    def set_status(self, status: str, style: dict):
        self._icon = style["icon"]
        self._color = QColor(style["color"])
        if status in ACTIVE_STATUSES:
            if not self._timer.isActive():
                self._angle = 0
                self._timer.start()
        else:
            self._timer.stop()
        self.show()
        self.update()

    def clear(self):
        self._timer.stop()
        self.hide()

    def _advance(self):
        self._angle = (self._angle - 30) % 360
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._timer.isActive():
            pen = QPen(self._color, 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(QRectF(2.5, 2.5, 12, 12), self._angle * 16, 275 * 16)
            return

        painter.setPen(self._color)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._icon)


class WorkflowStage(QFrame):
    """One collapsible line in the assistant's execution trace."""

    def __init__(self, key: str, title: str, subtitle: str, parent=None):
        super().__init__(parent)
        self.key = key
        self._status = "pending"
        self._lines: list[str] = []
        self.setObjectName("workflowStage")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(9)
        self.indicator = StatusIndicator()
        header.addWidget(self.indicator)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("stageTitle")
        header.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("stageSubtitle")
        header.addWidget(self.subtitle_label, 1)

        self.status_label = QLabel("等待中")
        self.status_label.setObjectName("stageStatus")
        header.addWidget(self.status_label)

        self.toggle = QToolButton()
        self.toggle.setObjectName("disclosureButton")
        self.toggle.setText("⌄")
        self.toggle.setToolTip("展开阶段输出")
        self.toggle.setFixedSize(24, 24)
        self.toggle.clicked.connect(self._toggle_output)
        header.addWidget(self.toggle)
        layout.addLayout(header)

        self.output = QPlainTextEdit()
        self.output.setObjectName("stageOutput")
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(200)
        self.output.setMinimumHeight(54)
        self.output.setMaximumHeight(190)
        self.output.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.output.hide()
        layout.addWidget(self.output)
        self.set_status("pending")

    def _toggle_output(self):
        self.set_expanded(not self.output.isVisible())

    def set_expanded(self, expanded: bool):
        has_content = bool(self.output.toPlainText().strip())
        self.output.setVisible(expanded and has_content)
        self.toggle.setText("⌃" if self.output.isVisible() else "⌄")
        self.toggle.setToolTip("收起阶段输出" if self.output.isVisible() else "展开阶段输出")

    def reset(self):
        self._lines = []
        self.output.clear()
        self.output.hide()
        self.toggle.setText("⌄")
        self.set_status("pending")

    def set_status(self, status: str):
        self._status = status
        style = STATUS_STYLE.get(status, STATUS_STYLE["pending"])
        self.indicator.set_status(status, style)
        self.status_label.setText(style["text"])
        self.status_label.setStyleSheet(f"color:{style['color']};")
        if status in {"running", "failed", "blocked", "rejected", "fallback"}:
            self.set_expanded(True)

    def set_output(self, text: str, expand: bool | None = None):
        clean = (text or "").strip()
        self._lines = [clean] if clean else []
        self.output.setPlainText(clean)
        if expand is not None:
            self.set_expanded(expand)

    def append_output(self, text: str):
        clean = (text or "").strip()
        if not clean or clean in self._lines:
            return
        self._lines.append(clean)
        self.output.setPlainText("\n\n".join(self._lines[-20:]))
        if self._status in {"running", "failed", "blocked", "rejected", "fallback"}:
            self.set_expanded(True)


class ExecutionDisclosure(QFrame):
    """Secondary run information, kept inline and collapsed by default."""

    def __init__(self, key: str, title: str, empty_text: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.empty_text = empty_text
        self._line_count = 0
        self.setObjectName("executionDisclosure")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(5)
        self.button = QToolButton()
        self.button.setObjectName("detailButton")
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.ArrowType.RightArrow)
        self.button.setText(title)
        self.button.setToolTip(f"查看{title}")
        self.button.clicked.connect(self.toggle)
        layout.addWidget(self.button)

        self.output = QPlainTextEdit()
        self.output.setObjectName("detailOutput")
        self.output.setReadOnly(True)
        self.output.setFont(QFont(["SF Mono", "Menlo", "Consolas"], 10))
        self.output.setMinimumHeight(100)
        self.output.setMaximumHeight(260)
        self.output.hide()
        layout.addWidget(self.output)

    def toggle(self):
        self.set_expanded(not self.output.isVisible())

    def set_expanded(self, expanded: bool):
        self.output.setVisible(expanded)
        self.button.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    def clear(self):
        self.output.clear()
        self._line_count = 0
        self.set_expanded(False)

    def set_text(self, text: str):
        clean = (text or "").strip()
        self.output.setPlainText(clean or self.empty_text)
        self._line_count = len(clean.splitlines()) if clean else 0

    def append(self, text: str):
        clean = (text or "").strip()
        if not clean:
            return
        self.output.appendPlainText(clean)
        self._line_count += len(clean.splitlines())
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.output.setTextCursor(cursor)


class TaskPanel(QWidget):
    """A requirement shown as a conversation with an inline execution trace."""

    followup_requested = pyqtSignal(dict)

    STAGES = (
        ("user", "已接收需求", "用户输入"),
        ("governor", "裁决者", "目标、风险与边界"),
        ("planner", "策划者", "步骤与验收条件"),
        ("worker", "执行者", "文件修改与验证"),
        ("reviewer", "审核者", "结果检查"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_job: dict | None = None
        self._tasks: list[dict] = []
        self._worker_outputs: list[str] = []
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame()
        header.setObjectName("conversationHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(22, 10, 18, 10)
        names = QVBoxLayout()
        names.setSpacing(1)
        self.workflow_title = QLabel("新需求")
        self.workflow_title.setObjectName("conversationTitle")
        self.job_meta_label = QLabel("选择项目后即可开始")
        self.job_meta_label.setObjectName("mutedLabel")
        names.addWidget(self.workflow_title)
        names.addWidget(self.job_meta_label)
        header_layout.addLayout(names, 1)
        self.job_status_indicator = StatusIndicator()
        self.job_status_indicator.clear()
        header_layout.addWidget(self.job_status_indicator)
        self.job_status_label = QLabel("")
        self.job_status_label.setObjectName("jobStatus")
        header_layout.addWidget(self.job_status_label)
        self.followup_btn = QPushButton("继续此需求")
        self.followup_btn.setObjectName("quietButton")
        self.followup_btn.setToolTip("基于这次结果提出下一条需求")
        self.followup_btn.setEnabled(False)
        self.followup_btn.clicked.connect(self._request_followup)
        header_layout.addWidget(self.followup_btn)
        root.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setObjectName("conversationScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("conversationContent")
        self.feed_layout = QVBoxLayout(content)
        self.feed_layout.setContentsMargins(42, 34, 42, 38)
        self.feed_layout.setSpacing(24)

        self.empty_state = QFrame()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(40, 100, 40, 40)
        empty_layout.addStretch()
        empty_title = QLabel("交给 RockCore 来完成")
        empty_title.setObjectName("emptyTitle")
        empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_subtitle = QLabel("选择一个项目，然后在下方描述你要修改或构建的内容。")
        self.empty_subtitle.setObjectName("emptySubtitle")
        self.empty_subtitle.setWordWrap(True)
        self.empty_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(self.empty_subtitle)
        empty_layout.addStretch(2)
        self.feed_layout.addWidget(self.empty_state, 1)

        self.user_frame = QFrame()
        self.user_frame.setObjectName("userMessage")
        self.user_frame.setMaximumWidth(760)
        user_layout = QVBoxLayout(self.user_frame)
        user_layout.setContentsMargins(14, 11, 14, 11)
        user_label = QLabel("你")
        user_label.setObjectName("messageAuthor")
        self.user_output = QLabel("")
        self.user_output.setObjectName("userMessageText")
        self.user_output.setWordWrap(True)
        self.user_source_label = QLabel("")
        self.user_source_label.setObjectName("mutedLabel")
        user_layout.addWidget(user_label)
        user_layout.addWidget(self.user_output)
        user_layout.addWidget(self.user_source_label)
        user_row = QHBoxLayout()
        user_row.addStretch(1)
        user_row.addWidget(self.user_frame, 4)
        self.user_row_widget = QWidget()
        self.user_row_widget.setLayout(user_row)
        self.user_row_widget.hide()
        self.feed_layout.addWidget(self.user_row_widget)

        self.agent_frame = QFrame()
        self.agent_frame.setObjectName("agentMessage")
        agent_layout = QVBoxLayout(self.agent_frame)
        agent_layout.setContentsMargins(0, 0, 0, 0)
        agent_layout.setSpacing(9)
        author_row = QHBoxLayout()
        avatar = QLabel("R")
        avatar.setObjectName("assistantAvatar")
        avatar.setFixedSize(26, 26)
        avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        author_row.addWidget(avatar)
        author = QLabel("RockCore")
        author.setObjectName("messageAuthor")
        author_row.addWidget(author)
        author_row.addStretch()
        agent_layout.addLayout(author_row)

        self.agent_summary = QLabel("正在处理你的需求…")
        self.agent_summary.setObjectName("assistantSummary")
        self.agent_summary.setWordWrap(True)
        agent_layout.addWidget(self.agent_summary)

        trace_label = QLabel("执行过程")
        trace_label.setObjectName("traceLabel")
        agent_layout.addWidget(trace_label)
        self.stages: dict[str, WorkflowStage] = {}
        for key, title, subtitle in self.STAGES:
            stage = WorkflowStage(key, title, subtitle)
            self.stages[key] = stage
            agent_layout.addWidget(stage)

        details_label = QLabel("结果详情")
        details_label.setObjectName("traceLabel")
        agent_layout.addWidget(details_label)
        self.run_details = ExecutionDisclosure("activity", "运行记录", "暂无运行记录")
        self.diff_details = ExecutionDisclosure("changes", "代码变更", "暂无代码变更")
        self.test_details = ExecutionDisclosure("tests", "验收结果", "暂无验收结果")
        agent_layout.addWidget(self.run_details)
        agent_layout.addWidget(self.diff_details)
        agent_layout.addWidget(self.test_details)
        self.agent_frame.hide()
        self.feed_layout.addWidget(self.agent_frame)
        self.feed_layout.addStretch()
        self.scroll.setWidget(content)
        root.addWidget(self.scroll, 1)

    def set_project_context(self, name: str = "", root_path: str = ""):
        if name and not self._current_job:
            self.workflow_title.setText("新需求")
            self.job_meta_label.setText(f"{name}  ·  {root_path}")
            self.empty_subtitle.setText("在下方描述你希望 RockCore 完成的工作。")
        elif not name:
            self.workflow_title.setText("欢迎使用 RockCore")
            self.job_meta_label.setText("尚未选择项目")
            self.empty_subtitle.setText("先从左侧添加或选择一个本地项目。")

    def begin_new_request(self, project_name: str = "", root_path: str = ""):
        self.clear_workflow()
        self.set_project_context(project_name, root_path)

    def set_workflow(self, job: dict, constitution: dict | None = None,
                     plan: dict | None = None, tasks: list[dict] | None = None,
                     reviews: list[dict] | None = None):
        same_job = bool(self._current_job and self._current_job.get("job_id") == job.get("job_id"))
        worker_outputs = list(self._worker_outputs) if same_job else []
        if not same_job:
            for disclosure in (self.run_details, self.diff_details, self.test_details):
                disclosure.clear()
        self._current_job = job
        self._tasks = tasks or []
        self._worker_outputs = worker_outputs
        self.empty_state.hide()
        self.user_row_widget.show()
        self.agent_frame.show()
        for stage in self.stages.values():
            stage.reset()

        status = job.get("status", "created")
        source = job.get("source_job_id")
        request = job.get("user_request", "")
        self.workflow_title.setText(request.replace("\n", " ")[:72] or "未命名需求")
        meta = f"{job.get('job_id', '')}  ·  {self._format_time(job.get('created_at', ''))}"
        if source:
            meta += f"  ·  承接 {source}"
        self.job_meta_label.setText(meta)
        self._set_header_status(status)
        self.followup_btn.setEnabled(status in {"done", "failed", "cancelled"})
        self.user_output.setText(request)
        self.user_source_label.setText(f"继续自 {source}" if source else "")
        self.user_source_label.setVisible(bool(source))
        self.stages["user"].set_status("success")
        self.stages["user"].set_output("需求已进入工作流", expand=False)

        fast_path = bool(
            constitution
            and not constitution.get("requires_final_review", True)
            and plan
            and not plan.get("raw_output")
        )
        if constitution and not fast_path:
            lines = [f"目标：{constitution.get('goal', '')}"]
            constraints = constitution.get("constraints") or []
            criteria = constitution.get("acceptance_criteria") or []
            if constraints:
                lines.append("约束：\n" + "\n".join(f"- {value}" for value in constraints))
            if criteria:
                lines.append("验收标准：\n" + "\n".join(f"- {value}" for value in criteria))
            lines.append(f"风险：{constitution.get('risk', '未知')}")
            governor_fallback = (constitution.get("raw_output") or {}).get("fallback")
            self.stages["governor"].set_status(
                "fallback" if governor_fallback else "success"
            )
            if governor_fallback:
                lines.append("注意：模型调用失败，本阶段使用了默认约束。")
            self.stages["governor"].set_output("\n\n".join(lines))
        elif status == "governing":
            self.stages["governor"].set_status("running")

        if plan and not fast_path:
            lines = [plan.get("summary", "已生成执行计划")]
            plan_tasks = (plan.get("raw_output") or {}).get("tasks", [])
            if plan_tasks:
                lines.append("\n".join(
                    f"{task.get('id', '?')} · {task.get('title', '')}" for task in plan_tasks
                ))
            self.stages["planner"].set_status("success")
            self.stages["planner"].set_output("\n\n".join(lines))
        elif status == "planning":
            self.stages["planner"].set_status("running")

        self._refresh_worker_stage()
        self._populate_test_details()

        if reviews:
            review = reviews[0]
            result = review.get("result", "pending")
            review_stage_status = (
                "success" if result == "pass" else
                "failed" if result == "error" else "rejected"
            )
            self.stages["reviewer"].set_status(review_stage_status)
            lines = [review.get("summary", "") or ("审核通过" if result == "pass" else "审核未通过")]
            issues = review.get("issues") or []
            if issues:
                lines.append("问题：\n" + "\n".join(f"- {self._issue_text(issue)}" for issue in issues))
            self.stages["reviewer"].set_output("\n\n".join(lines))
        elif status == "reviewing":
            self.stages["reviewer"].set_status("running")

        if fast_path or status in {"done", "failed", "cancelled"}:
            if fast_path or not constitution:
                self.stages["governor"].set_status("skipped")
                self.stages["governor"].set_output("简单任务使用快速流程，已跳过独立裁决。")
            if fast_path or not plan:
                self.stages["planner"].set_status("skipped")
                self.stages["planner"].set_output("简单任务使用单步执行，已跳过独立策划。")
            if not reviews:
                self.stages["reviewer"].set_status("skipped")
                self.stages["reviewer"].set_output("本次未启用独立审核阶段。")

        if status == "done":
            self.agent_summary.setText("需求已完成。你可以查看执行过程、代码变更和验收结果，或继续提出修改。")
        elif status == "failed":
            self.agent_summary.setText("本次执行未完成。失败原因保留在对应步骤中，可以直接继续提出修复要求。")
        elif status == "cancelled":
            self.agent_summary.setText("执行已停止。当前结果仍然保留，可以从这里继续。")
        else:
            self.agent_summary.setText("正在处理你的需求，步骤状态会在执行过程中实时更新。")
        if same_job:
            QTimer.singleShot(0, self._scroll_to_bottom)
        else:
            QTimer.singleShot(0, self._scroll_to_top)

    def clear_workflow(self):
        self._current_job = None
        self._tasks = []
        self._worker_outputs = []
        self.workflow_title.setText("新需求")
        self.job_meta_label.setText("选择项目后即可开始")
        self.job_status_indicator.clear()
        self.job_status_label.clear()
        self.followup_btn.setEnabled(False)
        self.user_row_widget.hide()
        self.agent_frame.hide()
        self.empty_state.show()
        for stage in self.stages.values():
            stage.reset()
        for disclosure in (self.run_details, self.diff_details, self.test_details):
            disclosure.clear()

    def update_stage(self, key: str, status: str, summary: str = "",
                     details: dict | None = None):
        stage = self.stages.get(key)
        if not stage:
            return
        stage.set_status(status)
        if summary:
            stage.append_output(summary)
        if details:
            stage.append_output(self._format_details(details))
        self.agent_summary.setText(summary or "工作流正在继续执行。")
        QTimer.singleShot(0, self._scroll_to_bottom)

    def append_stage_output(self, key: str, text: str):
        stage = self.stages.get(key)
        if stage:
            stage.append_output(text)

    def add_model_output(self, agent_type: str, provider: str, response: str,
                         error: str | None, duration_ms: int,
                         input_tokens: int = 0, output_tokens: int = 0):
        key = "worker" if agent_type.startswith("worker") else agent_type
        stage = self.stages.get(key)
        if not stage:
            return
        duration = f"{duration_ms / 1000:.1f}s" if duration_ms >= 1000 else f"{duration_ms}ms"
        if error:
            stage.set_status("failed")
            text = f"{provider.upper()} · {duration}\n错误：{error[:600]}"
        else:
            content = self._normalize_model_output(response)
            if not content:
                return
            text = f"{provider.upper()} · {duration} · {input_tokens}+{output_tokens} tokens\n{content}"
        if key == "worker":
            self._worker_outputs.append(text)
            self._refresh_worker_stage()
        else:
            stage.append_output(text)

    def set_tasks(self, tasks: list[dict]):
        self._tasks = tasks
        self._refresh_worker_stage()
        self._populate_test_details()

    def update_task_status(self, task_id: str, status: str):
        for task in self._tasks:
            if task.get("task_id") == task_id:
                task["status"] = status
                break
        self._refresh_worker_stage()

    def has_task(self, task_id: str) -> bool:
        return any(task.get("task_id") == task_id for task in self._tasks)

    def update_job_status(self, job_id: str, status: str):
        if not self._current_job or self._current_job.get("job_id") != job_id:
            return
        self._current_job["status"] = status
        self._set_header_status(status)
        self.followup_btn.setEnabled(status in {"done", "failed", "cancelled"})

    def log(self, message: str, tab: str = "log"):
        if tab.lower() == "test":
            self.test_details.append(message)
        else:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.run_details.append(f"[{timestamp}] {message}")

    def log_event(self, event_type: str, **data):
        timestamp = datetime.now().strftime("%H:%M:%S")
        useful = []
        for key in (
            "task_id", "title", "status", "summary", "error", "command",
            "max_turns", "exploration_limit", "budget_reason",
        ):
            value = data.get(key)
            if value not in (None, ""):
                useful.append(f"{key}={str(value)[:180]}")
        suffix = "  ".join(useful)
        self.run_details.append(f"[{timestamp}] {event_type}{'  ' + suffix if suffix else ''}")

    def log_test_result(self, task_id: str, status: str, output: str):
        self.test_details.append(
            f"{task_id or '任务'} · {STATUS_STYLE.get(status, {}).get('text', status)}\n"
            f"{(output or '').strip()[:3000]}"
        )

    def set_diff(self, diff_text: str):
        self.diff_details.set_text(diff_text or "(无更改)")

    def expand_detail(self, name: str):
        mapping = {
            "运行记录": self.run_details,
            "代码变更": self.diff_details,
            "验收结果": self.test_details,
        }
        disclosure = mapping.get(name)
        if disclosure:
            disclosure.set_expanded(True)
            QTimer.singleShot(0, self._scroll_to_bottom)

    def _request_followup(self):
        if self._current_job:
            self.followup_requested.emit(self._current_job)

    def _set_header_status(self, status: str):
        style = STATUS_STYLE.get(status, STATUS_STYLE["created"])
        self.job_status_indicator.set_status(status, style)
        self.job_status_label.setText(style["text"])
        self.job_status_label.setStyleSheet(f"color:{style['color']};")

    def _refresh_worker_stage(self):
        stage = self.stages["worker"]
        if not self._tasks:
            stage.set_status("pending")
            if self._worker_outputs:
                stage.set_output("\n\n".join(self._worker_outputs[-10:]), expand=True)
            return
        statuses = [task.get("status", "pending") for task in self._tasks]
        if any(status == "failed" for status in statuses):
            overall = "failed"
        elif any(status in {"running", "executing"} for status in statuses):
            overall = "running"
        elif all(status == "done" for status in statuses):
            overall = "success"
        elif any(status == "cancelled" for status in statuses):
            overall = "cancelled"
        elif any(status == "blocked" for status in statuses):
            overall = "blocked"
        else:
            overall = "pending"
        stage.set_status(overall)
        lines = []
        for task in self._tasks:
            style = STATUS_STYLE.get(task.get("status", "pending"), STATUS_STYLE["pending"])
            lines.append(f"{style['icon']} {task.get('task_id', '?')} · {task.get('title', '')} · {style['text']}")
            description = task.get("description", "").strip()
            if description and description != task.get("title", ""):
                lines.append(f"    {description[:300]}")
            paths = task.get("allowed_paths") or []
            if paths and paths != ["*"]:
                lines.append(f"    文件：{', '.join(paths[:8])}")
            for result in (task.get("test_results") or [])[:3]:
                result_style = STATUS_STYLE.get(result.get("status", "pending"), STATUS_STYLE["pending"])
                lines.append(
                    f"    {result_style['icon']} 验收：{result.get('command', '') or '本地检查'} · "
                    f"{result_style['text']}"
                )
        if self._worker_outputs:
            lines.append("\n模型输出：")
            lines.extend(self._worker_outputs[-10:])
        stage.set_output("\n".join(lines), expand=overall in {"running", "failed"})

    def _populate_test_details(self):
        lines = []
        for task in self._tasks:
            for result in task.get("test_results") or []:
                style = STATUS_STYLE.get(result.get("status", "pending"), STATUS_STYLE["pending"])
                lines.append(
                    f"{style['icon']} {task.get('task_id', '?')} · "
                    f"{result.get('command', '') or '本地检查'} · {style['text']}"
                )
                output = (result.get("output") or "").strip()
                if output:
                    lines.append(output[:2000])
        self.test_details.set_text("\n\n".join(lines))

    def _scroll_to_bottom(self):
        bar = self.scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _scroll_to_top(self):
        self.scroll.verticalScrollBar().setValue(0)

    @staticmethod
    def _format_time(value: str) -> str:
        return value.replace("T", " ")[:16] if value else "时间未知"

    @staticmethod
    def _issue_text(issue) -> str:
        if isinstance(issue, dict):
            return str(issue.get("problem") or issue.get("message") or issue)
        return str(issue)

    @staticmethod
    def _format_details(details: dict) -> str:
        if "tasks" in details:
            return "执行步骤：\n" + "\n".join(
                f"- {item.get('id', '?')} · {item.get('title', '')}"
                for item in details.get("tasks", [])
            )
        if "issues" in details:
            return "问题：\n" + "\n".join(
                f"- {TaskPanel._issue_text(item)}" for item in details.get("issues", [])
            )
        return json.dumps(details, ensure_ascii=False, indent=2, default=str)[:2000]

    @staticmethod
    def _normalize_model_output(response: str) -> str:
        text = (response or "").strip()
        if not text:
            return ""
        try:
            data = json.loads(text)
            return json.dumps(data, ensure_ascii=False, indent=2)[:3000]
        except (json.JSONDecodeError, TypeError):
            return text[:3000]
