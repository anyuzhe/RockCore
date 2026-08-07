"""Project-level Agent Configuration — persisted per project in .ai/agents.json."""

import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Available model versions per provider
PROVIDER_MODELS = {
    "kimi": ["kimi-k3", "kimi-k2.6", "kimi-k2.5", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "deepseek": ["deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
    "codex": ["codex-sdk"],
}


@dataclass
class AgentProfile:
    enabled: bool = True
    provider: str = ""
    model: str = ""
    max_turns: int = 0
    retry_count: int = 1


@dataclass
class WorkerProfile(AgentProfile):
    max_exploration_turns: int = 4
    patch_recovery_turns: int = 2


@dataclass
class ProjectAgentConfig:
    """Per-project AI workflow configuration. Persisted to .ai/agents.json."""

    # ── Mode ──
    # Auto keeps the full workflow and only adjusts Worker budgets by complexity.
    mode: str = "auto"  # "auto" | "fast" | "standard" | "strict" | "custom"

    # ── Agent profiles ──
    governor: AgentProfile = field(default_factory=lambda: AgentProfile(enabled=True, provider="codex", model="codex-sdk"))
    planner: AgentProfile = field(default_factory=lambda: AgentProfile(enabled=True, provider="kimi", model="kimi-k2.6", max_turns=8))
    worker: WorkerProfile = field(default_factory=lambda: WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-flash", max_turns=20, max_exploration_turns=4, patch_recovery_turns=2, retry_count=2))
    reviewer: AgentProfile = field(default_factory=lambda: AgentProfile(enabled=True, provider="codex", model="codex-sdk"))

    # ── Per-complexity turn overrides ──
    complexity_turns: dict[str, int] = field(default_factory=lambda: {
        "simple": 10,
        "normal": 20,
        "complex": 30,
    })
    complexity_exploration: dict[str, int] = field(default_factory=lambda: {
        "simple": 3,
        "normal": 6,
        "complex": 8,
    })

    # ── Features ──
    continuation_context: bool = True
    auto_validation: bool = True
    auto_repair: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectAgentConfig":
        cfg = cls()
        if "mode" in data:
            cfg.mode = data["mode"]
        if "governor" in data:
            cfg.governor = AgentProfile(**data["governor"])
        if "planner" in data:
            cfg.planner = AgentProfile(**data["planner"])
        if "worker" in data:
            cfg.worker = WorkerProfile(**data["worker"])
        if "reviewer" in data:
            cfg.reviewer = AgentProfile(**data["reviewer"])
        if "complexity_turns" in data:
            cfg.complexity_turns.update(data["complexity_turns"])
        if "complexity_exploration" in data:
            cfg.complexity_exploration.update(data["complexity_exploration"])
        for k in ("continuation_context", "auto_validation", "auto_repair"):
            if k in data:
                setattr(cfg, k, data[k])
        return cfg

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "governor": asdict(self.governor),
            "planner": asdict(self.planner),
            "worker": asdict(self.worker),
            "reviewer": asdict(self.reviewer),
            "complexity_turns": self.complexity_turns,
            "complexity_exploration": self.complexity_exploration,
            "continuation_context": self.continuation_context,
            "auto_validation": self.auto_validation,
            "auto_repair": self.auto_repair,
        }

    def get_worker_turns(self, complexity: str) -> int:
        return self.complexity_turns.get(complexity, self.worker.max_turns or 20)

    def get_exploration_turns(self, complexity: str) -> int:
        return self.complexity_exploration.get(complexity,
                                                self.worker.max_exploration_turns or 4)

    # ── Presets ──

    @classmethod
    def fast_preset(cls) -> "ProjectAgentConfig":
        """Fast mode: Worker only, no Governor/Planner/Reviewer."""
        return cls(
            mode="fast",
            governor=AgentProfile(enabled=False, provider="codex", model="codex-sdk"),
            planner=AgentProfile(enabled=False, provider="kimi", model="kimi-k2.6", max_turns=0),
            worker=WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-flash", max_turns=10, max_exploration_turns=3, retry_count=1),
            reviewer=AgentProfile(enabled=False, provider="codex", model="codex-sdk"),
            complexity_turns={"simple": 8, "normal": 10, "complex": 12},
            continuation_context=True,
        )

    @classmethod
    def standard_preset(cls) -> "ProjectAgentConfig":
        """Standard mode: the complete workflow with moderate budgets."""
        return cls(
            mode="standard",
            governor=AgentProfile(enabled=True, provider="codex", model="codex-sdk"),
            planner=AgentProfile(enabled=True, provider="kimi", model="kimi-k2.6", max_turns=8),
            worker=WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-flash", max_turns=16, max_exploration_turns=4, retry_count=2),
            reviewer=AgentProfile(enabled=True, provider="codex", model="codex-sdk"),
            complexity_turns={"simple": 12, "normal": 16, "complex": 20},
            continuation_context=True,
            auto_validation=True,
        )

    @classmethod
    def strict_preset(cls) -> "ProjectAgentConfig":
        """Strict mode: full pipeline with Governor + Reviewer mandatory."""
        return cls(
            mode="strict",
            governor=AgentProfile(enabled=True, provider="codex", model="codex-sdk"),
            planner=AgentProfile(enabled=True, provider="kimi", model="kimi-k2.6", max_turns=10),
            worker=WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-flash", max_turns=25, max_exploration_turns=6, retry_count=2),
            reviewer=AgentProfile(enabled=True, provider="codex", model="codex-sdk"),
            complexity_turns={"simple": 14, "normal": 25, "complex": 30},
            continuation_context=True,
            auto_validation=True,
            auto_repair=True,
        )


def load_project_config(project_root: str) -> ProjectAgentConfig:
    """Load project AI config from .ai/agents.json, or return a sensible default."""
    config_path = Path(project_root) / ".ai" / "agents.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text())
            mode = data.get("mode", "auto")
            # If mode is a preset name and no custom overrides, return the preset
            if mode == "fast" and "worker" not in data:
                return ProjectAgentConfig.fast_preset()
            if mode == "standard" and "worker" not in data:
                return ProjectAgentConfig.standard_preset()
            if mode == "strict" and "worker" not in data:
                return ProjectAgentConfig.strict_preset()
            config = ProjectAgentConfig.from_dict(data)
            # Named modes own the phase topology. Older versions could persist
            # contradictory flags (for example standard + governor disabled).
            if mode == "fast":
                config.governor.enabled = False
                config.planner.enabled = False
                config.reviewer.enabled = False
            elif mode in {"auto", "standard", "strict"}:
                config.governor.enabled = True
                config.planner.enabled = True
                config.reviewer.enabled = True
            return config
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning(f"Failed to load {config_path}: {e}")
    return ProjectAgentConfig()  # default: auto mode


def save_project_config(project_root: str, config: ProjectAgentConfig):
    """Save project AI config to .ai/agents.json."""
    config_dir = Path(project_root) / ".ai"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "agents.json"
    config_path.write_text(json.dumps(config.to_dict(), indent=2, ensure_ascii=False))
    logger.info(f"Project config saved: {config_path}")
