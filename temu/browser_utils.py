"""Real Chrome helpers for Temu verify (CDP port 9223 — separate from Junk Mail 9222)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
CHROME_PROFILE = PRODUCTS_ROOT / "temu" / "chrome_profile"
DEFAULT_CDP_PORT = 9223


def _chrome_executable() -> str:
    if sys.platform == "darwin":
        path = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if path.exists():
            return str(path)
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError("Google Chrome not found.")


def is_cdp_available(url: str = f"http://127.0.0.1:{DEFAULT_CDP_PORT}") -> bool:
    try:
        urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=2)
        return True
    except Exception:
        return False


def start_manual_chrome(
    *,
    url: str = "https://www.temu.com/za/",
    port: int = DEFAULT_CDP_PORT,
    wait_seconds: int = 45,
) -> str:
    """Start real Chrome with remote debugging (not Playwright — slider works)."""
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)
    target = f"http://127.0.0.1:{port}"
    if is_cdp_available(target):
        return target

    chrome = os.environ.get("CHROME_PATH") or _chrome_executable()
    subprocess.Popen(
        [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={CHROME_PROFILE.resolve()}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_cdp_available(target):
            return target
        time.sleep(1)

    raise RuntimeError(f"Temu Chrome did not open CDP port {port} within {wait_seconds}s.")
