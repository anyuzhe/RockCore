"""Shell command execution tools with security restrictions."""

import asyncio
import logging
import os
import subprocess
import sys
from typing import Any

from app.subprocess_utils import (
    command_basename,
    decode_process_output,
    no_window_creation_flags,
    utf8_environment,
)

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
        self.allowed_commands = allowed_commands or {
            "pytest", "npm", "pnpm", "yarn", "python", "python3",
            "cmake", "ctest", "make", "ruff", "flake8", "black",
            "eslint", "tsc", "vitest", "jest", "mypy",
            "git", "pip", "pip3", "poetry", "cargo", "go",
            "node", "deno", "bun", "echo", "cat", "ls", "head", "tail",
            "ruff", "mypy", "black", "isort",
        }

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

        try:
            if sys.platform == "win32":
                proc = subprocess.Popen(
                    command,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.project_root,
                    env=utf8_environment(),
                    creationflags=no_window_creation_flags(),
                )
                try:
                    stdout, stderr = await asyncio.to_thread(
                        proc.communicate, timeout=timeout
                    )
                except asyncio.CancelledError:
                    proc.kill()
                    await asyncio.to_thread(proc.communicate)
                    raise
                except subprocess.TimeoutExpired:
                    proc.kill()
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
                env=utf8_environment(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return {
                    "error": f"Command timed out ({timeout}s)",
                    "status": "timeout",
                    "stdout": "",
                    "stderr": "",
                    "return_code": -1,
                }

            stdout_str = decode_process_output(stdout)[:max_output]
            stderr_str = decode_process_output(stderr)[:max_output]

            return {
                "stdout": stdout_str,
                "stderr": stderr_str,
                "return_code": proc.returncode or 0,
                "status": "success" if proc.returncode == 0 else "failed",
            }
        except Exception as e:
            logger.error(f"Command execution error: {e}")
            return {
                "error": str(e),
                "status": "error",
                "stdout": "",
                "stderr": str(e),
                "return_code": -1,
            }
