"""Configurable crawl limits for Junk Mail (stored in crawler_filters)."""
from __future__ import annotations

from typing import Any

DEFAULT_MAX_PAGES_PER_SEARCH = 5
DEFAULT_MAX_STORED_PER_SEARCH = 50
DEFAULT_STORE_ONLY_SCENARIO_MATCHES = False


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_int(value: str | None, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def get_crawl_limits() -> dict[str, Any]:
    from .db import get_crawler_filters

    raw = get_crawler_filters()
    return {
        "max_pages_per_search": _parse_int(
            raw.get("max_pages_per_search"), DEFAULT_MAX_PAGES_PER_SEARCH, minimum=1
        ),
        "max_stored_per_search": _parse_int(
            raw.get("max_stored_per_search"), DEFAULT_MAX_STORED_PER_SEARCH, minimum=1
        ),
        "store_only_scenario_matches": _parse_bool(
            raw.get("store_only_scenario_matches"), DEFAULT_STORE_ONLY_SCENARIO_MATCHES
        ),
    }


def int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def price_in_search_range(price: int | None, search: dict[str, Any]) -> bool:
    """True if card price is within the search target range."""

    from junkmail_crawler.parsers import is_junkmail_jobs_category_path

    category_path = search.get("category_path")
    url = str(search.get("url") or "")
    if is_junkmail_jobs_category_path(category_path) or "/jobs/" in url.lower():
        return True

    min_p = int_or_none(search.get("min_price"))
    max_p = int_or_none(search.get("max_price"))
    if min_p is not None or max_p is not None:
        if price is None:
            return False
    elif price is None:
        return True
    if min_p is not None and price < min_p:
        return False
    if max_p is not None and price > max_p:
        return False
    return True
