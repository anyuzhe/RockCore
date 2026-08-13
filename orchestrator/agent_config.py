"""Project-level Agent Configuration — persisted per project in .ai/agents.json."""

import json
import logging
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


KIMI_K27_CODE_MODEL = "kimi-k2.7-code"
KIMI_MODEL_ALIASES = {
    # Kimi K2.7 was released as a coding model; the API never accepted the
    # shorter display-name-like identifier that older RockCore builds stored.
    "kimi-k2.7": KIMI_K27_CODE_MODEL,
}


def normalize_model_id(provider: str, model: str) -> str:
    """Return the provider API model ID, migrating known legacy aliases."""
    normalized = str(model or "").strip()
    if str(provider or "").strip().lower() == "kimi":
        return KIMI_MODEL_ALIASES.get(normalized, normalized)
    return normalized


# Available model versions per provider
PROVIDER_MODELS = {
    "kimi": [
        "kimi-k3", KIMI_K27_CODE_MODEL, "kimi-k2.6", "kimi-k2.5",
        "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k",
    ],
    "deepseek": [
        "deepseek-v4-pro", "deepseek-v4-flash",
        "deepseek-chat", "deepseek-reasoner",
    ],
    "codex": ["gpt-5.6-sol", "codex-sdk"],
}

PROVIDER_REASONING_LEVELS = {
    "codex": ["none", "low", "medium", "high", "xhigh", "max"],
    # These providers do not expose the same Codex reasoning-effort contract.
    "kimi": ["default"],
    "deepseek": ["default"],
}


@dataclass
class AgentProfile:
    enabled: bool = True
    provider: str = ""
    model: str = ""
    reasoning_effort: str = "default"
    max_turns: int = 0
    retry_count: int = 1


@dataclass
class WorkerProfile(AgentProfile):
    max_exploration_turns: int = 48
    patch_recovery_turns: int = 6
    emergency_after_failures: int = 3
    fallback_provider: str = "kimi"
    fallback_model: str = KIMI_K27_CODE_MODEL


@dataclass
class HookConfig:
    """Project-owned deterministic lifecycle commands."""

    enabled: bool = False
    before_job: list[str] = field(default_factory=list)
    after_write: list[str] = field(default_factory=list)
    before_test: list[str] = field(default_factory=list)
    before_commit: list[str] = field(default_factory=list)
    after_job: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None) -> "HookConfig":
        values = data if isinstance(data, dict) else {}
        result = cls(enabled=bool(values.get("enabled", False)))
        for name in (
            "before_job", "after_write", "before_test",
            "before_commit", "after_job",
        ):
            raw = values.get(name) or []
            if isinstance(raw, str):
                raw = [raw]
            setattr(result, name, [str(item).strip() for item in raw if str(item).strip()])
        return result


CORE_BUILTIN_SKILLS = [
    "simple-create",
    "simple-edit",
    "bug-fix",
    "refactor",
    "code-review",
    "pyqt",
    "web",
]

COMMON_PLUGINS = {
    "documents": {
        "display_name": "@documents",
        "description": "读取、创建和整理 Word（.docx）文档",
        "skill": "documents",
        "availability": "local",
    },
    "browser": {
        "display_name": "@browser",
        "description": "导航、点击和检查网页；需要配置 Browser MCP 服务",
        "skill": "browser",
        "availability": "mcp_required",
    },
    "openai-docs": {
        "display_name": "$openai-docs",
        "description": "搜索并读取 OpenAI、ChatGPT 与 Codex 官方文档",
        "skill": "openai-docs",
        "availability": "builtin_mcp",
    },
    "pdf": {
        "display_name": "@pdf",
        "description": "分页读取、创建和整理 PDF 文件",
        "skill": "pdf",
        "availability": "local",
    },
    "presentations": {
        "display_name": "@presentations",
        "description": "读取和创建 PowerPoint（.pptx）演示文稿",
        "skill": "presentations",
        "availability": "local",
    },
}
PLUGIN_SKILLS = [item["skill"] for item in COMMON_PLUGINS.values()]
BUILTIN_SKILLS = [*CORE_BUILTIN_SKILLS, *PLUGIN_SKILLS]


