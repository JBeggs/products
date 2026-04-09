"""
Scenario evaluation and location scoring for Gumtree crawler listings.
"""
from __future__ import annotations

from typing import Any


def _normalize_text(value: str | None) -> str:
    return " ".join((value or "").lower().split())


def _normalize_token(value: str | None) -> str:
    return "".join(ch for ch in _normalize_text(value) if ch.isalnum() or ch == " ").strip()


def get_listing_text(listing: dict[str, Any]) -> str:
    parts = [
        listing.get("title") or "",
        listing.get("description") or "",
        listing.get("location") or "",
    ]
    return _normalize_text(" ".join(parts))


def score_location(
    province: str,
    city: str,
    suburb: str,
    preferences: dict[str, Any] | None,
) -> dict[str, Any]:
    """Score a location using editable province/city/suburb preferences."""

    prefs = preferences or {}
    weights = prefs.get("weights") or {}
    province_weight = float(weights.get("province", 20))
    city_weight = float(weights.get("city", 35))
    suburb_weight = float(weights.get("suburb", 45))

    pref_provinces = [_normalize_token(v) for v in prefs.get("preferred_provinces") or [] if str(v).strip()]
    pref_cities = [_normalize_token(v) for v in prefs.get("preferred_cities") or [] if str(v).strip()]
    pref_suburbs = [_normalize_token(v) for v in prefs.get("preferred_suburbs") or [] if str(v).strip()]

    province_norm = _normalize_token(province)
    city_norm = _normalize_token(city)
    suburb_norm = _normalize_token(suburb)

    score = 0.0
    matched: list[str] = []

    def _rank_bonus(items: list[str], value: str) -> float:
        if not value or value not in items:
            return 0.0
        idx = items.index(value)
        size = max(len(items), 1)
        return max(0.4, (size - idx) / size)

    if province_norm and province_norm in pref_provinces:
        score += province_weight * _rank_bonus(pref_provinces, province_norm)
        matched.append("province")
    if city_norm and city_norm in pref_cities:
        score += city_weight * _rank_bonus(pref_cities, city_norm)
        matched.append("city")
    if suburb_norm and suburb_norm in pref_suburbs:
        score += suburb_weight * _rank_bonus(pref_suburbs, suburb_norm)
        matched.append("suburb")

    if "suburb" in matched:
        grade = "green"
    elif "city" in matched:
        grade = "green"
    elif "province" in matched:
        grade = "yellow"
    else:
        grade = "red"

    return {
        "score": round(score, 2),
        "grade": grade,
        "matched_levels": matched,
    }


def evaluate_listing_for_scenario(listing: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    """
    Evaluate a listing against one scenario.

    Visibility is strict: a listing must satisfy all hard requirements to become visible
    in the scenario tab.
    """

    text = get_listing_text(listing)
    attrs = listing.get("attributes") or {}
    seller_text = _normalize_text(listing.get("seller"))
    reasons: list[str] = []

    price = listing.get("price")
    min_price = scenario.get("min_price")
    max_price = scenario.get("max_price")
    if price is None:
        reasons.append("missing price")
    else:
        if min_price is not None and price < int(min_price):
            reasons.append(f"price below R{min_price}")
        if max_price is not None and price > int(max_price):
            reasons.append(f"price above R{max_price}")

    for field_name in scenario.get("required_fields_all") or []:
        if field_name == "price":
            if listing.get("price") is None:
                reasons.append("missing price")
        elif field_name == "location":
            if not (listing.get("location") or "").strip():
                reasons.append("missing location")
        elif field_name == "description":
            if not (listing.get("description") or "").strip():
                reasons.append("missing description")
        elif field_name == "posted_at":
            if not (listing.get("posted_at") or "").strip():
                reasons.append("missing posted date")

    for field_name in scenario.get("required_attribute_keys") or []:
        if not attrs.get(field_name):
            reasons.append(f"missing {field_name}")

    year = attrs.get("year")
    if scenario.get("require_year") and not year:
        reasons.append("missing year")
    if year is not None:
        min_year = scenario.get("min_year")
        max_year = scenario.get("max_year")
        if min_year is not None and int(year) < int(min_year):
            reasons.append(f"year before {min_year}")
        if max_year is not None and int(year) > int(max_year):
            reasons.append(f"year after {max_year}")

    for key, min_value in (scenario.get("min_numeric") or {}).items():
        current = attrs.get(key)
        if current is None:
            reasons.append(f"missing {key}")
        elif float(current) < float(min_value):
            reasons.append(f"{key} below {min_value}")

    for key, max_value in (scenario.get("max_numeric") or {}).items():
        current = attrs.get(key)
        if current is None:
            reasons.append(f"missing {key}")
        elif float(current) > float(max_value):
            reasons.append(f"{key} above {max_value}")

    for keyword in scenario.get("required_keywords_all") or []:
        if _normalize_text(keyword) not in text:
            reasons.append(f"missing keyword: {keyword}")

    any_groups = scenario.get("required_any_groups") or []
    if any_groups:
        group_match = False
        for group in any_groups:
            if any(_normalize_text(keyword) in text for keyword in group):
                group_match = True
                break
        if not group_match:
            reasons.append("missing required keyword group")

    for keyword in scenario.get("excluded_keywords") or []:
        if _normalize_text(keyword) in text:
            reasons.append(f"excluded keyword: {keyword}")

    allowlist = [_normalize_text(v) for v in scenario.get("seller_allowlist") or [] if v]
    denylist = [_normalize_text(v) for v in scenario.get("seller_denylist") or [] if v]
    if allowlist and seller_text and not any(v in seller_text for v in allowlist):
        reasons.append("seller not allowed")
    if denylist and seller_text and any(v in seller_text for v in denylist):
        reasons.append("seller denied")

    urgency_keywords = scenario.get("urgency_keywords") or []
    strong_urgency_keywords = scenario.get("strong_urgency_keywords") or []
    urgency_hits = [k for k in urgency_keywords if _normalize_text(k) in text]
    strong_hits = [k for k in strong_urgency_keywords if _normalize_text(k) in text]

    price_score = 0.0
    if price is not None and min_price is not None and max_price is not None and int(max_price) > int(min_price):
        ratio = (int(max_price) - int(price)) / max(int(max_price) - int(min_price), 1)
        price_score = max(0.0, min(100.0, ratio * 100.0))

    match_penalty = min(len(reasons) * 12, 90)
    match_score = max(0.0, 100.0 - match_penalty)
    urgency_score = min(100.0, len(urgency_hits) * 18 + len(strong_hits) * 32)
    visible = len(reasons) == 0

    special_state = None
    if strong_hits:
        special_state = "black"
    elif visible and match_score >= 85 and price_score >= 60:
        special_state = "gold"

    return {
        "visible": visible,
        "match_score": round(match_score, 2),
        "price_score": round(price_score, 2),
        "urgency_score": round(urgency_score, 2),
        "special_state": special_state,
        "reasons": reasons,
        "urgency_hits": urgency_hits,
        "strong_urgency_hits": strong_hits,
    }
