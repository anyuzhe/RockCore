"""Code search tools for the AI worker."""

import os
import re
from pathlib import Path
from typing import Any


class SearchTools:
    """Code search tools (grep, find, etc.) limited to project root."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    def _resolve_path(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.project_root / p
        p = p.resolve()
        if not str(p).startswith(str(self.project_root)):
            raise PermissionError(f"Path outside project root: {path}")
        return p

    async def search_code(self, pattern: str, path: str = ".",
                          glob_pattern: str | None = None,
                          max_results: int = 50) -> dict:
        """Search for a pattern in code files."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"Path not found: {path}", "results": []}

        results = []
        count = 0
        for root, dirs, files in os.walk(resolved):
            # Skip hidden dirs and common non-source dirs
            dirs[:] = [d for d in dirs if not d.startswith(".")
                       and d not in ("node_modules", "__pycache__",
                                     ".venv", "venv", "env", "dist", "build")]
            for fname in files:
                if glob_pattern:
                    from fnmatch import fnmatch
                    if not fnmatch(fname, glob_pattern):
                        continue
                fpath = Path(root) / fname
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for line_no, line in enumerate(f, 1):
                            if pattern in line or re.search(pattern, line):
                                rel_path = fpath.relative_to(self.project_root)
                                results.append({
                                    "file": str(rel_path),
                                    "line": line_no,
                                    "content": line.rstrip()[:200],
                                })
                                count += 1
                                if count >= max_results:
                                    return {"results": results, "count": count}
                except (IOError, UnicodeDecodeError):
                    continue

        return {"results": results, "count": count}

    async def read_log(self, path: str, tail: int = 50) -> dict:
        """Read the last N lines of a file."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}"}

        with open(resolved, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        tail_lines = lines[-tail:]
        return {
            "path": str(resolved.relative_to(self.project_root)),
            "lines": [l.rstrip() for l in tail_lines],
            "total_lines": len(lines),
            "showing": min(tail, len(lines)),
        }