@dataclass
class PluginConfig:
    """Frequently used capability packs composed from Skills and tools."""

    enabled: bool = True
    enabled_plugins: list[str] = field(
        default_factory=lambda: list(COMMON_PLUGINS)
    )

    @classmethod
    def from_dict(cls, data: dict | None) -> "PluginConfig":
        values = data if isinstance(data, dict) else {}
        raw = values.get("enabled_plugins", list(COMMON_PLUGINS))
        if not isinstance(raw, list):
            raw = list(COMMON_PLUGINS)
        return cls(
            enabled=bool(values.get("enabled", True)),
            enabled_plugins=[
                str(name).strip() for name in raw
                if str(name).strip() in COMMON_PLUGINS
            ],
        )

    def skill_names(self) -> set[str]:
        if not self.enabled:
            return set()
        return {
            COMMON_PLUGINS[name]["skill"]
            for name in self.enabled_plugins if name in COMMON_PLUGINS
        }


@dataclass
class SkillConfig:
    """Project skill discovery and prompt-loading policy."""

    enabled: bool = True
    enabled_builtin: list[str] = field(
        default_factory=lambda: list(BUILTIN_SKILLS)
    )
    allow_project_skills: bool = True
    max_selected: int = 3

    @classmethod
    def from_dict(cls, data: dict | None) -> "SkillConfig":
        values = data if isinstance(data, dict) else {}
        enabled_builtin = values.get("enabled_builtin", BUILTIN_SKILLS)
        if not isinstance(enabled_builtin, list):
            enabled_builtin = list(BUILTIN_SKILLS)
        return cls(
            enabled=bool(values.get("enabled", True)),
            enabled_builtin=[
                str(name).strip() for name in enabled_builtin
                if str(name).strip()
            ],
            allow_project_skills=bool(
                values.get("allow_project_skills", True)
            ),
            max_selected=max(1, min(8, int(values.get("max_selected", 3)))),
        )


@dataclass
class MCPServerConfig:
    """One explicitly configured MCP stdio or Streamable HTTP server."""

    name: str = ""
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: int = 20
    allow_tools: list[str] = field(default_factory=lambda: ["*"])
    read_only: bool = True

    @classmethod
    def from_dict(cls, data: dict | None) -> "MCPServerConfig":
        values = data if isinstance(data, dict) else {}
        name = str(values.get("name") or "").strip().lower()
        if name and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", name):
            raise ValueError(
                f"MCP 服务名不合法：{name}；仅允许小写字母、数字、_ 和 -"
            )
        command = str(values.get("command") or "").strip()
        transport = str(values.get("transport") or "stdio").strip().lower()
        if transport not in {"stdio", "streamable_http"}:
            raise ValueError(
                f"MCP 服务 {name or '<unnamed>'} 的 transport 必须是 "
                "stdio 或 streamable_http"
            )
        args = values.get("args") or []
        env = values.get("env") or {}
        url = str(values.get("url") or "").strip()
        headers = values.get("headers") or {}
        allow_tools = values.get("allow_tools") or ["*"]
        if not isinstance(args, list):
            raise ValueError(f"MCP 服务 {name or '<unnamed>'} 的 args 必须是数组")
        if not isinstance(env, dict):
            raise ValueError(f"MCP 服务 {name or '<unnamed>'} 的 env 必须是对象")
        if not isinstance(headers, dict):
            raise ValueError(f"MCP 服务 {name or '<unnamed>'} 的 headers 必须是对象")
        if not isinstance(allow_tools, list):
            raise ValueError(
                f"MCP 服务 {name or '<unnamed>'} 的 allow_tools 必须是数组"
            )
        return cls(
            name=name,
            transport=transport,
            command=command,
            args=[str(value) for value in args],
            env={str(key): str(value) for key, value in env.items()},
            url=url,
            headers={str(key): str(value) for key, value in headers.items()},
            enabled=bool(values.get("enabled", True)),
            timeout_seconds=max(
                2, min(300, int(values.get("timeout_seconds", 20)))
            ),
            allow_tools=[str(value) for value in allow_tools],
            read_only=bool(values.get("read_only", True)),
        )


@dataclass
class MCPConfig:
    """Project MCP feature flag and server list."""

    enabled: bool = False
    servers: list[MCPServerConfig] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict | None) -> "MCPConfig":
        values = data if isinstance(data, dict) else {}
        raw_servers = values.get("servers") or []
        if not isinstance(raw_servers, list):
            raise ValueError("MCP servers 必须是数组")
        servers = [MCPServerConfig.from_dict(item) for item in raw_servers]
        names = [server.name for server in servers if server.name]
        if len(names) != len(set(names)):
            raise ValueError("MCP 服务名不能重复")
        for server in servers:
            if not server.enabled:
                continue
            if not server.name:
                raise ValueError("启用的 MCP 服务必须配置 name")
            if server.transport == "stdio" and not server.command:
                raise ValueError("启用的 stdio MCP 服务必须配置 command")
            if server.transport == "streamable_http":
                if not server.url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                    raise ValueError(
                        "Streamable HTTP MCP 仅允许 HTTPS 或本机 HTTP URL"
                    )
        return cls(enabled=bool(values.get("enabled", False)), servers=servers)


