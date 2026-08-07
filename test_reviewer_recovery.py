"""Reviewer response parsing and provider recovery tests."""

import asyncio
import subprocess
from types import SimpleNamespace

from agents.reviewer import ReviewerAgent


class _ReviewRouter:
    def __init__(self, responses, has_kimi=True):
        self.responses = list(responses)
        self.has_kimi = has_kimi
        self.calls = []

    def has_provider(self, name):
        return name == "kimi" and self.has_kimi

    async def chat(self, *_args, **kwargs):
        self.calls.append(kwargs.get("provider_override"))
        return {"content": self.responses.pop(0)}


def test_reviewer_retries_an_empty_codex_response():
    router = _ReviewRouter([
        "",
        '```json\n{"result":"pass","summary":"ok"}\n```',
    ])
    reviewer = ReviewerAgent(router)

    review = asyncio.run(reviewer._request_review([
        {"role": "user", "content": "review"}
    ]))

    assert review["result"] == "pass"
    assert router.calls == [None, None]


def test_reviewer_falls_back_to_kimi_after_invalid_codex_output():
    router = _ReviewRouter([
        "",
        "not json",
        'Review result: {"result":"reject","issues":[]}',
    ])
    reviewer = ReviewerAgent(router)

    review = asyncio.run(reviewer._request_review([
        {"role": "user", "content": "review"}
    ]))

    assert review["result"] == "reject"
    assert router.calls == [None, None, "kimi"]


def test_reviewer_reports_empty_output_clearly_when_all_attempts_fail():
    router = _ReviewRouter(["", ""], has_kimi=False)
    reviewer = ReviewerAgent(router)

    try:
        asyncio.run(reviewer._request_review([
            {"role": "user", "content": "review"}
        ]))
    except RuntimeError as exc:
        assert "模型返回了空响应" in str(exc)
    else:
        raise AssertionError("Expected invalid reviewer output to fail")


def test_reviewer_reads_changes_from_commits_for_the_current_job(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True,
            capture_output=True, text=True,
        )

    git("init")
    (tmp_path / "game.js").write_text("const state = 'old';\n")
    git("add", "game.js")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "Initial project state")
    (tmp_path / "game.js").write_text("const state = 'playing';\n")
    git("add", "game.js")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "AI JOB-20260807-007: T002 - Add game state")

    diff, changed_files = ReviewerAgent._collect_job_changes(
        str(tmp_path), "JOB-20260807-007"
    )

    assert "const state = 'playing'" in diff
    assert changed_files == ["game.js"]
