#!/usr/bin/env python3
"""Test the full job pipeline without UI."""
import asyncio
import json
import logging
import sys
from pathlib import Path

project_root = Path(__file__).parent.resolve()
sys.path.insert(0, str(project_root))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Create a temp project directory
test_dir = project_root / ".test_project"
test_dir.mkdir(exist_ok=True)
(test_dir / "index.html").write_text("<html><body>Hello</body></html>")

async def run_pipeline():
    from storage.database import init_database, create_session_factory
    from storage.repositories import ProjectRepository, JobRepository, TaskRepository
    from orchestrator.engine import Engine

    # Use in-memory database for testing
    engine = Engine(db_path=":memory:")
    await engine.start()

    # Register mock providers (no real API calls)
    class MockProvider:
        def __init__(self):
            self.call_count = 0
            # Return valid governor constitution on first call, plan on second
            self._responses = [
                # 1st call: governor returns constitution
                json.dumps({
                    "goal": "写一个显示今天星期几的HTML小程序",
                    "constraints": [],
                    "acceptance_criteria": ["HTML页面能正常显示"],
                    "risk": "low",
                    "protected_paths": [],
                    "requires_final_review": False,
                }),
                # 2nd call: planner returns task plan
                json.dumps({
                    "summary": "创建一个显示今天星期几的HTML页面",
                    "tasks": [{
                        "id": "T001",
                        "title": "创建HTML文件",
                        "type": "coding",
                        "description": "创建一个显示今天星期几的HTML页面",
                        "dependencies": [],
                        "allowed_paths": ["*.html"],
                        "acceptance_command": "",
                    }]
                }),
            ]

        async def chat(self, *args, **kwargs):
            self.call_count += 1
            idx = min(self.call_count - 1, len(self._responses) - 1)
            content = self._responses[idx]
            logger.info(f"MockProvider.chat #{self.call_count} returning {content[:60]}...")
            return {"content": content, "finish_reason": "stop", "usage": {}}

        async def chat_with_tools(self, *args, **kwargs):
            self.call_count += 1
            # 3rd call: worker — return a write_file tool call
            if self.call_count == 3:
                return {
                    "content": "I'll create the HTML file.",
                    "finish_reason": "tool_calls",
                    "usage": {"input_tokens": 200, "output_tokens": 50},
                    "tool_calls": [{
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "today.html",
                                "content": "<html><body><h1>今天是星期四</h1></body></html>",
                            }),
                        },
                    }],
                }
            # 4th call: worker wrap-up (no tool calls)
            if self.call_count == 4:
                return {
                    "content": "Done.",
                    "finish_reason": "stop",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                }
            return {"content": "{}", "finish_reason": "stop", "usage": {}}

    mock = MockProvider()
    engine.model_router.register_provider("codex", mock)
    engine.model_router.register_provider("kimi", mock)
    engine.model_router.register_provider("deepseek", mock)

    # Register mock agents
    from agents.governor import GovernorAgent
    from agents.planner import PlannerAgent
    from agents.worker import WorkerAgent
    from agents.reviewer import ReviewerAgent
    from agents.emergency_coder import EmergencyCoderAgent
    from tools.tool_broker import ToolBroker
    from orchestrator.policy_engine import PolicyEngine

    tool_broker = ToolBroker(project_root=str(test_dir), policy_engine=PolicyEngine())
    engine.register_agent("governor", GovernorAgent(engine.model_router))
    engine.register_agent("planner", PlannerAgent(engine.model_router))
    engine.register_agent("worker", WorkerAgent(engine.model_router, tool_broker))
    engine.register_agent("reviewer", ReviewerAgent(engine.model_router))
    engine.register_agent("emergency_coder", EmergencyCoderAgent(engine.model_router, tool_broker))

    # Create project
    repos = engine._get_repos()
    try:
        project = repos["project"].create(
            name="TestProject",
            root_path=str(test_dir),
            description="Test project for pipeline validation",
        )
        logger.info(f"Project created: id={project.id}, name={project.name}")

        # Create job
        result = await engine.create_job(
            project_id=project.id,
            user_request="写一个显示今天星期几的HTML小程序",
            project_root=str(test_dir),
        )
        logger.info(f"Job created: {result}")

        # Run job
        logger.info("Starting job pipeline...")
        await engine.run_job(result["job_id"], str(test_dir))

        # Check results
        job = repos["job"].get_by_id(result["job_id"])
        logger.info(f"Job status: {job.status if job else 'not found'}")

        tasks = repos["task"].list_by_job(project.id)
        logger.info(f"Tasks: {len(tasks)}")
        for t in tasks:
            logger.info(f"  {t.task_id}: {t.title} [{t.status}]")

    finally:
        repos["_session"].close()

    await engine.stop()
    logger.info("Test complete")

def test():
    """Pytest-compatible wrapper that does not require pytest-asyncio."""
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    test()
