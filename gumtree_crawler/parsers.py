"""
Gumtree parsers: search card and detail page extraction.
Based on Gumtree markup: .related-item, .ad-price, .related-ad-title, .vip-main-content, etc.
"""
import re
from typing import Any
from urllib.parse import urljoin, urlparse


def extract_ad_id_from_url(url: str) -> str | None:
    """Extract ad ID from Gumtree listing URL (numeric ID at end of path)."""
    parsed = urlparse(url)
    path = (parsed.path or "").rstrip("/")
    match = re.search(r"/(\d{15,})\s*$", path)
    return match.group(1) if match else None


def normalize_price(text: str | None) -> int | None:
    """Parse price from text to numeric ZAR. Returns None if not parseable."""
    if not text:
        return None
    # Remove R, spaces, commas
    cleaned = re.sub(r"[R\s,]", "", str(text))
    match = re.search(r"(\d+)", cleaned)
    return int(match.group(1)) if match else None


# Expected URL path slugs per search category (from /a-SLUG/ in listing URLs)
# Only links matching these slugs are from the actual search; others are related/sponsored
_CATEGORY_PATH_SLUGS: dict[str, list[str]] = {
    "motorcycles": ["motorcycles-scooters", "motorcycles", "scooters"],
    "laptops": ["computers-laptops", "computers", "laptops"],
}


def _url_matches_search_category(path_or_url: str, category: str) -> bool:
    """True if the listing URL path belongs to the search category (not related/sponsored)."""
    slugs = _CATEGORY_PATH_SLUGS.get(category)
    if not slugs:
        return True  # unknown category, allow
    path = path_or_url
    if path.startswith("http"):
        path = urlparse(path).path or ""
    path_lower = path.lower()
    return any(slug in path_lower for slug in slugs)


def parse_search_cards(html: str, base_url: str, category: str) -> list[dict[str, Any]]:
    """
    Parse search results page for listing cards.
    Extracts: ad_id, url, title, price, location, seller hint, creation_date.
    Only includes links whose URL path matches the search category (filters out related/sponsored).
    """
    results = []
    # Gumtree listing links: /a-category/location/title-slug/ADID
    link_pattern = re.compile(
        r'href="(https?://[^"]*gumtree\.co\.za/a-[^"]+/(\d{15,}))"',
        re.IGNORECASE,
    )
    rel_pattern = re.compile(
        r'href="(/a-[^"]+/(\d{15,}))"',
        re.IGNORECASE,
    )

    seen_ad_ids: set[str] = set()

    for m in link_pattern.finditer(html):
        full_url, ad_id = m.group(1), m.group(2)
        if ad_id in seen_ad_ids or not _url_matches_search_category(full_url, category):
            continue
        seen_ad_ids.add(ad_id)
        card = _extract_card_context(html, m.start(), m.end(), full_url, ad_id, base_url, category)
        if card:
            results.append(card)

    for m in rel_pattern.finditer(html):
        path, ad_id = m.group(1), m.group(2)
        if ad_id in seen_ad_ids or not _url_matches_search_category(path, category):
            continue
        full_url = urljoin("https://www.gumtree.co.za/", path)
        seen_ad_ids.add(ad_id)
        card = _extract_card_context(html, m.start(), m.end(), full_url, ad_id, base_url, category)
        if card:
            results.append(card)

    return results


def _extract_card_context(
    html: str, start: int, end: int, url: str, ad_id: str, base_url: str, category: str
) -> dict[str, Any] | None:
    """Extract title, price, location from context around the link."""
    # Look in a window around the match (typical card is ~500-1500 chars)
    window_start = max(0, start - 800)
    window_end = min(len(html), end + 1200)
    block = html[window_start:window_end]

    title = None
    # Title: often in the link text or nearby
    title_m = re.search(r">([^<]{10,120})</a>", block)
    if title_m:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_m.group(1))).strip()
        if len(title) < 5 or "gumtree" in title.lower():
            title = None

    # Price: R1234 or NEGOTIABLE
    price = None
    price_m = re.search(r'"price"\s*:\s*(\d+)', block)
    if price_m:
        price = int(price_m.group(1))
    if price is None:
        price_m = re.search(r"R\s*(\d[\d\s,]*)", block)
        if price_m:
            price = normalize_price(price_m.group(0))

    # Location: often after location icon or in structured data
    location = None
    loc_m = re.search(r'"location"[^}]*"name"\s*:\s*"([^"]+)"', block)
    if loc_m:
        location = loc_m.group(1).strip()
    if not location:
        loc_m = re.search(r'class="[^"]*location[^"]*"[^>]*>([^<]+)<', block, re.I)
        if loc_m:
            location = loc_m.group(1).strip()

    return {
        "ad_id": ad_id,
        "url": url,
        "title": title or "Unknown",
        "price": price,
        "location": location,
        "category": category,
    }


