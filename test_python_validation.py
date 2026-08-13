"""Tests for the Python validation runtime embedded in packaged RockCore."""

import asyncio

from app.python_validation import run_embedded_python_command
from tools.shell_tools import ShellTools
from tools.test_tools import TestTools


def test_embedded_py_compile_validates_without_creating_bytecode(tmp_path):
    source = tmp_path / "main.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")

    result = run_embedded_python_command(
        "python -m py_compile main.py", tmp_path
    )

    assert result is not None
    assert result.returncode == 0
    assert "内置 Python 语法验收通过" in result.stdout
    assert not (tmp_path / "__pycache__").exists()


def test_embedded_py_compile_reports_syntax_errors(tmp_path):
    (tmp_path / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    result = run_embedded_python_command(
        "py -3 -m py_compile broken.py", tmp_path
    )

    assert result is not None
    assert result.returncode == 1
    assert "broken.py" in result.stderr


def test_embedded_unittest_runs_standard_library_tests(tmp_path):
    (tmp_path / "main.py").write_text(
        "def add(left, right):\n    return left + right\n", encoding="utf-8"
    )
    (tmp_path / "test_main.py").write_text(
        "import unittest\n"
        "from main import add\n\n"
        "class MainTests(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )

    result = run_embedded_python_command(
        "python -m unittest -v test_main.py", tmp_path
    )

    assert result is not None
    assert result.returncode == 0
    assert "test_add" in result.stdout
    assert "OK" in result.stdout


def test_embedded_pytest_runs_without_host_python(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "def test_answer():\n    assert 6 * 7 == 42\n",
        encoding="utf-8",
    )

    result = run_embedded_python_command(
        "python -m pytest -q test_sample.py", tmp_path, timeout=20
    )

    assert result is not None
    assert result.returncode == 0
    assert "RockCore 内置 Python pytest 验收" in result.stdout
    assert "1 passed" in result.stdout
    assert not (tmp_path / ".pytest_cache").exists()
    assert not (tmp_path / "__pycache__").exists()


def test_direct_pytest_command_uses_embedded_runtime(tmp_path):
    (tmp_path / "test_direct.py").write_text(
        "def test_direct():\n    assert True\n", encoding="utf-8"
    )

    result = run_embedded_python_command(
        "pytest -q test_direct.py", tmp_path, timeout=20
    )

    assert result is not None
    assert result.returncode == 0
    assert "1 passed" in result.stdout


def test_embedded_python_runs_project_script(tmp_path):
    (tmp_path / "check.py").write_text(
        "print('项目脚本验收通过')\n", encoding="utf-8"
    )

    result = run_embedded_python_command("python check.py", tmp_path, timeout=10)

    assert result is not None
    assert result.returncode == 0
    assert "项目脚本验收通过" in result.stdout


def test_python_shell_chain_is_rejected_without_host_fallback(tmp_path):
    result = run_embedded_python_command(
        "python -m pytest -q && echo unsafe", tmp_path
    )

    assert result is not None
    assert result.returncode == 2
    assert "不接受 shell 组合操作" in result.stderr


def test_shell_tool_uses_embedded_python_when_system_python_is_irrelevant(tmp_path):
    (tmp_path / "main.py").write_text("value = '中文'\n", encoding="utf-8")
    tool = ShellTools(tmp_path)

    result = asyncio.run(tool.run_command("python -m py_compile main.py"))

    assert result["status"] == "success"
    assert result["runtime"] == "rockcore_embedded_python"


def test_test_tool_uses_embedded_unittest(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "import unittest\n\n"
        "class SampleTests(unittest.TestCase):\n"
        "    def test_true(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    tool = TestTools(str(tmp_path))

    result = asyncio.run(
        tool.run_tests("python -m unittest discover -v", timeout=10)
    )

    assert result["status"] == "passed"
    assert result["runtime"] == "rockcore_embedded_python"


def test_test_tool_uses_embedded_pytest(tmp_path):
    (tmp_path / "test_sample.py").write_text(
        "def test_true():\n    assert True\n", encoding="utf-8"
    )
    tool = TestTools(str(tmp_path))

    result = asyncio.run(tool.run_tests("pytest -q test_sample.py", timeout=20))

    assert result["status"] == "passed"
    assert result["runtime"] == "rockcore_embedded_python"
