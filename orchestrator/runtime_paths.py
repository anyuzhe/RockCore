"""Canonical classification for RockCore-owned project runtime state.

These paths live beside a user's project so jobs can be resumed and audited,
but they are not product source files.  Keeping the rule in one module avoids
merge, review, Git-ignore and checkpoint code disagreeing about ownership.
"""

from __future__ import annotations


RUNTIME_PATH_PREFIXES = (
    ".ai/evals/",
    ".ai/reports/",
    ".ai/runtime/",
    ".ai/recovery/",
    ".ai/worktrees/",
)

RUNTIME_EXACT_PATHS = frozenset({
    ".ai/skill-learning.json",
    ".ai/repository_map.json",
    ".ai/agents.json",
    ".ai/project.md",
    ".ai/architecture.md",
    ".ai/decisions.md",
    ".ai/coding_rules.md",
    ".ai/protected_paths.md",
    ".ai/known_issues.md",
    ".ai/glossary.md",
})


def normalize_project_path(path: str) -> str:
    """Return a repository-relative path using stable POSIX separators."""
    normalized = str(path or "").replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_runtime_path(path: str) -> bool:
    """Whether *path* is generated/owned by RockCore rather than the user."""
    normalized = normalize_project_path(path)
    return normalized in RUNTIME_EXACT_PATHS or any(
        normalized == prefix.rstrip("/") or normalized.startswith(prefix)
        for prefix in RUNTIME_PATH_PREFIXES
    )


def runtime_gitignore_rules() -> tuple[str, ...]:
    """Git-ignore patterns matching the canonical runtime ownership rule."""
    return tuple(RUNTIME_PATH_PREFIXES) + tuple(sorted(RUNTIME_EXACT_PATHS))
