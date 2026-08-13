"""Main window for the AI Engineering Studio."""

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QLabel, QTextEdit, QMessageBox, QFrame, QFileDialog,
    QScrollArea,
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QByteArray, QBuffer, QIODevice, QUrl,
)
from PyQt6.QtGui import (
    QAction, QDesktopServices, QFont, QKeySequence, QShortcut, QImage,
    QImageReader, QPixmap,
)

from app.image_attachments import (
    MAX_IMAGE_ATTACHMENTS,
    SUPPORTED_IMAGE_SUFFIXES,
    store_image_bytes,
    store_image_file,
)
from app.paths import ProjectStateCleanupError, remove_project_state
from .project_panel import ProjectPanel
from .task_panel import TaskPanel
from .settings_dialog import SettingsDialog, load_config
from .time_utils import as_utc_isoformat
from app.branding import COMPANY_NAME, FULL_PRODUCT_NAME, LEGAL_COMPANY_NAME, PRODUCT_LINE
from app.subprocess_utils import run_process

logger = logging.getLogger(__name__)


class ComposerTextEdit(QTextEdit):
    """Text editor that turns pasted images or image files into attachments."""

    image_pasted = pyqtSignal(object)
    image_files_pasted = pyqtSignal(list)

    def canInsertFromMimeData(self, source):
        return bool(source.hasImage() or super().canInsertFromMimeData(source))

    def insertFromMimeData(self, source):
        image_files = [
            url.toLocalFile()
            for url in source.urls()
            if url.isLocalFile()
            and Path(url.toLocalFile()).suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
        ] if source.hasUrls() else []
        if image_files:
            self.image_files_pasted.emit(image_files)
            return
        if source.hasImage():
            self.image_pasted.emit(source.imageData())
            return
        super().insertFromMimeData(source)


