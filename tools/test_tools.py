"""Test execution tools for the AI worker."""

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class TestTools:
    """Test execution and validation tools."""

    __test__ = False

    def __init__(self, project_root: str):
        self.project_root = project_root

    async def run_tests(self, command: str = "pytest", timeout: int = 300) -> dict:
        """Run tests and return results."""
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.project_root,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                return {
                    "status": "timeout",
                    "output": f"Tests timed out ({timeout}s)",
                    "passed": 0, "failed": 0, "skipped": 0,
                }

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
        except Exception as e:
            logger.error(f"Test execution error: {e}")
            return {
                "status": "error",
                "output": str(e),
                "passed": 0, "failed": 0, "skipped": 0,
                "return_code": -1,
            }
