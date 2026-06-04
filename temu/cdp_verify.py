"""Temu Verify all — real Chrome via CDP (Playwright-launched Chrome fails slider captcha)."""
from __future__ import annotations

import logging
import time
from typing import Callable

from playwright.sync_api import Browser, Page, sync_playwright

from shared.cdp_connect import connect_cdp_browser
from temu.browser_utils import DEFAULT_CDP_PORT, is_cdp_available, start_manual_chrome

LOG = logging.getLogger("products.scraper")

CDP_URL = f"http://127.0.0.1:{DEFAULT_CDP_PORT}"
NAV_TIMEOUT_MS = 60_000


class TemuBrowser:
    def __init__(self, browser: Browser, page: Page, *, owns_playwright) -> None:
        self._browser = browser
        self.page = page
        self._owns_playwright = owns_playwright

    def close(self) -> None:
        try:
            self._browser.close()
        except Exception as exc:
            LOG.debug("Temu CDP disconnect: %s", exc)
        if self._owns_playwright:
            try:
                self._owns_playwright.stop()
            except Exception as exc:
                LOG.debug("Playwright stop: %s", exc)


def _pick_temu_page(browser: Browser) -> Page | None:
    for context in browser.contexts:
        for page in context.pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if "temu.com" in url and "about:" not in url and "doubleclick" not in url:
                return page
    return None


def _page_needs_user_action(page) -> str | None:
    """Return 'login', 'verify', or None when product browsing should work."""
    try:
        return page.evaluate(
            """() => {
  const href = (location.href || '').toLowerCase();
  if (href.includes('/login')) return 'login';
  const body = (document.body?.innerText || '').toLowerCase();
  if (/verify you are human|security check|slide to verify|performing security|robot check|captcha/.test(body))
    return 'verify';
  if (document.getElementById('goods_price')) return null;
  const sn = window.rawData?.store?.pageSn;
  if (sn === 10032 && body.length > 800) return null;
  if (body.length < 400) return 'verify';
  return null;
}"""
        )
    except Exception:
        return "verify"


def open_temu_browser(
    *,
    log: Callable[[str], None] | None = None,
    auto_start_chrome: bool = True,
) -> TemuBrowser:
    if not is_cdp_available(CDP_URL):
        if not auto_start_chrome:
            raise RuntimeError(
                "Temu Chrome is not running. Run: python temu/setup_verify.py"
            )
        msg = "Starting Temu Chrome (real browser — use this window for login/slider)..."
        LOG.info("[temu-verify] %s", msg)
        if log:
            log(msg)
        else:
            print(f"  {msg}", flush=True)
        start_manual_chrome()
        if not is_cdp_available(CDP_URL):
            raise RuntimeError("Temu Chrome did not open on CDP port 9223.")

    playwright = sync_playwright().start()
    try:
        browser = connect_cdp_browser(playwright, CDP_URL)
    except Exception as exc:
        playwright.stop()
        err = str(exc)
        if "setDownloadBehavior" in err or "context management" in err:
            raise RuntimeError(
                "Temu Chrome CDP attach failed. Try: cd products && .venv/bin/pip install 'playwright>=1.60.0'"
            ) from exc
        raise

    page = _pick_temu_page(browser)
    if page is None:
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto("https://www.temu.com/za/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    return TemuBrowser(browser, page, owns_playwright=playwright)


def wait_for_temu_user_ready(
    page: Page,
    *,
    log: Callable[[str], None] | None = None,
    wait_seconds: int = 600,
) -> None:
    """Wait until login / slider verification is done in real Chrome. Never navigates during."""
    deadline = time.monotonic() + wait_seconds
    announced: set[str] = set()
    while time.monotonic() < deadline:
        state = _page_needs_user_action(page)
        if state is None:
            return
        if state not in announced:
            if state == "login":
                msg = (
                    "Temu login — sign in in the Temu Chrome window (port 9223). "
                    "Verify will continue when done."
                )
            else:
                msg = (
                    "Temu security slider — complete it in the Temu Chrome window (real browser). "
                    "Do not use the Playwright window. Verify continues when the site loads."
                )
            LOG.info("[temu-verify] %s", msg)
            if log:
                log(msg)
            else:
                print(f"  {msg}", flush=True)
            announced.add(state)
        time.sleep(2)

    raise RuntimeError(
        "Temu login/verification not completed. Run: python temu/setup_verify.py "
        "and pass the slider in the Chrome window that opens."
    )
