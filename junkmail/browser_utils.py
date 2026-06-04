"""
Junk Mail browser helpers for the products crawler and scraper.

Crawler uses a saved cookie session file when available so it does not fight
with an already-open Chrome window on the same profile.

The scenario crawler (`junkmail_crawler/`) uses real Chrome via CDP instead —
see `junkmail/cdp_fetch.py` and `junkmail/setup_cloudflare.py`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable

from junkmail_crawler.parsers import is_cloudflare_challenge

LOG = logging.getLogger("junkmail.browser")

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
CHROME_PROFILE = PRODUCTS_ROOT / "junkmail" / "chrome_profile"
SESSION_FILE = PRODUCTS_ROOT / "junkmail" / "junkmail_session.json"
DEFAULT_CDP_PORT = 9222

JUNKMAIL_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
JUNKMAIL_IGNORE_DEFAULT_ARGS = ["--enable-automation"]

PROFILE_IN_USE_MSG = (
    "Junk Mail Chrome profile is in use. For setup, keep that window open and run: "
    "python junkmail/setup_cloudflare.py"
)


def profile_in_use() -> bool:
    if (CHROME_PROFILE / "SingletonLock").exists():
        return True
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["pgrep", "-f", str(CHROME_PROFILE.resolve())],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        except Exception:
            pass
    return False


def save_session_state(context) -> None:
    """Persist cookies/storage after Cloudflare clearance."""
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(SESSION_FILE))
    LOG.info("Saved Junk Mail session to %s", SESSION_FILE)


def _headed_default() -> bool:
    return os.environ.get("JUNKMAIL_CRAWLER_HEADED", "").lower() in ("1", "true", "yes")


def _launch_with_session(playwright, *, headless: bool):
    browser = playwright.chromium.launch(
        channel="chrome",
        headless=headless,
        args=JUNKMAIL_LAUNCH_ARGS,
        ignore_default_args=JUNKMAIL_IGNORE_DEFAULT_ARGS,
    )
    context = browser.new_context(
        storage_state=str(SESSION_FILE),
        locale="en-ZA",
        viewport={"width": 1280, "height": 900} if headless else None,
    )
    context._browser_ref = browser  # type: ignore[attr-defined]
    return context


def _launch_persistent(playwright, *, headless: bool):
    return playwright.chromium.launch_persistent_context(
        str(CHROME_PROFILE),
        channel="chrome",
        headless=headless,
        args=JUNKMAIL_LAUNCH_ARGS,
        ignore_default_args=JUNKMAIL_IGNORE_DEFAULT_ARGS,
        locale="en-ZA",
        viewport=None,
    )


def launch_junkmail_context(playwright, *, headless: bool | None = None, prefer_session: bool = True):
    """
    Open Chrome for Junk Mail crawl/scrape.

    If junkmail_session.json exists (from setup_cloudflare.py), uses those cookies
    in a fresh Chrome — no profile lock conflict with an open browser window.
    """
    CHROME_PROFILE.mkdir(parents=True, exist_ok=True)

    if headless is None:
        headless = not _headed_default()

    if prefer_session and SESSION_FILE.exists():
        LOG.info("Launching Junk Mail Chrome with saved session cookies")
        return _launch_with_session(playwright, headless=headless)

    if profile_in_use():
        raise RuntimeError(PROFILE_IN_USE_MSG)

    try:
        LOG.info("Launching Junk Mail Chrome with persistent profile")
        return _launch_persistent(playwright, headless=headless)
    except Exception as exc:
        err = str(exc).lower()
        if (
            "existing browser session" in err
            or "processsingleton" in err
            or "singletonlock" in err
            or "profile directory" in err
        ):
            if SESSION_FILE.exists():
                LOG.warning("Profile busy; falling back to saved session")
                return _launch_with_session(playwright, headless=headless)
            raise RuntimeError(PROFILE_IN_USE_MSG) from exc
        raise


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


def is_cdp_available(url: str = "http://127.0.0.1:9222") -> bool:
    try:
        urllib.request.urlopen(f"{url.rstrip('/')}/json/version", timeout=2)
        return True
    except Exception:
        return False


def start_manual_chrome(
    *,
    url: str = "https://www.junkmail.co.za/",
    port: int = DEFAULT_CDP_PORT,
    wait_seconds: int = 30,
) -> str:
    """Start real Chrome with remote debugging (setup_cloudflare.py only)."""
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

    raise RuntimeError(f"Chrome did not open CDP port {port} within {wait_seconds}s.")


def pick_page(context, *, prefer_junkmail: bool = True):
    pages = list(context.pages)
    if prefer_junkmail:
        for page in pages:
            try:
                if page.url and "junkmail.co.za" in page.url and "about:blank" not in page.url:
                    return page
            except Exception:
                continue
    if pages:
        return pages[0]
    return context.new_page()


def wait_for_cloudflare_clearance(
    page,
    *,
    timeout_seconds: int = 600,
    log: Callable[[str], None] | None = None,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    announced = False
    while time.monotonic() < deadline:
        try:
            html = page.content()
            title = page.title()
        except Exception as exc:
            LOG.debug("CF wait read failed: %s", exc)
            time.sleep(2)
            continue
        if not is_cloudflare_challenge(html, title):
            if log:
                log("Cloudflare cleared.")
            return True
        if log and not announced:
            log(
                "Cloudflare — complete verification in the Junk Mail Chrome window "
                "(real Chrome, not an automated browser). "
                "Or run: python junkmail/setup_cloudflare.py"
            )
            announced = True
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        time.sleep(2)
    return False


def goto_junkmail(page, url: str, *, timeout_ms: int = 60000, wait_cf: bool = True) -> bool:
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    time.sleep(1)
    if wait_cf and is_cloudflare_challenge(page.content(), page.title()):
        return wait_for_cloudflare_clearance(page)
    return True


def close_context(context) -> None:
    browser = getattr(context, "_browser_ref", None)
    try:
        context.close()
    except Exception as exc:
        LOG.debug("Context close: %s", exc)
    if browser is not None:
        try:
            browser.close()
        except Exception as exc:
            LOG.debug("Browser close: %s", exc)
