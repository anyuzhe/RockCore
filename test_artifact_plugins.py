"""Real round-trip tests for the local document plugin tools."""

import asyncio
from types import SimpleNamespace

from orchestrator.policy_engine import PolicyEngine
from tools.tool_broker import ToolBroker


def test_document_pdf_and_presentation_plugins_round_trip_chinese(tmp_path):
    broker = ToolBroker(tmp_path, PolicyEngine())
    task = SimpleNamespace(
        task_type="coding", allowed_paths=["*.docx", "*.pptx", "*.pdf"],
        protected_paths=[], skills=["documents", "pdf", "presentations"],
    )

    async def exercise():
        docx = await broker.execute(task, "write_docx", {
            "path": "中文文档.docx", "title": "测试文档",
            "content": "# 第一章\n- 要点一\n正文内容",
        })
        docx_read = await broker.execute(task, "read_docx", {
            "path": "中文文档.docx",
        })
        pptx = await broker.execute(task, "write_pptx", {
            "path": "演示.pptx", "title": "测试演示",
            "slides": [{"title": "第一页", "bullets": ["要点一", "要点二"]}],
        })
        pptx_read = await broker.execute(task, "read_pptx", {
            "path": "演示.pptx",
        })
        pdf = await broker.execute(task, "write_pdf", {
            "path": "报告.pdf", "title": "测试报告",
            "content": "# 第一章\n- 中文要点\n正文内容",
        })
        pdf_read = await broker.execute(task, "read_pdf", {"path": "报告.pdf"})
        return docx, docx_read, pptx, pptx_read, pdf, pdf_read

    docx, docx_read, pptx, pptx_read, pdf, pdf_read = asyncio.run(exercise())
    assert docx["status"] == pptx["status"] == pdf["status"] == "written"
    assert "测试文档" in docx_read["content"]
    assert "第一页" in pptx_read["content"]
    assert "中文要点" in pdf_read["content"]


def test_artifact_schemas_are_only_loaded_for_selected_plugins(tmp_path):
    broker = ToolBroker(tmp_path, PolicyEngine())
    plain = {
        item["function"]["name"]
        for item in broker.get_tool_definitions("coding", skills=[])
    }
    enabled = {
        item["function"]["name"]
        for item in broker.get_tool_definitions(
            "coding", skills=["documents", "presentations", "pdf"]
        )
    }
    assert "write_docx" not in plain
    assert {"write_docx", "write_pptx", "write_pdf"} <= enabled
