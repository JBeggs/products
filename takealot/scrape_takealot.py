#!/usr/bin/env python3
"""
Takealot product scraper - session-based browse and save.
Extracts product data from Takealot product pages (Next.js/React).
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

SESSION_FILE = Path(__file__).parent / "takealot_session.json"

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"

URLS_HEADER = """# Takealot product URLs (one per line)
# Example: https://www.takealot.com/product-name/PLID99812186

"""


def extract_plid(url: str) -> str | None:
    """Extract PLID from Takealot URL: /product-name/PLID99812186"""
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    m = re.search(r"/PLID(\d+)(?:\?|$|/)", path, re.I) or re.search(r"PLID(\d+)", path, re.I)
    return m.group(1) if m else None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from Takealot page via DOM and meta."""
    try:
        data = page.evaluate("""
            () => {
                const titleEl = document.querySelector('h1.pdp-module_title') || document.querySelector('.pdp h1') || document.querySelector('h1');
                const title = titleEl ? (titleEl.textContent || titleEl.innerText || '').trim() : null;

                const priceEl = document.querySelector('.buybox-offer-module_offer_1JNpe .currency') || document.querySelector('[class*="buybox"] .currency') || document.querySelector('.currency');
                let priceText = priceEl ? (priceEl.textContent || priceEl.innerText || '').trim() : '';
                const priceMatch = priceText.match(/R\\s*([\\d.,]+)/i) || priceText.match(/([\\d.,]+)/);
                const price = priceMatch ? parseFloat(priceMatch[1].replace(/[,\\s]/g, '')) : null;

                const gallery = [];
                const seen = new Set();
                // Main gallery: img[data-ref^="main-gallery-photo-"] (covers_images + covers_tsins)
                const mainImgs = document.querySelectorAll('img[data-ref^="main-gallery-photo-"]');
                const isProductImg = (s) => s && (s.includes('media.takealot.com/covers_images/') || s.includes('media.takealot.com/covers_tsins/')) && !s.includes('/promotions/');
                for (const img of mainImgs) {
                    let src = (img.getAttribute('src') || img.getAttribute('data-src') || '').trim();
                    if (isProductImg(src)) {
                        const base = src.split('?')[0];
                        if (!seen.has(base)) {
                            seen.add(base);
                            gallery.push(src.startsWith('//') ? 'https:' + src : src);
                        }
                    }
                }
                // Fallback: any product images in pdp-gallery (covers_images or covers_tsins)
                if (gallery.length === 0) {
                    const fallback = document.querySelectorAll('.pdp-gallery img[src*="media.takealot.com"], [class*="pdp-gallery"] img[src*="media.takealot.com"]');
                    for (const img of fallback) {
                        let src = (img.getAttribute('src') || img.getAttribute('data-src') || '').trim();
                        if (isProductImg(src)) {
                            const base = src.split('?')[0];
                            if (!seen.has(base)) {
                                seen.add(base);
                                gallery.push(src.startsWith('//') ? 'https:' + src : src);
                            }
                        }
                    }
                }

                const descEl = document.querySelector('[class*="description"]') || document.querySelector('.pdp-module_description');
                const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;

                return { goodsName: title, salePrice: price, gallery, desc };
            }
        """)
        if data and (data.get("goodsName") or data.get("salePrice") is not None):
            return data
    except Exception as e:
        if debug:
            print(f"  DEBUG: extraction error: {e}")

    try:
        content = page.content()
        m = re.search(r'<meta name="description" content="([^"]+)"', content)
        title = m.group(1).strip().split(".")[0] if m else None
        if not title:
            m = re.search(r'<title>([^<|]+)', content)
            title = m.group(1).strip().split("|")[0].strip() if m else None

        gallery = []
        for pat in (r'rel="preload"[^>]*href="(https://media\.takealot\.com/(?:covers_images|covers_tsins)/[^"]+)"',
                    r'https://media\.takealot\.com/(?:covers_images|covers_tsins)/[^"\s]+\.(?:webp|jpg|jpeg|png|file)',
                    r'src="(https://media\.takealot\.com/(?:covers_images|covers_tsins)/[^"]+)"'):
            for m in re.finditer(pat, content):
                u = (m.group(1) if m.lastindex else m.group(0)).split("?")[0]
                if u not in gallery and "/promotions/" not in u:
                    gallery.append(u)

        gallery = [u for u in gallery if ("covers_images" in u or "covers_tsins" in u) and "/promotions/" not in u]

        price = None
        m = re.search(r'R\s*([\d,]+\.?\d*)', content)
        if m:
            price = float(m.group(1).replace(",", ""))
        if price is None or price < 10:
            for m in re.finditer(r'"amount":\s*([\d.]+)', content):
                p = float(m.group(1))
                if 10 < p < 1000000:
                    price = p
                    break

        if title or price is not None:
            return {
                "goodsName": title,
                "salePrice": price,
                "gallery": gallery[:10],
                "desc": title,
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
        if not url or "takealot.com" not in url:
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
    goods_id = data.get("goodsId") or extract_plid(url) or "unknown"
    sale_price_zar = data.get("salePrice")
    sale_price_cents = int(sale_price_zar * 100) if sale_price_zar is not None else 0

    name = first_n_words(remove_special_chars(title), 5)
    short_desc = truncate_name(title, 150)
    description = clean_description(data.get("desc") or title)[:2000]

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    products = _load_products(output_dir)
    image_urls = [u for u in (data.get("gallery") or []) if u and ("covers_images" in u or "covers_tsins" in u)][:10]
    image_files = []
    base_prefix = f"{image_prefix(title, 20)}_{re.sub(r'[^a-zA-Z0-9_-]', '_', str(goods_id)[:30])}"
    for i, img_url in enumerate(image_urls, 1):
        try:
            if not img_url.startswith("http"):
                img_url = "https:" + img_url if img_url.startswith("//") else "https://media.takealot.com" + img_url
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

    sell_price = apply_tiered_markup(sale_price_cents, "takealot")
    cost = calculate_supplier_cost(sale_price_cents, "takealot")
    compare_at_price = get_compare_at_price(sell_price)

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "takealot_price": sale_price_zar,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["takealot"],
        "goods_id": goods_id,
    }
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_current_page(page, output_dir: Path) -> bool:
    """Extract product data from current page and save. Returns True on success."""
    url = page.url or ""
    if "takealot.com" not in url or not extract_plid(url):
        return False
    data = extract_product_data(page, debug=False)
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        return False
    data["goodsId"] = extract_plid(url)
    product_json = _build_and_save_product(data, url, output_dir)
    return product_json is not None


