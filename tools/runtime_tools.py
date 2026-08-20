"""Task-scoped intermediate files kept outside the visible project workspace."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from pathlib import Path, PureWindowsPath

from tools.file_tools import FileTools

logger = logging.getLogger(__name__)

INTERMEDIATE_SUFFIXES = {".txt", ".md", ".json", ".csv", ".log"}


def _safe_component(value: str) -> str:
    """Return a stable, filesystem-safe job/task directory component."""
    original = str(value or "unknown")
    compact = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in original
    ).strip("._") or "unknown"
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:8]
    return f"{compact[:64]}-{digest}"


class TaskRuntimeTools:
    """Read/write scratch files and atomically promote declared final artifacts."""

    def __init__(
        self,
        project_root: str | os.PathLike[str],
        state_root: str | os.PathLike[str],
        job_id: str,
        task_id: str,
        final_outputs: list[str] | None = None,
        input_paths: list[str] | None = None,
        require_declared_outputs: bool = False,
        source_job_id: str = "",
    ):
        self.project_root = Path(project_root).resolve()
        runtime_base = Path(state_root).resolve() / ".ai" / "runtime"
        runtime_base.mkdir(parents=True, exist_ok=True)
        # `.ai` can be a safe application-managed symlink when the project is
        # read-only. Resolve it before later cleanup boundary checks.
        self.runtime_base = runtime_base.resolve()
        self.job_component = _safe_component(job_id)
        self.task_component = _safe_component(task_id)
        self.root = self.runtime_base / self.job_component / self.task_component
        self.final_outputs = {
            self._normalize_relative(path)
            for path in (final_outputs or [])
            if path and "*" not in str(path)
        }
        self.input_paths = {
            self._normalize_relative(path)
            for path in (input_paths or [])
            if path and "*" not in str(path)
        }
        self.require_declared_outputs = bool(require_declared_outputs)
        self.explicit_final_outputs: set[str] = set()
        self.root.mkdir(parents=True, exist_ok=True)
        self.files = FileTools(self.root)
        self.resumed_from = ""
        if source_job_id:
            source = (
                self.runtime_base / _safe_component(source_job_id)
                / self.task_component
            )
            if source.is_dir() and source != self.root:
                shutil.copytree(source, self.root, dirs_exist_ok=True)
                self.resumed_from = str(source)

    @staticmethod
    def _normalize_relative(path: str) -> str:
        value = str(path or "").replace("\\", "/")
        while value.startswith("./"):
            value = value[2:]
        candidate = Path(value)
        if (
            not value
            or candidate.is_absolute()
            or PureWindowsPath(value).is_absolute()
            or ".." in candidate.parts
        ):
            raise PermissionError(f"Unsafe task runtime path: {path}")
        return candidate.as_posix()

    def _temp_path(self, path: str) -> Path:
        relative = self._normalize_relative(path)
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError(f"Temporary artifact escapes task runtime: {path}")
        return resolved

    def _project_path(self, path: str) -> Path:
        relative = self._normalize_relative(path)
        resolved = (self.project_root / relative).resolve()
        if not resolved.is_relative_to(self.project_root):
            raise PermissionError(f"Final artifact escapes project root: {path}")
        return resolved

    def has_temp_file(self, path: str) -> bool:
        try:
            relative = self._normalize_relative(path)
        except PermissionError:
            return False
        try:
            return self._temp_path(relative).is_file()
        except PermissionError:
            return False

    def is_protected_input(self, path: str) -> bool:
        try:
            return self._normalize_relative(path) in self.input_paths
        except PermissionError:
            return False

    def mark_explicit_final(self, path: str) -> None:
        self.explicit_final_outputs.add(self._normalize_relative(path))

    def should_route_intermediate(self, task, path: str, purpose: str = "") -> bool:
        """Identify document helper files without hiding declared final outputs."""
        try:
            relative = self._normalize_relative(path)
        except PermissionError:
            return False
        normalized_purpose = str(purpose or "").strip().lower()
        if (
            normalized_purpose == "final"
            or relative in self.final_outputs
            or relative in self.explicit_final_outputs
        ):
            return False
        if normalized_purpose in {"intermediate", "temporary", "temp", "scratch"}:
            return True
        if self.has_temp_file(relative):
            return True
        document_task = bool(getattr(task, "_rockcore_document_profile", None))
        if not document_task:
            return False
        candidate = Path(relative)
        return candidate.suffix.lower() in INTERMEDIATE_SUFFIXES

    @staticmethod
    def _looks_intermediate(relative: str) -> bool:
        candidate = Path(relative)
        return candidate.suffix.lower() in INTERMEDIATE_SUFFIXES

    async def write_temp_file(self, path: str, content: str,
                              encoding: str = "preserve", **_kwargs) -> dict:
        relative = self._normalize_relative(path)
        result = await self.files.write_file(relative, content, encoding=encoding)
        result.pop("absolute_path", None)
        result.update({
            "path": relative,
            "scope": "task_runtime",
            "temporary": True,
            "cleanup": "removed after successful task completion",
        })
        return result

    async def read_temp_file(self, path: str, start: int = 0, end: int = 0,
                             **_kwargs) -> dict:
        relative = self._normalize_relative(path)
        result = await self.files.read_file(relative, start=start, end=end)
        result["scope"] = "task_runtime"
        result["temporary"] = True
        return result

    async def list_temp_files(self, path: str = ".", **_kwargs) -> dict:
        relative = "." if path in {"", "."} else self._normalize_relative(path)
        result = await self.files.list_files(relative)
        result["scope"] = "task_runtime"
        return result

    async def promote_artifact(self, temp_path: str, target_path: str,
                               overwrite: bool = True, **_kwargs) -> dict:
        source_relative = self._normalize_relative(temp_path)
        target_relative = self._normalize_relative(target_path)
        if target_relative in self.input_paths:
            return {
                "status": "rejected",
                "error": f"Refusing to overwrite a declared input: {target_relative}",
            }
        if (
            (self.require_declared_outputs or self.final_outputs)
            and target_relative not in self.final_outputs
        ):
            return {
                "status": "rejected",
                "error": (
                    f"Final artifact is not declared by the task: {target_relative}. "
                    f"Declared outputs: {sorted(self.final_outputs)}"
                ),
            }
        source = self._temp_path(source_relative)
        if not source.is_file():
            return {"status": "error", "error": f"Temporary file not found: {temp_path}"}
        target = self._project_path(target_relative)
        if target.exists() and not overwrite:
            return {"status": "rejected", "error": f"Final artifact exists: {target_path}"}
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_target = target.with_name(
            f".{target.name}.rockcore-promote-{uuid.uuid4().hex}.tmp"
        )
        try:
            shutil.copy2(source, temporary_target)
            os.replace(temporary_target, target)
        finally:
            temporary_target.unlink(missing_ok=True)
        return {
            "status": "promoted",
            "temp_path": source_relative,
            "path": target_relative,
            "size": target.stat().st_size,
            "scope": "project_final",
        }

    async def apply_temp_patch(self, path: str, search: str, replace: str,
                               **_kwargs) -> dict:
        return await self.files.apply_patch(
            self._normalize_relative(path), search, replace
        )

    async def insert_temp_before(self, path: str, anchor: str, content: str,
                                 **_kwargs) -> dict:
        return await self.files.insert_before(
            self._normalize_relative(path), anchor, content
        )

    async def insert_temp_after(self, path: str, anchor: str, content: str,
                                **_kwargs) -> dict:
        return await self.files.insert_after(
            self._normalize_relative(path), anchor, content
        )

    def relocate_project_intermediates(self, added_paths: list[str]) -> list[dict]:
        """Move helper files only when final outputs were declared.

        An empty output declaration means the planner did not provide a
        manifest. In that case a newly-created Markdown/TXT file may be the
        user's requested deliverable, so hiding it in the task runtime would
        silently lose the result from the visible project. Explicit temporary
        files already go through ``write_temp_file`` and do not need this
        fallback relocation.
        """
        if not self.final_outputs and not self.explicit_final_outputs:
            return []
        moved = []
        for relative in added_paths:
            if (
                relative in self.final_outputs
                or relative in self.explicit_final_outputs
                or not self._looks_intermediate(relative)
            ):
                continue
            source = self._project_path(relative)
            if not source.is_file() or source.is_symlink():
                continue
            destination = (self.root / self._normalize_relative(relative)).resolve()
            if not destination.is_relative_to(self.root):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            # The project worktree and fallback application-state directory can
            # live on different volumes (especially on Windows). Copy first, then
            # unlink so a cross-device rename cannot strand the task.
            staging = destination.with_name(
                f".{destination.name}.relocate-{uuid.uuid4().hex}.tmp"
            )
            try:
                shutil.copy2(source, staging)
                os.replace(staging, destination)
                source.unlink()
            finally:
                staging.unlink(missing_ok=True)
            moved.append({"from": relative, "to": relative, "scope": "task_runtime"})
            logger.info("Moved intermediate artifact out of project root: %s", relative)
        return moved

    def checkpoint(self) -> dict:
        files = []
        for path in self.root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                files.append(path.relative_to(self.root).as_posix())
        return {
            "path": str(self.root),
            "files": sorted(files),
            "file_count": len(files),
            "resumed_from": self.resumed_from,
        }

    def cleanup(self) -> dict:
        if not self.root.exists():
            return {"status": "already_clean", "path": str(self.root)}
        resolved = self.root.resolve()
        if not resolved.is_relative_to(self.runtime_base) or resolved == self.runtime_base:
            return {"status": "error", "error": f"Unsafe runtime cleanup target: {resolved}"}
        shutil.rmtree(resolved)
        for parent in (resolved.parent, resolved.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break
        return {"status": "cleaned", "path": str(resolved)}
