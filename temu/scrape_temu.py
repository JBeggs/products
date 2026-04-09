#!/usr/bin/env python3
"""
Temu product scraper - reads URLs from urls.txt, scrapes product data,
saves to local folders (for review/debugging), optionally uploads to Django API.
"""
import argparse
import html
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Add products root for shared imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.utils import (
    apply_tiered_markup,
    calculate_supplier_cost,
    get_compare_at_price,
    clean_description,
    first_n_words,
    image_prefix,
    remove_special_chars,
    slugify,
    truncate_name,
)
from shared.upload import get_auth_token, get_or_create_category, upload_product
from shared.config import get_category_for_slug, resolve_upload_targets, merge_supplier_delivery_from_scrape

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SESSION_FILE = Path(__file__).parent / "temu_session.json"
CHROME_PROFILE = Path(__file__).parent / "chrome_profile"


def extract_goods_id(url: str) -> str | None:
    """Extract goods_id from Temu URL (path -g-XXXXX or query)."""
    parsed = urlparse(url)
    path = parsed.path or ""
    match = re.search(r"-g-(\d+)\.html", path)
    if match:
        return match.group(1)
    qs = parse_qs(parsed.query)
    return qs.get("goods_id", [None])[0]


def extract_description_from_dom(page):
    """Extract product details from visible DOM (Product details section)."""
    try:
        result = page.evaluate("""
            () => {
                const h2 = Array.from(document.querySelectorAll('h2, h3, [role="heading"]')).find(el =>
                    /Product details|Specification|Description/i.test(el.innerText || '')
                );
                if (h2) {
                    let el = h2.parentElement;
                    while (el && !el.querySelector('div, p')) el = el.parentElement;
                    const text = (el?.innerText || h2?.nextElementSibling?.innerText || '').trim();
                    if (text.length > 50) return text;
                }
                const goodsDetail = document.querySelector('[id*="goodsDetail"], [class*="goodsDetail"], [class*="product-detail"]');
                if (goodsDetail) {
                    const text = (goodsDetail.innerText || goodsDetail.textContent || '').trim();
                    if (text.length > 50) return text;
                }
                return null;
            }
        """)
        return result
    except Exception:
        return None


def extract_variants_from_dom(page):
    """Extract color options only from the Color spec section (aria-label on buttons)."""
    try:
        result = page.evaluate("""
            () => {
                const colorSpan = Array.from(document.querySelectorAll('span')).find(s => s.textContent.trim() === 'Color');
                if (!colorSpan) return [];
                let section = colorSpan.closest('div');
                for (let i = 0; i < 8 && section; i++) {
                    const buttons = section.querySelectorAll('[role="button"][aria-label]');
                    if (buttons.length >= 2 && buttons.length <= 20) {
                        const variants = [];
                        const seen = new Set();
                        buttons.forEach(btn => {
                            const label = (btn.getAttribute('aria-label') || '').trim();
                            if (label && label.length >= 3 && label.length <= 80 && !/^\\d+$/.test(label)) {
                                if (!seen.has(label)) { seen.add(label); variants.push(label); }
                            }
                        });
                        if (variants.length > 0) return variants;
                    }
                    section = section.parentElement;
                }
                return [];
            }
        """)
        return result if result else []
    except Exception:
        return []


def extract_category_from_breadcrumb(page):
    """Extract category from breadcrumb - third li with link (e.g. Camping & Hiking)."""
    try:
        result = page.evaluate("""
            () => {
                const lis = Array.from(document.querySelectorAll('li'));
                const withLinks = lis.filter(li => li.querySelector('a[href]'));
                const third = withLinks[2];
                if (!third) return null;
                const a = third.querySelector('a[href]');
                if (!a) return null;
                const name = (a.textContent || a.innerText || '').trim();
                const href = a.getAttribute('href') || '';
                const slugMatch = href.match(/\\/([^/]+)\\.html$/);
                const slug = slugMatch ? slugMatch[1].replace(/-o\\d+-\\d+$/, '').replace(/[^a-z0-9-]/gi, '-').replace(/-+/g, '-').replace(/^-|-$/g, '') : null;
                return name ? { name, slug: slug || name.toLowerCase().replace(/\\s+/g, '-').replace(/[^a-z0-9-]/g, '') } : null;
            }
        """)
        return result
    except Exception:
        return None


def extract_price_from_dom(page):
    """Extract sale price from visible DOM. Uses ONLY 'Estimated R' - no fallbacks."""
    try:
        result = page.evaluate("""
            () => {
                const priceEl = document.getElementById('goods_price');
                let text = priceEl ? (priceEl.innerText || priceEl.textContent || '').trim() : '';
                if (!text) {
                    const rightContent = document.getElementById('rightContent');
                    if (rightContent) text = (rightContent.innerText || rightContent.textContent || '').trim();
                }
                if (!text) {
                    const alt = document.querySelector('[data-price], [id*="price"], [class*="goods_price"]');
                    if (alt) text = (alt.innerText || alt.textContent || '').trim();
                }
                const estMatch = text.match(/Estimated\\s*R\\s*([\\d,]+)/i);
                return estMatch ? (parseInt(estMatch[1].replace(/,/g, ''), 10) || null) : null;
            }
        """)
        return result
    except Exception:
        return None


