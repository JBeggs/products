"""
Junk Mail search-card and detail-page parsers.
"""
from __future__ import annotations

import json
import re
from html import unescape
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from gumtree_crawler.parsers import (
    GENERIC_URGENCY_PHRASES,
    extract_listing_attributes,
    extract_posted_at,
)

JUNKMAIL_HOST = "junkmail.co.za"
JUNKMAIL_BASE_URL = f"https://www.{JUNKMAIL_HOST}"
AD_ID_PATTERN = re.compile(r"/([a-f0-9]{32})\s*$", re.I)
LISTING_HREF_PATTERN = re.compile(
    r'href="((?:https?://(?:www\.)?junkmail\.co\.za)?/[^"#?]+/([a-f0-9]{32}))"',
    re.I,
)
JOBMAIL_HOST = "jobmail.co.za"
JOBMAIL_BASE_URL = f"https://www.{JOBMAIL_HOST}"
JOBMAIL_AD_ID_PATTERN = re.compile(r"-id-(\d+)\s*$", re.I)
JOBMAIL_LISTING_HREF_PATTERN = re.compile(
    r'href="(https?://(?:www\.)?jobmail\.co\.za/jobs/[^"#?]+-id-(\d+)(?:\?[^"]*)?)"',
    re.I,
)
CANONICAL_LINK_PATTERN = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']',
    re.I,
)
CANONICAL_LINK_PATTERN_ALT = re.compile(
    r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']canonical["\']',
    re.I,
)
OG_URL_PATTERN = re.compile(
    r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']',
    re.I,
)
OG_URL_PATTERN_ALT = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:url["\']',
    re.I,
)
JSON_LD_URL_PATTERN = re.compile(
    r'"(?:@id|url)"\s*:\s*"(https?://[^"]*junkmail[^"]*/[a-f0-9]{32})"',
    re.I,
)
MAP_MARKER_LOCATION_PATTERN = re.compile(
    r"fa-map-marker[^>]*></i>\s*([^<]+)",
    re.I,
)
JSON_LD_SCRIPT_PATTERN = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
    re.I,
)
SA_PROVINCE_SLUGS = frozenset(
    {
        "eastern-cape",
        "free-state",
        "gauteng",
        "kwazulu-natal",
        "limpopo",
        "mpumalanga",
        "northern-cape",
        "north-west",
        "western-cape",
    }
)
PROVINCE_CANONICAL = {
    "eastern cape": "Eastern Cape",
    "free state": "Free State",
    "gauteng": "Gauteng",
    "kwazulu natal": "KwaZulu-Natal",
    "limpopo": "Limpopo",
    "mpumalanga": "Mpumalanga",
    "northern cape": "Northern Cape",
    "north west": "North West",
    "western cape": "Western Cape",
}
SKIP_PATH_FRAGMENTS = (
    "/page",
    "/so-",
    "/private-adverts",
    "/business-adverts",
    "/q-",
    "/login",
    "/register",
)

# CV / job-seeker ads — not employer vacancies.
JOB_SEEKER_PATH_FRAGMENT = "/job-seekers/"


def is_junkmail_job_seeker_listing(url_or_path: str | None) -> bool:
    """True for Junk Mail CV ads posted by people looking for work (not vacancies)."""

    raw = (url_or_path or "").strip()
    if not raw:
        return False
    path = raw.lower()
    if path.startswith("http"):
        path = (urlparse(unescape(raw)).path or "").lower()
    return JOB_SEEKER_PATH_FRAGMENT in path


def extract_ad_id_from_url(url: str) -> str | None:
    """Extract ad ID from Junk Mail listing URL (trailing 32-char hex UUID)."""

    parsed = urlparse(unescape((url or "").strip()))
    path = (parsed.path or "").rstrip("/")
    match = AD_ID_PATTERN.search(path)
    return match.group(1).lower() if match else None


def extract_jobmail_ad_id_from_url(url: str) -> str | None:
    """Extract numeric JobMail vacancy ID and prefix for DB storage."""

    parsed = urlparse(unescape((url or "").strip()))
    path = (parsed.path or "").rstrip("/")
    match = JOBMAIL_AD_ID_PATTERN.search(path)
    if not match:
        return None
    return f"jobmail-{match.group(1)}"


def is_jobmail_listing_url(url: str | None) -> bool:
    return extract_jobmail_ad_id_from_url(url or "") is not None


