"""Reused headless browser for verify-all (one launch per batch, not per product)."""
from __future__ import annotations

from playwright.sync_api import Page, sync_playwright

from shared.dom_product_extract import extract_generic_retail_product
from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, VERIFY_CONTENT_TIMEOUT_MS, VERIFY_NAV_TIMEOUT_MS, block_heavy_resources
from shared.retail_product_pipeline import default_is_product_url
from shared.verify_pricing import pricing_result_from_data

_session: dict = {}


def _get_page() -> Page:
    page = _session.get("page")
    if page is not None:
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass
        close_verify_browser()
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=CHROMIUM_PERFORMANCE_ARGS)
    page = browser.new_page(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        locale="en-ZA",
    )
    _session["playwright"] = pw
    _session["browser"] = browser
    _session["page"] = page
    return page


def close_verify_browser() -> None:
    browser = _session.pop("browser", None)
    pw = _session.pop("playwright", None)
    _session.pop("page", None)
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def fetch_generic_retail_playwright(url: str, supplier_slug: str, host: str) -> dict | None:
    """Playwright fallback for sites without JSON-LD in initial HTML."""
    try:
        page = _get_page()
        page.set_default_navigation_timeout(VERIFY_NAV_TIMEOUT_MS)
        page.set_default_timeout(VERIFY_CONTENT_TIMEOUT_MS)
        block_heavy_resources(page)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=VERIFY_NAV_TIMEOUT_MS)
        except Exception:
            if not default_is_product_url(page.url or "", host):
                return None
        try:
            page.wait_for_selector('script[type="application/ld+json"]', timeout=8_000)
        except Exception:
            try:
                page.wait_for_selector("h1", timeout=5_000)
            except Exception:
                if not default_is_product_url(page.url or "", host):
                    return None
        if not default_is_product_url(page.url or "", host):
            return None
        data = extract_generic_retail_product(page, debug=False)
        in_stock = True
        try:
            from shared.verify_utils import playwright_page_in_stock
            in_stock = playwright_page_in_stock(page)
        except Exception:
            pass
        if data:
            data["in_stock"] = in_stock
        return pricing_result_from_data(data, supplier_slug)
    except Exception:
        return None
