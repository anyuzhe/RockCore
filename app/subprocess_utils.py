"""Cross-platform subprocess helpers for GUI and packaged runtimes."""

import locale
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any


def utf8_environment(environ: dict | None = None) -> dict:
    """Return a child environment with deterministic Unicode behavior."""
    result = dict(os.environ if environ is None else environ)
    result.setdefault("PYTHONUTF8", "1")
    result.setdefault("PYTHONIOENCODING", "utf-8")
    result.setdefault("NO_COLOR", "1")
    return result


def decode_process_output(value: bytes | str | None) -> str:
    """Decode UTF-8 first, then the Windows/local code page without crashing."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value

    encodings = ["utf-8-sig", locale.getpreferredencoding(False)]
    if sys.platform == "win32":
        encodings.extend(["mbcs", "gb18030"])
    else:
        # Useful for logs copied from Chinese Windows into a non-Windows test.
        encodings.append("gb18030")
    seen: set[str] = set()
    for encoding in encodings:
        normalized = str(encoding or "").lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return value.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return value.decode("utf-8", errors="replace")


def run_process(command: Any, **kwargs) -> subprocess.CompletedProcess:
    """Run a process and return decoded text using tolerant Unicode handling."""
    kwargs.pop("text", None)
    kwargs.pop("universal_newlines", None)
    kwargs.pop("encoding", None)
    kwargs.pop("errors", None)
    if isinstance(kwargs.get("input"), str):
        kwargs["input"] = kwargs["input"].encode("utf-8")
    kwargs["env"] = utf8_environment(kwargs.get("env"))
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", no_window_creation_flags())
    completed = subprocess.run(command, **kwargs)
    if isinstance(completed.stdout, bytes):
        completed.stdout = decode_process_output(completed.stdout)
    if isinstance(completed.stderr, bytes):
        completed.stderr = decode_process_output(completed.stderr)
    return completed


def no_window_creation_flags() -> int:
    """Avoid flashing a console window for child processes on Windows."""
    if sys.platform != "win32":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


def command_basename(command: str, platform: str | None = None) -> str:
    """Extract a normalized executable name from POSIX or Windows commands."""
    platform_name = sys.platform if platform is None else platform
    looks_windows = bool(re.match(r'^\s*["\']?[A-Za-z]:\\', command))
    try:
        tokens = shlex.split(
            command, posix=platform_name != "win32" and not looks_windows
        )
    except ValueError:
        tokens = command.strip().split()
    if not tokens:
        return ""
    token = tokens[0].strip('"\'')
    name = (
        PureWindowsPath(token).name
        if "\\" in token or platform_name == "win32" or looks_windows
        else Path(token).name
    ).lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name


def quote_command_arg(value: str, platform: str | None = None) -> str:
    """Quote one shell argument using cmd.exe or POSIX rules as appropriate."""
    platform_name = sys.platform if platform is None else platform
    if platform_name == "win32":
        return subprocess.list2cmdline([str(value)])
    return shlex.quote(str(value))
