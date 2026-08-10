"""Safe, persistent image attachments for desktop job requests."""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import uuid
from pathlib import Path
from typing import Iterable

from app.paths import app_data_dir


MAX_IMAGE_ATTACHMENTS = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024
SUPPORTED_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
}


def attachment_store_dir() -> Path:
    """Return the private per-user attachment directory."""
    directory = app_data_dir() / "attachments"
    directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _validate_source(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise ValueError(f"图片文件不存在：{path}")
    if source.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
        raise ValueError(f"不支持的图片格式：{source.suffix or '未知'}")
    size = source.stat().st_size
    if size <= 0:
        raise ValueError("图片文件为空")
    if size > MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 20 MB")
    return source


def _record(path: Path, *, name: str, source: str) -> dict:
    data = path.read_bytes()
    mime_type = mimetypes.guess_type(name)[0] or "image/png"
    return {
        "id": uuid.uuid4().hex,
        "name": name[:255],
        "path": str(path.resolve()),
        "mime_type": mime_type,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "source": source,
    }


def store_image_file(source_path: str | Path) -> dict:
    """Copy a user-selected image into RockCore-owned persistent storage."""
    source = _validate_source(Path(source_path))
    destination = attachment_store_dir() / f"{uuid.uuid4().hex}{source.suffix.lower()}"
    shutil.copy2(source, destination)
    return _record(destination, name=source.name, source="file")


def store_image_bytes(data: bytes, *, name: str = "剪贴板图片.png") -> dict:
    """Persist an encoded clipboard image and return its job-safe metadata."""
    if not data:
        raise ValueError("剪贴板图片为空")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("单张图片不能超过 20 MB")
    destination = attachment_store_dir() / f"{uuid.uuid4().hex}.png"
    destination.write_bytes(data)
    return _record(destination, name=name, source="clipboard")


def normalize_attachments(records: Iterable[dict] | None) -> list[dict]:
    """Validate persisted records before a job or model may use their paths."""
    normalized: list[dict] = []
    store = attachment_store_dir()
    raw_records = list(records or [])
    for raw in raw_records[:MAX_IMAGE_ATTACHMENTS]:
        if not isinstance(raw, dict):
            raise ValueError("图片附件记录无效")
        path = _validate_source(Path(str(raw.get("path", ""))))
        if not _is_within(path, store):
            raise ValueError("图片附件必须位于 RockCore 用户数据目录")
        actual = _record(
            path,
            name=str(raw.get("name") or path.name),
            source=str(raw.get("source") or "file"),
        )
        actual["id"] = str(raw.get("id") or actual["id"])
        normalized.append(actual)
    if len(raw_records) > MAX_IMAGE_ATTACHMENTS:
        raise ValueError(f"一次最多附加 {MAX_IMAGE_ATTACHMENTS} 张图片")
    return normalized


def attachment_context(records: Iterable[dict] | None) -> str:
    """Render a concise prompt manifest without embedding binary image data."""
    items = list(records or [])
    if not items:
        return ""
    lines = ["\n=== IMAGE ATTACHMENTS ==="]
    for index, item in enumerate(items, start=1):
        lines.append(f"{index}. {item.get('name', '图片')}")
    lines.append(
        "The images are transmitted separately to image-capable models. They are "
        "request context, not project files; do not add their storage paths to a plan."
    )
    return "\n".join(lines)