def normalize_jobmail_listing_url(url: str | None) -> str | None:
    """Return a clean absolute JobMail vacancy URL, or None if not a listing."""

    if not (url or "").strip():
        return None
    raw = unescape(url.strip())
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if JOBMAIL_HOST not in host:
        return None
    path = (parsed.path or "").rstrip("/")
    if not path or not extract_jobmail_ad_id_from_url(raw):
        return None
    return f"https://www.{JOBMAIL_HOST}{path}"


def is_junkmail_jobs_search_url(base_url: str | None) -> bool:
    """True for Junk Mail job keyword searches that embed JobMail vacancy cards."""

    path = (urlparse(base_url or "").path or "").lower()
    return path.startswith("/jobs/") and "/q-" in path


def is_generic_jobmail_page_title(title: str | None) -> bool:
    """True when JobMail returns a site-wide landing title instead of the vacancy."""

    text = (title or "").strip().lower()
    if not text:
        return False
    return (
        "jobs in south africa" in text
        or "jobs on job mail" in text
        or text in {"job mail", "jobmail", "jobs"}
    )


def title_from_jobmail_url(url: str | None) -> str | None:
    """Derive a readable job title from the JobMail listing URL slug."""

    path = (urlparse(unescape((url or "").strip())).path or "").rstrip("/")
    if not path:
        return None
    last = path.split("/")[-1]
    match = re.match(r"(.+)-id-\d+$", last, re.I)
    if not match:
        return None
    slug = match.group(1).strip("-")
    if not slug:
        return None
    return _title_case_slug(slug)


def extract_location_from_jobmail_url(url: str | None) -> str | None:
    """Best-effort city/province from JobMail listing URL path."""

    segments = [segment for segment in (urlparse(unescape((url or "").strip())).path or "").split("/") if segment]
    if len(segments) < 2:
        return None
    loc_slug = segments[-2].strip()
    if not loc_slug or loc_slug.lower() == "jobs" or "-id-" in loc_slug.lower():
        return None
    return _title_case_slug(loc_slug)


def build_jobmail_listing_description(
    title: str | None,
    location: str | None,
    description: str | None = None,
) -> str | None:
    """Use vacancy title/location when JobMail detail HTML has no description body."""

    body = (description or "").strip()
    if body:
        return body
    clean_title = (title or "").strip()
    if not clean_title or is_generic_jobmail_page_title(clean_title):
        return None
    loc = (location or "").strip()
    if loc and loc.lower() not in {"south africa"}:
        return f"{clean_title}. Location: {loc}."
    return f"{clean_title}."


def enrich_jobmail_listing_fields(listing: dict[str, Any]) -> dict[str, Any]:
    """Fill missing JobMail vacancy fields from URL/card data."""

    if not is_jobmail_listing_url(listing.get("url")):
        return listing
    enriched = dict(listing)
    url = enriched.get("url") or ""
    title = (enriched.get("title") or "").strip()
    if not title or is_generic_jobmail_page_title(title):
        recovered = title_from_jobmail_url(url)
        if recovered:
            enriched["title"] = recovered
    if not (enriched.get("location") or "").strip():
        location = extract_location_from_jobmail_url(url)
        if location:
            enriched["location"] = location
    description = build_jobmail_listing_description(
        enriched.get("title"),
        enriched.get("location"),
        enriched.get("description"),
    )
    if description:
        enriched["description"] = description
    attrs = dict(enriched.get("attributes") or {})
    attrs.setdefault("source", "junkmail_jobs_referral")
    attrs.setdefault("referral_url", url)
    enriched["attributes"] = attrs
    return enriched


def merge_search_card_with_detail(card: dict[str, Any], detail: dict[str, Any] | None) -> dict[str, Any]:
    """Merge search-card fields with detail fetch, keeping the better JobMail card data."""

    merged = dict(card)
    if not detail:
        return enrich_jobmail_listing_fields(merged)
    for key, value in detail.items():
        if value in (None, "", "Unknown"):
            continue
        if key == "title" and is_generic_jobmail_page_title(str(value)):
            continue
        if key == "location" and not str(value).strip():
            continue
        merged[key] = value
    return enrich_jobmail_listing_fields(merged)


