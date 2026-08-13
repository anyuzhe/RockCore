"""Regression coverage for deterministic runtime resolution and stage context."""

import asyncio
import json
from types import SimpleNamespace

from agents.planner import PlannerAgent
from memory.context_manager import ContextManager
from orchestrator.engine import Engine
from orchestrator.policy_engine import PolicyEngine
from orchestrator.project_resolver import ProjectResolver


def test_html_entry_follows_nested_scripts_and_marks_unreferenced_code_legacy(
    tmp_path,
):
    (tmp_path / "site" / "js").mkdir(parents=True)
    (tmp_path / "site" / "css").mkdir()
    (tmp_path / "site" / "index.html").write_text(
        '<link rel="stylesheet" href="css/app.css">'
        '<script type="module" src="js/main.js"></script>',
        encoding="utf-8",
    )
    (tmp_path / "site" / "css" / "app.css").write_text(
        "body { color: black; }", encoding="utf-8"
    )
    (tmp_path / "site" / "js" / "main.js").write_text(
        "import { Player } from './player.js';\nnew Player();",
        encoding="utf-8",
    )
    (tmp_path / "site" / "js" / "player.js").write_text(
        "export class Player {}", encoding="utf-8"
    )
    (tmp_path / "site" / "js" / "old-player.js").write_text(
        "class Player {}", encoding="utf-8"
    )

    surface = ProjectResolver(tmp_path).resolve()

    assert surface["entrypoints"][0]["path"] == "site/index.html"
    assert surface["active_files"] == [
        "site/css/app.css", "site/index.html", "site/js/main.js",
        "site/js/player.js",
    ]
    assert "site/js/old-player.js" in surface["legacy_files"]
    assert surface["duplicate_symbols"] == [{
        "name": "Player",
        "files": ["site/js/old-player.js", "site/js/player.js"],
    }]


def test_package_scripts_import_graph_and_commands_are_resolved(tmp_path):
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "package.json").write_text(json.dumps({
        "main": "src/server.js",
        "scripts": {"start": "node src/server.js", "test": "vitest"},
    }), encoding="utf-8")
    (tmp_path / "src" / "server.js").write_text(
        "const api = require('./lib/api');", encoding="utf-8"
    )
    (tmp_path / "src" / "lib" / "api.js").write_text(
        "export const api = {};", encoding="utf-8"
    )

    surface = ProjectResolver(tmp_path).resolve()

    assert surface["active_files"] == [
        "package.json", "src/lib/api.js", "src/server.js",
    ]
    assert surface["commands"]["start"] == "npm run start"
    assert surface["commands"]["test"] == "npm test"


