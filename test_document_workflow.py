"""Regression tests for size-aware long-document processing."""

import asyncio
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

from agents.worker import WorkerAgent
from orchestrator.cost_engine import CostEngine
from orchestrator.engine import Engine
from orchestrator.merge_manager import MergeManager
from orchestrator.policy_engine import PolicyEngine
from orchestrator.state_machine import JobState
from orchestrator.test_manager import TestManager
from git.repository import Repository
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


def test_document_budget_scales_with_actual_page_count(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.touch()
    monkeypatch.setattr(
        Engine,
        "_pdf_page_count",
        staticmethod(lambda path: (
            24 if path.stat().st_size < 2 * 1024 * 1024
            else 80 if path.stat().st_size < 8 * 1024 * 1024
            else 151
        )),
    )
    short = Engine._document_task_profile(_task(), str(tmp_path))

    with source.open("r+b") as handle:
        handle.truncate(3 * 1024 * 1024)
    medium = Engine._document_task_profile(
        _task(description="整理 PDF 内容"), str(tmp_path)
    )
    with source.open("r+b") as handle:
        handle.truncate(9 * 1024 * 1024)
    long = Engine._document_task_profile(
        _task(description="整理整本 PDF 书籍"), str(tmp_path)
    )

    assert short["input_budget"] == 788_000
    assert medium["input_budget"] == 1_460_000
    assert long["processing_input_budget"] == 2_112_000
    assert long["finalization_reserve"] == 253_440
    assert long["input_budget"] == 2_365_440
    assert long["pdf_pages"] == 151
    assert long["page_batches"] == 19
    assert long["max_turns"] == 96
    assert long["api_call_budget"] == 132
    assert long["exploration_turns"] > medium["exploration_turns"]


def test_document_reservation_keeps_paid_api_cost_limit_unchanged():
    engine = CostEngine()
    before = engine.get_budget("JOB-DOC").max_cost_cny

    budget = engine.reserve_document_budget(
        "JOB-DOC", 2_112_000,
        required_api_calls=120,
        required_output_tokens=226_500,
    )

    assert budget.max_input_tokens >= 2_462_000
    assert budget.max_total_tokens >= 2_914_000
    assert budget.max_api_calls >= 180
    assert budget.max_cost_cny == before


def test_generated_pdf_receives_structural_validation(tmp_path):
    try:
        from pypdf import PdfWriter
    except ImportError:
        from PyPDF2 import PdfWriter
    output = tmp_path / "summary.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with output.open("wb") as handle:
        writer.write(handle)

    valid = TestManager._validate_source(output)
    invalid = tmp_path / "broken.pdf"
    invalid.write_bytes(b"not-a-pdf")
    broken = TestManager._validate_source(invalid)

    assert valid["status"] == "passed"
    assert broken["status"] == "failed"
    assert "PDF" in broken["issues"][0]


def test_worker_cannot_finish_while_pdf_reports_unread_pages():
    class Router:
        def __init__(self):
            self.calls = 0
            self.messages = []

        async def chat_with_tools(self, *_args, **kwargs):
            self.calls += 1
            self.messages.append(kwargs.get("messages") or _args[2])
            if self.calls == 1:
                name = "read_pdf"
                arguments = {"path": "source.pdf", "start_page": 1}
            elif self.calls == 2:
                name = "write_file"
                arguments = {"path": "summary.md", "content": "pages 1-8"}
            elif self.calls == 3:
                return {
                    "content": "Everything is complete.",
                    "tool_calls": [], "usage": {},
                }
            elif self.calls == 4:
                name = "read_pdf"
                arguments = {"path": "source.pdf", "start_page": 9}
            else:
                return {
                    "content": "All pages are now complete.",
                    "tool_calls": [], "usage": {},
                }
            return {
                "content": "Continuing document work.",
                "tool_calls": [{
                    "id": f"call-{self.calls}",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments),
                    },
                }],
                "usage": {},
            }

    class Broker:
        policy = None

        @staticmethod
        def get_tool_definitions(*_args, **_kwargs):
            return []

        @staticmethod
        async def execute(_task_value, name, args):
            if name == "read_pdf" and args.get("start_page") == 1:
                return {
                    "status": "success", "path": "source.pdf",
                    "page_start": 1, "page_end": 8, "page_count": 16,
                    "has_more": True, "next_page": 9,
                }
            if name == "read_pdf":
                return {
                    "status": "success", "path": "source.pdf",
                    "page_start": 9, "page_end": 16, "page_count": 16,
                    "has_more": False, "next_page": None,
                }
            return {"status": "written"}

    async def scenario():
        router = Router()
        worker = WorkerAgent(router, Broker(), max_turns=8)
        result = await worker.run(_task(), project_root=".")

        assert result["status"] == "completed"
        assert router.calls == 5
        assert any(
            "start_page=9" in str(message)
            for message in router.messages[3]
        )

    asyncio.run(scenario())


