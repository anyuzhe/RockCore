"""Persistent fixed context for one coherent requirement conversation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


SESSION_VERSION = 1


def new_session(session_id: str, goal: str, *, source_job_id: str = "") -> dict:
    now = datetime.now().astimezone().isoformat()
    return {
        "version": SESSION_VERSION,
        "session_id": str(session_id),
        "root_job_id": str(source_job_id or session_id),
        "goal": str(goal or "").strip(),
        "acceptance_criteria": [],
        "constraints": [],
        "checklist": [],
        "decisions": [],
        "read_evidence": {},
        "changed_files": [],
        "validation": [],
        "current_step": "",
        "next_action": "",
        "recoverable_error": {},
        "created_at": now,
        "updated_at": now,
    }


def normalize_session(raw: dict | None, *, session_id: str, goal: str) -> dict:
    session = new_session(session_id, goal)
    if isinstance(raw, dict):
        for key in session:
            if key in raw:
                session[key] = raw[key]
    session["version"] = SESSION_VERSION
    session["session_id"] = str(session_id)
    session["goal"] = str(session.get("goal") or goal).strip()
    session["updated_at"] = datetime.now().astimezone().isoformat()
    return session

def render_fixed_context(session: dict | None, *, max_chars: int = 24_000) -> str:
    """Render state that must survive transcript compaction."""
    if not session:
        return ""
    payload = {
        "session_id": session.get("session_id", ""),
        "root_job_id": session.get("root_job_id", ""),
        "goal": session.get("goal", ""),
        "acceptance_criteria": list(session.get("acceptance_criteria") or [])[:24],
        "constraints": list(session.get("constraints") or [])[:24],
        "checklist": list(session.get("checklist") or [])[:16],
        "decisions": list(session.get("decisions") or [])[-16:],
        "read_evidence": dict(list((session.get("read_evidence") or {}).items())[-40:]),
        "changed_files": list(session.get("changed_files") or [])[:80],
        "validation": list(session.get("validation") or [])[-16:],
        "current_step": session.get("current_step", ""),
        "next_action": session.get("next_action", ""),
        "recoverable_error": dict(session.get("recoverable_error") or {}),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) > max_chars:
        # Drop lower-value read evidence before ever clipping goals/checklist.
        payload["read_evidence"] = dict(
            list(payload["read_evidence"].items())[-12:]
        )
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return "=== FIXED EXECUTION SESSION ===\n" + text


def update_checklist(session: dict, tasks: list[Any]) -> dict:
    session["checklist"] = [{
        "id": str(getattr(task, "task_id", "") or ""),
        "title": str(getattr(task, "title", "") or ""),
        "status": str(getattr(task, "status", "pending") or "pending"),
        "summary": str(getattr(task, "result_summary", "") or "")[:1200],
    } for task in tasks]
    session["current_step"] = next((
        item["id"] for item in session["checklist"]
        if item["status"] in {"pending", "ready", "running", "testing", "interrupted", "needs_attention"}
    ), "")
    session["next_action"] = (
        f"Continue {session['current_step']}" if session["current_step"] else "Finalize and report"
    )
    session["updated_at"] = datetime.now().astimezone().isoformat()
    return session
