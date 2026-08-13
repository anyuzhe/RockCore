"""Persistent fixed context for one coherent requirement conversation."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


SESSION_VERSION = 2


def new_session(session_id: str, goal: str, *, source_job_id: str = "") -> dict:
    now = datetime.now().astimezone().isoformat()
    return {
        "version": SESSION_VERSION,
        "session_id": str(session_id),
        "root_job_id": str(source_job_id or session_id),
        "goal": str(goal or "").strip(),
        "title": " ".join(str(goal or "").split())[:80],
        "active_turn_id": str(source_job_id or session_id),
        "turns": [],
        "conversation_summary": "",
        "last_user_request": str(goal or "").strip(),
        "last_assistant_summary": "",
        "acceptance_criteria": [],
        "constraints": [],
        "checklist": [],
        "decisions": [],
        "read_evidence": {},
        "changed_files": [],
        "validation": [],
        "advisor_history": [],
        "substeps": [],
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
        "conversation_summary": session.get("conversation_summary", ""),
        "last_user_request": session.get("last_user_request", ""),
        "last_assistant_summary": session.get("last_assistant_summary", ""),
        "turns": list(session.get("turns") or [])[-12:],
        "acceptance_criteria": list(session.get("acceptance_criteria") or [])[:24],
        "constraints": list(session.get("constraints") or [])[:24],
        "checklist": list(session.get("checklist") or [])[:16],
        "decisions": list(session.get("decisions") or [])[-16:],
        "read_evidence": dict(list((session.get("read_evidence") or {}).items())[-40:]),
        "changed_files": list(session.get("changed_files") or [])[:80],
        "validation": list(session.get("validation") or [])[-16:],
        "advisor_history": list(session.get("advisor_history") or [])[-12:],
        "substeps": list(session.get("substeps") or [])[-24:],
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


def record_turn(session: dict, *, job_id: str, request: str,
                status: str = "created", summary: str = "") -> dict:
    """Upsert one public conversation turn without storing hidden reasoning."""
    turns = list(session.get("turns") or [])
    item = {
        "job_id": str(job_id),
        "request": str(request or "")[:4000],
        "status": str(status or "created"),
        "summary": str(summary or "")[:4000],
    }
    for index, existing in enumerate(turns):
        if existing.get("job_id") == job_id:
            turns[index] = {**existing, **item}
            break
    else:
        turns.append(item)
    session["turns"] = turns[-40:]
    session["active_turn_id"] = str(job_id)
    session["last_user_request"] = str(request or "")
    if summary:
        session["last_assistant_summary"] = str(summary)[:4000]
    session["updated_at"] = datetime.now().astimezone().isoformat()
    return session


def record_substep(session: dict, *, parent_task_id: str, key: str,
                   title: str, status: str, summary: str = "") -> dict:
    """Persist an ephemeral execution step outside the database Task DAG."""
    substep_id = f"{parent_task_id}:{key}"
    substeps = list(session.get("substeps") or [])
    item = {
        "id": substep_id,
        "parent_task_id": str(parent_task_id),
        "title": str(title or key)[:200],
        "status": str(status),
        "summary": str(summary or "")[:800],
    }
    for index, existing in enumerate(substeps):
        if existing.get("id") == substep_id:
            substeps[index] = {**existing, **item}
            break
    else:
        substeps.append(item)
    session["substeps"] = substeps[-80:]
    session["updated_at"] = datetime.now().astimezone().isoformat()
    return session


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
