"""Project AI Configuration Dialog — Agents, Skills, MCP, and workflow."""

import json
from dataclasses import asdict

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QPushButton, QComboBox, QSpinBox, QCheckBox, QTabWidget,
    QWidget, QMessageBox, QGroupBox, QListWidget, QListWidgetItem,
    QPlainTextEdit,
)
from PyQt6.QtCore import Qt

from orchestrator.agent_config import (
    ProjectAgentConfig, load_project_config, save_project_config,
    PROVIDER_MODELS, PROVIDER_REASONING_LEVELS, BUILTIN_SKILLS,
    SkillConfig, MCPConfig,
)
from mcp_runtime.trust import approve_project_mcp, revoke_project_mcp
from skills.trust import approve_project_skills, revoke_project_skills


PROVIDERS = ["codex", "kimi", "deepseek"]
MODES = [
    ("auto", "自动（裁决者评估后按低/中/高风险路由）"),
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
        self.setMinimumHeight(600)
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
        for agent_type, label, default_provider, default_model, default_effort in [
            ("governor", "裁决者 (Governor)", "codex", "gpt-5.6-sol", "high"),
            ("planner", "策划者 (Planner)", "kimi", "kimi-k3", "default"),
            ("reviewer", "审核者 (Reviewer)", "codex", "gpt-5.6-sol", "high"),
            ("emergency_coder", "紧急修复 (Emergency)", "codex", "gpt-5.6-sol", "max"),
        ]:
            gb = QGroupBox(label)
            form = QFormLayout(gb)
            enabled_cb = QCheckBox("启用")
            enabled_cb.setChecked(True)
            enabled_cb.clicked.connect(self._mark_custom_mode)
            provider_combo = QComboBox()
            model_combo = QComboBox()
            reasoning_combo = QComboBox()
            for p in PROVIDERS:
                provider_combo.addItem(p.upper(), p)
            idx = provider_combo.findData(default_provider)
            if idx >= 0:
                provider_combo.setCurrentIndex(idx)

            def _refresh_models(
                pc=provider_combo, mc=model_combo, rc=reasoning_combo
            ):
                provider = pc.currentData() or ""
                models = PROVIDER_MODELS.get(provider, [provider])
                mc.clear()
                for m in models:
                    mc.addItem(m, m)
                rc.clear()
                for effort in PROVIDER_REASONING_LEVELS.get(provider, ["default"]):
                    rc.addItem(
                        "供应商默认" if effort == "default" else effort.upper(),
                        effort,
                    )

            provider_combo.currentIndexChanged.connect(
                lambda _, pc=provider_combo, mc=model_combo, rc=reasoning_combo:
                _refresh_models(pc, mc, rc)
            )
            _refresh_models(provider_combo, model_combo)

            midx = model_combo.findData(default_model)
            if midx >= 0:
                model_combo.setCurrentIndex(midx)
            ridx = reasoning_combo.findData(default_effort)
            if ridx >= 0:
                reasoning_combo.setCurrentIndex(ridx)

            form.addRow(enabled_cb)
            form.addRow("模型：", provider_combo)
            form.addRow("版本：", model_combo)
            form.addRow("推理强度：", reasoning_combo)
            agents_layout.addWidget(gb)
            self._agent_widgets[agent_type] = {
                "enabled": enabled_cb,
                "provider": provider_combo,
                "model": model_combo,
                "reasoning": reasoning_combo,
            }

        self.tabs.addTab(agents_widget, "智能体")

        # ── Worker Tab ──
        worker_widget = QWidget()
        worker_layout = QVBoxLayout(worker_widget)

        gb = QGroupBox("执行者 (Worker)")
        form = QFormLayout(gb)
        self.worker_provider = QComboBox()
        self.worker_model = QComboBox()
        self.worker_reasoning = QComboBox()
        for p in PROVIDERS:
            self.worker_provider.addItem(p.upper(), p)
        self.worker_provider.setCurrentIndex(self.worker_provider.findData("deepseek"))

        def _refresh_worker_models():
            provider = self.worker_provider.currentData() or ""
            models = PROVIDER_MODELS.get(provider, [provider])
            self.worker_model.clear()
            for m in models:
                self.worker_model.addItem(m, m)
            self.worker_reasoning.clear()
            for effort in PROVIDER_REASONING_LEVELS.get(provider, ["default"]):
                self.worker_reasoning.addItem(
                    "供应商默认" if effort == "default" else effort.upper(),
                    effort,
                )

        self.worker_provider.currentIndexChanged.connect(lambda _: _refresh_worker_models())
        _refresh_worker_models()
        self.worker_model.setCurrentIndex(self.worker_model.findData("deepseek-v4-flash"))
        form.addRow("模型：", self.worker_provider)
        form.addRow("版本：", self.worker_model)
        form.addRow("推理强度：", self.worker_reasoning)

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
        form.addRow("同模型重试次数：", self.worker_retry)

        self.worker_emergency_after = QSpinBox()
        self.worker_emergency_after.setRange(1, 6)
        self.worker_emergency_after.setValue(3)
        self.worker_emergency_after.setToolTip(
            "执行者连续失败达到该次数后，升级到 Emergency"
        )
        form.addRow("Emergency 前失败次数：", self.worker_emergency_after)

        self.worker_fallback_provider = QComboBox()
        self.worker_fallback_model = QComboBox()
        for provider in PROVIDERS:
            self.worker_fallback_provider.addItem(provider.upper(), provider)
        self.worker_fallback_provider.setCurrentIndex(
            self.worker_fallback_provider.findData("kimi")
        )

        def _refresh_fallback_models():
            provider = self.worker_fallback_provider.currentData() or ""
            self.worker_fallback_model.clear()
            for model in PROVIDER_MODELS.get(provider, [provider]):
                self.worker_fallback_model.addItem(model, model)

        self.worker_fallback_provider.currentIndexChanged.connect(
            lambda _: _refresh_fallback_models()
        )
        _refresh_fallback_models()
        fallback_index = self.worker_fallback_model.findData("kimi-k2.7")
        if fallback_index >= 0:
            self.worker_fallback_model.setCurrentIndex(fallback_index)
        form.addRow("供应商异常备用：", self.worker_fallback_provider)
        form.addRow("备用模型：", self.worker_fallback_model)

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

        # ── Skills Tab ──
        skills_widget = QWidget()
        skills_layout = QVBoxLayout(skills_widget)
        self.skills_enabled_cb = QCheckBox("启用 Skills 按需加载")
        self.skills_enabled_cb.setChecked(True)
        self.skills_enabled_cb.setToolTip(
            "只把当前任务命中的 Skill 正文加入上下文，未命中的仅保留元数据"
        )
        skills_layout.addWidget(self.skills_enabled_cb)

        self.project_skills_cb = QCheckBox(
            "信任并加载项目自定义 .ai/skills"
        )
        self.project_skills_cb.setChecked(True)
        skills_layout.addWidget(self.project_skills_cb)

        skills_form = QFormLayout()
        self.max_skills_spin = QSpinBox()
        self.max_skills_spin.setRange(1, 8)
        self.max_skills_spin.setValue(3)
        self.max_skills_spin.setToolTip("限制单个任务注入的 Skill 数量，控制上下文大小")
        skills_form.addRow("单任务最多加载：", self.max_skills_spin)
        skills_layout.addLayout(skills_form)

        skills_layout.addWidget(QLabel("内置 Skills："))
        self.skill_list = QListWidget()
        self._skill_items = {}
        for name in BUILTIN_SKILLS:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.skill_list.addItem(item)
            self._skill_items[name] = item
        skills_layout.addWidget(self.skill_list)
        skill_hint = QLabel(
            "项目 Skill 目录：.ai/skills/<skill-name>/SKILL.md\n"
            "SKILL.md 仅使用 name、description 元数据，正文只在命中后加载。"
            "保存会把当前 Skill 内容指纹批准到项目外；文件变化后需重新确认。"
        )
        skill_hint.setWordWrap(True)
        skills_layout.addWidget(skill_hint)
        self.tabs.addTab(skills_widget, "Skills")

        # ── MCP Tab ──
        mcp_widget = QWidget()
        mcp_layout = QVBoxLayout(mcp_widget)
        self.mcp_enabled_cb = QCheckBox("启用 MCP 外部工具")
        self.mcp_enabled_cb.setChecked(False)
        self.mcp_enabled_cb.setToolTip(
            "MCP 连接失败不会禁用本地文件、Git、Shell 和测试工具"
        )
        mcp_layout.addWidget(self.mcp_enabled_cb)
        mcp_layout.addWidget(QLabel("MCP stdio 服务（JSON 数组）："))
        self.mcp_servers_text = QPlainTextEdit()
        self.mcp_servers_text.setPlaceholderText(
            '[\n  {\n    "name": "github",\n'
            '    "command": "npx",\n    "args": ["-y", "server-package"],\n'
            '    "env": {"TOKEN": "${GITHUB_TOKEN}"},\n'
            '    "read_only": true,\n    "allow_tools": ["*"]\n  }\n]'
        )
        mcp_layout.addWidget(self.mcp_servers_text)
        mcp_hint = QLabel(
            "安全规则：不通过 shell 启动；密钥请写 ${环境变量名}；"
            "read_only=true 时会隐藏写入型工具。工具统一显示为 "
            "mcp__服务__工具。启用并保存表示信任这些本地启动命令；"
            "审批记录保存在项目目录之外，仓库不能自行授权。"
        )
        mcp_hint.setWordWrap(True)
        mcp_layout.addWidget(mcp_hint)
        self.tabs.addTab(mcp_widget, "MCP")

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
        self.auto_repair_cb.setChecked(True)
        self.auto_repair_cb.setToolTip(
            "审核未通过时由策划者判断可修复性，并自动重新策划、执行和审核"
        )
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
        for agent_type in (
            "governor", "planner", "reviewer", "emergency_coder"
        ):
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
                ridx = w["reasoning"].findData(profile.reasoning_effort)
                if ridx >= 0:
                    w["reasoning"].setCurrentIndex(ridx)

        # Worker
        idx = self.worker_provider.findData(cfg.worker.provider)
        if idx >= 0:
            self.worker_provider.setCurrentIndex(idx)
        midx = self.worker_model.findData(cfg.worker.model)
        if midx >= 0:
            self.worker_model.setCurrentIndex(midx)
        ridx = self.worker_reasoning.findData(cfg.worker.reasoning_effort)
        if ridx >= 0:
            self.worker_reasoning.setCurrentIndex(ridx)
        self.worker_max_turns.setValue(cfg.worker.max_turns or 24)
        self.worker_exploration.setValue(cfg.worker.max_exploration_turns or 4)
        self.worker_retry.setValue(cfg.worker.retry_count or 2)
        self.worker_emergency_after.setValue(
            cfg.worker.emergency_after_failures or 3
        )
        fallback_provider_index = self.worker_fallback_provider.findData(
            cfg.worker.fallback_provider
        )
        if fallback_provider_index >= 0:
            self.worker_fallback_provider.setCurrentIndex(
                fallback_provider_index
            )
        fallback_model_index = self.worker_fallback_model.findData(
            cfg.worker.fallback_model
        )
        if fallback_model_index >= 0:
            self.worker_fallback_model.setCurrentIndex(fallback_model_index)
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

        # Skills
        self.skills_enabled_cb.setChecked(cfg.skills.enabled)
        self.project_skills_cb.setChecked(cfg.skills.allow_project_skills)
        self.max_skills_spin.setValue(cfg.skills.max_selected)
        enabled_builtin = set(cfg.skills.enabled_builtin)
        for name, item in self._skill_items.items():
            item.setCheckState(
                Qt.CheckState.Checked
                if name in enabled_builtin else Qt.CheckState.Unchecked
            )

        # MCP
        self.mcp_enabled_cb.setChecked(cfg.mcp.enabled)
        self.mcp_servers_text.setPlainText(json.dumps(
            [asdict(server) for server in cfg.mcp.servers],
            ensure_ascii=False, indent=2,
        ))

    def _save(self):
        cfg = self._config
        try:
            raw_mcp = self.mcp_servers_text.toPlainText().strip() or "[]"
            servers = json.loads(raw_mcp)
            cfg.mcp = MCPConfig.from_dict({
                "enabled": self.mcp_enabled_cb.isChecked(),
                "servers": servers,
            })
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            QMessageBox.warning(self, "MCP 配置无效", str(error))
            return
        cfg.mode = self.mode_combo.currentData() or "auto"

        for agent_type in (
            "governor", "planner", "reviewer", "emergency_coder"
        ):
            if agent_type in self._agent_widgets:
                profile = getattr(cfg, agent_type)
                w = self._agent_widgets[agent_type]
                profile.enabled = w["enabled"].isChecked()
                profile.provider = w["provider"].currentData() or ""
                profile.model = w["model"].currentData() or ""
                profile.reasoning_effort = (
                    w["reasoning"].currentData() or "default"
                )

        cfg.worker.provider = self.worker_provider.currentData() or "deepseek"
        cfg.worker.model = self.worker_model.currentData() or "deepseek-v4-flash"
        cfg.worker.reasoning_effort = (
            self.worker_reasoning.currentData() or "default"
        )
        cfg.worker.max_turns = self.worker_max_turns.value()
        cfg.worker.max_exploration_turns = self.worker_exploration.value()
        cfg.worker.retry_count = self.worker_retry.value()
        cfg.worker.emergency_after_failures = self.worker_emergency_after.value()
        cfg.worker.fallback_provider = (
            self.worker_fallback_provider.currentData() or "kimi"
        )
        cfg.worker.fallback_model = (
            self.worker_fallback_model.currentData() or "kimi-k2.7"
        )
        cfg.worker.patch_recovery_turns = self.worker_patch_recovery.value()

        for key in ("simple", "normal", "complex"):
            if key in self._complexity_spins:
                cfg.complexity_turns[key] = self._complexity_spins[key].value()

        cfg.continuation_context = self.continuation_cb.isChecked()
        cfg.auto_validation = self.auto_validate_cb.isChecked()
        cfg.auto_repair = self.auto_repair_cb.isChecked()
        cfg.skills = SkillConfig(
            enabled=self.skills_enabled_cb.isChecked(),
            enabled_builtin=[
                name for name, item in self._skill_items.items()
                if item.checkState() == Qt.CheckState.Checked
            ],
            allow_project_skills=self.project_skills_cb.isChecked(),
            max_selected=self.max_skills_spin.value(),
        )

        try:
            save_project_config(self.project_root, cfg)
            if cfg.mcp.enabled:
                approve_project_mcp(self.project_root, cfg.mcp)
            else:
                revoke_project_mcp(self.project_root)
            if cfg.skills.allow_project_skills:
                approve_project_skills(self.project_root, cfg.skills)
            else:
                revoke_project_skills(self.project_root)
            QMessageBox.information(self, "保存成功",
                f"配置已保存到 {self.project_root}/.ai/agents.json")
            self.accept()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
