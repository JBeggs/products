"""
Gumtree search-card and detail-page parsers.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlparse

GENERIC_URGENCY_PHRASES = [
    "urgent sale",
    "urgent",
    "must sell",
    "must go",
    "need cash",
    "price reduced",
    "negotiable",
    "moving",
    "relocating",
    "immigrating",
    "desperate",
]


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
    cleaned = re.sub(r"[R\s,]", "", str(text))
    match = re.search(r"(\d+)", cleaned)
    return int(match.group(1)) if match else None


def _url_matches_search_category(path_or_url: str, path_slugs: list[str] | None) -> bool:
    """True if the listing URL path belongs to the intended search category."""

    slugs = [slug.lower() for slug in (path_slugs or []) if slug]
    if not slugs:
        return True
    path = path_or_url
    if path.startswith("http"):
        path = urlparse(path).path or ""
    path_lower = path.lower()
    return any(slug in path_lower for slug in slugs)


def parse_search_cards(
    html: str,
    base_url: str,
    category: str,
    path_slugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse search results page for listing cards.
    Extracts card-level id, url, title, price, location, and best-effort posted date.
    """

    results = []
    link_pattern = re.compile(
        r'href="(https?://[^"]*gumtree\.co\.za/a-[^"]+/(\d{15,}))"',
        re.IGNORECASE,
    )
    rel_pattern = re.compile(
        r'href="(/a-[^"]+/(\d{15,}))"',
        re.IGNORECASE,
    )
    seen_ad_ids: set[str] = set()

    for match in link_pattern.finditer(html):
        full_url, ad_id = match.group(1), match.group(2)
        if ad_id in seen_ad_ids or not _url_matches_search_category(full_url, path_slugs):
            continue
        seen_ad_ids.add(ad_id)
        card = _extract_card_context(html, match.start(), match.end(), full_url, ad_id, category)
        if card:
            results.append(card)

    for match in rel_pattern.finditer(html):
        path, ad_id = match.group(1), match.group(2)
        if ad_id in seen_ad_ids or not _url_matches_search_category(path, path_slugs):
            continue
        full_url = urljoin("https://www.gumtree.co.za/", path)
        seen_ad_ids.add(ad_id)
        card = _extract_card_context(html, match.start(), match.end(), full_url, ad_id, category)
        if card:
            results.append(card)

    return results


def _extract_card_context(
    html: str,
    start: int,
    end: int,
    url: str,
    ad_id: str,
    category: str,
) -> dict[str, Any] | None:
    """Extract title, price, location, and posted date from nearby card markup."""

    window_start = max(0, start - 800)
    window_end = min(len(html), end + 1400)
    block = html[window_start:window_end]

    title = None
    title_match = re.search(r">([^<]{10,140})</a>", block)
    if title_match:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
        if len(title) < 5 or "gumtree" in title.lower():
            title = None

    price = None
    price_match = re.search(r'"price"\s*:\s*(\d+)', block)
    if price_match:
        price = int(price_match.group(1))
    if price is None:
        price_match = re.search(r"R\s*(\d[\d\s,]*)", block)
        if price_match:
            price = normalize_price(price_match.group(0))

    location = None
    loc_match = re.search(r'"location"[^}]*"name"\s*:\s*"([^"]+)"', block)
    if loc_match:
        location = loc_match.group(1).strip()
    if not location:
        loc_match = re.search(r'class="[^"]*location[^"]*"[^>]*>([^<]+)<', block, re.I)
        if loc_match:
            location = loc_match.group(1).strip()

    posted_at = extract_posted_at(block)

    return {
        "ad_id": ad_id,
        "url": url,
        "title": title or "Unknown",
        "price": price,
        "location": location,
        "posted_at": posted_at,
        "category": category,
    }


