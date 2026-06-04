#!/usr/bin/env python3
"""
Cosmetic Connection (cosmeticconnection.co.za) — session-based browse and save scraper.

Generic PDP extraction + debug capture support.
"""
from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

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

SUPPLIER_SLUG = "cosmeticconnection"
HOST = "cosmeticconnection.co.za"
BASE_URL = "https://www.cosmeticconnection.co.za/"
SOURCE_PRICE_KEY = "cosmeticconnection_price"
TAGS = ["cosmeticconnection", "cosmetics", "haircare"]
SESSION_FILE = Path(__file__).parent / "cosmeticconnection_session.json"

URLS_HEADER = f"""# Cosmetic Connection — product URLs (one per line), or browse and Save in the UI.
# Store: {BASE_URL}

"""


def extract_product_data(page, debug: bool = False):
    return extract_generic_retail_product(page, debug=debug)


def scrape_current_page(page, output_dir: Path) -> bool:
    url = page.url or ""
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
        goods_id_from_url,
    )
    if out is None:
        capture_page_debug(page, output_dir, SUPPLIER_SLUG, "save_failed", dbg)
    return out is not None


def scrape_url(page, url: str, output_dir: Path, debug: bool = False) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    scrape_current_page(page, output_dir)


def fetch_current_pricing(url: str, product: dict | None = None) -> dict | None:
    from shared.verify_pricing import fetch_retail_pricing_http
    return fetch_retail_pricing_http(url, SUPPLIER_SLUG, product=product)


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
    parser = argparse.ArgumentParser(description="Scrape Cosmetic Connection (browse and save)")
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
