"""Deterministic regression coverage for verified desktop updates."""

import hashlib
import json
import urllib.error

import pytest

import app.updater as updater
from app.updater import (
    ReleaseAsset,
    NoStableRelease,
    UpdateError,
    UpdateInfo,
    UpdateManager,
    normalize_version,
    parse_checksum_manifest,
    version_key,
)


class _Response:
    def __init__(self, data: bytes, url: str):
        self.data = data
        self.url = url
        self.headers = {"Content-Length": str(len(data))}
        self.position = 0

    def read(self, size=-1):
        if size < 0:
            result = self.data[self.position:]
            self.position = len(self.data)
            return result
        result = self.data[self.position:self.position + size]
        self.position += len(result)
        return result

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _release(version="1.2.0", *, prerelease=False):
    installer = f"RockCore-Setup-{version}-x64.exe"
    return {
        "tag_name": f"v{version}",
        "name": f"RockCore {version}",
        "body": "更新说明",
        "html_url": f"https://github.com/anyuzhe/RockCore/releases/tag/v{version}",
        "published_at": "2026-08-13T01:00:00Z",
        "draft": False,
        "prerelease": prerelease,
        "assets": [
            {
                "name": installer,
                "browser_download_url": (
                    f"https://github.com/anyuzhe/RockCore/releases/download/"
                    f"v{version}/{installer}"
                ),
                "size": 12,
            },
            {
                "name": "SHA256SUMS.txt",
                "browser_download_url": (
                    f"https://github.com/anyuzhe/RockCore/releases/download/"
                    f"v{version}/SHA256SUMS.txt"
                ),
                "size": 100,
            },
        ],
    }


def test_version_normalization_and_comparison_are_numeric():
    assert normalize_version("v1.10.0") == "1.10.0"
    assert version_key("1.10.0") > version_key("1.9.9")
    assert version_key("1.0") == (1, 0, 0, 0)


def test_check_selects_exact_installer_and_checksum_assets(monkeypatch):
    payload = json.dumps(_release()).encode()
    monkeypatch.setattr(updater, "current_version", lambda: "1.0.1")
    monkeypatch.setattr(
        updater.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _Response(payload, updater.LATEST_RELEASE_API),
    )

    result = UpdateManager().check()

    assert result is not None
    assert result.version == "1.2.0"
    assert result.installer.name == "RockCore-Setup-1.2.0-x64.exe"
    assert result.checksums.name == "SHA256SUMS.txt"


def test_check_ignores_current_and_rejects_prerelease(monkeypatch):
    monkeypatch.setattr(updater, "current_version", lambda: "1.2.0")

    def respond(payload):
        monkeypatch.setattr(
            updater.urllib.request,
            "urlopen",
            lambda *_args, **_kwargs: _Response(
                json.dumps(payload).encode(), updater.LATEST_RELEASE_API
            ),
        )

    respond(_release())
    assert UpdateManager().check() is None
    respond(_release("1.3.0", prerelease=True))
    with pytest.raises(UpdateError, match="稳定版本"):
        UpdateManager().check()


def test_latest_404_without_releases_is_not_reported_as_network_failure(
    monkeypatch,
):
    calls = []

    def respond(request, **_kwargs):
        calls.append(request.full_url)
        if request.full_url == updater.LATEST_RELEASE_API:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, None
            )
        return _Response(b"[]", updater.RELEASES_API)

    monkeypatch.setattr(updater.urllib.request, "urlopen", respond)

    with pytest.raises(NoStableRelease, match="尚未发布") as failure:
        UpdateManager().check()

    assert "无法连接" not in str(failure.value)
    assert calls == [updater.LATEST_RELEASE_API, updater.RELEASES_API]


def test_latest_404_can_recover_from_stable_release_list(monkeypatch):
    payload = json.dumps([_release("1.3.0")]).encode()
    monkeypatch.setattr(updater, "current_version", lambda: "1.2.0")

    def respond(request, **_kwargs):
        if request.full_url == updater.LATEST_RELEASE_API:
            raise urllib.error.HTTPError(
                request.full_url, 404, "Not Found", {}, None
            )
        return _Response(payload, updater.RELEASES_API)

    monkeypatch.setattr(updater.urllib.request, "urlopen", respond)

    result = UpdateManager().check()

    assert result is not None
    assert result.version == "1.3.0"


def test_checksum_manifest_requires_one_exact_filename():
    digest = "a" * 64
    assert parse_checksum_manifest(
        f"{digest}  RockCore-Setup-1.2.0-x64.exe\n",
        "RockCore-Setup-1.2.0-x64.exe",
    ) == digest
    with pytest.raises(UpdateError, match="唯一"):
        parse_checksum_manifest(
            f"{digest}  RockCore-Setup-1.2.0-x64.exe\n"
            f"{digest}  RockCore-Setup-1.2.0-x64.exe\n",
            "RockCore-Setup-1.2.0-x64.exe",
        )


def test_download_installer_requires_matching_sha256(tmp_path, monkeypatch):
    installer_bytes = b"verified installer"
    digest = hashlib.sha256(installer_bytes).hexdigest()
    name = "RockCore-Setup-1.2.0-x64.exe"
    info = UpdateInfo(
        version="1.2.0", current_version="1.0.1",
        release_name="RockCore 1.2.0", notes="", release_url="",
        published_at="",
        installer=ReleaseAsset(name, "https://github.com/installer", len(installer_bytes)),
        checksums=ReleaseAsset(
            "SHA256SUMS.txt", "https://github.com/checksums", 100
        ),
    )
    manager = UpdateManager()
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        manager,
        "_download_bytes",
        lambda _asset, _limit: f"{digest}  {name}\n".encode(),
    )
    monkeypatch.setattr(
        manager,
        "_download_file",
        lambda _asset, target, _limit: (
            target.write_bytes(installer_bytes), digest
        ),
    )

    path = manager.download_and_verify(info)

    assert path.read_bytes() == installer_bytes
    assert manager.verify_installer(path, info)


def test_download_removes_a_checksum_mismatch(tmp_path, monkeypatch):
    name = "RockCore-Setup-1.2.0-x64.exe"
    info = UpdateInfo(
        version="1.2.0", current_version="1.0.1",
        release_name="", notes="", release_url="", published_at="",
        installer=ReleaseAsset(name, "https://github.com/installer", 3),
        checksums=ReleaseAsset("SHA256SUMS.txt", "https://github.com/sums", 80),
    )
    manager = UpdateManager()
    monkeypatch.setattr(updater, "app_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        manager,
        "_download_bytes",
        lambda _asset, _limit: ("0" * 64 + f"  {name}\n").encode(),
    )
    monkeypatch.setattr(
        manager,
        "_download_file",
        lambda _asset, target, _limit: (
            target.write_bytes(b"bad"), hashlib.sha256(b"bad").hexdigest()
        ),
    )

    with pytest.raises(UpdateError, match="校验失败"):
        manager.download_and_verify(info)

    update_dir = tmp_path / "updates" / "1.2.0"
    assert not (update_dir / name).exists()
    assert not (update_dir / f"{name}.download").exists()


def test_untrusted_download_host_is_rejected():
    with pytest.raises(UpdateError, match="不受信任"):
        updater._safe_https_url("https://example.com/RockCore-Setup.exe")
