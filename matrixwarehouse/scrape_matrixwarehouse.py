#!/usr/bin/env python3
"""
Matrix Warehouse product scraper - session-based browse and save.
Extracts product data from Matrix Warehouse (Shopify) product pages.
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

from shared.config import merge_supplier_delivery_from_scrape
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

SESSION_FILE = Path(__file__).parent / "matrixwarehouse_session.json"

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"

URLS_HEADER = """# Matrix Warehouse product URLs (one per line)
# Example: https://matrixwarehouse.co.za/products/kalobee-sk7-ultra-hd-smartwatch

"""


def extract_delivery_info(page) -> dict | None:
    """Extract delivery time/cost from Matrix Warehouse JSON-LD (Shopify schema)."""
    try:
        content = page.content()
        # shippingDetails with deliveryTime.transitTime and shippingRate
        m = re.search(
            r'"transitTime"\s*:\s*\{[^}]*"minValue"\s*:\s*(\d+)[^}]*"maxValue"\s*:\s*(\d+)',
            content,
            re.DOTALL,
        )
        delivery_time = None
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            delivery_time = f"{lo}-{hi} business days"
        m = re.search(r'"shippingRate"\s*:\s*\{[^}]*"value"\s*:\s*([\d.]+)', content, re.DOTALL)
        delivery_cost = float(m.group(1)) if m else None
        out = {}
        if delivery_time:
            out["delivery_time"] = delivery_time
        if delivery_cost is not None:
            out["delivery_cost"] = delivery_cost
        return out if out else None
    except Exception:
        return None


def extract_item_id(url: str) -> str | None:
    """Extract product handle from Matrix Warehouse URL."""
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    if "/products/" in path:
        return path.split("/products/")[-1].split("?")[0] or None
    return None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from Matrix Warehouse (Shopify/Rise) page via DOM and meta."""
    try:
        data = page.evaluate("""
            () => {
                const titleEl = document.querySelector('h1.product__title') || document.querySelector('h1');
                const title = titleEl ? (titleEl.textContent || titleEl.innerText || '').trim() : null;

                const salePriceEl = document.querySelector('span.price-item--sale.price--final') || document.querySelector('span.price-item--sale') || document.querySelector('.price--final .price-item');
                let priceText = salePriceEl ? (salePriceEl.textContent || salePriceEl.innerText || salePriceEl.getAttribute('data-amount') || '').trim() : '';
                const priceMatch = priceText.match(/R\\s*([\\d.,]+)/i) || priceText.match(/([\\d.,]+)/);
                let price = priceMatch ? parseFloat(priceMatch[1].replace(/[,\\s]/g, '')) : null;
                if (price && price < 100 && salePriceEl && salePriceEl.getAttribute('data-amount')) {
                    price = parseFloat(salePriceEl.getAttribute('data-amount').replace(',', '.'));
                }

                const gallery = [];
                const seen = new Set();
                const imgs = document.querySelectorAll('.product__media img, .slider img, slider-component img, .slideshow img');
                for (const img of imgs) {
                    let src = (img.getAttribute('src') || img.getAttribute('data-src') || '').trim();
                    if (src && (src.includes('matrixwarehouse.co.za') || src.includes('cdn.shopify'))) {
                        src = src.replace(/&width=\\d+/, '&width=1096').replace(/\\?width=\\d+/, '?width=1096');
                        if (!seen.has(src.split('?')[0])) { seen.add(src.split('?')[0]); gallery.push(src.startsWith('//') ? 'https:' + src : src); }
                    }
                }

                const descEl = document.querySelector('text-expandable.product__info-block--description') || document.querySelector('.product__info-block--description') || document.querySelector('[data-button-text-expand]');
                const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;

                return { goodsName: title, salePrice: price, gallery, desc };
            }
        """)
        if data and data.get("salePrice") is not None:
            if not data.get("goodsName"):
                try:
                    content = page.content()
                    m = re.search(r'property="og:title"[^>]*content="([^"]+)"', content) or re.search(r'<title>([^<]+)</title>', content)
                    if m:
                        t = m.group(1).strip()
                        if " – " in t:
                            t = t.split(" – ")[0].strip()
                        data["goodsName"] = t
                except Exception:
                    pass
            return data
    except Exception as e:
        if debug:
            print(f"  DEBUG: extraction error: {e}")

    try:
        content = page.content()
        m = re.search(r'property="og:title"[^>]*content="([^"]+)"', content) or re.search(r'<title>([^<]+)</title>', content)
        title = m.group(1).strip() if m else None
        if title and " – " in title:
            title = title.split(" – ")[0].strip()
        m = re.search(r'property="og:price:amount"[^>]*content="([^"]+)"', content)
        price = float(m.group(1).replace(",", "").strip()) if m else None
        if not m:
            m = re.search(r'"price":\s*"([\d.,]+)"', content) or re.search(r'data-amount="([\d.]+)"[^>]*>R\s*[\d,]+', content)
            price = float(m.group(1).replace(",", ".")) if m else None
        gallery = []
        for m in re.finditer(r'https?://[^"\s]*matrixwarehouse\.co\.za/cdn/shop/files/[^"\s]+\.(?:webp|jpg|jpeg|png)', content):
            u = m.group(0).split("?")[0]
            if "width=" in m.group(0):
                u = re.sub(r"[?&]width=\d+", "?width=1096", m.group(0)).split("?")[0]
            if u not in gallery:
                gallery.append(u)
        if not gallery:
            m = re.search(r'property="og:image:secure_url"[^>]*content="([^"]+)"', content) or re.search(r'property="og:image"[^>]*content="([^"]+)"', content)
            if m:
                gallery = [m.group(1).replace("http://", "https://")]
        desc = None
        m = re.search(r'property="og:description"[^>]*content="([^"]+)"', content)
        if m:
            desc = m.group(1).strip()
        if title or price is not None:
            return {
                "goodsName": title,
                "salePrice": price,
                "gallery": gallery[:10],
                "desc": desc or title,
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
        if not url or "matrixwarehouse.co.za" not in url:
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
    description = clean_description(data.get("desc") or title)[:2000]

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    products = _load_products(output_dir)
    image_urls = list(data.get("gallery") or [])[:10]
    image_files = []
    base_prefix = f"{image_prefix(title, 20)}_{re.sub(r'[^a-zA-Z0-9_-]', '_', str(goods_id)[:30])}"
    for i, img_url in enumerate(image_urls, 1):
        try:
            if not img_url.startswith("http"):
                img_url = "https:" + img_url if img_url.startswith("//") else "https://matrixwarehouse.co.za" + img_url
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

    sell_price = apply_tiered_markup(sale_price_cents, "matrixwarehouse")
    cost = calculate_supplier_cost(sale_price_cents, "matrixwarehouse")
    compare_at_price = get_compare_at_price(sell_price)

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "matrixwarehouse_price": sale_price_zar,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["matrixwarehouse"],
        "goods_id": goods_id,
    }
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_current_page(page, output_dir: Path) -> bool:
    """Extract product data from current page and save. Returns True on success."""
    url = page.url or ""
    if "matrixwarehouse.co.za" not in url or "/products/" not in url:
        return False
    delivery_info = extract_delivery_info(page)
    if delivery_info:
        merge_supplier_delivery_from_scrape("matrixwarehouse", delivery_info)
    data = extract_product_data(page, debug=False)
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        return False
    data["goodsId"] = extract_item_id(url)
    product_json = _build_and_save_product(data, url, output_dir)
    return product_json is not None


