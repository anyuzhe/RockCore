"""File operation tools for the AI worker."""

import logging
import os
import re
from pathlib import Path
from typing import Any

from app.text_utils import (
    encode_text_compatible,
    read_text_compatible,
    write_text_compatible,
)

logger = logging.getLogger(__name__)


class FileTools:
    """Safe file operations limited to project root."""

    def __init__(self, project_root: str | os.PathLike[str] | None):
        # Config defaults may be pathlib.Path objects (notably on Windows).
        # Normalize before string-only validation such as ``strip``.
        project_root = os.fspath(project_root) if project_root is not None else ""
        if not project_root or not project_root.strip():
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

    def _relative_path(self, path: Path) -> str:
        """Return the model/API path with platform-independent separators."""
        return path.relative_to(self.project_root).as_posix()

    async def list_files(self, path: str = ".", pattern: str | None = None,
                          **kwargs) -> dict:
        """List files in a directory within the project."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"Path does not exist: {path}", "files": []}
        if not resolved.is_dir():
            return {"error": f"Not a directory: {path}", "files": []}

        files = []
        source_entries: list[str] = []
        for f in resolved.iterdir():
            if pattern and not f.match(pattern):
                continue
            stat = f.stat()
            relative_path = self._relative_path(f)
            item_type = "directory" if f.is_dir() else "file"
            files.append({
                "name": f.name,
                "path": relative_path,
                "type": item_type,
                "size": stat.st_size if f.is_file() else 0,
            })
            source_entries.append(
                f"{relative_path}:{item_type}:{stat.st_mtime_ns}:{stat.st_size}"
            )

        files.sort(key=lambda x: (x["type"] != "directory", x["name"]))
        directory_version = "|".join(sorted(source_entries))
        return {
            "files": files, "count": len(files),
            "source_version": directory_version,
        }

    async def read_file(self, path: str, start: int = 0, end: int = 0,
                          max_size: int = 1024 * 1024, **kwargs) -> dict:
        """Read a file's contents, optionally with line-range pagination.
        start/end are 1-indexed line numbers. 0 means from beginning/to end."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}"}
        if not resolved.is_file():
            return {"error": f"Not a file: {path}"}

        stat = resolved.stat()
        size = stat.st_size
        if size > max_size:
            return {"error": f"File too large ({size} bytes, max {max_size})"}

        content, file_encoding = read_text_compatible(resolved)
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
            "path": self._relative_path(resolved),
            "size": size,
            "total_lines": total_lines,
            "encoding": file_encoding,
            "source_version": f"{stat.st_mtime_ns}:{stat.st_size}",
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

    async def read_pdf(self, path: str, start_page: int = 1,
                       end_page: int = 0, max_chars: int = 16_000,
                       **kwargs) -> dict:
        """Extract a bounded page range from a PDF inside the project.

        Page ranges are intentionally limited so a model can process a long
        document incrementally without putting the entire book in one prompt.
        """
        resolved = self._resolve_path(path)
        relative_path = self._relative_path(resolved)
        if not resolved.exists():
            return {
                "status": "error", "error_code": "file_not_found",
                "error": f"File not found: {path}", "path": relative_path,
            }
        if not resolved.is_file():
            return {
                "status": "error", "error_code": "not_a_file",
                "error": f"Not a file: {path}", "path": relative_path,
            }
        if resolved.suffix.lower() != ".pdf":
            return {
                "status": "error", "error_code": "not_a_pdf",
                "error": f"Not a PDF file: {path}", "path": relative_path,
            }

        stat = resolved.stat()
        source_version = f"{stat.st_mtime_ns}:{stat.st_size}"

        try:
            from pypdf import PdfReader
        except ImportError:
            return {
                "status": "error", "error_code": "pdf_dependency_missing",
                "error": (
                    "Built-in PDF support is unavailable. Install RockCore's "
                    "declared dependencies and restart the application."
                ),
                "path": relative_path,
            }

        try:
            reader = PdfReader(str(resolved))
        except Exception as error:
            return {
                "status": "error", "error_code": "pdf_invalid",
                "error": f"PDF could not be opened: {error}",
                "path": relative_path,
            }

        encrypted = bool(getattr(reader, "is_encrypted", False))
        if encrypted:
            try:
                unlocked = bool(reader.decrypt(""))
            except Exception:
                unlocked = False
            if not unlocked:
                return {
                    "status": "password_required",
                    "error_code": "pdf_password_required",
                    "error": (
                        "PDF is encrypted and requires a password. Provide an "
                        "unlocked copy of the file, then continue this task."
                    ),
                    "path": relative_path,
                    "encrypted": True,
                }

        try:
            total_pages = len(reader.pages)
        except Exception as error:
            return {
                "status": "error", "error_code": "pdf_read_failed",
                "error": f"PDF pages could not be read: {error}",
                "path": relative_path, "encrypted": encrypted,
            }
        if total_pages == 0:
            return {
                "status": "error", "error_code": "pdf_empty",
                "error": "PDF contains no pages.", "path": relative_path,
                "page_count": 0, "encrypted": encrypted,
            }

        start_page = max(1, int(start_page or 1))
        if start_page > total_pages:
            return {
                "status": "error", "error_code": "page_out_of_range",
                "error": (
                    f"Start page {start_page} exceeds the PDF page count "
                    f"({total_pages})."
                ),
                "path": relative_path, "page_count": total_pages,
            }
        requested_end = int(end_page or 0)
        if requested_end <= 0:
            requested_end = start_page + 7
        requested_end = max(start_page, requested_end)
        # At most 8 pages and 16k characters per call keeps page extraction
        # predictable while still being efficient for long-form reading.
        requested_end = min(total_pages, requested_end, start_page + 7)
        max_chars = max(2_000, min(16_000, int(max_chars or 16_000)))

        chunks: list[str] = []
        extracted_chars = 0
        truncated = False
        last_page = start_page - 1
        for page_number in range(start_page, requested_end + 1):
            try:
                page_text = reader.pages[page_number - 1].extract_text() or ""
            except Exception as error:
                page_text = f"[Page extraction failed: {error}]"
            page_chunk = f"\n--- Page {page_number} ---\n{page_text.strip()}"
            remaining = max_chars - extracted_chars
            if remaining <= 0:
                truncated = True
                break
            if len(page_chunk) > remaining:
                page_chunk = page_chunk[:remaining]
                truncated = True
            chunks.append(page_chunk)
            extracted_chars += len(page_chunk)
            last_page = page_number
            if truncated:
                break

        content = "".join(chunks).lstrip()
        has_more = last_page < total_pages or truncated
        next_page = last_page + 1 if last_page < total_pages else None
        if not content or not any(
            line.strip() and not line.startswith("--- Page")
            for line in content.splitlines()
        ):
            # A blank cover/front-matter range is not proof that the whole PDF
            # needs OCR. Sample the beginning, middle, and end before stopping
            # the workflow for user action.
            sample_indexes = {0, total_pages // 2, total_pages - 1}
            sampled_text = False
            for page_index in sample_indexes:
                try:
                    if (reader.pages[page_index].extract_text() or "").strip():
                        sampled_text = True
                        break
                except Exception:
                    continue
            if sampled_text:
                return {
                    "status": "empty_page_range",
                    "message": (
                        "This page range contains no text, but other sampled "
                        "pages are extractable. Continue with next_page when present."
                    ),
                    "path": relative_path,
                    "page_start": start_page,
                    "page_end": last_page,
                    "page_count": total_pages,
                    "has_more": has_more,
                    "next_page": next_page,
                    "encrypted": encrypted,
                    "source_version": source_version,
                }
            return {
                "status": "no_extractable_text",
                "error_code": "pdf_ocr_required",
                "error": (
                    "No extractable text was found in this page range. The PDF "
                    "may be scanned or image-only and requires OCR."
                ),
                "path": relative_path,
                "page_start": start_page,
                "page_end": last_page,
                "page_count": total_pages,
                "has_more": has_more,
                "next_page": next_page,
                "encrypted": encrypted,
                "source_version": source_version,
            }

        return {
            "status": "success",
            "content": content,
            "path": relative_path,
            "page_start": start_page,
            "page_end": last_page,
            "page_count": total_pages,
            "has_more": has_more,
            "next_page": next_page,
            "truncated": truncated,
            "extracted_chars": extracted_chars,
            "encrypted": encrypted,
            "source_version": source_version,
        }

    async def write_file(self, path: str, content: str,
                         encoding: str = "preserve", **kwargs) -> dict:
        """Write content to a file (overwrite)."""
        resolved = self._resolve_path(path)
        file_encoding = str(encoding or "preserve").lower()
        if file_encoding == "preserve":
            if resolved.is_file():
                _, file_encoding = read_text_compatible(resolved)
            else:
                file_encoding = "utf-8"
        try:
            encoded = encode_text_compatible(content, file_encoding)
        except (LookupError, UnicodeEncodeError) as error:
            return {
                "path": self._relative_path(resolved),
                "status": "encoding_error",
                "error": (
                    f"Content cannot be encoded as {file_encoding}: {error}. "
                    "Retry write_file with encoding='utf-8' to explicitly "
                    "convert the file."
                ),
                "encoding": file_encoding,
            }
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(encoded)
        logger.info(f"[file_written] path={resolved} bytes={len(encoded)}")
        return {
            "path": self._relative_path(resolved),
            "absolute_path": str(resolved),
            "size": len(encoded),
            "encoding": file_encoding,
            "status": "written",
        }

    async def search_in_file(self, path: str, text: str, context: int = 3) -> dict:
        """Search for text within a specific file. Returns matching lines with context."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "matches": []}
        if not resolved.is_file():
            return {"error": f"Not a file: {path}", "matches": []}

        content, file_encoding = read_text_compatible(resolved)
        stat = resolved.stat()
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
            "path": self._relative_path(resolved),
            "matches": matches[:20],
            "count": len(matches),
            "total_lines": len(lines),
            "encoding": file_encoding,
            "source_version": f"{stat.st_mtime_ns}:{stat.st_size}",
        }

    async def apply_patch(self, path: str, search: str, replace: str) -> dict:
        """Apply a search-and-replace patch to a file.
        Returns match_count and reason on failure — no more silent '0 changes'."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "status": "failed", "reason": "file_not_found"}

        content, file_encoding = read_text_compatible(resolved)
        match_count = content.count(search)

        if match_count == 0:
            return {
                "status": "no_match",
                "reason": "search_text_not_found",
                "hint": "The exact text was not found. Check whitespace, indentation, or use search_in_file to locate the correct text.",
                "path": self._relative_path(resolved),
            }

        if match_count > 1:
            # Show context of each match to help the model disambiguate
            return {
                "status": "ambiguous",
                "reason": f"search_text_matched_{match_count}_times",
                "hint": f"The search text appeared {match_count} times. Use more surrounding context to make it unique, or use insert_before/insert_after with a unique anchor.",
                "match_count": match_count,
                "path": self._relative_path(resolved),
            }

        new_content = content.replace(search, replace, 1)
        try:
            write_text_compatible(resolved, new_content, file_encoding)
        except (LookupError, UnicodeEncodeError, ValueError) as error:
            return {
                "path": self._relative_path(resolved),
                "status": "encoding_error",
                "error": f"Patch cannot preserve {file_encoding}: {error}",
                "encoding": file_encoding,
            }
        old_lines = content.count("\n")
        new_lines = new_content.count("\n")
        return {
            "path": self._relative_path(resolved),
            "status": "patched",
            "line_delta": new_lines - old_lines,
            "encoding": file_encoding,
        }

    async def insert_before(self, path: str, anchor: str, content: str) -> dict:
        """Insert text before the first occurrence of anchor in a file."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "status": "failed"}

        text, file_encoding = read_text_compatible(resolved)
        match_count = text.count(anchor)

        if match_count == 0:
            return {
                "status": "no_match",
                "reason": "anchor_not_found",
                "hint": f"The anchor text was not found. Use search_in_file to locate the correct insertion point.",
                "path": self._relative_path(resolved),
            }

        if match_count > 1:
            return {
                "status": "ambiguous",
                "reason": f"anchor_matched_{match_count}_times",
                "hint": f"The anchor appeared {match_count} times. Use a longer/more unique anchor to pinpoint the location.",
                "match_count": match_count,
                "path": self._relative_path(resolved),
            }

        idx = text.index(anchor)
        new_text = text[:idx] + content + text[idx:]
        try:
            write_text_compatible(resolved, new_text, file_encoding)
        except (LookupError, UnicodeEncodeError, ValueError) as error:
            return {
                "path": self._relative_path(resolved),
                "status": "encoding_error",
                "error": f"Insert cannot preserve {file_encoding}: {error}",
                "encoding": file_encoding,
            }
        return {
            "path": self._relative_path(resolved),
            "status": "inserted",
            "position": idx,
            "encoding": file_encoding,
        }

    async def insert_after(self, path: str, anchor: str, content: str) -> dict:
        """Insert text after the first occurrence of anchor in a file."""
        resolved = self._resolve_path(path)
        if not resolved.exists():
            return {"error": f"File not found: {path}", "status": "failed"}

        text, file_encoding = read_text_compatible(resolved)
        match_count = text.count(anchor)

        if match_count == 0:
            return {
                "status": "no_match",
                "reason": "anchor_not_found",
                "hint": f"The anchor text was not found. Use search_in_file to locate the correct insertion point.",
                "path": self._relative_path(resolved),
            }

        if match_count > 1:
            return {
                "status": "ambiguous",
                "reason": f"anchor_matched_{match_count}_times",
                "hint": f"The anchor appeared {match_count} times. Use a longer/more unique anchor to pinpoint the location.",
                "match_count": match_count,
                "path": self._relative_path(resolved),
            }

        idx = text.index(anchor) + len(anchor)
        new_text = text[:idx] + content + text[idx:]
        try:
            write_text_compatible(resolved, new_text, file_encoding)
        except (LookupError, UnicodeEncodeError, ValueError) as error:
            return {
                "path": self._relative_path(resolved),
                "status": "encoding_error",
                "error": f"Insert cannot preserve {file_encoding}: {error}",
                "encoding": file_encoding,
            }
        return {
            "path": self._relative_path(resolved),
            "status": "inserted",
            "position": idx,
            "encoding": file_encoding,
        }
