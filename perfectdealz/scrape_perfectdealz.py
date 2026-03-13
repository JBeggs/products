#!/usr/bin/env python3
"""
Perfect Dealz product scraper - session-based browse and save.
Extracts product data from Perfect Dealz (Shopify) product pages.
"""
import argparse
import json
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

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

SESSION_FILE = Path(__file__).parent / "perfectdealz_session.json"

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"

URLS_HEADER = """# Perfect Dealz product URLs (one per line)
# Example: https://perfectdealz.co.za/products/v8-chrome-rear-badge-sticker

"""


def extract_item_id(url: str) -> str | None:
    """Extract product handle from Perfect Dealz URL."""
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    if "/products/" in path:
        return path.split("/products/")[-1].split("?")[0] or None
    return None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from Perfect Dealz (Shopify) page via DOM."""
    try:
        data = page.evaluate("""
            () => {
                const titleEl = document.querySelector('h1.product-single__title') || document.querySelector('h1');
                const title = titleEl ? (titleEl.textContent || titleEl.innerText || '').trim() : null;

                const salePriceEl = document.querySelector('span.psinglePrice.sale') || document.querySelector('[id^="ProductPrice-"]') || document.querySelector('.product-single__price');
                let priceText = salePriceEl ? (salePriceEl.textContent || salePriceEl.innerText || salePriceEl.getAttribute('data-amount') || '').trim() : '';
                const priceMatch = priceText.match(/R\\s*([\\d.,]+)/i) || priceText.match(/([\\d.,]+)/);
                let price = priceMatch ? parseFloat(priceMatch[1].replace(/[,\\s]/g, '')) : null;
                if (price && price < 100 && salePriceEl && salePriceEl.getAttribute('data-amount')) {
                    price = parseFloat(salePriceEl.getAttribute('data-amount').replace(',', '.'));
                }

                const gallery = [];
                const seen = new Set();
                const imgs = document.querySelectorAll('.pr_lg_img img, .primgSlider img, media-gallery img, .product-single__media img');
                for (const img of imgs) {
                    let src = (img.getAttribute('src') || img.getAttribute('data-src') || img.closest('a')?.href || '').trim();
                    if (typeof src !== 'string') continue;
                    if (src && (src.includes('perfectdealz.co.za') || src.includes('cdn.shopify'))) {
                        src = src.replace(/&width=\\d+/, '&width=1096').replace(/\\?width=\\d+/, '?width=1096');
                        if (!seen.has(src.split('?')[0])) { seen.add(src.split('?')[0]); gallery.push(src.startsWith('//') ? 'https:' + src : src); }
                    }
                }

                const descEl = document.querySelector('.product-single__description') || document.querySelector('[id*="ProductDescription"]') || document.querySelector('.product-form__description');
                const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;

                let warranty = null;
                const descStr = desc || '';
                const wMatch = descStr.match(/[Ww]arranty[:\\s]+([^.\\n]+)/);
                if (wMatch) warranty = wMatch[1].trim();

                return { goodsName: title, salePrice: price, gallery, desc, warranty };
            }
        """)
        if data and (data.get("goodsName") or data.get("salePrice") is not None):
            return data
    except Exception as e:
        if debug:
            print(f"  DEBUG: extraction error: {e}")

    try:
        content = page.content()
        m = re.search(r'property="og:title"[^>]*content="([^"]+)"', content) or re.search(r'<title>([^<|]+)', content)
        title = m.group(1).strip() if m else None
        if title and "–" in title:
            title = title.split("–")[0].strip()
        if title and "|" in title:
            title = title.split("|")[0].strip()

        m = re.search(r'property="og:price:amount"[^>]*content="([^"]+)"', content)
        price = float(m.group(1).replace(",", "").strip()) if m else None
        if not m:
            m = re.search(r'"price":\s*"([\d.,]+)"', content) or re.search(r'ProductPrice[^>]*>R\s*([\d.,]+)', content)
            price = float(m.group(1).replace(",", ".")) if m else None
        if not m:
            m = re.search(r'"price":(\d+)', content)
            if m:
                price = float(m.group(1)) / 100

        gallery = []
        for m in re.finditer(r'https?://[^"\s]*perfectdealz\.co\.za/cdn/shop/files/[^"\s]+\.(?:webp|jpg|jpeg|png)', content):
            u = m.group(0).split("?")[0]
            if "width=" in m.group(0):
                u = re.sub(r"[?&]width=\d+", "?width=1096", m.group(0)).split("?")[0]
            if u not in gallery:
                gallery.append(u)
        if not gallery:
            m = re.search(r'property="og:image"[^>]*content="([^"]+)"', content)
            if m:
                gallery = [m.group(1).replace("http://", "https://")]

        m = re.search(r'property="og:description"[^>]*content="([^"]+)"', content)
        desc = m.group(1).strip() if m else title
        if desc:
            desc = desc.replace("&amp;", "&")

        warranty = None
        wm = re.search(r'[Ww]arranty[:\s]+([^.<\n]+)', content)
        if wm:
            warranty = wm.group(1).strip()[:80]

        if title or price is not None:
            return {
                "goodsName": title,
                "salePrice": price,
                "gallery": gallery[:10],
                "desc": desc or title,
                "warranty": warranty,
            }
    except Exception as e:
        if debug:
            print(f"  DEBUG: regex fallback error: {e}")
    return None


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
        if not url or "perfectdealz.co.za" not in url:
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
    base_prefix = f"{image_prefix(title, 20)}_{re.sub(r'[^a-zA-Z0-9_-]', '_', str(goods_id)[:30])}"
    for i, img_url in enumerate(image_urls, 1):
        try:
            if not img_url.startswith("http"):
                img_url = "https:" + img_url if img_url.startswith("//") else "https://perfectdealz.co.za" + img_url
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

    sell_price = apply_tiered_markup(sale_price_cents, "perfectdealz")
    cost = calculate_supplier_cost(sale_price_cents, "perfectdealz")
    compare_at_price = get_compare_at_price(sell_price)

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "perfectdealz_price": sale_price_zar,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["perfectdealz"],
        "goods_id": goods_id,
    }
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_current_page(page, output_dir: Path) -> bool:
    """Extract product data from current page and save. Returns True on success."""
    url = page.url or ""
    if "perfectdealz.co.za" not in url or "/products/" not in url:
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
            "perfectdealz_price": p.get("perfectdealz_price"),
            "goods_id": p.get("goods_id", ""),
        }
        for p in products
    ]
    index_path = output_dir / "index.json"
    index_path.write_text(json.dumps({"products": index_items}, indent=2), encoding="utf-8")


def fetch_current_pricing(url: str) -> dict | None:
    """
    Fetch current price/cost from Perfect Dealz URL. No persistence.
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
                data = extract_product_data(page, debug=False)
                if not data or data.get("salePrice") is None:
                    return None
                sale_price_zar = data.get("salePrice")
                sale_price_cents = int(sale_price_zar * 100)
                sell_price = apply_tiered_markup(sale_price_cents, "perfectdealz")
                cost = calculate_supplier_cost(sale_price_cents, "perfectdealz")
                return {
                    "price": round(sell_price, 2),
                    "cost": round(cost, 2),
                    "source_price": round(sale_price_zar, 2),
                    "valid": True,
                }
            finally:
                browser.close()
    except Exception:
        return None


def run_scrape_session(
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_options: dict | None = None,
) -> None:
    """Run Playwright scrape session for Perfect Dealz."""
    config = GenericScraperConfig(
        base_url="https://perfectdealz.co.za/",
        login_url="https://perfectdealz.co.za/",
        session_file=SESSION_FILE,
        hostname_pattern="perfectdealz.co.za",
        supplier_slug="perfectdealz",
        skip_script_on_paths=("login", "challenge", "account", "auth", "customers"),
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
    parser = argparse.ArgumentParser(description="Scrape Perfect Dealz products (browse and save)")
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
