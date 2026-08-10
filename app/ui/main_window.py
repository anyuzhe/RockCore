"""Main window for the AI Engineering Studio."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
    QPushButton, QLabel, QTextEdit, QMessageBox, QFrame
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt6.QtGui import QAction, QFont, QKeySequence, QShortcut

from .project_panel import ProjectPanel
from .task_panel import TaskPanel
from .settings_dialog import SettingsDialog, load_config
from .time_utils import as_utc_isoformat
from app.branding import COMPANY_NAME, FULL_PRODUCT_NAME, LEGAL_COMPANY_NAME, PRODUCT_LINE

logger = logging.getLogger(__name__)


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
        self._followup_source_job_id = None
        self._queued_request = None
        self._queued_source_job_id = None
        self._config = load_config()
        self._setup_ui()
        self._connect_signals()
        self._setup_menu()

        # Poll timer for async engine events
        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._poll_events)
        self._poll_timer.start(200)

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

        self.input_text = QTextEdit()
        self.input_text.setObjectName("composerInput")
        self.input_text.setPlaceholderText(
            "描述你希望 RockCore 完成的工作"
        )
        self.input_text.setMinimumHeight(54)
        self.input_text.setMaximumHeight(130)
        self.input_text.textChanged.connect(self._update_send_state)
        composer_layout.addWidget(self.input_text)

        composer_actions = QHBoxLayout()
        composer_actions.setSpacing(6)
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

        self.project_panel.project_selected.connect(self._on_project_selected)
        self.project_panel.project_deleted.connect(self._on_project_deleted)

    def _on_project_selected(self, data: dict):
        if data.get("action") == "create":
            self._create_project(data)
            return

        self._current_project = data
        self._selected_job_id = None
        self._followup_source_job_id = None
        self.followup_source_label.setText("新需求")
        self.clear_followup_btn.setVisible(False)
        self._update_send_state()
        self.status_label.setText(f"项目：{data.get('name', '')} · 可以提交需求")
        self.task_panel.begin_new_request(data.get("name", ""), data.get("root_path", ""))
        self.task_panel.log(f"已选择项目：{data.get('name', '')}", "log")

        # Load job history for the selected project
        self._refresh_job_list()

    def _on_job_selected(self, data: dict):
        """Load the full persisted execution workflow for one requirement."""
        if self.engine:
            repos = self._get_repos()
            try:
                job = repos["job"].get_by_id(data.get("job_id", ""))
                if job:
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
                            "dependencies": t.dependencies or [],
                            "acceptance_command": t.acceptance_command or "",
                            "description": t.description or "",
                            "allowed_paths": t.allowed_paths or [],
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
                        "status": job.status,
                        "source_job_id": job.source_job_id,
                        "failure_code": getattr(job, "failure_code", "") or "",
                        "failure_reason": getattr(job, "failure_reason", "") or "",
                        "recovery_hint": getattr(job, "recovery_hint", "") or "",
                        "created_at": as_utc_isoformat(job.created_at),
                        "usage": self._job_usage(job),
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
                    self._followup_source_job_id = job.job_id
                    self.followup_source_label.setText(f"继续 {job.job_id}")
                    self.clear_followup_btn.setVisible(True)
                    self.input_text.setPlaceholderText("继续描述要补充、修改或修复的内容")
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

    def _clear_followup_source(self):
        self._followup_source_job_id = None
        self.followup_source_label.setText("新需求")
        self.clear_followup_btn.setVisible(False)
        self.input_text.setPlaceholderText("描述你希望 RockCore 完成的工作")
        self.status_label.setText("已切换为独立的新需求")

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
                    self.task_panel.log(f"已删除项目：{name}", "log")
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

    def _on_submit_request(self):
        if not self._current_project:
            QMessageBox.information(self, "选择项目", "请先从左侧选择一个项目。")
            return
        request = self.input_text.toPlainText().strip()
        if not request:
            return

        if self._running_job_id or self._job_starting:
            self._queued_request = request
            self._queued_source_job_id = (
                self._followup_source_job_id or self._running_job_id
            )
            self.input_text.clear()
            preview = request.replace("\n", " ")[:80]
            self.queue_label.setText(f"下一轮已排队：{preview}")
            self.queue_bar.show()
            source_label = self._running_job_id or "当前任务"
            self.followup_source_label.setText(f"继续 {source_label}")
            self.clear_followup_btn.setVisible(False)
            self.status_label.setText("下一轮需求已排队，将在当前任务结束后自动开始")
            self._update_send_state()
            return

        self.task_panel.log(f"已提交需求：{request[:100]}", "log")

        self.input_text.clear()
        self.input_text.setPlaceholderText("当前任务运行中；可输入下一轮需求并排队")

        if self.engine:
            source_job_id = self._followup_source_job_id
            self._followup_source_job_id = None
            self.followup_source_label.setText("正在创建任务")
            self.clear_followup_btn.setVisible(False)
            self._job_starting = True
            asyncio.ensure_future(self._run_job_async(request, source_job_id))
        else:
            self.status_label.setText("执行引擎尚未连接")
        self._update_send_state()

    async def _run_job_async(self, request: str, source_job_id: str | None):
        try:
            repos = self._get_repos()
            try:
                project = repos["project"].get_by_name(self._current_project["name"])
                if not project:
                    self.bridge.log_message.emit("数据库中未找到项目", "log")
                    return

                # Create job
                result = await self.engine.create_job(
                    project_id=project.id,
                    user_request=request,
                    project_root=project.root_path,
                    source_job_id=source_job_id,
                )
                self._running_job_id = result["job_id"]
                self._job_starting = False
                self._selected_job_id = result["job_id"]
                self.pause_btn.show()
                self.stop_btn.show()
                self._followup_source_job_id = result["job_id"]
                self.followup_source_label.setText(f"继续 {result['job_id']}")
                self.clear_followup_btn.setVisible(True)
                self.bridge.log_message.emit(
                    f"任务已创建：{result['job_id']}（分支：{result['branch']}）", "log"
                )
                self.bridge.job_status.emit(result["job_id"], "created")
                self._refresh_job_list()
                self.project_panel.select_job(result["job_id"])

                # Run the full pipeline
                await self.engine.run_job(result["job_id"], project.root_path)

            finally:
                self._close_repos(repos)
        except Exception as e:
            self.bridge.log_message.emit(f"错误：{str(e)}", "log")
            logger.exception("任务执行失败")
        finally:
            self.bridge.log_message.emit("任务结束", "log")
            finished_job_id = self._running_job_id
            self._refresh_job_list()
            if finished_job_id:
                self.project_panel.select_job(finished_job_id)
            self.run_btn.hide()
            self.pause_btn.hide()
            self.stop_btn.hide()
            self._running_job_id = None
            self._job_starting = False
            if finished_job_id:
                self._followup_source_job_id = finished_job_id
                self.followup_source_label.setText(f"继续 {finished_job_id}")
                self.clear_followup_btn.setVisible(True)
            else:
                self._followup_source_job_id = None
                self.followup_source_label.setText("新需求")
                self.clear_followup_btn.setVisible(False)
            self.input_text.setPlaceholderText("继续描述要补充、修改或修复的内容")
            self._update_send_state()
            if self._queued_request and finished_job_id:
                QTimer.singleShot(0, self._submit_queued_followup)
            elif self._queued_request:
                request = self._queued_request
                self._queued_request = None
                self.queue_bar.hide()
                self.input_text.setPlainText(request)

    def _submit_queued_followup(self):
        request = self._queued_request
        if not request or self._running_job_id or self._job_starting:
            return
        self._queued_request = None
        source_job_id = self._queued_source_job_id
        self._queued_source_job_id = None
        self.queue_bar.hide()
        self._followup_source_job_id = source_job_id
        if source_job_id:
            self.followup_source_label.setText(f"继续 {source_job_id}")
        self.input_text.setPlainText(request)
        self._on_submit_request()

    def _cancel_queued_request(self):
        self._queued_request = None
        self._queued_source_job_id = None
        self.queue_bar.hide()
        self.status_label.setText("已取消排队的下一轮需求")

    def _update_send_state(self):
        has_request = bool(self.input_text.toPlainText().strip())
        self.start_task_btn.setEnabled(bool(self._current_project) and has_request)
        self.start_task_btn.setToolTip(
            "排队为下一轮需求（⌘/Ctrl + Enter）"
            if self._running_job_id or self._job_starting
            else "提交需求（⌘/Ctrl + Enter）"
        )

    def _on_run(self):
        if self._running_job_id and self.engine:
            asyncio.ensure_future(self.engine.resume_job(self._running_job_id))
            self.task_panel.log("任务已恢复", "log")
            self.run_btn.hide()
            self.pause_btn.show()

    def _on_pause(self):
        if self._running_job_id and self.engine:
            asyncio.ensure_future(self.engine.pause_job(self._running_job_id))
            self.task_panel.log("任务已暂停", "log")
            self.pause_btn.hide()
            self.run_btn.show()

    def _on_stop(self):
        if self._running_job_id and self.engine:
            asyncio.ensure_future(self.engine.cancel_job(self._running_job_id))
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
                not self._running_job_id
                or self._selected_job_id == self._running_job_id
            )
        )
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
            "job_reviewing": "reviewing",
            "job_done": "done",
            "job_failed": "failed",
            "job_cancelled": "cancelled",
        }.get(event_type)
        if event_job_id and live_status:
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
        elif event_type == "task_done" and is_selected:
            self.bridge.task_update.emit(data.get("task_id", ""), "done")
            result = data.get("result") or {}
            if result.get("no_changes"):
                self.task_panel.append_stage_output(
                    "worker", "检查完成：未发现需要修改的问题。",
                    repair_round=task_repair_round,
                )
            self._capture_diff(data.get("result"))
        elif event_type == "task_failed" and is_selected:
            self.bridge.task_update.emit(data.get("task_id", ""), "failed")
            self.task_panel.append_stage_output(
                "worker", f"错误：{data.get('error', '未知错误')}",
                repair_round=task_repair_round,
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
            self.task_panel.append_stage_output(
                "worker",
                f"正在验收：{data.get('command', '')}",
                repair_round=task_repair_round,
            )
        elif event_type == "job_reviewing" and is_selected:
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
            elif fin_status in {"interrupted", "needs_attention"}:
                self.status_label.setText("任务需继续处理")
            else:
                self.status_label.setText("任务完成")
            self._capture_diff()
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
            )
        elif event_type == "test_result":
            self.task_panel.log_test_result(
                data.get("task_id", ""), data.get("status", "?"), data.get("output", "")
            )
            if is_selected:
                self.task_panel.append_stage_output(
                    "worker",
                    f"验收 {data.get('status', '?')}：{data.get('output', '')[:500]}",
                )

    def _capture_diff(self, task_result: dict | None = None):
        """Show the latest task's actual changes, including committed changes."""
        if not self._current_project:
            return
        import subprocess
        try:
            changes = (task_result or {}).get("changes", {})
            changed_files = changes.get("changed", []) if isinstance(changes, dict) else []
            lines = []
            if changed_files:
                lines.append("本次任务修改的文件：")
                lines.extend(f"  {path}" for path in changed_files)
                lines.append("")
            root = self._current_project.get("root_path", ".")
            result = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True, text=True,
                cwd=root,
            )
            if result.returncode == 0:
                commit_result = subprocess.run(
                    [
                        "git", "log", "--reverse", "--format=%H",
                        "--fixed-strings", f"--grep=AI {self._selected_job_id}:",
                    ],
                    capture_output=True, text=True, cwd=root,
                )
                commits = [value for value in commit_result.stdout.splitlines() if value]
                if commits:
                    for commit in commits:
                        show_result = subprocess.run(
                            ["git", "show", "--format=medium", "--stat", "--patch", commit],
                            capture_output=True, text=True, cwd=root,
                        )
                        if show_result.stdout.strip():
                            lines.append(show_result.stdout.strip())
                elif task_result:
                    diff_result = subprocess.run(
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
        """Reload job history for the current project."""
        if not self._current_project or not self.engine:
            return
        repos = self._get_repos()
        try:
            project = repos["project"].get_by_name(self._current_project["name"])
            if project:
                jobs = repos["job"].list_by_project(project.id)
                job_dicts = [
                    {
                        "job_id": j.job_id,
                        "user_request": j.user_request,
                        "status": j.status,
                        "source_job_id": j.source_job_id,
                        "failure_code": getattr(j, "failure_code", "") or "",
                        "failure_reason": getattr(j, "failure_reason", "") or "",
                        "recovery_hint": getattr(j, "recovery_hint", "") or "",
                        "risk_level": j.risk_level,
                        "created_at": as_utc_isoformat(j.created_at),
                    }
                    for j in jobs
                ]
                self.project_panel.set_jobs(job_dicts)
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
            self.task_panel.log(
                "预算、并发、角色路由和模型设置已更新；新密钥需要重启后加载",
                "log",
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
            ProjectRepository, JobRepository, TaskRepository,
            ConstitutionRepository, PlanRepository, ReviewRepository,
            TestRunRepository, AgentRunRepository,
        )
        return {
            "project": ProjectRepository(session),
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
    def _job_usage(job) -> dict:
        return {
            "input_tokens": int(getattr(job, "usage_input_tokens", 0) or 0),
            "cached_input_tokens": int(
                getattr(job, "usage_cached_input_tokens", 0) or 0
            ),
            "output_tokens": int(getattr(job, "usage_output_tokens", 0) or 0),
            "calls": int(getattr(job, "usage_calls", 0) or 0),
            "cost": float(getattr(job, "usage_cost", 0.0) or 0.0),
            "billable_cost": getattr(job, "usage_billable_cost", None),
        }

    def _close_repos(self, repos):
        repos["_session"].close()

    def closeEvent(self, event):
        self._poll_timer.stop()
        if self.engine:
            asyncio.ensure_future(self.engine.stop())
        event.accept()
