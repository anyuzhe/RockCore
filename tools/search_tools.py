"""Code search tools for the AI worker."""

import hashlib
import os
import re
from pathlib import Path
from typing import Any

from app.text_utils import read_text_compatible


class SearchTools:
    """Code search tools (grep, find, etc.) limited to project root."""

    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()

    def _resolve_path(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.project_root / p
        p = p.resolve()
        if not p.is_relative_to(self.project_root):
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
        source_state: list[str] = []
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
                    stat = fpath.stat()
                    source_state.append(
                        f"{fpath.relative_to(self.project_root).as_posix()}:"
                        f"{stat.st_mtime_ns}:{stat.st_size}"
                    )
                    content, _ = read_text_compatible(fpath)
                    for line_no, line in enumerate(content.splitlines(), 1):
                        if pattern in line or re.search(pattern, line):
                            rel_path = fpath.relative_to(self.project_root)
                            results.append({
                                "file": rel_path.as_posix(),
                                "line": line_no,
                                "content": line.rstrip()[:200],
                            })
                            count += 1
                            if count >= max_results:
                                return {
                                    "results": results,
                                    "count": count,
                                    "truncated": True,
                                    "source_version": hashlib.sha256(
                                        "\n".join(source_state).encode("utf-8")
                                    ).hexdigest(),
                                }
                except (IOError, UnicodeDecodeError):
                    continue

        return {
            "results": results,
            "count": count,
            "truncated": False,
            "source_version": hashlib.sha256(
                "\n".join(source_state).encode("utf-8")
            ).hexdigest(),
        }

    async def read_log(self, path: str, tail: int = 50) -> dict:
        """Read the last N lines of a file."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}"}

        content, file_encoding = read_text_compatible(resolved)
        stat = resolved.stat()
        lines = content.splitlines()

        tail_lines = lines[-tail:]
        return {
            "path": resolved.relative_to(self.project_root).as_posix(),
            "lines": [line.rstrip() for line in tail_lines],
            "total_lines": len(lines),
            "showing": min(tail, len(lines)),
            "encoding": file_encoding,
            "source_version": f"{stat.st_mtime_ns}:{stat.st_size}",
        }
