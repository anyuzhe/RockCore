"""Codex-style requirement conversation and inline execution trace."""

import json
import re
from datetime import datetime

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QTextCursor, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from app.ui.status_constants import ACTIVE_STATUSES, STATUS_STYLE
from app.ui.time_utils import format_local_timestamp, to_local_datetime


CONVERSATION_STATUS_TEXT = {
    "done": "已完成", "failed": "未完成", "cancelled": "已停止",
    "interrupted": "待继续", "needs_attention": "需要处理",
    "rolled_back": "已回退", "created": "等待中", "governing": "分析中",
    "planning": "规划中", "executing": "执行中", "reviewing": "验证中",
}

TERMINAL_JOB_STATUSES = {
    "done", "failed", "cancelled", "interrupted", "needs_attention",
    "rolled_back",
}


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

    def add_content_widget(self, widget: QWidget) -> None:
        """Place live phase-specific content inside this workflow stage."""
        # Keep the stage's diagnostic text last so the normal activity stream
        # remains directly below the stage header.
        self.layout().insertWidget(1, widget)

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
        if status in {
            "running", "failed", "blocked", "rejected", "fallback",
            "needs_attention", "interrupted",
        }:
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
        if self._status in {
            "running", "failed", "blocked", "rejected", "fallback",
            "needs_attention", "interrupted",
        }:
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


class ExecutionActivityItem(QFrame):
    """One readable Worker action with optional technical details."""

    def __init__(self, activity_id: str, parent=None):
        super().__init__(parent)
        self.activity_id = activity_id
        self.activity_status = "pending"
        self.setObjectName("executionActivityItem")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(4)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.indicator = StatusIndicator()
        row.addWidget(self.indicator)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(1)
        self.summary = QLabel("")
        self.summary.setObjectName("activitySummary")
        self.summary.setWordWrap(True)
        self.meta = QLabel("")
        self.meta.setObjectName("activityMeta")
        self.meta.setWordWrap(True)
        text_layout.addWidget(self.summary)
        text_layout.addWidget(self.meta)
        row.addLayout(text_layout, 1)
        self.toggle = QToolButton()
        self.toggle.setObjectName("activityDisclosureButton")
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolTip("查看技术详情")
        self.toggle.clicked.connect(self.toggle_details)
        self.toggle.hide()
        row.addWidget(self.toggle)
        layout.addLayout(row)

        self.details = QPlainTextEdit()
        self.details.setObjectName("activityDetails")
        self.details.setReadOnly(True)
        self.details.setFont(QFont(["SF Mono", "Menlo", "Consolas"], 9))
        self.details.setMinimumHeight(72)
        self.details.setMaximumHeight(220)
        self.details.hide()
        layout.addWidget(self.details)

    def update_activity(self, *, status: str, summary: str, meta: str = "",
                        details: str = "", variant: str = "action"):
        successful = {
            "success", "completed", "written", "read", "found", "passed",
            "ok", "cached", "promoted", "created", "updated", "clean",
        }
        style_status = {
            **{value: "success" for value in successful},
            "started": "running", "running": "running",
            "error": "failed", "rejected": "failed",
        }.get(status, status or "pending")
        self.activity_status = style_status
        is_narrative = variant == "narrative"
        self.indicator.setVisible(not is_narrative)
        if not is_narrative:
            self.indicator.set_status(
                style_status, STATUS_STYLE.get(style_status, STATUS_STYLE["pending"])
            )
        else:
            self.indicator.clear()
        self.summary.setObjectName(
            "activityNarrative" if is_narrative else "activitySummary"
        )
        self.summary.style().unpolish(self.summary)
        self.summary.style().polish(self.summary)
        self.summary.setText(summary)
        self.meta.setText(meta)
        self.meta.setVisible(bool(meta))
        self.details.setPlainText(details)
        self.toggle.setVisible(bool(details.strip()))
        self.toggle.setVisible(not is_narrative and bool(details.strip()))
        if not details.strip():
            self.details.hide()

    def toggle_details(self):
        visible = not self.details.isVisible()
        self.details.setVisible(visible)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow
        )
        self.toggle.setToolTip("收起技术详情" if visible else "查看技术详情")


class ExecutionActivityTimeline(QWidget):
    """Codex-style live activity feed for Worker actions."""

    MAX_RECENT_COMPLETED = 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("executionActivityTimeline")
        self._items: dict[str, ExecutionActivityItem] = {}
        self._sequence = 0
        self._live = False
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.hide()

    def clear(self):
        self._items = {}
        self._sequence = 0
        while self.layout.count():
            item = self.layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.hide()

    def set_live(self, live: bool):
        """Show activities only while the Worker execution phase is live."""
        self._live = bool(live)
        self._refresh_visible_items()

    def _refresh_visible_items(self):
        """Keep four recent results plus the single current action visible."""
        ordered = list(self._items.items())
        running_ids = [
            activity_id for activity_id, item in ordered
            if item.activity_status == "running"
        ]
        current_id = running_ids[-1] if running_ids else ""
        completed_ids = [
            activity_id for activity_id, item in ordered
            if activity_id != current_id and item.activity_status != "running"
        ][-self.MAX_RECENT_COMPLETED:]
        visible_ids = set(completed_ids)
        if current_id:
            visible_ids.add(current_id)
        for activity_id, item in ordered:
            item.setVisible(self._live and activity_id in visible_ids)
        self.setVisible(bool(self._live and visible_ids))

    def add_or_update(self, activity_id: str = "", **activity):
        if not activity_id:
            self._sequence += 1
            activity_id = f"activity-{self._sequence}"
        item = self._items.get(activity_id)
        if item is None:
            item = ExecutionActivityItem(activity_id)
            self._items[activity_id] = item
            self.layout.addWidget(item)
        item.update_activity(**activity)
        self._refresh_visible_items()
        return item

    def item(self, activity_id: str) -> ExecutionActivityItem | None:
        return self._items.get(activity_id)

    @property
    def has_items(self) -> bool:
        return bool(self._items)


