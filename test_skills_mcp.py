"""Regression coverage for Skills and MCP capability layers."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from agents.worker import WorkerAgent
from mcp_runtime.client import prepare_stdio_command
from mcp_runtime.manager import MCPManager
from mcp_runtime.trust import (
    approve_project_mcp,
    is_project_mcp_approved,
)
from orchestrator.agent_config import (
    MCPConfig,
    MCPServerConfig,
    ProjectAgentConfig,
    SkillConfig,
    load_project_config,
    save_project_config,
)
from orchestrator.policy_engine import PolicyEngine
from skills.manager import SkillManager
from skills.trust import (
    approve_project_skills,
    is_project_skills_approved,
)
from storage.database import create_session_factory, init_database
from storage.repositories import JobRepository, ProjectRepository, TaskRepository
from tools.tool_broker import ToolBroker


def test_project_config_round_trips_skills_and_mcp(tmp_path):
    config = ProjectAgentConfig()
    config.skills = SkillConfig(
        enabled=True,
        enabled_builtin=["bug-fix", "pyqt"],
        allow_project_skills=False,
        max_selected=2,
    )
    config.mcp = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="demo",
        command=sys.executable,
        args=["server.py"],
        env={"DEMO_TOKEN": "${DEMO_TOKEN}"},
        read_only=True,
    )])

    save_project_config(str(tmp_path), config)
    loaded = load_project_config(str(tmp_path))

    assert loaded.config_version == 3
    assert loaded.skills.enabled_builtin == ["bug-fix", "pyqt"]
    assert loaded.skills.max_selected == 2
    assert loaded.mcp.enabled
    assert loaded.mcp.servers[0].env["DEMO_TOKEN"] == "${DEMO_TOKEN}"


def test_skill_manager_loads_only_selected_bodies_and_project_override(
    tmp_path, monkeypatch
):
    project_skill = tmp_path / ".ai" / "skills" / "bug-fix"
    project_skill.mkdir(parents=True)
    project_skill.joinpath("SKILL.md").write_text(
        "---\nname: bug-fix\n"
        "description: Project-specific failure repair SOP.\n---\n\n"
        "# Project Bug Fix\n\nRun the project's focused regression first.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "skills.manager.is_project_skills_approved",
        lambda *_args, **_kwargs: True,
    )
    manager = SkillManager(tmp_path, SkillConfig(max_selected=3))

    assert manager._body_cache == {}
    selected, prompt = manager.render_for_task({
        "title": "修复 PyQt Windows 启动报错",
        "description": "窗口打开时 exception",
        "type": "coding",
        "allowed_paths": ["app/ui/main_window.py"],
    })

    assert "bug-fix" in selected
    assert "pyqt" in selected
    assert "Project Bug Fix" in prompt
    assert "simple-create" not in prompt
    assert all(path.name == "SKILL.md" for path in manager._body_cache)


def test_explicit_skill_is_honored_without_inventing_unknown_names(tmp_path):
    manager = SkillManager(tmp_path, SkillConfig(max_selected=2))
    selected = manager.select_for_task({
        "title": "Update module with $refactor",
        "description": "Use $does-not-exist too",
        "type": "coding",
        "skills": ["code-review"],
    })

    assert selected == ["code-review", "refactor"]


def test_task_repository_persists_selected_skills(tmp_path):
    engine = init_database(str(tmp_path / "studio.db"))
    session = create_session_factory(engine)()
    try:
        project = ProjectRepository(session).create("demo", str(tmp_path))
        job = JobRepository(session).create("JOB-1", project.id, "repair")
        task = TaskRepository(session).create(
            "T001", job.id, "repair UI", skills=["bug-fix", "pyqt"]
        )
        session.expire_all()

        assert TaskRepository(session).get_by_pk(task.id).skills == [
            "bug-fix", "pyqt",
        ]
    finally:
        session.close()


def _write_mcp_server(path: Path):
    path.write_text(
        """import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "read_item", "description": "Read one item", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, "annotations": {"readOnlyHint": True}},
            {"name": "create_item", "description": "Create one item", "inputSchema": {"type": "object", "properties": {}}, "annotations": {"readOnlyHint": False}}
        ]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "读取成功：" + message["params"]["arguments"].get("id", "")}], "isError": False}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}, ensure_ascii=False) + "\\n")
    sys.stdout.flush()
