#!/usr/bin/env python3
"""
Vinyl Cutters (vinylcutters.co.za) — session-based browse and save.

Shopify storefront; JSON-LD + /products/handle.json for verify. Script injection
skipped on challenge/captcha paths (same pattern as TSA Welding).
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.dom_product_extract import extract_generic_retail_product, goods_id_from_url
from shared.generic_session_scraper import GenericScraperConfig, run_generic_scrape_session
from shared.retail_product_pipeline import (
    build_and_save_product,
    build_scraped_index,
    default_is_product_url,
)
from shared.scraper_debug import capture_page_debug, combined_debug, dump_normalized_fields, log_extract_probe
from shared.verify_pricing import fetch_retail_pricing_http

SUPPLIER_SLUG = "vinylcutters"
HOST = "vinylcutters.co.za"
BASE_URL = "https://vinylcutters.co.za/"
SOURCE_PRICE_KEY = "vinylcutters_price"
TAGS = ["vinylcutters", "vinyl-cutters", "sublimation", "heat-press", "shopify"]
SESSION_FILE = Path(__file__).parent / "vinylcutters_session.json"

URLS_HEADER = f"""# Vinyl Cutters — product URLs (one per line), or browse and Save in the UI.
# Store: {BASE_URL}
# Example: https://vinylcutters.co.za/products/combo-sublimation-printer-with-mug-heat-press

"""


def canonical_vinylcutters_url(url: str) -> str:
    """Normalize Shopify collection-scoped PDP URLs to /products/{handle}."""
    u = (url or "").strip().split("#")[0].split("?")[0]
    m = re.search(r"/products/([^/]+)/?$", u, re.I)
    if not m:
        return u
    parsed = urlparse(u)
    host = (parsed.netloc or HOST).lower().replace("www.", "")
    return f"https://{host}/products/{m.group(1)}"


def extract_product_data(page, debug: bool = False):
    return extract_generic_retail_product(page, debug=debug)


def scrape_current_page(page, output_dir: Path) -> bool:
    url = canonical_vinylcutters_url(page.url or "")
    dbg = combined_debug(False)
    if not default_is_product_url(url, HOST):
        if dbg:
            capture_page_debug(page, output_dir, SUPPLIER_SLUG, "skip_non_pdp", dbg)
        return False
    data = extract_product_data(page, debug=dbg)
    log_extract_probe(SUPPLIER_SLUG, data, dbg)
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        capture_page_debug(page, output_dir, SUPPLIER_SLUG, "extract_empty", dbg)
        dump_normalized_fields(output_dir, SUPPLIER_SLUG, "extract_empty", {"raw": data}, dbg)
        return False
    dump_normalized_fields(
        output_dir,
        SUPPLIER_SLUG,
        "pre_save",
        {
            "goodsName": data.get("goodsName"),
            "salePrice": data.get("salePrice"),
            "gallery_n": len(data.get("gallery") or []),
        },
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
        goods_id_from_url,
    )
    if out is None:
        capture_page_debug(page, output_dir, SUPPLIER_SLUG, "save_failed", dbg)
    return out is not None


def scrape_url(page, url: str, output_dir: Path, debug: bool = False) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    scrape_current_page(page, output_dir)


def fetch_current_pricing(url: str, product: dict | None = None) -> dict | None:
    return fetch_retail_pricing_http(canonical_vinylcutters_url(url), SUPPLIER_SLUG, product=product)


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
        skip_script_on_paths=("challenge", "captcha"),
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
    parser = argparse.ArgumentParser(description="Scrape Vinyl Cutters (browse and save)")
    parser.add_argument("--output-dir", "-o", default=str(Path(__file__).parent / "scraped"))
    parser.add_argument("--save-session", action="store_true")
    args = parser.parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stop_flag = threading.Event()
    save_session_flag = threading.Event()
    if args.save_session:

        def _wait():
            input("  Browse in the browser, then press Enter to save session... ")
            save_session_flag.set()

        threading.Thread(target=_wait, daemon=True).start()
    run_scrape_session(output_dir, stop_flag, save_session_flag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
