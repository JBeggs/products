#!/usr/bin/env python3
"""
Gumtree listing scraper - reads URLs from urls.txt, scrapes listing data,
saves to local folders (for review/debugging), optionally uploads to Django API.
Supports browse-and-save mode via Playwright (floating Save button).
"""
import argparse
import html
import json
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Add products root for shared imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, PAGE_LOAD_TIMEOUT
from shared.utils import clean_description, first_n_words, get_compare_at_price, image_prefix, remove_special_chars, slugify, truncate_name
from shared.upload import get_auth_token, get_or_create_category, upload_product
from shared.config import get_category_for_slug, resolve_upload_targets

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SESSION_FILE = Path(__file__).parent / "gumtree_session.json"
CHROME_PROFILE = Path(__file__).parent / "chrome_profile"

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# Gumtree tiered markup (different from Temu)
TIER_MULTIPLIERS = [
    (30, 2.0), (99, 2.0), (199, 2.0), (2000.01, 1.20), (10000.01, 1.08), (30000.01, 1.06), (float("inf"), 1.05),
]


def apply_gumtree_markup(gumtree_price: float) -> float:
    """Apply tiered markup. Returns sell price in ZAR."""
    cost = float(gumtree_price)
    for threshold, mult in TIER_MULTIPLIERS:
        if cost < threshold:
            return round(cost * mult, 2)
    return round(cost * 1.35, 2)


def extract_ad_id(url: str) -> str | None:
    """Extract ad ID from Gumtree URL (typically at end of path)."""
    parsed = urlparse(url)
    path = parsed.path or ""
    # Match numeric ID at end: /listing-title/10013494682001010915604009
    match = re.search(r"/(\d{15,})\s*$", path.rstrip("/"))
    if match:
        return match.group(1)
    return None


def extract_category_from_url(url: str) -> tuple[str | None, str | None]:
    """Extract category from Gumtree URL path. e.g. /a-cars-bakkies/diep-river/... -> slug 'cars-bakkies', name 'Cars and Bakkies'."""
    parsed = urlparse(url)
    path = (parsed.path or "").strip("/")
    parts = path.split("/")
    if not parts:
        return None, None
    first = parts[0]
    if first.startswith("a-"):
        slug = first[2:]  # Remove "a-" prefix
    else:
        slug = first
    if not slug:
        return None, None
    # Convert slug to display name: "cars-bakkies" -> "Cars and Bakkies"
    words = slug.replace("-", " and ").split()
    name = " ".join(w.capitalize() if w != "and" else w for w in words)
    return name, slug


def extract_listing_data(html: str, url: str, debug: bool = False) -> dict | None:
    """Extract listing data from Gumtree HTML. Uses requests (Playwright blocked by Gumtree)."""
    title = None
    desc = None
    price = None
    location = None
    images = []

    # Title: <h1>
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if m:
        title = m.group(1).strip()

    # Price: "price": 62000 (numeric) or "amount": "62000"
    m = re.search(r'"price"\s*:\s*(\d+)', html)
    if m:
        price = int(m.group(1))
    if price is None:
        m = re.search(r'"amount"\s*:\s*"?(\d+)"?', html)
        if m:
            price = int(m.group(1))

    # Description: description-content div
    m = re.search(r'class="description-content"[^>]*>([\s\S]*?)</div>\s*</div>\s*</div>', html)
    if m:
        raw = m.group(1)
        desc = re.sub(r"<[^>]+>", " ", raw)
        desc = re.sub(r"\s+", " ", desc).strip()[:2000]

    # Location: General Details > Location links
    m = re.search(r"Location[^<]*</[^>]+>[^<]*<a[^>]*>([^<]+)</a>", html)
    if m:
        location = m.group(1).strip()

    # Images: gms.gumtree.co.za/v2/images/za_ads_* - exclude related-items section
    related_start = html.find('<div class="related-items">')
    html_for_images = html[:related_start] if related_start >= 0 else html
    seen = set()
    for m in re.finditer(r"gms\.gumtree\.co\.za/v2/images/[^\"\s<>]+", html_for_images):
        u = m.group(0)
        if not u.startswith("http"):
            u = "https://" + u
        base = u.split("?")[0]
        if base not in seen and "za_ads_" in u:
            seen.add(base)
            u = u.replace("size=s", "size=l") if "size=s" in u else u
            images.append(u)

    if title or desc:
        return {
            "title": title,
            "desc": desc,
            "price": price,
            "location": location,
            "images": images[:10],
        }
    if debug:
        print(f"  DEBUG: title={title!r}, desc={bool(desc)}, price={price}, images={len(images)}")
    return None


PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"


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
    """Save products to products.json and sync urls.txt."""
    from datetime import datetime
    path = output_dir / PRODUCTS_FILE
    path.write_text(
        json.dumps({"products": products, "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    sync_urls_from_products(products, output_dir)


URLS_HEADER = """# Add Gumtree listing URLs (one per line)
# Example: https://www.gumtree.co.za/a-cars-bakkies/diep-river/listing-title/10013494682001010915604009

"""


def sync_urls_from_products(products: list, output_dir: Path) -> None:
    """Rebuild urls.txt from products list. Preserves product order, dedupes by base URL."""
    urls_path = output_dir.parent / "urls.txt"
    seen = set()
    urls = []
    for p in products:
        url = (p.get("url") or "").strip()
        if not url or "gumtree" not in url.lower():
            continue
        base = url.split("?")[0].strip()
        if base and base not in seen:
            seen.add(base)
            urls.append(base)
    urls_path.parent.mkdir(parents=True, exist_ok=True)
    urls_path.write_text(URLS_HEADER + "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")


def _build_and_save_gumtree_product(data: dict, url: str, output_dir: Path, session: requests.Session | None = None) -> dict | None:
    """Build product dict from extracted data and append to products.json. Downloads images via session."""
    if session is None:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)

    title = data.get("title") or "Unknown Listing"
    ad_id = data.get("ad_id") or extract_ad_id(url) or "unknown"
    price = data.get("price") or 0

    name = first_n_words(remove_special_chars(title), 5)
    short_desc = truncate_name(title, 150)
    img_prefix = image_prefix(title, 20)

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    image_urls = data.get("images") or []
    image_files = []
    base_prefix = f"{img_prefix}_{ad_id}"
    for i, img_url in enumerate(image_urls[:10], 1):
        try:
            resp = session.get(img_url, timeout=15)
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

    sell_price = apply_gumtree_markup(price) if price else 0
    compare_at_price = get_compare_at_price(sell_price) if sell_price else None

    product_json = {
        "url": url,
        "name": name,
        "description": clean_description(data.get("desc") or title)[:2000],
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": float(price),
        "gumtree_price": price,
        "images": image_files,
        "variants": [],
        "in_stock": True,
        "stock_quantity": 1,
        "status": "active",
        "tags": ["vintage"],
        "ad_id": ad_id,
        "location": data.get("location"),
    }
    if data.get("category_name") and data.get("category_slug"):
        product_json["category_name"] = data["category_name"]
        product_json["category_slug"] = data["category_slug"]

    products = _load_products(output_dir)
    products.append(product_json)
    _save_products(output_dir, products)
    return product_json


def fetch_current_pricing(url: str) -> dict | None:
    """
    Fetch current price/cost from Gumtree URL. No persistence.
    Returns {price, cost, source_price, valid: True} or None if invalid/blocked.
    """
    try:
        session = requests.Session()
        session.headers.update(DEFAULT_HEADERS)
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return None

    if "The request is blocked" in html or "Service unavailable" in html:
        return None

    data = extract_listing_data(html, url, debug=False)
    if not data or (not data.get("title") and data.get("price") is None):
        return None

    price = data.get("price") or 0
    if not price:
        return None

    sell_price = apply_gumtree_markup(price)
    cost = float(price)
    gumtree_price = price
    return {
        "price": round(sell_price, 2),
        "cost": round(cost, 2),
        "source_price": round(gumtree_price, 2),
        "valid": True,
    }


def scrape_url(session: requests.Session, url: str, output_dir: Path, debug: bool = False) -> dict | None:
    """Scrape one Gumtree URL and append to products.json. Images in output_dir/images/."""
    print(f"  Scraping: {url[:80]}...")
    try:
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"  ERROR: Failed to load page: {e}")
        return None

    if "The request is blocked" in html or "Service unavailable" in html:
        print(f"  ERROR: Page blocked by Gumtree")
        return None

    data = extract_listing_data(html, url, debug=debug)
    if not data:
        print("  ERROR: Could not extract listing data")
        return None

    category_name, category_slug = extract_category_from_url(url)
    if category_name and category_slug:
        data["category_name"] = category_name
        data["category_slug"] = category_slug

    product_json = _build_and_save_gumtree_product(data, url, output_dir, session=session)
    if product_json:
        print(f"  Saved to {PRODUCTS_FILE}")
    return {"data": product_json} if product_json else None


def scrape_current_page(page, output_dir: Path) -> bool:
    """Scrape current page from Playwright. Returns True if saved. URL must be a Gumtree listing page."""
    url = page.url
    if "gumtree" not in url.lower() or not extract_ad_id(url):
        return False
    try:
        html = page.content()
    except Exception:
        return False
    if "The request is blocked" in html or "Service unavailable" in html:
        return False
    data = extract_listing_data(html, url, debug=False)
    if not data:
        return False
    category_name, category_slug = extract_category_from_url(url)
    if category_name and category_slug:
        data["category_name"] = category_name
        data["category_slug"] = category_slug
    _build_and_save_gumtree_product(data, url, output_dir, session=None)
    return True


def build_scraped_index(output_dir: Path) -> None:
    """Build index.json and README.md from products.json."""
    from datetime import datetime

    products = _load_products(output_dir)
    if not products:
        return

    index_items = [{"name": p.get("name", ""), "price": p.get("price"), "gumtree_price": p.get("gumtree_price"), "ad_id": p.get("ad_id", "")} for p in products]
    index = {"updated": datetime.now().isoformat(), "product_count": len(products), "products": index_items}
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")

    lines = ["# Scraped Gumtree Listings\n", f"*{len(products)} listings, updated {index['updated'][:10]}*\n\n"]
    lines.append("| Name | Price |\n")
    lines.append("|------|-------|\n")
    for p in index_items:
        name = (p["name"][:50] + "..") if len(p["name"]) > 50 else p["name"]
        lines.append(f"| {name} | R{p.get('price', 'N/A')} |\n")
    (output_dir / "README.md").write_text("".join(lines), encoding="utf-8")


def create_browser_context(browser, load_session: bool = True):
    """Create context with optional saved Gumtree session.
    viewport=None uses the actual window size so the user can resize and go full screen."""
    opts = {
        "user_agent": USER_AGENT,
        "viewport": None,
        "locale": "en-ZA",
    }
    if load_session and SESSION_FILE.exists():
        opts["storage_state"] = str(SESSION_FILE)
    return browser.new_context(**opts)


# Allow Google OAuth popups (Gmail login) - otherwise Google blocks with "not secure"
PREVENT_NEW_TAB_SCRIPT = """
(function() {
  if (!location.hostname.includes('gumtree')) return;
  function allowPopup(u) {
    if (!u) return false;
    var l = (u + '').toLowerCase();
    return l.indexOf('accounts.google') >= 0 || l.indexOf('google.com') >= 0 || l.indexOf('firebaseapp') >= 0;
  }
  var _nativeOpen = window.open;
  window.open = function(url, target, features) {
    var u = (url && typeof url === 'string') ? url.trim() : '';
    if (allowPopup(u)) return _nativeOpen ? _nativeOpen.apply(this, arguments) : null;
    if (u && u !== 'about:blank' && (u.startsWith('http') || u.startsWith('/'))) {
      window.location.href = u;
    }
    return null;
  };
  document.addEventListener('click', function(e) {
    var a = e.target.closest('a');
    if (!a || !a.href) return;
    var href = (a.getAttribute('href') || a.href || '').trim();
    if (!href || href === '#' || href.startsWith('javascript:')) return;
    if (a.target === '_blank' || a.getAttribute('target') === '_blank' || e.ctrlKey || e.metaKey) {
      if (allowPopup(href)) return;
      e.preventDefault();
      e.stopPropagation();
      if (a.href && a.href !== 'about:blank') window.location.href = a.href;
      return false;
    }
  }, true);
  function stripBlankTarget() {
    try { document.querySelectorAll('a[target="_blank"]').forEach(function(el) {
      if (el.href && allowPopup(el.href)) return;
      el.removeAttribute('target');
    }); } catch(e) {}
  }
  if (document.body) { stripBlankTarget(); var obs = new MutationObserver(stripBlankTarget); obs.observe(document.body, { childList: true, subtree: true }); }
  else document.addEventListener('DOMContentLoaded', function() { stripBlankTarget(); var obs = new MutationObserver(stripBlankTarget); obs.observe(document.body, { childList: true, subtree: true }); });
})();
"""

FLOATING_BUTTON_SCRIPT = """
if (!location.hostname.includes('gumtree')) void 0;
else {
  function addBtn() {
    if (!document.body || document.getElementById('gumtree-scraper-save-btn')) return;
    const btn = document.createElement('button');
    btn.id = 'gumtree-scraper-save-btn';
    btn.textContent = 'Save product';
    btn.style.cssText = 'position:fixed;bottom:20px;right:20px;z-index:99999;padding:8px 16px;background:#2a7;color:white;border:none;border-radius:6px;cursor:pointer;font-size:14px;box-shadow:0 2px 8px rgba(0,0,0,0.3)';
    btn.onclick = function() {
      try {
        window.__gumtreeScraperSaveTrigger = true;
        btn.textContent = 'Saving...';
        setTimeout(function(){ btn.textContent = 'Save product'; }, 1500);
      } catch (e) { btn.textContent = 'Error'; setTimeout(function(){ btn.textContent = 'Save product'; }, 2000); }
    };
    document.body.appendChild(btn);
  }
  if (document.body) addBtn();
  else document.addEventListener('DOMContentLoaded', addBtn);
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
    Uses persistent Chrome profile so Gmail/Google OAuth works (avoids "not secure" block).
    Log in once; session persists in chrome_profile/.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    opts = scrape_options or {}
    proxy = {"server": opts["proxy_server"]} if opts.get("proxy_server") else None
    check_script = """
    () => {
        if (window.__gumtreeScraperSaveTrigger) {
            window.__gumtreeScraperSaveTrigger = false;
            return true;
        }
        return false;
    }
    """

    launch_opts = {
        "user_data_dir": str(CHROME_PROFILE),
        "headless": False,
        "args": CHROMIUM_PERFORMANCE_ARGS + ["--disable-blink-features=AutomationControlled"],
        "user_agent": USER_AGENT,
        "viewport": None,
        "locale": "en-ZA",
    }
    if proxy:
        launch_opts["proxy"] = proxy
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**launch_opts)
        try:
            context.add_init_script(PREVENT_NEW_TAB_SCRIPT)
            context.add_init_script(FLOATING_BUTTON_SCRIPT)
            pages = context.pages
            if pages:
                page = pages[0]
                page.goto("https://www.gumtree.co.za/", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
            else:
                page = context.new_page()
                page.goto("https://www.gumtree.co.za/", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                print("  Log in to Gumtree via Gmail in the browser. Session persists in chrome_profile/.")

            def close_blank_popup(new_page):
                try:
                    if new_page.url in ("about:blank", "") or "about:blank" in new_page.url:
                        new_page.close()
                except Exception:
                    pass

            context.on("page", close_blank_popup)
            try:
                page.evaluate("(function(){ " + PREVENT_NEW_TAB_SCRIPT + FLOATING_BUTTON_SCRIPT + " })()")
            except Exception:
                pass

            while not stop_flag.is_set():
                for pg in context.pages:
                    try:
                        if pg.evaluate(check_script):
                            if scrape_current_page(pg, output_dir):
                                print(f"  Saved: {pg.url[:70]}...")
                            else:
                                if extract_ad_id(pg.url):
                                    print("  Could not extract listing data.")
                                else:
                                    print("  Not a listing page. Open a Gumtree listing first.")
                            break
                    except Exception:
                        pass
                if save_session_flag.is_set():
                    context.storage_state(path=str(SESSION_FILE))
                    save_session_flag.clear()
                    print("  Session saved to JSON (profile already persists).")
                time.sleep(0.3)
        finally:
            context.close()
    build_scraped_index(output_dir)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Scrape Gumtree listings, save locally, optionally upload to API")
    parser.add_argument("--urls", default=str(Path(__file__).parent / "urls.txt"), help="File with Gumtree URLs (one per line)")
    parser.add_argument("--output-dir", "-o", default=str(Path(__file__).parent / "scraped"), help="Output directory for scraped data")
    parser.add_argument("--dry-run", action="store_true", help="Scrape and save to folders (no API upload)")
    parser.add_argument("--upload", action="store_true", help="After scraping, upload from local folders to API")
    parser.add_argument("--debug", action="store_true", help="Print debug info when extraction fails")
    parser.add_argument("--list-categories", action="store_true", help="List categories for COMPANY_SLUG (requires --upload env vars)")
    parser.add_argument("--upload-to", default=None, help="Upload to specific slug or 'all' (overrides COMPANY_SLUG/COMPANY_SLUGS)")
    args = parser.parse_args()

    urls_path = Path(args.urls)
    if not urls_path.exists():
        print(f"Create {urls_path} and add Gumtree listing URLs (one per line).")
        return 1

    urls = []
    for line in urls_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "gumtree.co.za" in line:
            urls.append(line)

    if not urls:
        print(f"No URLs found in {urls_path}. Add Gumtree listing URLs, one per line.")
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
    session = requests.Session()
    session.headers.update(DEFAULT_HEADERS)
    for i, url in enumerate(urls):
        print(f"[{i+1}/{len(urls)}]")
        scrape_url(session, url, output_dir, debug=args.debug)
        if i < len(urls) - 1:
            time.sleep(2)

    build_scraped_index(output_dir)
    print(f"Done. Listings saved to {output_dir}/")
    print("See index.json and README.md for overview. Edit products.json, then run --upload to push to API.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
