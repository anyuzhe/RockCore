"""Reviewer response parsing and provider recovery tests."""

import asyncio
import shutil
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


def test_reviewer_never_falls_back_to_planner_model_after_invalid_output():
    router = _ReviewRouter([
        "",
        "not json",
        'Review result: {"result":"reject","issues":[]}',
    ])
    reviewer = ReviewerAgent(router)

    try:
        asyncio.run(reviewer._request_review([
            {"role": "user", "content": "review"}
        ]))
    except RuntimeError as exc:
        assert "未回退到策划模型" in str(exc)
    else:
        raise AssertionError("Expected independent Codex review to fail closed")

    assert router.calls == [None, None]


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


def test_reviewer_uses_final_net_diff_across_multiple_job_commits(tmp_path):
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
    (tmp_path / "game.js").write_text("const state = 'temporary';\n")
    git("add", "game.js")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "AI JOB-NET: T001 - First pass")
    (tmp_path / "game.js").write_text("const state = 'final';\n")
    git("add", "game.js")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "-m", "AI JOB-NET: T002 - Final pass")

    diff, changed_files = ReviewerAgent._collect_job_changes(
        str(tmp_path), "JOB-NET"
    )

    assert "const state = 'final'" in diff
    assert "temporary" not in diff
    assert changed_files == ["game.js"]


def test_reviewer_diff_chunks_preserve_every_original_character():
    diff = (
        "diff --git a/a.js b/a.js\n" + "+const a = 1;\n" * 80
        + "diff --git a/b.js b/b.js\n" + "+const b = 2;\n" * 80
    )

    chunks = ReviewerAgent._split_diff(diff, max_chars=300)
    reconstructed = "".join(
        line
        for chunk in chunks
        for line in chunk.splitlines(keepends=True)
        if not line.startswith("[PARTIAL FILE DIFF")
    )

    assert len(chunks) > 2
    assert reconstructed == diff
    assert any("do not infer EOF" in chunk for chunk in chunks)


def test_reviewer_validates_complete_es_module_instead_of_diff_tail(tmp_path):
    if not shutil.which("node"):
        return
    source = "export const score = `${3}:${0}`;\n"
    (tmp_path / "game.js").write_text(source)

    validation = ReviewerAgent._validate_changed_files(
        str(tmp_path), ["game.js"]
    )

    assert validation == "- game.js: JavaScript syntax OK"


def test_reviewer_suppresses_chunk_boundary_syntax_false_positive():
    review = {
        "result": "reject",
        "severity": "high",
        "summary": "diff looks truncated",
        "issues": [{
            "file": "game.js",
            "line": 100,
            "problem": "Unclosed template literal at the truncated diff boundary",
            "severity": "high",
        }],
        "constraint_violations": [],
        "suggested_actions": [],
    }

    normalized = ReviewerAgent._suppress_false_syntax_issues(
        review, "- game.js: JavaScript syntax OK"
    )

    assert normalized["result"] == "pass"
    assert normalized["severity"] == "low"
    assert normalized["issues"] == []


def test_reviewer_keeps_non_syntax_findings_after_full_file_validation():
    review = {
        "result": "reject",
        "severity": "high",
        "summary": "security issue",
        "issues": [{
            "file": "game.js",
            "line": 10,
            "problem": "Unescaped user input is assigned to innerHTML",
            "severity": "high",
        }],
        "constraint_violations": [],
        "suggested_actions": [],
    }

    normalized = ReviewerAgent._suppress_false_syntax_issues(
        review, "- game.js: JavaScript syntax OK"
    )

    assert normalized == review
