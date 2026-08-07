"""Codex Reviewer Agent — reviews code changes in read-only mode via Codex SDK."""

import json
import logging
from typing import Any

from orchestrator.model_router import ModelRouter

logger = logging.getLogger(__name__)

REVIEWER_SYSTEM_PROMPT = """You are the Reviewer agent in an AI Engineering Studio.
You operate through the Codex SDK in read-only sandbox mode.

Your role is to review code changes for quality, correctness, and constraint compliance.

You have READ-ONLY access. You must review and report findings.

Check for:
1. Does the code correctly implement the requirements?
2. Are there any bugs or edge cases?
3. Are there any security issues?
4. Does the code respect the project's conventions?
5. Are there any constraint violations?
6. Are tests adequate?

Output ONLY valid JSON with this structure:
{
  "result": "pass|reject",
  "severity": "low|medium|high",
  "summary": "Brief summary of findings",
  "issues": [
    {
      "file": "path/to/file.py",
      "line": 123,
      "problem": "Description of the issue",
      "severity": "low|medium|high"
    }
  ],
  "constraint_violations": [],
  "suggested_actions": ["action1", "action2"]
}
"""


class ReviewerAgent:
    """Codex Reviewer: read-only code review via Codex SDK."""

    def __init__(self, model_router: ModelRouter):
        self.model_router = model_router
        self.agent_type = "reviewer"

    async def run(self, job) -> dict:
        """Run a review on the job's changes."""
        logger.info(f"Reviewer (Codex): reviewing job {job.job_id}")

        try:
            import subprocess
            result = subprocess.run(
                ["git", "diff"],
                capture_output=True, text=True,
                cwd=job.project.root_path if job.project else ".",
            )
            diff = result.stdout or "(no changes)"

            result2 = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True,
                cwd=job.project.root_path if job.project else ".",
            )
            changed_files = result2.stdout.strip().split("\n") if result2.stdout else []
        except Exception as e:
            diff = f"(error getting diff: {e})"
            changed_files = []

        messages = [
            {
                "role": "user",
                "content": f"""Review the following changes for job {job.job_id}.

User Request: {job.user_request}

Changed Files: {json.dumps(changed_files)}

Git Diff:
```diff
{diff[:10000]}
```

Review the changes carefully. Check for bugs, security issues, and constraint violations.
Output ONLY valid JSON."""
            }
        ]

        try:
            response = await self.model_router.chat(
                self.agent_type,
                REVIEWER_SYSTEM_PROMPT,
                messages,
            )

            content = response.get("content", "{}")
            review = self._parse_json(content)

            review.setdefault("result", "pass")
            review.setdefault("severity", "low")
            review.setdefault("summary", "Review completed")
            review.setdefault("issues", [])
            review.setdefault("constraint_violations", [])
            review.setdefault("suggested_actions", [])

            logger.info(f"Reviewer: result={review['result']}, "
                        f"issues={len(review['issues'])}")
            return review

        except Exception as e:
            logger.error(f"Reviewer (Codex) failed: {e}")
            raise

    def _parse_json(self, content: str) -> dict:
        content = content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        return json.loads(content)
