"""Normalize scenario edits from the crawler UI before saving to SQLite."""
from __future__ import annotations

from typing import Any

from junkmail_crawler.parsers import build_junkmail_search_url

KEYWORD_LIST_FIELDS = ("required_keywords_all", "excluded_keywords")
SEARCH_KEYWORD_FIELDS = (*KEYWORD_LIST_FIELDS, "required_any_groups")


def parse_comma_list(text: str | None) -> list[str]:
    if not text:
        return []
    return [part.strip().lower() for part in str(text).split(",") if part.strip()]


def format_comma_list(items: list[str] | None) -> str:
    if not items:
        return ""
    return ", ".join(str(v).strip() for v in items if str(v).strip())


def parse_or_groups(text: str | None) -> list[list[str]]:
    if not text:
        return []
    groups: list[list[str]] = []
    for line in str(text).splitlines():
        row = parse_comma_list(line)
        if row:
            groups.append(row)
    return groups


def format_or_groups(groups: list[list[str]] | None) -> str:
    if not groups:
        return ""
    lines: list[str] = []
    for group in groups:
        if not group:
            continue
        lines.append(format_comma_list(group))
    return "\n".join(lines)


def _normalize_keyword_value(field: str, val: Any) -> list[Any]:
    if field in KEYWORD_LIST_FIELDS:
        if isinstance(val, str):
            return parse_comma_list(val)
        if isinstance(val, list):
            return [str(v).strip().lower() for v in val if str(v).strip()]
        return []
    if field == "required_any_groups":
        if isinstance(val, str):
            return parse_or_groups(val)
        if isinstance(val, list):
            groups: list[list[str]] = []
            for item in val:
                if isinstance(item, str):
                    row = parse_comma_list(item)
                elif isinstance(item, list):
                    row = [str(v).strip().lower() for v in item if str(v).strip()]
                else:
                    row = []
                if row:
                    groups.append(row)
            return groups
        return []
    raise ValueError(f"Unknown keyword field: {field}")


def normalize_keyword_fields(updates: dict[str, Any]) -> dict[str, Any]:
    """Normalize keyword list fields from UI strings or lists."""

    out = dict(updates)
    for field in KEYWORD_LIST_FIELDS:
        if field not in out:
            continue
        out[field] = _normalize_keyword_value(field, out[field])
    if "required_any_groups" in out:
        out["required_any_groups"] = _normalize_keyword_value("required_any_groups", out["required_any_groups"])
    return out


def normalize_search_keyword_fields(search: dict[str, Any]) -> dict[str, Any]:
    """Normalize optional per-search keyword overrides on one search row."""

    out = dict(search)
    for field in SEARCH_KEYWORD_FIELDS:
        if field not in out:
            continue
        out[field] = _normalize_keyword_value(field, out[field])
    return out


def _int_price(raw: Any, field: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def normalize_junkmail_searches(searches: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not searches:
        raise ValueError("At least one search is required")

    out: list[dict[str, Any]] = []
    for index, raw in enumerate(searches):
        if not isinstance(raw, dict):
            raise ValueError(f"Search row {index + 1} is invalid")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError(f"Search row {index + 1} needs a name")
        category = str(raw.get("category") or "").strip()
        if not category:
            raise ValueError(f"Search '{name}' needs a category label")
        min_price = _int_price(raw.get("min_price"), "min_price")
        max_price = _int_price(raw.get("max_price"), "max_price")
        if min_price > max_price:
            raise ValueError(f"Search '{name}': min price cannot exceed max price")
        query = str(raw.get("query") or "").strip() or None
        category_path = str(raw.get("category_path") or "").strip().strip("/") or None
        private_only = bool(raw.get("private_only"))
        path_slugs_raw = raw.get("path_slugs")
        if isinstance(path_slugs_raw, str):
            path_slugs = parse_comma_list(path_slugs_raw)
        elif isinstance(path_slugs_raw, list):
            path_slugs = [str(v).strip().lower() for v in path_slugs_raw if str(v).strip()]
        else:
            path_slugs = []
        url = build_junkmail_search_url(
            min_price=min_price,
            max_price=max_price,
            query=query,
            category_path=category_path,
            private_only=private_only,
        )
        seller_type = str(raw.get("seller_type") or "owner").strip() or "owner"
        row: dict[str, Any] = {
            "name": name,
            "url": url,
            "category": category,
            "min_price": min_price,
            "max_price": max_price,
            "seller_type": seller_type,
            "path_slugs": path_slugs,
            "query": query,
            "category_path": category_path,
            "private_only": private_only,
        }
        for field in SEARCH_KEYWORD_FIELDS:
            if field in raw:
                row[field] = raw[field]
        out.append(normalize_search_keyword_fields(row))
    return out


def normalize_junkmail_scenario_patch(updates: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize a PATCH body for Junk Mail scenario config."""

    out = dict(updates or {})
    if "enabled" in out:
        out["enabled"] = bool(out["enabled"])
    for field in ("min_price", "max_price"):
        if field in out:
            out[field] = _int_price(out[field], field)
    if "min_price" in out and "max_price" in out and out["min_price"] > out["max_price"]:
        raise ValueError("Scenario min price cannot exceed max price")
    if "searches" in out:
        out["searches"] = normalize_junkmail_searches(out["searches"])
    if any(k in out for k in (*KEYWORD_LIST_FIELDS, "required_any_groups")):
        out = normalize_keyword_fields(out)
    return out
