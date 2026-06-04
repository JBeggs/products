#!/usr/bin/env python3
"""
Debug probe for Junk Mail parsers.

Validates card parsing, pagination URL building, price parsing, and UUID ad_id extraction
against fixture HTML and optionally live URLs (requires Cloudflare clearance).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PRODUCTS_ROOT))

from junkmail_crawler.parsers import (  # noqa: E402
    build_junkmail_search_url,
    extract_ad_id_from_url,
    extract_canonical_listing_url,
    extract_location_from_junkmail_url,
    normalize_junkmail_listing_url,
    normalize_price,
    parse_detail_page,
    parse_junkmail_location,
    parse_search_cards,
    resolve_listing_url,
    search_page_url,
)

SAMPLE_SEARCH_HTML = """
<div class="results">
  <a href="/office-and-business/gauteng/johannesburg/co2-laser-cutter-machine/abc123def4567890abcdef1234567890">
    <span class="title">CO2 Laser Cutter 100W</span>
  </a>
  <span>From R 14 000 Negotiable</span>
  <span>Region: Gauteng - Johannesburg</span>
  <a href="https://www.junkmail.co.za/office-and-business/gauteng/pretoria/dtf-printer/feedfeedfeedfeedfeedfeedfeedfeed">
    DTF Printer A3
  </a>
  <span>R 45 000</span>
  <span>Region: Gauteng - Pretoria</span>
</div>
"""

SAMPLE_DETAIL_HTML = """
<html><head><title>CO2 Laser Cutter</title>
<link rel="canonical" href="https://www.junkmail.co.za/office-and-business/gauteng/johannesburg/co2-laser-cutter/abc123def4567890abcdef1234567890" />
</head><body>
<h1>CO2 Laser Cutter 100W</h1>
<p>From R 14 000 Negotiable</p>
<p>Ad Type: Owner</p>
<p>Region: Gauteng - Johannesburg</p>
<p>Condition: Used</p>
<div class="description">Full spectrum laser with chiller. Urgent sale, must go.</div>
<img src="https://images.junkmail.co.za/listings/laser1.jpg" />
</body></html>
"""

SAMPLE_DETAIL_URL = (
    "https://www.junkmail.co.za/office-and-business/gauteng/johannesburg/"
    "co2-laser-cutter/abc123def4567890abcdef1234567890"
)

SAMPLE_MAP_MARKER_SEARCH_HTML = """
<div class="card">
  <a href="/computers-and-gaming/gaming/gauteng/reiger-park/gaming-pc-in-good-condition/7f24b49a775b4f09a855cb6469314ba3">
    <span class="title">Gaming pc in good condition</span>
  </a>
  <p class="card-text">R  13 500 <span>For Sale</span></p>
  <p class="badge badge-primary"><i class="fa fa-map-marker right-spaced" aria-hidden="true"></i>Reiger Park</p>
