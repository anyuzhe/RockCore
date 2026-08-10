"""Risk Engine — scores task risk from 0-100 for V6 smart routing."""

import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HIGH_RISK_PATHS = [
    "db", "database", "migration", "schema",
    "auth", "security", "oauth", "login",
    "payment", "billing", "checkout",
    "core", "kernel", "engine",
]

HIGH_RISK_EXTENSIONS = {".py", ".js", ".ts", ".java", ".go", ".rs"}

REQUEST_HIGH_RISK_MARKERS = (
    "数据库", "database", "schema", "migration", "迁移",
    "认证", "auth", "oauth", "登录", "security", "安全",
    "协议", "protocol", "public api", "公共 api", "公共接口",
    "兼容性", "dependency", "依赖", "package", "构建", "build",
    "git", "分支", "合并", "并发", "线程", "concurrent", "thread",
)
REQUEST_DESTRUCTIVE_MARKERS = (
    "删除", "清空", "drop table", "truncate", "批量移除",
    "delete", "remove all", "reset --hard",
)
REQUEST_LOW_RISK_MARKERS = (
    "html", "css", "markdown", "文档", "readme", "静态页面",
    "文案", "文字", "颜色", "样式", "排版", "错别字", "typo",
)


class RiskEngine:
    """Evaluates task risk based on file changes, module type, and history.

    Risk score (0-100):
    - 0-30: Low risk (docs, config, tests)
    - 31-60: Medium risk (feature code, non-core changes)
    - 61-80: High risk (core modules, database)
    - 81-100: Critical risk (auth, security, payment)
    """

    def __init__(self):
        self._history: dict[str, list[dict]] = {}

    def precheck_request(self, user_request: str,
                         project_root: str | Path) -> dict:
        """Provide a deterministic fallback if Governor cannot assess risk."""
        request = str(user_request or "")
        normalized = request.lower()
        root = Path(project_root)
        score = 35
        reasons = ["基础风险=35"]

        high_markers = [
            marker for marker in REQUEST_HIGH_RISK_MARKERS
            if marker in normalized
        ]
        destructive = [
            marker for marker in REQUEST_DESTRUCTIVE_MARKERS
            if marker in normalized
        ]
        low_markers = [
            marker for marker in REQUEST_LOW_RISK_MARKERS
            if marker in normalized
        ]
        if high_markers:
            score += 45
            reasons.append("高风险领域=" + ",".join(high_markers[:4]))
        if destructive:
            score += 35
            reasons.append("破坏性操作=" + ",".join(destructive[:3]))
        if low_markers and not high_markers and not destructive:
            score -= 15
            reasons.append("低风险内容=" + ",".join(low_markers[:4]))

        mentioned_paths = sorted(set(re.findall(
            r"(?<![\w.-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}",
            request,
        )))
        if len(mentioned_paths) > 10:
            score += 25
            reasons.append(f"涉及文件>{10}")
        elif len(mentioned_paths) > 3:
            score += 10
            reasons.append(f"涉及文件={len(mentioned_paths)}")
        elif any(Path(path).suffix.lower() in HIGH_RISK_EXTENSIONS
                 for path in mentioned_paths):
            score += 10
            reasons.append("涉及源码文件")

        vague_continuation = (
            len(request.strip()) < 20
            and any(marker in request for marker in ("继续", "接着", "完成它"))
        )
        if vague_continuation:
            score += 15
            reasons.append("继续需求上下文较少")

        has_tests = False
        if root.is_dir():
            try:
                has_tests = any(root.glob("test_*.py")) or any(
                    root.glob("tests/**/*")
                )
            except OSError:
                has_tests = False
        if not has_tests:
            score += 5
            reasons.append("未检测到现有测试")

        score = max(0, min(score, 100))
        level = self.get_risk_level(score)
        route = "high" if level in {"high", "critical"} else level
        return {
            "score": score,
            "level": level,
            "route": route,
            "reasons": reasons,
            "mentioned_paths": mentioned_paths,
            "has_tests": has_tests,
        }

    async def evaluate_task(self, task) -> int:
        """Score a task's risk level from 0-100."""
        score = 0
        reasons = []

        # Factor 1: Task type (0-30)
        type_risk = {
            "analysis": 5,
            "testing": 10,
            "review": 5,
            "coding": 20,
            "refactor": 25,
            "database": 30,
        }
        score += type_risk.get(task.task_type, 15)
        reasons.append(f"type={task.task_type}")

        # Factor 2: Allowed paths (0-30)
        for path in (task.allowed_paths or []):
            path_lower = path.lower()
            for keyword in HIGH_RISK_PATHS:
                if keyword in path_lower:
                    score += 15
                    reasons.append(f"path_risk={path}")
                    break
            ext = Path(path).suffix.lower()
            if ext in HIGH_RISK_EXTENSIONS:
                score += 5
                reasons.append(f"source_code={path}")

        # Factor 3: File count (0-20)
        file_count = len(task.allowed_paths or [])
        if file_count > 10:
            score += 15
            reasons.append(f"many_files={file_count}")
        elif file_count > 5:
            score += 10
            reasons.append(f"moderate_files={file_count}")

        # Factor 4: Historical failure rate (0-20)
        task_id = getattr(task, "task_id", str(id(task)))
        if task_id in self._history:
            failed = sum(1 for r in self._history[task_id] if r.get("status") == "failed")
            total = len(self._history[task_id])
            if total > 0:
                fail_rate = failed / total
                score += int(fail_rate * 20)
                reasons.append(f"history_fail_rate={fail_rate:.0%}")

        final_score = min(score, 100)
        logger.debug(f"Risk score {final_score} for {task_id}: {reasons}")
        return final_score

    async def record_result(self, task_id: str, result: dict):
        """Record task execution result for future risk scoring."""
        self._history.setdefault(task_id, []).append(result)

    def get_risk_level(self, score: int) -> str:
        if score <= 30:
            return "low"
        elif score <= 60:
            return "medium"
        elif score <= 80:
            return "high"
        return "critical"

    def get_suggested_model(self, score: int) -> str:
        """Suggest a model based on risk score."""
        if score <= 30:
            return "worker"  # Fast Flash
        elif score <= 60:
            return "worker"  # Flash with retry
        elif score <= 80:
            return "planner"  # Kimi for complex tasks
        return "emergency_coder"  # Codex for critical
