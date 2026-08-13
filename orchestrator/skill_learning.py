"""Suggest reusable Skills after repeated successful workflow patterns."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app.paths import project_state_dir


class SkillLearningService:
    def __init__(self, session_factory, threshold: int = 3):
        self._session_factory = session_factory
        self.threshold = max(2, int(threshold))

    @staticmethod
    def _slug(value: str) -> str:
        words = re.findall(r"[a-z0-9]+", value.lower())
        return "-".join(words[:6])[:48] or "project-workflow"

    def observe(self, job_id: str) -> dict | None:
        from storage.models import Job

        session = self._session_factory()
        try:
            job = session.query(Job).filter(Job.job_id == job_id).first()
            if not job or job.status != "done" or not job.project:
                return None
            tasks = sorted(job.tasks, key=lambda item: item.order)
            signature_parts = []
            for task in tasks:
                extensions = sorted({
                    Path(path).suffix.lower() or Path(path).name.lower()
                    for path in (task.allowed_paths or []) if path
                })
                signature_parts.append(
                    f"{task.task_type}:{','.join(task.skills or [])}:"
                    f"{','.join(extensions)}"
                )
            signature = "|".join(signature_parts) or "single:general"
            root = project_state_dir(job.project.root_path)
            path = root / "skill-learning.json"
        finally:
            session.close()

        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            state = {"patterns": {}}
        patterns = dict(state.get("patterns") or {})
        item = dict(patterns.get(signature) or {})
        observed_jobs = list(item.get("job_ids") or [])
        if job_id not in observed_jobs:
            observed_jobs.append(job_id)
        item["job_ids"] = observed_jobs[-50:]
        item["count"] = len(item["job_ids"])
        item["last_job_id"] = job_id
        item["updated_at"] = datetime.now().astimezone().isoformat()
        item["suggested_name"] = self._slug(
            "-".join(task.skills[0] for task in tasks if task.skills)
        )
        patterns[signature] = item
        state["patterns"] = patterns
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        if item["count"] < self.threshold or item.get("dismissed"):
            return None
        return {
            "name": item["suggested_name"], "count": item["count"],
            "source_job_id": job_id, "path": str(path),
            "message": (
                f"这个流程已成功重复 {item['count']} 次，可以沉淀为项目 Skill。"
            ),
        }
