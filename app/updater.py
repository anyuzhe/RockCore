"""Verified GitHub Release updates for the packaged Windows application."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from app.branding import resource_root
from app.paths import app_data_dir, application_dir


GITHUB_OWNER = "anyuzhe"
GITHUB_REPOSITORY = "RockCore"
LATEST_RELEASE_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
    "/releases/latest"
)
RELEASES_API = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPOSITORY}"
    "/releases?per_page=20"
)
MAX_RELEASE_METADATA_BYTES = 2 * 1024 * 1024
MAX_CHECKSUM_BYTES = 1024 * 1024
MAX_INSTALLER_BYTES = 1024 * 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class UpdateError(RuntimeError):
    """A safe, user-displayable update failure."""


class NoStableRelease(UpdateError):
    """The update service is reachable but has no installable release."""


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int = 0


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    current_version: str
    release_name: str
    notes: str
    release_url: str
    published_at: str
    installer: ReleaseAsset
    checksums: ReleaseAsset


def normalize_version(value: str) -> str:
    """Return a numeric dotted version suitable for display and comparison."""
    text = str(value or "").strip()
    if text.lower().startswith("v"):
        text = text[1:]
    match = re.match(r"^(\d+(?:\.\d+){0,3})", text)
    return match.group(1) if match else "0.0.0"


def version_key(value: str) -> tuple[int, int, int, int]:
    parts = [int(part) for part in normalize_version(value).split(".")]
    return tuple((parts + [0, 0, 0, 0])[:4])


def current_version() -> str:
    """Read the one build version embedded by PyInstaller or the source tree."""
    candidates = [
        resource_root() / "VERSION",
        application_dir() / "VERSION",
        Path(__file__).resolve().parent.parent / "VERSION",
    ]
    for candidate in candidates:
        try:
            value = normalize_version(candidate.read_text(encoding="utf-8"))
            if value != "0.0.0":
                return value
        except OSError:
            continue
    return "0.0.0"


def _safe_https_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in ALLOWED_DOWNLOAD_HOSTS:
        raise UpdateError("更新资源地址不受信任，已停止下载。")
    return parsed.geturl()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(
        _safe_https_url(url),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"RockCore-Updater/{current_version()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _read_response(response, limit: int) -> bytes:
    declared = int(response.headers.get("Content-Length") or 0)
    if declared > limit:
        raise UpdateError("更新服务器返回的数据超过安全大小限制。")
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("更新服务器返回的数据超过安全大小限制。")
    return data


def _asset_from_release(release: dict, name: str) -> ReleaseAsset | None:
    for item in release.get("assets") or []:
        if str(item.get("name") or "").lower() != name.lower():
            continue
        return ReleaseAsset(
            name=name,
            url=_safe_https_url(str(item.get("browser_download_url") or "")),
            size=max(0, int(item.get("size") or 0)),
        )
    return None


def parse_checksum_manifest(content: str, filename: str) -> str:
    """Extract exactly one SHA-256 entry for the selected installer."""
    matches = []
    for line in str(content or "").splitlines():
        match = re.fullmatch(r"\s*([0-9a-fA-F]{64})\s+[*]?(.+?)\s*", line)
        if match and Path(match.group(2)).name.lower() == filename.lower():
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        raise UpdateError("SHA256SUMS.txt 中没有唯一的安装包校验记录。")
    return matches[0]


def _decode_checksum_manifest(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise UpdateError("SHA256SUMS.txt 不是有效的 UTF-8 文件。") from error


class UpdateManager:
    """Check, download and verify stable RockCore Windows releases."""

    def __init__(self, *, api_url: str = LATEST_RELEASE_API,
                 timeout: float = 20.0):
        self.api_url = api_url
        self.timeout = max(2.0, float(timeout))

    @property
    def can_install(self) -> bool:
        return sys.platform == "win32" and bool(getattr(sys, "frozen", False))

    def _fetch_json(self, url: str):
        """Fetch one trusted JSON document while preserving HTTP status."""
        try:
            with urllib.request.urlopen(
                _request(url), timeout=self.timeout
            ) as response:
                _safe_https_url(response.geturl())
                return json.loads(
                    _read_response(response, MAX_RELEASE_METADATA_BYTES).decode(
                        "utf-8-sig"
                    )
                )
        except urllib.error.HTTPError:
            # HTTPError is also a URLError. Preserve it so a valid 404 response
            # is not incorrectly reported as a network connection failure.
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise UpdateError(f"无法连接更新服务器：{error}") from error
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise UpdateError("更新服务器返回了无法识别的版本信息。") from error

    @staticmethod
    def _raise_http_error(error: urllib.error.HTTPError):
        code = int(getattr(error, "code", 0) or 0)
        if code in {401, 403}:
            raise UpdateError(
                f"更新服务器拒绝访问（HTTP {code}）。请稍后重试；若持续出现，"
                "请检查 GitHub 匿名访问频率限制。"
            ) from error
        if code == 429:
            raise UpdateError("更新检查过于频繁（HTTP 429），请稍后再试。") from error
        if code == 404:
            raise UpdateError(
                "更新地址不存在或仓库无法公开访问（HTTP 404）。"
            ) from error
        raise UpdateError(
            f"更新服务器返回 HTTP {code or '错误'}：{error.reason or '未知原因'}"
        ) from error

    def _release_after_latest_404(self):
        """Distinguish an empty release feed from a missing repository."""
        try:
            releases = self._fetch_json(RELEASES_API)
        except urllib.error.HTTPError as error:
            self._raise_http_error(error)
        if not isinstance(releases, list):
            raise UpdateError("更新服务器返回了无法识别的版本列表。")
        for release in releases:
            if (
                isinstance(release, dict)
                and not release.get("draft")
                and not release.get("prerelease")
            ):
                return release
        raise NoStableRelease(
            "更新服务连接正常，但当前尚未发布可安装的稳定版本。"
            "Git 标签或 Actions 构建产物不会自动成为更新；请在 GitHub "
            "Releases 中发布带安装包和 SHA256SUMS.txt 的正式版本。"
        )

    def check(self) -> UpdateInfo | None:
        """Return a newer stable release, or ``None`` when already current."""
        try:
            payload = self._fetch_json(self.api_url)
        except urllib.error.HTTPError as error:
            if int(getattr(error, "code", 0) or 0) == 404 and (
                self.api_url == LATEST_RELEASE_API
            ):
                payload = self._release_after_latest_404()
            else:
                self._raise_http_error(error)

        if (
            not isinstance(payload, dict)
            or payload.get("draft")
            or payload.get("prerelease")
        ):
            raise UpdateError("更新服务器没有返回有效的稳定版本。")
        latest = normalize_version(payload.get("tag_name") or payload.get("name"))
        installed = current_version()
        if version_key(latest) <= version_key(installed):
            return None
        installer_name = f"RockCore-Setup-{latest}-x64.exe"
        installer = _asset_from_release(payload, installer_name)
        checksums = _asset_from_release(payload, "SHA256SUMS.txt")
        if installer is None or checksums is None:
            raise UpdateError(
                f"版本 {latest} 缺少 Windows 安装包或 SHA256SUMS.txt。"
            )
        if installer.size and installer.size > MAX_INSTALLER_BYTES:
            raise UpdateError("安装包超过安全大小限制。")
        return UpdateInfo(
            version=latest,
            current_version=installed,
            release_name=str(payload.get("name") or f"RockCore {latest}"),
            notes=str(payload.get("body") or "本版本未提供更新说明。")[:12000],
            release_url=_safe_https_url(str(payload.get("html_url") or "")),
            published_at=str(payload.get("published_at") or ""),
            installer=installer,
            checksums=checksums,
        )

    def _download_bytes(self, asset: ReleaseAsset, limit: int) -> bytes:
        try:
            with urllib.request.urlopen(
                _request(asset.url), timeout=max(self.timeout, 60.0)
            ) as response:
                _safe_https_url(response.geturl())
                return _read_response(response, limit)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise UpdateError(f"下载 {asset.name} 失败：{error}") from error

    def _download_file(self, asset: ReleaseAsset, target: Path,
                       limit: int) -> tuple[int, str]:
        """Stream an asset to disk while enforcing size and hashing it."""
        digest = hashlib.sha256()
        downloaded = 0
        try:
            with urllib.request.urlopen(
                _request(asset.url), timeout=max(self.timeout, 60.0)
            ) as response:
                _safe_https_url(response.geturl())
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > limit:
                    raise UpdateError("安装包超过安全大小限制。")
                with target.open("wb") as stream:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > limit:
                            raise UpdateError("安装包超过安全大小限制。")
                        stream.write(chunk)
                        digest.update(chunk)
        except UpdateError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise UpdateError(f"下载 {asset.name} 失败：{error}") from error
        return downloaded, digest.hexdigest().lower()

    def download_and_verify(self, update: UpdateInfo) -> Path:
        """Download to per-user storage and require a matching SHA-256 hash."""
        manifest = _decode_checksum_manifest(
            self._download_bytes(update.checksums, MAX_CHECKSUM_BYTES)
        )
        expected = parse_checksum_manifest(manifest, update.installer.name)
        update_root = (app_data_dir() / "updates" / update.version).resolve()
        target = (update_root / update.installer.name).resolve()
        if target.parent != update_root:
            raise UpdateError("安装包文件名不安全，已停止更新。")

        temporary = target.with_suffix(target.suffix + ".download")
        try:
            update_root.mkdir(parents=True, exist_ok=True)
            if target.is_file() and self._sha256(target) == expected:
                return target
            downloaded, actual = self._download_file(
                update.installer, temporary, MAX_INSTALLER_BYTES
            )
            if update.installer.size and downloaded != update.installer.size:
                raise UpdateError("安装包下载不完整，已停止更新。")
            if actual != expected:
                raise UpdateError("安装包 SHA-256 校验失败，文件已删除。")
            os.replace(temporary, target)
        except UpdateError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise UpdateError(f"无法保存更新安装包：{error}") from error
        return target

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest().lower()

    def verify_installer(self, path: Path, update: UpdateInfo) -> bool:
        """Revalidate a staged installer immediately before execution."""
        resolved = Path(path).resolve()
        expected_parent = (app_data_dir() / "updates" / update.version).resolve()
        if resolved.parent != expected_parent or resolved.name != update.installer.name:
            return False
        manifest = _decode_checksum_manifest(
            self._download_bytes(update.checksums, MAX_CHECKSUM_BYTES)
        )
        expected = parse_checksum_manifest(manifest, resolved.name)
        try:
            return resolved.is_file() and self._sha256(resolved) == expected
        except OSError:
            return False
