"""
Makro parsers: search card and detail page extraction.
Based on Makro markup: product links with pid, price format R X R Y Z% off.
"""
import re
from typing import Any
from urllib.parse import urljoin, urlparse, parse_qs


def extract_pid_from_url(url: str) -> str | None:
    """Extract pid (product ID) from Makro product URL. Used as stable ad_id."""
    parsed = urlparse(url)
    if "makro.co.za" not in (parsed.netloc or ""):
        return None
    qs = parse_qs(parsed.query)
    pids = qs.get("pid", [])
    return pids[0] if pids else None


def normalize_price(text: str | None) -> int | None:
    """Parse price from Makro text to numeric ZAR (cents). Returns None if not parseable.
    Makro uses format like R 14899 or R 1,06900 (cents: 14899 = R148.99, 106900 = R1069.00).
    """
    if not text:
        return None
    cleaned = re.sub(r"[R\s,]", "", str(text))
    match = re.search(r"(\d+)", cleaned)
    return int(match.group(1)) if match else None


def parse_search_cards(html: str, base_url: str, category: str) -> list[dict[str, Any]]:
    """
    Parse Makro search results page for product cards.
    Extracts: ad_id (pid), url, title, price (current), original_price, discount, availability.
    Makro URLs: https://www.makro.co.za/product-slug/p/itmXXX?pid=XXXX
    Price format: R 14899 R 29900 50% off
    """
    results = []
    # Match product links with pid
    link_pattern = re.compile(
        r'href="(https?://[^"]*makro\.co\.za/[^"]+)"[^>]*>',
        re.IGNORECASE,
    )
    rel_pattern = re.compile(
        r'href="(/[^"]+)"[^>]*>',
        re.IGNORECASE,
    )

    seen_pids: set[str] = set()

    def process_match(full_url: str, start: int, end: int) -> None:
        pid = extract_pid_from_url(full_url)
        if not pid or pid in seen_pids:
            return
        seen_pids.add(pid)
        card = _extract_card_context(html, start, end, full_url, pid, base_url, category)
        if card:
            results.append(card)

    for m in link_pattern.finditer(html):
        full_url = m.group(1).replace("&amp;", "&")
        if "/p/itm" in full_url or "pid=" in full_url:
            process_match(full_url, m.start(), m.end())

    for m in rel_pattern.finditer(html):
        path = m.group(1)
        if "/p/itm" not in path and "pid=" not in (path or ""):
            continue
        full_url = urljoin("https://www.makro.co.za/", path.replace("&amp;", "&"))
        if "makro.co.za" in full_url and extract_pid_from_url(full_url):
            process_match(full_url, m.start(), m.end())

    return results


def _extract_card_context(
    html: str, start: int, end: int, url: str, pid: str, base_url: str, category: str
) -> dict[str, Any] | None:
    """Extract title, price from context around the product link."""
    window_start = max(0, start - 600)
    window_end = min(len(html), end + 1000)
    block = html[window_start:window_end]

    # Title: prefer JSON/structured, then link text
    title = None
    m = re.search(r'"title"\s*:\s*"([^"]+)"', block)
    if m:
        title = re.sub(r"\s+", " ", m.group(1).strip())
    if not title:
        m = re.search(r'"name"\s*:\s*"([^"]+)"', block)
        if m:
            t = re.sub(r"\s+", " ", m.group(1).strip())
            if len(t) >= 4 and "makro" not in t.lower():
                title = t
    if not title:
        title_m = re.search(r">([^<]{8,150})</a>", block)
        if title_m:
            raw = re.sub(r"<[^>]+>", "", title_m.group(1))
            title = re.sub(r"\s+", " ", raw).strip()
    if title and (len(title) < 4 or "makro" in title.lower() or title.startswith("R ")):
        title = None

    # Price: R 14899 R 29900 50% off - first number is current price (cents)
    price = None
    # Try JSON/structured first
    price_m = re.search(r'"selling_price"\s*:\s*(\d+)', block)
    if price_m:
        price = int(price_m.group(1))
    if price is None:
        price_m = re.search(r'"price"\s*:\s*(\d+)', block)
        if price_m:
            price = int(price_m.group(1))
    if price is None:
        # R 14899 R 29900 or R 1,06900 R 2,49900
        price_m = re.search(r"R\s*([\d,]+)\s*R\s*[\d,]+", block)
        if price_m:
            price = normalize_price(price_m.group(1))
    if price is None:
        price_m = re.search(r"R\s*([\d,]+)", block)
        if price_m:
            price = normalize_price(price_m.group(1))

    return {
        "ad_id": pid,
        "url": url,
        "title": title,
        "price": price,
        "category": category,
    }