def extract_delivery_info(page) -> dict | None:
    """Try to extract delivery time/cost from Temu page. Returns dict or None."""
    try:
        content = page.content()
        # Look for patterns like "7-13 days", "7-15 business days", "Ships in X days"
        m = re.search(
            r"(?:ships?\s+in|delivery|estimated)\s*(?:in)?\s*(\d+)\s*[-–]\s*(\d+)\s*(?:business\s+)?days?",
            content,
            re.I,
        ) or re.search(
            r"(\d+)\s*[-–]\s*(\d+)\s*(?:business\s+)?days?\s*(?:delivery|shipping)?",
            content,
            re.I,
        )
        if m:
            return {"delivery_time": f"{m.group(1)}-{m.group(2)} business days"}
        m = re.search(r"(\d+)\s*(?:business\s+)?days?\s*(?:delivery|shipping)?", content, re.I)
        if m:
            return {"delivery_time": f"{m.group(1)} business days"}
    except Exception:
        pass
    return None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from Temu page via window.rawData or regex fallback."""
    # Try JS extraction first
    try:
        data = page.evaluate("""
            () => {
                const rd = window.rawData;
                if (!rd) return null;
                const find = (obj, key) => {
                    if (!obj || typeof obj !== 'object') return undefined;
                    if (obj[key] !== undefined) return obj[key];
                    for (const v of Object.values(obj)) {
                        const r = find(v, key);
                        if (r !== undefined) return r;
                    }
                    return undefined;
                };
                const findArray = (obj, key) => {
                    const v = find(obj, key);
                    return Array.isArray(v) ? v : [];
                };
                const goodsName = find(rd, 'goodsName');
                const salePrice = find(rd, 'salePrice');
                const goodsId = find(rd, 'goods_id') || (rd.store?.webLayoutData?.commonData?.query?.goods_id);
                const topGallery = find(rd, 'top_gallery_url') || (rd.store?.webLayoutData?.commonData?.query?.top_gallery_url);
                let gallery = findArray(rd, 'gallery');
                if (gallery.length === 0) gallery = findArray(rd, 'imgList') || findArray(rd, 'imageList');
                const desc = find(rd, 'desc') || find(rd, 'goodsDesc') || goodsName;
                const productDetail = find(rd, 'productDetail');
                let detailText = '';
                if (productDetail && productDetail.floorList && Array.isArray(productDetail.floorList)) {
                    const parts = [];
                    for (const floor of productDetail.floorList) {
                        if (floor.items && Array.isArray(floor.items)) {
                            for (const item of floor.items) {
                                if (item.text && typeof item.text === 'string') parts.push(item.text);
                            }
                        }
                    }
                    detailText = parts.join('\\n\\n');
                }
                const fullDesc = detailText ? (detailText + (desc ? '\\n\\n' + desc : '')) : desc;
                return {
                    goodsName: typeof goodsName === 'string' ? goodsName : null,
                    salePrice: typeof salePrice === 'string' ? parseInt(salePrice, 10) : (typeof salePrice === 'number' ? salePrice : null),
                    goodsId: typeof goodsId === 'string' ? goodsId : null,
                    topGalleryUrl: typeof topGallery === 'string' ? topGallery : null,
                    gallery: gallery.map(g => g && g.url ? g.url : null).filter(Boolean),
                    desc: typeof fullDesc === 'string' ? fullDesc : (typeof desc === 'string' ? desc : goodsName)
                };
            }
        """)
        if data and (data.get("goodsName") or data.get("salePrice") is not None):
            dom_price = extract_price_from_dom(page)
            if debug:
                print(f"  DEBUG: rawData salePrice={data.get('salePrice')}, dom_price (Estimated R only)={dom_price}")
            data["salePrice"] = dom_price
            dom_desc = extract_description_from_dom(page)
            if dom_desc:
                data["desc"] = dom_desc
            data["variants"] = extract_variants_from_dom(page)
            return data
        if debug and data:
            print(f"  DEBUG: rawData found but empty fields: {data}")
    except Exception as e:
        if debug:
            print(f"  DEBUG: JS extraction error: {e}")

    # Fallback: regex on page content
    try:
        content = page.content()
        goods_name = None
        sale_price = None
        goods_id = None
        top_gallery = None
        gallery_urls = []

        m = re.search(r'"goodsName"\s*:\s*"([^"]+)"', content)
        if m:
            goods_name = m.group(1)

        detail_parts = []
        for m in re.finditer(r'"text"\s*:\s*"((?:[^"\\]|\\.){50,2000})"', content):
            t = m.group(1).replace("\\u002F", "/").replace("\\n", "\n")
            if "Product" in t or "Specification" in t or "Characteristics" in t:
                detail_parts.append(t)
        detail_text = "\n\n".join(detail_parts) if detail_parts else None

        m = re.search(r'"salePrice"\s*:\s*"(\d+)"', content)
        if m:
            sale_price = int(m.group(1))

        m = re.search(r'"goods_id"\s*:\s*"(\d+)"', content)
        if m:
            goods_id = m.group(1)

        m = re.search(r'"top_gallery_url"\s*:\s*"(https://[^"]+)"', content)
        if m:
            top_gallery = m.group(1)

        for m in re.finditer(r'"url"\s*:\s*"(https://img\.kwcdn\.com[^"]+)"', content):
            u = m.group(1)
            if u not in gallery_urls:
                gallery_urls.append(u)

        if top_gallery and top_gallery not in gallery_urls:
            gallery_urls.insert(0, top_gallery)

        if goods_name or sale_price is not None:
            dom_price = extract_price_from_dom(page)
            if debug:
                print(f"  DEBUG: regex salePrice={sale_price}, dom_price (Estimated R only)={dom_price}")
            sale_price = dom_price
            dom_desc = extract_description_from_dom(page)
            desc = dom_desc or (detail_text + "\n\n" + goods_name if detail_text and goods_name else (detail_text or goods_name))
            variants = extract_variants_from_dom(page)
            return {
                "goodsName": goods_name,
                "salePrice": sale_price,
                "goodsId": goods_id,
                "topGalleryUrl": top_gallery,
                "gallery": gallery_urls,
                "desc": desc,
                "variants": variants,
            }
        if debug:
            print(f"  DEBUG: regex found goodsName={goods_name!r}, salePrice={sale_price}, goods_id={goods_id}")
    except Exception as e:
        if debug:
            print(f"  DEBUG: regex fallback error: {e}")

        if debug:
            try:
                has_raw = page.evaluate("() => !!window.rawData")
                body_len = page.evaluate("() => document.body?.innerText?.length || 0")
                # Sample rawData top-level keys and a recursive search for our target keys
                sample = page.evaluate("""
                    () => {
                        const rd = window.rawData;
                        if (!rd) return { error: 'no rawData' };
                        const topKeys = Object.keys(rd);
                        const findPath = (obj, key, path) => {
                            if (!obj || typeof obj !== 'object') return null;
                            if (obj[key] !== undefined) return (path || '') + (path ? '.' : '') + key;
                            for (const [k, v] of Object.entries(obj)) {
                                if (typeof v === 'object' && v !== null && k !== '$') {
                                    const r = findPath(v, key, (path || '') + (path ? '.' : '') + k);
                                    if (r) return r;
                                }
                            }
                            return null;
                        };
                        return {
                            topKeys,
                            goodsNamePath: findPath(rd, 'goodsName', ''),
                            salePricePath: findPath(rd, 'salePrice', ''),
                            goodsIdPath: findPath(rd, 'goods_id', ''),
                            galleryPath: findPath(rd, 'gallery', ''),
                        };
                    }
                """)
                print(f"  DEBUG: rawData top keys: {sample.get('topKeys', [])[:10]}")
                print(f"  DEBUG: paths: goodsName={sample.get('goodsNamePath')}, salePrice={sample.get('salePricePath')}, goods_id={sample.get('goodsIdPath')}, gallery={sample.get('galleryPath')}")
            except Exception as e:
                print(f"  DEBUG: could not inspect page: {e}")

    return None


PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"


def _load_products(output_dir: Path) -> list:
    """Load products from products.json. Returns list (empty if file missing)."""
    path = output_dir / PRODUCTS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("products", [])
    except Exception:
        return []


def _save_products(output_dir: Path, products: list) -> None:
    """Save products to products.json and sync urls.txt."""
    from datetime import datetime
    path = output_dir / PRODUCTS_FILE
    path.write_text(
        json.dumps({"products": products, "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    sync_urls_from_products(products, output_dir)


def _images_exist_for_goods_id(output_dir: Path, products: list, goods_id: str) -> list | None:
    """Return image paths if any product with this goods_id already has images; else None."""
    for p in products:
        if p.get("goods_id") == goods_id:
            imgs = p.get("images") or []
            if imgs:
                return imgs
    return None


URLS_HEADER = """# Add Temu product URLs (one per line)
# Example: https://www.temu.com/za/your-product-name-g-123456789.html