def normalize_junkmail_listing_url(url: str | None) -> str | None:
    """
    Return a clean absolute Junk Mail listing URL, or None if not a listing.

    Example:
      https://www.junkmail.co.za/computers-and-gaming/gaming/gauteng/johannesburg/gaming-pc/5acad4b0d6cd4f92b6904a9a28b8e303
    """
    if not (url or "").strip():
        return None
    raw = unescape(url.strip())
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if JUNKMAIL_HOST not in host and host not in ("", JUNKMAIL_HOST):
        return None
    path = (parsed.path or "").rstrip("/")
    if is_junkmail_job_seeker_listing(path):
        return None
    if not path or not extract_ad_id_from_url(raw):
        return None
    return f"https://www.{JUNKMAIL_HOST}{path}"


def extract_canonical_listing_url(html: str) -> str | None:
    """Extract canonical listing URL from detail page HTML."""
    for pattern in (
        CANONICAL_LINK_PATTERN,
        CANONICAL_LINK_PATTERN_ALT,
        OG_URL_PATTERN,
        OG_URL_PATTERN_ALT,
        JSON_LD_URL_PATTERN,
    ):
        match = pattern.search(html or "")
        if match:
            candidate = normalize_junkmail_listing_url(unescape(match.group(1)))
            if candidate:
                return candidate
    return None


def resolve_listing_url(
    html: str,
    *,
    request_url: str | None = None,
    page_url: str | None = None,
) -> str | None:
    """Pick the best listing URL: canonical from HTML, then browser URL, then request URL."""
    for candidate in (
        extract_canonical_listing_url(html),
        normalize_jobmail_listing_url(page_url),
        normalize_junkmail_listing_url(page_url),
        normalize_jobmail_listing_url(request_url),
        normalize_junkmail_listing_url(request_url),
    ):
        if candidate:
            return candidate
    return None


def _title_case_slug(slug: str) -> str:
    return " ".join(part.capitalize() for part in slug.replace("-", " ").split())


def _normalize_province_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("-", " ").strip())


def canonical_province(text: str | None) -> str | None:
    if not text or not str(text).strip():
        return None
    return PROVINCE_CANONICAL.get(_normalize_province_key(str(text)))


def parse_junkmail_location(
    loc: str | None,
    url: str | None = None,
) -> tuple[str, str, str]:
    """
    Split Junk Mail location text into (province, city, suburb).

    Handles formats like:
      Gauteng - Johannesburg
      Reiger Park - Gauteng
      Region: Kwazulu Natal - Durban
      Johannesburg, Gauteng
      Reiger Park  (with URL fallback for province/city)
    """
    province = city = suburb = ""
    raw_loc = (loc or "").strip() if isinstance(loc, str) else ""

    if raw_loc:
        if " - " in raw_loc:
            parts = [part.strip() for part in raw_loc.split(" - ") if part.strip()]
            if len(parts) >= 2:
                left, right = parts[0], parts[1]
                left_prov = canonical_province(left)
                right_prov = canonical_province(right)
                if left_prov and not right_prov:
                    province, city = left_prov, right
                elif right_prov and not left_prov:
                    province, city = right_prov, left
                else:
                    city, suburb = left, right
            elif len(parts) == 1:
                only = parts[0]
                only_prov = canonical_province(only)
                if only_prov:
                    province = only_prov
                else:
                    city = only
        elif "," in raw_loc:
            parts = [part.strip() for part in raw_loc.split(",") if part.strip()]
            if len(parts) == 3:
                suburb, city = parts[0], parts[1]
                province = canonical_province(parts[2]) or ""
            elif len(parts) == 2:
                left_prov = canonical_province(parts[0])
                right_prov = canonical_province(parts[1])
                if right_prov and not left_prov:
                    city, province = parts[0], right_prov
                elif left_prov and not right_prov:
                    province, city = left_prov, parts[1]
                else:
                    suburb, city = parts[0], parts[1]
            elif len(parts) == 1:
                only_prov = canonical_province(parts[0])
                if only_prov:
                    province = only_prov
                else:
                    city = parts[0]
        else:
            only_prov = canonical_province(raw_loc)
            if only_prov:
                province = only_prov
            else:
                city = raw_loc

    if not province and url:
        url_loc = extract_location_from_junkmail_url(url)
        if url_loc:
            url_prov, url_city, url_suburb = parse_junkmail_location(url_loc)
            province = province or url_prov
            city = city or url_city
            suburb = suburb or url_suburb
            if raw_loc and raw_loc not in {province, city, suburb} and not suburb:
                suburb = raw_loc

    return (province or "", city or "", suburb or "")


