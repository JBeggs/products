"""
Fetch Junk Mail HTML through real Chrome via CDP.

Exported cookies do not bypass Cloudflare TLS checks. Reuse the same Chrome
session (remote debugging port 9222) that already passed verification.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from playwright.sync_api import Browser, Page, sync_playwright

from junkmail.browser_utils import (
    DEFAULT_CDP_PORT,
    is_cdp_available,
    start_manual_chrome,
)
from junkmail_crawler.parsers import (
    is_cloudflare_challenge,
    is_jobmail_listing_url,
    is_junkmail_jobs_search_url,
    normalize_junkmail_listing_url,
)
from shared.cdp_connect import connect_cdp_browser

LOG = logging.getLogger("junkmail.cdp_fetch")

CDP_URL = f"http://127.0.0.1:{DEFAULT_CDP_PORT}"
NAV_TIMEOUT_MS = 60_000


class JunkmailBrowser:
    """CDP connection to real Chrome for Junk Mail fetches."""

    def __init__(self, browser: Browser, page: Page, *, owns_playwright) -> None:
        self._browser = browser
        self.page = page
        self._owns_playwright = owns_playwright
        self.last_fetched_url: str | None = None

    def close(self) -> None:
        try:
            self._browser.close()
        except Exception as exc:
            LOG.debug("CDP disconnect: %s", exc)
        if self._owns_playwright:
            try:
                self._owns_playwright.stop()
            except Exception as exc:
                LOG.debug("Playwright stop: %s", exc)


def _pick_junkmail_page(browser: Browser) -> Page | None:
    for context in browser.contexts:
        for page in context.pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if "junkmail.co.za" in url and "doubleclick" not in url and "about:" not in url:
                return page
    return None


def _ensure_junkmail_page(
    browser: Browser,
    *,
    log: Callable[[str], None] | None = None,
    wait_cf_seconds: int = 600,
) -> Page:
    page = _pick_junkmail_page(browser)
    if page is None:
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        page.goto("https://www.junkmail.co.za/", wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)

    deadline = time.monotonic() + wait_cf_seconds
    while time.monotonic() < deadline:
        try:
            html = page.content()
            title = page.title()
        except Exception as exc:
            LOG.debug("Page read failed: %s", exc)
            time.sleep(2)
            continue
        if not is_cloudflare_challenge(html, title):
            return page
        if log:
            log(
                "Cloudflare — complete verification in the Junk Mail Chrome window, "
                "then the crawl will continue."
            )
        time.sleep(3)

    raise RuntimeError(
        "Cloudflare not cleared in Chrome. Open junkmail.co.za in the Junk Mail Chrome "
        "window, pass verification, then run setup: python junkmail/setup_cloudflare.py"
    )


def open_junkmail_browser(
    *,
    log: Callable[[str], None] | None = None,
    auto_start_chrome: bool = True,
) -> JunkmailBrowser:
    if not is_cdp_available(CDP_URL):
        if not auto_start_chrome:
            raise RuntimeError(
                "Junk Mail Chrome is not running. Run: python junkmail/setup_cloudflare.py"
            )
        if log:
            log("Starting Junk Mail Chrome...")
        start_manual_chrome()
        if not is_cdp_available(CDP_URL):
            raise RuntimeError("Chrome did not open on CDP port 9222.")

    playwright = sync_playwright().start()
    try:
        browser = connect_cdp_browser(playwright, CDP_URL)
    except Exception as exc:
        err = str(exc)
        if "setDownloadBehavior" in err or "context management is not supported" in err:
            raise RuntimeError(
                "Junk Mail Chrome CDP attach failed (Playwright download-behavior conflict). "
                "Upgrade Playwright in products/.venv: pip install 'playwright>=1.60.0', "
                "then restart app.py. Ensure Junk Mail Chrome is running: "
                "python junkmail/setup_cloudflare.py"
            ) from exc
        raise
    page = _ensure_junkmail_page(browser, log=log)
    return JunkmailBrowser(browser, page, owns_playwright=playwright)


def fetch_html(
    jm: JunkmailBrowser,
    url: str,
    *,
    log: Callable[[str], None] | None = None,
) -> str:
    if is_jobmail_listing_url(url):
        raise ValueError(
            "Junk Mail crawler does not open jobmail.co.za — use card data from the Junk Mail search page."
        )
    if "junkmail.co.za" not in (url or "").lower():
        raise ValueError(f"Junk Mail crawler only fetches junkmail.co.za URLs, not: {url}")

    page = jm.page
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    if is_junkmail_jobs_search_url(url):
        try:
            page.wait_for_selector(".card-setup", timeout=20_000)
        except Exception:
            pass
        time.sleep(1.5)
    else:
        time.sleep(1)
    html = page.content()
    title = page.title()
    if is_cloudflare_challenge(html, title):
        if log:
            log("Cloudflare on fetch — complete verification in Chrome...")
        page = _ensure_junkmail_page(jm._browser, log=log, wait_cf_seconds=300)
        jm.page = page
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        if is_junkmail_jobs_search_url(url):
            try:
                page.wait_for_selector(".card-setup", timeout=20_000)
            except Exception:
                pass
            time.sleep(1.5)
        else:
            time.sleep(1)
        html = page.content()
        title = page.title()
        if is_cloudflare_challenge(html, title):
            raise RuntimeError(
                "Cloudflare blocked fetch. Pass verification in the Junk Mail Chrome window."
            )
    jm.last_fetched_url = normalize_junkmail_listing_url(page.url) or normalize_junkmail_listing_url(url)
    return html
