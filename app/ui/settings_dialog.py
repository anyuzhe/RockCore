"""Settings dialog for API keys, model config, budgets, and scoring."""

import json
import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit,
    QPushButton, QLabel, QTabWidget, QWidget, QMessageBox, QSpinBox,
    QDoubleSpinBox, QTextEdit, QComboBox, QGroupBox
)
from PyQt6.QtCore import Qt

from orchestrator.agent_config import PROVIDER_MODELS


CONFIG_PATH = Path.home() / ".ai_engineering_studio" / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


class SettingsDialog(QDialog):
    """Settings dialog for configuring API keys, budgets, and model scoring."""

    def __init__(self, parent=None, model_scoring=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(600)
        self.setMinimumHeight(500)
        self._config = load_config()
        self._model_scoring = model_scoring
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
        kimi_layout.addRow("API 密钥：", self.kimi_api_key)

        self.kimi_model = QComboBox()
        self.kimi_model.setEditable(True)
        for model in PROVIDER_MODELS["kimi"]:
            self.kimi_model.addItem(model, model)
        current_kimi_model = self._config.get("kimi", {}).get("model", "kimi-k2.6")
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
        ds_layout.addRow("API 密钥：", self.ds_api_key)

        self.ds_model = QLineEdit(
            self._config.get("deepseek", {}).get("model", "deepseek-v4-flash")
        )
        ds_layout.addRow("模型：", self.ds_model)
        self.tabs.addTab(ds_widget, "DeepSeek V4 Flash")

        # ── Codex Tab ──
        codex_widget = QWidget()
        codex_layout = QVBoxLayout(codex_widget)

        from providers.codex_provider import get_codex_auth_status

        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        auth_path = codex_home / "auth.json"
        auth_status = get_codex_auth_status(auth_path)

        status_label = QLabel()
        if auth_status["authenticated"]:
            status_label.setText("✓ 已读取 Codex 本地登录凭据")
            status_label.setStyleSheet("color: #4CAF50; font-weight: bold; padding: 8px;")
        else:
            status_label.setText("✗ 未找到可用的 Codex 登录凭据")
            status_label.setStyleSheet("color: #f44336; font-weight: bold; padding: 8px;")
        codex_layout.addWidget(status_label)

        info_label = QLabel(
            "RockCore 会自动使用本地 Codex 登录信息，无需在这里重复填写密钥。\n"
            f"认证来源：{auth_status['source']}\n"
            f"提供商：{auth_status['provider']} · {auth_status['wire_api']}\n"
            f"模型：{auth_status['model']}\n"
            f"连接：{auth_status['base_url']}\n"
            f"代理：{auth_status['proxy_source']}\n"
            f"配置文件：{auth_path}"
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
        self.max_cost.setRange(0.01, 100.0)
        self.max_cost.setSingleStep(0.10)
        self.max_cost.setDecimals(2)
        self.max_cost.setPrefix("$")
        self.max_cost.setValue(self._config.get("budget", {}).get("max_cost_usd", 0.50))
        budget_layout.addRow("最大成本：", self.max_cost)

        self.tabs.addTab(budget_widget, "预算")

        # ── Model Scoring Tab (V6) ──
        scoring_widget = QWidget()
        scoring_layout = QVBoxLayout(scoring_widget)

        self.scoring_text = QTextEdit()
        self.scoring_text.setReadOnly(True)
        self.scoring_text.setStyleSheet("font-family: monospace; font-size: 11px;")
        scoring_layout.addWidget(self.scoring_text)

        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self._refresh_scoring)
        scoring_layout.addWidget(refresh_btn)

        self.tabs.addTab(scoring_widget, "模型评分")
        self._refresh_scoring()

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

    def _refresh_scoring(self):
        if self._model_scoring:
            self.scoring_text.setPlainText(self._model_scoring.get_summary())
        else:
            self.scoring_text.setPlainText("模型评分模块未初始化")

    def _save(self):
        self._config["kimi"] = {
            "api_key": self.kimi_api_key.text().strip(),
            "model": self.kimi_model.currentText().strip() or "kimi-k2.6",
        }
        self._config["deepseek"] = {
            "api_key": self.ds_api_key.text().strip(),
            "model": self.ds_model.text().strip() or "deepseek-v4-flash",
        }
        self._config["codex"] = {
            "sandbox_mode": "read_only",
        }
        self._config["max_concurrent_workers"] = self.max_workers.value()
        self._config["working_dir"] = self.working_dir.text().strip()
        self._config["agent_provider_map"] = {
            agent_type: combo.currentData()
            for agent_type, combo in self._role_combos.items()
        }
        self._config["budget"] = {
            "max_total_tokens": self.max_tokens.value(),
            "max_api_calls": self.max_api_calls.value(),
            "max_cost_usd": self.max_cost.value(),
        }

        save_config(self._config)
        QMessageBox.information(self, "设置", "设置已保存。")
        self.accept()

    def get_config(self) -> dict:
        return self._config
