#!/usr/bin/env python3
"""
Game product scraper - session-based browse and save.
Extracts product data from Game (SAP Commerce) product pages.
"""
import argparse
import json
import re
import sys
import threading
import time
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
    get_compare_at_price,
    clean_description,
    first_n_words,
    image_prefix,
    remove_special_chars,
    truncate_name,
)

SESSION_FILE = Path(__file__).parent / "game_session.json"

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"

URLS_HEADER = """# Game product URLs (one per line)
# Example: https://www.game.co.za/Electronics-Entertainment/Television/TVs/p/000000000850037087

"""


def extract_item_id(url: str) -> str | None:
    """Extract product ID from Game URL: /p/000000000850037087"""
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    m = re.search(r"/p/(\d+)", path)
    return m.group(1) if m else None


def extract_product_data(page, debug: bool = False) -> dict | None:
    """Extract product data from Game page via DOM."""
    try:
        data = page.evaluate("""
            async () => {
                const imageKey = (url) => {
                    const clean = (url || '').trim();
                    if (!clean) return '';
                    // Game media endpoints often use ?context=... as the actual image identity.
                    if (clean.includes('/medias/') && clean.includes('context=')) {
                        return clean.split('#')[0];
                    }
                    return clean.split('?')[0].split('#')[0];
                };

                const titleEl = document.querySelector('h1[role="heading"]') || document.querySelector('h1') || document.querySelector('[itemprop="name"]');
                const title = titleEl ? (titleEl.textContent || titleEl.innerText || '').trim() : null;

                const priceEl = document.querySelector('.r-g3a1by.r-1x35g6') || document.querySelector('[class*="r-1x35g6"]') || document.querySelector('.r-g3a1by');
                let priceText = priceEl ? (priceEl.textContent || priceEl.innerText || '').trim() : '';
                const priceMatch = priceText.match(/R\\s*([\\d.,]+)/i) || priceText.match(/([\\d.,]+)/);
                const price = priceMatch ? parseFloat(priceMatch[1].replace(/[,\\s]/g, '')) : null;

                const gallery = [];
                const seen = new Set();
                const productIdMatch = (window.location.pathname || '').match(/\\/p\\/(\\d+)/);
                const productId = productIdMatch ? productIdMatch[1] : '';
                let detectedProductId = null;

                const isRecentlyViewedNode = (node) => {
                    if (!node || !node.closest) return false;
                    return !!node.closest('.ins-inline-versus-main-wrapper');
                };

                const addImage = (raw, sourceNode = null) => {
                    const src = (raw || '').trim();
                    if (!src || src.startsWith('data:')) return;
                    if (isRecentlyViewedNode(sourceNode)) return;
                    if (src.includes('pixel') || src.includes('track') || src.includes('analytics') || src.includes('doubleclick')) return;
                    let full = src.startsWith('//') ? 'https:' + src : (src.startsWith('/') ? 'https://www.game.co.za' + src : src);
                    // Game thumbnail URLs are smaller derivatives; force product-size media URL.
                    if (full.includes('/medias/Default-Thumbnail-')) {
                        full = full.replace('/medias/Default-Thumbnail-', '/medias/Default-Product-');
                    }
                    const key = imageKey(full);
                    if (key && !seen.has(key)) { seen.add(key); gallery.push(full); }
                };

                const captureProductIdFromText = (txt) => {
                    if (!txt || detectedProductId) return;
                    const m = String(txt).match(/media_(\\d+)_/i);
                    if (m && m[1]) detectedProductId = m[1];
                };

                const addMainImage = () => {
                    const mainImg = document.querySelector('[data-testid="click_event_component"] img[src], [data-testid="click_event_component"] img[data-src]');
                    if (!mainImg) return;
                    captureProductIdFromText(mainImg.getAttribute('alt'));
                    addImage(mainImg.getAttribute('src') || mainImg.getAttribute('data-src') || '', mainImg);
                };

                // Highest priority: image URLs inside the PDP gallery container.
                const pdpGallery = document.querySelector('[data-testid="pdpImageContainerScroll"]');
                if (pdpGallery) {
                    // Capture currently selected main image first (full-size).
                    addMainImage();

                    // Click each product thumbnail to force the full-size image to load, then capture main image URL.
                    const thumbNodes = pdpGallery.querySelectorAll('[aria-label*="media_"], [role="button"][aria-label*="media_"], img[alt*="media_"]');
                    for (const node of thumbNodes) {
                        captureProductIdFromText(node.getAttribute('aria-label'));
                        if (node.tagName === 'IMG') captureProductIdFromText(node.getAttribute('alt'));
                        try {
                            if (typeof node.click === 'function') node.click();
                        } catch (e) {}
                        await new Promise((resolve) => setTimeout(resolve, 140));
                        addMainImage();
                    }
                }

                if (gallery.length > 0) {
                    const descEl = document.querySelector('[itemprop="description"]') || document.querySelector('.product-description') || document.querySelector('[data-testid*="description"]');
                    const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;
                    return { goodsName: title, salePrice: price, gallery, desc, goodsId: detectedProductId || productId || null };
                }

                // Prefer product-scoped media nodes first (prevents unrelated carousel images).
                if (productId) {
                    const mediaNodes = document.querySelectorAll(`[aria-label*="media_${productId}_"]`);
                    for (const node of mediaNodes) {
                        captureProductIdFromText(node.getAttribute('aria-label'));
                        const img = node.tagName === 'IMG' ? node : node.querySelector('img');
                        if (img) {
                            captureProductIdFromText(img.getAttribute('alt'));
                            addImage(img.getAttribute('src') || img.getAttribute('data-src') || '', img);
                        }

                        const styleNode = node.matches('[style*="background-image"]') ? node : node.querySelector('[style*="background-image"]');
                        if (styleNode) {
                            const style = (styleNode.getAttribute('style') || '').trim();
                            const m = style.match(/background-image\\s*:\\s*url\\((['"]?)(.*?)\\1\\)/i);
                            if (m && m[2]) addImage(m[2], styleNode);
                        }
                    }

                    const altImgs = document.querySelectorAll(`img[alt*="media_${productId}_"]`);
                    for (const img of altImgs) {
                        captureProductIdFromText(img.getAttribute('alt'));
                        addImage(img.getAttribute('src') || img.getAttribute('data-src') || '', img);
                    }
                }

                if (gallery.length > 0) {
                    const descEl = document.querySelector('[itemprop="description"]') || document.querySelector('.product-description') || document.querySelector('[data-testid*="description"]');
                    const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;
                    return { goodsName: title, salePrice: price, gallery, desc, goodsId: detectedProductId || productId || null };
                }

                const descEl = document.querySelector('[itemprop="description"]') || document.querySelector('.product-description') || document.querySelector('[data-testid*="description"]');
                const desc = descEl ? (descEl.textContent || descEl.innerText || '').trim() : null;

                return { goodsName: title, salePrice: price, gallery, desc, goodsId: detectedProductId || productId || null };
            }
        """)
        if data and (data.get("goodsName") or data.get("salePrice") is not None):
            return data
    except Exception as e:
        if debug:
            print(f"  DEBUG: extraction error: {e}")

    try:
        content = page.content()
        # Remove "Recently Viewed" recommendation widget so its images are never scraped.
        content = re.sub(
            r'<div class="ins-inline-versus-main-wrapper"[\s\S]*?</ul>\s*</div>[\s\S]*?</div>\s*</div>',
            "",
            content,
            flags=re.I,
        )
        m = re.search(r'<title>([^<|]+)', content)
        title = m.group(1).strip() if m else None
        if title and "|" in title:
            title = title.split("|")[0].strip()
        m = re.search(r'"name":\s*"([^"]+)"', content)
        if m and "IndividualProduct" in content:
            title = m.group(1).strip()
        m = re.search(r'R\s*([\d,]+\.?\d*)', content)
        price = None
        for pm in re.finditer(r'class="[^"]*r-g3a1by[^"]*"[^>]*>R\s*([\d,]+\.?\d*)', content):
            price = float(pm.group(1).replace(",", ""))
            break
        if price is None:
            pm = re.search(r'content_price[^"]*"([\d.]+)"', content)
            if pm:
                price = float(pm.group(1))
        if price is None:
            pm = re.search(r'parseFloat\(a\.items\[0\]\.price\)', content)
            if pm:
                for m in re.finditer(r'"price"\s*:\s*"([\d.]+)"', content):
                    price = float(m.group(1))
                    break
        gallery = []
        seen = set()

        def _clean_url(raw: str) -> str:
            u = (raw or "").strip()
            u = u.replace("&quot;", "").replace("\\u0026", "&")
            return u.rstrip(");").strip()
        pid = extract_item_id(page.url or "") or ""
        # Strict regex fallback: only capture media URLs tied to this product id.
        if pid:
            alt_pattern = re.compile(
                rf'alt="media_{re.escape(pid)}_[^"]*?"[^>]*?src="([^"]+)"',
                re.I,
            )
            for mm in alt_pattern.finditer(content):
                u = _clean_url(mm.group(1) or "")
                if not u:
                    continue
                key = u.split("#")[0] if "/medias/" in u and "context=" in u else u.split("?")[0]
                if key and key not in seen:
                    seen.add(key)
                    gallery.append(u)

            # Main product image can appear outside the thumbnail list.
            main_pattern = re.compile(
                rf'src="([^"]+)"[^>]*alt="media_{re.escape(pid)}_[^"]*Default-Product[^"]*"',
                re.I,
            )
            for mm in main_pattern.finditer(content):
                u = _clean_url(mm.group(1) or "")
                if not u:
                    continue
                key = u.split("#")[0] if "/medias/" in u and "context=" in u else u.split("?")[0]
                if key and key not in seen:
                    seen.add(key)
                    gallery.append(u)

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
        if not url or "game.co.za" not in url:
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
    base_prefix = f"{image_prefix(title, 20)}_{re.sub(r'[^a-zA-Z0-9_-]', '_', str(goods_id)[:20])}"
    for i, img_url in enumerate(image_urls, 1):
        try:
            if not img_url.startswith("http"):
                img_url = "https:" + img_url if img_url.startswith("//") else "https://www.game.co.za" + img_url
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
        print("  WARNING: No images downloaded - using placeholder")
        try:
            resp = requests.get("https://via.placeholder.com/400x400?text=No+Image", timeout=10)
            resp.raise_for_status()
            fname = f"{base_prefix}_01.jpg"
            (images_dir / fname).write_bytes(resp.content)
            image_files = [f"{IMAGES_DIR}/{fname}"]
        except Exception:
            return None

    sell_price = apply_tiered_markup(sale_price_cents, "game")
    # Game is local supplier: cost must be exactly source price (no uplift/add-on).
    cost = round(float(sale_price_zar or 0), 2)
    compare_at_price = get_compare_at_price(sell_price)

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        "game_price": sale_price_zar,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": ["game"],
        "goods_id": goods_id,
    }
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_current_page(page, output_dir: Path) -> bool:
    """Extract product data from current page and save. Returns True on success."""
    url = page.url or ""
    if "game.co.za" not in url or "/p/" not in url:
        return False
    data = extract_product_data(page, debug=False)
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        return False
    url_pid = extract_item_id(url)
    data_pid = (data.get("goodsId") or "").strip() if data.get("goodsId") else ""
    if url_pid and data_pid and url_pid != data_pid:
        # Prevent saving stale DOM from a previous product during in-page transitions.
        print(f"  WARNING: Product mismatch detected (url={url_pid}, extracted={data_pid}). Skip save; click Save again once page is fully loaded.")
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
            "game_price": p.get("game_price"),
            "goods_id": p.get("goods_id", ""),
        }
        for p in products
    ]
    index_path = output_dir / "index.json"
    index_path.write_text(json.dumps({"products": index_items}, indent=2), encoding="utf-8")


def fetch_current_pricing(url: str) -> dict | None:
    """
    Fetch current price/cost from Game URL. No persistence.
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
            sell_price = apply_tiered_markup(sale_price_cents, "game")
            # Game is local supplier: cost must be exactly source price (no uplift/add-on).
            cost = round(float(sale_price_zar or 0), 2)
            source_price = sale_price_zar if sale_price_zar is not None else (sale_price_cents / 100)
            return {
                "price": round(sell_price, 2),
                "cost": round(cost, 2),
                "source_price": round(source_price, 2),
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
    """Run Playwright scrape session for Game."""
    config = GenericScraperConfig(
        base_url="https://www.game.co.za/",
        login_url="https://www.game.co.za/",
        session_file=SESSION_FILE,
        hostname_pattern="game.co.za",
        supplier_slug="game",
        use_persistent_context=True,
        persistent_user_data_dir=Path(__file__).parent / "chrome_profile",
        allow_popup_for_hosts=("accounts.google", "firebaseapp"),
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
    parser = argparse.ArgumentParser(description="Scrape Game products (browse and save)")
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
