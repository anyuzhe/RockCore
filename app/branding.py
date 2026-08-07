"""RockCore product branding and packaged resource lookup."""

import sys
from pathlib import Path


PRODUCT_NAME = "RockCore"
COMPANY_NAME = "岩创科技"
LEGAL_COMPANY_NAME = "浙江岩创科技有限公司"
PRODUCT_LINE = "多 AI 智能工程工作台"
FULL_PRODUCT_NAME = f"{PRODUCT_NAME} · {COMPANY_NAME}"


def resource_root() -> Path:
    """Return the read-only resource directory in source and frozen builds."""
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root)
    return Path(__file__).resolve().parent.parent


def logo_path() -> Path:
    """Return the preferred logo asset, with SVG as the source fallback."""
    branding_dir = resource_root() / "assets" / "branding"
    for filename in ("rockinnov_logo.png", "rockinnov_logo.svg"):
        path = branding_dir / filename
        if path.exists():
            return path
    return branding_dir / "rockinnov_logo.svg"


def icon_path() -> Path:
    return resource_root() / "assets" / "branding" / "rockcore.ico"
