"""Project AI Configuration Dialog — per-project agent and workflow settings."""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QPushButton, QComboBox, QSpinBox, QCheckBox, QTabWidget,
    QWidget, QMessageBox, QGroupBox
)
from PyQt6.QtCore import Qt

from orchestrator.agent_config import (
    ProjectAgentConfig, load_project_config, save_project_config, PROVIDER_MODELS
)


PROVIDERS = ["codex", "kimi", "deepseek"]
MODES = [
    ("auto", "自动（完整流程，按复杂度调节预算）"),
    ("fast", "快速（仅执行者，跳过裁决/策划/审核）"),
    ("standard", "标准（完整流程）"),
    ("strict", "严格（完整流程 + 自动修复）"),
    ("custom", "自定义（手动选择阶段）"),
]


class ProjectConfigDialog(QDialog):
    """Dialog for configuring project-level AI workflow settings."""

    def __init__(self, project_root: str, parent=None):
        super().__init__(parent)
        self.project_root = project_root
        self._config = load_project_config(project_root)
        self.setWindowTitle(f"项目 AI 配置 — {project_root}")
        self.setMinimumWidth(560)
        self.setMinimumHeight(480)
        self._setup_ui()
        self._load_from_config()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # ── Mode Tab ──
        mode_widget = QWidget()
        mode_layout = QVBoxLayout(mode_widget)

        mode_group = QGroupBox("工作模式")
        mode_form = QFormLayout(mode_group)
        self.mode_combo = QComboBox()
        for key, label in MODES:
            self.mode_combo.addItem(label, key)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_form.addRow("模式：", self.mode_combo)
        mode_layout.addWidget(mode_group)

        mode_layout.addStretch()
        self.tabs.addTab(mode_widget, "模式")

        # ── Agents Tab ──
        agents_widget = QWidget()
        agents_layout = QVBoxLayout(agents_widget)

        self._agent_widgets = {}
        for agent_type, label, default_provider, default_model in [
            ("governor", "裁决者 (Governor)", "codex", "codex-sdk"),
            ("planner", "策划者 (Planner)", "kimi", "kimi-k2.6"),
            ("reviewer", "审核者 (Reviewer)", "codex", "codex-sdk"),
        ]:
            gb = QGroupBox(label)
            form = QFormLayout(gb)
            enabled_cb = QCheckBox("启用")
            enabled_cb.setChecked(True)
            enabled_cb.clicked.connect(self._mark_custom_mode)
            provider_combo = QComboBox()
            model_combo = QComboBox()
            for p in PROVIDERS:
                provider_combo.addItem(p.upper(), p)
            idx = provider_combo.findData(default_provider)
            if idx >= 0:
                provider_combo.setCurrentIndex(idx)

            def _refresh_models(pc=provider_combo, mc=model_combo):
                provider = pc.currentData() or ""
                models = PROVIDER_MODELS.get(provider, [provider])
                mc.clear()
                for m in models:
                    mc.addItem(m, m)

            provider_combo.currentIndexChanged.connect(lambda _, pc=provider_combo, mc=model_combo: _refresh_models(pc, mc))
            _refresh_models(provider_combo, model_combo)

            midx = model_combo.findData(default_model)
            if midx >= 0:
                model_combo.setCurrentIndex(midx)

            form.addRow(enabled_cb)
            form.addRow("模型：", provider_combo)
            form.addRow("版本：", model_combo)
            agents_layout.addWidget(gb)
            self._agent_widgets[agent_type] = {
                "enabled": enabled_cb,
                "provider": provider_combo,
                "model": model_combo,
            }

        self.tabs.addTab(agents_widget, "智能体")

        # ── Worker Tab ──
        worker_widget = QWidget()
        worker_layout = QVBoxLayout(worker_widget)

        gb = QGroupBox("执行者 (Worker)")
        form = QFormLayout(gb)
        self.worker_provider = QComboBox()
        self.worker_model = QComboBox()
        for p in PROVIDERS:
            self.worker_provider.addItem(p.upper(), p)
        self.worker_provider.setCurrentIndex(self.worker_provider.findData("deepseek"))

        def _refresh_worker_models():
            provider = self.worker_provider.currentData() or ""
            models = PROVIDER_MODELS.get(provider, [provider])
            self.worker_model.clear()
            for m in models:
                self.worker_model.addItem(m, m)

        self.worker_provider.currentIndexChanged.connect(lambda _: _refresh_worker_models())
        _refresh_worker_models()
        self.worker_model.setCurrentIndex(self.worker_model.findData("deepseek-v4-flash"))
        form.addRow("模型：", self.worker_provider)
        form.addRow("版本：", self.worker_model)

        self.worker_max_turns = QSpinBox()
        self.worker_max_turns.setRange(4, 50)
        self.worker_max_turns.setValue(24)
        form.addRow("最大轮次：", self.worker_max_turns)

        self.worker_exploration = QSpinBox()
        self.worker_exploration.setRange(1, 20)
        self.worker_exploration.setValue(4)
        form.addRow("探索轮次上限：", self.worker_exploration)

        self.worker_retry = QSpinBox()
        self.worker_retry.setRange(1, 5)
        self.worker_retry.setValue(2)
        form.addRow("重试次数：", self.worker_retry)

        self.worker_patch_recovery = QSpinBox()
        self.worker_patch_recovery.setRange(0, 5)
        self.worker_patch_recovery.setValue(2)
        form.addRow("补丁修复轮次：", self.worker_patch_recovery)
        worker_layout.addWidget(gb)

        # Turn budget per complexity
        gb2 = QGroupBox("各复杂度轮次预算")
        form2 = QFormLayout(gb2)
        self._complexity_spins = {}
        for key, label in [("simple", "简单"), ("normal", "普通"), ("complex", "复杂")]:
            spin = QSpinBox()
            spin.setRange(4, 50)
            spin.setValue({"simple": 16, "normal": 24, "complex": 36}[key])
            form2.addRow(f"{label}：", spin)
            self._complexity_spins[key] = spin
        worker_layout.addWidget(gb2)

        worker_layout.addStretch()
        self.tabs.addTab(worker_widget, "执行者")

        # ── Features Tab ──
        feat_widget = QWidget()
        feat_layout = QVBoxLayout(feat_widget)
        self.continuation_cb = QCheckBox("启用连续任务上下文")
        self.continuation_cb.setChecked(True)
        self.continuation_cb.setToolTip("后续任务自动继承前一个任务的文件和需求上下文")
        feat_layout.addWidget(self.continuation_cb)

        self.auto_validate_cb = QCheckBox("启用自动验证")
        self.auto_validate_cb.setChecked(True)
        self.auto_validate_cb.setToolTip("任务完成后自动运行文件检查和语法验证")
        feat_layout.addWidget(self.auto_validate_cb)

        self.auto_repair_cb = QCheckBox("启用失败自动修复")
        self.auto_repair_cb.setChecked(False)
        self.auto_repair_cb.setToolTip("任务失败时自动尝试修复（会增加耗时）")
        feat_layout.addWidget(self.auto_repair_cb)

        feat_layout.addStretch()
        self.tabs.addTab(feat_widget, "特性")

        # ── Buttons ──
        btn_layout = QHBoxLayout()

        self.preset_fast = QPushButton("快速模式")
        self.preset_fast.clicked.connect(lambda: self._apply_preset(ProjectAgentConfig.fast_preset()))
        self.preset_standard = QPushButton("标准模式")
        self.preset_standard.clicked.connect(lambda: self._apply_preset(ProjectAgentConfig.standard_preset()))
        self.preset_strict = QPushButton("严格模式")
        self.preset_strict.clicked.connect(lambda: self._apply_preset(ProjectAgentConfig.strict_preset()))

        btn_layout.addWidget(self.preset_fast)
        btn_layout.addWidget(self.preset_standard)
        btn_layout.addWidget(self.preset_strict)
        btn_layout.addStretch()

        self.save_btn = QPushButton("保存配置")
        self.save_btn.setStyleSheet("QPushButton { background-color: #4CAF50; color: white; padding: 6px 16px; font-weight: bold; }")
        self.save_btn.clicked.connect(self._save)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.save_btn)
        btn_layout.addWidget(self.cancel_btn)
        layout.addLayout(btn_layout)

    def _on_mode_changed(self, _idx):
        mode = self.mode_combo.currentData()
        if mode == "auto":
            self._apply_preset(ProjectAgentConfig())
        elif mode == "fast":
            self._apply_preset(ProjectAgentConfig.fast_preset())
        elif mode == "standard":
            self._apply_preset(ProjectAgentConfig.standard_preset())
        elif mode == "strict":
            self._apply_preset(ProjectAgentConfig.strict_preset())

    def _mark_custom_mode(self):
        index = self.mode_combo.findData("custom")
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def _apply_preset(self, preset: ProjectAgentConfig):
        self._config = preset
        self._load_from_config()

    def _load_from_config(self):
        cfg = self._config
        # Mode
        idx = self.mode_combo.findData(cfg.mode)
        if idx >= 0:
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentIndex(idx)
            self.mode_combo.blockSignals(False)

        # Agents
        for agent_type in ("governor", "planner", "reviewer"):
            if agent_type in self._agent_widgets:
                profile = getattr(cfg, agent_type)
                w = self._agent_widgets[agent_type]
                w["enabled"].setChecked(profile.enabled)
                idx = w["provider"].findData(profile.provider)
                if idx >= 0:
                    w["provider"].setCurrentIndex(idx)
                midx = w["model"].findData(profile.model)
                if midx >= 0:
                    w["model"].setCurrentIndex(midx)

        # Worker
        idx = self.worker_provider.findData(cfg.worker.provider)
        if idx >= 0:
            self.worker_provider.setCurrentIndex(idx)
        midx = self.worker_model.findData(cfg.worker.model)
        if midx >= 0:
            self.worker_model.setCurrentIndex(midx)
        self.worker_max_turns.setValue(cfg.worker.max_turns or 24)
        self.worker_exploration.setValue(cfg.worker.max_exploration_turns or 4)
        self.worker_retry.setValue(cfg.worker.retry_count or 2)
        self.worker_patch_recovery.setValue(cfg.worker.patch_recovery_turns or 2)

        # Complexity
        for key in ("simple", "normal", "complex"):
            if key in self._complexity_spins:
                self._complexity_spins[key].setValue(
                    cfg.complexity_turns.get(
                        key, {"simple": 16, "normal": 24, "complex": 36}[key]
                    )
                )

        # Features
        self.continuation_cb.setChecked(cfg.continuation_context)
        self.auto_validate_cb.setChecked(cfg.auto_validation)
        self.auto_repair_cb.setChecked(cfg.auto_repair)

    def _save(self):
        cfg = self._config
        cfg.mode = self.mode_combo.currentData() or "auto"

        for agent_type in ("governor", "planner", "reviewer"):
            if agent_type in self._agent_widgets:
                profile = getattr(cfg, agent_type)
                w = self._agent_widgets[agent_type]
                profile.enabled = w["enabled"].isChecked()
                profile.provider = w["provider"].currentData() or ""
                profile.model = w["model"].currentData() or ""

        cfg.worker.provider = self.worker_provider.currentData() or "deepseek"
        cfg.worker.model = self.worker_model.currentData() or "deepseek-v4-flash"
        cfg.worker.max_turns = self.worker_max_turns.value()
        cfg.worker.max_exploration_turns = self.worker_exploration.value()
        cfg.worker.retry_count = self.worker_retry.value()
        cfg.worker.patch_recovery_turns = self.worker_patch_recovery.value()

        for key in ("simple", "normal", "complex"):
            if key in self._complexity_spins:
                cfg.complexity_turns[key] = self._complexity_spins[key].value()

        cfg.continuation_context = self.continuation_cb.isChecked()
        cfg.auto_validation = self.auto_validate_cb.isChecked()
        cfg.auto_repair = self.auto_repair_cb.isChecked()

        try:
            save_project_config(self.project_root, cfg)
            QMessageBox.information(self, "保存成功",
                f"配置已保存到 {self.project_root}/.ai/agents.json")
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
