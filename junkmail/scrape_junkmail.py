#!/usr/bin/env python3
"""
Junk Mail listing scraper — browse-and-save via real Chrome (CDP).

Uses junkmail/chrome_profile/ with remote debugging. Pass Cloudflare once in
that Chrome window; Playwright only attaches via CDP (does not launch automation).
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from junkmail.browser_utils import (
    CHROME_PROFILE,
    goto_junkmail,
    is_cdp_available,
    start_manual_chrome,
    wait_for_cloudflare_clearance,
)
from junkmail.cdp_fetch import open_junkmail_browser
from junkmail_crawler.parsers import extract_detail_images, is_cloudflare_challenge, normalize_junkmail_listing_url, parse_detail_page
from shared.config import load_scraper_config
from shared.playwright_utils import PAGE_LOAD_TIMEOUT
from shared.utils import clean_description, first_n_words, get_compare_at_price, image_prefix, remove_special_chars, truncate_name

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"

TIER_MULTIPLIERS = [
    (500, 1.35),
    (2000, 1.25),
    (10000, 1.18),
    (30000, 1.12),
    (100000, 1.08),
    (250000, 1.06),
    (float("inf"), 1.05),
]


def _tiers_from_config() -> list[tuple[float, float]]:
    cfg = load_scraper_config()
    tiers = (cfg.get("supplier_tiers") or {}).get("junkmail")
    if not tiers:
        return TIER_MULTIPLIERS
    parsed: list[tuple[float, float]] = []
    for entry in tiers:
        threshold = entry.get("threshold")
        mult = entry.get("multiplier")
        if mult is None:
            continue
        parsed.append((float(threshold) if threshold is not None else float("inf"), float(mult)))
    parsed.sort(key=lambda x: x[0])
    return parsed or TIER_MULTIPLIERS


def apply_junkmail_markup(junkmail_price: float) -> float:
    """Apply tiered markup from scraper_config.json. Returns sell price in ZAR."""
    cost = float(junkmail_price)
    for threshold, mult in _tiers_from_config():
        if cost < threshold:
            return round(cost * mult, 2)
    return round(cost * 1.05, 2)


def extract_ad_id(url: str) -> str | None:
    from junkmail_crawler.parsers import extract_ad_id_from_url

    return extract_ad_id_from_url(url)


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
    path = output_dir / PRODUCTS_FILE
    path.write_text(
        json.dumps({"products": products, "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    sync_urls_from_products(products, output_dir)


URLS_HEADER = """# Add Junk Mail listing URLs (one per line)
# Example: https://www.junkmail.co.za/office-and-business/gauteng/pretoria/listing-title/abc123...32hex

