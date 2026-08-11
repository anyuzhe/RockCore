"""Codex-style sidebar for projects and requirement history."""

from pathlib import Path

from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QMenu,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.branding import COMPANY_NAME, PRODUCT_NAME, logo_path
from app.paths import is_usable_project_dir
from app.ui.time_utils import format_local_timestamp


JOB_STATUS = {
    "done": ("#55a86b", "已完成"),
    "failed": ("#d96868", "失败"),
    "cancelled": ("#8f8f98", "已停止"),
    "interrupted": ("#d9914f", "待继续"),
    "needs_attention": ("#d9914f", "需处理"),
    "governing": ("#d4a94f", "分析中"),
    "planning": ("#d4a94f", "规划中"),
    "executing": ("#d4a94f", "执行中"),
    "reviewing": ("#d4a94f", "审核中"),
    "created": ("#8f8f98", "等待中"),
}


class ProjectDialog(QDialog):
    """Dialog for adding a local project directory."""

    def __init__(self, parent=None, project_data: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("项目" if project_data else "添加项目")
        self.setMinimumWidth(500)
        layout = QFormLayout(self)

        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("项目名称")
        layout.addRow("名称", self.name_input)

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText("本地项目目录")
        browse_btn = QPushButton("选择…")
        browse_btn.clicked.connect(self._browse)
        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_input)
        path_layout.addWidget(browse_btn)
        layout.addRow("目录", path_layout)

        self.desc_input = QTextEdit()
        self.desc_input.setPlaceholderText("项目说明（可选）")
        self.desc_input.setMaximumHeight(90)
        layout.addRow("说明", self.desc_input)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton("添加项目")
        ok_btn.setProperty("primary", True)
        ok_btn.clicked.connect(self.accept)
        actions.addWidget(cancel_btn)
        actions.addWidget(ok_btn)
        layout.addRow(actions)

        if project_data:
            self.name_input.setText(project_data.get("name", ""))
            self.path_input.setText(project_data.get("root_path", ""))
            self.desc_input.setPlainText(project_data.get("description", ""))

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择项目目录")
        if path:
            self.path_input.setText(path)
            if not self.name_input.text().strip():
                folder_name = Path(path).name
                if folder_name:
                    self.name_input.setText(folder_name)

    def get_data(self) -> dict:
        return {
            "name": self.name_input.text().strip(),
            "root_path": self.path_input.text().strip(),
            "description": self.desc_input.toPlainText().strip(),
        }


