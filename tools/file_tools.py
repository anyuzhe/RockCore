"""File operation tools for the AI worker."""

import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class FileTools:
    """Safe file operations limited to project root."""

    def __init__(self, project_root: str):
        if not project_root or not project_root.strip():
            import os
            project_root = os.getcwd()
            logger.warning(f"FileTools: empty project_root, falling back to cwd: {project_root}")
        self.project_root = Path(project_root).resolve()

    def _resolve_path(self, path: str) -> Path:
        """Resolve a path relative to project root, preventing traversal.
        AI must use relative paths only — absolute paths are rejected."""
        # Expand ~ and normalize
        path = os.path.expanduser(path)
        p = Path(path)
        if p.is_absolute():
            raise PermissionError(
                f"Absolute path not allowed: {path}. Use relative paths like 'src/file.py'."
            )
        p = (self.project_root / p).resolve()
        # Use is_relative_to for robust containment check
        if not p.is_relative_to(self.project_root):
            raise PermissionError(f"Path outside project root: {path}")
        return p

    async def list_files(self, path: str = ".", pattern: str | None = None,
                          **kwargs) -> dict:
        """List files in a directory within the project."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"Path does not exist: {path}", "files": []}
        if not resolved.is_dir():
            return {"error": f"Not a directory: {path}", "files": []}

        files = []
        for f in resolved.iterdir():
            if pattern and not f.match(pattern):
                continue
            files.append({
                "name": f.name,
                "path": str(f.relative_to(self.project_root)),
                "type": "directory" if f.is_dir() else "file",
                "size": f.stat().st_size if f.is_file() else 0,
            })

        files.sort(key=lambda x: (x["type"] != "directory", x["name"]))
        return {"files": files, "count": len(files)}

    async def read_file(self, path: str, start: int = 0, end: int = 0,
                          max_size: int = 1024 * 1024, **kwargs) -> dict:
        """Read a file's contents, optionally with line-range pagination.
        start/end are 1-indexed line numbers. 0 means from beginning/to end."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}"}
        if not resolved.is_file():
            return {"error": f"Not a file: {path}"}

        size = resolved.stat().st_size
        if size > max_size:
            return {"error": f"File too large ({size} bytes, max {max_size})"}

        content = resolved.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        total_lines = len(lines)

        # Pagination support
        start_idx = max(0, (start - 1) if start > 0 else 0)
        end_idx = min(total_lines, end if end > 0 else total_lines)

        if start_idx > 0 or end_idx < total_lines:
            lines = lines[start_idx:end_idx]
            content = "\n".join(lines)

        result = {
            "content": content,
            "path": str(resolved.relative_to(self.project_root)),
            "size": size,
            "total_lines": total_lines,
        }

        # Add pagination metadata
        if start > 0 or end > 0:
            result["start_line"] = start_idx + 1
            result["end_line"] = end_idx
            result["has_more"] = end_idx < total_lines
            result["next_start"] = end_idx + 1 if end_idx < total_lines else None
        elif total_lines > 500:
            # Auto-truncate for large files and tell the model
            result["start_line"] = 1
            result["end_line"] = min(400, total_lines)
            result["total_lines"] = total_lines
            result["has_more"] = total_lines > 400
            result["next_start"] = 401 if total_lines > 400 else None
            result["content"] = "\n".join(content.split("\n")[:400])
            result["_note"] = f"File has {total_lines} lines. Use start={401} to read more."

        return result

    async def write_file(self, path: str, content: str, **kwargs) -> dict:
        """Write content to a file (overwrite)."""
        resolved = self._resolve_path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        logger.info(f"[file_written] path={resolved} bytes={len(content)}")
        return {
            "path": str(resolved.relative_to(self.project_root)),
            "absolute_path": str(resolved),
            "size": len(content),
            "status": "written",
        }

    async def search_in_file(self, path: str, text: str, context: int = 3) -> dict:
        """Search for text within a specific file. Returns matching lines with context."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "matches": []}
        if not resolved.is_file():
            return {"error": f"Not a file: {path}", "matches": []}

        content = resolved.read_text(encoding="utf-8", errors="replace")
        lines = content.split("\n")
        matches = []
        text_lower = text.lower()

        for i, line in enumerate(lines, 1):
            if text_lower in line.lower():
                start = max(0, i - context - 1)
                end = min(len(lines), i + context)
                snippet_lines = []
                for j in range(start, end):
                    prefix = ">>>" if j == i - 1 else "   "
                    snippet_lines.append(f"{prefix} {j+1:5d}: {lines[j][:200]}")
                matches.append({
                    "line": i,
                    "content": line.strip()[:200],
                    "context": "\n".join(snippet_lines),
                })

        return {
            "path": str(resolved.relative_to(self.project_root)),
            "matches": matches[:20],
            "count": len(matches),
            "total_lines": len(lines),
        }

    async def apply_patch(self, path: str, search: str, replace: str) -> dict:
        """Apply a search-and-replace patch to a file.
        Returns match_count and reason on failure — no more silent '0 changes'."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "status": "failed", "reason": "file_not_found"}

        content = resolved.read_text(encoding="utf-8")
        match_count = content.count(search)

        if match_count == 0:
            return {
                "status": "no_match",
                "reason": "search_text_not_found",
                "hint": "The exact text was not found. Check whitespace, indentation, or use search_in_file to locate the correct text.",
                "path": str(resolved.relative_to(self.project_root)),
            }

        if match_count > 1:
            # Show context of each match to help the model disambiguate
            return {
                "status": "ambiguous",
                "reason": f"search_text_matched_{match_count}_times",
                "hint": f"The search text appeared {match_count} times. Use more surrounding context to make it unique, or use insert_before/insert_after with a unique anchor.",
                "match_count": match_count,
                "path": str(resolved.relative_to(self.project_root)),
            }

        new_content = content.replace(search, replace, 1)
        resolved.write_text(new_content, encoding="utf-8")
        old_lines = content.count("\n")
        new_lines = new_content.count("\n")
        return {
            "path": str(resolved.relative_to(self.project_root)),
            "status": "patched",
            "line_delta": new_lines - old_lines,
        }

    async def insert_before(self, path: str, anchor: str, content: str) -> dict:
        """Insert text before the first occurrence of anchor in a file."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "status": "failed"}

        text = resolved.read_text(encoding="utf-8")
        match_count = text.count(anchor)

        if match_count == 0:
            return {
                "status": "no_match",
                "reason": "anchor_not_found",
                "hint": f"The anchor text was not found. Use search_in_file to locate the correct insertion point.",
                "path": str(resolved.relative_to(self.project_root)),
            }

        if match_count > 1:
            return {
                "status": "ambiguous",
                "reason": f"anchor_matched_{match_count}_times",
                "hint": f"The anchor appeared {match_count} times. Use a longer/more unique anchor to pinpoint the location.",
                "match_count": match_count,
                "path": str(resolved.relative_to(self.project_root)),
            }

        idx = text.index(anchor)
        new_text = text[:idx] + content + text[idx:]
        resolved.write_text(new_text, encoding="utf-8")
        return {
            "path": str(resolved.relative_to(self.project_root)),
            "status": "inserted",
            "position": idx,
        }

    async def insert_after(self, path: str, anchor: str, content: str) -> dict:
        """Insert text after the first occurrence of anchor in a file."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "status": "failed"}

        text = resolved.read_text(encoding="utf-8")
        match_count = text.count(anchor)

        if match_count == 0:
            return {
                "status": "no_match",
                "reason": "anchor_not_found",
                "hint": f"The anchor text was not found. Use search_in_file to locate the correct insertion point.",
                "path": str(resolved.relative_to(self.project_root)),
            }

        if match_count > 1:
            return {
                "status": "ambiguous",
                "reason": f"anchor_matched_{match_count}_times",
                "hint": f"The anchor appeared {match_count} times. Use a longer/more unique anchor to pinpoint the location.",
                "match_count": match_count,
                "path": str(resolved.relative_to(self.project_root)),
            }

        idx = text.index(anchor) + len(anchor)
        new_text = text[:idx] + content + text[idx:]
        resolved.write_text(new_text, encoding="utf-8")
        return {
            "path": str(resolved.relative_to(self.project_root)),
            "status": "inserted",
            "position": idx,
        }