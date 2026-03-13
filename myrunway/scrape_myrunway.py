#!/usr/bin/env python3
"""
MyRunway (myrunway.co.za) product scraper - reads URLs from urls.txt, scrapes product data,
saves to local folders (for review/debugging), optionally uploads to Django API.
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse, unquote

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Add products root for shared imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.utils import (
    apply_tiered_markup,
    get_compare_at_price,
    clean_description,
    first_n_words,
    image_prefix,
    remove_special_chars,
    slugify,
    truncate_name,
)
from shared.upload import get_auth_token, upload_product
from shared.config import get_category_for_slug, resolve_upload_targets

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"


def extract_product_id(url: str) -> str | None:
    """Extract product ID from MyRunway URL: /product/PUM_02365702_RED"""
    parsed = urlparse(url)
    path = parsed.path or ""
    match = re.search(r"/product/([A-Za-z0-9_-]+)(?:\?|$)", path)
    return match.group(1) if match else None


def _decode_next_image_url(img_src: str) -> str | None:
    """Extract real image URL from Next.js /_next/image?url=... format."""
    if not img_src or "/_next/image" not in img_src:
        return img_src
    try:
        from urllib.parse import parse_qs
        parsed = urlparse(img_src)
        if parsed.path and "/_next/image" in parsed.path:
            qs = parse_qs(parsed.query)
            url_param = qs.get("url", [None])[0]
            if url_param:
                decoded = unquote(url_param)
                # Replace %7C with | for S3 keys like PUM_02365702_RED|1.jpg
                decoded = decoded.replace("%7C", "|")
                return decoded
    except Exception:
        pass
    return None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from MyRunway product page via DOM."""
    try:
        data = page.evaluate("""
            () => {
                const productDetail = document.querySelector('[data-cy-id="product-detail"]');
                if (!productDetail) return null;

                // Brand: a[href^="/brand/"] span
                const brandEl = productDetail.querySelector('a[href^="/brand/"] span');
                const brand = brandEl ? (brandEl.textContent || '').trim() : null;

                // Product name: h1
                const h1 = productDetail.querySelector('h1');
                const productName = h1 ? (h1.textContent || '').trim() : null;
                const fullName = brand && productName ? (brand + ' ' + productName) : (productName || brand || 'Unknown Product');

                // Sale price: font-bold text-runway (R237)
                let salePrice = null;
                const priceEl = productDetail.querySelector('.font-bold.text-runway, [class*="text-runway"]');
                if (priceEl) {
                    const txt = (priceEl.textContent || '').replace(/[^0-9.]/g, '');
                    if (txt) salePrice = parseFloat(txt);
                }
                if (salePrice === null) {
                    const anyRunway = productDetail.querySelector('.text-runway');
                    if (anyRunway) {
                        const txt = (anyRunway.textContent || '').replace(/[^0-9.]/g, '');
                        if (txt) salePrice = parseFloat(txt);
                    }
                }

                // Compare-at: line-through
                let compareAt = null;
                const lineThrough = productDetail.querySelector('.line-through');
                if (lineThrough) {
                    const txt = (lineThrough.textContent || '').replace(/[^0-9.]/g, '');
                    if (txt) compareAt = parseFloat(txt);
                }

                // Images: swiper slides or carousel
                const gallery = [];
                const seen = new Set();
                const slides = productDetail.querySelectorAll('.swiper-slide img, [data-cy-id="carousel-img"]');
                for (const img of slides) {
                    const src = img.getAttribute('src') || img.getAttribute('srcset') || '';
                    const urls = src.split(',').map(s => s.trim().split(' ')[0]).filter(Boolean);
                    for (let u of urls) {
                        if (u && (u.includes('myrunway') || u.includes('s3-eu-west-1') || u.includes('/_next/image'))) {
                            const key = u.split('?')[0];
                            if (!seen.has(key)) {
                                seen.add(key);
                                gallery.push(u);
                            }
                        }
                    }
                }

                return {
                    goodsName: fullName,
                    salePrice,
                    compareAt,
                    gallery,
                    desc: fullName
                };
            }
        """)
        if data and (data.get("goodsName") or data.get("salePrice") is not None):
            return data
    except Exception as e:
        if debug:
            print(f"  DEBUG: extraction error: {e}")

    # Fallback: regex on page content
    try:
        content = page.content()
        m = re.search(r'font-bold text-runway[^>]*>R?\s*([\d.]+)', content)
        if not m:
            m = re.search(r'text-runway[^>]*>R?\s*([\d.]+)', content)
        price = float(m.group(1).replace(",", "")) if m else None
        m = re.search(r'<h1[^>]*>([^<]+)</h1>', content)
        title = m.group(1).strip() if m else None
        if title:
            title = title.replace("&amp;", "&").replace("&#39;", "'")
        m = re.search(r'href="/brand/([^"]+)"', content)
        brand = m.group(1).replace("%20", " ") if m else None
        if brand and title:
            title = f"{brand} {title}"
        gallery = []
        for m in re.finditer(r'/_next/image\?url=([^&"\s]+)', content):
            decoded = unquote(m.group(1))
            decoded = decoded.replace("%7C", "|")
            if "s3-eu-west-1" in decoded or "myrunway" in decoded:
                if decoded not in gallery:
                    gallery.append(decoded)
        if title or price is not None:
            return {"goodsName": title or "Unknown", "salePrice": price, "compareAt": None, "gallery": gallery[:10], "desc": title}
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
    sync_urls_from_products(products, output_dir)


