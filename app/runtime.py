"""Desktop runtime setup shared by source and packaged launches."""

import logging
import sys

from .branding import FULL_PRODUCT_NAME


def configure_runtime_logging():
    """Keep a useful file log in the user data directory on packaged builds."""
    from .paths import app_data_dir

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(handler, logging.FileHandler) for handler in root.handlers):
        root.addHandler(logging.FileHandler(
            app_data_dir() / "rockcore.log", encoding="utf-8"
        ))


def configure_windows_identity():
    """Give Windows taskbar grouping a stable RockCore identity."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "RockInnovation.RockCore"
        )
    except (AttributeError, OSError):
        logging.getLogger(__name__).debug("Windows AppUserModelID unavailable")
