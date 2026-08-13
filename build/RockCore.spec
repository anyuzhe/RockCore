# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the RockCore Windows desktop application."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


ROOT = Path(SPECPATH).resolve().parent
APP = ROOT / "app" / "main.py"
ASSETS = ROOT / "assets"
BUILTIN_SKILLS = ROOT / "skills" / "builtin"
VERSION_FILE = ROOT / "build" / "version_info.generated.txt"
MINGIT = ROOT / "build" / "vendor" / "mingit"

if not (MINGIT / "cmd" / "git.exe").is_file():
    raise SystemExit(
        "Bundled MinGit is missing. Run scripts/build_windows.ps1 so the "
        "verified runtime is downloaded before PyInstaller."
    )

hiddenimports = ["qasync", "PyQt6.QtSvg", "PyQt6.QtSvgWidgets"]
hiddenimports += collect_submodules("sqlalchemy")
hiddenimports += collect_submodules("pypdf")
hiddenimports += collect_submodules("docx")
hiddenimports += collect_submodules("pptx")
hiddenimports += collect_submodules("reportlab")
hiddenimports += collect_submodules("unittest")
# Project acceptance must not depend on a separately installed host Python.
# Bundle pytest and its internal modules with the RockCore runtime instead.
hiddenimports += collect_submodules("pytest")
hiddenimports += collect_submodules("_pytest")

a = Analysis(
    [str(APP)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ASSETS), "assets"),
        (str(ROOT / "VERSION"), "."),
        (str(BUILTIN_SKILLS), "skills/builtin"),
        (str(MINGIT), "runtime/git"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe_kwargs = {
    "name": "RockCore",
    "console": False,
    "exclude_binaries": True,
    "disable_windowed_traceback": False,
    "icon": str(ASSETS / "branding" / "rockcore.ico"),
}
if VERSION_FILE.exists():
    exe_kwargs["version"] = str(VERSION_FILE)
exe = EXE(pyz, a.scripts, [], **exe_kwargs)
COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="RockCore")
