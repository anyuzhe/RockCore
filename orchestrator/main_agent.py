"""Persistent task owner coordinating optional specialist advisors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .execution_session import normalize_session, record_turn


@dataclass(frozen=True)
class AdvisorDecision:
    governor: bool
    planner: bool
    reviewer: bool
    reason: str


class MainAgent:
    """Own one conversation turn while specialist agents provide advice.

    This class intentionally stores only durable decisions, evidence and public
    summaries. Provider-private reasoning is never persisted.
    """

    def __init__(self, engine: Any):
        self.engine = engine

    @staticmethod
    def decide_advisors(*, mode: str, risk_route: str, complexity: str,
                        has_attachments: bool, governor_enabled: bool,
                        planner_enabled: bool, reviewer_enabled: bool) -> AdvisorDecision:
        governor = governor_enabled and (
            mode not in {"auto", "fast"} or risk_route == "high"
        )
        planner = planner_enabled and (
            mode != "fast" and (complexity != "simple" or risk_route != "low")
        )
        reviewer = reviewer_enabled and (mode == "strict" or risk_route == "high")
        enabled = [name for name, active in (
            ("裁决", governor), ("策划", planner), ("审核", reviewer)
        ) if active]
        return AdvisorDecision(
            governor=governor,
            planner=planner,
            reviewer=reviewer,
            reason=("按风险与复杂度咨询：" + "、".join(enabled)) if enabled else "由主控直接执行并确定性验证",
        )

    def prepare_turn(self, job: Any, repos: dict, *, resumed: bool = False) -> dict:
        checkpoint = dict(job.last_checkpoint or {})
        session = normalize_session(
            checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=job.user_request,
        )
        record_turn(
            session, job_id=job.job_id, request=job.user_request,
            status="resuming" if resumed else job.status,
        )
        session["current_step"] = "resume" if resumed else "understand"
        session["next_action"] = (
            "Continue from saved evidence and unfinished checklist"
            if resumed else "Understand this turn and choose only necessary advisors"
        )
        checkpoint["execution_session"] = session
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        return session

    async def run_turn(self, job_id: str, project_root: str):
        return await self.engine._run_job_pipeline_core(job_id, project_root)

    async def resume_turn(self, job_id: str, project_root: str):
        return await self.engine._resume_attention_pipeline_core(
            job_id, project_root
        )

    def record_advisor_decision(self, job: Any, repos: dict,
                                decision: AdvisorDecision) -> None:
        checkpoint = dict(job.last_checkpoint or {})
        session = normalize_session(
            checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=job.user_request,
        )
        history = list(session.get("advisor_history") or [])
        history.append({
            "job_id": job.job_id,
            "governor": decision.governor,
            "planner": decision.planner,
            "reviewer": decision.reviewer,
            "reason": decision.reason,
        })
        session["advisor_history"] = history[-24:]
        checkpoint["execution_session"] = session
        repos["job"].update_checkpoint(job.job_id, checkpoint)
