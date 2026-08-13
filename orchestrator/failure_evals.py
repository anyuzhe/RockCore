"""Turn production workflow failures into deterministic regression cases."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.paths import project_state_dir


class FailureEvalStore:
    """Append de-duplicated, model-free workflow eval cases per project."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._lock = threading.Lock()

    @staticmethod
    def _failure_class(code: str, reason: str) -> str:
        text = f"{code} {reason}".lower()
        if any(value in text for value in ("402", "balance", "余额", "quota")):
            return "provider_balance"
        if any(value in text for value in ("timeout", "timed out")):
            return "provider_timeout"
        if any(value in text for value in ("404", "model", "permission denied")):
            return "provider_model"
        if any(value in text for value in ("validation", "acceptance", "test")):
            return "validation"
        if any(value in text for value in ("merge", "worktree", "git")):
            return "integration"
        if any(value in text for value in ("budget", "token", "turn")):
            return "runtime_budget"
        return "execution"

    def capture(self, job_id: str) -> dict[str, Any] | None:
        """Create/update one compact Eval fixture for a terminal failed Job."""
        from storage.models import Job

        session = self._session_factory()
        try:
            job = session.query(Job).filter(Job.job_id == job_id).first()
            if not job or job.status not in {
                "failed", "interrupted", "needs_attention",
            } or not job.project:
                return None
            tasks = sorted(job.tasks, key=lambda item: item.order)
            failing = next((
                task for task in tasks
                if task.status in {"failed", "interrupted", "needs_attention"}
            ), tasks[-1] if tasks else None)
            reason = str(
                (failing.failure_reason if failing else "")
                or job.failure_reason or "unknown failure"
            )
            failure_code = str(job.failure_code or "")
            failure_class = self._failure_class(failure_code, reason)
            identity = "|".join((
                failure_class,
                str(getattr(failing, "task_type", "") or ""),
                ",".join(getattr(failing, "allowed_paths", None) or []),
            ))
            fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            case = {
                "id": f"failure-{fingerprint}",
                "source_job_id": job.job_id,
                "captured_at": datetime.now().astimezone().isoformat(),
                "request": job.user_request[:4000],
                "failure_class": failure_class,
                "failure_code": failure_code,
                "failure_reason": reason[:4000],
                "task": {
                    "id": str(getattr(failing, "task_id", "") or ""),
                    "type": str(getattr(failing, "task_type", "") or ""),
                    "allowed_paths": list(
                        getattr(failing, "allowed_paths", None) or []
                    )[:50],
                },
                "assertions": {
                    "must_not_silently_succeed": True,
                    "expected_failure_class": failure_class,
                    "must_preserve_checkpoint": bool(job.last_checkpoint),
                },
            }
            path = project_state_dir(job.project.root_path) / "evals" / "failures.jsonl"
        finally:
            session.close()

        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            existing = self.load(path)
            by_id = {str(item.get("id")): item for item in existing}
            by_id[case["id"]] = case
            encoded = "\n".join(
                json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                for item in by_id.values()
            )
            path.write_text(encoded + ("\n" if encoded else ""), encoding="utf-8")
        return {**case, "path": str(path)}

    def sync_historical(self) -> list[dict[str, Any]]:
        """Backfill Eval fixtures for terminal failures already in storage."""
        from storage.models import Job

        session = self._session_factory()
        try:
            job_ids = [
                str(value[0]) for value in session.query(Job.job_id).filter(
                    Job.status.in_(("failed", "interrupted", "needs_attention"))
                ).all()
            ]
        finally:
            session.close()
        captured = []
        for job_id in job_ids:
            case = self.capture(job_id)
            if case:
                captured.append(case)
        return captured

    @staticmethod
    def load(path: str | Path) -> list[dict[str, Any]]:
        source = Path(path)
        if not source.is_file():
            return []
        cases = []
        for line in source.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                cases.append(item)
        return cases

    @classmethod
    def evaluate_case(cls, case: dict[str, Any], outcome: dict[str, Any]) -> dict:
        """Evaluate a replayed outcome without asking a model to judge itself."""
        expected = str((case.get("assertions") or {}).get(
            "expected_failure_class", ""
        ))
        actual = cls._failure_class(
            str(outcome.get("failure_code") or ""),
            str(outcome.get("failure_reason") or outcome.get("error") or ""),
        )
        passed = actual == expected and not (
            (case.get("assertions") or {}).get("must_preserve_checkpoint")
            and not outcome.get("checkpoint")
        )
        return {"passed": passed, "expected": expected, "actual": actual}