def build_scraped_index(output_dir: Path) -> None:
    from datetime import datetime

    products = _load_products(output_dir)
    if not products:
        return
    index_items = [
        {"name": p.get("name", ""), "price": p.get("price"), "takealot_price": p.get("takealot_price"), "goods_id": p.get("goods_id", "")}
        for p in products
    ]
    index = {"updated": datetime.now().isoformat(), "product_count": len(products), "products": index_items}
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    lines = ["# Scraped Takealot Products\n", f"*{len(products)} products, updated {index['updated'][:10]}*\n\n"]
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
    """Run Playwright scrape session for Takealot."""
    config = GenericScraperConfig(
        base_url="https://www.takealot.com/",
        login_url="https://www.takealot.com/",
        session_file=SESSION_FILE,
        hostname_pattern="takealot.com",
        supplier_slug="takealot",
        use_persistent_context=True,
        persistent_user_data_dir=Path(__file__).parent / "chrome_profile",
        allow_popup_for_hosts=("accounts.google", "firebaseapp"),
    )
    run_generic_scrape_session(
        config, output_dir, stop_flag, save_session_flag,
        scrape_callback=scrape_current_page,
        build_index_callback=build_scraped_index,
        scrape_options=scrape_options,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape Takealot products (browse and save)")
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
