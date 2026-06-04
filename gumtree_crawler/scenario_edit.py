"""Normalize scenario edits from the Gumtree crawler UI before saving to SQLite."""
from __future__ import annotations

from typing import Any

from junkmail_crawler.scenario_edit import (
    SEARCH_KEYWORD_FIELDS,
    normalize_keyword_fields,
    normalize_search_keyword_fields,
    parse_comma_list,
)


def _int_price(raw: Any, field: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def normalize_gumtree_searches(searches: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not searches:
        raise ValueError("At least one search is required")

    out: list[dict[str, Any]] = []
    for index, raw in enumerate(searches):
        if not isinstance(raw, dict):
            raise ValueError(f"Search row {index + 1} is invalid")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"Search row {index + 1} needs a name")
        url = str(raw.get("url") or "").strip()
        if not url or "gumtree.co.za" not in url.lower():
            raise ValueError(f"Search '{name}' needs a valid Gumtree URL (gumtree.co.za)")
        category = str(raw.get("category") or "").strip()
        if not category:
            raise ValueError(f"Search '{name}' needs a category label")
        min_price = _int_price(raw.get("min_price"), "min_price")
        max_price = _int_price(raw.get("max_price"), "max_price")
        if min_price > max_price:
            raise ValueError(f"Search '{name}': min price cannot exceed max price")
        path_slugs_raw = raw.get("path_slugs")
        if isinstance(path_slugs_raw, str):
            path_slugs = parse_comma_list(path_slugs_raw)
        elif isinstance(path_slugs_raw, list):
            path_slugs = [str(v).strip().lower() for v in path_slugs_raw if str(v).strip()]
        else:
            path_slugs = []
        seller_type = str(raw.get("seller_type") or "owner").strip() or "owner"
        row: dict[str, Any] = {
            "name": name,
            "url": url,
            "category": category,
            "min_price": min_price,
            "max_price": max_price,
            "seller_type": seller_type,
            "path_slugs": path_slugs,
        }
        for field in SEARCH_KEYWORD_FIELDS:
            if field in raw:
                row[field] = raw[field]
        out.append(normalize_search_keyword_fields(row))
    return out


def normalize_gumtree_scenario_patch(updates: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize a PATCH body for Gumtree scenario config."""

    out = dict(updates or {})
    if "enabled" in out:
        out["enabled"] = bool(out["enabled"])
    for field in ("min_price", "max_price"):
        if field in out:
            out[field] = _int_price(out[field], field)
    if "min_price" in out and "max_price" in out and out["min_price"] > out["max_price"]:
        raise ValueError("Scenario min price cannot exceed max price")
    if "searches" in out:
        out["searches"] = normalize_gumtree_searches(out["searches"])
    keyword_fields = ("required_keywords_all", "excluded_keywords", "required_any_groups")
    if any(k in out for k in keyword_fields):
        out = normalize_keyword_fields(out)
    return out
