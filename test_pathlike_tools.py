"""Regression tests for platform-specific project root path handling."""

import asyncio
import os
from pathlib import Path

from orchestrator.policy_engine import PolicyEngine
from tools.file_tools import FileTools
from tools.search_tools import SearchTools
from tools.shell_tools import ShellTools
from tools.tool_broker import ToolBroker


def test_tools_accept_pathlib_project_roots(tmp_path):
    root = Path(tmp_path)

    file_tools = FileTools(root)
    shell_tools = ShellTools(root)
    broker = ToolBroker(root, PolicyEngine())

    assert file_tools.project_root == root.resolve()
    assert shell_tools.project_root == str(root)
    assert broker.project_root == str(root)


def test_tool_broker_normalizes_pathlike_roots_when_switched(tmp_path):
    broker = ToolBroker(str(tmp_path), PolicyEngine())
    new_root = Path(tmp_path) / "project"
    new_root.mkdir()

    broker.set_project_root(new_root)

    assert broker.project_root == str(new_root)
    assert broker.file_tools.project_root == new_root.resolve()
    assert broker.shell_tools.project_root == str(new_root)


def test_read_versions_change_after_same_size_file_rewrite(tmp_path):
    source = tmp_path / "game.js"
    source.write_text("old\n", encoding="utf-8")
    file_tools = FileTools(tmp_path)
    search_tools = SearchTools(tmp_path)

    first_read = asyncio.run(file_tools.read_file("game.js"))
    first_listing = asyncio.run(file_tools.list_files("."))
    first_search = asyncio.run(search_tools.search_code("old"))

    original = source.stat()
    source.write_text("new\n", encoding="utf-8")
    os.utime(
        source,
        ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000),
    )

    second_read = asyncio.run(file_tools.read_file("game.js"))
    second_listing = asyncio.run(file_tools.list_files("."))
    second_search = asyncio.run(search_tools.search_code("new"))

    assert first_read["source_version"] != second_read["source_version"]
    assert first_listing["source_version"] != second_listing["source_version"]
    assert first_search["source_version"] != second_search["source_version"]


def test_search_marks_capped_results_as_truncated(tmp_path):
    (tmp_path / "game.js").write_text(
        "match one\nmatch two\n", encoding="utf-8"
    )

    result = asyncio.run(
        SearchTools(tmp_path).search_code("match", max_results=1)
    )

    assert result["count"] == 1
    assert result["truncated"] is True
    assert result["source_version"]
