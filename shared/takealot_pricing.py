"""Takealot HTTP verify via public product-details API (no browser)."""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import requests

_LOG = logging.getLogger("products.scraper")

_API_BASE = "https://api.takealot.com/rest/v-1-16-0"
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-ZA,en;q=0.9",
    "Referer": "https://www.takealot.com/",
    "Origin": "https://www.takealot.com",
    "Connection": "close",
}


def extract_plid(url: str) -> str | None:
    """Extract numeric PLID from a Takealot product URL."""
    path = (urlparse(url or "").path or "").strip("/")
    m = re.search(r"/PLID(\d+)(?:\?|$|/)", path, re.I) or re.search(r"PLID(\d+)", path, re.I)
    return m.group(1) if m else None


def _item_label(item: dict[str, Any]) -> str:
    for key in ("variant_name", "display_name", "name", "title"):
        val = item.get(key)
        if val:
            return str(val).strip()
    variant = item.get("variant")
    if isinstance(variant, dict):
        for key in ("name", "display_name", "value"):
            val = variant.get(key)
            if val:
                return str(val).strip()
    return ""


def pick_takealot_buybox_item(
    items: list[dict[str, Any]],
    *,
    price_hint: float | None = None,
    label_hint: str | None = None,
) -> dict[str, Any] | None:
    if not items:
        return None

    pool = list(items)
    label_hint_l = (label_hint or "").strip().lower()

    if label_hint_l:
        matched = [
            item
            for item in pool
            if label_hint_l in _item_label(item).lower()
            or label_hint_l in str(item.get("variant") or "").lower()
        ]
        if matched:
            pool = matched

    if price_hint is not None:
        matched = []
        for item in pool:
            try:
                if abs(float(item.get("price") or 0) - float(price_hint)) < 0.02:
                    matched.append(item)
            except (TypeError, ValueError):
                continue
        if matched:
            pool = matched

    selected = [item for item in pool if item.get("is_selected")]
    if selected:
        pool = selected

    available = [item for item in pool if item.get("is_add_to_cart_available") is not False]
    if available:
        pool = available

    return pool[0] if pool else None


def extract_product_from_takealot_api(
    payload: dict[str, Any],
    *,
    price_hint: float | None = None,
    label_hint: str | None = None,
) -> dict[str, Any] | None:
    buybox = payload.get("buybox") if isinstance(payload.get("buybox"), dict) else {}
    items = buybox.get("items") if isinstance(buybox.get("items"), list) else []
    item = pick_takealot_buybox_item(items, price_hint=price_hint, label_hint=label_hint)
    if not item:
        return None

    title = (payload.get("title") or (payload.get("core") or {}).get("title") or "").strip() or None
    try:
        sale_price = float(item.get("price"))
    except (TypeError, ValueError):
        sale_price = None
    if sale_price is not None and sale_price <= 0:
        sale_price = None

    in_stock = bool(item.get("is_add_to_cart_available", True))
    variant_label = _item_label(item) or None
    return {
        "goodsName": title,
        "salePrice": sale_price,
        "in_stock": in_stock,
        "variantSize": variant_label,
    }


def fetch_takealot_product_via_api(
    url: str,
    *,
    timeout: float = 25.0,
    price_hint: float | None = None,
    label_hint: str | None = None,
) -> dict[str, Any] | None:
    plid = extract_plid(url)
    if not plid:
        return None
    api_url = f"{_API_BASE}/product-details/PLID{plid}?platform=desktop"
    try:
        resp = requests.get(
            api_url,
            headers=_FETCH_HEADERS,
            timeout=(5.0, timeout),
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return None
        payload = resp.json()
    except Exception as exc:
        _LOG.debug("Takealot API fetch failed for %s: %s", url, exc)
        return None
    if not isinstance(payload, dict):
        return None
    return extract_product_from_takealot_api(
        payload,
        price_hint=price_hint,
        label_hint=label_hint,
    )
