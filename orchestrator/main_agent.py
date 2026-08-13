"""Model-backed task owner with deterministic runtime fallbacks."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .execution_session import normalize_session, record_turn, render_fixed_context

logger = logging.getLogger(__name__)


MAIN_AGENT_SYSTEM_PROMPT = """You are RockCore's Main Agent and the persistent
owner of one software-engineering conversation. You understand the user's
current turn in light of prior turns, decide the smallest useful workflow, and
coordinate specialist agents. You do not edit files, run commands, change
database state, grant permissions, or claim tests passed. The deterministic
runtime performs those actions and remains authoritative.

Return ONLY valid JSON with this structure:
{
  "goal": "the concrete current goal",
  "constraints": ["bounded implementation constraints"],
  "acceptance_criteria": ["observable and testable outcomes"],
  "risk": "low|medium|high",
  "risk_score": 0,
  "risk_reasons": ["specific reasons"],
  "protected_paths": ["paths that must not be changed"],
  "image_observations": ["facts visible in attachments"],
  "execution_strategy": "direct|planned",
  "use_planner": false,
  "use_reviewer": false,
  "summary": "brief user-facing understanding of this turn",
  "next_action": "the next concrete action"
}

Use direct only for a narrow, well-specified change. Use planned for multi-file,
ambiguous, architectural, historical-code, or broad tasks. High-risk work must
use an independent reviewer. Preserve the user's intent across follow-ups.
Do not expose private reasoning; return decisions and concise reasons only.
"""


MAIN_AGENT_SUMMARY_PROMPT = """You are RockCore's Main Agent. Produce the final
user-facing result summary for this conversation turn from persisted execution
evidence. Never invent changes or test results. Clearly distinguish completed,
failed, user-action-required, and unverified work.