class ProjectPanel(QWidget):
    """Persistent navigation: projects at the top, requirements below."""

    project_selected = pyqtSignal(dict)
    project_deleted = pyqtSignal(str)
    job_selected = pyqtSignal(dict)
    settings_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._projects: list[dict] = []
        self._jobs: list[dict] = []
        self.setObjectName("sidebar")
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(10)

        brand_row = QHBoxLayout()
        brand_mark = QLabel()
        brand_mark.setFixedSize(28, 28)
        pixmap = QPixmap(str(logo_path()))
        if not pixmap.isNull():
            brand_mark.setPixmap(pixmap.scaled(
                24, 24, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
        else:
            brand_mark.setText("岩")
        brand_row.addWidget(brand_mark)
        brand_names = QVBoxLayout()
        brand_names.setSpacing(0)
        brand = QLabel(PRODUCT_NAME)
        brand.setObjectName("brandLabel")
        company = QLabel(COMPANY_NAME)
        company.setObjectName("brandCompanyLabel")
        brand_names.addWidget(brand)
        brand_names.addWidget(company)
        brand_row.addLayout(brand_names)
        brand_row.addStretch()
        layout.addLayout(brand_row)

        self.new_project_btn = QPushButton("＋  新项目")
        self.new_project_btn.setObjectName("newProjectButton")
        self.new_project_btn.setToolTip("添加本地项目")
        self.new_project_btn.clicked.connect(self._add_project)
        layout.addWidget(self.new_project_btn)

        project_title = QLabel("项目")
        project_title.setObjectName("sectionLabel")
        layout.addWidget(project_title)

        self.project_list = QListWidget()
        self.project_list.setObjectName("projectList")
        self.project_list.setMaximumHeight(132)
        self.project_list.currentItemChanged.connect(self._on_project_selected)
        self.project_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.project_list.customContextMenuRequested.connect(self._show_project_menu)
        layout.addWidget(self.project_list)

        jobs_header = QHBoxLayout()
        jobs_title = QLabel("需求")
        jobs_title.setObjectName("sectionLabel")
        jobs_header.addWidget(jobs_title)
        jobs_header.addStretch()
        self.history_count = QLabel("0")
        self.history_count.setObjectName("mutedLabel")
        jobs_header.addWidget(self.history_count)
        layout.addLayout(jobs_header)

        self.job_list = QListWidget()
        self.job_list.setObjectName("jobList")
        self.job_list.currentItemChanged.connect(self._on_job_selected)
        layout.addWidget(self.job_list, 1)

        self.settings_btn = QPushButton("⚙  设置")
        self.settings_btn.setObjectName("bottomSettingsButton")
        self.settings_btn.setToolTip("打开 RockCore 设置")
        self.settings_btn.clicked.connect(self.settings_requested)
        layout.addWidget(self.settings_btn)

    def set_projects(self, projects: list[dict]):
        selected_name = self.current_project_name()
        self._projects = projects
        self.project_list.blockSignals(True)
        self.project_list.clear()
        selected_row = -1
        for row, project in enumerate(projects):
            item = QListWidgetItem(project.get("name", "未命名项目"))
            item.setData(Qt.ItemDataRole.UserRole, project)
            item.setToolTip(project.get("root_path", ""))
            item.setSizeHint(QSize(0, 34))
            self.project_list.addItem(item)
            if project.get("name") == selected_name:
                selected_row = row
        self.project_list.blockSignals(False)
        self.project_list.setFixedHeight(min(132, max(42, len(projects) * 34 + 6)))
        if projects:
            self.project_list.setCurrentRow(selected_row if selected_row >= 0 else 0)

    def set_jobs(self, jobs: list[dict]):
        selected = self.current_job_id()
        self._jobs = jobs
        self.job_list.blockSignals(True)
        self.job_list.clear()
        self.history_count.setText(str(len(jobs)))
        selected_row = -1
        for row, job in enumerate(jobs):
            status = job.get("status", "created")
            _, status_text = JOB_STATUS.get(status, ("#8f8f98", status))
            request = job.get("user_request", "").replace("\n", " ").strip()
            timestamp = self._format_time(job.get("created_at", ""))
            item = QListWidgetItem(f"{request[:34] or '未命名需求'}\n{status_text}  ·  {timestamp}")
            item.setData(Qt.ItemDataRole.UserRole, job)
            item.setForeground(QColor("#34312d"))
            item.setSizeHint(QSize(0, 58))
            item.setToolTip(request)
            self.job_list.addItem(item)
            if job.get("job_id") == selected:
                selected_row = row
        self.job_list.blockSignals(False)
        if jobs:
            self.job_list.setCurrentRow(selected_row if selected_row >= 0 else 0)

    def select_job(self, job_id: str):
        for row in range(self.job_list.count()):
            item = self.job_list.item(row)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if data.get("job_id") == job_id:
                self.job_list.setCurrentRow(row)
                return

    def clear_job_selection(self):
        self.job_list.clearSelection()
        self.job_list.setCurrentRow(-1)

    def select_project(self, name: str):
        for row in range(self.project_list.count()):
            item = self.project_list.item(row)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if data.get("name") == name:
                self.project_list.setCurrentRow(row)
                return

    def current_job_id(self) -> str | None:
        item = self.job_list.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else {}
        return (data or {}).get("job_id")

    def current_project_name(self) -> str | None:
        item = self.project_list.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else {}
        return (data or {}).get("name")

    def update_job_status(self, job_id: str, status: str):
        for row in range(self.job_list.count()):
            item = self.job_list.item(row)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if data.get("job_id") != job_id:
                continue
            data = {**data, "status": status}
            _, status_text = JOB_STATUS.get(status, ("#8f8f98", status))
            request = data.get("user_request", "").replace("\n", " ").strip()
            timestamp = self._format_time(data.get("created_at", ""))
            item.setData(Qt.ItemDataRole.UserRole, data)
            item.setText(f"{request[:34] or '未命名需求'}\n{status_text}  ·  {timestamp}")
            item.setForeground(QColor("#34312d"))
            break

    def remove_project(self, name: str):
        for row in range(self.project_list.count()):
            item = self.project_list.item(row)
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if data.get("name") == name:
                self.project_list.takeItem(row)
                break

    def _on_project_selected(self, current, previous):
        if current:
            self.project_selected.emit(current.data(Qt.ItemDataRole.UserRole))

    def _on_job_selected(self, current, previous):
        if current:
            self.job_selected.emit(current.data(Qt.ItemDataRole.UserRole))

    def _add_project(self):
        dialog = ProjectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        data = dialog.get_data()
        if not data["name"] or not data["root_path"]:
            QMessageBox.warning(self, "信息不完整", "请填写项目名称并选择目录。")
            return
        if not Path(data["root_path"]).is_dir():
            QMessageBox.warning(self, "目录不可用", "项目目录不存在或不是文件夹。")
            return
        if not is_usable_project_dir(data["root_path"]):
            QMessageBox.warning(
                self,
                "目录不可写",
                "RockCore 需要修改项目文件并保存项目状态。请选择当前用户可写的"
                "项目目录，不要选择 Program Files 或 RockCore 安装目录。",
            )
            return
        self.project_selected.emit({"action": "create", **data})

    def _show_project_menu(self, position):
        item = self.project_list.itemAt(position)
        if not item:
            return
        self.project_list.setCurrentItem(item)
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        menu = QMenu(self)
        remove_action = menu.addAction("移除项目")
        remove_action.setToolTip(
            "移除 RockCore 记录并清理 .ai 状态，不删除项目源码"
        )
        selected = menu.exec(self.project_list.viewport().mapToGlobal(position))
        if selected is remove_action:
            self._delete_project(item)

    def _delete_project(self, item=None):
        target = item or self.project_list.currentItem()
        if not target:
            return
        data = target.data(Qt.ItemDataRole.UserRole) or {}
        name = data.get("name", "")
        reply = QMessageBox.question(
            self,
            "移除项目",
            f"从 RockCore 中移除“{name}”？\n"
            "项目源码不会被删除，但 RockCore 生成的 .ai 状态、记忆、配置和"
            "临时 worktree 会被清理。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.project_deleted.emit(name)

    @staticmethod
    def _format_time(value: str) -> str:
        return format_local_timestamp(
            value, fmt="%m-%d %H:%M", unknown="刚刚"
        )
