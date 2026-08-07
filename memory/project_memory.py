"""Project Memory — manages .ai/ project knowledge base for V5."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MEMORY_FILES = {
    "project": "project.md",
    "architecture": "architecture.md",
    "decisions": "decisions.md",
    "coding_rules": "coding_rules.md",
    "protected_paths": "protected_paths.md",
    "known_issues": "known_issues.md",
    "glossary": "glossary.md",
}


class ProjectMemory:
    """Manages the .ai/ project knowledge base directory.

    Each project has a .ai/ directory with markdown files that capture
    project-level knowledge accumulated across jobs.
    """

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.ai_dir = self.project_root / ".ai"
        self.ai_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_defaults()

    def _ensure_defaults(self):
        """Create default memory files if they don't exist."""
        for name, filename in MEMORY_FILES.items():
            path = self.ai_dir / filename
            if not path.exists():
                headers = {
                    "project": "# Project Overview\n\n",
                    "architecture": "# Architecture\n\n",
                    "decisions": "# Design Decisions\n\n",
                    "coding_rules": "# Coding Rules\n\n",
                    "protected_paths": "# Protected Paths\n\n",
                    "known_issues": "# Known Issues\n\n",
                    "glossary": "# Glossary\n\n",
                }
                path.write_text(headers.get(name, ""))

    def read_memory(self, name: str) -> str:
        """Read a memory file by name."""
        filename = MEMORY_FILES.get(name)
        if not filename:
            logger.warning(f"Unknown memory file: {name}")
            return ""
        path = self.ai_dir / filename
        if not path.exists():
            return ""
        return path.read_text()

    def write_memory(self, name: str, content: str):
        """Write content to a memory file."""
        filename = MEMORY_FILES.get(name)
        if not filename:
            logger.warning(f"Unknown memory file: {name}")
            return
        path = self.ai_dir / filename
        header = f"# {name.replace('_', ' ').title()}\n\n"
        timestamp = f"\n\n---\n*Updated: {datetime.now(timezone.utc).isoformat()}*\n"
        path.write_text(header + content.strip() + timestamp)
        logger.info(f"Updated memory: {name}")

    def append_memory(self, name: str, entry: str):
        """Append an entry to a memory file."""
        existing = self.read_memory(name)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        new_entry = f"\n- [{timestamp}] {entry}\n"
        self.write_memory(name, existing + new_entry)

    def get_all_memories(self) -> dict[str, str]:
        """Read all memory files into a dict."""
        return {name: self.read_memory(name) for name in MEMORY_FILES}

    def get_context_summary(self) -> str:
        """Get a condensed summary of all memories for prompt injection."""
        parts = []
        for name, filename in MEMORY_FILES.items():
            content = self.read_memory(name)
            if content and len(content) > 50:
                # Truncate to first 500 chars
                summary = content[:500].strip()
                if len(content) > 500:
                    summary += "..."
                parts.append(f"=== {name.replace('_', ' ').title()} ===\n{summary}")
        return "\n\n".join(parts)

    @property
    def exists(self) -> bool:
        return self.ai_dir.exists()