"""Test execution tools for the AI worker."""

import asyncio
import logging
import sys
import threading
from typing import Any

from app.python_validation import run_embedded_python_command
from app.subprocess_utils import terminate_process_tree

logger = logging.getLogger(__name__)


class TestTools:
    """Test execution and validation tools."""

    __test__ = False

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def run_tests(self, command: str = "pytest", timeout: int = 300) -> dict:
        """Run tests and return results."""
        embedded_cancel = threading.Event()
        try:
            embedded = await asyncio.wait_for(
                asyncio.to_thread(
                    run_embedded_python_command, command, self.project_root,
                    timeout=max(1, timeout - 1),
                    cancel_event=embedded_cancel,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            embedded_cancel.set()
            await asyncio.sleep(0.3)
            return {
                "status": "timeout",
                "output": f"Tests timed out ({timeout}s)",
                "passed": 0, "failed": 0, "skipped": 0,
            }
        except asyncio.CancelledError:
            embedded_cancel.set()
            await asyncio.sleep(0.3)
            raise
        try:
            if embedded is not None:
                stdout_str = str(embedded.stdout or "")
                stderr_str = str(embedded.stderr or "")
                output = stdout_str + ("\n" + stderr_str if stderr_str else "")
                return {
                    "status": "passed" if embedded.returncode == 0 else "failed",
                    "output": output[:5000],
                    "passed": 1 if embedded.returncode == 0 else 0,
                    "failed": 0 if embedded.returncode == 0 else 1,
                    "skipped": 0,
                    "return_code": int(embedded.returncode),
                    "runtime": "rockcore_embedded_python",
                }
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
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
                    "status": "timeout",
                    "output": f"Tests timed out ({timeout}s)",
                    "passed": 0, "failed": 0, "skipped": 0,
                }
            except asyncio.CancelledError:
                terminate_process_tree(proc)
                await proc.communicate()
                raise

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace")
            output = stdout_str + "\n" + stderr_str

            passed = stdout_str.count("passed") if proc.returncode == 0 else 0
            failed = stdout_str.count("FAILED") if proc.returncode != 0 else 0
            skipped = stdout_str.count("skipped")

            return {
                "status": "passed" if proc.returncode == 0 else "failed",
                "output": output[:5000],
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "return_code": proc.returncode or 0,
            }
        except asyncio.CancelledError:
            embedded_cancel.set()
            await asyncio.sleep(0.3)
            raise
        except Exception as e:
            logger.error(f"Test execution error: {e}")
            return {
                "status": "error",
                "output": str(e),
                "passed": 0, "failed": 0, "skipped": 0,
                "return_code": -1,
            }
