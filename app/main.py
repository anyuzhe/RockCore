#!/usr/bin/env python3
"""AI Engineering Studio — Entry Point.

Architecture: ChatGPT Plus → Codex SDK + Kimi + DeepSeek V4
  Codex SDK → Governor / Reviewer
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

# Ensure project root is on Python path
project_root = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(project_root))

from qasync import QApplication

from app.branding import COMPANY_NAME, FULL_PRODUCT_NAME, icon_path
from app.paths import (
    app_data_dir,
    application_dir,
    configure_bundled_git,
    resolve_working_dir,
)
from app.runtime import configure_runtime_logging, configure_windows_identity

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main():
    configure_runtime_logging()
    bundled_git = configure_bundled_git()
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        if bundled_git is None:
            raise RuntimeError("Windows 安装包缺少内置 Git 运行时")
        from app.subprocess_utils import run_process
        git_check = run_process(
            [str(bundled_git), "--version"], capture_output=True
        )
        if git_check.returncode != 0:
            raise RuntimeError(
                "内置 Git 无法启动："
                + (git_check.stderr.strip() or f"exit {git_check.returncode}")
            )
        logger.info("Bundled Git ready: %s", git_check.stdout.strip())
    from storage.database import init_database
    from orchestrator.engine import Engine
    from orchestrator.model_router import ModelRouter
    from orchestrator.risk_engine import RiskEngine
    from orchestrator.cost_engine import CostEngine, JobBudget
    from tools.tool_broker import ToolBroker
    from orchestrator.policy_engine import PolicyEngine
    from agents.governor import GovernorAgent
    from agents.planner import PlannerAgent
    from agents.worker import WorkerAgent
    from agents.reviewer import ReviewerAgent
    from agents.emergency_coder import EmergencyCoderAgent
    from app.ui.settings_dialog import load_config, save_config
    from memory.context_manager import ContextManager
    from skills.manager import SkillManager

    config = load_config()
    working_path = resolve_working_dir(
        config.get("working_dir"),
        install_dir=application_dir() if getattr(sys, "frozen", False) else None,
    )
    working_dir = str(working_path)
    if config.get("working_dir") != working_dir:
        # Migrate stale values such as C:\Program Files\...\RockCore from
        # pre-packaging builds before any context component writes `.ai`.
        config["working_dir"] = working_dir
        save_config(config)

    # ── Initialize V6 engines ──
    risk_engine = RiskEngine()
    cost_engine = CostEngine()

    # Configure default budget
    budget_cfg = config.get("budget", {})
    default_budget = CostEngine.budget_from_config(budget_cfg)
    cost_engine.set_default_budget(default_budget)

    engine = Engine(
        max_concurrent_workers=config.get("max_concurrent_workers", 3)
    )
    # Replace default ModelRouter with V6 smart router
    agent_provider_map = config.get("agent_provider_map", {})
    engine.model_router = ModelRouter(
        risk_engine=risk_engine,
        cost_engine=cost_engine,
        provider_map=agent_provider_map,
        event_bus=engine.event_bus,
    )
    engine.apply_runtime_config(config)
    await engine.start()

    # ── Initialize context manager (V5) ──
    context_manager = ContextManager(working_dir)
    await context_manager.initialize()

    # ── Register providers ──
    # Codex SDK: Governor + Reviewer
    from providers.codex_provider import CodexProvider
    codex_provider = CodexProvider(config.get("codex", {}))
    engine.model_router.register_provider("governor", codex_provider)
    engine.model_router.register_provider("reviewer", codex_provider)
    engine.model_router.register_provider("codex", codex_provider)
    if codex_provider.is_authenticated:
        logger.info(
            "Codex provider registered "
            f"(auth_mode={codex_provider.authentication_mode}, "
            f"provider={codex_provider.model_provider}, "
            f"wire_api={codex_provider.wire_api}, model={codex_provider.model}, "
            f"proxy={codex_provider.proxy_source})"
        )
    else:
        logger.warning(
            "Codex provider registered but neither ChatGPT login nor "
            "OPENAI_API_KEY is available"
        )

    # Kimi K3: Planner (K2.7 is reserved for provider-failure fallback)
    if config.get("kimi", {}).get("api_key"):
        from providers.kimi_provider import KimiProvider
        kimi_provider = KimiProvider(config.get("kimi", {}))
        engine.model_router.register_provider("kimi", kimi_provider)
        logger.info("Kimi Planner provider registered")
    else:
        logger.warning("No Kimi API key configured — Planner will use defaults")

    # DeepSeek V4: Worker
    if config.get("deepseek", {}).get("api_key"):
        from providers.deepseek_provider import DeepSeekProvider
        ds_provider = DeepSeekProvider(config.get("deepseek", {}))
        engine.model_router.register_provider("deepseek", ds_provider)
        logger.info("DeepSeek Worker provider registered")
    else:
        logger.warning("No DeepSeek API key configured — Worker will use defaults")

    # ── Register agents ──
    skill_manager = SkillManager(working_dir)
    engine.skill_manager = skill_manager
    tool_broker = ToolBroker(
        project_root=working_dir,
        policy_engine=engine.policy_engine,
    )
    engine.tool_broker = tool_broker

    engine.register_agent("governor", GovernorAgent(engine.model_router))
    engine.register_agent("planner", PlannerAgent(
        engine.model_router, context_manager=context_manager,
        skill_manager=skill_manager,
    ))
    engine.register_agent("worker", WorkerAgent(
        engine.model_router, tool_broker, context_manager=context_manager,
        skill_manager=skill_manager,
    ))
    engine.register_agent("reviewer", ReviewerAgent(
        engine.model_router, skill_manager=skill_manager,
    ))
    engine.register_agent("emergency_coder", EmergencyCoderAgent(engine.model_router, tool_broker))

    logger.info(
        "Agents registered: Codex(Governor/Reviewer/Emergency) + "
        "Kimi(Planner) + DeepSeek(Worker) + Skills + MCP + Memory"
    )

    if "--startup-smoke-test" in sys.argv:
        logger.info("Packaged startup smoke test passed")
        await engine.stop()
        return

    # ── Start PyQt application ──
    # qasync.run() already created the QApplication, get the existing instance
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(FULL_PRODUCT_NAME)
    app.setOrganizationName(COMPANY_NAME)
    configure_windows_identity()
    if icon_path().exists():
        from PyQt6.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path())))

    app.setStyleSheet("""
        QMainWindow, QDialog, QMessageBox, QWidget#workspace,
        QWidget#conversationContent, QScrollArea#conversationScroll,
        QScrollArea#conversationScroll > QWidget > QWidget {
            background: #fbfaf8;
            color: #25231f;
        }
        QWidget#sidebar {
            background: #f1eee8;
            border-right: 1px solid #ded9d0;
        }
        QLabel { color: #25231f; }
        QLabel#brandLabel { font-size: 17px; font-weight: 700; }
        QLabel#brandCompanyLabel { color: #b85a20; font-size: 10px; font-weight: 600; }
        QLabel#sectionLabel, QLabel#traceLabel {
            color: #7a746c; font-size: 11px; font-weight: 600;
        }
        QLabel#mutedLabel, QLabel#composerStatus, QLabel#stageSubtitle {
            color: #817b73; font-size: 11px;
        }
        QLabel#conversationTitle { font-size: 14px; font-weight: 600; }
        QLabel#jobStatus, QLabel#stageStatus { font-size: 11px; font-weight: 600; }
        QLabel#emptyTitle { font-size: 21px; font-weight: 600; }
        QLabel#emptySubtitle { color: #817b73; font-size: 13px; }
        QLabel#messageAuthor { font-size: 12px; font-weight: 600; }
        QLabel#userMessageText, QLabel#assistantSummary { font-size: 13px; }
        QLabel#assistantAvatar {
            background: #25231f; color: #ffffff; border-radius: 6px; font-weight: 700;
        }
        QFrame#conversationHeader {
            background: #fbfaf8; border-bottom: 1px solid #e2ded7;
        }
        QFrame#userMessage {
            background: #efede8; border: 1px solid #ded9d1; border-radius: 8px;
        }
        QFrame#attentionCard {
            background: #fff8e6; border: 1px solid #e2bd63; border-radius: 8px;
        }
        QLabel#attentionTitle { color: #7a5200; font-size: 13px; font-weight: 700; }
        QLabel#attentionReason { color: #4f3b12; font-size: 12px; }
        QLabel#attentionHint { color: #755d27; font-size: 12px; }
        QLabel#submittedImagePreview {
            background: #ffffff; border: 1px solid #d8d3cb; border-radius: 6px;
            color: #756f67; font-size: 10px;
        }
        QFrame#agentMessage { background: transparent; }
        QFrame#workflowStage {
            background: transparent; border-bottom: 1px solid #e2ded7;
        }
        QWidget#workerProgressWrap { background: #fbfaf8; }
        QFrame#workerProgressCard {
            background: #ffffff; border: 1px solid #dedad3; border-radius: 14px;
        }
        QLabel#workerProgressLabel { color: #5e5952; font-size: 12px; }
        QLabel#stageTitle { font-size: 12px; font-weight: 600; }
        QPlainTextEdit#stageOutput, QPlainTextEdit#detailOutput {
            background: #ffffff; color: #34312d; border: 1px solid #dedad3;
            border-radius: 6px; padding: 8px; selection-background-color: #ead9c7;
        }
        QWidget#composerWrap { background: #fbfaf8; }
        QFrame#composer {
            background: #ffffff; border: 1px solid #d7d2ca; border-radius: 8px;
        }
        QFrame#queueBar {
            background: #fff8e6; border: 1px solid #e6cf91; border-radius: 6px;
        }
        QScrollArea#attachmentScroll, QWidget#attachmentContent {
            background: transparent; border: none;
        }
        QFrame#attachmentChip {
            background: #f3f0eb; border: 1px solid #ded9d1; border-radius: 7px;
        }
        QLabel#attachmentPreview {
            background: #ffffff; border: 1px solid #e2ded7; border-radius: 5px;
        }
        QLabel#attachmentName { color: #4d4842; font-size: 11px; }
        QLabel#queueLabel { color: #7a5a10; font-size: 11px; }
        QLabel#composerContext {
            color: #665f57; background: #f0ede8; border-radius: 5px; padding: 3px 7px;
            font-size: 11px;
        }
        QTextEdit#composerInput {
            background: transparent; color: #25231f; border: none; padding: 3px;
            selection-background-color: #ead9c7;
        }
        QListWidget {
            background: transparent; color: #34312d; border: none; outline: none;
        }
        QListWidget::item { border-radius: 7px; padding: 6px 8px; }
        QListWidget::item:hover { background: #e9e5de; }
        QListWidget::item:selected { background: #e6dfd5; color: #25231f; }
        QPushButton {
            background: #ffffff; color: #34312d; border: 1px solid #d8d3cb;
            padding: 6px 10px; border-radius: 6px;
        }
        QPushButton:hover { background: #f1eee9; }
        QPushButton:pressed { background: #e7e2da; }
        QPushButton:disabled { color: #aaa49c; background: #f2f0ec; border-color: #e2ded7; }
        QPushButton#newProjectButton {
            background: #ffffff; color: #25231f; border: 1px solid #d8d3cb;
            text-align: left; padding: 8px 11px; font-weight: 600;
        }
        QPushButton#newProjectButton:hover { background: #e9e4dc; }
        QPushButton#bottomSettingsButton {
            background: transparent; color: #4b4640; border: none;
            text-align: left; padding: 8px 9px; font-weight: 500;
        }
        QPushButton#bottomSettingsButton:hover { background: #e6dfd5; }
        QPushButton#sendButton {
            background: #25231f; color: #ffffff; border: none; padding: 0;
            border-radius: 8px; font-size: 18px; font-weight: 700;
        }
        QPushButton#sendButton:hover { background: #3c3934; }
        QPushButton#sendButton:disabled { background: #d8d4cd; color: #f5f3ef; }
        QPushButton#iconButton, QPushButton#quietIconButton, QPushButton#composerToolButton {
            padding: 0; background: transparent; border: none;
        }
        QPushButton#iconButton:hover, QPushButton#quietIconButton:hover,
        QPushButton#composerToolButton:hover { background: #e6e1da; }
        QPushButton#quietButton { background: transparent; border: 1px solid #d8d3cb; }
        QPushButton#attentionResumeButton {
            background: #c87a22; color: #ffffff; border: 1px solid #b46a18;
            border-radius: 6px; padding: 7px 14px; font-weight: 600;
        }
        QPushButton#attentionResumeButton:hover { background: #b96c18; }
        QPushButton#attentionResumeButton:disabled { background: #d8c5aa; color: #ffffff; }
        QToolButton#disclosureButton, QToolButton#detailButton {
            background: transparent; color: #766f67; border: none; padding: 3px;
        }
        QToolButton#disclosureButton:hover, QToolButton#detailButton:hover {
            color: #25231f; background: #eeeae4; border-radius: 4px;
        }
        QToolButton#passwordRevealButton {
            background: transparent; color: #766f67; border: none;
            border-radius: 5px; padding: 0;
        }
        QToolButton#passwordRevealButton:hover,
        QToolButton#passwordRevealButton:checked { background: #eeeae4; color: #25231f; }
        QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
            background: #ffffff; color: #25231f; border: 1px solid #d6d1c9;
            border-radius: 6px; padding: 6px; selection-background-color: #ead9c7;
        }
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
        QSpinBox:focus, QDoubleSpinBox:focus { border: 1px solid #b85a20; }
        QComboBox QAbstractItemView {
            background: #ffffff; color: #25231f; border: 1px solid #d6d1c9;
            selection-background-color: #e6dfd5; selection-color: #25231f;
        }
        QCheckBox { color: #34312d; spacing: 7px; }
        QGroupBox {
            color: #34312d; background: #ffffff; border: 1px solid #ddd8d0;
            border-radius: 7px; margin-top: 12px; padding: 12px 8px 8px 8px;
            font-weight: 600;
        }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
        QTabWidget::pane {
            background: #ffffff; border: 1px solid #ddd8d0; border-radius: 7px;
        }
        QTabBar::tab {
            background: transparent; color: #746e66; border: none;
            padding: 8px 12px; margin-right: 2px;
        }
        QTabBar::tab:hover { color: #25231f; background: #eeeae4; }
        QTabBar::tab:selected {
            color: #b84f13; background: #ffffff; border-bottom: 2px solid #c45112;
            font-weight: 600;
        }
        QSplitter::handle { background: #ded9d0; }
        QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
        QScrollBar::handle:vertical { background: #c8c2b9; min-height: 28px; border-radius: 4px; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QMenuBar { background: #f1eee8; color: #34312d; }
        QMenuBar::item:selected, QMenu::item:selected {
            background: #e6dfd5; color: #25231f;
        }
        QMenu {
            background: #ffffff; color: #34312d; border: 1px solid #d8d3cb;
            padding: 5px;
        }
        QMenu::item { padding: 6px 24px 6px 10px; border-radius: 5px; }
        QToolTip { background: #25231f; color: #ffffff; border: none; padding: 5px; }
    """)

    from app.ui.main_window import MainWindow
    window = MainWindow(engine)
    window.show()

    repos = engine._get_repos()
    try:
        projects = repos["project"].list_all()
        window.bridge.projects_loaded.emit([
            {"name": p.name, "root_path": p.root_path,
             "description": p.description}
            for p in projects
        ])
    finally:
        repos["_session"].close()

    # qasync already runs the event loop — keep alive until window closes
    exit_event = asyncio.Event()
    app.aboutToQuit.connect(exit_event.set)
    await exit_event.wait()
    await engine.stop()


if __name__ == "__main__":
    # Required for the isolated Python acceptance runner in PyInstaller builds.
    # It must run before qasync starts the desktop event loop.
    import multiprocessing
    multiprocessing.freeze_support()
    import qasync
    try:
        qasync.run(main())
    except Exception as error:
        logger.exception("RockCore startup failed")
        try:
            from PyQt6.QtWidgets import QApplication as QtApplication, QMessageBox

            app = QtApplication.instance() or QtApplication(sys.argv)
            log_path = app_data_dir() / "rockcore.log"
            QMessageBox.critical(
                None,
                "RockCore 启动失败",
                "RockCore 无法完成启动。程序不会向安装目录写入数据。\n\n"
                f"原因：{error}\n\n详细日志：{log_path}",
            )
        except Exception:
            pass
        raise SystemExit(1)