Return ONLY valid JSON:
{
  "summary": "concise result in the user's language",
  "completed": ["verified outcomes"],
  "remaining": ["unfinished or unverified items"],
  "next_action": "optional next action"
}
Do not include hidden reasoning or internal Job/Task identifiers.
"""


@dataclass(frozen=True)
class AdvisorDecision:
    governor: bool
    planner: bool
    reviewer: bool
    reason: str


class MainAgent:
    """Own a conversation while code retains safety and execution authority."""

    def __init__(self, engine: Any):
        self.engine = engine

    @staticmethod
    def decide_advisors(*, mode: str, risk_route: str, complexity: str,
                        has_attachments: bool, governor_enabled: bool,
                        planner_enabled: bool, reviewer_enabled: bool) -> AdvisorDecision:
        model_owner = governor_enabled and (
            mode not in {"auto", "fast"}
            or risk_route == "high"
            or complexity != "simple"
            or has_attachments
        )
        planner = planner_enabled and (
            mode != "fast" and (complexity != "simple" or risk_route != "low")
        )
        reviewer = reviewer_enabled and (mode == "strict" or risk_route == "high")
        enabled = [name for name, active in (
            ("主控模型", model_owner), ("策划顾问", planner),
            ("独立审核", reviewer),
        ) if active]
        return AdvisorDecision(
            governor=model_owner,
            planner=planner,
            reviewer=reviewer,
            reason=("按风险与复杂度启用：" + "、".join(enabled))
            if enabled else "简单低风险任务由确定性主控直接执行",
        )

    @staticmethod
    def _parse_json(content: str) -> dict:
        value = str(content or "").strip()
        if "```json" in value:
            value = value.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in value:
            value = value.split("```", 1)[1].split("```", 1)[0].strip()
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("Main Agent response must be a JSON object")
        return parsed

    @staticmethod
    def _normalize_assessment(data: dict, *, fallback_risk: str,
                              user_request: str) -> dict:
        result = dict(data or {})
        try:
            score = max(0, min(100, int(result.get("risk_score", 50))))
        except (TypeError, ValueError, OverflowError):
            score = 50
        explicit = str(result.get("risk") or fallback_risk or "medium").lower()
        if explicit not in {"low", "medium", "high"}:
            explicit = "medium"
        score_risk = "low" if score <= 30 else "medium" if score <= 60 else "high"
        rank = {"low": 0, "medium": 1, "high": 2}
        deterministic_risk = str(fallback_risk or "medium").lower()
        if deterministic_risk not in rank:
            deterministic_risk = "medium"
        # A model may promote risk but cannot bypass the deterministic safety
        # floor derived from permissions and known high-impact operations.
        risk = max((explicit, score_risk, deterministic_risk), key=rank.get)
        if risk == "medium":
            score = max(score, 31)
        elif risk == "high":
            score = max(score, 61)
        strategy = str(result.get("execution_strategy") or "planned").lower()
        if strategy not in {"direct", "planned"}:
            strategy = "planned"
        result.update({
            "goal": str(result.get("goal") or user_request).strip(),
            "constraints": [str(item)[:500] for item in (
                result.get("constraints") or []
            ) if str(item).strip()][:10],
            "acceptance_criteria": [str(item)[:500] for item in (
                result.get("acceptance_criteria") or []
            ) if str(item).strip()][:10] or ["确定性验证通过"],
            "risk": risk,
            "risk_score": score,
            "risk_reasons": [str(item)[:300] for item in (
                result.get("risk_reasons") or []
            ) if str(item).strip()][:8] or ["主控模型按影响范围评估"],
            "protected_paths": [str(item)[:500] for item in (
                result.get("protected_paths") or []
            ) if str(item).strip()][:20],
            "image_observations": [str(item)[:800] for item in (
                result.get("image_observations") or []
            ) if str(item).strip()][:16],
            "execution_strategy": strategy,
            "use_planner": bool(result.get("use_planner", strategy == "planned")),
            "use_reviewer": bool(result.get("use_reviewer", risk == "high")),
            "summary": str(result.get("summary") or "")[:2000],
            "next_action": str(result.get("next_action") or "")[:1000],
            "source": "main_agent",
        })
        if risk == "high":
            result["use_reviewer"] = True
        return result

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

    async def assess_turn(self, job: Any, repos: dict, *, fallback_risk: str,
                          resumed: bool = False) -> dict | None:
        """Ask Codex to own the turn; return None for deterministic fallback."""
        checkpoint = dict(job.last_checkpoint or {})
        session = normalize_session(
            checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=job.user_request,
        )
        prompt = render_fixed_context(session, max_chars=20_000)
        project_surface = dict(
            getattr(job, "_rockcore_project_surface", None)
            or checkpoint.get("project_surface") or {}
        )
        surface_context = json.dumps({
            key: project_surface.get(key)
            for key in (
                "framework", "runtime", "entrypoints", "active_files",
                "source_roots", "test_commands", "confidence", "evidence",
            ) if project_surface.get(key) not in (None, "", [], {})
        }, ensure_ascii=False, indent=2, default=str)
        messages = [{
            "role": "user",
            "content": (
                f"Current user turn:\n{job.user_request}\n\n"
                f"Resuming saved work: {'yes' if resumed else 'no'}\n\n{prompt}"
                f"\n\n=== ACTIVE PROJECT SURFACE ===\n{surface_context}"
            ),
        }]
        try:
            response = await self.engine.model_router.chat(
                "main_agent", MAIN_AGENT_SYSTEM_PROMPT, messages,
                job_id=job.job_id,
                project_root=(job.project.root_path if job.project else "."),
                attachments=list(getattr(job, "attachments", None) or []),
                allow_provider_fallback=False,
                estimated_output_tokens=4_096,
            )
            assessment = self._normalize_assessment(
                self._parse_json(response.get("content", "")),
                fallback_risk=fallback_risk,
                user_request=job.user_request,
            )
        except Exception as error:
            logger.warning("Main Agent model unavailable for %s: %s", job.job_id, error)
            await self.engine.event_bus.publish(
                "main_agent_fallback", job_id=job.job_id,
                error=str(error),
                summary="主控模型不可用，已切换到确定性流程继续执行",
            )
            return None

        history = list(session.get("advisor_history") or [])
        history.append({
            "job_id": job.job_id,
            "role": "main_agent",
            "risk": assessment["risk"],
            "execution_strategy": assessment["execution_strategy"],
            "use_planner": assessment["use_planner"],
            "use_reviewer": assessment["use_reviewer"],
            "summary": assessment["summary"],
        })
        session["advisor_history"] = history[-24:]
        session["acceptance_criteria"] = assessment["acceptance_criteria"]
        session["constraints"] = assessment["constraints"]
        session["current_step"] = "plan" if assessment["use_planner"] else "execute"
        session["next_action"] = assessment["next_action"] or (
            "Ask Planner for a grounded plan" if assessment["use_planner"]
            else "Execute the focused change"
        )
        checkpoint["execution_session"] = session
        checkpoint["main_agent_assessment"] = assessment
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        await self.engine.event_bus.publish(
            "main_agent_decided", job_id=job.job_id,
            summary=assessment["summary"],
            risk_level=assessment["risk"],
            execution_strategy=assessment["execution_strategy"],
            use_planner=assessment["use_planner"],
            use_reviewer=assessment["use_reviewer"],
            next_action=assessment["next_action"],
        )
        return assessment

    async def summarize_turn(self, job: Any, repos: dict) -> str:
        """Create a grounded public summary without making completion decisions."""
        checkpoint = dict(job.last_checkpoint or {})
        session = normalize_session(
            checkpoint.get("execution_session"),
            session_id=(job.execution_session_id or job.job_id),
            goal=job.user_request,
        )
        evidence = render_fixed_context(session, max_chars=20_000)
        try:
            response = await self.engine.model_router.chat(
                "main_agent_summary", MAIN_AGENT_SUMMARY_PROMPT,
                [{"role": "user", "content": (
                    f"Authoritative terminal status: {job.status}\n\n{evidence}"
                )}],
                job_id=job.job_id,
                project_root=(job.project.root_path if job.project else "."),
                allow_provider_fallback=False,
                estimated_output_tokens=2_048,
            )
            result = self._parse_json(response.get("content", ""))
            summary = str(result.get("summary") or "").strip()[:4000]
        except Exception as error:
            logger.warning("Main Agent summary unavailable for %s: %s", job.job_id, error)
            return ""
        if not summary:
            return ""
        record_turn(
            session, job_id=job.job_id, request=job.user_request,
            status=job.status, summary=summary,
        )
        session["conversation_summary"] = summary
        checkpoint["execution_session"] = session
        repos["job"].update_checkpoint(job.job_id, checkpoint)
        await self.engine.event_bus.publish(
            "main_agent_summary", job_id=job.job_id, summary=summary,
        )
        return summary

    async def run_turn(self, job_id: str, project_root: str):
        return await self.engine._run_job_pipeline_core(job_id, project_root)

    async def resume_turn(self, job_id: str, project_root: str):
        return await self.engine._resume_attention_pipeline_core(job_id, project_root)

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
            "role": "deterministic_router",
            "main_agent_model": decision.governor,
            "planner": decision.planner,
            "reviewer": decision.reviewer,
            "reason": decision.reason,
        })
        session["advisor_history"] = history[-24:]
        checkpoint["execution_session"] = session
        repos["job"].update_checkpoint(job.job_id, checkpoint)