""",
        encoding="utf-8",
    )


def test_mcp_tools_are_namespaced_filtered_and_called(tmp_path):
    server = tmp_path / "fake_mcp.py"
    _write_mcp_server(server)
    manager = MCPManager(tmp_path)
    config = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="demo",
        command=sys.executable,
        args=[str(server)],
        read_only=True,
    )])

    async def exercise():
        statuses = await manager.configure(tmp_path, config)
        broker = ToolBroker(tmp_path, PolicyEngine(), mcp_manager=manager)
        task = SimpleNamespace(task_type="analysis", allowed_paths=["*"])
        definitions = broker.get_tool_definitions("analysis")
        names = [item["function"]["name"] for item in definitions]
        assert statuses["demo"]["status"] == "connected"
        assert "mcp__demo__read_item" in names
        assert "mcp__demo__create_item" not in names
        result = await broker.execute(
            task, "mcp__demo__read_item", {"id": "中文"}
        )
        await manager.close()
        return result

    result = asyncio.run(exercise())
    assert result["status"] == "success"
    assert result["content"][0]["text"] == "读取成功：中文"


def test_unavailable_mcp_server_does_not_remove_local_tools(tmp_path):
    manager = MCPManager(tmp_path)
    config = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="missing", command="definitely-not-a-real-mcp-command",
    )])

    async def exercise():
        status = await manager.configure(tmp_path, config)
        broker = ToolBroker(tmp_path, PolicyEngine(), mcp_manager=manager)
        names = [
            item["function"]["name"]
            for item in broker.get_tool_definitions("coding")
        ]
        await manager.close()
        return status, names

    status, names = asyncio.run(exercise())
    assert status["missing"]["status"] == "unavailable"
    assert "read_file" in names
    assert "apply_patch" in names


def test_tool_broker_refuses_unapproved_project_mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.tool_broker.is_project_mcp_approved",
        lambda *_args, **_kwargs: False,
    )
    broker = ToolBroker(tmp_path, PolicyEngine())
    config = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="untrusted", command="must-not-run",
    )])

    status = asyncio.run(broker.configure_mcp(tmp_path, config))

    assert status["policy"]["status"] == "approval_required"
    assert all(
        not item["function"]["name"].startswith("mcp__")
        for item in broker.get_tool_definitions("coding")
    )


def test_worker_completes_an_explicit_external_action_without_local_edits(tmp_path):
    server = tmp_path / "fake_mcp.py"
    _write_mcp_server(server)
    manager = MCPManager(tmp_path)
    config = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="demo",
        command=sys.executable,
        args=[str(server)],
        read_only=False,
    )])

    class Router:
        _current_job_id = "JOB-1"
        cost_engine = None
        event_bus = None

        def __init__(self):
            self.calls = 0

        async def chat_with_tools(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "content": "",
                    "tool_calls": [{
                        "id": "call-1",
                        "function": {
                            "name": "mcp__demo__create_item",
                            "arguments": "{}",
                        },
                    }],
                    "usage": {},
                }
            return {"content": "外部项目已创建", "tool_calls": [], "usage": {}}

    async def exercise():
        await manager.configure(tmp_path, config)
        broker = ToolBroker(tmp_path, PolicyEngine(), mcp_manager=manager)
        worker = WorkerAgent(Router(), broker, max_turns=3)
        task = SimpleNamespace(
            task_id="T001", title="创建外部项目", description="通过 MCP 创建",
            task_type="action", allowed_paths=[], acceptance_command="",
            skills=[], job=None,
        )
        result = await worker.run(task, project_root=str(tmp_path))
        await manager.close()
        return result

    result = asyncio.run(exercise())
    assert result["status"] == "completed"
    assert result["external_action"] is True
    assert result["content"] == "外部项目已创建"


def test_windows_mcp_batch_launcher_uses_comspec():
    command = prepare_stdio_command(
        r"C:\Users\测试\AppData\Roaming\npm\mcp.cmd",
        ["--stdio"],
        platform="win32",
        environ={"COMSPEC": r"C:\Windows\System32\cmd.exe", "PATH": ""},
    )

    assert command[:4] == [
        r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c",
    ]
    assert "mcp.cmd" in command[4]


def test_mcp_approval_is_project_local_and_invalidated_by_config_change(tmp_path):
    store = tmp_path / "user-data" / "mcp_approvals.json"
    project = tmp_path / "project"
    project.mkdir()
    config = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="demo", command="demo-server", read_only=True,
    )])

    assert not is_project_mcp_approved(project, config, store)
    approve_project_mcp(project, config, store)
    assert is_project_mcp_approved(project, config, store)

    changed = MCPConfig(enabled=True, servers=[MCPServerConfig(
        name="demo", command="different-server", read_only=True,
    )])
    assert not is_project_mcp_approved(project, changed, store)


def test_project_skill_approval_is_invalidated_when_skill_content_changes(tmp_path):
    store = tmp_path / "user-data" / "skill_approvals.json"
    project = tmp_path / "project"
    skill_file = project / ".ai" / "skills" / "team-style" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: team-style\ndescription: Team conventions.\n---\nOne\n",
        encoding="utf-8",
    )
    config = SkillConfig(allow_project_skills=True)

    approve_project_skills(project, config, store)
    assert is_project_skills_approved(project, config, store)

    skill_file.write_text(
        "---\nname: team-style\ndescription: Team conventions.\n---\nTwo\n",
        encoding="utf-8",
    )
    assert not is_project_skills_approved(project, config, store)
