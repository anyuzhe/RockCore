"""Settings dialog for provider credentials, role routing, and budgets."""

import json
import os
import sys
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QLabel, QTabWidget, QWidget, QMessageBox, QSpinBox,
    QDoubleSpinBox, QComboBox, QGroupBox, QToolButton
)
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QPainter, QPainterPath, QPen

from orchestrator.agent_config import PROVIDER_MODELS
from orchestrator.cost_engine import CostEngine
from app.paths import application_dir, config_path, resolve_working_dir


CONFIG_PATH = config_path()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                return {}
            version = int(config.get("workflow_defaults_version", 1) or 1)
            if version < 2:
                provider_map = config.setdefault("agent_provider_map", {})
                provider_map.update({
                    "governor": "codex",
                    "planner": "kimi",
                    "worker": "deepseek",
                    "reviewer": "codex",
                    "emergency_coder": "codex",
                })
                kimi = config.setdefault("kimi", {})
                if kimi.get("model") in {None, "", "kimi-k2.6"}:
                    kimi["model"] = "kimi-k3"
                config["workflow_defaults_version"] = 2
            if int(config.get("pricing_currency_version", 0) or 0) < 1:
                budget = config.setdefault("budget", {})
                if "max_cost_cny" not in budget:
                    legacy_limit = budget.pop("max_cost_usd", 0.50)
                    budget["max_cost_cny"] = round(
                        float(legacy_limit or 0.50)
                        * CostEngine.LEGACY_USD_TO_CNY,
                        2,
                    )
                config["pricing_currency_version"] = 1
            return config
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return {}
    return {}


def save_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


class PasswordRevealButton(QToolButton):
    """Eye button that toggles one password field without exposing it by default."""

    def __init__(self, field: QLineEdit, parent=None):
        super().__init__(parent)
        self._field = field
        self.setObjectName("passwordRevealButton")
        self.setCheckable(True)
        self.setFixedSize(32, 32)
        self.toggled.connect(self._set_password_visible)
        self._set_password_visible(False)

    def _set_password_visible(self, visible: bool):
        self._field.setEchoMode(
            QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        )
        action = "隐藏密钥" if visible else "显示密钥"
        self.setToolTip(action)
        self.setAccessibleName(action)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().buttonText().color(), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        eye = QPainterPath()
        eye.moveTo(6, 16)
        eye.cubicTo(9, 11, 12, 9, 16, 9)
        eye.cubicTo(20, 9, 23, 11, 26, 16)
        eye.cubicTo(23, 21, 20, 23, 16, 23)
        eye.cubicTo(12, 23, 9, 21, 6, 16)
        painter.drawPath(eye)
        painter.drawEllipse(QRectF(13, 13, 6, 6))
        if not self.isChecked():
            painter.drawLine(7, 24, 25, 8)


def _password_field(field: QLineEdit) -> tuple[QWidget, PasswordRevealButton]:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(field, 1)
    reveal = PasswordRevealButton(field)
    layout.addWidget(reveal)
    return container, reveal