def extract_location_from_junkmail_url(url: str | None) -> str | None:
    """
    Extract province and suburb/city from Junk Mail listing URL path.

    Example:
      .../computers-and-gaming/gaming/gauteng/reiger-park/gaming-pc/{uuid}
      -> Gauteng - Reiger Park
    """
    if not (url or "").strip():
        return None
    path = urlparse(unescape(url.strip())).path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 1 and AD_ID_PATTERN.search(f"/{segments[-1]}"):
        segments = segments[:-1]
    if len(segments) >= 1 and segments[-1].count("-") >= 2:
        # Drop listing title slug before UUID.
        segments = segments[:-1]
    for index, segment in enumerate(segments):
        if segment.lower() not in SA_PROVINCE_SLUGS:
            continue
        province = _title_case_slug(segment)
        if index + 1 < len(segments):
            city = _title_case_slug(segments[index + 1])
            return f"{province} - {city}"
        return province
    return None


def _parse_json_ld_objects(html: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for match in JSON_LD_SCRIPT_PATTERN.finditer(html or ""):
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            objects.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            objects.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                objects.extend(item for item in graph if isinstance(item, dict))
    return objects


def _clean_json_ld_text(value: str | None) -> str | None:
    if not value or not str(value).strip():
        return None
    text = unescape(str(value))
    text = text.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 20:
        return None
    if "junk mail marketplace" in text.lower():
        return None
    return text[:4000]


def _json_ld_field(objects: list[dict[str, Any]], field: str) -> str | None:
    for obj in objects:
        value = obj.get(field)
        if isinstance(value, str):
            cleaned = _clean_json_ld_text(value)
            if cleaned:
                return cleaned
    return None


def _json_ld_price(objects: list[dict[str, Any]]) -> int | None:
    for obj in objects:
        offers = obj.get("offers")
        if isinstance(offers, dict):
            price = offers.get("price")
            if price is not None:
                try:
                    return int(float(price))
                except (TypeError, ValueError):
                    pass
        price = obj.get("price")
        if price is not None:
            try:
                return int(float(price))
            except (TypeError, ValueError):
                pass
    return None


def _json_ld_location(objects: list[dict[str, Any]]) -> str | None:
    for obj in objects:
        address = obj.get("address")
        if isinstance(address, dict):
            parts: list[str] = []
            for key in ("addressLocality", "addressRegion"):
                value = address.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
            if parts:
                return " - ".join(parts)
            name = address.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return None


def _parse_map_marker_location(html: str) -> str | None:
    match = MAP_MARKER_LOCATION_PATTERN.search(html or "")
    if match:
        return match.group(1).strip()
    return None


def normalize_price(text: str | None) -> int | None:
    """Parse ZAR price from Junk Mail text (e.g. R 14 000, From R 2 500 Negotiable)."""

    if not text:
        return None
    match = re.search(r"\bR\s*([\d\s,]+)", str(text), re.I)
    if match:
        digits = re.sub(r"[\s,]", "", match.group(1))
        if digits.isdigit():
            return int(digits)
    cleaned = re.sub(r"[R\s,]", "", str(text))
    num_match = re.search(r"(\d+)", cleaned)
    return int(num_match.group(1)) if num_match else None


def junkmail_page_has_content(html: str, title: str | None = None) -> bool:
    """True when HTML looks like real Junk Mail content (not a CF interstitial)."""
    title_lower = (title or "").lower()
    if "junk mail" in title_lower and "just a moment" not in title_lower:
        return True
    html_lower = (html or "").lower()
    if len(html) > 20000 and "junkmail.co.za" in html_lower:
        if parse_search_cards(html, "https://www.junkmail.co.za/", "cf-check", path_slugs=[]):
            return True
    if AD_ID_PATTERN.search(html or "") and "<h1" in html_lower:
        return True
    return False


def is_cloudflare_challenge(html: str, title: str | None = None) -> bool:
    """True when Junk Mail returns a Cloudflare bot challenge instead of listings."""

    if junkmail_page_has_content(html, title):
        return False

    title_lower = (title or "").lower()
    if "just a moment" in title_lower:
        return True
    html_lower = html.lower()
    text_markers = (
        "performing security verification",
        "verifies you are not a bot",
        "security service to protect against malicious bots",
        "cf-browser-verification",
        "checking your browser",
    )
    return any(marker in html_lower for marker in text_markers)


def _looks_like_listing_path(path: str) -> bool:
    path_lower = path.lower()
    if any(frag in path_lower for frag in SKIP_PATH_FRAGMENTS):
        # Allow category paths that also contain listing UUID at end
        if not AD_ID_PATTERN.search(path):
            return False
    segments = [s for s in path.split("/") if s]
    return len(segments) >= 3


def _url_matches_search_category(path_or_url: str, path_slugs: list[str] | None) -> bool:
    slugs = [slug.lower() for slug in (path_slugs or []) if slug]
    if not slugs:
        return True
    path = path_or_url
    if path.startswith("http"):
        path = urlparse(path).path or ""
    path_lower = path.lower()
    return any(slug in path_lower for slug in slugs)


def is_junkmail_jobs_category_path(category_path: str | None) -> bool:
    """Jobs listings on Junk Mail do not use product-style /prmin-max/ URL segments."""

    path = (category_path or "").strip().strip("/").lower()
    if not path:
        return False
    return path == "jobs" or path.startswith("jobs/")


def build_junkmail_search_url(
    *,
    min_price: int | None = None,
    max_price: int | None = None,
    query: str | None = None,
    category_path: str | None = None,
    private_only: bool = False,
    sort: str | None = "so-latest",
    base: str = JUNKMAIL_BASE_URL,
) -> str:
    """
    Build a Junk Mail search URL with optional price segment.

    Examples:
      /pr10000-20000/computers-and-gaming/q-gaming%20pc/so-latest
      /jobs/q-full%20stack%20developer
      /pr5000-75000/bikes/private-adverts/so-latest
    """
    parts = [base.rstrip("/")]
    jobs_search = is_junkmail_jobs_category_path(category_path)
    if not jobs_search and min_price is not None and max_price is not None:
        parts.append(f"pr{int(min_price)}-{int(max_price)}")
    if category_path:
        parts.extend(segment.strip("/") for segment in category_path.split("/") if segment.strip())
    if query and query.strip():
        parts.append("q-" + quote(query.strip(), safe=""))
    if private_only:
        parts.append("private-adverts")
    if sort and not jobs_search:
        parts.append(sort.strip("/"))
    return "/".join(parts)


def search_page_url(base_url: str, page_num: int) -> str:
    """Build paginated Junk Mail search URL (/page2, /page3, ...)."""

    base = (base_url or "").rstrip("/")
    if page_num <= 1:
        return base
    return f"{base}/page{page_num}"


def parse_search_cards(
    html: str,
    base_url: str,
    category: str,
    path_slugs: list[str] | None = None,
) -> list[dict[str, Any]]:
    """
    Parse Junk Mail search results for listing cards.

    Job searches on junkmail.co.za often embed JobMail referral links in the HTML.
    Those are parsed here for title/location only — the crawler never opens jobmail.co.za.
    Native Junk Mail listings use junkmail.co.za URLs with a 32-char hex UUID.
    """

    results: list[dict[str, Any]] = []
    seen_ad_ids: set[str] = set()

    for match in LISTING_HREF_PATTERN.finditer(html):
        raw_path, ad_id = match.group(1), match.group(2).lower()
        if ad_id in seen_ad_ids:
            continue
        if raw_path.startswith("http"):
            full_url = unescape(raw_path)
            path = urlparse(full_url).path or ""
        else:
            path = unescape(raw_path)
            full_url = urljoin(f"{JUNKMAIL_BASE_URL}/", path.lstrip("/"))

        full_url = normalize_junkmail_listing_url(full_url)
        if not full_url:
            continue
        path = urlparse(full_url).path or ""
        if is_junkmail_job_seeker_listing(path):
            continue
        if not _looks_like_listing_path(path):
            continue
        if not _url_matches_search_category(path, path_slugs):
            continue

        seen_ad_ids.add(ad_id)
        card = _extract_card_context(html, match.start(), match.end(), full_url, ad_id, category)
        if card:
            results.append(card)

    if is_junkmail_jobs_search_url(base_url):
        for match in JOBMAIL_LISTING_HREF_PATTERN.finditer(html):
            raw_url = unescape(match.group(1))
            full_url = normalize_jobmail_listing_url(raw_url)
            if not full_url:
                continue
            ad_id = extract_jobmail_ad_id_from_url(full_url)
            if not ad_id or ad_id in seen_ad_ids:
                continue
            seen_ad_ids.add(ad_id)
            card = _extract_card_context(html, match.start(), match.end(), full_url, ad_id, category)
            if card:
                attrs = dict(card.get("attributes") or {})
                attrs["source"] = "junkmail_jobs_referral"
                attrs["discovered_on"] = base_url
                attrs["referral_url"] = full_url
                card["attributes"] = attrs
                card = enrich_jobmail_listing_fields(card)
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
    window_start = max(0, start - 900)
    window_end = min(len(html), end + 1600)
    block = html[window_start:window_end]

    title = None
    for pattern in (
        r'class="[^"]*title[^"]*"[^>]*>([^<]{5,160})<',
        r'alt="([^"]{5,160})"',
        r">([^<]{10,140})</a>",
    ):
        title_match = re.search(pattern, block, re.I)
        if title_match:
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", title_match.group(1))).strip()
            if len(title) >= 5 and "junkmail" not in title.lower():
                break
            title = None

    price = None
    price_match = re.search(r'"price"\s*:\s*(\d+)', block)
    if price_match:
        price = int(price_match.group(1))
    if price is None:
        price_match = re.search(r"(?:From\s+)?\bR\s*[\d\s,]+", block, re.I)
        if price_match:
            price = normalize_price(price_match.group(0))

    location = _parse_map_marker_location(block)
    if not location:
        loc_match = re.search(r"Region:\s*([^<\n]+)", block, re.I)
        if loc_match:
            location = loc_match.group(1).strip()
    if not location:
        loc_match = re.search(r'class="[^"]*location[^"]*"[^>]*>([^<]+)<', block, re.I)
        if loc_match:
            location = loc_match.group(1).strip()
    if not location:
        location = extract_location_from_junkmail_url(url)

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


def _parse_seller_type(html: str) -> str | None:
    match = re.search(r"Ad Type:\s*([^<\n]+)", html, re.I)
    if match:
        value = match.group(1).strip().lower()
        if "owner" in value or "private" in value:
            return "Owner"
        if "business" in value or "dealer" in value:
            return "Business"
        return match.group(1).strip()
    snippet = html.lower()[:4000]
    if "ad type" in snippet and "owner" in snippet:
        return "Owner"
    if "business" in snippet:
        return "Business"
    return None


def _parse_region(html: str, url: str | None = None) -> str | None:
    json_ld = _parse_json_ld_objects(html)
    location = _json_ld_location(json_ld)
    if location:
        return location
    match = _parse_map_marker_location(html)
    if match:
        return match
    match = re.search(r"Region:\s*([^<\n]+)", html, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r'"addressRegion"\s*:\s*"([^"]+)"', html)
    if match:
        return match.group(1).strip()
    return extract_location_from_junkmail_url(url)


def extract_detail_images(html: str, max_images: int = 10) -> list[str]:
    """Extract image URLs from Junk Mail listing detail page."""

    seen: set[str] = set()
    images: list[str] = []
    patterns = (
        r'(https?://[^"\s<>]+junkmail[^"\s<>]+\.(?:jpg|jpeg|png|webp)[^"\s<>]*)',
        r'(https?://[^"\s<>]+\.(?:jpg|jpeg|png|webp)[^"\s<>]*)',
    )
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I):
            url = match.group(1).split("?")[0]
            if url in seen:
                continue
            if any(skip in url.lower() for skip in ("logo", "icon", "sprite", "avatar")):
                continue
            seen.add(url)
            images.append(match.group(1))
            if len(images) >= max_images:
                return images
    return images


