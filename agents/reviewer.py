"""Codex Reviewer Agent — reviews code changes in read-only mode via Codex SDK."""

import json
import logging
import re
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

        project_root = job.project.root_path if job.project else "."
        diff, changed_files = self._collect_job_changes(project_root, job.job_id)

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
            review = await self._request_review(messages)

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

    async def _request_review(self, messages: list[dict]) -> dict:
        """Retry malformed output, then use a configured alternate reviewer."""
        attempts = [
            (None, messages),
            (None, messages + [{
                "role": "user",
                "content": (
                    "Your previous response was empty or invalid. Return the review "
                    "now as one valid JSON object only, with no Markdown fences or prose."
                ),
            }]),
        ]
        if self.model_router.has_provider("kimi"):
            attempts.append(("kimi", messages + [{
                "role": "user",
                "content": "Return exactly one valid JSON review object.",
            }]))

        failures = []
        for provider_override, attempt_messages in attempts:
            provider_name = provider_override or "codex"
            try:
                response = await self.model_router.chat(
                    self.agent_type,
                    REVIEWER_SYSTEM_PROMPT,
                    attempt_messages,
                    provider_override=provider_override,
                    response_format={"type": "json_object"},
                )
                content = response.get("content", "")
                return self._parse_json(content)
            except Exception as exc:
                failures.append(f"{provider_name}: {exc}")
                logger.warning(
                    "Reviewer attempt failed via %s: %s",
                    provider_name, exc,
                )

        raise RuntimeError(
            "审核模型未返回有效 JSON；已重试 Codex"
            + (" 并尝试 Kimi 备用审核" if self.model_router.has_provider("kimi") else "")
            + "。" + "；".join(failures)[:500]
        )

    @staticmethod
    def _collect_job_changes(project_root: str, job_id: str) -> tuple[str, list[str]]:
        """Collect committed patches for this job, with worktree diff as fallback."""
        import subprocess

        try:
            commit_result = subprocess.run(
                [
                    "git", "log", "--reverse", "--format=%H", "--fixed-strings",
                    f"--grep=AI {job_id}:",
                ],
                capture_output=True, text=True, cwd=project_root, timeout=10,
            )
            commits = [line.strip() for line in commit_result.stdout.splitlines() if line.strip()]
            if commit_result.returncode == 0 and commits:
                patches = []
                changed_files = set()
                for commit in commits:
                    show = subprocess.run(
                        ["git", "show", "--format=medium", "--stat", "--patch", commit],
                        capture_output=True, text=True, cwd=project_root, timeout=10,
                    )
                    if show.stdout.strip():
                        patches.append(show.stdout.strip())
                    names = subprocess.run(
                        ["git", "show", "--format=", "--name-only", commit],
                        capture_output=True, text=True, cwd=project_root, timeout=10,
                    )
                    changed_files.update(
                        line.strip() for line in names.stdout.splitlines() if line.strip()
                    )
                return "\n\n".join(patches) or "(no changes)", sorted(changed_files)

            diff_result = subprocess.run(
                ["git", "diff"], capture_output=True, text=True,
                cwd=project_root, timeout=10,
            )
            names_result = subprocess.run(
                ["git", "diff", "--name-only"], capture_output=True, text=True,
                cwd=project_root, timeout=10,
            )
            changed_files = [
                line.strip() for line in names_result.stdout.splitlines() if line.strip()
            ]
            return diff_result.stdout or "(no changes)", changed_files
        except Exception as exc:
            return f"(error getting job changes: {exc})", []

    def _parse_json(self, content: str) -> dict:
        content = (content or "").strip()
        if not content:
            raise ValueError("模型返回了空响应")
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # Some providers prepend one short sentence despite the JSON-only
            # instruction. Extract the outer object without accepting prose as data.
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise ValueError("响应中没有 JSON 对象")
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("审核响应必须是 JSON 对象")
        return parsed