def test_document_token_budget_auto_expands_before_retry(tmp_path):
    class Worker:
        max_turns = 64
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "status": "failed",
                    "error": (
                        "RockCore job budget exceeded: Task input tokens "
                        "exceeded: 600001/600000"
                    ),
                }
            return {"status": "completed", "content": "finished"}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        task = _task()
        task._rockcore_input_budget = 600_000
        task._rockcore_document_profile = {
            "max_turns": 64,
            "api_call_budget": 88,
            "output_budget": 200_000,
        }
        worker = Worker()
        job = SimpleNamespace(job_id="JOB-DOC-EXTEND", project=None)

        result = await engine._execute_single_task_with_escalation(
            task, job, {}, worker, str(tmp_path)
        )

        assert result["status"] == "completed"
        assert worker.calls == 2
        assert task._rockcore_input_budget == 1_200_000
        assert engine.model_router.cost_engine.get_budget(
            job.job_id
        ).max_cost_cny == 3.60
        assert engine.event_bus.get_history("document_budget_extended")

    asyncio.run(scenario())


def test_final_document_attempt_can_expand_into_short_finalization(tmp_path):
    class Worker:
        max_turns = 64
        max_exploration_turns = 20
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls <= 2:
                return {"status": "failed", "error": "Max turns (32) reached"}
            if self.calls == 3:
                return {
                    "status": "failed",
                    "error": (
                        "RockCore job budget exceeded: Task input tokens "
                        "exceeded: 600001/600000"
                    ),
                }
            return {"status": "completed", "content": "artifacts verified"}

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        task = _task()
        task._rockcore_input_budget = 600_000
        task._rockcore_document_profile = {
            "max_turns": 64,
            "finalization_turns": 12,
            "api_call_budget": 88,
            "output_budget": 200_000,
        }
        engine._check_file_changes = lambda *_args, **_kwargs: asyncio.sleep(
            0, result=True
        )
        worker = Worker()
        job = SimpleNamespace(job_id="JOB-DOC-FINAL", project=None)

        result = await engine._execute_single_task_with_escalation(
            task, job, {}, worker, str(tmp_path)
        )

        assert result["status"] == "completed"
        assert worker.calls == 4
        assert task._rockcore_finalization_mode is True
        assert worker.max_turns <= 12
        assert engine.event_bus.get_history("document_budget_extended")

    asyncio.run(scenario())