"""


def sync_urls_from_products(products: list, output_dir: Path) -> None:
    """Rebuild urls.txt from products list. Preserves product order, dedupes by base URL."""
    urls_path = output_dir.parent / "urls.txt"
    seen = set()
    urls = []
    for p in products:
        url = (p.get("url") or "").strip()
        if not url or "-g-" not in url:
            continue
        base = url.split("?")[0].strip()
        if base and base not in seen:
            seen.add(base)
            urls.append(base)
    urls_path.parent.mkdir(parents=True, exist_ok=True)
    urls_path.write_text(URLS_HEADER + "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")


def _build_and_save_product(data: dict, url: str, output_dir: Path, variant_name: str | None = None) -> dict | None:
    """Build product dict from extracted data and append to products.json. Does not navigate.
    Only saves when salePrice (Estimated R) is present and > 0."""
    sale_price_rands = data.get("salePrice")
    if sale_price_rands is None or sale_price_rands <= 0:
        return None
    title = data.get("goodsName") or "Unknown Product"
    goods_id = data.get("goodsId") or extract_goods_id(url) or "unknown"
    sale_price_cents = int(sale_price_rands * 100)

    base_name = first_n_words(remove_special_chars(title), 5)
    name = f"{base_name} ({variant_name})" if variant_name else base_name
    short_desc = truncate_name(title, 300)

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    products = _load_products(output_dir)
    existing_images = None
    if not variant_name:
        existing_images = _images_exist_for_goods_id(output_dir, products, goods_id)

    if existing_images:
        image_files = existing_images
    else:
        image_urls = []
        if data.get("topGalleryUrl"):
            image_urls.append(data["topGalleryUrl"])
        for u in data.get("gallery") or []:
            base = u.split("?")[0]
            if not any(base == x.split("?")[0] for x in image_urls):
                image_urls.append(u)

        image_files = []
        base_prefix = f"{image_prefix(title, 20)}_{goods_id}"
        for i, img_url in enumerate(image_urls[:10], 1):
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

    sell_price = apply_tiered_markup(sale_price_cents, "temu")
    cost = calculate_supplier_cost(sale_price_cents, "temu")
    compare_at_price = get_compare_at_price(sell_price)

    product_json = {
        "url": url,
        "name": name,
        "description": clean_description(data.get("desc") or title)[:2000],
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "temu_price": sale_price_rands,
        "images": image_files,
        "variants": data.get("variants") or [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["imports"],
        "goods_id": goods_id,
    }
    if variant_name:
        product_json["variant"] = variant_name
    if data.get("category_name"):
        product_json["category_name"] = data["category_name"]
    if data.get("category_slug"):
        product_json["category_slug"] = data["category_slug"]

    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_url(page, url: str, output_dir: Path, debug: bool = False, variant_name: str | None = None) -> dict | None:
    """Scrape one Temu URL and append to products.json. Images in output_dir/images/."""
    debug = debug or (os.environ.get("SCRAPER_DEBUG", "").lower() in ("1", "true", "yes"))
    print(f"  Scraping: {url[:80]}...")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_function("window.rawData || document.body?.innerText?.length > 500", timeout=20000)
        time.sleep(2)
    except Exception as e:
        print(f"  ERROR: Failed to load page: {e}")
        try:
            diag = _get_page_diagnostics(page)
            _save_debug_html(page, output_dir, f"Page load failed: {e}", diag)
        except Exception:
            pass
        return None

    data = extract_product_data(page, debug=debug)
    if not data or data.get("salePrice") is None or data.get("salePrice") <= 0:
        diag = _get_page_diagnostics(page)
        _save_debug_html(page, output_dir, "Extraction failed or no Estimated R price", diag)
        print(f"  ERROR: Could not extract product data or no Estimated R price found. HTML saved to debug_html/")
        if debug:
            print(f"  DEBUG: goods_price: {repr((diag or {}).get('goodsPriceText', ''))[:120]}")
        return None

    delivery_info = extract_delivery_info(page)
    if delivery_info:
        merge_supplier_delivery_from_scrape("temu", delivery_info)

    category_info = extract_category_from_breadcrumb(page)
    if category_info:
        data["category_name"] = category_info.get("name")
        data["category_slug"] = category_info.get("slug") or slugify(category_info.get("name", ""))

    product_json = _build_and_save_product(data, url, output_dir, variant_name=variant_name)
    if product_json:
        print(f"  Saved to {PRODUCTS_FILE}")
    return {"data": product_json} if product_json else None


def fetch_current_pricing(url: str) -> dict | None:
    """
    Fetch current price/cost from Temu URL. No persistence.
    Returns {price, cost, source_price, valid: True} or None if invalid/blocked.
    """
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = create_browser_context(browser)
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_function("window.rawData || document.body?.innerText?.length > 500", timeout=20000)
                time.sleep(1.5)
            except Exception:
                browser.close()
                return None

            data = extract_product_data(page, debug=False)
            browser.close()
            if not data or (not data.get("goodsName") and data.get("salePrice") is None):
                return None

            sale_price_rands = data.get("salePrice")
            sale_price_cents = int(sale_price_rands * 100) if sale_price_rands is not None else 0
            if sale_price_cents <= 0:
                return None

            sell_price = apply_tiered_markup(sale_price_cents, "temu")
            cost = calculate_supplier_cost(sale_price_cents, "temu")
            temu_price = sale_price_rands if sale_price_rands is not None else (sale_price_cents / 100)
            return {
                "price": round(sell_price, 2),
                "cost": round(cost, 2),
                "source_price": round(temu_price, 2),
                "valid": True,
            }
    except Exception:
        return None


def _get_page_diagnostics(page) -> dict:
    """Return diagnostic info about page state for debugging."""
    try:
        return page.evaluate("""
            () => {
                const priceEl = document.getElementById('goods_price');
                const rc = document.getElementById('rightContent');
                const priceText = priceEl ? (priceEl.innerText || priceEl.textContent || '').trim() : '';
                const rcText = rc ? (rc.innerText || rc.textContent || '').substring(0, 500) : '';
                return {
                    hasGoodsPrice: !!priceEl,
                    goodsPriceText: priceText,
                    hasRightContent: !!rc,
                    bodyLength: document.body ? (document.body.innerText || '').length : 0,
                    hasRawData: !!window.rawData,
                    url: location.href,
                };
            }
        """)
    except Exception as e:
        return {"error": str(e), "url": page.url}


def _save_debug_html(page, output_dir: Path, reason: str, diagnostics: dict | None = None) -> Path | None:
    """Save page HTML to debug_html/ for debugging. Returns path or None."""
    from datetime import datetime
    debug_dir = output_dir / "debug_html"
    debug_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    url_slug = re.sub(r"[^\w\-]", "_", (page.url or "page")[:80])
    fname = f"{ts}_{url_slug}.html"
    path = debug_dir / fname
    try:
        html = page.content()
        path.write_text(html, encoding="utf-8")
        diag = f" (goods_price={repr((diagnostics or {}).get('goodsPriceText', ''))[:80]})" if diagnostics else ""
        print(f"  DEBUG: {reason}. HTML saved to {path}{diag}")
        return path
    except Exception as e:
        print(f"  DEBUG: {reason}. Could not save HTML: {e}")
        return None


def scrape_current_page(page, output_dir: Path, debug: bool = False) -> bool:
    """Scrape current page without navigating. Returns True if saved. URL must be a product page (-g-XXX.html)."""
    url = page.url
    if not re.search(r"-g-\d+\.html", url):
        return False
    debug = debug or (os.environ.get("SCRAPER_DEBUG", "").lower() in ("1", "true", "yes"))
    # Wait for product content to load (Estimated R price) - Temu pages can load slowly
    try:
        page.wait_for_function(
            """() => {
                const priceEl = document.getElementById('goods_price');
                let text = priceEl ? (priceEl.innerText || priceEl.textContent || '').trim() : '';
                if (!text) {
                    const rc = document.getElementById('rightContent');
                    if (rc) text = (rc.innerText || rc.textContent || '').trim();
                }
                return /Estimated\\s*R\\s*[\\d,]+/i.test(text);
            }""",
            timeout=15000,
        )
    except Exception as e:
        diag = _get_page_diagnostics(page)
        _save_debug_html(page, output_dir, f"Estimated R price never appeared (timeout 15s): {e}", diag)
        print(f"  DEBUG: goods_price text: {repr((diag or {}).get('goodsPriceText', ''))[:120]}")
        if debug:
            print(f"  DEBUG: Full diagnostics: {diag}")
        return False
    data = extract_product_data(page, debug=debug)
    if not data or data.get("salePrice") is None or data.get("salePrice") <= 0:
        diag = _get_page_diagnostics(page)
        _save_debug_html(page, output_dir, "Extraction failed or no Estimated R price", diag)
        print(f"  DEBUG: goods_price text: {repr((diag or {}).get('goodsPriceText', ''))[:120]}")
        if debug:
            print(f"  DEBUG: extract_product_data returned: {data}")
            print(f"  DEBUG: Full diagnostics: {diag}")
        return False
    category_info = extract_category_from_breadcrumb(page)
    if category_info:
        data["category_name"] = category_info.get("name")
        data["category_slug"] = category_info.get("slug") or slugify(category_info.get("name", ""))
    _build_and_save_product(data, url, output_dir, variant_name=None)
    return True


def _get_variant_snapshot(page):
    """Get current price and selected variant from DOM. Handles Color, Size, Number Of Products, etc."""
    try:
        return page.evaluate("""
            () => {
                const priceEl = document.getElementById('goods_price');
                let text = priceEl ? (priceEl.innerText || priceEl.textContent || '') : '';
                const estMatch = text.match(/Estimated\\s*R\\s*([\\d,]+)/i);
                const price = estMatch ? parseInt(estMatch[1].replace(/,/g, ''), 10) : null;

                let variant = null;
                const specRoot = document.getElementById('rightContent') || document.body;
                // 1) Collect ALL selected values from <em> in spec sections (Color:, Size:, Number Of Products:, etc.)
                const btns = document.querySelectorAll('[role="button"][aria-label]');
                const selectedFromBtns = [];
                const specLabels = ['Model', 'Color', 'Size', 'Number', 'Products', 'Specification'];
                for (const btn of btns) {
                    let inSpec = false;
                    let sectionLabel = '';
                    for (let el = btn; el && el !== document.body; el = el.parentElement) {
                        const txt = el.textContent || '';
                        if (txt.length >= 15 && txt.length < 400) {
                            const found = specLabels.find(l => txt.includes(l + ':') || txt.includes(l + ' '));
                            if (found) { inSpec = true; sectionLabel = found; break; }
                        }
                    }
                    if (!inSpec) continue;
                    const isSelected = btn.classList.contains('_363EuJDX') || btn.getAttribute('aria-pressed') === 'true' || (sectionLabel !== 'Color' && btn.querySelector('svg'));
                    if (isSelected) {
                        const vb = (btn.getAttribute('aria-label') || btn.textContent || '').trim();
                        const skip = /^(select amount|add to cart|share|qty|quantity|choose|select)$/i.test(vb) || vb.length > 50;
                        if (vb && vb.length >= 2 && !skip && !selectedFromBtns.includes(vb)) selectedFromBtns.push(vb);
                    }
                }
                if (selectedFromBtns.length > 0) variant = selectedFromBtns.join(' - ');
                if (!variant) {
                const selectedFromEm = [];
                const ems = specRoot.querySelectorAll('em');
                for (const em of ems) {
                    const parent = em.closest('div');
                    if (!parent) continue;
                    const parentText = (parent.textContent || '').trim();
                    const v = (em.textContent || '').trim();
                    if (!v || v.length < 2 || v.length > 80 || /^\\d+$/.test(v)) continue;
                        if (parentText.includes('Color:') || parentText.includes('Size:') || parentText.includes('Model') || parentText.includes('Number') || parentText.includes('Products') || parentText.includes('Specification') || /^[A-Za-z0-9\\s]+:\\s*/.test(parentText)) {
                        if (!selectedFromEm.includes(v)) selectedFromEm.push(v);
                    }
                }
                if (selectedFromEm.length > 0) variant = selectedFromEm.join(' - ');
                }
                // 3) Legacy: aria-pressed
                if (!variant) {
                    const selected = document.querySelector('[role="button"][aria-pressed="true"]');
                    if (selected) variant = (selected.getAttribute('aria-label') || selected.textContent || '').trim() || null;
                }
                return { price, variant };
            }
        """)
    except Exception:
        return None


def _get_current_main_image_url(page) -> str | None:
    """Get the currently displayed main product image src from DOM (changes when color variant is selected)."""
    try:
        return page.evaluate("""
            () => {
                const imgs = document.querySelectorAll('img[src*="kwcdn.com"][src*="product"], img[src*="kwcdn.com"][src*="fancy"]');
                for (const img of imgs) {
                    const src = (img.getAttribute('src') || '').trim();
                    if (src && src.includes('kwcdn.com') && (src.includes('/product/') || src.includes('fancy'))) {
                        const rect = img.getBoundingClientRect();
                        if (rect.width >= 200 && rect.height >= 100) return src;
                    }
                }
                const main = document.querySelector('[class*="lazy-image"][src*="kwcdn.com"]');
                return main ? (main.getAttribute('src') || '').trim() || null : null;
            }
        """)
    except Exception:
        return None


def run_variant_capture_mode(page, output_dir: Path, urls: list) -> None:
    """Interactive mode: auto-advances through URLs. Enter=stop here, n=next, s=save."""
    cmd_queue = Queue()
    DWELL_SEC = 4

    def read_commands():
        while True:
            try:
                line = input().strip().lower()
                cmd_queue.put(line if line else "[stop]")
            except EOFError:
                break

    t = threading.Thread(target=read_commands, daemon=True)
    t.start()

    print("\nVariant capture mode. Auto-advances through URLs.")
    print("Enter=stop here and watch, n=next URL, s=save, q=quit\n")

    url_index = 0
    while url_index < len(urls):
        url = urls[url_index]
        print(f"  Loading: {url[:70]}...")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_function("window.rawData || document.body?.innerText?.length > 500", timeout=20000)
            time.sleep(1.5)
        except Exception as e:
            print(f"  ERROR: {e}")
            url_index += 1
            continue

        print(f"  (Press Enter within {DWELL_SEC}s to stop here, or wait to auto-advance)")
        stopped = False
        for _ in range(DWELL_SEC * 10):
            try:
                cmd = cmd_queue.get_nowait()
                if cmd == "[stop]":
                    stopped = True
                    break
                if cmd == "n":
                    break
                if cmd == "q":
                    print("  Quitting.")
                    return
            except Empty:
                pass
            time.sleep(0.1)
        else:
            # Auto-advance: save base product (no variant)
            data = extract_product_data(page, debug=False)
            if data:
                _build_and_save_product(data, url, output_dir, variant_name=None)
                print(f"  [Auto] saved")
            url_index += 1
            continue

        if not stopped:
            url_index += 1
            continue

        print("  Stopped. Click variants, s=save, n=next URL.")

        while True:
            try:
                cmd = cmd_queue.get_nowait()
                if cmd == "q":
                    print("  Quitting.")
                    return
                if cmd == "n":
                    break
                if cmd == "s":
                    data = extract_product_data(page, debug=False)
                    if data:
                        snapshot = _get_variant_snapshot(page)
                        variant = (snapshot or {}).get("variant") or "default"
                        current_img = _get_current_main_image_url(page)
                        if current_img:
                            data["topGalleryUrl"] = current_img
                            data["gallery"] = [current_img] + [u for u in (data.get("gallery") or []) if u != current_img][:9]
                        _build_and_save_product(data, url, output_dir, variant_name=variant)
                        print(f"  [Keypress] saved variant {variant!r}")
                    continue
            except Empty:
                pass

            time.sleep(0.3)

        url_index += 1

    print("  No more URLs.")


def build_scraped_index(output_dir: Path) -> None:
    """Build index.json and README.md from products.json for quick overview."""
    from datetime import datetime

    products = _load_products(output_dir)
    if not products:
        return

    index_items = [
        {"name": p.get("name", ""), "price": p.get("price"), "temu_price": p.get("temu_price"), "goods_id": p.get("goods_id", ""), "variant": p.get("variant")}
        for p in products
    ]
    index = {
        "updated": datetime.now().isoformat(),
        "product_count": len(products),
        "products": index_items,
    }
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    lines = ["# Scraped Products\n", f"*{len(products)} products, updated {index['updated'][:10]}*\n\n"]
    lines.append("| Name | Price | Temu | Variant |\n")
    lines.append("|------|-------|------|--------|\n")
    for p in index_items:
        name = (p["name"][:50] + "..") if len(p["name"]) > 50 else p["name"]
        var = p.get("variant") or ""
        lines.append(f"| {name} | R{p['price']} | R{p['temu_price']} | {var} |\n")
    (output_dir / "README.md").write_text("".join(lines), encoding="utf-8")


def create_browser_context(browser, load_session: bool = True, proxy: dict | None = None):
    """Create context with optional saved Temu session (cookies for Gmail login).
    viewport=None uses the actual window size so the user can resize and go full screen."""
    opts = {
        "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "viewport": None,
        "locale": "en-ZA",
    }
    if load_session and SESSION_FILE.exists():
        opts["storage_state"] = str(SESSION_FILE)
    if proxy:
        opts["proxy"] = proxy
    return browser.new_context(**opts)


# Match java-mellow-products (working): simpler scripts, no webdriver mask
PREVENT_NEW_TAB_SCRIPT = """
(function() {
  if (!location.hostname.includes('temu.com')) return;
  window.open = function(url, target, features) {
    var u = (url && typeof url === 'string') ? url.trim() : '';
    if (u && u !== 'about:blank' && (u.startsWith('http') || u.startsWith('/'))) {
      window.location.href = u;
    }
    return null;
  };
  document.addEventListener('click', function(e) {
    var a = e.target.closest('a');
    if (!a || !a.href) return;
    var href = (a.getAttribute('href') || '').trim();
    if (!href || href === '#' || href.startsWith('javascript:')) return;
    if (a.target === '_blank' || a.getAttribute('target') === '_blank' || e.ctrlKey || e.metaKey) {
      e.preventDefault();
      e.stopPropagation();
      if (a.href && a.href !== 'about:blank') window.location.href = a.href;
      return false;
    }
  }, true);
  function stripBlankTarget() {
    try { document.querySelectorAll('a[target="_blank"]').forEach(function(el) { el.removeAttribute('target'); }); } catch(e) {}
  }
  if (document.body) { stripBlankTarget(); var obs = new MutationObserver(stripBlankTarget); obs.observe(document.body, { childList: true, subtree: true }); }
  else document.addEventListener('DOMContentLoaded', function() { stripBlankTarget(); var obs = new MutationObserver(stripBlankTarget); obs.observe(document.body, { childList: true, subtree: true }); });
})();
"""

FLOATING_BUTTON_SCRIPT = """
if (!location.hostname.includes('temu.com')) void 0;
else {
  function fireSave() {
    if (window._temuScraperSaveCooldown && Date.now() - window._temuScraperSaveCooldown < 2500) return;
    window._temuScraperSaveCooldown = Date.now();
    window.__temuScraperSaveTrigger = true;
    var bar = document.getElementById('temu-scraper-save-btn');
    var b = bar ? bar.querySelector('button') : null;
    if (b) {
      b.textContent = 'Saving...';
      setTimeout(function(){ b.textContent = 'Saved!'; }, 1200);
      setTimeout(function(){ b.textContent = 'Save product'; }, 2500);
    }
  }
  if (!window._temuScraperKbdBound) {
    window._temuScraperKbdBound = true;
    document.addEventListener('keydown', function(e) {
      if (e.ctrlKey && e.shiftKey && e.key === 'S') {
        e.preventDefault();
        fireSave();
      }
    }, true);
  }
  function loadPos() {
    try {
      var s = localStorage.getItem('scraper_btn_temu-scraper-save-btn');
      if (s) { var j = JSON.parse(s); return { x: j.x, y: j.y, right: j.right }; }
    } catch(e) {}
    return null;
  }
  function savePos(x, y, right) {
    try { localStorage.setItem('scraper_btn_temu-scraper-save-btn', JSON.stringify({ x: x, y: y, right: right })); } catch(e) {}
  }
  function ensureBtn() {
    if (!document.body) return;
    if (document.getElementById('temu-scraper-save-btn')) return;
    var pos = loadPos();
    var startRight = pos ? pos.right : true;
    var startY = pos && pos.y != null ? Math.max(0, Math.min(pos.y, window.innerHeight - 80)) : 80;
    var margin = 12;
    var bar = document.createElement('div');
    bar.id = 'temu-scraper-save-btn';
    bar.style.cssText = 'position:fixed!important;z-index:2147483647!important;background:#1a5!important;color:white!important;padding:8px 14px!important;font-family:sans-serif!important;font-size:14px!important;font-weight:bold!important;display:flex!important;align-items:center!important;gap:10px!important;box-shadow:0 2px 10px rgba(0,0,0,0.4)!important;border-radius:8px!important;cursor:grab!important;user-select:none!important;-webkit-user-select:none!important;';
    bar.style.top = startY + 'px';
    bar.style[startRight ? 'right' : 'left'] = margin + 'px';
    if (startRight) bar.style.left = 'auto'; else bar.style.right = 'auto';
    bar.innerHTML = '<span style="cursor:grab">⋮⋮</span><button style="padding:6px 16px!important;background:#fff!important;color:#1a5!important;border:none!important;border-radius:6px!important;cursor:pointer!important;font-size:13px!important;font-weight:bold!important;">Save product</button><span style="font-size:11px!important;font-weight:normal!important;">Ctrl+Shift+S</span>';
    var btn = bar.querySelector('button');
    btn.onclick = function(e) { e.stopPropagation(); };
    bar.onclick = function(e) {
      if (e.target === btn || btn.contains(e.target)) {
        try { fireSave(); } catch (err) { btn.textContent = 'Error'; setTimeout(function(){ btn.textContent = 'Save product'; }, 2000); }
      }
    };
    var drag = { active: false, startX: 0, startY: 0, startLeft: 0, startTop: 0 };
    bar.addEventListener('mousedown', function(e) {
      if (e.target === btn || btn.contains(e.target)) return;
      e.preventDefault();
      var r = bar.getBoundingClientRect();
      drag.active = true;
      drag.startX = e.clientX;
      drag.startY = e.clientY;
      drag.startLeft = r.left;
      drag.startTop = r.top;
      bar.style.cursor = 'grabbing';
    });
    bar.addEventListener('touchstart', function(e) {
      if (e.target === btn || btn.contains(e.target)) return;
      var t = e.touches[0], r = bar.getBoundingClientRect();
      drag.active = true;
      drag.startX = t.clientX;
      drag.startY = t.clientY;
      drag.startLeft = r.left;
      drag.startTop = r.top;
    }, { passive: true });
    function onMove(e) {
      if (!drag.active) return;
      var x = (e.touches ? e.touches[0].clientX : e.clientX) - drag.startX;
      var y = (e.touches ? e.touches[0].clientY : e.clientY) - drag.startY;
      var r = bar.getBoundingClientRect();
      var newLeft = Math.max(margin, Math.min(window.innerWidth - r.width - margin, drag.startLeft + x));
      var newTop = Math.max(0, Math.min(window.innerHeight - r.height, drag.startTop + y));
      bar.style.left = newLeft + 'px';
      bar.style.right = 'auto';
      bar.style.top = newTop + 'px';
    }
    function onUp(e) {
      if (!drag.active) return;
      drag.active = false;
      bar.style.cursor = 'grab';
      var rect = bar.getBoundingClientRect();
      var midX = rect.left + rect.width / 2;
      var snapRight = midX > window.innerWidth / 2;
      bar.style.left = snapRight ? 'auto' : margin + 'px';
      bar.style.right = snapRight ? margin + 'px' : 'auto';
      rect = bar.getBoundingClientRect();
      savePos(rect.left, rect.top, snapRight);
    }
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
    document.addEventListener('touchmove', onMove, { passive: true });
    document.addEventListener('touchend', onUp);
    document.body.appendChild(bar);
  }
  function scheduleAdd() {
    if (document.body) {
      ensureBtn();
      var obs = new MutationObserver(function() { if (!document.getElementById('temu-scraper-save-btn')) ensureBtn(); });
      obs.observe(document.body, { childList: true, subtree: true });
    } else {
      document.addEventListener('DOMContentLoaded', function() { scheduleAdd(); }, { once: true });
    }
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() { setTimeout(scheduleAdd, 100); }, { once: true });
  } else {
    setTimeout(scheduleAdd, 100);
  }
}
"""


def run_scrape_session(
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_options: dict | None = None,
) -> None:
    """
    Run Playwright scrape session in current thread. For web UI: run in a daemon thread.
    Uses persistent Chrome profile so Gmail/Google OAuth works (sessionStorage persists).
    Log in once; session persists in chrome_profile/.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    debug = os.environ.get("SCRAPER_DEBUG", "").lower() in ("1", "true", "yes")
    opts = scrape_options or {}
    proxy = {"server": opts["proxy_server"]} if opts.get("proxy_server") else None
    check_script = """
    () => {
        if (window.__temuScraperSaveTrigger) {
            window.__temuScraperSaveTrigger = false;
            return true;
        }
        return false;
    }
    """

    # Match java-mellow-products (working): regular launch + new_context, NOT persistent context
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=False)
        try:
            if SESSION_FILE.exists():
                context = create_browser_context(browser, load_session=True, proxy=proxy)
            else:
                context = create_browser_context(browser, load_session=False, proxy=proxy)
            context.add_init_script(PREVENT_NEW_TAB_SCRIPT)
            context.add_init_script(FLOATING_BUTTON_SCRIPT)
            page = context.new_page()
            if SESSION_FILE.exists():
                page.goto("https://www.temu.com/za/", wait_until="domcontentloaded", timeout=30000)
            else:
                page.goto("https://www.temu.com/za/login.html", wait_until="domcontentloaded", timeout=30000)
                print("  Log in to Temu via Gmail in the browser. Session persists in temu_session.json.")

            def close_blank_popup(new_page):
                try:
                    if new_page.url in ("about:blank", "") or "about:blank" in new_page.url:
                        new_page.close()
                except Exception:
                    pass

            context.on("page", close_blank_popup)
            try:
                page.evaluate("(function(){ " + PREVENT_NEW_TAB_SCRIPT + FLOATING_BUTTON_SCRIPT + " })()")
            except Exception as e:
                print(f"  [temu] Initial inject failed: {e}", flush=True)

            inject_count = 0
            print("  [temu] Loop started. Save: green bar at top or Ctrl+Shift+S", flush=True)
            while not stop_flag.is_set():
                inject_count += 1
                try:
                    for pg in context.pages:
                        try:
                            if pg.url and "temu.com" in pg.url and "about:blank" not in pg.url:
                                pg.evaluate("(function(){ " + FLOATING_BUTTON_SCRIPT + " })()")
                        except Exception:
                            pass
                except Exception:
                    pass
                try:
                    for pg in context.pages:
                        try:
                            if pg.evaluate(check_script):
                                if scrape_current_page(pg, output_dir, debug=debug):
                                    print(f"  Saved: {pg.url[:70]}...")
                                else:
                                    if re.search(r"-g-\d+\.html", pg.url):
                                        print("  Could not extract product data. Check debug_html/ for saved page.")
                                    else:
                                        print("  Not a product page. Open a product (URL with -g-XXX.html) first.")
                                break
                        except Exception as e:
                            if "Target" in str(e) and "closed" in str(e):
                                raise
                            pass
                    if save_session_flag.is_set():
                        context.storage_state(path=str(SESSION_FILE))
                        save_session_flag.clear()
                        print("  Session saved to temu_session.json (for headless use).")
                    time.sleep(0.3)
                except Exception as e:
                    if "Target" in str(e) and "closed" in str(e):
                        print("  Browser was closed.")
                        break
                    raise
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    build_scraped_index(output_dir)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scrape Temu products, save locally, optionally upload to API")
    parser.add_argument("--urls", default=str(Path(__file__).parent / "urls.txt"), help="File with Temu URLs (one per line)")
    parser.add_argument("--output-dir", "-o", default=str(Path(__file__).parent / "scraped"), help="Output directory for scraped data")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and save to folders (no API upload)")
    parser.add_argument("--upload", action="store_true", help="After scraping, upload from local folders to API")
    parser.add_argument("--headless", default=False, type=lambda x: str(x).lower() == "true", help="Run browser headless (Temu may block headless)")
    parser.add_argument("--debug", action="store_true", help="Print debug info when extraction fails")
    parser.add_argument("--save-session", action="store_true", help="Open browser, log in via Gmail, then save session for future runs")
    parser.add_argument("--list-categories", action="store_true", help="List categories for COMPANY_SLUG (requires --upload env vars)")
    parser.add_argument("--upload-to", default=None, help="Upload to specific slug or 'all' (overrides COMPANY_SLUG/COMPANY_SLUGS)")
    parser.add_argument("--variant-mode", "-v", action="store_true", help="Interactive: click variants, auto-save on change. Commands: b=start watching, s=save, n=next URL, q=quit")
    args = parser.parse_args()

    urls_path = Path(args.urls)
    if not urls_path.exists():
        print(f"Create {urls_path} and add Temu product URLs (one per line).")
        return 1

    urls = []
    for line in urls_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "temu.com" in line:
            urls.append(line)

    if not urls:
        print(f"No URLs found in {urls_path}. Add Temu product URLs, one per line.")
        return 1

    print(f"Found {len(urls)} URL(s) in {urls_path}")

    output_dir = Path(args.output_dir)

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
                    print(f"No categories found for {company_slug}. Create one in the admin first.")
                else:
                    print(f"Categories for {company_slug}:")
                    for c in items:
                        print(f"  {c.get('id')}  {c.get('name', '')} (slug: {c.get('slug', '')})")
            except Exception as e:
                print(f"Failed to list categories for {company_slug}: {e}")
        return 0

    if args.variant_mode:
        output_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=False)
            context = create_browser_context(browser)
            page = context.new_page()
            run_variant_capture_mode(page, output_dir, urls)
            browser.close()
        build_scraped_index(output_dir)
        return 0

    if args.save_session:
        print("Opening browser - log in to Temu via Gmail, then press Enter here when done.")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=False)
            context = create_browser_context(browser, load_session=False)
            page = context.new_page()
            page.goto("https://www.temu.com/za/login.html", wait_until="domcontentloaded", timeout=30000)
            input("  Log in via Gmail in the browser, then press Enter to save session... ")
            context.storage_state(path=str(SESSION_FILE))
            print(f"  Session saved to {SESSION_FILE}")
            browser.close()
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
        browser = p.chromium.launch(
            channel="chrome",
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"] if args.headless else [],
        )
        context = create_browser_context(browser)
        page = context.new_page()
        for i, url in enumerate(urls):
            print(f"[{i+1}/{len(urls)}]")
            scrape_url(page, url, output_dir, debug=args.debug)
            if i < len(urls) - 1:
                time.sleep(2)
        browser.close()

    build_scraped_index(output_dir)
    print(f"Done. Products saved to {output_dir}/")
    print("See index.json and README.md for overview. Edit product.json, then run --upload to push to API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
