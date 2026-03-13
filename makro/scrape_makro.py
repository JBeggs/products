#!/usr/bin/env python3
"""
Makro product scraper - session-based browse and save.
Extracts product data from Makro product pages (Flipkart-style layout).
"""
import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from playwright.sync_api import sync_playwright

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.generic_session_scraper import GenericScraperConfig, run_generic_scrape_session
from shared.utils import (
    apply_tiered_markup,
    calculate_supplier_cost,
    get_compare_at_price,
    clean_description,
    first_n_words,
    image_prefix,
    remove_special_chars,
    truncate_name,
)

SESSION_FILE = Path(__file__).parent / "makro_session.json"

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"

URLS_HEADER = """# Makro product URLs (one per line)
# Example: https://www.makro.co.za/honor-x7c-4g-dual-sim-256gb-black-256-gb/p/itm0975edb559f7a?pid=MOBH7E4A9DDBCMGQ

"""


def extract_item_id(url: str) -> str | None:
    """Extract product ID from Makro URL: pid=MOBH7E4A9DDBCMGQ"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    pids = qs.get("pid", [])
    return pids[0] if pids else None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from Makro page via DOM."""
    try:
        data = page.evaluate("""
            () => {
                const titleEl = document.querySelector('h1 span.B_NuCI') || document.querySelector('h1.yhB1nd span.B_NuCI') || document.querySelector('h1 span.B_NuCI') || document.querySelector('h1');
                const title = titleEl ? (titleEl.textContent || titleEl.innerText || '').trim() : null;

                const priceEl = document.querySelector('div.Xaaq-1._16Jk6d') || document.querySelector('div.CEmiEU div._30jeq3') || document.querySelector('div._30jeq3._16Jk6d') || document.querySelector('div._30jeq3');
                let priceText = priceEl ? (priceEl.textContent || priceEl.innerText || '').trim() : '';
                const priceMatch = priceText.match(/R\\s*([\\d.,]+)/i) || priceText.match(/([\\d.,]+)/);
                const price = priceMatch ? parseFloat(priceMatch[1].replace(/[,\\s]/g, '')) : null;

                const gallery = [];
                const seen = new Set();

                const mainImg = document.querySelector('img._396cs4._2amPTt._3qGmMb') || document.querySelector('img._396cs4[alt]');
                if (mainImg) {
                    let src = (mainImg.getAttribute('src') || mainImg.getAttribute('data-src') || '').trim();
                    if (src && src.includes('makro.co.za')) {
                        src = src.replace('/128/128/', '/832/832/').replace('/416/416/', '/832/832/').replace('/312/312/', '/832/832/');
                        if (!seen.has(src.split('?')[0])) { seen.add(src.split('?')[0]); gallery.push(src); }
                    }
                }

                const thumbImgs = document.querySelectorAll('img.q6DClP, ul._3GnUWp img, ._2mLllQ img');
                for (const img of thumbImgs) {
                    let src = (img.getAttribute('src') || img.getAttribute('data-src') || '').trim();
                    if (src && src.includes('makro.co.za') && src.includes('asset')) {
                        src = src.replace('/128/128/', '/832/832/').replace('/416/416/', '/832/832/').replace('/312/312/', '/832/832/');
                        if (!seen.has(src.split('?')[0])) { seen.add(src.split('?')[0]); gallery.push(src); }
                    }
                }

                const descEl = document.querySelector('div._2o-xpa div._1mXcCf') || document.querySelector('div._1mXcCf') || document.querySelector('div._2o-xpa');
                const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;

                let warranty = null;
                const warrantyEl = document.querySelector('div._352bdz') || document.querySelector('div.XcYV4g div._352bdz');
                if (warrantyEl) warranty = (warrantyEl.textContent || warrantyEl.innerText || '').trim();

                const specRows = [];
                const tables = document.querySelectorAll('table._14cfVK');
                for (const t of tables) {
                    const rows = t.querySelectorAll('tr');
                    for (const r of rows) {
                        const label = r.querySelector('td._1hKmbr');
                        const val = r.querySelector('td.URwL2w');
                        if (label && val) {
                            const l = (label.textContent || '').trim();
                            const v = (val.textContent || '').trim();
                            if (l && v) specRows.push({ label: l, value: v });
                        }
                    }
                }
                if (!warranty && specRows.length) {
                    const w = specRows.find(function(x) { return /warranty/i.test(x.label); });
                    if (w) warranty = w.value;
                }

                return { goodsName: title, salePrice: price, gallery, desc, warranty, specRows };
            }
        """)
        if data and (data.get("goodsName") or data.get("salePrice") is not None):
            return data
    except Exception as e:
        if debug:
            print(f"  DEBUG: extraction error: {e}")

    try:
        content = page.content()
        m = re.search(r'"B_NuCI"[^>]*>([^<]+)<', content)
        title = m.group(1).strip() if m else None
        m = re.search(r'R\s*([\d.,]+)', content)
        price = float(m.group(1).replace(",", "").strip()) if m else None
        gallery = []
        for m in re.finditer(r'https://[^"]*makro\.co\.za/asset[^"]*\.(?:jpeg|jpg|png|webp)', content):
            u = m.group(0).split("?")[0]
            u = re.sub(r"/\d+/\d+/", "/832/832/", u)
            if u not in gallery:
                gallery.append(u)
        warranty = None
        wm = re.search(r'Warranty[^>]*>.*?([^<]+(?:Year|Month|day)[^<]*)', content, re.I | re.S)
        if wm:
            warranty = wm.group(1).strip()[:50]
        if title or price is not None:
            return {
                "goodsName": title,
                "salePrice": price,
                "gallery": gallery[:10],
                "desc": title,
                "warranty": warranty,
                "specRows": [],
            }
    except Exception as e:
        if debug:
            print(f"  DEBUG: regex fallback error: {e}")
    return None