def parse_detail_page(html: str, url: str, category: str) -> dict[str, Any] | None:
    """Parse Junk Mail or JobMail listing detail page."""

    if is_cloudflare_challenge(html):
        return None

    if is_jobmail_listing_url(url):
        return _parse_jobmail_detail_page(html, url, category)

    ad_id = extract_ad_id_from_url(url)
    if not ad_id:
        return None

    resolved_url = resolve_listing_url(html, request_url=url)
    if not resolved_url:
        return None
    ad_id = extract_ad_id_from_url(resolved_url) or ad_id

    json_ld = _parse_json_ld_objects(html)

    title = None
    match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
    if not title:
        title = _json_ld_field(json_ld, "name")
    if not title:
        match = re.search(r'"name"\s*:\s*"([^"]{5,200})"', html)
        if match:
            title = match.group(1).strip()

    price = _json_ld_price(json_ld)
    if price is None:
        match = re.search(r'"price"\s*:\s*(\d+)', html)
        if match:
            price = int(match.group(1))
    if price is None:
        match = re.search(r"(?:From\s+)?\bR\s*[\d\s,]+(?:\s+Negotiable)?", html, re.I)
        if match:
            price = normalize_price(match.group(0))

    location = _parse_region(html, resolved_url)
    seller = _parse_seller_type(html)

    condition = None
    match = re.search(r"Condition:\s*([^<\n]+)", html, re.I)
    if match:
        condition = match.group(1).strip()

    description = _json_ld_field(json_ld, "description")
    if not description:
        for pattern in (
            r'class="[^"]*description[^"]*"[^>]*>([\s\S]*?)</div>',
            r'itemprop="description"[^>]*>([\s\S]*?)</',
        ):
            match = re.search(pattern, html, re.I)
            if match:
                raw = match.group(1)
                description = re.sub(r"<[^>]+>", " ", raw)
                description = re.sub(r"\s+", " ", description).strip()
                if description and "junk mail marketplace" not in description.lower():
                    description = description[:4000]
                    break
                description = None
    if not description:
        match = re.search(r'"description"\s*:\s*"([^"]{20,4000})"', html, re.I)
        if match:
            description = _clean_json_ld_text(match.group(1))

    posted_at = extract_posted_at(html)
    image_urls = extract_detail_images(html)
    attributes, signals = extract_listing_attributes(title, description)
    attributes["image_count"] = len(image_urls)

    if not title and not price and not description:
        return None

    return {
        "ad_id": ad_id,
        "url": resolved_url,
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


def _parse_jobmail_detail_page(html: str, url: str, category: str) -> dict[str, Any] | None:
    """Parse a JobMail vacancy detail page linked from Junk Mail job search."""

    resolved_url = normalize_jobmail_listing_url(url)
    if not resolved_url:
        return None
    ad_id = extract_jobmail_ad_id_from_url(resolved_url)
    if not ad_id:
        return None

    json_ld = _parse_json_ld_objects(html)

    title = None
    match = re.search(r"<h1[^>]*>([^<]+)</h1>", html)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
    if not title:
        title = _json_ld_field(json_ld, "name")
    if not title:
        match = re.search(r'class="[^"]*card-title[^"]*"[^>]*>([^<]+)<', html, re.I)
        if match:
            title = match.group(1).strip()
    if is_generic_jobmail_page_title(title):
        title = title_from_jobmail_url(resolved_url) or title

    location = _parse_region(html, resolved_url)
    if not location:
        match = re.search(r"fa-map-marker[^>]*></i>\s*([^<]+)", html, re.I)
        if match:
            location = match.group(1).strip()
    if not location:
        location = extract_location_from_jobmail_url(resolved_url)

    description = _json_ld_field(json_ld, "description")
    if not description:
        for pattern in (
            r'class="[^"]*job-description[^"]*"[^>]*>([\s\S]*?)</div>',
            r'class="[^"]*description[^"]*"[^>]*>([\s\S]*?)</div>',
            r'itemprop="description"[^>]*>([\s\S]*?)</',
        ):
            match = re.search(pattern, html, re.I)
            if match:
                raw = match.group(1)
                description = re.sub(r"<[^>]+>", " ", raw)
                description = re.sub(r"\s+", " ", description).strip()
                if len(description) >= 20:
                    description = description[:4000]
                    break
                description = None

    description = build_jobmail_listing_description(title, location, description)

    posted_at = extract_posted_at(html)
    image_urls = extract_detail_images(html)
    attributes, signals = extract_listing_attributes(title, description)
    attributes["image_count"] = len(image_urls)
    attributes["source"] = "jobmail"

    if not title and not description:
        return None

    return {
        "ad_id": ad_id,
        "url": resolved_url,
        "title": title or "Unknown",
        "price": None,
        "location": location,
        "seller": "Employer",
        "condition": None,
        "description": description,
        "posted_at": posted_at,
        "attributes": attributes,
        "signals": signals,
        "image_urls": image_urls,
        "category": category,
    }