@dataclass
class ProjectAgentConfig:
    """Per-project AI workflow configuration. Persisted to .ai/agents.json."""

    # ── Mode ──
    config_version: int = 10
    # The historical governor profile now configures the model Main Agent.
    mode: str = "auto"  # "auto" | "fast" | "standard" | "strict" | "custom"

    # ── Agent profiles ──
    governor: AgentProfile = field(default_factory=lambda: AgentProfile(
        enabled=True, provider="codex", model="gpt-5.6-sol",
        reasoning_effort="high",
    ))
    planner: AgentProfile = field(default_factory=lambda: AgentProfile(
        enabled=True, provider="kimi", model="kimi-k3",
        reasoning_effort="default", max_turns=24,
    ))
    worker: WorkerProfile = field(default_factory=lambda: WorkerProfile(
        enabled=True, provider="deepseek", model="deepseek-v4-pro",
        reasoning_effort="default", max_turns=96,
        max_exploration_turns=60, patch_recovery_turns=6, retry_count=2,
        emergency_after_failures=3, fallback_provider="kimi",
        fallback_model=KIMI_K27_CODE_MODEL,
    ))
    reviewer: AgentProfile = field(default_factory=lambda: AgentProfile(
        enabled=True, provider="codex", model="gpt-5.6-sol",
        reasoning_effort="high",
    ))
    emergency_coder: AgentProfile = field(default_factory=lambda: AgentProfile(
        enabled=True, provider="codex", model="gpt-5.6-sol",
        reasoning_effort="max",
    ))

    # ── Per-complexity turn overrides ──
    complexity_turns: dict[str, int] = field(default_factory=lambda: {
        "simple": 60,
        "normal": 96,
        "complex": 144,
    })
    complexity_exploration: dict[str, int] = field(default_factory=lambda: {
        "simple": 36,
        "normal": 60,
        "complex": 96,
    })

    # ── Features ──
    continuation_context: bool = True
    auto_validation: bool = True
    auto_repair: bool = True

    # ── Reusable capabilities and external tools ──
    skills: SkillConfig = field(default_factory=SkillConfig)
    plugins: PluginConfig = field(default_factory=PluginConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    hooks: HookConfig = field(default_factory=HookConfig)

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectAgentConfig":
        cfg = cls()
        source_version = int(data.get("config_version", 1) or 1)
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
        if "emergency_coder" in data:
            cfg.emergency_coder = AgentProfile(**data["emergency_coder"])
        if "complexity_turns" in data:
            cfg.complexity_turns.update(data["complexity_turns"])
        if "complexity_exploration" in data:
            cfg.complexity_exploration.update(data["complexity_exploration"])
        for k in ("continuation_context", "auto_validation", "auto_repair"):
            if k in data:
                setattr(cfg, k, data[k])
        cfg.skills = SkillConfig.from_dict(data.get("skills"))
        cfg.plugins = PluginConfig.from_dict(data.get("plugins"))
        cfg.mcp = MCPConfig.from_dict(data.get("mcp"))
        cfg.hooks = HookConfig.from_dict(data.get("hooks"))
        if source_version < 2:
            cfg._upgrade_legacy_recommendations()
        if source_version < 5:
            cfg._upgrade_completion_limits()
        if source_version < 6:
            cfg._upgrade_exploration_limits()
        if source_version < 8:
            cfg._triple_runtime_limits(data)
        if source_version < 9:
            cfg._upgrade_default_worker_model()
        cfg._normalize_provider_model_ids()
        cfg.config_version = 10
        return cfg

    def _upgrade_default_worker_model(self):
        """Move the former built-in Flash Worker default to V4 Pro."""
        if (
            self.worker.provider == "deepseek"
            and self.worker.model in {"", "deepseek-v4-flash"}
        ):
            self.worker.model = "deepseek-v4-pro"

    def _triple_runtime_limits(self, source: dict):
        """Triple persisted non-monetary ceilings once for existing projects."""
        profile_names = (
            "governor", "planner", "worker", "reviewer", "emergency_coder",
        )
        for name in profile_names:
            profile_data = source.get(name)
            profile = getattr(self, name)
            if (
                isinstance(profile_data, dict)
                and "max_turns" in profile_data
                and profile.max_turns > 0
            ):
                profile.max_turns *= 3
        worker_data = source.get("worker")
        if isinstance(worker_data, dict):
            if "max_exploration_turns" in worker_data:
                self.worker.max_exploration_turns = max(
                    1, self.worker.max_exploration_turns * 3
                )
            if "patch_recovery_turns" in worker_data:
                self.worker.patch_recovery_turns = max(
                    0, self.worker.patch_recovery_turns * 3
                )
        if "complexity_turns" in source:
            self.complexity_turns = {
                key: max(1, int(value) * 3)
                for key, value in self.complexity_turns.items()
            }
        if "complexity_exploration" in source:
            self.complexity_exploration = {
                key: max(1, int(value) * 3)
                for key, value in self.complexity_exploration.items()
            }

    def _normalize_provider_model_ids(self):
        """Migrate persisted display aliases to callable provider API IDs."""
        for profile in (
            self.governor, self.planner, self.worker,
            self.reviewer, self.emergency_coder,
        ):
            profile.model = normalize_model_id(profile.provider, profile.model)
        self.worker.fallback_model = normalize_model_id(
            self.worker.fallback_provider, self.worker.fallback_model
        )

    def _upgrade_exploration_limits(self):
        """Raise former built-in soft reminders without changing custom limits."""
        if self.worker.max_exploration_turns in {4, 6}:
            self.worker.max_exploration_turns = 20
        old_exploration = {"simple": 6, "normal": 8, "complex": 10}
        new_exploration = {"simple": 12, "normal": 20, "complex": 32}
        for level, old_value in old_exploration.items():
            if self.complexity_exploration.get(level) == old_value:
                self.complexity_exploration[level] = new_exploration[level]

    def _upgrade_completion_limits(self):
        """Raise former built-in ceilings without overwriting custom values."""
        if self.worker.max_turns == 24:
            self.worker.max_turns = 32
        if self.worker.max_exploration_turns == 4:
            self.worker.max_exploration_turns = 6
        old_turns = {"simple": 16, "normal": 24, "complex": 36}
        new_turns = {"simple": 20, "normal": 32, "complex": 48}
        for level, old_value in old_turns.items():
            if self.complexity_turns.get(level) == old_value:
                self.complexity_turns[level] = new_turns[level]
        old_exploration = {"simple": 4, "normal": 6, "complex": 8}
        new_exploration = {"simple": 6, "normal": 8, "complex": 10}
        for level, old_value in old_exploration.items():
            if self.complexity_exploration.get(level) == old_value:
                self.complexity_exploration[level] = new_exploration[level]

    def _upgrade_legacy_recommendations(self):
        """Upgrade only the former built-in defaults, not arbitrary choices."""
        if self.governor.provider == "codex" and self.governor.model == "codex-sdk":
            self.governor.model = "gpt-5.6-sol"
        if self.reviewer.provider in {"", "kimi", "codex"}:
            self.reviewer.provider = "codex"
            if self.reviewer.model in {"", "codex-sdk", "kimi-k2.6", "kimi-k3"}:
                self.reviewer.model = "gpt-5.6-sol"
        if self.planner.provider == "kimi" and self.planner.model == "kimi-k2.6":
            self.planner.model = "kimi-k3"
        self.governor.reasoning_effort = "high"
        self.reviewer.reasoning_effort = "high"
        self.emergency_coder.reasoning_effort = "max"

    def to_dict(self) -> dict:
        return {
            "config_version": self.config_version,
            "mode": self.mode,
            "governor": asdict(self.governor),
            "planner": asdict(self.planner),
            "worker": asdict(self.worker),
            "reviewer": asdict(self.reviewer),
            "emergency_coder": asdict(self.emergency_coder),
            "complexity_turns": self.complexity_turns,
            "complexity_exploration": self.complexity_exploration,
            "continuation_context": self.continuation_context,
            "auto_validation": self.auto_validation,
            "auto_repair": self.auto_repair,
            "skills": asdict(self.skills),
            "plugins": asdict(self.plugins),
            "mcp": asdict(self.mcp),
            "hooks": asdict(self.hooks),
        }

    def builtin_mcp_servers(self, request: str = "") -> list[MCPServerConfig]:
        """Return trusted read-only MCP servers needed by this request."""
        if not self.plugins.enabled or "openai-docs" not in self.plugins.enabled_plugins:
            return []
        text = str(request or "").lower()
        if not re.search(
            r"\b(openai|chatgpt|codex|gpt|responses? api|agents? sdk)\b|"
            r"开放人工智能|模型接口|官方文档",
            text,
        ):
            return []
        return [MCPServerConfig(
            name="openai_developer_docs",
            transport="streamable_http",
            url="https://developers.openai.com/mcp",
            read_only=True,
            allow_tools=["*"],
            timeout_seconds=20,
        )]

    def get_worker_turns(self, complexity: str) -> int:
        return self.complexity_turns.get(complexity, self.worker.max_turns or 20)

    def get_exploration_turns(self, complexity: str) -> int:
        return self.complexity_exploration.get(complexity,
                                                self.worker.max_exploration_turns or 48)

    # ── Presets ──

    @classmethod
    def fast_preset(cls) -> "ProjectAgentConfig":
        """Fast mode: Worker only, no model Main Agent/Planner/Reviewer."""
        return cls(
            mode="fast",
            governor=AgentProfile(enabled=False, provider="codex", model="gpt-5.6-sol", reasoning_effort="high"),
            planner=AgentProfile(enabled=False, provider="kimi", model="kimi-k3", max_turns=0),
            worker=WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-pro", max_turns=30, max_exploration_turns=30, patch_recovery_turns=6, retry_count=2, emergency_after_failures=3),
            complexity_exploration={"simple": 30, "normal": 42, "complex": 60},
            reviewer=AgentProfile(enabled=False, provider="codex", model="gpt-5.6-sol", reasoning_effort="high"),
            emergency_coder=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="max"),
            complexity_turns={"simple": 24, "normal": 30, "complex": 36},
            continuation_context=True,
            auto_repair=False,
        )

    @classmethod
    def standard_preset(cls) -> "ProjectAgentConfig":
        """Standard mode: the complete workflow with moderate budgets."""
        return cls(
            mode="standard",
            governor=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="high"),
            planner=AgentProfile(enabled=True, provider="kimi", model="kimi-k3", max_turns=24),
            worker=WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-pro", max_turns=96, max_exploration_turns=60, patch_recovery_turns=6, retry_count=2, emergency_after_failures=3),
            reviewer=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="high"),
            emergency_coder=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="max"),
            complexity_turns={"simple": 60, "normal": 96, "complex": 144},
            complexity_exploration={"simple": 36, "normal": 60, "complex": 96},
            continuation_context=True,
            auto_validation=True,
            auto_repair=True,
        )

    @classmethod
    def strict_preset(cls) -> "ProjectAgentConfig":
        """Strict mode: model Main Agent plus independent Reviewer."""
        return cls(
            mode="strict",
            governor=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="high"),
            planner=AgentProfile(enabled=True, provider="kimi", model="kimi-k3", max_turns=30),
            worker=WorkerProfile(enabled=True, provider="deepseek", model="deepseek-v4-pro", max_turns=90, max_exploration_turns=60, patch_recovery_turns=6, retry_count=2, emergency_after_failures=3),
            reviewer=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="high"),
            emergency_coder=AgentProfile(enabled=True, provider="codex", model="gpt-5.6-sol", reasoning_effort="max"),
            complexity_turns={"simple": 60, "normal": 90, "complex": 120},
            complexity_exploration={"simple": 36, "normal": 60, "complex": 96},
            continuation_context=True,
            auto_validation=True,
            auto_repair=True,
        )


def load_project_config(project_root: str) -> ProjectAgentConfig:
    """Load project AI config from .ai/agents.json, or return a sensible default."""
    config_path = Path(project_root) / ".ai" / "agents.json"
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8-sig"))
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
                config.emergency_coder.enabled = True
            elif mode in {"auto", "standard", "strict"}:
                config.governor.enabled = True
                config.planner.enabled = True
                config.reviewer.enabled = True
                config.emergency_coder.enabled = True
            return config
        except (
            json.JSONDecodeError, OSError, UnicodeError, TypeError, ValueError
        ) as e:
            logger.warning(f"Failed to load {config_path}: {e}")
    return ProjectAgentConfig()  # default: auto mode


def save_project_config(project_root: str, config: ProjectAgentConfig):
    """Save project AI config to .ai/agents.json."""
    config_dir = Path(project_root) / ".ai"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "agents.json"
    config_path.write_text(
        json.dumps(config.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"Project config saved: {config_path}")