"""


def sync_urls_from_products(products: list, output_dir: Path) -> None:
    urls_path = output_dir.parent / "urls.txt"
    seen: set[str] = set()
    urls: list[str] = []
    for p in products:
        url = (p.get("url") or "").strip()
        if not url or "junkmail" not in url.lower():
            continue
        base = url.split("?")[0].strip()
        if base and base not in seen:
            seen.add(base)
            urls.append(base)
    urls_path.parent.mkdir(parents=True, exist_ok=True)
    urls_path.write_text(URLS_HEADER + "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")


def _build_and_save_junkmail_product(data: dict, url: str, output_dir: Path) -> dict | None:
    session = requests.Session()
    session.headers.update({"Accept": "image/*,*/*"})

    title = data.get("title") or "Unknown Listing"
    ad_id = data.get("ad_id") or extract_ad_id(url) or "unknown"
    price = data.get("price") or 0

    name = first_n_words(remove_special_chars(title), 5)
    short_desc = truncate_name(title, 150)
    img_prefix = image_prefix(title, 20)

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    image_urls = data.get("images") or []
    image_files: list[str] = []
    base_prefix = f"{img_prefix}_{ad_id}"
    for i, img_url in enumerate(image_urls[:10], 1):
        try:
            resp = session.get(img_url, timeout=15)
            resp.raise_for_status()
            ext = ".jpg"
            ct = resp.headers.get("content-type", "")
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            fname = f"{base_prefix}_{i:02d}{ext}"
            rel_path = f"{IMAGES_DIR}/{fname}"
            (images_dir / fname).write_bytes(resp.content)
            image_files.append(rel_path)
        except Exception as exc:
            print(f"  WARNING: Could not download image {i}: {exc}")

    sell_price = apply_junkmail_markup(price) if price else 0
    compare_at_price = get_compare_at_price(sell_price) if sell_price else None

    product_json = {
        "url": normalize_junkmail_listing_url(url) or url,
        "name": name,
        "description": clean_description(data.get("description") or title)[:2000],
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": float(price),
        "junkmail_price": price,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 1,
        "status": "active",
        "tags": ["vintage"],
        "ad_id": ad_id,
        "location": data.get("location"),
    }

    products = _load_products(output_dir)
    products = [p for p in products if (p.get("url") or "").split("?")[0] != url.split("?")[0]]
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def scrape_current_page(page, output_dir: Path) -> bool:
    """Scrape current Playwright page if it is a Junk Mail listing."""
    url = page.url
    if "junkmail" not in url.lower() or not extract_ad_id(url):
        return False
    try:
        html = page.content()
    except Exception:
        return False
    if is_cloudflare_challenge(html, page.title()):
        print("  Cloudflare challenge — complete verification in the Chrome window, then retry Save.")
        if wait_for_cloudflare_clearance(page, timeout_seconds=120, log=print):
            html = page.content()
        else:
            return False
    page_url = normalize_junkmail_listing_url(page.url) or url
    detail = parse_detail_page(html, page_url, "general")
    if not detail:
        return False
    detail["images"] = extract_detail_images(html)
    save_url = detail.get("url") or normalize_junkmail_listing_url(page_url)
    if not save_url:
        return False
    _build_and_save_junkmail_product(detail, save_url, output_dir)
    return True


def scrape_url(page, url: str, output_dir: Path, debug: bool = False) -> dict | None:
    """Scrape one Junk Mail URL from an open Playwright page."""
    print(f"  Scraping: {url[:80]}...")
    try:
        if not goto_junkmail(page, url, timeout_ms=PAGE_LOAD_TIMEOUT):
            print("  ERROR: Cloudflare not cleared. Run: python junkmail/setup_cloudflare.py")
            return None
    except Exception as exc:
        print(f"  ERROR: Failed to load page: {exc}")
        return None
    if scrape_current_page(page, output_dir):
        return {"ok": True}
    if debug:
        print("  ERROR: Could not extract listing (not a listing page or Cloudflare block)")
    return None


def fetch_current_pricing(url: str, product: dict | None = None) -> dict | None:
    """
    Fetch current price/cost from a Junk Mail listing URL (Verify all).

    Uses curl_cffi + cookies from junkmail/junkmail_session.json (export via
    python junkmail/setup_cloudflare.py). Returns None when Cloudflare blocks.
    """
    from shared.verify_utils import page_text_indicates_sold_out, page_text_indicates_unavailable

    page_url = normalize_junkmail_listing_url((url or "").strip()) or (url or "").strip()
    if not page_url or "junkmail" not in page_url.lower():
        return None

    html = None
    for attempt in range(2):
        try:
            from junkmail.http_session import build_requests_session, fetch_html

            session = build_requests_session()
            html = fetch_html(session, page_url, timeout=60)
            break
        except Exception:
            if attempt == 0 and is_cdp_available():
                try:
                    from junkmail.http_session import export_cookies_via_cdp

                    export_cookies_via_cdp()
                    continue
                except Exception:
                    pass
            return None

    if not html:
        return None

    if is_cloudflare_challenge(html):
        return None

    if page_text_indicates_unavailable(html):
        return {
            "price": None,
            "cost": None,
            "source_price": None,
            "valid": True,
            "in_stock": False,
            "unavailable": True,
        }

    detail = parse_detail_page(html, page_url, "general")
    if not detail or (not detail.get("title") and detail.get("price") is None):
        if page_text_indicates_sold_out(html):
            return {
                "price": None,
                "cost": None,
                "source_price": None,
                "valid": True,
                "in_stock": False,
                "unavailable": False,
            }
        return None

    price = detail.get("price") or 0
    if not price:
        if page_text_indicates_sold_out(html):
            return {
                "price": None,
                "cost": None,
                "source_price": None,
                "valid": True,
                "in_stock": False,
                "unavailable": False,
            }
        return None

    cost = float(price)
    sell_price = apply_junkmail_markup(cost)
    return {
        "price": sell_price,
        "cost": cost,
        "source_price": cost,
        "valid": True,
        "in_stock": True,
        "unavailable": False,
    }


def build_scraped_index(output_dir: Path) -> None:
    products = _load_products(output_dir)
    if not products:
        return
    index_items = [
        {
            "name": p.get("name", ""),
            "price": p.get("price"),
            "junkmail_price": p.get("junkmail_price"),
            "ad_id": p.get("ad_id", ""),
        }
        for p in products
    ]
    index = {"updated": datetime.now().isoformat(), "product_count": len(products), "products": index_items}
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")


PREVENT_NEW_TAB_SCRIPT = """
(function() {
  if (!location.hostname.includes('junkmail')) return;
  if (window._junkmailScraperPreventTabInit) return;
  window._junkmailScraperPreventTabInit = true;
  var _nativeOpen = window.open;
  window.open = function(url, target, features) {
    var u = (url && typeof url === 'string') ? url.trim() : '';
    if (u && u !== 'about:blank' && (u.startsWith('http') || u.startsWith('/'))) {
      window.location.href = u;
    }
    return null;
  };
  document.addEventListener('click', function(e) {
    if (e.target && e.target.closest && e.target.closest('#junkmail-scraper-save-btn')) return;
    var a = e.target.closest('a');
    if (!a || !a.href) return;
    var href = (a.getAttribute('href') || a.href || '').trim();
    if (!href || href === '#' || href.startsWith('javascript:')) return;
    if (a.target === '_blank' || a.getAttribute('target') === '_blank' || e.ctrlKey || e.metaKey) {
      e.preventDefault();
      e.stopPropagation();
      if (a.href && a.href !== 'about:blank') window.location.href = a.href;
      return false;
    }
  }, true);
})();
"""

FLOATING_BUTTON_SCRIPT = """
if (!location.hostname.includes('junkmail')) void 0;
else if (window._junkmailScraperFloatingInit) { void 0; }
else {
  window._junkmailScraperFloatingInit = true;
  function fireSave() {
    if (window._junkmailScraperSaveCooldown && Date.now() - window._junkmailScraperSaveCooldown < 2500) return;
    window._junkmailScraperSaveCooldown = Date.now();
    window.__junkmailScraperSaveTrigger = true;
    var bar = document.getElementById('junkmail-scraper-save-btn');
    var b = bar ? bar.querySelector('button') : null;
    if (b) {
      b.textContent = 'Saving...';
      setTimeout(function(){ b.textContent = 'Saved!'; }, 1200);
      setTimeout(function(){ b.textContent = 'Save product'; }, 2500);
    }
  }
  if (!window._junkmailScraperKbdBound) {
    window._junkmailScraperKbdBound = true;
    document.addEventListener('keydown', function(e) {
      if (e.ctrlKey && e.shiftKey && e.key === 'S') {
        e.preventDefault();
        fireSave();
      }
    }, true);
  }
  function ensureBtn() {
    if (!document.body) return;
    if (document.getElementById('junkmail-scraper-save-btn')) return;
    var bar = document.createElement('div');
    bar.id = 'junkmail-scraper-save-btn';
    bar.style.cssText = 'position:fixed!important;z-index:2147483647!important;top:80px!important;right:12px!important;background:#2a7!important;color:white!important;padding:8px 14px!important;font-family:sans-serif!important;font-size:14px!important;font-weight:bold!important;display:flex!important;align-items:center!important;gap:10px!important;box-shadow:0 2px 10px rgba(0,0,0,0.4)!important;border-radius:8px!important;';
    bar.innerHTML = '<button type="button" style="padding:6px 16px!important;background:#fff!important;color:#2a7!important;border:none!important;border-radius:6px!important;cursor:pointer!important;font-size:13px!important;font-weight:bold!important;">Save product</button><span style="font-size:11px!important;font-weight:normal!important;">Ctrl+Shift+S</span>';
    bar.querySelector('button').onclick = function(e) { e.stopPropagation(); fireSave(); };
    document.body.appendChild(bar);
  }
  if (document.body) ensureBtn();
  else document.addEventListener('DOMContentLoaded', ensureBtn, { once: true });
  new MutationObserver(ensureBtn).observe(document.documentElement, { childList: true, subtree: true });
}
"""


def run_scrape_session(
    output_dir: Path,
    stop_flag: threading.Event,
    save_session_flag: threading.Event,
    scrape_options: dict | None = None,
) -> None:
    """
    Browse-and-save via real Chrome (CDP). Playwright-launched Chrome re-triggers
    Cloudflare; we attach to normal Chrome on port 9222 instead.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    check_script = """
    () => {
        if (window.__junkmailScraperSaveTrigger) {
            window.__junkmailScraperSaveTrigger = false;
            return true;
        }
        return false;
    }
    """

    jm = None
    try:
        if not is_cdp_available():
            print("  Opening Junk Mail Chrome (real browser — pass Cloudflare here)...")
            start_manual_chrome()
        jm = open_junkmail_browser(log=print, auto_start_chrome=True)
        browser = jm._browser

        for context in browser.contexts:
            try:
                context.add_init_script(PREVENT_NEW_TAB_SCRIPT)
                context.add_init_script(FLOATING_BUTTON_SCRIPT)
            except Exception:
                pass

        def close_blank_popup(new_page):
            try:
                if new_page.url in ("about:blank", "") or "about:blank" in (new_page.url or ""):
                    new_page.close()
            except Exception:
                pass

        for context in browser.contexts:
            try:
                context.on("page", close_blank_popup)
            except Exception:
                pass

        print("  Junk Mail Chrome ready.")
        print(f"  Profile: {CHROME_PROFILE}")
        print("  Browse listings. On a listing page, click Save product or press Ctrl+Shift+S.")

        while not stop_flag.is_set():
            try:
                for context in browser.contexts:
                    for pg in context.pages:
                        try:
                            if pg.url and "junkmail" in pg.url.lower() and "about:blank" not in pg.url:
                                pg.evaluate("(function(){ " + FLOATING_BUTTON_SCRIPT + " })()")
                        except Exception:
                            pass
            except Exception:
                pass

            try:
                for context in browser.contexts:
                    for pg in context.pages:
                        try:
                            if pg.url and "junkmail" in pg.url.lower() and "about:blank" not in pg.url:
                                if pg.evaluate(check_script):
                                    if scrape_current_page(pg, output_dir):
                                        print(f"  Saved: {pg.url[:70]}...")
                                    elif extract_ad_id(pg.url):
                                        print("  Could not extract listing (Cloudflare or parse error).")
                                    else:
                                        print("  Not a listing page. Open a Junk Mail listing first.")
                                    break
                        except Exception:
                            pass
            except Exception:
                pass

            if save_session_flag.is_set():
                save_session_flag.clear()
                try:
                    from junkmail.http_session import export_cookies_via_cdp

                    export_cookies_via_cdp()
                    print("  Session cookies saved to junkmail_session.json")
                except Exception as exc:
                    print(f"  Cookie save failed: {exc}")

            time.sleep(0.55)
    except Exception as exc:
        print(f"  ERROR: {exc}")
        print("  Run: python junkmail/setup_cloudflare.py — pass Cloudflare in real Chrome, then retry.")
    finally:
        if jm is not None:
            jm.close()
    build_scraped_index(output_dir)