</div>
"""

SAMPLE_JSON_LD_DETAIL_HTML = """
<html><head><title>Gaming pc | Junk Mail Marketplace</title>
<link rel="canonical" href="https://www.junkmail.co.za/computers-and-gaming/gaming/gauteng/reiger-park/gaming-pc-in-good-condition/7f24b49a775b4f09a855cb6469314ba3" />
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Gaming pc in good condition",
  "description": "ASUS tuff gaming motherboard (wifi)&#xD;&#xA;Ryzen 5 3600x cpu &#xD;&#xA;Palit GeForce 1650 graphics card&#xD;&#xA;Klevv 950Gb ssd &#xD;&#xA;Klevv 8Gb ram",
  "offers": {"price": "13500", "priceCurrency": "ZAR"},
  "address": {"addressLocality": "Reiger Park", "addressRegion": "Gauteng"}
}
</script>
</head><body>
<h1>Gaming pc in good condition</h1>
<p>From R 13 500 Negotiable</p>
</body></html>
"""

SAMPLE_JSON_LD_DETAIL_URL = (
    "https://www.junkmail.co.za/computers-and-gaming/gaming/gauteng/reiger-park/"
    "gaming-pc-in-good-condition/7f24b49a775b4f09a855cb6469314ba3"
)


def run_fixture_tests() -> bool:
    ok = True

    price = normalize_price("R 14 000 Negotiable")
    if price != 14000:
        print(f"FAIL price: expected 14000 got {price}")
        ok = False
    else:
        print(f"OK price parse: R 14 000 -> {price}")

    price2 = normalize_price("From R 2 500")
    if price2 != 2500:
        print(f"FAIL from-price: expected 2500 got {price2}")
        ok = False
    else:
        print(f"OK from-price: From R 2 500 -> {price2}")

    ad_id = extract_ad_id_from_url(SAMPLE_DETAIL_URL)
    if ad_id != "abc123def4567890abcdef1234567890":
        print(f"FAIL ad_id: got {ad_id}")
        ok = False
    else:
        print(f"OK ad_id: {ad_id}")

    page2 = search_page_url("https://www.junkmail.co.za/q-laser%20cutter/so-latest", 2)
    expected = "https://www.junkmail.co.za/q-laser%20cutter/so-latest/page2"
    if page2 != expected:
        print(f"FAIL pagination URL: {page2}")
        ok = False
    else:
        print(f"OK pagination: {page2}")

    gaming_url = build_junkmail_search_url(
        min_price=10000,
        max_price=20000,
        query="gaming pc",
        category_path="computers-and-gaming",
    )
    expected_gaming = (
        "https://www.junkmail.co.za/pr10000-20000/computers-and-gaming/q-gaming%20pc/so-latest"
    )
    if gaming_url != expected_gaming:
        print(f"FAIL gaming URL: {gaming_url}")
        ok = False
    else:
        print(f"OK gaming URL: {gaming_url}")

    page2_price = search_page_url(gaming_url, 2)
    if page2_price != expected_gaming + "/page2":
        print(f"FAIL price pagination: {page2_price}")
        ok = False
    else:
        print(f"OK price pagination: {page2_price}")

    example = (
        "https://www.junkmail.co.za/computers-and-gaming/gaming/gauteng/johannesburg/"
        "gaming-pc/5acad4b0d6cd4f92b6904a9a28b8e303"
    )
    normalized = normalize_junkmail_listing_url(example)
    if normalized != example:
        print(f"FAIL normalize example URL: {normalized}")
        ok = False
    else:
        print(f"OK normalize example URL")

    short_bad = "https://www.junkmail.co.za/office/gauteng/pretoria/co2-laser/aabbccddeeff00112233445566778899"
    if normalize_junkmail_listing_url(short_bad) != short_bad:
        print(f"FAIL normalize short path URL")
        ok = False
    else:
        print("OK normalize short path URL (valid shape)")

    canonical = extract_canonical_listing_url(SAMPLE_DETAIL_HTML)
    if canonical != SAMPLE_DETAIL_URL:
        print(f"FAIL canonical URL: {canonical}")
        ok = False
    else:
        print(f"OK canonical URL: {canonical}")

    cards = parse_search_cards(SAMPLE_SEARCH_HTML, "https://www.junkmail.co.za/q-laser/so-latest", "laser-cutting", path_slugs=["laser", "office"])
    if len(cards) != 2:
        print(f"FAIL cards: expected 2 got {len(cards)}")
        ok = False
    else:
        print(f"OK cards: {len(cards)} listings")
        for card in cards:
            print(f"  - {card['ad_id'][:8]}... {card['title']!r} R{card.get('price')}")

    detail = parse_detail_page(SAMPLE_DETAIL_HTML, "https://www.junkmail.co.za/wrong/path/abc123def4567890abcdef1234567890", "laser-cutting")
    if not detail or detail.get("price") != 14000:
        print(f"FAIL detail parse: {detail}")
        ok = False
    elif detail.get("url") != SAMPLE_DETAIL_URL:
        print(f"FAIL detail canonical url: {detail.get('url')}")
        ok = False
    else:
        print(f"OK detail: {detail['title']!r} url={detail['url'][-40:]} seller={detail.get('seller')} location={detail.get('location')}")

    map_cards = parse_search_cards(
        SAMPLE_MAP_MARKER_SEARCH_HTML,
        "https://www.junkmail.co.za/pr10000-20000/computers-and-gaming/q-gaming%20pc/so-latest",
        "desktop-computers",
        path_slugs=["computers-and-gaming", "gaming"],
    )
    if len(map_cards) != 1:
        print(f"FAIL map-marker cards: expected 1 got {len(map_cards)}")
        ok = False
    elif map_cards[0].get("location") != "Reiger Park":
        print(f"FAIL map-marker location: {map_cards[0].get('location')!r}")
        ok = False
    else:
        print(f"OK map-marker card location: {map_cards[0]['location']!r}")

    url_location = extract_location_from_junkmail_url(SAMPLE_JSON_LD_DETAIL_URL)
    if url_location != "Gauteng - Reiger Park":
        print(f"FAIL URL location: {url_location!r}")
        ok = False
    else:
        print(f"OK URL location: {url_location!r}")

    json_detail = parse_detail_page(SAMPLE_JSON_LD_DETAIL_HTML, SAMPLE_JSON_LD_DETAIL_URL, "desktop-computers")
    if not json_detail:
        print("FAIL json-ld detail parse: None")
        ok = False
    elif json_detail.get("price") != 13500:
        print(f"FAIL json-ld price: {json_detail.get('price')}")
        ok = False
    elif "GeForce 1650" not in (json_detail.get("description") or ""):
        print(f"FAIL json-ld description: {json_detail.get('description')!r}")
        ok = False
    elif json_detail.get("location") != "Reiger Park - Gauteng":
        print(f"FAIL json-ld location: {json_detail.get('location')!r}")
        ok = False
    elif not json_detail.get("attributes", {}).get("cpu_model"):
        print(f"FAIL json-ld attrs: {json_detail.get('attributes')}")
        ok = False
    else:
        attrs = json_detail["attributes"]
        print(
            f"OK json-ld detail: price={json_detail['price']} location={json_detail.get('location')!r} "
            f"cpu={attrs.get('cpu_model')} ram={attrs.get('system_ram_gb')}"
        )

    location_cases = [
        (("Gauteng - Reiger Park", None), ("Gauteng", "Reiger Park", "")),
        (("Reiger Park - Gauteng", None), ("Gauteng", "Reiger Park", "")),
        (("Kwazulu Natal - Durban", None), ("KwaZulu-Natal", "Durban", "")),
        (("Western Cape - Durbanville", None), ("Western Cape", "Durbanville", "")),
        (("Johannesburg, Gauteng", None), ("Gauteng", "Johannesburg", "")),
        (("Gauteng", None), ("Gauteng", "", "")),
        (
            ("Reiger Park", SAMPLE_JSON_LD_DETAIL_URL),
            ("Gauteng", "Reiger Park", ""),
        ),
    ]
    for (loc, url), expected in location_cases:
        parsed = parse_junkmail_location(loc, url)
        if parsed != expected:
            print(f"FAIL location parse {loc!r} url={url!r}: expected {expected} got {parsed}")
            ok = False
        else:
            print(f"OK location parse {loc!r} -> province={parsed[0]!r} city={parsed[1]!r}")

    from junkmail_crawler.parsers import is_cloudflare_challenge, junkmail_page_has_content

    cf_html = "<html><head><title>Just a moment...</title></head><body>challenge-platform</body></html>"
    if not is_cloudflare_challenge(cf_html, "Just a moment..."):
        print("FAIL CF detect: should flag interstitial")
        ok = False
    else:
        print("OK CF detect interstitial")

    if is_cloudflare_challenge(SAMPLE_SEARCH_HTML, "Laser cutter | Junk Mail"):
        print("FAIL CF detect: should not flag normal search HTML")
        ok = False
    else:
        print("OK CF detect allows search HTML")

    if not junkmail_page_has_content(SAMPLE_SEARCH_HTML, "Laser cutter | Junk Mail"):
        print("FAIL junkmail_page_has_content on search HTML")
        ok = False
    else:
        print("OK junkmail_page_has_content on search HTML")

    return ok


def probe_live(url: str) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright not installed")
        return

    from junkmail_crawler.parsers import is_cloudflare_challenge

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=60000)
        page.wait_for_timeout(3000)
        html = page.content()
        title = page.title()
        browser.close()

    if is_cloudflare_challenge(html, title):
        print(f"Cloudflare challenge on {url} (title={title!r})")
        return

    cards = parse_search_cards(html, url, "probe", path_slugs=[])
    print(f"Live cards on {url}: {len(cards)}")
    for card in cards[:5]:
        print(f"  {card['ad_id'][:8]}... {card['title']!r} R{card.get('price')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Junk Mail parser debug probe")
    parser.add_argument("--live", metavar="URL", help="Probe a live Junk Mail search URL")
    args = parser.parse_args()

    print("=== Fixture tests ===")
    if not run_fixture_tests():
        return 1

    if args.live:
        print("\n=== Live probe ===")
        probe_live(args.live)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