def _parse_dimensions_from_specs(spec_rows: list) -> dict:
    """Parse dimension_width, dimension_height, dimension_length, weight from specRows.
    Makro uses Width/Height/Depth (cm) and Weight (kg). We store weight in grams."""
    result = {}
    if not spec_rows:
        return result
    label_to_key = {
        "width": "dimension_width",
        "height": "dimension_height",
        "depth": "dimension_length",
    }
    for row in spec_rows:
        label = (row.get("label") or "").strip().lower()
        value = (row.get("value") or "").strip()
        if not value:
            continue
        if label == "weight":
            # e.g. "8 kg" -> 8000 grams
            m = re.search(r"([\d.]+)\s*(kg|g|gram|grams)?", value, re.I)
            if m:
                num = float(m.group(1))
                unit = (m.group(2) or "kg").lower()
                if "g" in unit and "kg" not in unit:
                    result["weight"] = int(num)
                else:
                    result["weight"] = int(num * 1000)
            continue
        for kw, key in label_to_key.items():
            if kw in label:
                m = re.search(r"([\d.]+)\s*(cm|mm|m)?", value, re.I)
                if m:
                    num = float(m.group(1))
                    unit = (m.group(2) or "cm").lower()
                    if unit == "mm":
                        num = num / 10
                    elif unit == "m":
                        num = num * 100
                    result[key] = round(num, 2)
                break
    return result


def _load_products(output_dir: Path) -> list:
    path = output_dir / PRODUCTS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("products", [])
    except Exception:
        return []