def test_python_entry_follows_local_imports_and_detects_validation(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text(
        "from app.service import run\nrun()", encoding="utf-8"
    )
    (tmp_path / "app" / "service.py").write_text(
        "def run():\n    return True\n", encoding="utf-8"
    )
    (tmp_path / "test_service.py").write_text("def test_run(): pass\n", encoding="utf-8")

    surface = ProjectResolver(tmp_path).resolve()

    assert {"app/main.py", "app/service.py"}.issubset(surface["active_files"])
    assert "test_service.py" in surface["support_files"]
    assert surface["legacy_files"] == []
    assert surface["commands"]["start"] == "python app/main.py"
    assert surface["commands"]["test"] == "python -m pytest -q"


def test_active_surface_narrows_broad_plan_but_preserves_new_output():
    plan = {"tasks": [{
        "id": "T001", "type": "coding", "allowed_paths": ["**/*", "new.css"],
    }]}
    changed = Engine._ground_plan_in_project_surface(plan, {
        "active_files": ["index.html", "js/main.js"],
        "legacy_files": ["js/old.js"],
    })

    assert changed is True
    assert plan["tasks"][0]["allowed_paths"] == [
        "index.html", "js/main.js", "new.css",
    ]


def test_active_surface_preserves_scoped_globs_for_new_tests():
    plan = {"tasks": [{
        "id": "T001", "type": "testing",
        "allowed_paths": ["tests/**/*.py"],
    }]}

    changed = Engine._ground_plan_in_project_surface(plan, {
        "active_files": ["app/main.py"],
    })

    assert changed is False
    assert plan["tasks"][0]["allowed_paths"] == ["tests/**/*.py"]


def test_context_manager_prefers_active_runtime_over_arbitrary_sources(tmp_path):
    (tmp_path / "index.html").write_text(
        '<script src="active.js"></script>', encoding="utf-8"
    )
    (tmp_path / "active.js").write_text("const active = true;", encoding="utf-8")
    (tmp_path / "legacy.js").write_text("const old = true;", encoding="utf-8")
    manager = ContextManager(str(tmp_path))
    asyncio.run(manager.initialize())
    manager.set_project_surface(ProjectResolver(tmp_path).resolve())
    task = SimpleNamespace(
        task_type="coding", allowed_paths=["**/*"],
        _rockcore_project_surface=manager.project_surface,
    )

    context = asyncio.run(manager.build_task_context(task))

    assert "Runtime files (authoritative)" in context
    assert "active.js" in context
    assert "legacy.js" in context
    assert "do not edit unless" in context


def test_planner_receives_active_surface_and_stage_limit():
    class Router:
        def __init__(self):
            self.system = ""
            self.message = ""

        async def chat(self, _agent_type, system, messages, **_kwargs):
            self.system = system
            self.message = messages[0]["content"]
            return {"content": '{"summary":"ok","tasks":[]}'}

    router = Router()
    planner = PlannerAgent(router)
    job = SimpleNamespace(
        job_id="JOB-1", user_request="修改界面", attachments=[],
        _rockcore_project_surface={
            "entrypoints": [{"path": "index.html"}],
            "active_files": ["index.html", "app.js"],
            "legacy_files": ["old.js"],
        },
    )

    asyncio.run(planner.run(job))

    assert "Maximum 8 tasks" in router.system
    assert "Active Project Surface" in router.message
    assert '"old.js"' in router.message


def test_shared_execution_checkpoint_contains_results_without_chat_history():
    job = SimpleNamespace(
        last_checkpoint={},
        _rockcore_project_surface={
            "entrypoints": [{"path": "index.html"}],
            "active_files": ["index.html", "app.js"],
            "legacy_files": ["old.js"],
            "commands": {"test": "npm test"},
        },
    )
    task = SimpleNamespace(task_id="T002")
    context = Engine._execution_continuation_context(job, task, {
        "T001": {
            "output": "Implemented state management",
            "changes": {"changed": ["app.js"]},
            "raw_messages": ["must not be replayed"],
        }
    })

    assert "SHARED EXECUTION CHECKPOINT" in context
    assert "Implemented state management" in context
    assert '"app.js"' in context
    assert "must not be replayed" not in context


def test_policy_rejects_more_than_eight_execution_stages():
    plan = {"tasks": [{
        "id": f"T{index:03d}", "title": "stage", "description": "work",
        "dependencies": [], "allowed_paths": ["app.py"],
    } for index in range(1, 10)]}

    errors = PolicyEngine().check_task_plan(plan, {})

    assert any("at most 8" in error for error in errors)


def test_engine_persists_surface_before_model_phases(tmp_path):
    async def scenario():
        (tmp_path / "index.html").write_text(
            '<script src="app.js"></script>', encoding="utf-8"
        )
        (tmp_path / "app.js").write_text("const app = true;", encoding="utf-8")
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Resolved", str(tmp_path))
            created = await engine.create_job(
                project.id, "修改页面", str(tmp_path)
            )
            job = repos["job"].get_by_id(created["job_id"])

            surface = await engine._resolve_project_surface(
                job, repos, str(tmp_path)
            )
            repos["_session"].refresh(job)

            assert surface["active_files"] == ["app.js", "index.html"]
            assert job.last_checkpoint["project_surface"]["active_files"] == [
                "app.js", "index.html",
            ]
            events = engine.event_bus.get_history("project_resolved")
            assert events[-1]["data"]["confidence"] == 0.98
        finally:
            repos["_session"].close()

    asyncio.run(scenario())
