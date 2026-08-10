"""Unicode-safe text file helpers with legacy Windows code-page support."""

import locale
from pathlib import Path


def read_text_compatible(path: str | Path) -> tuple[str, str]:
    """Read text without corrupting UTF BOMs or common Chinese Windows files."""
    data = Path(path).read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"

    preferred = locale.getpreferredencoding(False)
    preferred_lower = str(preferred or "").lower()
    chinese_code_pages = {"gbk", "gb18030", "cp936", "936"}
    encodings = ["utf-8"]
    if preferred_lower in chinese_code_pages:
        encodings.append(preferred)
    encodings.extend(["gb18030", preferred, "cp1252"])
    seen: set[str] = set()
    for encoding in encodings:
        normalized = str(encoding or "").lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return data.decode(encoding), normalized
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8"


def encode_text_compatible(content: str, encoding: str = "utf-8") -> bytes:
    """Encode fully before a write so encoding errors cannot truncate a file."""
    normalized = str(encoding or "utf-8").lower()
    if normalized not in {
        "utf-8", "utf-8-sig", "utf-16", "gb18030", "gbk", "cp936",
        "cp1252",
    }:
        raise ValueError(f"Unsupported text encoding: {encoding}")
    return content.encode(normalized)


def write_text_compatible(path: str | Path, content: str,
                          encoding: str = "utf-8") -> None:
    Path(path).write_bytes(encode_text_compatible(content, encoding))
