"""Regression coverage for Windows packaged runtime compatibility."""

import asyncio
import json
import sys
from pathlib import Path

import app.paths as app_paths
import app.subprocess_utils as subprocess_utils
import providers.codex_provider as codex_module
from app.subprocess_utils import (
    command_basename,
    decode_process_output,
    quote_command_arg,
    run_process,
)
from memory.context_manager import ContextManager
from memory.project_memory import ProjectMemory
from orchestrator.test_manager import TestManager
from providers.codex_provider import (
    CodexProvider,
    _find_codex_binary,
    _prepare_codex_command,
)
from tools.file_tools import FileTools


def test_stale_install_directory_is_migrated_to_user_workspace(
    tmp_path, monkeypatch
):
    install_dir = tmp_path / "Program Files" / "Rock Innovation" / "RockCore"
    install_dir.mkdir(parents=True)
    fallback = tmp_path / "用户" / "RockCore Projects"
    fallback.mkdir(parents=True)
    monkeypatch.setattr(app_paths, "default_workspace_dir", lambda: fallback)

    resolved = app_paths.resolve_working_dir(
        install_dir, install_dir=install_dir
    )

    assert resolved == fallback
    assert resolved != install_dir


def test_unicode_user_workspace_remains_valid(tmp_path):
    workspace = tmp_path / "项目资料" / "源代码"
    workspace.mkdir(parents=True)

    assert app_paths.resolve_working_dir(workspace) == workspace.resolve()
    assert app_paths.is_usable_project_dir(workspace)


