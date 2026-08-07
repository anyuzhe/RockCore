"""Regression tests for platform-specific project root path handling."""

from pathlib import Path

from orchestrator.policy_engine import PolicyEngine
from tools.file_tools import FileTools
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