def build_scraped_index(output_dir: Path) -> None:
    from datetime import datetime

    products = _load_products(output_dir)
    if not products:
        return
    index_items = [
        {"name": p.get("name", ""), "price": p.get("price"), "matrixwarehouse_price": p.get("matrixwarehouse_price"), "goods_id": p.get("goods_id", "")}
        for p in products
    ]
    index = {"updated": datetime.now().isoformat(), "product_count": len(products), "products": index_items}
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    lines = ["# Scraped Matrix Warehouse Products\n", f"*{len(products)} products, updated {index['updated'][:10]}*\n\n"]
    lines.append("| Name | Price |\n")
    lines.append("|------|-------|\n")
    for p in index_items:
        name = (p["name"][:50] + "..") if len(p["name"]) > 50 else p["name"]
        lines.append(f"| {name} | R{p.get('price', 'N/A')} |\n")
    (output_dir / "README.md").write_text("".join(lines), encoding="utf-8")


def run_scrape_session(
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_options: dict | None = None,
) -> None:
    """Run Playwright scrape session for Matrix Warehouse."""
    config = GenericScraperConfig(
        base_url="https://matrixwarehouse.co.za/",
        login_url="https://matrixwarehouse.co.za/",
        session_file=SESSION_FILE,
        hostname_pattern="matrixwarehouse.co.za",
        supplier_slug="matrixwarehouse",
        skip_script_on_paths=("login", "challenge", "account", "auth", "customers"),
        button_position="left",
    )
    run_generic_scrape_session(
        config, output_dir, stop_flag, save_session_flag,
        scrape_callback=scrape_current_page,
        build_index_callback=build_scraped_index,
        scrape_options=scrape_options,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Matrix Warehouse products (browse and save)")
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