URLS_HEADER = """# Add MyRunway product URLs (one per line)
# Example: https://myrunway.co.za/product/PUM_02365702_RED

"""


def sync_urls_from_products(products: list, output_dir: Path) -> None:
    urls_path = output_dir.parent / "urls.txt"
    seen = set()
    urls = []
    for p in products:
        url = (p.get("url") or "").strip()
        if not url or "myrunway.co.za" not in url:
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
    entity_id = extract_product_id(url) or "unknown"
    sale_price_zar = data.get("salePrice")
    sale_price_cents = int(sale_price_zar * 100) if sale_price_zar is not None else 0

    name = first_n_words(remove_special_chars(title), 5)
    short_desc = truncate_name(title, 150)

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    products = _load_products(output_dir)
    raw_urls = list(data.get("gallery") or [])[:10]
    image_files = []
    base_prefix = f"{image_prefix(title, 20)}_{entity_id}"
    for i, img_url in enumerate(raw_urls, 1):
        real_url = _decode_next_image_url(img_url) if "/_next/image" in (img_url or "") else img_url
        if not real_url:
            continue
        try:
            resp = requests.get(real_url, timeout=15)
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

    sell_price = apply_tiered_markup(sale_price_cents, "myrunway")
    cost = sale_price_zar or 0  # MyRunway is local (ZAR), no import duty
    compare_at_zar = data.get("compareAt")
    compare_at_price = get_compare_at_price(sell_price)
    if compare_at_zar and compare_at_zar > (sale_price_zar or 0):
        compare_at_price = round(compare_at_zar, 2)

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": clean_description(data.get("desc") or title)[:2000],
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "myrunway_price": sale_price_zar,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["imports"],
        "entity_id": entity_id,
    }
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def fetch_current_pricing(url: str) -> dict | None:
    """
    Fetch current price/cost from MyRunway URL. No persistence.
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
                time.sleep(3)
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

            sell_price = apply_tiered_markup(sale_price_cents, "myrunway")
            cost = sale_price_zar or 0  # MyRunway is local
            return {
                "price": round(sell_price, 2),
                "cost": round(cost, 2),
                "source_price": round(sale_price_zar or 0, 2),
                "valid": True,
            }
    except Exception:
        return None


def scrape_url(page, url: str, output_dir: Path, debug: bool = False) -> dict | None:
    """Scrape one MyRunway URL and append to products.json."""
    print(f"  Scraping: {url[:80]}...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)
    except Exception as e:
        print(f"  ERROR: Failed to load page: {e}")
        return None

    data = extract_product_data(page, debug=debug)
    if not data:
        print("  ERROR: Could not extract product data")
        return None

    product_json = _build_and_save_product(data, url, output_dir)
    if product_json:
        print(f"  Saved to {PRODUCTS_FILE}")
    return {"data": product_json} if product_json else None


def scrape_current_page(page, output_dir: Path) -> bool:
    """Scrape current page from Playwright. Returns True if saved. URL must be a MyRunway product page."""
    url = page.url
    if "myrunway.co.za" not in url.lower() or not extract_product_id(url):
        return False
    data = extract_product_data(page, debug=False)
    if not data:
        return False
    product_json = _build_and_save_product(data, url, output_dir)
    return product_json is not None


def run_scrape_session(
    output_dir: Path,
    stop_flag,
    save_session_flag,
    scrape_options: dict | None = None,
) -> None:
    """Run Playwright scrape session for MyRunway - browse and save."""
    from shared.generic_session_scraper import GenericScraperConfig, run_generic_scrape_session

    SESSION_FILE = Path(__file__).parent / "myrunway_session.json"
    config = GenericScraperConfig(
        base_url="https://myrunway.co.za/",
        login_url="https://myrunway.co.za/",
        session_file=SESSION_FILE,
        hostname_pattern="myrunway.co.za",
        supplier_slug="myrunway",
        allow_popup_for_hosts=("myrunway.co.za",),
    )
    run_generic_scrape_session(
        config, output_dir, stop_flag, save_session_flag,
        scrape_callback=scrape_current_page,
        build_index_callback=build_scraped_index,
        scrape_options=scrape_options,
    )


def build_scraped_index(output_dir: Path) -> None:
    from datetime import datetime
    products = _load_products(output_dir)
    if not products:
        return
    index_items = [
        {"name": p.get("name", ""), "price": p.get("price"), "myrunway_price": p.get("myrunway_price"), "entity_id": p.get("entity_id", "")}
        for p in products
    ]
    index = {"updated": datetime.now().isoformat(), "product_count": len(products), "products": index_items}
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    lines = ["# Scraped MyRunway Products\n", f"*{len(products)} products, updated {index['updated'][:10]}*\n\n"]
    lines.append("| Name | Price | MyRunway |\n")
    lines.append("|------|-------|----------|\n")
    for p in index_items:
        name = (p["name"][:50] + "..") if len(p["name"]) > 50 else p["name"]
        lines.append(f"| {name} | R{p['price']} | R{p['myrunway_price']} |\n")
    (output_dir / "README.md").write_text("".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Scrape MyRunway products, save locally, optionally upload to API")
    parser.add_argument("--urls", default=str(Path(__file__).parent / "urls.txt"), help="File with MyRunway URLs (one per line)")
    parser.add_argument("--output-dir", "-o", default=str(Path(__file__).parent / "scraped"), help="Output directory")
    parser.add_argument("--upload", action="store_true", help="Upload from scraped files to API")
    parser.add_argument("--upload-to", default=None, help="Upload to specific slug or 'all'")
    parser.add_argument("--list-categories", action="store_true", help="List categories for COMPANY_SLUG")
    parser.add_argument("--debug", action="store_true", help="Print debug info")
    args = parser.parse_args()

    urls_path = Path(args.urls)
    urls = []
    if urls_path.exists():
        for line in urls_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "myrunway.co.za" in line:
                urls.append(line)

    output_dir = Path(args.output_dir)
    if not urls:
        print("No URLs in urls.txt - starting interactive browse & save mode.")
        import threading
        stop_flag = threading.Event()
        save_session_flag = threading.Event()
        run_scrape_session(output_dir, stop_flag, save_session_flag)
        return 0

    print(f"Found {len(urls)} URL(s) in {urls_path} - batch mode")
    if args.list_categories:
        import os
        base_url = os.environ.get("API_BASE_URL", "").strip()
        username = os.environ.get("API_USERNAME", "").strip()
        password = os.environ.get("API_PASSWORD", "").strip()
        slugs = resolve_upload_targets(args.upload_to)
        if not slugs:
            slug = os.environ.get("COMPANY_SLUG", "").strip()
            slugs = [slug] if slug else [s.strip() for s in os.environ.get("COMPANY_SLUGS", "").split(",") if s.strip()]
        if not all([base_url, username, password]) or not slugs:
            print("Set API_BASE_URL, API_USERNAME, API_PASSWORD, COMPANY_SLUG or COMPANY_SLUGS in .env")
            return 1
        for company_slug in slugs:
            token = get_auth_token(base_url, username, password, company_slug=company_slug)
            if not token:
                print(f"Login failed for {company_slug}")
                continue
            headers = {"Authorization": f"Bearer {token}", "X-Company-Slug": company_slug}
            try:
                r = requests.get(f"{base_url.rstrip('/')}/v1/categories/", headers=headers, timeout=15)
                r.raise_for_status()
                data = r.json()
                items = data.get("results") if isinstance(data.get("results"), list) else (data if isinstance(data, list) else [])
                if not items:
                    print(f"No categories found for {company_slug}.")
                else:
                    print(f"Categories for {company_slug}:")
                    for c in items:
                        print(f"  {c.get('id')}  {c.get('name', '')} (slug: {c.get('slug', '')})")
            except Exception as e:
                print(f"Failed to list categories for {company_slug}: {e}")
        return 0

    if args.upload:
        import os
        base_url = os.environ.get("API_BASE_URL", "").strip()
        username = os.environ.get("API_USERNAME", "").strip()
        password = os.environ.get("API_PASSWORD", "").strip()
        use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
        target_slugs = resolve_upload_targets(args.upload_to)
        if not target_slugs:
            print("For --upload, set COMPANY_SLUG or COMPANY_SLUGS in .env, or use --upload-to <slug|all>")
            return 1
        if not all([base_url, username, password]):
            print("For --upload, set API_BASE_URL, API_USERNAME, API_PASSWORD in .env")
            return 1
        build_scraped_index(output_dir)
        products = _load_products(output_dir)
        for company_slug in target_slugs:
            category_id = get_category_for_slug(company_slug)
            if not category_id:
                print(f"  SKIP {company_slug}: no CATEGORY_ID in .env (CATEGORY_IDS=slug:uuid)")
                continue
            token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
            if not token:
                print(f"  Login failed for {company_slug}")
                continue
            print(f"Uploading to {company_slug}...")
            for data in products:
                upload_product(data, output_dir, base_url, token, company_slug, category_id)
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-ZA",
        )
        for i, url in enumerate(urls):
            print(f"[{i+1}/{len(urls)}]")
            scrape_url(page, url, output_dir, debug=args.debug)
            if i < len(urls) - 1:
                time.sleep(3)
        browser.close()

    build_scraped_index(output_dir)
    print(f"Done. Products saved to {output_dir}/")
    print("Edit products.json, then run --upload to push to API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