def _save_products(output_dir: Path, products: list) -> None:
    from datetime import datetime

    path = output_dir / PRODUCTS_FILE
    path.write_text(
        json.dumps({"products": products, "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    _sync_urls_from_products(products, output_dir)


def _sync_urls_from_products(products: list, output_dir: Path) -> None:
    urls_path = output_dir.parent / "urls.txt"
    seen = set()
    urls = []
    for p in products:
        url = (p.get("url") or "").strip()
        if not url or "makro.co.za" not in url:
            continue
        base = url.split("?")[0].strip()
        if base and base not in seen:
            seen.add(base)
            urls.append(base)
    urls_path.parent.mkdir(parents=True, exist_ok=True)
    urls_path.write_text(URLS_HEADER + "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")


def _build_and_save_product(data: dict, url: str, output_dir: Path) -> dict | None:
    """Build product dict from extracted data and append to products.json."""
    title = data.get("goodsName") or "Unknown Product"
    goods_id = data.get("goodsId") or extract_item_id(url) or "unknown"
    sale_price_zar = data.get("salePrice")
    sale_price_cents = int(sale_price_zar * 100) if sale_price_zar is not None else 0

    name = first_n_words(remove_special_chars(title), 5)
    short_desc = truncate_name(title, 150)

    desc_parts = [data.get("desc") or title]
    warranty = (data.get("warranty") or "").strip()
    if warranty:
        desc_parts.append(f"\n\nWarranty: {warranty}")
    description = clean_description("\n".join(desc_parts))[:2000]

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    products = _load_products(output_dir)
    image_urls = list(data.get("gallery") or [])[:10]
    image_files = []
    base_prefix = f"{image_prefix(title, 20)}_{goods_id}"
    for i, img_url in enumerate(image_urls, 1):
        try:
            resp = requests.get(img_url, timeout=15)
            resp.raise_for_status()
            ext = ".jpg"
            if "png" in resp.headers.get("content-type", ""):
                ext = ".png"
            elif "webp" in resp.headers.get("content-type", ""):
                ext = ".webp"
            fname = f"{base_prefix}_{i:02d}{ext}"
            rel_path = f"{IMAGES_DIR}/{fname}"
            (images_dir / fname).write_bytes(resp.content)
            image_files.append(rel_path)
        except Exception as e:
            print(f"  WARNING: Could not download image {i}: {e}")

    if not image_files:
        print("  WARNING: No images downloaded")
        return None

    sell_price = apply_tiered_markup(sale_price_cents, "makro")
    cost = calculate_supplier_cost(sale_price_cents, "makro")
    compare_at_price = get_compare_at_price(sell_price)

    dims = _parse_dimensions_from_specs(data.get("specRows") or [])

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "makro_price": sale_price_zar,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["makro"],
        "goods_id": goods_id,
    }
    if dims.get("weight") is not None:
        product_json["weight"] = dims["weight"]
    if dims.get("dimension_width") is not None:
        product_json["dimension_width"] = dims["dimension_width"]
    if dims.get("dimension_height") is not None:
        product_json["dimension_height"] = dims["dimension_height"]
    if dims.get("dimension_length") is not None:
        product_json["dimension_length"] = dims["dimension_length"]
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_current_page(page, output_dir: Path) -> bool:
    """Extract product data from current page and save. Returns True on success."""
    url = page.url or ""
    if "makro.co.za" not in url or "/p/" not in url:
        return False
    data = extract_product_data(page, debug=False)
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        return False
    result = _build_and_save_product(data, url, output_dir)
    return result is not None


def build_scraped_index(output_dir: Path) -> None:
    """Build index from products.json for display."""
    products = _load_products(output_dir)
    index_items = [
        {
            "name": p.get("name", ""),
            "price": p.get("price"),
            "makro_price": p.get("makro_price"),
            "goods_id": p.get("goods_id", ""),
        }
        for p in products
    ]
    index_path = output_dir / "index.json"
    index_path.write_text(json.dumps({"products": index_items}, indent=2), encoding="utf-8")


def fetch_current_pricing(url: str) -> dict | None:
    """
    Fetch current price/cost from Makro URL. No persistence.
    Returns {price, cost, source_price, valid: True} or None if invalid/blocked.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-ZA",
            )
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=15000)
                time.sleep(2)
            except Exception:
                browser.close()
                return None

            data = extract_product_data(page, debug=False)
            browser.close()
            if not data or (not data.get("goodsName") and data.get("salePrice") is None):
                return None

            sale_price_zar = data.get("salePrice")
            sale_price_cents = int(sale_price_zar * 100) if sale_price_zar is not None else 0
            if sale_price_cents <= 0:
                return None
            sell_price = apply_tiered_markup(sale_price_cents, "makro")
            cost = calculate_supplier_cost(sale_price_cents, "makro")
            makro_price = sale_price_zar if sale_price_zar is not None else (sale_price_cents / 100)
            return {
                "price": round(sell_price, 2),
                "cost": round(cost, 2),
                "source_price": round(makro_price, 2),
                "valid": True,
            }
    except Exception:
        return None


def run_scrape_session(
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_options: dict | None = None,
) -> None:
    """Run Playwright scrape session for Makro."""
    config = GenericScraperConfig(
        base_url="https://www.makro.co.za/",
        login_url="https://www.makro.co.za/",
        session_file=SESSION_FILE,
        hostname_pattern="makro.co.za",
        supplier_slug="makro",
    )
    run_generic_scrape_session(
        config,
        output_dir,
        stop_flag,
        save_session_flag,
        scrape_callback=scrape_current_page,
        build_index_callback=build_scraped_index,
        scrape_options=scrape_options,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Makro products (browse and save)")
    parser.add_argument("--output-dir", "-o", default=str(Path(__file__).parent / "scraped"))
    parser.add_argument("--save-session", action="store_true", help="Log in, then press Enter to save session")
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
