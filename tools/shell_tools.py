"""Shell command execution tools with security restrictions."""

import asyncio
import logging
import os
import subprocess
import sys
import threading
from typing import Any

from app.subprocess_utils import (
    command_basename,
    decode_process_output,
    no_window_creation_flags,
    terminate_process_tree,
    utf8_environment,
)
from app.python_validation import run_embedded_python_command

logger = logging.getLogger(__name__)


class ShellTools:
    """Restricted shell command execution."""

    def __init__(self, project_root: str | os.PathLike[str] | None,
                 allowed_commands: set[str] | None = None):
        project_root = os.fspath(project_root) if project_root is not None else ""
        if not project_root or not project_root.strip():
            project_root = os.getcwd()
            logger.warning(f"ShellTools: empty project_root, falling back to cwd: {project_root}")
        self.project_root = project_root
        self.temp_directory = ""
        self.allowed_commands = allowed_commands or {
            "pytest", "npm", "pnpm", "yarn", "python", "python3",
            "py",
            "cmake", "ctest", "make", "ruff", "flake8", "black",
            "eslint", "tsc", "vitest", "jest", "mypy",
            "git", "pip", "pip3", "poetry", "cargo", "go",
            "node", "deno", "bun", "echo", "cat", "ls", "head", "tail",
            "ruff", "mypy", "black", "isort",
        }

    def set_temp_directory(self, path: str | os.PathLike[str]) -> None:
        """Route subprocess-managed temporary files into the task runtime."""
        value = os.fspath(path)
        os.makedirs(value, exist_ok=True)
        self.temp_directory = value

    def _command_environment(self) -> dict[str, str]:
        environment = utf8_environment()
        if self.temp_directory:
            environment.update({
                "TMPDIR": self.temp_directory,
                "TEMP": self.temp_directory,
                "TMP": self.temp_directory,
            })
        return environment

    async def run_command(self, command: str, timeout: int = 120,
                          max_output: int = 10000) -> dict:
        """Run a shell command with restrictions."""
        if not command or not command.strip():
            return {
                "error": "Empty command",
                "status": "rejected",
                "stdout": "",
                "stderr": "Command is empty",
                "return_code": -1,
            }
        base_cmd = command_basename(command)
        if base_cmd not in self.allowed_commands:
            return {
                "error": f"Command not allowed: {base_cmd}",
                "status": "rejected",
                "stdout": "",
                "stderr": "Command not in allowed list",
                "return_code": -1,
            }

        embedded_cancel = threading.Event()
        try:
            embedded = await asyncio.to_thread(
                run_embedded_python_command, command, self.project_root,
                timeout=timeout,
                cancel_event=embedded_cancel,
            )
            if embedded is not None:
                return {
                    "stdout": str(embedded.stdout or "")[:max_output],
                    "stderr": str(embedded.stderr or "")[:max_output],
                    "return_code": int(embedded.returncode),
                    "status": (
                        "success" if embedded.returncode == 0 else "failed"
                    ),
                    "runtime": "rockcore_embedded_python",
                }
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.project_root,
                    env=self._command_environment(),
                    creationflags=no_window_creation_flags(),
                )
                try:
                    stdout, stderr = await asyncio.to_thread(
                        proc.communicate, timeout=timeout
                    )
                except asyncio.CancelledError:
                    terminate_process_tree(proc)
                    await asyncio.to_thread(proc.communicate)
                    raise
                except subprocess.TimeoutExpired:
                    terminate_process_tree(proc)
                    await asyncio.to_thread(proc.communicate)
                    return {
                        "error": f"Command timed out ({timeout}s)",
                        "status": "timeout",
                        "stdout": "",
                        "stderr": "",
                        "return_code": -1,
                    }
                return {
                    "stdout": decode_process_output(stdout)[:max_output],
                    "stderr": decode_process_output(stderr)[:max_output],
                    "return_code": proc.returncode or 0,
                    "status": "success" if proc.returncode == 0 else "failed",
                }

            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
                env=self._command_environment(),
                start_new_session=(sys.platform != "win32"),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                terminate_process_tree(proc)
                await proc.communicate()
                return {
                    "error": f"Command timed out ({timeout}s)",
                    "status": "timeout",
                    "stdout": "",
                    "stderr": "",
                    "return_code": -1,
                }
            except asyncio.CancelledError:
                terminate_process_tree(proc)
                await proc.communicate()
                raise

            stdout_str = decode_process_output(stdout)[:max_output]
            stderr_str = decode_process_output(stderr)[:max_output]

            return {
                "stdout": stdout_str,
                "stderr": stderr_str,
                "return_code": proc.returncode or 0,
                "status": "success" if proc.returncode == 0 else "failed",
            }
        except asyncio.CancelledError:
            embedded_cancel.set()
            # The isolated validation child observes this event within its
            # polling interval and is terminated as a process tree.
            await asyncio.sleep(0.3)
            raise
        except Exception as e:
            logger.error(f"Command execution error: {e}")
            return {
                "error": str(e),
                "status": "error",
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
            }