class SignalBridge(QObject):
    """Bridge for cross-thread signals."""

    event_received = pyqtSignal(str, dict)
    log_message = pyqtSignal(str, str)
    job_status = pyqtSignal(str, str)
    task_update = pyqtSignal(str, str)
    diff_updated = pyqtSignal(str)
    projects_loaded = pyqtSignal(list)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, engine=None):
        super().__init__()
        self.engine = engine
        self.bridge = SignalBridge()
        self._current_project = None
        self._selected_job_id = None
        self._running_job_id = None
        self._job_starting = False
        self._running_jobs: dict[str, str] = {}
        self._starting_projects: set[str] = set()
        self._job_projects: dict[str, str] = {}
        self._queued_by_project: dict[str, dict] = {}
        self._job_event_buffers: dict[str, list[tuple[str, dict]]] = {}
        self._followup_source_job_id = None
        self._queued_request = None
        self._queued_source_job_id = None
        self._attachments: list[dict] = []
        self._queued_attachments: list[dict] = []
        self._config = load_config()
        self._setup_ui()
        self._connect_signals()
        self._setup_menu()

        # Poll timer for async engine events
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll_events)
        self._poll_timer.start(200)

    @staticmethod
    def _project_key(project: dict | None) -> str:
        root = str((project or {}).get("root_path") or "").strip()
        return os.path.normcase(os.path.abspath(root)) if root else ""

    def _current_project_key(self) -> str:
        return self._project_key(self._current_project)

    def _current_running_job(self) -> str | None:
        key = self._current_project_key()
        return self._running_jobs.get(key) or self._running_job_id

    def _selected_running_job(self) -> str | None:
        if self._selected_job_id:
            return (
                self._selected_job_id
                if self._selected_job_id in self._running_jobs.values()
                else None
            )
        return self._current_running_job()

    def _sync_project_runtime_state(self):
        key = self._current_project_key()
        self._running_job_id = self._running_jobs.get(key)
        self._job_starting = key in self._starting_projects
        running = self._selected_running_job()
        self.pause_btn.setVisible(bool(running))
        self.stop_btn.setVisible(bool(running))
        if not running:
            self.run_btn.hide()

    def _sync_current_queue_state(self):
        queued = self._queued_by_project.get(self._current_project_key())
        self._queued_request = queued.get("request") if queued else None
        self._queued_source_job_id = (
            queued.get("source_job_id") if queued else None
        )
        self._queued_attachments = list(
            queued.get("attachments") or []
        ) if queued else []
        if queued:
            preview = str(queued["request"]).replace("\n", " ")[:80]
            self.queue_label.setText(f"下一轮已排队：{preview}")
            self.queue_bar.show()
        else:
            self.queue_bar.hide()

    def _replay_buffered_job_events(self, job_id: str):
        events = self._job_event_buffers.pop(job_id, [])
        for event_type, data in events:
            self._on_event(event_type, data)

    def _setup_ui(self):
        self.setWindowTitle(FULL_PRODUCT_NAME)
        self.setMinimumSize(1080, 720)
        self.resize(1380, 900)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        self.project_panel = ProjectPanel()
        self.project_panel.setMinimumWidth(230)
        self.project_panel.setMaximumWidth(310)
        splitter.addWidget(self.project_panel)

        workspace = QWidget()
        workspace.setObjectName("workspace")
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)

        self.task_panel = TaskPanel()
        workspace_layout.addWidget(self.task_panel, 1)

        composer_wrap = QWidget()
        composer_wrap.setObjectName("composerWrap")
        composer_outer = QHBoxLayout(composer_wrap)
        composer_outer.setContentsMargins(40, 10, 40, 20)
        composer_outer.addStretch(1)
        composer = QFrame()
        composer.setObjectName("composer")
        composer.setMaximumWidth(900)
        composer_layout = QVBoxLayout(composer)
        composer_layout.setContentsMargins(12, 8, 10, 9)
        composer_layout.setSpacing(5)

        self.queue_bar = QFrame()
        self.queue_bar.setObjectName("queueBar")
        queue_layout = QHBoxLayout(self.queue_bar)
        queue_layout.setContentsMargins(8, 4, 4, 4)
        self.queue_label = QLabel("")
        self.queue_label.setObjectName("queueLabel")
        queue_layout.addWidget(self.queue_label, 1)
        self.cancel_queue_btn = QPushButton("×")
        self.cancel_queue_btn.setObjectName("quietIconButton")
        self.cancel_queue_btn.setFixedSize(24, 24)
        self.cancel_queue_btn.setToolTip("取消排队")
        self.cancel_queue_btn.clicked.connect(self._cancel_queued_request)
        queue_layout.addWidget(self.cancel_queue_btn)
        self.queue_bar.hide()
        composer_layout.addWidget(self.queue_bar)

        self.input_text = ComposerTextEdit()
        self.input_text.setObjectName("composerInput")
        self.input_text.setPlaceholderText(
            "描述你希望 RockCore 完成的工作"
        )
        self.input_text.setMinimumHeight(54)
        self.input_text.setMaximumHeight(130)
        self.input_text.textChanged.connect(self._update_send_state)
        self.input_text.image_pasted.connect(self._add_clipboard_image)
        self.input_text.image_files_pasted.connect(self._add_image_files)
        composer_layout.addWidget(self.input_text)

        self.attachment_scroll = QScrollArea()
        self.attachment_scroll.setObjectName("attachmentScroll")
        self.attachment_scroll.setWidgetResizable(True)
        self.attachment_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.attachment_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.attachment_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.attachment_scroll.setFixedHeight(70)
        self.attachment_content = QWidget()
        self.attachment_content.setObjectName("attachmentContent")
        self.attachment_layout = QHBoxLayout(self.attachment_content)
        self.attachment_layout.setContentsMargins(0, 2, 0, 2)
        self.attachment_layout.setSpacing(6)
        self.attachment_layout.addStretch(1)
        self.attachment_scroll.setWidget(self.attachment_content)
        self.attachment_scroll.hide()
        composer_layout.addWidget(self.attachment_scroll)

        composer_actions = QHBoxLayout()
        composer_actions.setSpacing(6)
        self.attach_image_btn = QPushButton("＋")
        self.attach_image_btn.setObjectName("composerToolButton")
        self.attach_image_btn.setFixedSize(30, 30)
        self.attach_image_btn.setToolTip("添加图片（也可直接粘贴剪贴板图片）")
        self.attach_image_btn.clicked.connect(self._choose_images)
        composer_actions.addWidget(self.attach_image_btn)
        self.followup_source_label = QLabel("新需求")
        self.followup_source_label.setObjectName("composerContext")
        composer_actions.addWidget(self.followup_source_label)
        self.clear_followup_btn = QPushButton("×")
        self.clear_followup_btn.setObjectName("quietIconButton")
        self.clear_followup_btn.setFixedSize(24, 24)
        self.clear_followup_btn.setToolTip("取消承接，改为独立的新需求")
        self.clear_followup_btn.setVisible(False)
        self.clear_followup_btn.clicked.connect(self._clear_followup_source)
        composer_actions.addWidget(self.clear_followup_btn)
        self.status_label = QLabel("")
        self.status_label.setObjectName("composerStatus")
        composer_actions.addWidget(self.status_label)
        composer_actions.addStretch()

        self.run_btn = QPushButton("▶")
        self.run_btn.setObjectName("composerToolButton")
        self.run_btn.setFixedSize(32, 32)
        self.run_btn.setToolTip("继续执行")
        self.run_btn.clicked.connect(self._on_run)
        self.run_btn.hide()
        composer_actions.addWidget(self.run_btn)
        self.pause_btn = QPushButton("Ⅱ")
        self.pause_btn.setObjectName("composerToolButton")
        self.pause_btn.setFixedSize(32, 32)
        self.pause_btn.setToolTip("当前步骤结束后暂停")
        self.pause_btn.clicked.connect(self._on_pause)
        self.pause_btn.hide()
        composer_actions.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("■")
        self.stop_btn.setObjectName("composerToolButton")
        self.stop_btn.setFixedSize(32, 32)
        self.stop_btn.setToolTip("停止执行")
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.hide()
        composer_actions.addWidget(self.stop_btn)

        self.start_task_btn = QPushButton("↑")
        self.start_task_btn.setObjectName("sendButton")
        self.start_task_btn.setFixedSize(34, 34)
        self.start_task_btn.setToolTip("提交需求（⌘/Ctrl + Enter）")
        self.start_task_btn.clicked.connect(self._on_submit_request)
        self.start_task_btn.setEnabled(False)
        composer_actions.addWidget(self.start_task_btn)
        composer_layout.addLayout(composer_actions)
        composer_outer.addWidget(composer, 8)
        composer_outer.addStretch(1)
        workspace_layout.addWidget(composer_wrap)

        splitter.addWidget(workspace)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([272, 1108])
        main_layout.addWidget(splitter)

        self.submit_shortcut = QShortcut(QKeySequence("Ctrl+Return"), self.input_text)
        self.submit_shortcut.activated.connect(self._on_submit_request)
        self.submit_shortcut_mac = QShortcut(QKeySequence("Meta+Return"), self.input_text)
        self.submit_shortcut_mac.activated.connect(self._on_submit_request)

    def _setup_menu(self):
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件")
        project_config_action = QAction("项目配置...", self)
        project_config_action.triggered.connect(self._open_project_config)
        file_menu.addAction(project_config_action)

        file_menu.addSeparator()
        exit_action = QAction("退出", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menubar.addMenu("查看")
        for name in ["运行记录", "代码变更", "验收结果"]:
            action = QAction(name, self)
            action.triggered.connect(lambda checked, n=name: self._switch_tab(n))
            view_menu.addAction(action)

        help_menu = menubar.addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _connect_signals(self):
        self.bridge.event_received.connect(self._on_event)
        self.bridge.log_message.connect(self.task_panel.log)
        self.bridge.job_status.connect(self._on_job_status)
        self.bridge.task_update.connect(self.task_panel.update_task_status)
        self.bridge.diff_updated.connect(self.task_panel.set_diff)
        self.bridge.projects_loaded.connect(self.project_panel.set_projects)
        self.bridge.job_status.connect(self.task_panel.update_job_status)
        self.bridge.job_status.connect(self.project_panel.update_job_status)
        self.project_panel.job_selected.connect(self._on_job_selected)
        self.project_panel.settings_requested.connect(self._open_settings)
        self.task_panel.followup_requested.connect(self._on_followup_requested)
        self.task_panel.attention_resume_requested.connect(
            self._on_attention_resume_requested
        )
        self.task_panel.rollback_requested.connect(self._on_rollback_requested)
        self.task_panel.report_requested.connect(self._on_report_requested)

        self.project_panel.project_selected.connect(self._on_project_selected)
        self.project_panel.project_deleted.connect(self._on_project_deleted)

    def _on_project_selected(self, data: dict):
        if data.get("action") == "create":
            self._create_project(data)
            return

        self._current_project = data
        self._selected_job_id = None
        self._followup_source_job_id = self._latest_project_job_id(data)
        self._sync_project_runtime_state()
        self._sync_current_queue_state()
        if self._followup_source_job_id:
            self.followup_source_label.setText(
                f"继续 {self._followup_source_job_id}"
            )
            self.clear_followup_btn.setVisible(True)
            self.input_text.setPlaceholderText(
                "继续描述这个项目要补充、修改或修复的内容"
            )
        else:
            self.followup_source_label.setText("首个需求")
            self.clear_followup_btn.setVisible(False)
        self._update_send_state()
        running = self._current_running_job()
        self.status_label.setText(
            f"项目：{data.get('name', '')} · "
            + (f"{running} 正在执行，可继续提交其他项目" if running
               else "可以提交需求")
        )
        self.task_panel.begin_new_request(data.get("name", ""), data.get("root_path", ""))
        self.task_panel.log(f"已选择项目：{data.get('name', '')}", "log")

        # Load job history for the selected project
        self._refresh_job_list()
        if running:
            self.project_panel.select_job(running)

    def _on_job_selected(self, data: dict):
        """Load one conversation and the latest turn's live execution trace."""
        if self.engine:
            repos = self._get_repos()
            try:
                session_id = str(
                    data.get("session_id") or data.get("execution_session_id") or ""
                )
                selected_job = repos["job"].get_by_id(
                    str(data.get("latest_job_id") or data.get("job_id") or "")
                )
                if not session_id and selected_job:
                    session_id = str(selected_job.execution_session_id or "")
                conversation = (
                    repos["conversation"].get(session_id) if session_id else None
                )
                turns = (
                    repos["job"].list_by_session(session_id)
                    if session_id else []
                )
                latest_job_id = str(
                    data.get("latest_job_id") or data.get("job_id") or ""
                )
                job = turns[-1] if turns else selected_job
                if job:
                    if not turns:
                        turns = [job]
                    tasks = repos["task"].list_by_job(job.id)
                    constitution = repos["constitution"].get_by_job(job.id)
                    plan = repos["plan"].get_by_job(job.id)
                    reviews = repos["review"].list_by_job(job.id)
                    self._selected_job_id = job.job_id
                    task_dicts = [
                        {
                            "task_id": t.task_id,
                            "title": t.title,
                            "task_type": t.task_type,
                            "status": t.status,
                            "failure_reason": t.failure_reason or "",
                            "dependencies": t.dependencies or [],
                            "acceptance_command": t.acceptance_command or "",
                            "description": t.description or "",
                            "allowed_paths": t.allowed_paths or [],
                            "worker_activities": self._task_worker_activities(
                                t.task_id,
                                repos["agent_run"].list_by_task(t.id),
                            ),
                            "usage": self._task_usage(
                                repos["agent_run"].list_by_task(t.id)
                            ),
                            "test_results": [
                                {
                                    "command": test.command,
                                    "status": test.status,
                                    "output": test.output or "",
                                }
                                for test in repos["test_run"].list_by_task(t.id)
                            ],
                        }
                        for t in tasks
                    ]
                    job_dict = {
                        "job_id": job.job_id,
                        "user_request": job.user_request,
                        "attachments": list(getattr(job, "attachments", None) or []),
                        "status": job.status,
                        "source_job_id": job.source_job_id,
                        "execution_session_id": job.execution_session_id,
                        "failure_code": getattr(job, "failure_code", "") or "",
                        "failure_reason": getattr(job, "failure_reason", "") or "",
                        "recovery_hint": getattr(job, "recovery_hint", "") or "",
                        "created_at": as_utc_isoformat(job.created_at),
                        "turn_number": len(turns),
                        "turn_total": len(turns),
                        "usage": self._job_usage(job),
                        "report_path": str(
                            self.engine.job_reports.report_path(
                                job.job_id, existing_only=True,
                            ) or ""
                        ),
                    }
                    constitution_dict = None
                    if constitution:
                        constitution_dict = {
                            "goal": constitution.goal,
                            "constraints": constitution.constraints or [],
                            "acceptance_criteria": constitution.acceptance_criteria or [],
                            "risk": constitution.risk,
                            "requires_final_review": constitution.requires_final_review,
                            "raw_output": constitution.raw_output or {},
                        }
                    plan_dict = None
                    if plan:
                        plan_dict = {
                            "summary": plan.summary,
                            "raw_output": plan.raw_output or {},
                        }
                    review_dicts = [
                        {
                            "result": review.result,
                            "severity": review.severity,
                            "issues": review.issues or [],
                            "summary": review.summary or "",
                        }
                        for review in reviews
                    ]
                    self.task_panel.set_workflow(
                        job_dict, constitution_dict, plan_dict, task_dicts, review_dicts
                    )
                    session_dict = {
                        "session_id": job.execution_session_id,
                        "title": (
                            getattr(conversation, "title", "")
                            or turns[0].user_request
                        ),
                    }
                    public_turns = []
                    for turn in turns:
                        turn_tasks = repos["task"].list_by_job(turn.id)
                        summary = next((
                            task.result_summary for task in reversed(turn_tasks)
                            if task.result_summary
                        ), "")
                        public_turns.append({
                            "job_id": turn.job_id,
                            "user_request": turn.user_request,
                            "status": turn.status,
                            "summary": summary,
                        })
                    self.task_panel.set_conversation(session_dict, public_turns)
                    if job.status in {"needs_attention", "interrupted"}:
                        self._followup_source_job_id = None
                        self.followup_source_label.setText(
                            (
                                f"等待处理 {job.job_id}"
                                if job.status == "needs_attention"
                                else f"待继续 {job.job_id}"
                            )
                        )
                        self.clear_followup_btn.setVisible(False)
                        self.input_text.setPlaceholderText(
                            (
                                "请按上方提示处理，然后点击“已处理，继续完成任务”"
                                if job.status == "needs_attention"
                                else "点击右上角“继续此需求”，将从中断步骤直接继续"
                            )
                        )
                    else:
                        self._followup_source_job_id = job.job_id
                        self.followup_source_label.setText(f"继续 {job.job_id}")
                        self.clear_followup_btn.setVisible(True)
                        self.input_text.setPlaceholderText(
                            "继续描述要补充、修改或修复的内容"
                        )
                    self._sync_project_runtime_state()
                    self._replay_buffered_job_events(job.job_id)
                    self._capture_diff()
            finally:
                self._close_repos(repos)

    def _on_followup_requested(self, data: dict):
        source_id = data.get("job_id")
        if not source_id:
            return
        self._followup_source_job_id = source_id
        self.followup_source_label.setText(f"继续 {source_id}")
        self.clear_followup_btn.setVisible(True)
        self.input_text.setFocus()
        self.input_text.setPlaceholderText("继续描述要补充、修改或修复的内容")
        self.status_label.setText(f"已选择 {source_id}，请输入后续需求")

    def _on_attention_resume_requested(self, data: dict):
        """Resume the persisted Job instead of creating a continuation Job."""
        job_id = str(data.get("job_id") or "")
        if not job_id or not self.engine or not self._current_project:
            return
        project_data = dict(self._current_project)
        project_key = self._project_key(project_data)
        if project_key in self._running_jobs or project_key in self._starting_projects:
            self.task_panel.attention_resume_btn.setEnabled(True)
            QMessageBox.information(
                self,
                "项目正在执行",
                "这个项目已有任务在运行，请等待当前任务结束后再继续。",
            )
            return
        self._running_jobs[project_key] = job_id
        self._job_projects[job_id] = project_key
        self._running_job_id = job_id
        self._selected_job_id = job_id
        self._followup_source_job_id = None
        self.followup_source_label.setText(f"恢复 {job_id}")
        self.clear_followup_btn.setVisible(False)
        self.pause_btn.show()
        self.stop_btn.show()
        self.task_panel.update_job_status(job_id, "executing")
        self.status_label.setText("正在从中断位置继续任务")
        asyncio.ensure_future(
            self._resume_attention_job_async(job_id, project_data)
        )

    async def _resume_attention_job_async(self, job_id: str,
                                          project_data: dict):
        project_key = self._project_key(project_data)
        try:
            await self.engine.resume_attention_job(
                job_id, str(project_data.get("root_path") or "")
            )
        except Exception as error:
            if project_key == self._current_project_key():
                self.bridge.log_message.emit(
                    f"恢复任务失败：{error}", "log"
                )
            logger.exception("恢复任务失败：%s", job_id)
        finally:
            if self._running_jobs.get(project_key) == job_id:
                self._running_jobs.pop(project_key, None)
            self._job_projects.pop(job_id, None)
            if project_key == self._current_project_key():
                self._running_job_id = self._running_jobs.get(project_key)
                self._refresh_job_list()
                self.project_panel.select_job(job_id)
                self._sync_project_runtime_state()
                self._update_send_state()

    def _on_rollback_requested(self, data: dict):
        """Confirm and reverse one Job's Git commits without exposing Git."""
        job_id = str(data.get("job_id") or "")
        if not job_id or not self.engine or not self._current_project:
            return
        project_data = dict(self._current_project)
        project_key = self._project_key(project_data)
        if project_key in self._running_jobs or project_key in self._starting_projects:
            QMessageBox.information(
                self, "项目正在执行",
                "请等待这个项目的当前任务结束后再回退。",
            )
            return
        answer = QMessageBox.question(
            self,
            "回退此需求",
            "RockCore 将撤销这次需求产生的代码变更，但会保留"
            "需求记录、执行报告和后续需求。\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.task_panel.rollback_btn.setEnabled(False)
        self.status_label.setText(f"正在安全回退 {job_id}")
        asyncio.ensure_future(
            self._rollback_job_async(job_id, project_data)
        )

    async def _rollback_job_async(self, job_id: str, project_data: dict):
        try:
            result = await self.engine.rollback_job(
                job_id, str(project_data.get("root_path") or "")
            )
            if result.get("status") != "rolled_back":
                QMessageBox.warning(
                    self, "无法回退",
                    str(result.get("error") or "未能安全回退这次需求。"),
                )
            else:
                QMessageBox.information(
                    self, "已回退",
                    "这次需求的代码变更已安全撤销，历史记录已保留。",
                )
        except Exception as error:
            logger.exception("回退任务失败：%s", job_id)
            QMessageBox.warning(self, "回退失败", str(error))
        finally:
            self._refresh_job_list()
            self.project_panel.select_job(job_id)
            self._capture_diff()

    def _on_report_requested(self, data: dict):
        """Open an existing report or generate one for a historical Job."""
        job_id = str(data.get("job_id") or "")
        if not job_id or not self.engine:
            self.task_panel.set_report_state(available=True)
            return
        existing = self.engine.job_reports.report_path(
            job_id, existing_only=True,
        )
        if existing:
            self._open_job_report(str(existing))
            return
        self.task_panel.set_report_state(generating=True, available=True)
        asyncio.ensure_future(self._generate_and_open_job_report(job_id))

    async def _generate_and_open_job_report(self, job_id: str):
        try:
            path = await self.engine.generate_job_report(job_id)
        except Exception as error:
            self.task_panel.set_report_state(available=True)
            QMessageBox.warning(
                self, "报告生成失败", f"无法生成 {job_id} 的 PDF 报告：\n{error}",
            )
            return
        self._open_job_report(path)

    def _open_job_report(self, path: str):
        report = Path(path)
        self.task_panel.set_report_state(
            path=str(report), available=report.is_file(),
        )
        if not report.is_file():
            QMessageBox.warning(self, "报告不可用", "PDF 报告文件尚未生成。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(report))):
            QMessageBox.information(
                self, "任务报告", f"报告已生成，请从以下位置打开：\n{report}",
            )

    def _clear_followup_source(self):
        self._followup_source_job_id = None
        self.followup_source_label.setText("独立会话")
        self.clear_followup_btn.setVisible(False)
        self.input_text.setPlaceholderText("描述你希望 RockCore 完成的工作")
        self.status_label.setText("已切换为这个项目的独立会话")

    def _latest_project_job_id(self, project_data: dict | None) -> str | None:
        """Return the default continuation anchor for a selected project."""
        if not self.engine or not project_data:
            return None
        repos = self._get_repos()
        try:
            project = repos["project"].get_by_name(
                str(project_data.get("name") or "")
            )
            if not project:
                return None
            latest = repos["job"].latest_by_project(project.id)
            return latest.job_id if latest else None
        finally:
            self._close_repos(repos)

    def _start_new_request(self):
        self._selected_job_id = None
        self.project_panel.clear_job_selection()
        self._clear_followup_source()
        if self._current_project:
            self.task_panel.begin_new_request(
                self._current_project.get("name", ""),
                self._current_project.get("root_path", ""),
            )
        self.input_text.setFocus()

    def _on_project_deleted(self, name: str):
        if self.engine:
            repos = self._get_repos()
            try:
                project = repos["project"].get_by_name(name)
                if project:
                    project_key = self._project_key({
                        "name": project.name,
                        "root_path": project.root_path,
                    })
                    if (
                        project_key in self._running_jobs
                        or project_key in self._starting_projects
                    ):
                        QMessageBox.warning(
                            self,
                            "项目正在执行",
                            "请先停止该项目正在运行的任务，再删除项目。",
                        )
                        return
                    try:
                        removed_state = remove_project_state(project.root_path)
                    except ProjectStateCleanupError as error:
                        self.task_panel.log(
                            f"项目未删除，状态清理失败：{error}", "log"
                        )
                        QMessageBox.critical(
                            self,
                            "无法删除项目",
                            "RockCore 无法安全清理该项目的 .ai 状态目录，"
                            "因此保留了项目记录。\n\n"
                            f"{error}",
                        )
                        return
                    repos["project"].delete(project.id)
                    self.project_panel.remove_project(name)
                    if self._current_project and self._current_project.get("name") == name:
                        self._current_project = None
                        self._selected_job_id = None
                        self._clear_followup_source()
                        self.project_panel.set_jobs([])
                        self.task_panel.set_tasks([])
                        self.task_panel.begin_new_request()
                        self._update_send_state()
                        self.status_label.setText("项目已删除，请选择其他项目")
                    state_note = (
                        f"，已清理 {len(removed_state)} 个 .ai 状态目录"
                        if removed_state else "，未发现遗留 .ai 状态"
                    )
                    self.task_panel.log(
                        f"已删除项目：{name}{state_note}", "log"
                    )
            finally:
                self._close_repos(repos)

    def _create_project(self, data: dict):
        if self.engine:
            repos = self._get_repos()
            try:
                project = repos["project"].create(
                    name=data["name"],
                    root_path=data["root_path"],
                    description=data.get("description", ""),
                )
                self._current_project = {
                    "id": project.id,
                    "name": project.name,
                    "root_path": project.root_path,
                }
                from git.repository import Repository
                git_state = Repository(project.root_path).ensure_initialized()
                if git_state.get("status") == "initialized":
                    self.task_panel.log(
                        f"已建立 Git 初始基线：{git_state.get('commit', '')}", "log"
                    )
                elif git_state.get("gitignore_updated"):
                    self.task_panel.log(
                        "已保留原有规则并自动更新项目 .gitignore", "log"
                    )
                elif git_state.get("status") == "failed":
                    self.task_panel.log(
                        f"Git 初始化失败，将使用文件快照：{git_state.get('error', '')}",
                        "log",
                    )
                self._update_send_state()
                self.status_label.setText(f"项目：{project.name}")
                self.task_panel.begin_new_request(project.name, project.root_path)
                self.task_panel.log(f"已创建项目：{project.name}", "log")

                # Refresh project list
                all_projects = repos["project"].list_all()
                self.bridge.projects_loaded.emit([
                    {"name": p.name, "root_path": p.root_path,
                     "description": p.description}
                    for p in all_projects
                ])
                self.project_panel.select_project(project.name)
            finally:
                self._close_repos(repos)

    def _choose_images(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp)",
        )
        if paths:
            self._add_image_files(paths)

    def _add_image_files(self, paths: list[str]):
        errors = []
        for path_text in paths:
            if len(self._attachments) >= MAX_IMAGE_ATTACHMENTS:
                errors.append(f"一次最多附加 {MAX_IMAGE_ATTACHMENTS} 张图片")
                break
            reader = QImageReader(path_text)
            if not reader.canRead():
                errors.append(f"无法读取图片：{Path(path_text).name}")
                continue
            try:
                record = store_image_file(path_text)
            except (OSError, ValueError) as error:
                errors.append(str(error))
                continue
            if any(
                item.get("sha256") == record.get("sha256")
                for item in self._attachments
            ):
                continue
            self._attachments.append(record)
        self._render_attachments()
        self._update_send_state()
        if errors:
            QMessageBox.warning(self, "无法添加部分图片", "\n".join(errors[:4]))

    def _add_clipboard_image(self, image_data):
        if len(self._attachments) >= MAX_IMAGE_ATTACHMENTS:
            QMessageBox.information(
                self, "图片数量已满",
                f"一次最多附加 {MAX_IMAGE_ATTACHMENTS} 张图片。",
            )
            return
        if isinstance(image_data, QPixmap):
            image = image_data.toImage()
        elif isinstance(image_data, QImage):
            image = image_data
        else:
            image = QImage(image_data)
        if image.isNull():
            QMessageBox.warning(self, "无法粘贴图片", "剪贴板中的图片无法读取。")
            return
        encoded = QByteArray()
        buffer = QBuffer(encoded)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly) or not image.save(
            buffer, "PNG"
        ):
            QMessageBox.warning(self, "无法粘贴图片", "图片转换为 PNG 失败。")
            return
        try:
            record = store_image_bytes(bytes(encoded))
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "无法粘贴图片", str(error))
            return
        if not any(
            item.get("sha256") == record.get("sha256")
            for item in self._attachments
        ):
            self._attachments.append(record)
        self._render_attachments()
        self._update_send_state()

    def _set_attachments(self, attachments: list[dict] | None):
        self._attachments = list(attachments or [])[:MAX_IMAGE_ATTACHMENTS]
        self._render_attachments()
        self._update_send_state()

    def _clear_attachments(self):
        self._attachments = []
        self._render_attachments()

    def _remove_attachment(self, attachment_id: str):
        self._attachments = [
            item for item in self._attachments
            if str(item.get("id", "")) != attachment_id
        ]
        self._render_attachments()
        self._update_send_state()

    def _render_attachments(self):
        while self.attachment_layout.count():
            item = self.attachment_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        for attachment in self._attachments:
            chip = QFrame()
            chip.setObjectName("attachmentChip")
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(4, 4, 4, 4)
            chip_layout.setSpacing(5)

            preview = QLabel()
            preview.setObjectName("attachmentPreview")
            preview.setFixedSize(48, 48)
            preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
            pixmap = QPixmap(str(attachment.get("path", "")))
            if not pixmap.isNull():
                preview.setPixmap(pixmap.scaled(
                    48, 48,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ))
            chip_layout.addWidget(preview)

            name = QLabel(str(attachment.get("name") or "图片"))
            name.setObjectName("attachmentName")
            name.setMaximumWidth(130)
            name.setToolTip(str(attachment.get("name") or "图片"))
            chip_layout.addWidget(name)

            remove = QPushButton("×")
            remove.setObjectName("quietIconButton")
            remove.setFixedSize(22, 22)
            attachment_id = str(attachment.get("id", ""))
            remove.clicked.connect(
                lambda _checked=False, value=attachment_id:
                self._remove_attachment(value)
            )
            chip_layout.addWidget(remove)
            self.attachment_layout.addWidget(chip)
        self.attachment_layout.addStretch(1)
        self.attachment_scroll.setVisible(bool(self._attachments))

    def _on_submit_request(self):
        if not self._current_project:
            QMessageBox.information(self, "选择项目", "请先从左侧选择一个项目。")
            return
        request = self.input_text.toPlainText().strip()
        attachments = list(self._attachments)
        if not request and not attachments:
            return
        if not request:
            request = "请分析附加图片并完成图片中表达的需求。"

        project_data = dict(self._current_project)
        project_key = self._project_key(project_data)
        running_job_id = self._running_jobs.get(project_key) or self._running_job_id
        project_starting = (
            project_key in self._starting_projects or self._job_starting
        )
        if running_job_id or project_starting:
            queued = {
                "request": request,
                "attachments": attachments,
                # Only an explicitly selected follow-up inherits the running
                # Job. Clicking "新需求" leaves this empty.
                "source_job_id": self._followup_source_job_id,
                "project": project_data,
            }
            self._queued_by_project[project_key] = queued
            self._queued_request = request
            self._queued_attachments = attachments
            self._queued_source_job_id = queued["source_job_id"]
            self.input_text.clear()
            self._clear_attachments()
            preview = request.replace("\n", " ")[:80]
            self.queue_label.setText(f"下一轮已排队：{preview}")
            self.queue_bar.show()
            source_label = running_job_id or "当前任务"
            self.followup_source_label.setText(f"继续 {source_label}")
            self.clear_followup_btn.setVisible(False)
            self.status_label.setText("下一轮需求已排队，将在当前任务结束后自动开始")
            self._update_send_state()
            return

        self.task_panel.log(f"已提交需求：{request[:100]}", "log")

        self.input_text.clear()
        self._clear_attachments()
        self.input_text.setPlaceholderText("当前任务运行中；可输入下一轮需求并排队")

        if self.engine:
            source_job_id = self._followup_source_job_id
            self._followup_source_job_id = None
            self.followup_source_label.setText("正在创建任务")
            self.clear_followup_btn.setVisible(False)
            self._starting_projects.add(project_key)
            self._job_starting = True
            asyncio.ensure_future(
                self._run_job_async(
                    request, source_job_id, attachments, project_data
                )
            )
        else:
            self.status_label.setText("执行引擎尚未连接")
        self._update_send_state()

    async def _run_job_async(self, request: str, source_job_id: str | None,
                             attachments: list[dict] | None = None,
                             project_data: dict | None = None):
        project_data = dict(project_data or self._current_project or {})
        project_key = self._project_key(project_data)
        project_name = str(project_data.get("name") or "")
        finished_job_id = None
        try:
            repos = self._get_repos()
            try:
                project = repos["project"].get_by_name(project_name)
                if not project:
                    if project_key == self._current_project_key():
                        self.bridge.log_message.emit("数据库中未找到项目", "log")
                    return

                # Create job
                result = await self.engine.create_job(
                    project_id=project.id,
                    user_request=request,
                    project_root=project.root_path,
                    source_job_id=source_job_id,
                    attachments=attachments or [],
                )
                finished_job_id = result["job_id"]
                self._running_jobs[project_key] = finished_job_id
                self._job_projects[finished_job_id] = project_key
                self._starting_projects.discard(project_key)
                if project_key == self._current_project_key():
                    self._running_job_id = finished_job_id
                    self._job_starting = False
                    self._selected_job_id = finished_job_id
                    self.pause_btn.show()
                    self.stop_btn.show()
                    self._followup_source_job_id = finished_job_id
                    self.followup_source_label.setText(
                        f"继续 {finished_job_id}"
                    )
                    self.clear_followup_btn.setVisible(True)
                    self.bridge.log_message.emit(
                        f"任务已创建：{finished_job_id}"
                        f"（分支：{result['branch']}）",
                        "log",
                    )
                    self.bridge.job_status.emit(finished_job_id, "created")
                    self._refresh_job_list()
                    self.project_panel.select_job(finished_job_id)

                # Run the full pipeline
                await self.engine.run_job(finished_job_id, project.root_path)

            finally:
                self._close_repos(repos)
        except Exception as e:
            if project_key == self._current_project_key():
                self.bridge.log_message.emit(f"错误：{str(e)}", "log")
            logger.exception("任务执行失败：%s", finished_job_id or project_name)
        finally:
            self._starting_projects.discard(project_key)
            if finished_job_id:
                if self._running_jobs.get(project_key) == finished_job_id:
                    self._running_jobs.pop(project_key, None)
                self._job_projects.pop(finished_job_id, None)
            if project_key == self._current_project_key():
                self.bridge.log_message.emit("任务结束", "log")
                self._running_job_id = self._running_jobs.get(project_key)
                self._job_starting = project_key in self._starting_projects
                self._refresh_job_list()
                if finished_job_id:
                    self.project_panel.select_job(finished_job_id)
                    self._followup_source_job_id = finished_job_id
                    self.followup_source_label.setText(
                        f"继续 {finished_job_id}"
                    )
                    self.clear_followup_btn.setVisible(True)
                else:
                    self._followup_source_job_id = None
                    self.followup_source_label.setText("新需求")
                    self.clear_followup_btn.setVisible(False)
                self.input_text.setPlaceholderText(
                    "继续描述要补充、修改或修复的内容"
                )
                self._sync_project_runtime_state()
                self._update_send_state()
            if project_key in self._queued_by_project:
                QTimer.singleShot(
                    0, lambda key=project_key: self._submit_queued_followup(key)
                )

    def _submit_queued_followup(self, project_key: str | None = None):
        project_key = project_key or self._current_project_key()
        queued = self._queued_by_project.get(project_key)
        if (
            not queued
            or project_key in self._running_jobs
            or project_key in self._starting_projects
        ):
            return
        self._queued_by_project.pop(project_key, None)
        self._starting_projects.add(project_key)
        if project_key == self._current_project_key():
            self._queued_request = None
            self._queued_source_job_id = None
            self._queued_attachments = []
            self.queue_bar.hide()
            self._job_starting = True
            self.followup_source_label.setText("正在创建任务")
        asyncio.ensure_future(self._run_job_async(
            str(queued["request"]),
            queued.get("source_job_id"),
            list(queued.get("attachments") or []),
            dict(queued["project"]),
        ))

    def _cancel_queued_request(self):
        self._queued_by_project.pop(self._current_project_key(), None)
        self._queued_request = None
        self._queued_source_job_id = None
        self._queued_attachments = []
        self.queue_bar.hide()
        self.status_label.setText("已取消排队的下一轮需求")

    def _update_send_state(self):
        has_request = bool(self.input_text.toPlainText().strip() or self._attachments)
        self.start_task_btn.setEnabled(bool(self._current_project) and has_request)
        project_busy = bool(
            self._current_project_key() in self._running_jobs
            or self._current_project_key() in self._starting_projects
            or self._running_job_id
            or self._job_starting
        )
        self.start_task_btn.setToolTip(
            "排队为下一轮需求（⌘/Ctrl + Enter）"
            if project_busy
            else "提交需求（⌘/Ctrl + Enter）"
        )

    def _on_run(self):
        running_job_id = self._selected_running_job()
        if running_job_id and self.engine:
            asyncio.ensure_future(self.engine.resume_job(running_job_id))
            self.task_panel.log("任务已恢复", "log")
            self.run_btn.hide()
            self.pause_btn.show()

    def _on_pause(self):
        running_job_id = self._selected_running_job()
        if running_job_id and self.engine:
            asyncio.ensure_future(self.engine.pause_job(running_job_id))
            self.task_panel.log("任务已暂停", "log")
            self.pause_btn.hide()
            self.run_btn.show()

    def _on_stop(self):
        running_job_id = self._selected_running_job()
        if running_job_id and self.engine:
            asyncio.ensure_future(self.engine.cancel_job(running_job_id))
            self.task_panel.log("任务已停止", "log")
            self.run_btn.hide()
            self.pause_btn.hide()
            self.stop_btn.hide()

    def _on_event(self, event_type: str, data: dict):
        """Handle events from the engine."""
        event_job_id = data.get("job_id")
        is_selected = (
            event_job_id == self._selected_job_id
            if event_job_id
            else (
                not self._current_running_job()
                or self._selected_job_id == self._current_running_job()
            )
        )
        if event_job_id and not is_selected:
            buffered = self._job_event_buffers.setdefault(event_job_id, [])
            buffered.append((event_type, dict(data)))
            if len(buffered) > 500:
                del buffered[:-500]
        if is_selected:
            self.task_panel.log_event(event_type, **data)
        repair_round = int(data.get("repair_round", 0) or 0)
        task_repair_round = self.task_panel._repair_round_from_task_id(
            data.get("task_id", "")
        )
        live_status = {
            "job_governing": "governing",
            "job_planning": "planning",
            "job_executing": "executing",
            "job_resuming_from_checkpoint": "executing",
            "job_reviewing": "reviewing",
            "job_done": "done",
            "job_failed": "failed",
            "job_needs_attention": "needs_attention",
            "job_cancelled": "cancelled",
        }.get(event_type)
        if event_job_id and live_status:
            if is_selected:
                self.task_panel.update_job_status(event_job_id, live_status)
            self.project_panel.update_job_status(event_job_id, live_status)

        if event_type == "job_governing" and is_selected:
            self.task_panel.update_stage("governor", "running", "正在分析需求目标与边界")
        elif event_type == "governor_risk_assessed" and is_selected:
            risk_name = {
                "low": "低", "medium": "中", "high": "高", "critical": "关键",
            }.get(data.get("risk_level", "medium"), "中")
            route_name = {
                "low": "直接执行",
                "medium": "策划后执行",
                "high": "完整治理与审核",
                "configured": "按项目配置",
            }.get(data.get("workflow_route", "configured"), "按项目配置")
            source_name = {
                "governor": "裁决者风险评估",
                "rules_fallback": "裁决者不可用，规则兜底",
                "fast_mode_rules": "快速模式规则评估",
            }.get(data.get("source", "governor"), "裁决者风险评估")
            self.task_panel.append_stage_output(
                "governor",
                f"{source_name}：{risk_name}风险（{data.get('risk_score', 0)} 分）"
                f"，流程：{route_name}",
            )
        elif event_type == "job_governed" and is_selected:
            if self.task_panel.stages["governor"]._status not in {"fallback", "failed"}:
                self.task_panel.update_stage("governor", "success")
        elif event_type == "job_planning" and is_selected:
            self.task_panel.update_stage(
                "planner", "running", "正在生成执行计划",
                repair_round=repair_round,
            )
        elif event_type == "plan_ready" and is_selected:
            self.task_panel.update_stage(
                "planner", "success", repair_round=repair_round
            )
            self._reload_selected_workflow()
        elif event_type == "plan_rejected" and is_selected:
            self.task_panel.update_stage(
                "planner", "rejected", "计划未通过约束检查", {"errors": data.get("errors", [])}
            )
        elif event_type == "job_executing" and is_selected:
            self.task_panel.update_stage(
                "worker", "running", "正在准备执行步骤",
                repair_round=repair_round,
            )
        elif event_type == "task_running" and is_selected:
            if not self.task_panel.has_task(data.get("task_id", "")):
                self._reload_selected_workflow()
            self.bridge.task_update.emit(data.get("task_id", ""), "running")
            self.task_panel.set_worker_progress(
                data.get("task_id", ""),
                task_index=int(data.get("task_index", 0) or 0),
                task_total=int(data.get("task_total", 0) or 0),
                phase=(
                    "正在读取并分析"
                    if data.get("task_type") in {"analysis", "review"}
                    else "正在执行"
                ),
                max_turns=int(data.get("max_turns", 0) or 0),
            )
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-task",
                event_kind="task_started",
                status="started",
                summary=f"正在执行 {data.get('title', '当前步骤')}",
                meta=(
                    f"第 {int(data.get('task_index', 1) or 1)}/"
                    f"{int(data.get('task_total', 1) or 1)} 步"
                ),
            )
            skills = data.get("skills") or []
            if skills:
                self.task_panel.append_stage_output(
                    "worker",
                    f"{data.get('task_id', '')} 已加载 Skills："
                    + "、".join(map(str, skills)),
                    repair_round=task_repair_round,
                )
        elif event_type == "task_progress" and is_selected:
            self.task_panel.set_worker_progress(
                data.get("task_id", ""),
                task_index=int(data.get("task_index", 0) or 0),
                task_total=int(data.get("task_total", 0) or 0),
                phase=str(data.get("phase") or "正在执行"),
                path=str(data.get("path") or ""),
                changes=data.get("changes") or {},
                turn=int(data.get("turn", 0) or 0),
                max_turns=int(data.get("max_turns", 0) or 0),
            )
        elif event_type in {
            "worker_tool_started", "worker_tool_completed",
        } and is_selected:
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                event_kind=(
                    "tool_started" if event_type == "worker_tool_started"
                    else "tool_completed"
                ),
                tool=str(data.get("tool") or ""),
                path=str(data.get("path") or ""),
                turn=int(data.get("turn", 0) or 0),
                status=str(data.get("status") or (
                    "started" if event_type == "worker_tool_started" else "success"
                )),
                arguments=data.get("arguments") or {},
                result=data.get("result") or {},
                duration_ms=int(data.get("duration_ms", 0) or 0),
            )
        elif event_type == "task_done" and is_selected:
            self.bridge.task_update.emit(data.get("task_id", ""), "done")
            result = data.get("result") or {}
            if result.get("no_changes"):
                self.task_panel.append_stage_output(
                    "worker", "检查完成：未发现需要修改的问题。",
                    repair_round=task_repair_round,
                )
            changes = result.get("changes") or {}
            changed = changes.get("changed") or []
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-task",
                event_kind="task_done",
                status="success",
                summary=(
                    "步骤已完成"
                    + (f"，更新了 {len(changed)} 个文件" if changed else "")
                ),
                meta=data.get("task_id", ""),
            )
            self._capture_diff(data.get("result"))
        elif event_type == "task_reclassified" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                "已自动纠正为只读分析任务：已有分析报告作为交付物，"
                "无需修改项目文件。",
                repair_round=task_repair_round,
            )
        elif event_type == "task_pending_validation" and is_selected:
            self.bridge.task_update.emit(data.get("task_id", ""), "running")
            self.task_panel.append_stage_output(
                "worker",
                "产物已生成，正在优先进行确定性验收",
                repair_round=task_repair_round,
            )
        elif event_type == "task_validation_repairing" and is_selected:
            self.bridge.task_update.emit(data.get("task_id", ""), "running")
            self.task_panel.append_stage_output(
                "worker",
                f"{data.get('task_id', '')} 验收未通过，正在自动进行一次聚焦修复",
                repair_round=task_repair_round,
            )
        elif event_type == "document_finalization_started" and is_selected:
            self.task_panel.update_stage(
                "worker", "running",
                f"{data.get('task_id', '')} 已进入收尾模式，停止重复读取",
                repair_round=task_repair_round,
            )
        elif event_type == "task_budget_compacting" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"{data.get('task_id', '')} Token 达到 70%，已压缩上下文",
                repair_round=task_repair_round,
            )
        elif event_type == "task_budget_checkpoint" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"{data.get('task_id', '')} Token 达到 85%，已保存执行进度",
                repair_round=task_repair_round,
            )
        elif event_type == "task_budget_pressure" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"{data.get('task_id', '')} Token 达到 92% 软额度，继续聚焦执行并自动扩容",
                repair_round=task_repair_round,
            )
        elif event_type in {
            "budget_auto_expanded", "task_budget_extended",
        } and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"{data.get('task_id', '流程')} 软预算已自动扩容，不增加人民币硬上限",
                repair_round=task_repair_round,
            )
        elif event_type == "task_needs_continuation" and is_selected:
            self.bridge.task_update.emit(
                data.get("task_id", ""), "interrupted"
            )
            self.task_panel.update_stage(
                "worker", "interrupted",
                "已保存有效文件或结构化进度，等待继续",
                repair_round=task_repair_round,
            )
            self.task_panel.append_stage_output(
                "worker",
                f"待继续原因：{data.get('reason', '达到用户设置的硬上限')}",
                repair_round=task_repair_round,
            )
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-task",
                event_kind="task_interrupted",
                status="interrupted",
                summary="步骤进度已保存，等待继续",
                meta=data.get("task_id", ""),
            )
        elif event_type == "task_needs_user_action" and is_selected:
            self.bridge.task_update.emit(
                data.get("task_id", ""), "needs_attention"
            )
            self.task_panel.update_stage(
                "worker", "needs_attention",
                "任务需要你完成一项外部操作",
                repair_round=task_repair_round,
            )
            self.task_panel.append_stage_output(
                "worker",
                f"需要处理：{data.get('reason', '请查看任务说明')}",
                repair_round=task_repair_round,
            )
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-task",
                event_kind="task_attention",
                status="needs_attention",
                summary=f"等待处理：{data.get('reason', '需要用户操作')}",
                meta=data.get("task_id", ""),
            )
        elif event_type == "task_failed" and is_selected:
            failure_stage = data.get("failure_stage", "")
            self.bridge.task_update.emit(
                data.get("task_id", ""),
                "model_configuration_failed"
                if failure_stage == "model_configuration" else "failed",
            )
            prefix = (
                "失败—模型配置不可用"
                if failure_stage == "model_configuration"
                else "RockCore 流程错误"
                if failure_stage in {
                    "budget", "budget_finalization", "validation",
                    "worktree_create", "git_integration",
                }
                else "错误"
            )
            detail = str(data.get("error", "未知错误"))
            if (
                failure_stage == "model_configuration"
                and detail.startswith("模型配置不可用：")
            ):
                detail = detail.removeprefix("模型配置不可用：")
            self.task_panel.append_stage_output(
                "worker", f"{prefix}：{detail}",
                repair_round=task_repair_round,
            )
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-task",
                event_kind="task_failed",
                status="failed",
                summary=f"步骤未完成：{detail[:500]}",
                meta=data.get("task_id", ""),
            )
            self._capture_diff(data.get("result"))
        elif event_type == "task_blocked" and is_selected:
            self.bridge.task_update.emit(data.get("task_id", ""), "blocked")
            blocked_by = ", ".join(data.get("blocked_by") or [])
            self.task_panel.append_stage_output(
                "worker",
                f"{data.get('task_id', '')} 因依赖任务失败而未执行"
                + (f"：{blocked_by}" if blocked_by else ""),
                repair_round=task_repair_round,
            )
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-task",
                event_kind="task_blocked",
                status="blocked",
                summary=(
                    "步骤等待依赖"
                    + (f"：{blocked_by}" if blocked_by else "")
                ),
                meta=data.get("task_id", ""),
            )
        elif event_type == "task_repairing" and is_selected:
            self.task_panel.update_stage(
                "worker", "running",
                f"{data.get('task_id', '')} 首次执行失败，正在准备修复",
                repair_round=task_repair_round,
            )
        elif event_type == "task_continuing" and is_selected:
            self.task_panel.update_stage(
                "worker", "running",
                f"{data.get('task_id', '')} 已保留部分改动，正在继续完成",
                repair_round=task_repair_round,
            )
        elif event_type == "task_replanning" and is_selected:
            self.task_panel.update_stage(
                "worker", "running",
                f"{data.get('task_id', '')} 正在重新规划修复步骤",
                repair_round=task_repair_round,
            )
        elif event_type == "task_escalating" and is_selected:
            self.task_panel.update_stage(
                "worker", "running",
                f"{data.get('task_id', '')} 已升级到紧急修复执行者",
                repair_round=task_repair_round,
            )
        elif event_type == "task_provider_fallback" and is_selected:
            self.task_panel.update_stage(
                "worker", "running",
                f"执行模型从 {data.get('from_provider', '?')} 切换到 "
                f"{data.get('to_provider', '?')}",
                repair_round=task_repair_round,
            )
        elif event_type == "task_model_fallback" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"模型 {data.get('from_model', '?')} 不可用，正在尝试 "
                f"{data.get('to_model', '?')}",
                repair_round=task_repair_round,
            )
        elif event_type == "task_model_fallback_succeeded" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"已自动降级：{data.get('from_model', '?')} → "
                f"{data.get('to_model', '?')}，任务继续执行",
                repair_round=task_repair_round,
            )
        elif event_type == "task_provider_fallback_succeeded" and is_selected:
            self.task_panel.append_stage_output(
                "worker",
                f"已自动降级到 {data.get('to_provider', '?')}，任务继续执行",
                repair_round=task_repair_round,
            )
        elif event_type == "task_refined" and is_selected:
            paths = ", ".join(data.get("allowed_paths") or [])
            self.task_panel.update_stage(
                "planner", "success",
                f"已根据前置分析更新 {data.get('task_id', '')}"
                + (f" 的目标文件：{paths}" if paths else ""),
                repair_round=task_repair_round,
            )
        elif event_type == "task_refinement_rejected" and is_selected:
            self.task_panel.update_stage(
                "planner", "rejected",
                f"{data.get('task_id', '')} 的动态路径未通过安全检查",
                {"errors": data.get("errors", [])},
                repair_round=task_repair_round,
            )
        elif event_type == "review_repair_assessing" and is_selected:
            self.task_panel.update_stage(
                "planner", "running",
                f"正在判断第 {data.get('repair_round', 1)} 轮审核问题能否自动修复",
                repair_round=repair_round,
            )
        elif event_type == "review_repair_executed" and is_selected:
            self.task_panel.update_stage(
                "worker", "success",
                f"第 {data.get('repair_round', 1)} 轮修复已执行，准备再次审核",
                repair_round=repair_round,
            )
            self._reload_selected_workflow()
        elif event_type == "review_repair_failed" and is_selected:
            self._reload_selected_workflow()
        elif event_type == "test_running" and is_selected:
            self.task_panel.set_worker_progress(
                data.get("task_id", ""), phase="正在执行验证"
            )
            self.task_panel.append_stage_output(
                "worker",
                f"正在验收：{data.get('command', '')}",
                repair_round=task_repair_round,
            )
            self.task_panel.add_worker_activity(
                data.get("task_id", ""),
                activity_id=f"{data.get('task_id', '')}-validation",
                event_kind="validation_started",
                status="started",
                summary="正在验证 " + (data.get("command") or "项目结果"),
                meta=data.get("task_id", ""),
            )
        elif event_type == "job_reviewing" and is_selected:
            self.task_panel.clear_worker_progress()
            self._reload_selected_workflow()
            self.task_panel.update_stage(
                "reviewer", "running", "正在审核执行结果",
                repair_round=repair_round,
            )
        elif event_type == "review_complete" and is_selected:
            result = data.get("result", "pass")
            self.task_panel.update_stage(
                "reviewer",
                "success" if result == "pass" else "failed" if result == "error" else "rejected",
                "审核通过" if result == "pass" else "审核执行失败" if result == "error" else "审核未通过",
                repair_round=repair_round,
            )
        elif event_type == "phase_summary" and is_selected:
            agent = data.get("agent_type", "")
            phase_status = data.get("status", "")
            stage_key = "worker" if agent in {"worker", "execution"} else agent
            self.task_panel.update_stage(
                stage_key, phase_status,
                data.get("summary", ""), data.get("details"),
                repair_round=repair_round,
            )
        elif event_type == "job_done":
            self.status_label.setText("任务完成")
            self._capture_diff()
        elif event_type == "job_finished":
            finished_job_id = data.get("job_id", "")
            fin_status = data.get("status", "")
            # job_finished is the authoritative terminal event. Keep the
            # conversation header and sidebar history on the same final state.
            self.task_panel.update_job_status(finished_job_id, fin_status)
            self.project_panel.update_job_status(finished_job_id, fin_status)
            if fin_status == "failed":
                self.status_label.setText("任务失败")
            elif fin_status == "interrupted":
                self.status_label.setText("任务已保存有效进度，待继续")
            elif fin_status == "needs_attention":
                self.status_label.setText("任务需要你的处理")
            else:
                self.status_label.setText("任务完成")
            self._capture_diff()
            if is_selected:
                self._reload_selected_workflow()
        elif event_type == "job_report_ready":
            if is_selected:
                self.task_panel.set_report_state(
                    path=str(data.get("path") or ""), available=True,
                )
        elif event_type == "job_report_failed":
            if is_selected:
                self.task_panel.set_report_state(available=True)
                self.task_panel.log(
                    f"任务报告生成失败：{data.get('error', '未知错误')}", "log",
                )
        elif event_type == "job_needs_attention":
            self.status_label.setText("任务需要你的处理")
            if is_selected:
                self.task_panel.set_attention_reason(
                    data.get("reason", "需要用户完成必要操作"),
                    data.get("recovery_hint", ""),
                )
        elif event_type == "job_resuming_from_checkpoint":
            self.status_label.setText("正在从中断位置继续任务")
            if is_selected:
                self._reload_selected_workflow()
        elif event_type == "job_failed":
            self.status_label.setText("任务失败")
            self._capture_diff()
        elif event_type == "job_cancelled":
            self.status_label.setText("任务已停止")
            self.task_panel.update_job_status(data.get("job_id", ""), "cancelled")
        if event_type == "model_chat" and is_selected:
            self.task_panel.add_model_output(
                agent_type=data.get("agent_type", "?"),
                provider=data.get("provider", "?"),
                response=data.get("response", ""),
                error=data.get("error"),
                duration_ms=data.get("duration_ms", 0),
                input_tokens=data.get("input_tokens", 0),
                cached_input_tokens=data.get("cached_input_tokens", 0),
                output_tokens=data.get("output_tokens", 0),
                task_id=data.get("task_id", ""),
                estimated_cost=data.get("estimated_cost", 0.0),
                billable_cost=data.get("billable_cost"),
                billing_mode=data.get("billing_mode", "api"),
                budget=data.get("budget"),
            )
        elif event_type == "test_result":
            self.task_panel.log_test_result(
                data.get("task_id", ""), data.get("status", "?"), data.get("output", "")
            )
            if is_selected:
                result_status = str(data.get("status") or "")
                self.task_panel.add_worker_activity(
                    data.get("task_id", ""),
                    activity_id=f"{data.get('task_id', '')}-validation",
                    event_kind="validation_completed",
                    status=(
                        "success" if result_status == "passed" else "failed"
                    ),
                    summary=(
                        "验收通过" if result_status == "passed"
                        else "验收未通过"
                    ),
                    meta=data.get("task_id", ""),
                    result={
                        "status": result_status,
                        "output": str(data.get("output") or "")[:3000],
                    },
                )
                self.task_panel.append_stage_output(
                    "worker",
                    f"验收 {data.get('status', '?')}：{data.get('output', '')[:500]}",
                )

    def _capture_diff(self, task_result: dict | None = None):
        """Show the latest task's actual changes, including committed changes."""
        if not self._current_project:
            return
        try:
            changes = (task_result or {}).get("changes", {})
            changed_files = changes.get("changed", []) if isinstance(changes, dict) else []
            lines = []
            if changed_files:
                lines.append("本次任务修改的文件：")
                lines.extend(f"  {path}" for path in changed_files)
                lines.append("")
            root = self._current_project.get("root_path", ".")
            result = run_process(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True,
                cwd=root,
            )
            if result.returncode == 0:
                commit_result = run_process(
                    [
                        "git", "log", "--reverse", "--format=%H",
                        "--fixed-strings", f"--grep=AI {self._selected_job_id}:",
                    ],
                    capture_output=True, text=True, cwd=root,
                )
                commits = [value for value in commit_result.stdout.splitlines() if value]
                if commits:
                    for commit in commits:
                        show_result = run_process(
                            ["git", "show", "--format=medium", "--stat", "--patch", commit],
                            capture_output=True, text=True, cwd=root,
                        )
                        if show_result.stdout.strip():
                            lines.append(show_result.stdout.strip())
                elif task_result:
                    diff_result = run_process(
                        ["git", "show", "--format=medium", "--stat", "--patch", "HEAD"],
                        capture_output=True, text=True, cwd=root,
                    )
                    if diff_result.stdout.strip():
                        lines.append(diff_result.stdout.strip())
                elif not changed_files:
                    lines.append("未找到这条需求关联的提交；旧记录可能没有任务标识。")
            elif not changed_files:
                lines.append("当前项目不是 Git 仓库，暂无可显示的变更快照。")
            diff = "\n".join(lines) or "(无更改)"
            self.bridge.diff_updated.emit(diff)
        except Exception as e:
            self.bridge.log_message.emit(f"获取 diff 失败：{e}", "log")

    def _reload_selected_workflow(self):
        if not self._selected_job_id:
            return
        self._on_job_selected({"job_id": self._selected_job_id})

    def _refresh_job_list(self):
        """Reload user-facing conversations for the current project."""
        if not self._current_project or not self.engine:
            return
        repos = self._get_repos()
        try:
            project = repos["project"].get_by_name(self._current_project["name"])
            if project:
                conversations = repos["conversation"].list_by_project(project.id)
                session_dicts = []
                for conversation in conversations:
                    item = repos["conversation"].aggregate(conversation)
                    item["created_at"] = as_utc_isoformat(item["created_at"])
                    item["updated_at"] = as_utc_isoformat(item["updated_at"])
                    session_dicts.append(item)
                self.project_panel.set_sessions(session_dicts)
        finally:
            self._close_repos(repos)

    def _on_job_status(self, job_id: str, status: str):
        self.status_label.setText(f"{job_id}: {status}")
        self.task_panel.update_job_status(job_id, status)

    def _poll_events(self):
        """Poll for engine events from the event bus."""
        if self.engine:
            events = self.engine.event_bus.drain_history()
            for event in events:
                self._on_event(event["type"], event["data"])

    def _open_project_config(self):
        if not self._current_project:
            QMessageBox.warning(self, "提示", "请先选择一个项目")
            return
        from .project_config_dialog import ProjectConfigDialog
        root = self._current_project.get("root_path", "")
        if not root:
            QMessageBox.warning(self, "提示", "当前项目没有设置根路径")
            return
        dialog = ProjectConfigDialog(root, self)
        dialog.exec()

    def _open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self._config = dialog.get_config()
            self.engine.apply_runtime_config(self._config)
            asyncio.ensure_future(self._reload_codex_provider())
            self.task_panel.log(
                "预算、并发、角色路由和模型设置已更新；正在重新检测 Codex 登录",
                "log",
            )

    async def _reload_codex_provider(self):
        """Apply Codex path/auth changes without requiring an app restart."""
        from providers.codex_provider import CodexProvider

        try:
            provider = await asyncio.to_thread(
                CodexProvider, dict(self._config.get("codex", {}))
            )
        except Exception as error:
            self.task_panel.log(f"Codex 重新检测失败：{error}", "error")
            return
        for route in ("codex", "governor", "reviewer"):
            self.engine.model_router.register_provider(route, provider)
        if provider.is_authenticated:
            self.task_panel.log(
                "Codex 已热重载："
                f"{provider.chatgpt_source} · {provider.codex_binary}",
                "success",
            )
        else:
            self.task_panel.log(
                "Codex 仍不可用：" + provider.chatgpt_source,
                "error",
            )

    def _switch_tab(self, name: str):
        self.task_panel.expand_detail(name)

    def _show_about(self):
        QMessageBox.about(
            self, f"关于 {PRODUCT_NAME}",
            f"{FULL_PRODUCT_NAME}\n\n"
            f"{PRODUCT_LINE}\n"
            f"{LEGAL_COMPANY_NAME}\n\n"
            "Codex SDK（裁决/审核）→ Kimi（策划）→ DeepSeek V4（执行）"
        )

    def _get_repos(self):
        from storage.database import create_session_factory
        session = create_session_factory(self.engine._engine)()
        from storage.repositories import (
            ProjectRepository, ExecutionConversationRepository, JobRepository, TaskRepository,
            ConstitutionRepository, PlanRepository, ReviewRepository,
            TestRunRepository, AgentRunRepository,
        )
        return {
            "project": ProjectRepository(session),
            "conversation": ExecutionConversationRepository(session),
            "job": JobRepository(session),
            "task": TaskRepository(session),
            "constitution": ConstitutionRepository(session),
            "plan": PlanRepository(session),
            "review": ReviewRepository(session),
            "test_run": TestRunRepository(session),
            "agent_run": AgentRunRepository(session),
            "_session": session,
        }

    @staticmethod
    def _task_usage(runs: list) -> dict:
        billable_values = [
            getattr(run, "billable_cost", None) for run in runs
        ]
        return {
            "input_tokens": sum(int(run.input_tokens or 0) for run in runs),
            "cached_input_tokens": sum(
                int(getattr(run, "cached_input_tokens", 0) or 0)
                for run in runs
            ),
            "output_tokens": sum(int(run.output_tokens or 0) for run in runs),
            "calls": len(runs),
            "cost": round(sum(float(run.cost or 0.0) for run in runs), 6),
            "billable_cost": (
                round(sum(float(value or 0.0) for value in billable_values), 6)
                if all(value is not None for value in billable_values)
                else None
            ),
        }

    @staticmethod
    def _task_worker_activities(task_id: str, runs: list) -> list[dict]:
        """Restore persisted Worker tool calls as a readable activity timeline."""
        activities = []
        for run in runs:
            if getattr(run, "agent_type", "") != "worker":
                continue
            for call in sorted(
                getattr(run, "tool_calls", None) or [],
                key=lambda item: item.created_at,
            ):
                try:
                    result = json.loads(call.result_summary or "{}")
                    if not isinstance(result, dict):
                        result = {"summary": call.result_summary or ""}
                except json.JSONDecodeError:
                    result = {"summary": call.result_summary or ""}
                arguments = dict(call.arguments or {})
                path = str(
                    result.get("path") or arguments.get("path")
                    or arguments.get("target_path") or arguments.get("query")
                    or arguments.get("pattern") or arguments.get("command") or ""
                )
                activities.append({
                    "task_id": task_id,
                    "tool": call.tool_name,
                    "path": path,
                    "status": call.status or "success",
                    "arguments": arguments,
                    "result": result,
                    "duration_ms": int(call.duration_ms or 0),
                    "created_at": as_utc_isoformat(call.created_at),
                })
        return activities

    @staticmethod
    def _job_usage(job) -> dict:
        checkpoint = getattr(job, "last_checkpoint", None) or {}
        return {
            "input_tokens": int(getattr(job, "usage_input_tokens", 0) or 0),
            "cached_input_tokens": int(
                getattr(job, "usage_cached_input_tokens", 0) or 0
            ),
            "output_tokens": int(getattr(job, "usage_output_tokens", 0) or 0),
            "calls": int(getattr(job, "usage_calls", 0) or 0),
            "cost": float(getattr(job, "usage_cost", 0.0) or 0.0),
            "billable_cost": getattr(job, "usage_billable_cost", None),
            "budget": dict(checkpoint.get("budget") or {}),
        }

    def _close_repos(self, repos):
        repos["_session"].close()

    def closeEvent(self, event):
        self._poll_timer.stop()
        if self.engine:
            asyncio.ensure_future(self.engine.stop())
        event.accept()