def parse_detail_page(html: str, url: str, category: str) -> dict[str, Any] | None:
    """
    Parse Makro product detail page for full data.
    Extracts: title, price, description, seller, product attributes.
    """
    if "Are you a human" in html or "human verification" in html.lower():
        return None
    if "The request is blocked" in html or "Service unavailable" in html:
        return None

    pid = extract_pid_from_url(url)
    if not pid:
        return None

    # Title: prefer JSON-LD/og:title, then h1, then generic title
    title = None
    m = re.search(r'"title"\s*:\s*"([^"]+)"', html)
    if m:
        title = re.sub(r"\s+", " ", m.group(1).strip())
    if not title:
        m = re.search(r'"name"\s*:\s*"([^"]+)"', html)
        if m:
            t = re.sub(r"\s+", " ", m.group(1).strip())
            if len(t) >= 4 and "makro" not in t.lower():
                title = t
    if not title:
        m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html, re.I)
        if m:
            title = re.sub(r"\s+", " ", m.group(1).strip())
    if not title:
        m = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()

    # Price: selling_price or price in JSON
    price = None
    m = re.search(r'"selling_price"\s*:\s*(\d+)', html)
    if m:
        price = int(m.group(1))
    if price is None:
        m = re.search(r'"price"\s*:\s*(\d+)', html)
        if m:
            price = int(m.group(1))
    if price is None:
        m = re.search(r"R\s*([\d,]+)", html)
        if m:
            price = normalize_price(m.group(1))

    # Seller: Product Sold By
    seller = None
    m = re.search(r"Product\s+Sold\s+By[^<]*</[^>]+>[^<]*<[^>]+>([^<]+)</", html, re.I | re.S)
    if m:
        seller = m.group(1).strip()
    if not seller and "Product Sold By Makro" in html:
        seller = "Makro"
    if not seller and "More4Less" in html:
        seller = "More4Less"

    # Description
    description = None
    m = re.search(r'"description"\s*:\s*"([^"]*(?:\\.[^"]*)*)"', html)
    if m:
        raw = m.group(1).replace("\\n", "\n").replace('\\"', '"')
        description = re.sub(r"<[^>]+>", " ", raw)[:2000].strip()
    if not description:
        m = re.search(r'class="[^"]*description[^"]*"[^>]*>([\s\S]*?)</div>', html)
        if m:
            description = re.sub(r"<[^>]+>", " ", m.group(1))[:2000].strip()

    if not title and not price and not description:
        return None

    return {
        "ad_id": pid,
        "url": url,
        "title": title,
        "price": price,
        "location": None,
        "seller": seller,
        "condition": None,
        "description": description,
        "category": category,
    }


def get_next_page_url(html: str, current_url: str) -> str | None:
    """Extract next page URL from Makro pagination."""
    # Makro uses page=2, page=3 in query
    m = re.search(r'href="([^"]*makro\.co\.za[^"]*page[=\-]\d+[^"]*)"', html, re.I)
    if m:
        return urljoin(current_url, m.group(1).replace("&amp;", "&"))
    # Try page param in link
    m = re.search(r'[?&]page=(\d+)', html)
    if m:
        parsed = urlparse(current_url)
        qs = parse_qs(parsed.query)
        qs["page"] = [m.group(1)]
        from urllib.parse import urlencode
        new_qs = urlencode({k: v[0] for k, v in qs.items()}, doseq=False)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{new_qs}"
    return None