def extract_posted_at(html: str) -> str | None:
    """Extract Gumtree posted date from JSON-LD or nearby text when possible."""

    patterns = [
        r'"datePosted"\s*:\s*"([^"]+)"',
        r'"datePublished"\s*:\s*"([^"]+)"',
        r'"dateCreated"\s*:\s*"([^"]+)"',
        r'"postedAt"\s*:\s*"([^"]+)"',
        r'Posted[^<]{0,40}</[^>]+>\s*<[^>]+>([^<]+)</',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.I | re.S)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value:
                return value[:120]
    return None


def _extract_first_year(text: str) -> int | None:
    """Extract the first plausible year from free text."""

    for match in re.finditer(r"\b(19[89]\d|20[0-2]\d)\b", text):
        year = int(match.group(1))
        if 1980 <= year <= 2029:
            return year
    return None


def _extract_system_ram_gb(text: str) -> float | None:
    patterns = [
        r"\b(\d{1,3})\s*(?:gb|g)\s*(?:ram|memory)\b",
        r"\b(?:ram|memory)\s*(?:of)?\s*(\d{1,3})\s*(?:gb|g)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    return None


def _extract_storage_gb(text: str) -> float | None:
    patterns = [
        (r"\b(\d+(?:\.\d+)?)\s*tb\b", 1024.0),
        (r"\b(\d{2,5})\s*gb\b", 1.0),
    ]
    for pattern, multiplier in patterns:
        for match in re.finditer(pattern, text, re.I):
            value = float(match.group(1)) * multiplier
            if value >= 64:
                return value
    return None


def _extract_gpu_model(text: str) -> str | None:
    patterns = [
        r"\b(rtx\s?\d{3,4}(?:\s?ti)?)\b",
        r"\b(gtx\s?\d{3,4}(?:\s?ti)?)\b",
        r"\b(rx\s?\d{3,4}(?:\s?xt)?)\b",
        r"\b(radeon\s+[a-z0-9\s]+?\d{3,4})\b",
        r"\b(quadro\s+[a-z0-9-]+)\b",
        r"\b(arc\s+[a-z0-9-]+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def _extract_gpu_vram_gb(text: str) -> float | None:
    patterns = [
        r"\b(\d{1,2})\s*gb\s*(?:vram|gpu|graphics|video memory)\b",
        r"\b(?:gpu|graphics)\s*(?:with)?\s*(\d{1,2})\s*gb\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return float(match.group(1))
    return None


def _extract_cpu_model(text: str) -> str | None:
    patterns = [
        r"\b(i[3579][-\s]?\d{4,5}[a-z]{0,2})\b",
        r"\b(ryzen\s+[3579]\s+\d{4,5}[a-z]{0,2})\b",
        r"\b(xeon\s+[a-z0-9-]+)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def _extract_phone_storage_gb(text: str) -> float | None:
    for match in re.finditer(r"\b(64|128|256|512|1024)\s*gb\b", text, re.I):
        return float(match.group(1))
    return None


def _extract_phone_model(text: str) -> str | None:
    patterns = [
        r"\b(iphone\s+(?:1[0-7]|x[rsm]?|11|12|13|14|15|16)(?:\s+(?:pro|max|plus|mini))?)\b",
        r"\b(samsung\s+galaxy\s+[a-z0-9+\s]+)\b",
        r"\b(pixel\s+\d[a-z]?)\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def _extract_condition_phrases(text: str) -> list[str]:
    phrases = []
    for phrase in ["excellent condition", "good condition", "fair condition", "used", "refurbished", "sealed"]:
        if phrase in text:
            phrases.append(phrase)
    return phrases


def extract_listing_attributes(title: str | None, description: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Extract year/spec/urgency signals from title + description text."""

    combined = " ".join(part for part in [title or "", description or ""] if part).strip()
    combined_lower = combined.lower()
    attributes: dict[str, Any] = {}
    signals: dict[str, Any] = {}

    year = _extract_first_year(combined_lower)
    if year:
        attributes["year"] = year

    system_ram = _extract_system_ram_gb(combined_lower)
    if system_ram is not None:
        attributes["system_ram_gb"] = system_ram

    storage = _extract_storage_gb(combined_lower)
    if storage is not None:
        attributes["storage_gb"] = storage

    gpu_model = _extract_gpu_model(combined_lower)
    if gpu_model:
        attributes["gpu_model"] = gpu_model

    gpu_vram = _extract_gpu_vram_gb(combined_lower)
    if gpu_vram is not None:
        attributes["gpu_vram_gb"] = gpu_vram

    cpu_model = _extract_cpu_model(combined_lower)
    if cpu_model:
        attributes["cpu_model"] = cpu_model

    phone_storage = _extract_phone_storage_gb(combined_lower)
    if phone_storage is not None:
        attributes["phone_storage_gb"] = phone_storage

    phone_model = _extract_phone_model(combined_lower)
    if phone_model:
        attributes["phone_model"] = phone_model

    urgency_hits = [phrase for phrase in GENERIC_URGENCY_PHRASES if phrase in combined_lower]
    if urgency_hits:
        signals["urgency_hits"] = urgency_hits

    condition_hits = _extract_condition_phrases(combined_lower)
    if condition_hits:
        signals["condition_hits"] = condition_hits

    return attributes, signals


def parse_detail_page(html: str, url: str, category: str) -> dict[str, Any] | None:
    """
    Parse listing detail page for full data.
    Extracts title, price, location, seller, condition, description, posted date, and parsed attributes.
    """

    if "The request is blocked" in html or "Service unavailable" in html:
        return None

    ad_id = extract_ad_id_from_url(url)
    if not ad_id:
        return None

    title = None
    match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()

    price = None
    match = re.search(r'"price"\s*:\s*(\d+)', html)
    if match:
        price = int(match.group(1))
    if price is None:
        match = re.search(r'"amount"\s*:\s*"?(\d+)"?', html)
        if match:
            price = int(match.group(1))
    if price is None:
        match = re.search(r"R\s*(\d[\d\s,]*)", html)
        if match:
            price = normalize_price(match.group(0))

    location = None
    province, suburb = _parse_breadcrumbs_location(html)
    if province or suburb:
        location = " > ".join(part for part in (province, suburb) if part)
    if not location:
        match = re.search(r"Location[^<]*</[^>]+>[^<]*<a[^>]*>([^<]+)</a>", html, re.I | re.S)
        if match:
            location = match.group(1).strip()
    if not location:
        match = re.search(r'"location"[^}]*"name"\s*:\s*"([^"]+)"', html)
        if match:
            location = match.group(1).strip()

    seller = None
    match = re.search(r"For\s+Sale\s+By[^<]*</[^>]+>[^<]*<[^>]+>([^<]+)</", html, re.I | re.S)
    if match:
        seller = match.group(1).strip()
    if not seller:
        match = re.search(r"owner|dealer|private", html, re.I)
        if match:
            html_lower = html.lower()[:2500]
            seller = "Owner" if "owner" in html_lower else ("Dealer" if "dealer" in html_lower else "Private")

    condition = None
    match = re.search(r"Condition[^<]*</[^>]+>[^<]*<[^>]+>([^<]+)</", html, re.I | re.S)
    if match:
        condition = match.group(1).strip()

    description = None
    match = re.search(r'class="description-content"[^>]*>([\s\S]*?)</div>\s*</div>\s*</div>', html)
    if match:
        raw = match.group(1)
        description = re.sub(r"<[^>]+>", " ", raw)
        description = re.sub(r"\s+", " ", description).strip()[:4000]

    posted_at = extract_posted_at(html)
    image_urls = extract_detail_images(html)
    attributes, signals = extract_listing_attributes(title, description)
    attributes["image_count"] = len(image_urls)

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
        "posted_at": posted_at,
        "attributes": attributes,
        "signals": signals,
        "image_urls": image_urls,
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