def test_windows_appdata_path_supports_chinese_user_names(tmp_path, monkeypatch):
    appdata = tmp_path / "用户资料" / "Roaming"
    monkeypatch.setattr(app_paths.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert app_paths.app_data_dir() == (appdata / "RockCore").resolve()


def test_read_only_project_metadata_falls_back_to_user_data(
    tmp_path, monkeypatch
):
    project = tmp_path / "installed-project"
    project.mkdir()
    user_data = tmp_path / "user-data"
    real_writable_check = app_paths.is_writable_directory
    preferred = project / ".ai"

    def writable(path, *, create=False):
        if Path(path) == preferred:
            return False
        return real_writable_check(path, create=create)

    monkeypatch.setattr(app_paths, "is_writable_directory", writable)
    monkeypatch.setattr(app_paths, "app_data_dir", lambda: user_data)

    context = ContextManager(str(project))
    asyncio.run(context.initialize())

    assert context.state_dir.is_relative_to(user_data)
    assert context.repo_map.map_path.exists()
    assert not preferred.exists()


def test_project_memory_round_trips_utf8_on_windows_style_paths(tmp_path):
    project = tmp_path / "工程"
    state = tmp_path / "用户数据" / ".ai"
    project.mkdir()
    state.mkdir(parents=True)
    memory = ProjectMemory(str(project), state_dir=state)

    memory.write_memory("project", "中文项目：字符集兼容 ✓")

    assert "中文项目：字符集兼容 ✓" in memory.read_memory("project")


def test_subprocess_output_decodes_utf8_and_windows_chinese_codepage():
    assert decode_process_output("拒绝访问".encode("gb18030")) == "拒绝访问"
    result = run_process(
        [sys.executable, "-c", "print('中文输出 ✓')"],
        capture_output=True,
    )
    assert result.returncode == 0
    assert "中文输出 ✓" in result.stdout


def test_synchronous_windows_processes_do_not_flash_console(monkeypatch):
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess_utils.subprocess.CompletedProcess(
            command, 0, stdout=b"ok", stderr=b""
        )

    monkeypatch.setattr(subprocess_utils.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess_utils.subprocess, "CREATE_NO_WINDOW", 0x08000000,
        raising=False,
    )
    monkeypatch.setattr(subprocess_utils.subprocess, "run", fake_run)

    result = subprocess_utils.run_process(["git", "status"], capture_output=True)

    assert result.stdout == "ok"
    assert captured["kwargs"]["creationflags"] == 0x08000000


def test_windows_executable_paths_are_recognized_as_allowed_commands():
    command = '"C:\\Python 311\\python.exe" -m pytest -q'

    assert command_basename(command, platform="win32") == "python"
    assert TestManager._is_shell_command(command)


def test_windows_command_arguments_quote_paths_with_spaces():
    quoted = quote_command_arg(
        r"C:\Program Files\Python 311\python.exe", platform="win32"
    )

    assert quoted == r'"C:\Program Files\Python 311\python.exe"'


def test_gb18030_project_file_is_read_without_mojibake(tmp_path):
    source = tmp_path / "旧项目.txt"
    source.write_bytes("中文内容：兼容 Windows".encode("gb18030"))
    tools = FileTools(tmp_path)

    result = asyncio.run(tools.read_file("旧项目.txt"))

    assert result["content"] == "中文内容：兼容 Windows"
    assert result["encoding"] == "gb18030"


def test_patch_preserves_legacy_windows_file_encoding(tmp_path):
    source = tmp_path / "配置.txt"
    source.write_bytes("标题=旧值\r\n说明=中文\r\n".encode("gb18030"))
    tools = FileTools(tmp_path)

    result = asyncio.run(tools.apply_patch("配置.txt", "旧值", "新值"))

    assert result["status"] == "patched"
    assert result["encoding"] == "gb18030"
    assert source.read_bytes().decode("gb18030") == "标题=新值\r\n说明=中文\r\n"


def test_failed_legacy_encoding_write_does_not_truncate_file(tmp_path):
    source = tmp_path / "配置.txt"
    original = "Résumé=ancien\r\n".encode("cp1252")
    source.write_bytes(original)
    tools = FileTools(tmp_path)

    result = asyncio.run(
        tools.write_file("配置.txt", "Résumé=中文 😀\r\n")
    )

    assert result["status"] == "encoding_error"
    assert source.read_bytes() == original


def test_codex_finds_npm_cmd_launcher_without_gui_path(tmp_path):
    appdata = tmp_path / "AppData" / "Roaming"
    launcher = appdata / "npm" / "codex.cmd"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("@echo off\r\n", encoding="utf-8")

    found = _find_codex_binary({"APPDATA": str(appdata), "PATH": ""})

    assert found == str(launcher)


def test_codex_cmd_launcher_is_wrapped_with_comspec():
    command = _prepare_codex_command(
        r"C:\Users\测试\AppData\Roaming\npm\codex.cmd",
        ["login", "status"],
        environ={"COMSPEC": r"C:\Windows\System32\cmd.exe"},
        platform="win32",
    )

    assert command[:4] == [
        r"C:\Windows\System32\cmd.exe", "/d", "/s", "/c"
    ]
    assert "codex.cmd" in command[4]
    assert "login" in command[4]


def test_codex_uses_popen_instead_of_unsupported_asyncio_subprocess_on_windows(
    tmp_path, monkeypatch
):
    captured = {}

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            captured["timeout"] = timeout
            return b'{"type":"turn.completed"}\n', "完成".encode("gb18030")

        def kill(self):
            captured["killed"] = True

    async def forbidden_async_subprocess(*_args, **_kwargs):
        raise AssertionError("Windows qasync must not use asyncio subprocesses")

    monkeypatch.setattr(codex_module.sys, "platform", "win32")
    monkeypatch.setattr(codex_module.subprocess, "Popen", Process)
    monkeypatch.setattr(
        codex_module.asyncio,
        "create_subprocess_exec",
        forbidden_async_subprocess,
    )

    provider = CodexProvider.__new__(CodexProvider)
    provider.codex_binary = r"C:\Users\测试\npm\codex.cmd"
    provider._process_environment = {
        "COMSPEC": r"C:\Windows\System32\cmd.exe",
        "PYTHONUTF8": "1",
    }
    provider.cli_timeout = 321

    stdout, stderr, returncode = asyncio.run(provider._run_codex_exec(
        "你好，Codex",
        cwd=str(tmp_path),
        sandbox_mode="read-only",
    ))

    assert returncode == 0
    assert "turn.completed" in stdout
    assert stderr == "完成"
    assert captured["input"] == "你好，Codex".encode("utf-8")
    assert captured["timeout"] == 321
    assert captured["command"][0].lower().endswith("cmd.exe")


def test_settings_file_is_saved_as_utf8(tmp_path, monkeypatch):
    from app.ui import settings_dialog

    config_path = tmp_path / "配置" / "config.json"
    monkeypatch.setattr(settings_dialog, "CONFIG_PATH", config_path)
    settings_dialog.save_config({"working_dir": r"C:\用户\项目"})

    raw = config_path.read_bytes()
    assert "用户".encode("utf-8") in raw
    assert settings_dialog.load_config()["working_dir"] == r"C:\用户\项目"