def test_exhausted_document_with_artifacts_moves_to_validation(tmp_path):
    class Worker:
        max_turns = 24
        max_exploration_turns = 8
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            return {
                "status": "failed",
                "error": (
                    "RockCore job budget exceeded: Task input tokens "
                    "exceeded: 600001/600000"
                ),
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        task = _task()
        task._rockcore_input_budget = 600_000
        task._rockcore_document_profile = {
            "max_turns": 24,
            "finalization_turns": 8,
            "api_call_budget": 48,
            "output_budget": 200_000,
        }
        engine._check_file_changes = lambda *_args, **_kwargs: asyncio.sleep(
            0, result=True
        )

        result = await engine._execute_single_task_with_escalation(
            task, SimpleNamespace(job_id="JOB-DOC-PENDING", project=None),
            {}, Worker(), str(tmp_path),
        )

        assert result["status"] == "pending_validation"
        assert result["failure_stage"] == "budget_finalization"
        assert engine.event_bus.get_history("task_pending_validation")

    asyncio.run(scenario())


def test_execution_marks_budget_exhausted_artifact_done_after_validation(tmp_path):
    try:
        from pypdf import PdfWriter
    except ImportError:
        from PyPDF2 import PdfWriter
    project_root = tmp_path / "project"
    project_root.mkdir()
    source_writer = PdfWriter()
    source_writer.add_blank_page(width=595, height=842)
    with (project_root / "source.pdf").open("wb") as handle:
        source_writer.write(handle)

    class Worker:
        max_turns = 24
        max_exploration_turns = 8

        def scoped_to(self, root):
            self.root = Path(root)
            return self

        async def run(self, *_args, **_kwargs):
            output = PdfWriter()
            output.add_blank_page(width=595, height=842)
            with (self.root / "summary.pdf").open("wb") as handle:
                output.write(handle)
            return {
                "status": "failed",
                "error": (
                    "RockCore job budget exceeded: Task input tokens "
                    "exceeded: 600001/600000"
                ),
            }

    async def scenario():
        engine = Engine(db_path=str(tmp_path / "studio.db"))
        engine._check_file_changes = lambda *_args, **_kwargs: asyncio.sleep(
            0, result=True
        )
        repos = engine._get_repos()
        try:
            project = repos["project"].create("Docs", str(project_root))
            job = repos["job"].create(
                "JOB-DOC-VALIDATE", project.id, "精简 PDF 文档"
            )
            task = repos["task"].create(
                "T001", job.id, "生成精简 PDF", task_type="coding",
                allowed_paths=["summary.pdf"],
            )
            engine.register_agent("worker", Worker())
            engine.state_machine._states[job.job_id] = JobState.READY

            await engine._run_execution(
                job, repos,
                job_baseline=engine.test_manager.capture_snapshot(project_root),
            )

            repos["_session"].refresh(task)
            done = engine.event_bus.get_history("task_done")[-1]["data"]["result"]
            assert task.status == "done"
            assert done["recovered_from_budget"] is True
            assert done["status"] == "completed"
            assert engine.event_bus.get_history("task_pending_validation")
        finally:
            repos["_session"].close()

    asyncio.run(scenario())


def test_untracked_pdf_input_is_not_committed_or_merged_as_output(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    repository = Repository(str(project))
    assert repository.ensure_initialized()["status"] == "initialized"
    source = project / "原始书籍.pdf"
    source.write_bytes(b"source-pdf")
    manager = MergeManager(str(project))

    async def scenario():
        created = await manager.create_task_worktree("T001", "JOB-DOC")
        assert created["status"] == "created"
        worktree = Path(created["path"])
        assert (worktree / source.name).read_bytes() == b"source-pdf"
        (worktree / "精简版.md").write_text("complete", encoding="utf-8")

        merged = await manager.commit_and_merge("T001", "document output")

        assert merged["status"] == "merged"

    asyncio.run(scenario())

    assert source.read_bytes() == b"source-pdf"
    assert repository._run(
        "ls-files", "--error-unmatch", "精简版.md"
    ).returncode == 0
    assert repository._run(
        "ls-files", "--error-unmatch", source.name
    ).returncode != 0


def test_task_snapshot_does_not_treat_preexisting_untracked_pdf_as_output(
    tmp_path,
):
    project = tmp_path / "project"
    project.mkdir()
    repository = Repository(str(project))
    assert repository.ensure_initialized()["status"] == "initialized"
    (project / "source.pdf").write_bytes(b"input")
    engine = Engine(db_path=str(tmp_path / "studio.db"))
    baseline = engine.test_manager.capture_snapshot(project)

    assert asyncio.run(
        engine._check_file_changes(str(project), baseline)
    ) is False

    (project / "summary.md").write_text("output", encoding="utf-8")
    assert asyncio.run(
        engine._check_file_changes(str(project), baseline)
    ) is True


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