class SettingsDialog(QDialog):
    """Settings dialog for configuring providers, roles, and budgets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        self._config = load_config()
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # ── Kimi Tab ──
        kimi_widget = QWidget()
        kimi_layout = QFormLayout(kimi_widget)
        self.kimi_api_key = QLineEdit()
        self.kimi_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.kimi_api_key.setText(self._config.get("kimi", {}).get("api_key", ""))
        kimi_key_field, self.kimi_api_key_reveal = _password_field(self.kimi_api_key)
        kimi_layout.addRow("API 密钥：", kimi_key_field)

        self.kimi_model = QComboBox()
        self.kimi_model.setEditable(True)
        for model in PROVIDER_MODELS["kimi"]:
            self.kimi_model.addItem(model, model)
        current_kimi_model = self._config.get("kimi", {}).get("model", "kimi-k3")
        model_index = self.kimi_model.findData(current_kimi_model)
        if model_index >= 0:
            self.kimi_model.setCurrentIndex(model_index)
        else:
            self.kimi_model.setEditText(current_kimi_model)
        kimi_layout.addRow("模型：", self.kimi_model)
        self.tabs.addTab(kimi_widget, "Kimi")

        # ── DeepSeek Tab ──
        ds_widget = QWidget()
        ds_layout = QFormLayout(ds_widget)
        self.ds_api_key = QLineEdit()
        self.ds_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ds_api_key.setText(self._config.get("deepseek", {}).get("api_key", ""))
        ds_key_field, self.ds_api_key_reveal = _password_field(self.ds_api_key)
        ds_layout.addRow("API 密钥：", ds_key_field)

        self.ds_model = QComboBox()
        self.ds_model.setEditable(True)
        for model in PROVIDER_MODELS["deepseek"]:
            self.ds_model.addItem(model, model)
        current_ds_model = self._config.get("deepseek", {}).get(
            "model", "deepseek-v4-flash"
        )
        ds_model_index = self.ds_model.findData(current_ds_model)
        if ds_model_index >= 0:
            self.ds_model.setCurrentIndex(ds_model_index)
        else:
            self.ds_model.setEditText(current_ds_model)
        ds_layout.addRow("模型：", self.ds_model)
        self.tabs.addTab(ds_widget, "DeepSeek")

        # ── Codex Tab ──
        codex_widget = QWidget()
        codex_layout = QVBoxLayout(codex_widget)

        from providers.codex_provider import get_codex_auth_status

        codex_home = Path(os.path.expandvars(os.fspath(
            os.environ.get("CODEX_HOME", Path.home() / ".codex")
        ))).expanduser()
        auth_path = codex_home / "auth.json"
        configured_codex = self._config.get("codex", {})
        auth_status = get_codex_auth_status(
            auth_path,
            configured_api_key=configured_codex.get("api_key", ""),
        )

        self.codex_chatgpt_status = QLabel()
        if auth_status["chatgpt_authenticated"]:
            self.codex_chatgpt_status.setText(
                "✓ ChatGPT 登录有效 · 通过本机 codex exec 调用"
            )
            self.codex_chatgpt_status.setStyleSheet(
                "color: #4CAF50; font-weight: bold; padding: 8px;"
            )
        else:
            self.codex_chatgpt_status.setText(
                "✗ 未检测到 ChatGPT 登录 · 请运行 codex login"
            )
            self.codex_chatgpt_status.setStyleSheet(
                "color: #f44336; font-weight: bold; padding: 8px;"
            )
        codex_layout.addWidget(self.codex_chatgpt_status)

        platform_group = QGroupBox("Platform API（按 API 用量计费，可选）")
        platform_layout = QFormLayout(platform_group)
        self.codex_api_key = QLineEdit()
        self.codex_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.codex_api_key.setText(configured_codex.get("api_key", ""))
        codex_key_field, self.codex_api_key_reveal = _password_field(
            self.codex_api_key
        )
        platform_layout.addRow("OPENAI_API_KEY：", codex_key_field)
        self.codex_platform_status = QLabel(
            "已配置 · 公共 Platform API 通道"
            if auth_status["platform_api_configured"]
            else "未配置 · 不会调用公共 Platform API"
        )
        platform_layout.addRow("状态：", self.codex_platform_status)
        codex_layout.addWidget(platform_group)

        route_names = {
            "chatgpt_cli": "ChatGPT 登录 → 本机 codex exec",
            "platform_api": "OPENAI_API_KEY → Platform API",
            "unavailable": "不可用",
        }
        self.codex_active_route = QLabel(
            f"当前通道：{route_names[auth_status['authentication_mode']]}"
        )
        self.codex_active_route.setStyleSheet("font-weight: bold; padding: 4px 8px;")
        codex_layout.addWidget(self.codex_active_route)

        info_label = QLabel(
            "两种认证互相独立：ChatGPT 登录不会被当作 API Key 发送到公共接口。\n"
            f"ChatGPT：{auth_status['chatgpt_source']}\n"
            f"Platform API：{auth_status['platform_api_source']}\n"
            f"Codex 模型：{auth_status['model']}\n"
            f"Codex CLI：{auth_status['codex_binary'] or '未找到'}\n"
            f"代理：{auth_status['proxy_source']}\n"
            f"认证文件：{auth_path}\n"
            "Windows 会自动查找 PATH 和 %APPDATA%\\npm\\codex.cmd；"
            "自定义位置可设置 CODEX_BINARY。"
        )
        info_label.setStyleSheet("color: #888; padding: 4px 8px;")
        info_label.setWordWrap(True)
        codex_layout.addWidget(info_label)

        codex_layout.addStretch()
        self.tabs.addTab(codex_widget, "Codex SDK")

        # ── Role Mapping Tab (可配置角色用哪个模型) ──
        role_widget = QWidget()
        role_layout = QVBoxLayout(role_widget)

        role_info = QLabel("选择每个角色使用的模型提供者：")
        role_info.setStyleSheet("font-weight: bold; padding: 4px;")
        role_layout.addWidget(role_info)

        agent_providers = self._config.get("agent_provider_map", {})
        self._role_combos: dict[str, QComboBox] = {}

        role_form = QFormLayout()
        roles = [
            ("governor", "裁决者 (Governor)"),
            ("planner", "策划者 (Planner)"),
            ("worker", "执行者 (Worker)"),
            ("reviewer", "审核者 (Reviewer)"),
            ("emergency_coder", "紧急修复 (Emergency)"),
        ]
        for agent_type, label in roles:
            combo = QComboBox()
            combo.addItem("codex", "codex")
            combo.addItem("kimi", "kimi")
            combo.addItem("deepseek", "deepseek")
            # Set current value from config
            current = agent_providers.get(agent_type, "codex" if agent_type in ("governor", "reviewer", "emergency_coder") else "kimi" if agent_type == "planner" else "deepseek")
            idx = combo.findData(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            role_form.addRow(f"{label}：", combo)
            self._role_combos[agent_type] = combo

        role_layout.addLayout(role_form)
        role_layout.addStretch()
        self.tabs.addTab(role_widget, "角色配置")

        # ── Budget Tab (V6) ──
        budget_widget = QWidget()
        budget_layout = QFormLayout(budget_widget)

        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(10000, 10000000)
        self.max_tokens.setSingleStep(100000)
        self.max_tokens.setValue(self._config.get("budget", {}).get("max_total_tokens", 1000000))
        budget_layout.addRow("最大 Token 数：", self.max_tokens)

        self.max_api_calls = QSpinBox()
        self.max_api_calls.setRange(10, 1000)
        self.max_api_calls.setValue(self._config.get("budget", {}).get("max_api_calls", 100))
        budget_layout.addRow("最大 API 调用：", self.max_api_calls)

        self.max_cost = QDoubleSpinBox()
        self.max_cost.setRange(0.10, 10000.0)
        self.max_cost.setSingleStep(1.00)
        self.max_cost.setDecimals(2)
        self.max_cost.setPrefix("¥")
        self.max_cost.setValue(
            self._config.get("budget", {}).get(
                "max_cost_cny", CostEngine.DEFAULT_MAX_COST_CNY
            )
        )
        budget_layout.addRow("可计费 API 成本上限：", self.max_cost)

        budget_note = QLabel(
            "仅限制通过 API Key 单独计费的调用。ChatGPT 登录下的 Codex "
            "调用仍统计 Token 和人民币等价估算成本，但不会消耗此人民币预算。\n"
            "PDF/长文档任务会按源文件规模自动预留 30万/60万/100万的任务 "
            "Token，并同步扩展 Token 与调用次数安全上限；不会自动提高上面的"
            "可计费 API 成本上限。\n"
            "价格单位：人民币/百万 Token。DeepSeek Flash 缓存/输入/输出 "
            "¥0.02/¥1/¥2，Pro ¥0.025/¥3/¥6；Kimi K2.6 "
            "¥1.10/¥6.50/¥27，K2.7 Code ¥1.30/¥6.50/¥27，"
            "K3 ¥2/¥20/¥100。"
        )
        budget_note.setWordWrap(True)
        budget_note.setStyleSheet("color: #888; padding: 4px 0;")
        budget_layout.addRow("", budget_note)

        self.tabs.addTab(budget_widget, "预算")

        # ── General Tab ──
        general_widget = QWidget()
        general_layout = QFormLayout(general_widget)
        self.max_workers = QSpinBox()
        self.max_workers.setRange(1, 8)
        self.max_workers.setValue(self._config.get("max_concurrent_workers", 3))
        general_layout.addRow("最大 Worker 数：", self.max_workers)

        self.working_dir = QLineEdit(self._config.get("working_dir", ""))
        general_layout.addRow("工作目录：", self.working_dir)
        self.tabs.addTab(general_widget, "通用")

        # ── Buttons ──
        btn_layout = QHBoxLayout()
        self.save_btn = QPushButton("保存")
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addStretch()
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def _save(self):
        self._config["kimi"] = {
            "api_key": self.kimi_api_key.text().strip(),
            "model": self.kimi_model.currentText().strip() or "kimi-k3",
        }
        self._config["deepseek"] = {
            "api_key": self.ds_api_key.text().strip(),
            "model": self.ds_model.currentText().strip() or "deepseek-v4-flash",
        }
        previous_codex = self._config.get("codex", {})
        self._config["codex"] = {
            **previous_codex,
            "api_key": self.codex_api_key.text().strip(),
            "sandbox_mode": previous_codex.get("sandbox_mode", "read_only"),
        }
        self._config["max_concurrent_workers"] = self.max_workers.value()
        requested_working_dir = self.working_dir.text().strip()
        resolved_working_dir = resolve_working_dir(
            requested_working_dir,
            install_dir=(
                application_dir() if getattr(sys, "frozen", False) else None
            ),
        )
        self._config["working_dir"] = str(resolved_working_dir)
        self._config["agent_provider_map"] = {
            agent_type: combo.currentData()
            for agent_type, combo in self._role_combos.items()
        }
        self._config["budget"] = {
            "max_total_tokens": self.max_tokens.value(),
            "max_api_calls": self.max_api_calls.value(),
            "max_cost_cny": self.max_cost.value(),
        }
        self._config["workflow_defaults_version"] = 2
        self._config["pricing_currency_version"] = 1

        try:
            save_config(self._config)
        except OSError as error:
            QMessageBox.warning(self, "保存失败", f"设置文件无法写入：{error}")
            return
        if requested_working_dir != str(resolved_working_dir):
            self.working_dir.setText(str(resolved_working_dir))
            QMessageBox.information(
                self,
                "工作目录已调整",
                "原工作目录不可写或位于安装目录，已自动改为：\n"
                f"{resolved_working_dir}",
            )
        else:
            QMessageBox.information(self, "设置", "设置已保存。")
        self.accept()

    def get_config(self) -> dict:
        return self._config
