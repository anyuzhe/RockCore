"""Task-scoped read and search cache regression tests."""

import asyncio
from types import SimpleNamespace

from orchestrator.policy_engine import PolicyEngine
from tools.tool_broker import ToolBroker


def _task():
    return SimpleNamespace(
        task_type="coding",
        allowed_paths=["*", "**/*"],
        protected_paths=[],
        title="Update project",
        description="",
        acceptance_command="",
    )


def test_identical_reads_and_searches_use_task_cache(tmp_path):
    (tmp_path / "game.js").write_text(
        "const player = 'ready';\n", encoding="utf-8"
    )
    broker = ToolBroker(tmp_path, PolicyEngine())
    counts = {"read_file": 0, "search_in_file": 0, "search_code": 0}

    for tool_name in counts:
        original = broker._tool_registry[tool_name]

        async def counted(_original=original, _name=tool_name, **kwargs):
            counts[_name] += 1
            return await _original(**kwargs)

        broker._tool_registry[tool_name] = counted

    async def exercise():
        calls = [
            ("read_file", {"path": "game.js"}),
            ("search_in_file", {"path": "game.js", "text": "player"}),
            ("search_code", {"pattern": "player", "path": "."}),
        ]
        results = []
        for name, args in calls:
            first = await broker.execute(_task(), name, args)
            second = await broker.execute(_task(), name, args)
            results.append((first, second))
        return results

    results = asyncio.run(exercise())

    assert counts == {"read_file": 1, "search_in_file": 1, "search_code": 1}
    for first, second in results:
        assert "cache_hit" not in first
        assert second["cache_hit"] is True
        assert second["duration_ms"] == 0


def test_read_cache_keeps_distinct_pagination_and_search_arguments(tmp_path):
    (tmp_path / "game.js").write_text(
        "one\ntwo\nthree\nfour\n", encoding="utf-8"
    )
    broker = ToolBroker(tmp_path, PolicyEngine())
    calls = 0
    original = broker._tool_registry["read_file"]

    async def counted(**kwargs):
        nonlocal calls
        calls += 1
        return await original(**kwargs)

    broker._tool_registry["read_file"] = counted

    async def exercise():
        await broker.execute(
            _task(), "read_file", {"path": "game.js", "start": 1, "end": 2}
        )
        await broker.execute(
            _task(), "read_file", {"path": "game.js", "start": 3, "end": 4}
        )

    asyncio.run(exercise())

    assert calls == 2


def test_successful_write_invalidates_cached_reads(tmp_path):
    source = tmp_path / "game.js"
    source.write_text("old\n", encoding="utf-8")
    broker = ToolBroker(tmp_path, PolicyEngine())
    calls = 0
    original = broker._tool_registry["read_file"]

    async def counted(**kwargs):
        nonlocal calls
        calls += 1
        return await original(**kwargs)

    broker._tool_registry["read_file"] = counted

    async def exercise():
        first = await broker.execute(_task(), "read_file", {"path": "game.js"})
        cached = await broker.execute(_task(), "read_file", {"path": "game.js"})
        written = await broker.execute(_task(), "write_file", {
            "path": "game.js", "content": "new\n",
        })
        refreshed = await broker.execute(
            _task(), "read_file", {"path": "game.js"}
        )
        return first, cached, written, refreshed

    first, cached, written, refreshed = asyncio.run(exercise())

    assert first["content"] == "old\n"
    assert cached["cache_hit"] is True
    assert written["status"] == "written"
    assert refreshed["content"] == "new\n"
    assert "cache_hit" not in refreshed
    assert calls == 2


def test_read_errors_are_not_cached(tmp_path):
    broker = ToolBroker(tmp_path, PolicyEngine())
    calls = 0
    original = broker._tool_registry["read_file"]

    async def counted(**kwargs):
        nonlocal calls
        calls += 1
        return await original(**kwargs)

    broker._tool_registry["read_file"] = counted

    async def exercise():
        missing = await broker.execute(
            _task(), "read_file", {"path": "created-later.txt"}
        )
        (tmp_path / "created-later.txt").write_text("ready", encoding="utf-8")
        available = await broker.execute(
            _task(), "read_file", {"path": "created-later.txt"}
        )
        return missing, available

    missing, available = asyncio.run(exercise())

    assert "error" in missing
    assert available["content"] == "ready"
    assert "cache_hit" not in available
    assert calls == 2
