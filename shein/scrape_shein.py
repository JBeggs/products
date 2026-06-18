#!/usr/bin/env python3
"""
SHEIN South Africa (za.shein.com) — session-based browse and save.

PDP URLs typically end with -p-{goodsId}.html. Price is SPA-rendered (no JSON-LD); uses SHEIN-specific DOM/BFF extract.
Enable SCRAPER_DEBUG=1 for debug_capture/ artifacts.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.shein_extract import extract_shein_product_data, shein_goods_id_from_url
from shared.generic_session_scraper import GenericScraperConfig, run_generic_scrape_session
from shared.retail_product_pipeline import (
    build_and_save_product,
    build_scraped_index,
    default_is_product_url,
)
from shared.scraper_debug import capture_page_debug, combined_debug, dump_normalized_fields, log_extract_probe

SUPPLIER_SLUG = "shein"
HOST = "shein.com"
BASE_URL = "https://za.shein.com/"
SOURCE_PRICE_KEY = "shein_price"
TAGS = ["shein", "za-shein"]
SESSION_FILE = Path(__file__).parent / "shein_session.json"

URLS_HEADER = f"""# SHEIN South Africa — product URLs (one per line), or browse and Save in the UI.
# Store: {BASE_URL}

"""

_SHEIN_PDP = re.compile(r"-p-\d+(?:\.html)?(?:\?|$|#)", re.I)


def is_shein_product_url(url: str) -> bool:
    """SHEIN PDP: za.shein.com/...-p-{id}.html (not category/home)."""
    u = (url or "").lower()
    if HOST not in u:
        return False
    if _SHEIN_PDP.search(u):
        return True
    return default_is_product_url(url, HOST)


def extract_product_data(page, debug: bool = False):
    return extract_shein_product_data(page, debug=debug)


def scrape_current_page(page, output_dir: Path) -> bool:
    url = page.url or ""
    dbg = combined_debug(False)
    if not is_shein_product_url(url):
        if dbg:
            capture_page_debug(page, output_dir, SUPPLIER_SLUG, "skip_non_pdp", dbg)
        return False
    data = extract_product_data(page, debug=dbg)
    log_extract_probe(SUPPLIER_SLUG, data, dbg)
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        capture_page_debug(page, output_dir, SUPPLIER_SLUG, "extract_empty", dbg)
        dump_normalized_fields(output_dir, SUPPLIER_SLUG, "extract_empty", {"raw": data}, dbg)
        return False
    sale = data.get("salePrice")
    if sale is None or (isinstance(sale, (int, float)) and sale <= 0):
        print(
            "  SHEIN: price not found — wait until the ZAR price shows on the page, then Save again.",
            flush=True,
        )
        if dbg:
            capture_page_debug(page, output_dir, SUPPLIER_SLUG, "no_price", dbg)
        return False
    dump_normalized_fields(
        output_dir,
        SUPPLIER_SLUG,
        "pre_save",
        {"goodsName": data.get("goodsName"), "salePrice": data.get("salePrice"), "gallery_n": len(data.get("gallery") or [])},
        dbg,
    )
    out = build_and_save_product(
        data,
        url,
        output_dir,
        SUPPLIER_SLUG,
        HOST,
        TAGS,
        SOURCE_PRICE_KEY,
        URLS_HEADER,
        shein_goods_id_from_url,
    )
    if out is None:
        capture_page_debug(page, output_dir, SUPPLIER_SLUG, "save_failed", dbg)
    return out is not None


def scrape_url(page, url: str, output_dir: Path, debug: bool = False) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    scrape_current_page(page, output_dir)


_shein_verify: dict = {}


def _get_shein_verify_page():
    page = _shein_verify.get("page")
    if page is not None:
        try:
            if not page.is_closed():
                return page
        except Exception:
            pass
        close_shein_verify_session()

    from playwright.sync_api import sync_playwright

    from shared.generic_session_scraper import LAUNCH_ARGS, USER_AGENT
    from shared.playwright_utils import PAGE_LOAD_TIMEOUT

    ctx_opts: dict = {
        "user_agent": USER_AGENT,
        "viewport": None,
        "locale": "en-ZA",
    }
    if SESSION_FILE.exists() and SESSION_FILE.stat().st_size > 0:
        ctx_opts["storage_state"] = str(SESSION_FILE)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True, args=LAUNCH_ARGS)
    context = browser.new_context(**ctx_opts)
    page = context.new_page()
    page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)
    _shein_verify["playwright"] = pw
    _shein_verify["browser"] = browser
    _shein_verify["context"] = context
    _shein_verify["page"] = page
    return page


def close_shein_verify_session() -> None:
    context = _shein_verify.pop("context", None)
    browser = _shein_verify.pop("browser", None)
    pw = _shein_verify.pop("playwright", None)
    _shein_verify.pop("page", None)
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
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


def fetch_current_pricing(url: str, product: dict | None = None) -> dict | None:
    """SHEIN verify: Playwright + saved session, select stored variant before reading price."""
    from shared.playwright_utils import PAGE_LOAD_TIMEOUT
    from shared.shein_extract import (
        extract_shein_product_data,
        product_variant_hint,
        select_shein_variant,
        wait_for_shein_pdp,
    )
    from shared.verify_pricing import pricing_result_from_data

    if not is_shein_product_url(url):
        return None
    if not SESSION_FILE.exists() or SESSION_FILE.stat().st_size == 0:
        return None
    try:
        page = _get_shein_verify_page()
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        wait_for_shein_pdp(page)
        hint = product_variant_hint(product)
        if hint:
            select_shein_variant(page, hint)
        data = extract_shein_product_data(page)
        return pricing_result_from_data(data, SUPPLIER_SLUG)
    except Exception:
        return None


def run_scrape_session(
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_options: dict | None = None,
) -> None:
    config = GenericScraperConfig(
        base_url=BASE_URL,
        login_url=BASE_URL,
        session_file=SESSION_FILE,
        hostname_pattern=HOST,
        supplier_slug=SUPPLIER_SLUG,
        skip_script_on_paths=("challenge", "captcha", "risk", "login", "user"),
        # Google sign-in opens a popup; without this, prevent_new_tab hijacks OAuth (same as Takealot/Game).
        allow_popup_for_hosts=(
            "accounts.google",
            "google.com",
            "firebaseapp",
            "gstatic.com",
        ),
    )
    run_generic_scrape_session(
        config,
        output_dir,
        stop_flag,
        save_session_flag,
        scrape_callback=scrape_current_page,
        build_index_callback=lambda od: build_scraped_index(od, SUPPLIER_SLUG, SOURCE_PRICE_KEY),
        scrape_options=scrape_options,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape SHEIN ZA (browse and save)")
    parser.add_argument("--output-dir", "-o", default=str(Path(__file__).parent / "scraped"))
    parser.add_argument("--save-session", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stop_flag = threading.Event()
    save_session_flag = threading.Event()
    if args.save_session:

        def _wait():
            input("  Log in in the browser, then press Enter to save session... ")
            save_session_flag.set()

        threading.Thread(target=_wait, daemon=True).start()
    run_scrape_session(output_dir, stop_flag, save_session_flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