def parse_detail_page(html: str, url: str, category: str) -> dict[str, Any] | None:
    """
    Parse listing detail page for full data.
    Extracts: title, price, location, seller (For Sale By), condition, description.
    """
    if "The request is blocked" in html or "Service unavailable" in html:
        return None

    ad_id = extract_ad_id_from_url(url)
    if not ad_id:
        return None

    # Title: <h1> or .title h1
    title = None
    m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()

    # Price: "price": 62000 or "amount": "62000"
    price = None
    m = re.search(r'"price"\s*:\s*(\d+)', html)
    if m:
        price = int(m.group(1))
    if price is None:
        m = re.search(r'"amount"\s*:\s*"?(\d+)"?', html)
        if m:
            price = int(m.group(1))
    if price is None:
        m = re.search(r"R\s*(\d[\d\s,]*)", html)
        if m:
            price = normalize_price(m.group(0))

    # Location: prefer breadcrumbs (Province > Suburb), fallback to General Details
    location = None
    province, suburb = _parse_breadcrumbs_location(html)
    if province or suburb:
        location = " > ".join(p for p in (province, suburb) if p)
    if not location:
        m = re.search(r"Location[^<]*</[^>]+>[^<]*<a[^>]*>([^<]+)</a>", html, re.I | re.S)
        if m:
            location = m.group(1).strip()
    if not location:
        m = re.search(r'"location"[^}]*"name"\s*:\s*"([^"]+)"', html)
        if m:
            location = m.group(1).strip()

    # Seller: For Sale By
    seller = None
    m = re.search(r"For\s+Sale\s+By[^<]*</[^>]+>[^<]*<[^>]+>([^<]+)</", html, re.I | re.S)
    if m:
        seller = m.group(1).strip()
    if not seller:
        m = re.search(r"owner|dealer|private", html, re.I)
        if m:
            seller = "Owner" if "owner" in html.lower()[:2000] else ("Dealer" if "dealer" in html.lower()[:2000] else "Private")

    # Condition
    condition = None
    m = re.search(r"Condition[^<]*</[^>]+>[^<]*<[^>]+>([^<]+)</", html, re.I | re.S)
    if m:
        condition = m.group(1).strip()

    # Description
    description = None
    m = re.search(r'class="description-content"[^>]*>([\s\S]*?)</div>\s*</div>\s*</div>', html)
    if m:
        raw = m.group(1)
        description = re.sub(r"<[^>]+>", " ", raw)
        description = re.sub(r"\s+", " ", description).strip()[:2000]

    if not title and not price and not description:
        return None

    return {
        "ad_id": ad_id,
        "url": url,
        "title": title or "Unknown",
        "price": price,
        "location": location,
        "seller": seller,
        "condition": condition,
        "description": description,
        "category": category,
    }


def _parse_breadcrumbs_location(html: str) -> tuple[str, str]:
    """
    Extract province and suburb from breadcrumbs: (Province > Suburb > category...).
    Returns (province, suburb). First link = province, second = suburb/area.
    """
    province = ""
    suburb = ""
    idx = html.find('class="breadcrumbs"')
    if idx >= 0:
        block = html[idx : idx + 1200]
        links = re.findall(r'<a\s+href="[^"]*"[^>]*><span>([^<]+)</span></a>', block)
        if len(links) >= 2:
            province = links[0].strip().replace("&amp;", "&")
            suburb = links[1].strip().replace("&amp;", "&")
        elif len(links) >= 1:
            province = links[0].strip().replace("&amp;", "&")
    return (province, suburb)


def get_next_page_url(html: str, current_url: str) -> str | None:
    """Extract next page URL from pagination."""
    # page-2, page-3, etc.
    m = re.search(r'href="([^"]*gumtree\.co\.za[^"]*page-\d+[^"]*)"', html, re.I)
    if m:
        return urljoin(current_url, m.group(1))
    return None


def extract_detail_images(html: str, max_images: int = 10) -> list[str]:
    """
    Extract image URLs from Gumtree listing detail page HTML.
    Uses gms.gumtree.co.za/v2/images/za_ads_* pattern from vip-gallery or full page.
    Dedupes by base URL, upscales size=s to size=l. Returns up to max_images URLs.
    """
    # Prefer vip-gallery block (main listing images); fallback to full HTML
    gallery_start = html.find('vip-gallery')
    if gallery_start >= 0:
        html_for_images = html[gallery_start : gallery_start + 12000]
    else:
        html_for_images = html
    seen: set[str] = set()
    images: list[str] = []
    for m in re.finditer(r"gms\.gumtree\.co\.za/v2/images/[^\"\s<>]+", html_for_images):
        u = m.group(0)
        if not u.startswith("http"):
            u = "https://" + u
        base = u.split("?")[0]
        if base not in seen and "za_ads_" in u:
            seen.add(base)
            u = u.replace("size=s", "size=l") if "size=s" in u else u
            images.append(u)
            if len(images) >= max_images:
                break
    return images
