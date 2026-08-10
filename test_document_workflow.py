"""Regression tests for size-aware long-document processing."""

import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace

from agents.worker import WorkerAgent
from orchestrator.cost_engine import CostEngine
from orchestrator.engine import Engine
from orchestrator.policy_engine import PolicyEngine
from tools.file_tools import FileTools
from tools.tool_broker import ToolBroker


def _task(description="整理 PDF", allowed_paths=None):
    return SimpleNamespace(
        task_id="T001",
        title="整理项目中的 PDF 文件",
        description=description,
        task_type="coding",
        allowed_paths=allowed_paths or ["source.pdf", "summary.md"],
        acceptance_command="",
        dependencies=[],
    )


def test_document_request_is_not_misclassified_by_generic_one_phrase(tmp_path):
    engine = Engine(db_path=str(tmp_path / "studio.db"))

    assert engine._classify_request(
        "把项目里的 PDF 书籍整理成一个精简版"
    ) == "complex"
    assert engine._classify_request("创建一个简单 HTML 页面") == "simple"


def test_document_budget_scales_with_source_size(tmp_path):
    source = tmp_path / "source.pdf"
    source.touch()
    short = Engine._document_task_profile(_task(), str(tmp_path))

    with source.open("r+b") as handle:
        handle.truncate(3 * 1024 * 1024)
    medium = Engine._document_task_profile(
        _task(description="整理 PDF 内容"), str(tmp_path)
    )
    long = Engine._document_task_profile(
        _task(description="整理整本 PDF 书籍"), str(tmp_path)
    )

    assert short["input_budget"] == 300_000
    assert medium["input_budget"] == 600_000
    assert long["input_budget"] == 1_000_000
    assert long["exploration_turns"] > medium["exploration_turns"]


def test_document_reservation_keeps_paid_api_cost_limit_unchanged():
    engine = CostEngine()
    before = engine.get_budget("JOB-DOC").max_cost_cny

    budget = engine.reserve_document_budget("JOB-DOC", 1_000_000)

    assert budget.max_input_tokens >= 1_250_000
    assert budget.max_total_tokens >= 1_450_000
    assert budget.max_api_calls >= 160
    assert budget.max_cost_cny == before


def test_pdf_tool_is_available_without_shell_commands(tmp_path):
    broker = ToolBroker(tmp_path, PolicyEngine())
    analysis = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("analysis")
    }
    coding = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("coding")
    }

    assert "read_pdf" in analysis
    assert "read_pdf" in coding
    assert "read_pdf" in broker.get_available_tools()


def test_read_pdf_extracts_a_bounded_page_range(tmp_path, monkeypatch):
    (tmp_path / "source.pdf").write_bytes(b"fake pdf")

    class Page:
        def __init__(self, number):
            self.number = number

        def extract_text(self):
            return f"content from page {self.number}"

    class Reader:
        is_encrypted = False
        pages = [Page(number) for number in range(1, 16)]

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = lambda _path: Reader()
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    result = asyncio.run(FileTools(tmp_path).read_pdf(
        "source.pdf", start_page=2, end_page=15
    ))

    assert result["status"] == "success"
    assert result["page_start"] == 2
    assert result["page_end"] == 9
    assert result["next_page"] == 10
    assert "content from page 2" in result["content"]
    assert "content from page 9" in result["content"]


def test_read_pdf_reports_encryption_without_repeated_attempts(tmp_path, monkeypatch):
    (tmp_path / "locked.pdf").write_bytes(b"fake encrypted pdf")

    class Reader:
        is_encrypted = True

        @staticmethod
        def decrypt(_password):
            return 0

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = lambda _path: Reader()
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    result = asyncio.run(FileTools(tmp_path).read_pdf("locked.pdf"))

    assert result["status"] == "password_required"
    assert result["error_code"] == "pdf_password_required"
    assert result["encrypted"] is True


def test_read_pdf_distinguishes_blank_front_matter_from_scanned_pdf(
    tmp_path, monkeypatch
):
    (tmp_path / "source.pdf").write_bytes(b"fake pdf")

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class Reader:
        is_encrypted = False
        pages = [Page("") for _ in range(9)] + [Page("chapter text")]

    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = lambda _path: Reader()
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    result = asyncio.run(FileTools(tmp_path).read_pdf(
        "source.pdf", start_page=1, end_page=8
    ))

    assert result["status"] == "empty_page_range"
    assert result["next_page"] == 9

    Reader.pages = [Page("") for _ in range(10)]
    result = asyncio.run(FileTools(tmp_path).read_pdf("source.pdf"))
    assert result["status"] == "no_extractable_text"
    assert result["error_code"] == "pdf_ocr_required"


def test_worker_stops_immediately_when_pdf_needs_user_action():
    class Router:
        calls = 0

        async def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "content": "Reading the source PDF.",
                "tool_calls": [{
                    "id": "pdf-1",
                    "function": {
                        "name": "read_pdf",
                        "arguments": json.dumps({"path": "locked.pdf"}),
                    },
                }],
                "usage": {},
            }

    class Broker:
        policy = None

        @staticmethod
        def get_tool_definitions():
            return []

        @staticmethod
        async def execute(_task_value, _name, _args):
            return {
                "status": "password_required",
                "error": "PDF is encrypted and requires a password.",
                "path": "locked.pdf",
            }

    async def scenario():
        router = Router()
        worker = WorkerAgent(router, Broker(), max_turns=10)
        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "failed"
        assert result["error"].startswith("USER_INPUT_REQUIRED:")
        assert router.calls == 1

    asyncio.run(scenario())


def test_pdf_user_action_has_a_specific_recovery_hint():
    code, hint = Engine._failure_details(
        "USER_INPUT_REQUIRED: locked.pdf: PDF is encrypted and requires a password"
    )

    assert code == "pdf_password_required"
    assert "解除密码保护" in hint


def test_escalation_does_not_retry_a_user_input_requirement(tmp_path):
    class Worker:
        max_turns = 20
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "status": "failed",
                "error": (
                    "USER_INPUT_REQUIRED: locked.pdf: PDF is encrypted and "
                    "requires a password"
                ),
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        worker = Worker()
        result = await engine._execute_single_task_with_escalation(
            _task(), SimpleNamespace(job_id="JOB-DOC"), {}, worker, str(tmp_path)
        )

        assert result["status"] == "failed"
        assert worker.calls == 1

    asyncio.run(scenario())
