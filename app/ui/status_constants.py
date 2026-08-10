"""UI status metadata with no Qt runtime dependency.

Keep these values importable by orchestration and regression tests on headless
systems. Widget classes can consume them, but this module must stay Qt-free.
"""

STATUS_STYLE = {
    "done": {"icon": "✓", "color": "#55a86b", "text": "已完成"},
    "success": {"icon": "✓", "color": "#55a86b", "text": "已完成"},
    "passed": {"icon": "✓", "color": "#55a86b", "text": "已通过"},
    "failed": {"icon": "!", "color": "#d96868", "text": "失败"},
    "blocked": {"icon": "−", "color": "#8f8f98", "text": "已阻塞"},
    "rejected": {"icon": "!", "color": "#d9914f", "text": "未通过"},
    "fallback": {"icon": "!", "color": "#d9914f", "text": "已降级"},
    "cancelled": {"icon": "×", "color": "#8f8f98", "text": "已停止"},
    "interrupted": {"icon": "!", "color": "#d9914f", "text": "已中断，可继续"},
    "needs_attention": {"icon": "!", "color": "#d9914f", "text": "需处理"},
    "skipped": {"icon": "−", "color": "#8f8f98", "text": "已跳过"},
    "executing": {"icon": "●", "color": "#d4a94f", "text": "执行中"},
    "reviewing": {"icon": "●", "color": "#d4a94f", "text": "审核中"},
    "governing": {"icon": "●", "color": "#d4a94f", "text": "分析中"},
    "planning": {"icon": "●", "color": "#d4a94f", "text": "规划中"},
    "running": {"icon": "●", "color": "#d4a94f", "text": "运行中"},
    "created": {"icon": "○", "color": "#8f8f98", "text": "等待中"},
    "pending": {"icon": "○", "color": "#74747e", "text": "等待中"},
    "idle": {"icon": "○", "color": "#74747e", "text": "等待中"},
}

ACTIVE_STATUSES = {"executing", "reviewing", "governing", "planning", "running"}