class TaskPanel(QWidget):
    """A requirement shown as a conversation with an inline execution trace."""

    followup_requested = pyqtSignal(dict)
    attention_resume_requested = pyqtSignal(dict)
    rollback_requested = pyqtSignal(dict)
    report_requested = pyqtSignal(dict)

    STAGES = (
        ("user", "理解需求", "目标与当前轮输入"),
        ("governor", "安全与范围", "主控理解与确定性预检"),
        ("planner", "执行计划", "动态步骤与验收条件"),
        ("worker", "工作过程", "读取、修改与即时验证"),
        ("reviewer", "验证结果", "确定性检查与按需审核"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_job: dict | None = None
        self._tasks: list[dict] = []
        self._worker_outputs: list[str] = []
        self._repair_rounds: list[dict] = []
        self._reviews: list[dict] = []
        self._active_repair_round = 0
        self._task_progress: dict[str, dict] = {}
        self._activity_counters: dict[str, int] = {}
        self._pending_tool_activities: dict[tuple[str, str, int], str] = {}
        # Exploratory reads are one user-visible step. Keep individual calls
        # as expandable evidence instead of flooding the conversation feed.
        self._read_activity_groups: dict[str, dict] = {}
        self._verification_activity_groups: dict[str, dict] = {}
        self._worker_narrative_sequence: dict[str, int] = {}
        self._task_timings: dict[str, dict] = {}
        self._progress_sequence = 0
        self._usage = self._empty_usage()
        self._task_timer = QTimer(self)
        self._task_timer.setInterval(1000)
        self._task_timer.timeout.connect(self._refresh_task_times)
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
        self.usage_label = QLabel("")
        self.usage_label.setObjectName("mutedLabel")
        self.usage_label.setToolTip(
            "等价估算用于比较模型用量；可计费 API 估算才参与人民币预算。"
            "ChatGPT 登录调用不计入可计费 API 成本。"
        )
        header_layout.addWidget(self.usage_label)
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
        self.rollback_btn = QPushButton("回退此需求")
        self.rollback_btn.setObjectName("quietButton")
        self.rollback_btn.setToolTip(
            "安全撤销这次需求产生的代码变更，保留执行记录和后续需求"
        )
        self.rollback_btn.setVisible(False)
        self.rollback_btn.clicked.connect(self._request_rollback)
        header_layout.addWidget(self.rollback_btn)
        self.report_btn = QPushButton("查看报告")
        self.report_btn.setObjectName("quietButton")
        self.report_btn.setToolTip("打开包含完整执行过程的 PDF 任务报告")
        self.report_btn.setVisible(False)
        self.report_btn.clicked.connect(self._request_report)
        header_layout.addWidget(self.report_btn)
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

        self.conversation_history = QFrame()
        self.conversation_history.setObjectName("conversationHistory")
        history_layout = QVBoxLayout(self.conversation_history)
        history_layout.setContentsMargins(0, 0, 0, 6)
        history_layout.setSpacing(6)
        history_title = QLabel("此前对话")
        history_title.setObjectName("traceLabel")
        self.conversation_history_text = QLabel("")
        self.conversation_history_text.setObjectName("assistantSummary")
        self.conversation_history_text.setWordWrap(True)
        self.conversation_history_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        history_layout.addWidget(history_title)
        history_layout.addWidget(self.conversation_history_text)
        self.conversation_history.hide()
        self.feed_layout.addWidget(self.conversation_history)

        self.user_frame = QFrame()
        self.user_frame.setObjectName("userMessage")
        self.user_frame.setMaximumWidth(760)
        self.user_frame.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.user_frame.customContextMenuRequested.connect(
            lambda position: self._show_user_request_menu(
                self.user_frame, position
            )
        )
        user_layout = QVBoxLayout(self.user_frame)
        user_layout.setContentsMargins(14, 11, 14, 11)
        user_label = QLabel("你")
        user_label.setObjectName("messageAuthor")
        self.user_output = QLabel("")
        self.user_output.setObjectName("userMessageText")
        self.user_output.setWordWrap(True)
        self.user_output.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.user_output.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.user_output.customContextMenuRequested.connect(
            lambda position: self._show_user_request_menu(
                self.user_output, position
            )
        )
        self.user_source_label = QLabel("")
        self.user_source_label.setObjectName("mutedLabel")
        self.user_attachments_widget = QWidget()
        self.user_attachments_layout = QHBoxLayout(self.user_attachments_widget)
        self.user_attachments_layout.setContentsMargins(0, 3, 0, 1)
        self.user_attachments_layout.setSpacing(6)
        self.user_attachments_widget.hide()
        user_layout.addWidget(user_label)
        user_layout.addWidget(self.user_output)
        user_layout.addWidget(self.user_attachments_widget)
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

        self.worker_activity = ExecutionActivityTimeline()

        self.attention_card = QFrame()
        self.attention_card.setObjectName("attentionCard")
        attention_layout = QVBoxLayout(self.attention_card)
        attention_layout.setContentsMargins(14, 12, 14, 12)
        attention_layout.setSpacing(7)
        attention_title = QLabel("需要你完成以下操作")
        attention_title.setObjectName("attentionTitle")
        self.attention_reason = QLabel("")
        self.attention_reason.setObjectName("attentionReason")
        self.attention_reason.setWordWrap(True)
        self.attention_hint = QLabel("")
        self.attention_hint.setObjectName("attentionHint")
        self.attention_hint.setWordWrap(True)
        attention_actions = QHBoxLayout()
        attention_actions.addStretch(1)
        self.attention_resume_btn = QPushButton("已处理，继续完成任务")
        self.attention_resume_btn.setObjectName("attentionResumeButton")
        self.attention_resume_btn.setToolTip(
            "沿用当前 Job 和检查点，从中断步骤继续，不会创建新需求"
        )
        self.attention_resume_btn.clicked.connect(
            self._request_attention_resume
        )
        attention_actions.addWidget(self.attention_resume_btn)
        attention_layout.addWidget(attention_title)
        attention_layout.addWidget(self.attention_reason)
        attention_layout.addWidget(self.attention_hint)
        attention_layout.addLayout(attention_actions)
        self.attention_card.hide()
        agent_layout.addWidget(self.attention_card)

        trace_label = QLabel("执行过程")
        trace_label.setObjectName("traceLabel")
        agent_layout.addWidget(trace_label)
        self.trace_layout = QVBoxLayout()
        self.trace_layout.setContentsMargins(0, 0, 0, 0)
        self.trace_layout.setSpacing(0)
        self.stages: dict[str, WorkflowStage] = {}
        for key, title, subtitle in self.STAGES:
            stage = WorkflowStage(key, title, subtitle)
            self.stages[key] = stage
            self.trace_layout.addWidget(stage)
        self.stages["worker"].add_content_widget(self.worker_activity)
        self.repair_stages: dict[str, WorkflowStage] = {}
        agent_layout.addLayout(self.trace_layout)

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

        self.worker_progress_wrap = QWidget()
        self.worker_progress_wrap.setObjectName("workerProgressWrap")
        progress_layout = QHBoxLayout(self.worker_progress_wrap)
        progress_layout.setContentsMargins(40, 6, 40, 2)
        progress_layout.addStretch(1)
        self.worker_progress_card = QFrame()
        self.worker_progress_card.setObjectName("workerProgressCard")
        self.worker_progress_card.setMaximumWidth(900)
        worker_progress_layout = QHBoxLayout(self.worker_progress_card)
        worker_progress_layout.setContentsMargins(13, 7, 13, 7)
        worker_progress_layout.setSpacing(8)
        self.worker_progress_indicator = StatusIndicator()
        self.worker_progress_indicator.set_status(
            "running", STATUS_STYLE["running"]
        )
        worker_progress_layout.addWidget(self.worker_progress_indicator)
        self.worker_progress_label = QLabel("")
        self.worker_progress_label.setObjectName("workerProgressLabel")
        self.worker_progress_label.setWordWrap(True)
        worker_progress_layout.addWidget(self.worker_progress_label, 1)
        progress_layout.addWidget(self.worker_progress_card, 8)
        progress_layout.addStretch(1)
        self.worker_progress_wrap.hide()
        root.addWidget(self.worker_progress_wrap)

    def _original_request_text(self) -> str:
        """Return the complete submitted request, independent of UI rendering."""
        if self._current_job:
            return str(self._current_job.get("user_request") or "")
        return self.user_output.text()

    def _copy_original_request(self) -> None:
        request = self._original_request_text()
        if request:
            QApplication.clipboard().setText(request)

    def _show_user_request_menu(self, source: QWidget, position) -> None:
        """Show copy actions for the user's submitted request bubble."""
        request = self._original_request_text()
        selected_text = (
            self.user_output.selectedText()
            if self.user_output.hasSelectedText() else ""
        )
        menu = QMenu(self.user_frame)
        copy_selected = menu.addAction("复制选中内容")
        copy_selected.setEnabled(bool(selected_text))
        menu.addSeparator()
        copy_original = menu.addAction("复制原始需求")
        copy_original.setEnabled(bool(request))
        chosen = menu.exec(source.mapToGlobal(position))
        if chosen is copy_selected:
            QApplication.clipboard().setText(selected_text)
        elif chosen is copy_original:
            self._copy_original_request()

    def set_project_context(self, name: str = "", root_path: str = ""):
        self.workflow_title.show()
        if name and not self._current_job:
            self.workflow_title.setText("新需求")
            self.job_meta_label.setText(f"{name}  ·  {root_path}")
            self.empty_subtitle.setText("在下方描述你希望 RockCore 完成的工作。")
        elif not name:
            self.workflow_title.setText("欢迎使用 RockCore")
            self.job_meta_label.setText("尚未选择项目")
            self.empty_subtitle.setText("先从左侧添加或选择一个本地项目。")

    @staticmethod
    def _empty_usage() -> dict:
        return {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "calls": 0,
            "cost": 0.0,
            "billable_cost": 0.0,
            "budget": {},
        }

    @staticmethod
    def _format_usage(usage: dict, prefix: str = "用量") -> str:
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        cached_input_tokens = min(
            input_tokens,
            int(usage.get("cached_input_tokens", 0) or 0),
        )
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        calls = int(usage.get("calls", 0) or 0)
        cost = float(usage.get("cost", 0.0) or 0.0)
        raw_billable_cost = usage.get("billable_cost")
        budget = usage.get("budget") or {}
        if not calls and not input_tokens and not output_tokens and not budget:
            return ""
        if raw_billable_cost is None:
            cost_text = (
                f"等价估算 ¥{cost:.4f} · 可计费 API：历史记录未区分"
            )
        else:
            billable_cost = float(raw_billable_cost or 0.0)
            cost_text = (
                f"等价估算 ¥{cost:.4f} · 可计费 API ¥{billable_cost:.4f}"
            )
        cache_text = (
            f"（其中缓存 {cached_input_tokens:,}）"
            if cached_input_tokens else ""
        )
        usage_text = (
            f"{prefix}：输入 {input_tokens:,}{cache_text} · "
            f"输出 {output_tokens:,} tokens"
            f" · {cost_text} · {calls} 次调用"
        )
        if not budget:
            return usage_text
        used = int(budget.get("used_tokens", 0) or 0)
        reserved = int(budget.get("reserved_tokens", 0) or 0)
        remaining = int(budget.get("remaining_tokens", 0) or 0)
        auto_limit = int(budget.get("max_auto_tokens", 0) or 0)
        hard_cost = float(budget.get("hard_cost_limit_cny", 0.0) or 0.0)
        live_billable = float(budget.get("billable_cost", 0.0) or 0.0)
        budget_text = (
            f"预算：有效已用 {used:,} · 已预留 {reserved:,} · "
            f"剩余 {remaining:,} · 最高自动扩容 {auto_limit:,} tokens · "
            f"人民币硬上限 ¥{live_billable:.4f}/¥{hard_cost:.2f}"
        )
        return usage_text + "\n" + budget_text

    def _set_usage(self, usage: dict | None):
        self._usage = {**self._empty_usage(), **(usage or {})}
        self.usage_label.setText(self._format_usage(self._usage, "总用量"))

    def begin_new_request(self, project_name: str = "", root_path: str = ""):
        self.clear_workflow()
        self.set_project_context(project_name, root_path)

    @staticmethod
    def _repair_round_from_task_id(task_id: str) -> int:
        match = re.match(r"^R(\d+)T", task_id or "")
        return int(match.group(1)) if match else 0

    def _clear_repair_stages(self):
        for stage in self.repair_stages.values():
            self.trace_layout.removeWidget(stage)
            stage.setParent(None)
            stage.deleteLater()
        self.repair_stages.clear()

    def _ensure_repair_stage(self, round_number: int,
                             agent_type: str) -> WorkflowStage:
        key = f"repair_{round_number}_{agent_type}"
        stage = self.repair_stages.get(key)
        if stage:
            return stage
        metadata = {
            "planner": ("策划者", f"第 {round_number} 轮 · 判断与修复计划"),
            "worker": ("执行者", f"第 {round_number} 轮 · 执行修复与验证"),
            "reviewer": ("审核者", f"第 {round_number} 轮 · 修复结果复审"),
        }
        title, subtitle = metadata[agent_type]
        stage = WorkflowStage(key, title, subtitle)
        self.repair_stages[key] = stage
        self.trace_layout.addWidget(stage)
        return stage

    def _stage_for(self, key: str, repair_round: int = 0) -> WorkflowStage | None:
        if repair_round and key in {"planner", "worker", "reviewer"}:
            return self._ensure_repair_stage(repair_round, key)
        return self.stages.get(key)

    @staticmethod
    def _review_status(result: str) -> str:
        if result == "pass":
            return "success"
        if result == "error":
            return "failed"
        return "rejected"

    def _set_review_stage(self, stage: WorkflowStage,
                          review: dict | None,
                          *, fallback: str = "审核未通过"):
        review = review or {}
        result = review.get("result", "pending")
        if result == "pending":
            stage.set_status("running")
        else:
            stage.set_status(self._review_status(result))
        summary = review.get("summary", "") or (
            "审核通过" if result == "pass" else fallback
        )
        lines = [summary]
        issues = review.get("issues") or []
        if issues:
            lines.append(
                "问题：\n" + "\n".join(
                    f"- {self._issue_text(issue)}" for issue in issues
                )
            )
        stage.set_output("\n\n".join(lines))

    def _repair_tasks(self, repair: dict) -> list[dict]:
        plan_tasks = (repair.get("plan") or {}).get("tasks") or []
        actual_by_id = {
            task.get("task_id"): task for task in self._tasks
            if task.get("task_id")
        }
        merged = []
        for planned in plan_tasks:
            task = dict(planned)
            actual = actual_by_id.get(planned.get("id")) or {}
            task.update({key: value for key, value in actual.items() if value is not None})
            task.setdefault("task_id", planned.get("id", "?"))
            task.setdefault("title", planned.get("title", ""))
            merged.append(task)
        return merged

    def _set_repair_worker_stage(self, stage: WorkflowStage,
                                 repair: dict):
        repair_status = repair.get("status", "planned")
        if repair_status == "executing":
            stage_status = "running"
        elif repair_status == "execution_failed":
            stage_status = "failed"
        elif repair_status == "planned":
            stage_status = "pending"
        else:
            stage_status = "success"
        stage.set_status(stage_status)
        lines = []
        for task in self._repair_tasks(repair):
            task_status = task.get("status", "pending")
            style = STATUS_STYLE.get(task_status, STATUS_STYLE["pending"])
            lines.append(
                f"{style['icon']} {task.get('task_id', '?')} · "
                f"{task.get('title', '')} · {style['text']}"
            )
        if repair.get("reason") and repair_status == "execution_failed":
            lines.append(f"失败原因：{repair['reason']}")
        stage.set_output("\n".join(lines) or "等待执行修复任务")

    def _populate_repair_timeline(self, repair_rounds: list[dict],
                                  reviews: list[dict]):
        self._clear_repair_stages()
        self._repair_rounds = list(repair_rounds or [])
        self._reviews = list(reviews or [])
        self._active_repair_round = 0
        chronological_reviews = list(reversed(self._reviews))
        terminal_repair_statuses = {
            "unrepairable", "assessment_failed", "plan_rejected",
            "execution_failed", "review_rejected", "review_error", "passed",
        }

        for repair in self._repair_rounds:
            round_number = int(repair.get("round", 0) or 0)
            if not round_number:
                continue
            repair_status = repair.get("status", "assessed")
            planner = self._ensure_repair_stage(round_number, "planner")
            if repair_status == "assessment_failed":
                planner_status = "failed"
            elif repair_status in {"unrepairable", "plan_rejected"}:
                planner_status = "rejected"
            elif repair_status == "assessed":
                planner_status = "running"
            else:
                planner_status = "success"
            planner.set_status(planner_status)
            planner_lines = [
                f"审核未通过后，进入第 {round_number} 轮修复判断。"
            ]
            if repair.get("reason"):
                planner_lines.append(f"判断说明：{repair['reason']}")
            repair_tasks = (repair.get("plan") or {}).get("tasks") or []
            if repair_tasks:
                planner_lines.append(
                    "修复步骤：\n" + "\n".join(
                        f"- {task.get('id', '?')} · {task.get('title', '')}"
                        for task in repair_tasks
                    )
                )
            planner.set_output("\n\n".join(planner_lines))

            worker_statuses = {
                "planned", "executing", "execution_failed", "executed",
                "review_rejected", "review_error", "passed",
            }
            if repair_status in worker_statuses:
                worker = self._ensure_repair_stage(round_number, "worker")
                self._set_repair_worker_stage(worker, repair)

            post_review = (
                chronological_reviews[round_number]
                if len(chronological_reviews) > round_number else None
            )
            review_statuses = {"executed", "review_rejected", "review_error", "passed"}
            if post_review or repair_status in review_statuses:
                reviewer = self._ensure_repair_stage(round_number, "reviewer")
                if post_review:
                    self._set_review_stage(reviewer, post_review)
                elif repair_status == "review_error":
                    reviewer.set_status("failed")
                    reviewer.set_output(repair.get("reason", "修复后审核无法完成"))
                elif repair_status == "review_rejected":
                    reviewer.set_status("rejected")
                    reviewer.set_output(repair.get("reason", "修复后审核仍未通过"))
                elif repair_status == "passed":
                    reviewer.set_status("success")
                    reviewer.set_output(
                        repair.get("final_review_summary", "修复后审核通过")
                    )
                else:
                    reviewer.set_status("running")
                    reviewer.set_output("修复已执行，正在再次审核")

            if repair_status not in terminal_repair_statuses:
                self._active_repair_round = round_number

    def _set_user_attachments(self, attachments: list[dict]):
        while self.user_attachments_layout.count():
            item = self.user_attachments_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for attachment in attachments[:8]:
            preview = QLabel()
            preview.setObjectName("submittedImagePreview")
            preview.setFixedSize(92, 68)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            preview.setToolTip(str(attachment.get("name") or "图片"))
            pixmap = QPixmap(str(attachment.get("path", "")))
            if not pixmap.isNull():
                preview.setPixmap(pixmap.scaled(
                    92, 68,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            else:
                preview.setText(str(attachment.get("name") or "图片")[:12])
            self.user_attachments_layout.addWidget(preview)
        self.user_attachments_layout.addStretch(1)
        self.user_attachments_widget.setVisible(bool(attachments))

    def set_workflow(self, job: dict, constitution: dict | None = None,
                     plan: dict | None = None, tasks: list[dict] | None = None,
                     reviews: list[dict] | None = None):
        same_job = bool(self._current_job and self._current_job.get("job_id") == job.get("job_id"))
        worker_outputs = list(self._worker_outputs) if same_job else []
        if not same_job:
            self._task_progress = {}
            self._activity_counters = {}
            self._pending_tool_activities = {}
            self._read_activity_groups = {}
            self._verification_activity_groups = {}
            self._worker_narrative_sequence = {}
            self._task_timings = {}
            self._progress_sequence = 0
            self.worker_activity.clear()
            for disclosure in (self.run_details, self.diff_details, self.test_details):
                disclosure.clear()
        self._current_job = job
        self._tasks = tasks or []
        self._reviews = reviews or []
        self._worker_outputs = worker_outputs
        self._set_usage(job.get("usage"))
        self.empty_state.hide()
        self.user_row_widget.show()
        self.agent_frame.show()
        for stage in self.stages.values():
            stage.reset()

        status = job.get("status", "created")
        worker_live = status == "executing"
        self.worker_activity.set_live(worker_live)
        if status in TERMINAL_JOB_STATUSES:
            self.worker_activity.clear()
            self.worker_progress_wrap.hide()
        source = job.get("source_job_id")
        request = job.get("user_request", "")
        # The complete requirement is already shown in the conversation and
        # summarized in the sidebar. Repeating it as a header wastes horizontal
        # space needed by usage and status information.
        self.workflow_title.hide()
        turn_number = max(1, int(job.get("turn_number") or 1))
        turn_total = max(turn_number, int(job.get("turn_total") or turn_number))
        meta = f"第 {turn_number}/{turn_total} 轮  ·  {self._format_time(job.get('created_at', ''))}"
        self.job_meta_label.setText(meta)
        self.job_meta_label.setToolTip(
            f"内部执行记录：{job.get('job_id', '')}\n"
            f"会话：{job.get('execution_session_id', '')}"
        )
        self._set_header_status(status)
        self._set_terminal_actions(status, job)
        self.set_report_state(
            path=str(job.get("report_path") or ""),
            generating=bool(job.get("report_generating")),
            available=status in {
                "done", "failed", "cancelled", "interrupted", "needs_attention",
                "rolled_back",
            },
        )
        self.user_output.setText(request)
        self._set_user_attachments(job.get("attachments") or [])
        self.user_source_label.setText("承接上一轮" if source else "")
        self.user_source_label.setVisible(bool(source))
        understanding_complete = bool(constitution) or status not in {
            "created", "governing",
        }
        self.stages["user"].set_status(
            "success" if understanding_complete else "running"
        )
        understanding = [f"需求：{request.strip() or '已提交当前需求'}"]
        raw_constitution = (constitution or {}).get("raw_output") or {}
        if constitution:
            goal = str(constitution.get("goal") or "").strip()
            if goal and goal != request.strip():
                understanding.append(f"目标：{goal}")
            constraints = constitution.get("constraints") or []
            criteria = constitution.get("acceptance_criteria") or []
            if constraints:
                understanding.append(
                    "约束：\n" + "\n".join(f"- {value}" for value in constraints)
                )
            if criteria:
                understanding.append(
                    "验收标准：\n" + "\n".join(f"- {value}" for value in criteria)
                )
            strategy = str(raw_constitution.get("execution_strategy") or "").strip()
            if strategy:
                understanding.append(
                    "执行策略：" + ("先规划后执行" if strategy == "planned" else "聚焦执行")
                )
            next_action = str(raw_constitution.get("next_action") or "").strip()
            if next_action:
                understanding.append("下一步：" + next_action)
            observations = raw_constitution.get("image_observations") or []
            if observations:
                understanding.append(
                    "附件观察：\n" + "\n".join(f"- {value}" for value in observations)
                )
        if understanding_complete:
            self.stages["user"].set_output(
                "\n\n".join(understanding), expand=False
            )
        else:
            self.stages["user"].set_output(
                "正在结合项目上下文理解目标、约束和验收标准",
                expand=True,
            )

        fast_path = bool(
            constitution
            and not constitution.get("requires_final_review", True)
            and plan
            and not plan.get("raw_output")
        )
        last_repair = None
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
                fallback_error = (constitution.get("raw_output") or {}).get("error", "")
                lines.append("注意：模型调用失败，本阶段使用了默认约束。")
                lines.append(
                    "降级原因：" + self._friendly_provider_error(fallback_error)
                )
            self.stages["governor"].set_output("\n\n".join(lines))
        elif status == "governing":
            self.stages["governor"].set_status("running")

        if plan and not fast_path:
            lines = [plan.get("summary", "已生成执行计划")]
            raw_plan = plan.get("raw_output") or {}
            plan_tasks = raw_plan.get("tasks", [])
            if plan_tasks:
                lines.append("\n".join(
                    f"{task.get('id', '?')} · {task.get('title', '')}" for task in plan_tasks
                ))
            repair_rounds = raw_plan.get("repair_rounds") or []
            if repair_rounds:
                last_repair = repair_rounds[-1]
            self.stages["planner"].set_status("success")
            self.stages["planner"].set_output("\n\n".join(lines))
        elif status == "planning":
            self.stages["planner"].set_status("running")

        self._refresh_worker_stage()
        self._refresh_worker_progress()
        self._populate_test_details()
        if worker_live:
            historical_activities = [
                activity
                for task in self._tasks
                for activity in (task.get("worker_activities") or [])
            ]
            historical_activities.sort(
                key=lambda item: str(item.get("created_at") or "")
            )
            if (
                historical_activities
                and (not same_job or not self.worker_activity.has_items)
            ):
                self.load_worker_activities(historical_activities)
        if not same_job:
            self.restore_task_timings(self._tasks)

        if reviews:
            # Reviews are loaded newest-first; the fixed stage represents the
            # initial review, while repair reviews are appended below it.
            self._set_review_stage(self.stages["reviewer"], reviews[-1])
        elif status == "reviewing":
            self.stages["reviewer"].set_status("running")

        self._populate_repair_timeline(
            (plan.get("raw_output") or {}).get("repair_rounds", []) if plan else [],
            reviews or [],
        )

        if fast_path or status in {
            "done", "failed", "cancelled", "interrupted", "needs_attention",
            "rolled_back",
        }:
            if fast_path or not constitution:
                self.stages["governor"].set_status("skipped")
                self.stages["governor"].set_output("简单任务使用确定性快速流程，已跳过模型主控。")
            if fast_path or not plan:
                self.stages["planner"].set_status("skipped")
                self.stages["planner"].set_output("简单任务使用单步执行，已跳过独立策划。")
            if not reviews:
                self.stages["reviewer"].set_status("skipped")
                self.stages["reviewer"].set_output("本次未启用独立审核阶段。")

        if status == "done":
            self.agent_summary.setText(
                job.get("assistant_summary")
                or "需求已完成。你可以查看执行过程、代码变更和验收结果，或继续提出修改。"
            )
        elif status == "failed":
            if last_repair and last_repair.get("reason"):
                self.agent_summary.setText(
                    f"本次自动修复未完成：{last_repair['reason']}"
                )
            else:
                reason = job.get("failure_reason") or "失败原因保留在对应步骤中"
                hint = job.get("recovery_hint") or "可以直接继续提出修复要求"
                self.agent_summary.setText(f"本次执行未完成：{reason}。{hint}")
        elif status == "interrupted":
            reason = job.get("failure_reason") or "上次运行未正常结束"
            hint = job.get("recovery_hint") or "可以从检查点继续"
            self.agent_summary.setText(f"已保存有效进度：{reason}。{hint}")
        elif status == "needs_attention":
            reason, hint = self._attention_details(job)
            self.agent_summary.setText(f"需要你的处理：{reason}。{hint}")
        elif status == "cancelled":
            self.agent_summary.setText("执行已停止。当前结果仍然保留，可以从这里继续。")
        elif status == "rolled_back":
            self.agent_summary.setText(
                "这次需求产生的代码变更已安全回退；需求记录和执行报告已保留。"
            )
        else:
            self.agent_summary.setText("正在处理你的需求，步骤状态会在执行过程中实时更新。")
        if same_job:
            QTimer.singleShot(0, self._scroll_to_bottom)
        else:
            QTimer.singleShot(0, self._scroll_to_top)

    def set_conversation(self, session: dict, turns: list[dict]):
        """Show prior public turns while the latest turn remains fully live."""
        previous = list(turns or [])[:-1]
        lines = []
        for index, turn in enumerate(previous, 1):
            request = " ".join(str(turn.get("user_request") or "").split())
            summary = " ".join(str(turn.get("summary") or "").split())
            status = CONVERSATION_STATUS_TEXT.get(
                str(turn.get("status") or "created"), "处理中"
            )
            lines.append(f"你 · 第 {index} 轮\n{request}")
            lines.append(
                f"RockCore · {status}\n{summary or '执行记录和结果已保留'}"
            )
        self.conversation_history_text.setText("\n\n".join(lines))
        self.conversation_history.setVisible(bool(lines))
        title = str(session.get("title") or "当前会话")
        self.workflow_title.setText(title)
        self.workflow_title.show()

    def clear_workflow(self):
        self._current_job = None
        self._tasks = []
        self._worker_outputs = []
        self._repair_rounds = []
        self._reviews = []
        self._active_repair_round = 0
        self._task_progress = {}
        self._activity_counters = {}
        self._pending_tool_activities = {}
        self._read_activity_groups = {}
        self._verification_activity_groups = {}
        self._worker_narrative_sequence = {}
        self._task_timings = {}
        self._task_timer.stop()
        self._progress_sequence = 0
        self.worker_activity.clear()
        self.worker_progress_wrap.hide()
        self.conversation_history.hide()
        self._clear_repair_stages()
        self._set_usage(self._empty_usage())
        self.workflow_title.setText("新需求")
        self.workflow_title.show()
        self.job_meta_label.setText("选择项目后即可开始")
        self.job_status_indicator.clear()
        self.job_status_label.clear()
        self.attention_card.hide()
        self.attention_resume_btn.setEnabled(True)
        self.followup_btn.show()
        self.followup_btn.setEnabled(False)
        self.report_btn.hide()
        self.report_btn.setEnabled(False)
        self.user_row_widget.hide()
        self.agent_frame.hide()
        self.empty_state.show()
        for stage in self.stages.values():
            stage.reset()
        self.stages["worker"].subtitle_label.setText(
            "读取分析 / 文件修改与验证"
        )
        for disclosure in (self.run_details, self.diff_details, self.test_details):
            disclosure.clear()

    def update_stage(self, key: str, status: str, summary: str = "",
                     details: dict | None = None,
                     repair_round: int = 0):
        stage = self._stage_for(key, repair_round)
        if not stage:
            return
        if repair_round:
            self._active_repair_round = repair_round
        stage.set_status(status)
        if summary:
            stage.append_output(summary)
        if details:
            stage.append_output(self._format_details(details))
        self.agent_summary.setText(summary or "工作流正在继续执行。")
        QTimer.singleShot(0, self._scroll_to_bottom)

    def append_stage_output(self, key: str, text: str,
                            repair_round: int = 0):
        stage = self._stage_for(key, repair_round)
        if stage:
            stage.append_output(text)

    @staticmethod
    def _tool_kind(tool: str) -> str:
        name = str(tool or "").lower()
        if name in {
            "read_file", "read_pdf", "read_docx", "read_pptx", "read_log",
            "read_temp_file", "list_files", "list_temp_files",
        }:
            return "read"
        if name in {"search_code", "search_in_file"}:
            return "search"
        if name in {
            "write_file", "apply_patch", "insert_before", "insert_after",
            "write_pdf", "promote_artifact",
        }:
            return "write"
        if name in {"run_tests", "run_command", "git_diff"}:
            return "verify"
        return "tool"

    @classmethod
    def _activity_summary(cls, tool: str, path: str = "",
                          status: str = "started") -> str:
        target = str(path or "").strip()
        action = {
            "read": "正在读取" if status == "started" else "已读取",
            "search": "正在搜索" if status == "started" else "已搜索",
            "write": "正在编辑" if status == "started" else "已编辑",
            "verify": "正在验证" if status == "started" else "已验证",
            "tool": "正在执行工具" if status == "started" else "已执行工具",
        }[cls._tool_kind(tool)]
        if status in {"error", "rejected", "failed", "password_required"}:
            action = {
                "read": "读取失败", "search": "搜索失败", "write": "编辑失败",
                "verify": "验证失败", "tool": "工具执行失败",
            }[cls._tool_kind(tool)]
        return f"{action} {target}".strip()

    @staticmethod
    def _activity_details(tool: str, arguments: dict | None,
                          result: dict | None) -> str:
        sections = [f"工具：{tool or 'unknown'}"]
        if arguments:
            display_arguments = dict(arguments)
            for key in ("content", "patch"):
                value = display_arguments.get(key)
                if isinstance(value, str) and len(value) > 500:
                    display_arguments[key] = (
                        value[:320] + f"\n…（共 {len(value):,} 字符）"
                    )
            sections.append(
                "参数：\n" + json.dumps(
                    display_arguments, ensure_ascii=False, indent=2, default=str
                )[:5000]
            )
        if result:
            sections.append(
                "结果：\n" + json.dumps(
                    result, ensure_ascii=False, indent=2, default=str
                )[:7000]
            )
        return "\n\n".join(sections)

    def add_worker_activity(
        self, task_id: str, *, event_kind: str, tool: str = "",
        path: str = "", turn: int = 0, status: str = "started",
        arguments: dict | None = None, result: dict | None = None,
        duration_ms: int = 0, activity_id: str = "", summary: str = "",
        meta: str = "",
    ) -> str:
        """Insert/update a readable live action in the execution conversation."""
        narrative_group = int(self._worker_narrative_sequence.get(task_id, 0) or 0)
        if self._tool_kind(tool) in {"read", "search"}:
            return self._add_grouped_read_activity(
                task_id, event_kind=event_kind, tool=tool, path=path,
                turn=turn, status=status, arguments=arguments, result=result,
                duration_ms=duration_ms, activity_id=activity_id,
                repair_round=self._active_repair_round,
                narrative_group=narrative_group,
            )
        if self._tool_kind(tool) == "verify":
            return self._add_grouped_verification_activity(
                task_id, event_kind=event_kind, tool=tool, path=path,
                turn=turn, status=status, arguments=arguments, result=result,
                duration_ms=duration_ms, activity_id=activity_id,
                repair_round=self._active_repair_round,
                narrative_group=narrative_group,
            )
        if not activity_id:
            counter = self._activity_counters.get(task_id, 0) + 1
            self._activity_counters[task_id] = counter
            activity_id = f"{task_id or 'task'}-{event_kind}-{turn}-{counter}"
        key = (str(task_id), str(tool), int(turn or 0))
        if event_kind == "tool_started":
            self._pending_tool_activities[key] = activity_id
        elif event_kind == "tool_completed":
            activity_id = self._pending_tool_activities.pop(key, activity_id)
        if not summary:
            summary = self._activity_summary(tool, path, status)
        if not meta:
            parts = [task_id] if task_id else []
            if duration_ms:
                parts.append(
                    f"{duration_ms / 1000:.1f}s"
                    if duration_ms >= 1000 else f"{duration_ms}ms"
                )
            if status in {"error", "rejected", "failed", "password_required"}:
                parts.append("未成功")
            meta = " · ".join(parts)
        details = self._activity_details(tool, arguments, result)
        self.worker_activity.add_or_update(
            activity_id,
            status=status,
            summary=summary,
            meta=meta,
            details=details if arguments or result else "",
        )
        if activity_id == f"{task_id}-task" and task_id in self._task_timings:
            self._refresh_task_time(task_id)
        QTimer.singleShot(0, self._scroll_to_bottom)
        return activity_id

    @staticmethod
    def _read_group_key(task_id: str, repair_round: int = 0,
                        narrative_group: int = 0) -> str:
        return (
            f"{task_id or 'task'}-project-read-{int(repair_round or 0)}"
            f"-{int(narrative_group or 0)}"
        )

    def _add_grouped_read_activity(
        self, task_id: str, *, event_kind: str, tool: str, path: str,
        turn: int, status: str, arguments: dict | None,
        result: dict | None, duration_ms: int, activity_id: str,
        repair_round: int, narrative_group: int = 0,
    ) -> str:
        """Combine read/search/list calls into one Codex-style activity."""
        key = self._read_group_key(task_id, repair_round, narrative_group)
        group = self._read_activity_groups.setdefault(key, {
            "files": [], "entries": {}, "active": 0, "failed": 0,
            "duration_ms": 0,
        })
        target = str(path or "").strip() or "项目文件"
        entry_key = str(activity_id or f"{tool}:{int(turn or 0)}:{target}")
        if event_kind == "tool_started":
            group["active"] += 1
        else:
            group["active"] = max(0, group["active"] - 1)
            if status in {"error", "rejected", "failed"}:
                group["failed"] += 1
        group["duration_ms"] += max(0, int(duration_ms or 0))
        if target not in group["files"]:
            group["files"].append(target)
        group["entries"][entry_key] = {
            "tool": tool, "path": target, "status": status,
            "arguments": arguments or {}, "result": result or {},
        }
        files = group["files"]
        shown = files[:8]
        suffix = f"、以及其他 {len(files) - 8} 项" if len(files) > 8 else ""
        state = "正在读取" if group["active"] else "已读取"
        summary = f"{state}项目文件（{len(files)} 项）"
        if group["failed"]:
            summary += f"，{group['failed']} 项失败"
        meta = "、".join(shown) + suffix
        if group["duration_ms"]:
            meta += f" · {group['duration_ms'] / 1000:.1f}s"
        lines = []
        raw_details = []
        for item in group["entries"].values():
            marker = "!" if item["status"] in {"error", "rejected", "failed"} else "✓"
            lines.append(f"{marker} {item['path']} · {item['tool']}")
            if item["arguments"] or item["result"]:
                raw_details.append(self._activity_details(
                    item["tool"], item["arguments"], item["result"]
                ))
        details = "\n".join(lines)
        if raw_details:
            details += "\n\n" + "\n\n".join(raw_details)
        details = details[:12000]
        item = self.worker_activity.add_or_update(
            key,
            status=("running" if group["active"] else (
                "failed" if group["failed"] else "success"
            )),
            summary=summary, meta=meta, details=details,
        )
        QTimer.singleShot(0, self._scroll_to_bottom)
        return key

    @staticmethod
    def _verification_group_key(task_id: str, repair_round: int = 0,
                                narrative_group: int = 0) -> str:
        return (
            f"{task_id or 'task'}-verification-{int(repair_round or 0)}"
            f"-{int(narrative_group or 0)}"
        )

    def _add_grouped_verification_activity(
        self, task_id: str, *, event_kind: str, tool: str, path: str,
        turn: int, status: str, arguments: dict | None,
        result: dict | None, duration_ms: int, activity_id: str,
        repair_round: int, narrative_group: int = 0,
    ) -> str:
        """Combine repeated commands and final acceptance into one activity."""
        key = self._verification_group_key(
            task_id, repair_round, narrative_group
        )
        group = self._verification_activity_groups.setdefault(key, {
            "entries": {}, "active": 0, "failed": 0, "passed": 0,
            "duration_ms": 0,
        })
        target = str(path or "").strip() or "项目结果"
        entry_key = str(activity_id or f"{tool}:{int(turn or 0)}:{target}")
        if event_kind in {"tool_started", "validation_started"}:
            group["active"] += 1
        else:
            group["active"] = max(0, group["active"] - 1)
            if status in {"error", "rejected", "failed"}:
                group["failed"] += 1
            else:
                group["passed"] += 1
        group["duration_ms"] += max(0, int(duration_ms or 0))
        group["entries"][entry_key] = {
            "tool": tool or "acceptance", "path": target, "status": status,
            "arguments": arguments or {}, "result": result or {},
        }
        total = len(group["entries"])
        if group["active"]:
            summary = f"正在验证项目（{total} 项）"
        elif group["failed"]:
            summary = f"项目验证完成（{total} 项），{group['failed']} 项未通过"
        else:
            summary = f"项目验证通过（{total} 项）"
        meta_parts = []
        if group["passed"]:
            meta_parts.append(f"{group['passed']} 项通过")
        if group["failed"]:
            meta_parts.append(f"{group['failed']} 项未通过")
        if group["duration_ms"]:
            meta_parts.append(f"{group['duration_ms'] / 1000:.1f}s")
        lines = []
        raw_details = []
        for item in group["entries"].values():
            marker = "!" if item["status"] in {"error", "rejected", "failed"} else "✓"
            lines.append(f"{marker} {item['path']} · {item['tool']}")
            if item["arguments"] or item["result"]:
                raw_details.append(self._activity_details(
                    item["tool"], item["arguments"], item["result"]
                ))
        details = "\n".join(lines)
        if raw_details:
            details += "\n\n" + "\n\n".join(raw_details)
        self.worker_activity.add_or_update(
            key,
            status=("running" if group["active"] else (
                "failed" if group["failed"] else "success"
            )),
            summary=summary, meta=" · ".join(meta_parts),
            details=details[:12000],
        )
        QTimer.singleShot(0, self._scroll_to_bottom)
        return key

    def add_worker_thought(self, task_id: str, content: str,
                           duration_ms: int = 0):
        clean = self._normalize_model_output(content)
        if not clean:
            return
        sequence = int(self._worker_narrative_sequence.get(task_id, 0) or 0) + 1
        self._worker_narrative_sequence[task_id] = sequence
        narrative = re.sub(r"\n{3,}", "\n\n", clean).strip()[:4000]
        self.worker_activity.add_or_update(
            f"{task_id or 'task'}-narrative-{sequence}",
            status="success", summary=narrative,
            meta=(f"本段用时 {duration_ms / 1000:.1f}s" if duration_ms else ""),
            details="", variant="narrative",
        )
        QTimer.singleShot(0, self._scroll_to_bottom)

    def load_worker_activities(self, activities: list[dict]):
        self.worker_activity.clear()
        self._activity_counters = {}
        self._pending_tool_activities = {}
        self._read_activity_groups = {}
        self._verification_activity_groups = {}
        self._worker_narrative_sequence = {}
        for index, activity in enumerate(activities or [], start=1):
            self.add_worker_activity(
                str(activity.get("task_id") or ""),
                activity_id=f"history-{index}",
                event_kind="tool_completed",
                tool=str(activity.get("tool") or ""),
                path=str(activity.get("path") or ""),
                turn=int(activity.get("turn", 0) or 0),
                status=str(activity.get("status") or "success"),
                arguments=activity.get("arguments") or {},
                result=activity.get("result") or {},
                duration_ms=int(activity.get("duration_ms", 0) or 0),
            )
        self.worker_activity.set_live(
            bool(self._current_job)
            and self._current_job.get("status") == "executing"
        )

    def add_validation_activity(
        self, task_id: str, *, event_kind: str, command: str = "",
        status: str = "started", output: str = "",
        repair_round: int = 0,
    ) -> str:
        """Route deterministic acceptance through the verification group."""
        return self._add_grouped_verification_activity(
            task_id, event_kind=event_kind, tool="acceptance",
            path=command or "最终验收", turn=0, status=status,
            arguments={"command": command} if command else {},
            result={
                "status": status, "output": str(output or "")[:3000],
            } if event_kind != "validation_started" else {},
            duration_ms=0,
            activity_id=f"{task_id or 'task'}-acceptance",
            repair_round=repair_round,
            narrative_group=int(
                self._worker_narrative_sequence.get(task_id, 0) or 0
            ),
        )

    def add_model_output(self, agent_type: str, provider: str, response: str,
                         error: str | None, duration_ms: int,
                         input_tokens: int = 0, output_tokens: int = 0,
                         cached_input_tokens: int = 0,
                         task_id: str = "", estimated_cost: float = 0.0,
                         billable_cost: float | None = None,
                         billing_mode: str = "api",
                         budget: dict | None = None):
        equivalent_cost = max(0.0, float(estimated_cost or 0.0))
        api_cost = (
            0.0
            if billing_mode == "chatgpt_cli"
            else equivalent_cost
            if billable_cost is None
            else max(0.0, float(billable_cost or 0.0))
        )
        self._usage["input_tokens"] += max(0, int(input_tokens or 0))
        cached_input_tokens = min(
            max(0, int(input_tokens or 0)),
            max(0, int(cached_input_tokens or 0)),
        )
        self._usage["cached_input_tokens"] += cached_input_tokens
        self._usage["output_tokens"] += max(0, int(output_tokens or 0))
        self._usage["calls"] += 1
        self._usage["cost"] += equivalent_cost
        if self._usage.get("billable_cost") is not None:
            self._usage["billable_cost"] += api_cost
        if isinstance(budget, dict):
            self._usage["budget"] = dict(budget)
        self.usage_label.setText(self._format_usage(self._usage, "总用量"))
        if task_id:
            task = next((item for item in self._tasks if item.get("task_id") == task_id), None)
            if task is not None:
                task_usage = task.setdefault("usage", self._empty_usage())
                task_usage["input_tokens"] += max(0, int(input_tokens or 0))
                task_usage["cached_input_tokens"] += cached_input_tokens
                task_usage["output_tokens"] += max(0, int(output_tokens or 0))
                task_usage["calls"] += 1
                task_usage["cost"] += equivalent_cost
                if task_usage.get("billable_cost") is not None:
                    task_usage["billable_cost"] += api_cost
        # Concise Main Agent decisions are rendered from dedicated events.
        # Do not expose raw structured JSON in the primary workflow.
        if agent_type in {"main_agent", "main_agent_summary"}:
            return
        key = (
            "worker" if agent_type.startswith("worker")
            else "governor" if agent_type == "main_agent"
            else agent_type
        )
        repair_round = self._repair_round_from_task_id(task_id)
        if not repair_round and key in {"planner", "reviewer"}:
            repair_round = self._active_repair_round
        stage = self._stage_for(key, repair_round)
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
            if billing_mode == "chatgpt_cli":
                cost_text = (
                    f"等价估算 ¥{equivalent_cost:.4f} · "
                    "ChatGPT 登录不计入 API 成本"
                )
            else:
                cost_text = (
                    f"等价估算 ¥{equivalent_cost:.4f} · "
                    f"可计费 API ¥{api_cost:.4f}"
                )
            cache_detail = (
                f"（缓存输入 {cached_input_tokens}）"
                if cached_input_tokens else ""
            )
            text = (
                f"{provider.upper()} · {duration} · "
                f"{input_tokens}{cache_detail}+{output_tokens} tokens · "
                f"{cost_text}\n"
                f"{content}"
            )
        if key == "worker" and not repair_round:
            self._worker_outputs.append(text)
            if error:
                self.add_worker_activity(
                    task_id, event_kind="model", status="failed",
                    summary=f"执行模型调用失败：{self._friendly_provider_error(error)}",
                    meta=f"{provider.upper()} · {duration}",
                )
            else:
                self.add_worker_thought(task_id, response, duration_ms)
        else:
            stage.append_output(text)

    def set_tasks(self, tasks: list[dict]):
        self._tasks = tasks
        self._refresh_worker_stage()
        self._populate_repair_timeline(self._repair_rounds, self._reviews)
        self._populate_test_details()

    def update_task_status(self, task_id: str, status: str):
        for task in self._tasks:
            if task.get("task_id") == task_id:
                task["status"] = status
                break
        repair_round = self._repair_round_from_task_id(task_id)
        if repair_round:
            repair = next((
                item for item in self._repair_rounds
                if int(item.get("round", 0) or 0) == repair_round
            ), None)
            if repair:
                stage = self._ensure_repair_stage(repair_round, "worker")
                self._set_repair_worker_stage(stage, repair)
        else:
            self._refresh_worker_stage()
        self._refresh_worker_progress()

    def set_worker_progress(
        self, task_id: str, *, task_index: int = 0, task_total: int = 0,
        phase: str = "正在执行", path: str = "", changes: dict | None = None,
        turn: int = 0, max_turns: int = 0,
    ):
        """Show a compact live Worker explanation above the input composer."""
        if not task_id:
            return
        self._progress_sequence += 1
        previous = dict(self._task_progress.get(task_id) or {})
        task = next(
            (item for item in self._tasks if item.get("task_id") == task_id),
            None,
        )
        if not task_index and task is not None:
            task_index = self._tasks.index(task) + 1
        # Live events from older checkpoint runs may describe the remaining
        # subset as 1/N. The complete task list loaded by the UI is authoritative
        # for user-facing original-plan progress.
        if task is not None:
            original_index = self._tasks.index(task) + 1
            original_total = len(self._tasks)
            if task_total and task_total < original_total:
                task_index = original_index
                task_total = original_total
        payload = {
            **previous,
            "task_id": task_id,
            "task_index": task_index or previous.get("task_index", 1),
            "task_total": task_total or previous.get(
                "task_total", max(1, len(self._tasks))
            ),
            "phase": phase or previous.get("phase", "正在执行"),
            "path": path or previous.get("path", ""),
            "turn": turn or previous.get("turn", 0),
            "max_turns": max_turns or previous.get("max_turns", 0),
            "changes": dict(changes or previous.get("changes") or {}),
            "sequence": self._progress_sequence,
        }
        self._task_progress[task_id] = payload
        self._refresh_worker_progress(preferred_task_id=task_id)

    def clear_worker_progress(self):
        self.worker_progress_wrap.hide()

    def _refresh_worker_progress(self, preferred_task_id: str = ""):
        running_ids = {
            str(task.get("task_id") or "") for task in self._tasks
            if task.get("status") in {"running", "executing"}
        }
        if not running_ids:
            self.worker_progress_wrap.hide()
            return
        selected = self._task_progress.get(preferred_task_id)
        if not selected or preferred_task_id not in running_ids:
            candidates = [
                value for key, value in self._task_progress.items()
                if key in running_ids
            ]
            selected = max(
                candidates, key=lambda item: item.get("sequence", 0),
                default=None,
            )
        if selected is None:
            task_id = next(iter(running_ids))
            task = next(
                item for item in self._tasks
                if str(item.get("task_id") or "") == task_id
            )
            selected = {
                "task_id": task_id,
                "task_index": self._tasks.index(task) + 1,
                "task_total": max(1, len(self._tasks)),
                "phase": "正在执行",
                "changes": {},
            }
        index = int(selected.get("task_index", 1) or 1)
        total = max(index, int(selected.get("task_total", 1) or 1))
        parts = [f"第 {index}/{total} 步", str(selected.get("phase") or "正在执行")]
        path = str(selected.get("path") or "").strip()
        if path:
            parts.append(path)
        changes = selected.get("changes") or {}
        changed_files = int(changes.get("files_changed", 0) or 0)
        additions = int(changes.get("additions", 0) or 0)
        deletions = int(changes.get("deletions", 0) or 0)
        if changed_files or additions or deletions:
            parts.append(
                f"{changed_files} 个文件已更改 "
                f"<span style='color:#1a9b50'>+{additions}</span> "
                f"<span style='color:#c94343'>-{deletions}</span>"
            )
        self.worker_progress_label.setText(" · ".join(parts))
        self.worker_progress_label.setToolTip(
            f"{selected.get('task_id', '')} 的实时执行进度；文件统计相对本步骤开始状态"
        )
        self.worker_progress_wrap.show()

    def has_task(self, task_id: str) -> bool:
        return any(task.get("task_id") == task_id for task in self._tasks)

    @staticmethod
    def _format_elapsed(seconds: int) -> str:
        seconds = max(0, int(seconds or 0))
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours}小时 {minutes}分钟 {seconds}秒"
        if minutes:
            return f"{minutes}分钟 {seconds}秒"
        return f"{seconds}秒"

    @staticmethod
    def _parse_optional_time(value) -> datetime | None:
        if not value:
            return None
        try:
            return to_local_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None

    def start_task_timer(self, task_id: str, *, title: str = "",
                         started_at=None) -> None:
        if not task_id:
            return
        timing = self._task_timings.setdefault(task_id, {})
        timing["started_at"] = (
            self._parse_optional_time(started_at)
            or timing.get("started_at")
            or datetime.now().astimezone()
        )
        timing["completed_at"] = None
        timing["title"] = title or timing.get("title", "")
        timing["running"] = True
        self._task_timer.start()
        self._refresh_task_time(task_id)

    def finish_task_timer(self, task_id: str, *, completed_at=None) -> None:
        timing = self._task_timings.get(task_id)
        if not timing:
            return
        timing["completed_at"] = (
            self._parse_optional_time(completed_at)
            or datetime.now().astimezone()
        )
        timing["running"] = False
        self._refresh_task_time(task_id)
        if not any(item.get("running") for item in self._task_timings.values()):
            self._task_timer.stop()

    def _refresh_task_times(self) -> None:
        for task_id, timing in list(self._task_timings.items()):
            if timing.get("running"):
                self._refresh_task_time(task_id)

    def _refresh_task_time(self, task_id: str) -> None:
        timing = self._task_timings.get(task_id) or {}
        started_at = timing.get("started_at")
        if not started_at:
            return
        end = timing.get("completed_at") or datetime.now().astimezone()
        elapsed = int(max(0, (end - started_at).total_seconds()))
        activity_id = f"{task_id}-task"
        item = self.worker_activity.item(activity_id)
        if item is not None:
            item.meta.setText(
                f"{task_id} · 已处理 {self._format_elapsed(elapsed)}"
            )
            item.meta.show()

    def restore_task_timings(self, tasks: list[dict]) -> None:
        self._task_timings = {}
        for task in tasks or []:
            task_id = str(task.get("task_id") or "")
            started_at = self._parse_optional_time(task.get("started_at"))
            if not task_id or not started_at:
                continue
            completed_at = self._parse_optional_time(task.get("completed_at"))
            running = task.get("status") in {"running", "executing"}
            self._task_timings[task_id] = {
                "started_at": started_at,
                "completed_at": None if running else completed_at,
                "title": task.get("title", ""),
                "running": running,
            }
            status = str(task.get("status") or "pending")
            if running:
                self.worker_activity.add_or_update(
                    f"{task_id}-task",
                    status="started",
                    summary=f"正在执行 {task.get('title', '当前步骤')}",
                    meta=task_id,
                    details="",
                )
        if any(item.get("running") for item in self._task_timings.values()):
            self._task_timer.start()
        else:
            self._task_timer.stop()
        for task_id in self._task_timings:
            self._refresh_task_time(task_id)

    def update_job_status(self, job_id: str, status: str):
        if not self._current_job or self._current_job.get("job_id") != job_id:
            return
        self._current_job["status"] = status
        self._set_header_status(status)
        self._set_terminal_actions(status, self._current_job)
        self.worker_activity.set_live(status == "executing")
        if status in TERMINAL_JOB_STATUSES:
            self.worker_activity.clear()
            self.worker_progress_wrap.hide()
            self.set_report_state(
                path=str(self._current_job.get("report_path") or ""),
                available=True,
            )

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
            "max_turns", "exploration_limit", "input_token_budget",
            "document_level", "task_input_budget", "job_input_budget",
            "job_total_budget", "job_api_call_budget", "pdf_pages",
            "estimated_pages", "page_batches", "extension_round",
            "previous_task_input_budget", "budget_reason",
            "skills", "project_skills_approved", "mcp",
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
        if not self._current_job:
            return
        if self._current_job.get("status") in {
            "interrupted", "needs_attention",
        }:
            self.followup_btn.setEnabled(False)
            self.attention_resume_requested.emit(self._current_job)
            return
        self.followup_requested.emit(self._current_job)

    def _request_rollback(self):
        if self._current_job:
            self.rollback_requested.emit(self._current_job)

    def _request_report(self):
        if self._current_job:
            self.report_btn.setEnabled(False)
            self.report_btn.setText("正在生成…")
            self.report_requested.emit(self._current_job)

    def set_report_state(self, *, path: str = "", generating: bool = False,
                         available: bool = True):
        """Expose an existing report or an on-demand generator for old Jobs."""
        if not self._current_job:
            self.report_btn.hide()
            return
        self._current_job["report_path"] = path
        self.report_btn.setVisible(bool(available or path or generating))
        self.report_btn.setText("正在生成…" if generating else "查看报告")
        self.report_btn.setEnabled(bool(available or path) and not generating)

    def _request_attention_resume(self):
        if (
            self._current_job
            and self._current_job.get("status") in {
                "needs_attention", "interrupted",
            }
        ):
            self.attention_resume_btn.setEnabled(False)
            self.attention_resume_requested.emit(self._current_job)

    def _set_terminal_actions(self, status: str, job: dict | None = None):
        """Keep checkpoint resume separate from creating a follow-up Job."""
        is_attention = status == "needs_attention"
        self.followup_btn.setVisible(not is_attention)
        self.followup_btn.setEnabled(status in {
            "done", "failed", "cancelled", "interrupted"
        })
        self.followup_btn.setToolTip(
            "从保存的检查点立即继续同一个需求"
            if status == "interrupted"
            else "基于这次结果提出下一条需求"
        )
        self.rollback_btn.setVisible(status in {
            "done", "failed", "cancelled", "interrupted", "needs_attention",
        })
        self.rollback_btn.setEnabled(status != "rolled_back")
        self.attention_card.setVisible(is_attention)
        self.attention_resume_btn.setEnabled(is_attention)
        if not is_attention:
            return
        job = job or self._current_job or {}
        reason, hint = self._attention_details(job)
        self.attention_reason.setText(f"原因：{reason}")
        self.attention_hint.setText(f"处理方法：{hint}")

    def set_attention_reason(self, reason: str, hint: str = ""):
        """Refresh the live attention card from the authoritative stop event."""
        if not self._current_job:
            return
        self._current_job.update({
            "status": "needs_attention",
            "failure_reason": str(reason or "需要你完成一项外部操作"),
            "recovery_hint": str(hint or ""),
        })
        self._set_header_status("needs_attention")
        self._set_terminal_actions("needs_attention", self._current_job)
        friendly, recovery = self._attention_details(self._current_job)
        self.agent_summary.setText(
            f"需要你的处理：{friendly}。"
            + recovery
        )

    def _attention_details(self, job: dict) -> tuple[str, str]:
        """Prefer a direct task blocker over stale aggregate Job metadata."""
        direct_reason = next((
            str(task.get("failure_reason") or "").strip()
            for task in self._tasks
            if task.get("status") == "needs_attention"
            and str(task.get("failure_reason") or "").strip()
        ), "")
        raw_reason = direct_reason or str(
            job.get("failure_reason") or "需要你完成一项外部操作"
        ).strip()
        normalized = raw_reason.lower()
        hint = str(job.get("recovery_hint") or "").strip()
        if any(marker in normalized for marker in (
            "insufficient balance", "insufficient_balance",
            "error code: 402", "status code: 402",
        )):
            hint = (
                "请充值当前模型供应商的 API 余额，或在项目 AI 配置中改用"
                "有可用额度的执行模型；保存后点击“已处理，继续完成任务”，"
                "将从当前任务检查点恢复。"
            )
        elif not hint:
            hint = "完成操作后点击下方按钮；RockCore 会从刚才中断的位置继续。"
        return self._friendly_provider_error(raw_reason), hint

    def _set_header_status(self, status: str):
        style = STATUS_STYLE.get(status, STATUS_STYLE["created"])
        self.job_status_indicator.set_status(status, style)
        self.job_status_label.setText(style["text"])
        self.job_status_label.setStyleSheet(f"color:{style['color']};")

    def _refresh_worker_stage(self):
        stage = self.stages["worker"]
        base_tasks = [
            task for task in self._tasks
            if not self._repair_round_from_task_id(task.get("task_id", ""))
        ]
        if not base_tasks:
            stage.set_status("pending")
            if self._worker_outputs:
                stage.set_output("\n\n".join(self._worker_outputs[-10:]), expand=True)
            return
        task_types = {
            str(task.get("task_type") or "coding") for task in base_tasks
        }
        if task_types and task_types <= {"analysis", "review"}:
            stage.subtitle_label.setText("项目读取与分析报告")
        elif task_types & {"analysis", "review"}:
            stage.subtitle_label.setText("项目分析、文件修改与验证")
        else:
            stage.subtitle_label.setText("文件修改与验证")
        statuses = [task.get("status", "pending") for task in base_tasks]
        if any(status in {"running", "executing"} for status in statuses):
            overall = "running"
        elif any(status == "needs_attention" for status in statuses):
            overall = "needs_attention"
        elif any(status == "interrupted" for status in statuses):
            overall = "interrupted"
        elif any(status == "failed" for status in statuses):
            overall = "failed"
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
        for index, task in enumerate(base_tasks, 1):
            style = STATUS_STYLE.get(task.get("status", "pending"), STATUS_STYLE["pending"])
            lines.append(
                f"{style['icon']} 步骤 {index} · {task.get('title', '')} · "
                f"{style['text']}"
            )
            description = task.get("description", "").strip()
            if description and description != task.get("title", ""):
                lines.append(f"    {description[:300]}")
            task_status = task.get("status", "pending")
            failure_reason = str(task.get("failure_reason") or "").strip()
            if task_status == "needs_attention" and failure_reason:
                lines.append(
                    "    需处理：" + self._friendly_provider_error(failure_reason)
                )
            elif task_status == "blocked":
                dependencies = task.get("dependencies") or []
                lines.append(
                    "    等待依赖任务完成后自动继续"
                    + (f"：{', '.join(dependencies)}" if dependencies else "")
                )
            elif task_status in {"failed", "interrupted"} and failure_reason:
                lines.append(f"    原因：{failure_reason[:500]}")
            paths = task.get("allowed_paths") or []
            if paths and paths != ["*"]:
                lines.append(f"    文件：{', '.join(paths[:8])}")
            usage_text = self._format_usage(task.get("usage") or {}, "    模型用量")
            if usage_text:
                lines.append(usage_text)
            for result in (task.get("test_results") or [])[:3]:
                result_style = STATUS_STYLE.get(result.get("status", "pending"), STATUS_STYLE["pending"])
                lines.append(
                    f"    {result_style['icon']} 验收：{result.get('command', '') or '本地检查'} · "
                    f"{result_style['text']}"
                )
        stage.set_output(
            "\n".join(lines),
            # The narrative activity stream is the primary execution view.
            # Keep the legacy per-task dump available behind the disclosure,
            # without opening a large duplicate text box automatically.
            expand=False,
        )
        stage.setToolTip("\n".join(
            f"步骤 {index}：{task.get('task_id', '?')}"
            for index, task in enumerate(base_tasks, 1)
        ))

    def _populate_test_details(self):
        lines = []
        for index, task in enumerate(self._tasks, 1):
            for result in task.get("test_results") or []:
                style = STATUS_STYLE.get(result.get("status", "pending"), STATUS_STYLE["pending"])
                lines.append(
                    f"{style['icon']} 步骤 {index} · "
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
        return format_local_timestamp(value, include_offset=True)

    @staticmethod
    def _issue_text(issue) -> str:
        if isinstance(issue, dict):
            return str(issue.get("problem") or issue.get("message") or issue)
        return str(issue)

    @staticmethod
    def _friendly_provider_error(error: str) -> str:
        normalized = (error or "").lower()
        if "insufficient balance" in normalized or "insufficient_balance" in normalized:
            return "当前模型供应商 API 余额不足（HTTP 402）"
        if "credit_balance_exhausted" in normalized or "no credits remaining" in normalized:
            return (
                "Platform API 账户无可用余额，或认证通道配置错误"
                "（这不代表 ChatGPT/Codex 用量耗尽）"
            )
        if "insufficient_quota" in normalized:
            return (
                "Platform API 配额不足（HTTP 429，insufficient_quota；"
                "与 ChatGPT/Codex 订阅用量无关）"
            )
        if "rate limit" in normalized or "too many requests" in normalized:
            return "模型服务触发速率限制（HTTP 429）"
        if "timed out" in normalized or "timeout" in normalized:
            return "模型请求超时"
        return (error or "未知错误")[:500]

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
