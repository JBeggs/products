#!/usr/bin/env python3
"""
Debug: fetch Gumtree search page and run parse_search_cards to see what we get.
Run from products/: python -m gumtree_crawler.debug_parse
"""
import sys
from pathlib import Path

# Ensure products/ is on path
PRODUCTS = Path(__file__).resolve().parent.parent
if str(PRODUCTS) not in sys.path:
    sys.path.insert(0, str(PRODUCTS))

from gumtree_crawler.config import SEARCHES
from gumtree_crawler.parsers import parse_search_cards

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("Playwright not installed. Testing with sample HTML only.")
    sync_playwright = None


def main():
    # Sample HTML from user's message - links like href="/a-.../ADID"
    sample = '''
    <a href="/a-other-replacement-car-part/boksburg/kia-seltos-automatic-gearbox-for-sale/10013524772341011481470009" class="related-ad-title"><span>Kia Seltos</span></a>
    <a href="/a-car-engines-engine-parts/pretoria-west/i-amp-s-merc-auto-part/10013524777031011446087009" class="related-ad-title"><span>I&S MERC</span></a>
    '''
    print("=== Test 1: Sample HTML (user's format) ===")
    cards = parse_search_cards(sample, "https://www.gumtree.co.za/s-other/v1c1p1", "test")
    print(f"Cards found: {len(cards)}")
    for c in cards[:3]:
        print(f"  {c.get('ad_id')} | {c.get('title')} | {c.get('url', '')[:60]}...")

    if not sync_playwright:
        return

    # Fetch real page
    search = SEARCHES[0]  # Motorcycles
    print(f"\n=== Test 2: Live fetch {search.name} ===")
    print(f"URL: {search.url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        )
        page.set_default_timeout(60000)
        try:
            page.goto(search.url, wait_until="domcontentloaded", timeout=60000)
            import time
            time.sleep(3)  # Allow JS render
            html = page.content()
            print(f"HTML length: {len(html)} chars")

            # Check if our patterns exist in raw HTML
            import re
            rel_matches = list(re.finditer(r'href="(/a-[^"]+/(\d{15,}))"', html))
            print(f"Regex href=/a-.../ID matches: {len(rel_matches)}")
            adlink_matches = list(re.finditer(r'data-adlink="(/a-[^"]+/(\d{15,}))"', html))
            print(f"Regex data-adlink matches: {len(adlink_matches)}")

            cards = parse_search_cards(html, search.url, search.category)
            print(f"parse_search_cards returned: {len(cards)} cards")
            for c in cards[:5]:
                print(f"  {c.get('ad_id')} | {c.get('title')} | {c.get('price')}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
