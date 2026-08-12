"""Codex Reviewer Agent — reviews code changes in read-only mode via Codex SDK."""

import ast
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from app.subprocess_utils import run_process
from app.text_utils import read_text_compatible
from orchestrator.model_router import ModelRouter
from orchestrator.cost_engine import BudgetExceededError

logger = logging.getLogger(__name__)

REVIEWER_SYSTEM_PROMPT = """You are the Reviewer agent in an AI Engineering Studio.
You operate through the Codex SDK in read-only sandbox mode.

Your role is to review code changes for quality, correctness, and constraint compliance.

Review independently. Do not assume the Planner's proposed files or approach
were complete or correct, and do not trust the Worker's completion claim.

You have READ-ONLY access. You must review and report findings.

Check for:
1. Does the code correctly implement the requirements?
2. Are there any bugs or edge cases?
3. Are there any security issues?
4. Does the code respect the project's conventions?
5. Are there any constraint violations?
6. Are tests adequate?
7. Were any required files or observable behaviors omitted?
8. Did the change unintentionally alter a public API or compatibility contract?
9. Are thread safety, cleanup/resource ownership, error paths, and incomplete
   states handled correctly where relevant?
10. Do tests actually exercise the changed behavior rather than merely exist?
11. Treat deterministic validation of complete files as authoritative. A diff
   chunk may end mid-function; never report truncated/unclosed syntax merely
   because a displayed diff fragment ends there.

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

    def __init__(self, model_router: ModelRouter, skill_manager=None):
        self.model_router = model_router
        self.agent_type = "reviewer"
        self.skill_manager = skill_manager

    async def run(self, job) -> dict:
        """Run a review on the job's changes."""
        logger.info(f"Reviewer (Codex): reviewing job {job.job_id}")

        project_root = job.project.root_path if job.project else "."
        diff, changed_files = self._collect_job_changes(project_root, job.job_id)
        validation = self._validate_changed_files(project_root, changed_files)
        workflow_context = str(
            getattr(job, "_rockcore_review_context", "") or ""
        )[:16000]
        chunks = self._split_diff(diff)
        reviews = []
        for index, chunk in enumerate(chunks, start=1):
            messages = [
                {
                    "role": "user",
                    "content": f"""Review the following changes for job {job.job_id}.

User Request: {job.user_request}

Changed Files: {json.dumps(changed_files)}

Deterministic validation of complete final files:
{validation}

Authoritative workflow context (constitution, plan coverage, task outcomes,
tests, and recovery history):
{workflow_context or "(not available)"}

Git Diff Chunk: {index}/{len(chunks)}
```diff
{chunk}
```

Review the changes carefully. Check for bugs, security issues, and constraint violations.
This may be one chunk of a larger diff. Do not infer a syntax error from the
chunk boundary. Only report syntax errors confirmed by deterministic validation.
Output ONLY valid JSON."""
                }
            ]

            try:
                chunk_review = await self._request_review(
                    messages, project_root=project_root,
                    attachments=getattr(job, "attachments", None) or [],
                )
                reviews.append(self._suppress_false_syntax_issues(
                    chunk_review, validation
                ))
            except Exception as e:
                logger.error(f"Reviewer (Codex) failed: {e}")
                raise

        review = self._merge_reviews(reviews)
        review.setdefault("result", "pass")
        review.setdefault("severity", "low")
        review.setdefault("summary", "Review completed")
        review.setdefault("issues", [])
        review.setdefault("constraint_violations", [])
        review.setdefault("suggested_actions", [])

        logger.info(f"Reviewer: result={review['result']}, "
                    f"issues={len(review['issues'])}")
        return review

    async def _request_review(
        self, messages: list[dict], project_root: str = ".",
        attachments: list[dict] | None = None,
    ) -> dict:
        """Retry malformed Codex output without falling back to the Planner stack."""
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
        failures = []
        for provider_override, attempt_messages in attempts:
            provider_name = provider_override or "codex"
            try:
                skill_prompt = ""
                if self.skill_manager:
                    body = self.skill_manager.get_body("code-review")
                    if body:
                        skill_prompt = (
                            "\n\nSelected Skill: code-review\n" + body[:12000]
                        )
                response = await self.model_router.chat(
                    self.agent_type,
                    REVIEWER_SYSTEM_PROMPT + skill_prompt,
                    attempt_messages,
                    provider_override=provider_override,
                    allow_provider_fallback=False,
                    response_format={"type": "json_object"},
                    project_root=project_root,
                    attachments=attachments or [],
                )
                content = response.get("content", "")
                return self._parse_json(content)
            except BudgetExceededError:
                raise
            except Exception as exc:
                failures.append(f"{provider_name}: {exc}")
                logger.warning(
                    "Reviewer attempt failed via %s: %s",
                    provider_name, exc,
                )

        raise RuntimeError(
            "独立审核模型未返回有效 JSON；已重试 Codex，未回退到策划模型。"
            + "；".join(failures)[:500]
        )

    @staticmethod
    def _collect_job_changes(project_root: str, job_id: str) -> tuple[str, list[str]]:
        """Collect the final net patch for this job, with worktree diff fallback."""

        try:
            commit_result = run_process(
                [
                    "git", "log", "--reverse", "--format=%H", "--fixed-strings",
                    f"--grep=AI {job_id}:",
                ],
                capture_output=True, text=True, cwd=project_root, timeout=10,
            )
            commits = [line.strip() for line in commit_result.stdout.splitlines() if line.strip()]
            if commit_result.returncode == 0 and commits:
                first_parent = run_process(
                    ["git", "rev-parse", f"{commits[0]}^"],
                    capture_output=True, text=True, cwd=project_root, timeout=10,
                )
                if first_parent.returncode == 0:
                    base = first_parent.stdout.strip()
                    target = commits[-1]
                    diff_result = run_process(
                        ["git", "diff", "--stat", "--patch", base, target, "--"],
                        capture_output=True, text=True, cwd=project_root, timeout=10,
                    )
                    names_result = run_process(
                        ["git", "diff", "--name-only", base, target, "--"],
                        capture_output=True, text=True, cwd=project_root, timeout=10,
                    )
                    changed_files = [
                        line.strip()
                        for line in names_result.stdout.splitlines()
                        if line.strip()
                    ]
                    return diff_result.stdout or "(no changes)", changed_files

                show = run_process(
                    ["git", "show", "--format=", "--stat", "--patch", commits[-1]],
                    capture_output=True, text=True, cwd=project_root, timeout=10,
                )
                names = run_process(
                    ["git", "show", "--format=", "--name-only", commits[-1]],
                    capture_output=True, text=True, cwd=project_root, timeout=10,
                )
                changed_files = [
                    line.strip() for line in names.stdout.splitlines() if line.strip()
                ]
                return show.stdout or "(no changes)", changed_files

            diff_result = run_process(
                ["git", "diff"], capture_output=True, text=True,
                cwd=project_root, timeout=10,
            )
            names_result = run_process(
                ["git", "diff", "--name-only"], capture_output=True, text=True,
                cwd=project_root, timeout=10,
            )
            changed_files = [
                line.strip() for line in names_result.stdout.splitlines() if line.strip()
            ]
            return diff_result.stdout or "(no changes)", changed_files
        except Exception as exc:
            return f"(error getting job changes: {exc})", []

    @staticmethod
    def _split_diff(diff: str, max_chars: int = 45_000) -> list[str]:
        """Split a diff at file/line boundaries without silently cutting text."""
        if len(diff) <= max_chars:
            return [diff]

        sections = [
            section for section in re.split(r"(?=^diff --git )", diff, flags=re.MULTILINE)
            if section
        ]
        chunks: list[str] = []
        current = ""
        for section in sections:
            if len(section) <= max_chars:
                if current and len(current) + len(section) > max_chars:
                    chunks.append(current)
                    current = ""
                current += section
                continue
            if current:
                chunks.append(current)
                current = ""
            lines = section.splitlines(keepends=True)
            part = ""
            for line in lines:
                if part and len(part) + len(line) > max_chars:
                    chunks.append(
                        "[PARTIAL FILE DIFF — continuation follows; do not infer EOF]\n"
                        + part
                    )
                    part = ""
                part += line
            if part:
                chunks.append(
                    "[PARTIAL FILE DIFF — may continue in another chunk; do not infer EOF]\n"
                    + part
                )
        if current:
            chunks.append(current)
        return chunks or ["(no changes)"]

    @staticmethod
    def _validate_changed_files(project_root: str,
                                changed_files: list[str]) -> str:
        """Run syntax checks against complete final files, never diff fragments."""
        root = Path(project_root).resolve()
        results = []
        node = shutil.which("node")
        for relative in changed_files:
            path = (root / relative).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                results.append(f"- {relative}: rejected path outside project")
                continue
            if not path.is_file():
                results.append(
                    f"- {relative}: absent in final workspace "
                    "(deleted or renamed; no syntax check applicable)"
                )
                continue
            try:
                suffix = path.suffix.lower()
                if suffix == ".json":
                    source, _ = read_text_compatible(path)
                    json.loads(source)
                    results.append(f"- {relative}: JSON syntax OK")
                elif suffix == ".py":
                    source, _ = read_text_compatible(path)
                    ast.parse(source, filename=relative)
                    results.append(f"- {relative}: Python syntax OK")
                elif suffix in {".js", ".mjs", ".cjs"} and node:
                    source, _ = read_text_compatible(path)
                    command = [node, "--check"]
                    check_input = None
                    if suffix != ".cjs":
                        # Parse browser scripts and ES modules without inheriting
                        # the repository's Node package type.
                        command = [node, "--input-type=module", "--check"]
                        check_input = source
                    else:
                        command.append(str(path))
                    checked = run_process(
                        command, input=check_input, capture_output=True,
                        text=True, timeout=15,
                    )
                    if checked.returncode == 0:
                        results.append(f"- {relative}: JavaScript syntax OK")
                    else:
                        detail = (checked.stderr or checked.stdout).strip()[:500]
                        results.append(
                            f"- {relative}: JavaScript syntax FAILED: {detail}"
                        )
                else:
                    results.append(f"- {relative}: complete file exists (syntax check not applicable)")
            except Exception as error:
                results.append(f"- {relative}: syntax validation FAILED: {error}")
        return "\n".join(results) if results else "- No changed files detected"

    @staticmethod
    def _merge_reviews(reviews: list[dict]) -> dict:
        """Merge independent diff-chunk reviews into one deterministic result."""
        if not reviews:
            return {"result": "error", "severity": "high", "summary": "No review result"}
        severity_rank = {"low": 0, "medium": 1, "high": 2}
        issues = []
        violations = []
        actions = []
        seen_issues = set()
        for review in reviews:
            for issue in review.get("issues") or []:
                key = json.dumps(issue, sort_keys=True, ensure_ascii=False, default=str)
                if key not in seen_issues:
                    seen_issues.add(key)
                    issues.append(issue)
            violations.extend(review.get("constraint_violations") or [])
            actions.extend(review.get("suggested_actions") or [])
        worst = max(
            reviews,
            key=lambda item: severity_rank.get(item.get("severity", "low"), 0),
        )
        rejected = any(review.get("result") == "reject" for review in reviews)
        summaries = [review.get("summary", "") for review in reviews if review.get("summary")]
        return {
            "result": "reject" if rejected else "pass",
            "severity": worst.get("severity", "low"),
            "summary": " | ".join(summaries)[:2000] or "Review completed",
            "issues": issues,
            "constraint_violations": list(dict.fromkeys(map(str, violations))),
            "suggested_actions": list(dict.fromkeys(map(str, actions))),
        }

    @staticmethod
    def _suppress_false_syntax_issues(review: dict, validation: str) -> dict:
        """Discard syntax claims contradicted by complete-file validation."""
        syntax_ok_files = set()
        for line in validation.splitlines():
            if line.startswith("- ") and line.endswith(" syntax OK"):
                syntax_ok_files.add(line[2:].rsplit(":", 1)[0])
        if not syntax_ok_files:
            return review

        syntax_markers = (
            "syntax", "parse error", "unexpected end", "unterminated",
            "unclosed", "missing closing", "语法", "未闭合", "截断",
        )
        original_issues = list(review.get("issues") or [])
        retained = []
        suppressed = []
        for issue in original_issues:
            problem = str(issue.get("problem") or "").lower()
            if (
                issue.get("file") in syntax_ok_files
                and any(marker in problem for marker in syntax_markers)
            ):
                suppressed.append(issue)
            else:
                retained.append(issue)
        if not suppressed:
            return review

        normalized = dict(review)
        normalized["issues"] = retained
        if (
            review.get("result") == "reject"
            and original_issues
            and not retained
            and not (review.get("constraint_violations") or [])
        ):
            normalized["result"] = "pass"
            normalized["severity"] = "low"
            normalized["summary"] = (
                "完整文件已通过确定性语法检查；忽略了仅由差异分块边界造成的"
                "语法误报。"
            )
        logger.warning(
            "Reviewer suppressed %s syntax issue(s) contradicted by full-file checks",
            len(suppressed),
        )
        return normalized

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
