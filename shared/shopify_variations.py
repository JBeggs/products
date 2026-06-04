"""Shopify variant helpers for HTTP verify (color/size matching)."""
from __future__ import annotations

import re
from typing import Any

import requests

from shared.woocommerce_variations import product_size_hint

_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept": "application/json,text/html,application/xhtml+xml",
    "Connection": "close",
}

_SIZE_TOKENS = frozenset({
    "xxs", "xs", "s", "m", "l", "xl", "xxl", "2xl", "3xl", "4xl", "5xl",
    "small", "medium", "large", "xlarge", "x-large", "xx-large",
})
_DEFAULT_SIZE_PREFS = ("l", "large", "lg", "xl", "m", "s")


def shopify_product_json_url(url: str) -> str:
    base = url.split("#")[0].split("?")[0].rstrip("/")
    if base.endswith(".json"):
        return base
    return f"{base}.json"


def _norm(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _option_values(variant: dict[str, Any]) -> list[str]:
    return [
        str(variant.get(key)).strip()
        for key in ("option1", "option2", "option3")
        if variant.get(key)
    ]


def _values_match(option: str, hint: str) -> bool:
    o = _norm(option)
    h = _norm(hint)
    if not o or not h:
        return False
    return o == h or h in o or o in h


def color_hint_from_product(product: dict[str, Any] | None) -> str | None:
    """Color from trailing ' - Brown' style product names."""
    if not product:
        return None
    for key in ("name", "short_description"):
        text = (product.get(key) or "").strip()
        if " - " in text:
            tail = text.rsplit(" - ", 1)[1].strip()
            if tail and len(tail) <= 40:
                return tail
    return None


def size_hint_from_product(product: dict[str, Any] | None) -> str | None:
    if not product:
        return None
    for item in product.get("variants") or []:
        if not isinstance(item, dict):
            continue
        for key in ("option", "volume", "size", "pack"):
            val = item.get(key)
            if val:
                return str(val).strip()
    text = product_size_hint(product)
    m = re.search(
        r"\b(xxs|xs|s|m|l|xl|xxl|2xl|3xl|4xl|5xl|small|medium|large|x-large|xx-large)\b",
        text,
        re.I,
    )
    return m.group(1) if m else None


def _has_size_options(variants: list[dict[str, Any]]) -> bool:
    for variant in variants:
        for opt in _option_values(variant):
            if _norm(opt) in _SIZE_TOKENS:
                return True
    return False


def pick_shopify_variant(
    variants: list[dict[str, Any]],
    *,
    color_hint: str | None = None,
    size_hint: str | None = None,
    price_hint: float | None = None,
) -> dict[str, Any] | None:
    if not variants:
        return None

    pool = list(variants)

    if color_hint:
        matched = [v for v in pool if any(_values_match(o, color_hint) for o in _option_values(v))]
        if matched:
            pool = matched

    if size_hint:
        matched = [v for v in pool if any(_values_match(o, size_hint) for o in _option_values(v))]
        if matched:
            pool = matched
    elif _has_size_options(variants):
        for pref in _DEFAULT_SIZE_PREFS:
            matched = [v for v in pool if any(_norm(o) == pref for o in _option_values(v))]
            if matched:
                pool = matched
                break

    if price_hint is not None:
        matched = []
        for variant in pool:
            try:
                if abs(float(variant.get("price") or 0) - float(price_hint)) < 0.02:
                    matched.append(variant)
            except (TypeError, ValueError):
                continue
        if matched:
            pool = matched

    available = [v for v in pool if v.get("available") is not False]
    if available:
        pool = available

    return pool[0] if pool else None


def extract_from_shopify_variant(product: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    title = (product.get("title") or "").strip() or None
    try:
        sale_price = float(variant.get("price") or 0)
    except (TypeError, ValueError):
        sale_price = None
    if sale_price is not None and sale_price <= 0:
        sale_price = None
    available = variant.get("available")
    in_stock = True if available is None else bool(available)
    opts = _option_values(variant)
    variant_label = " / ".join(opts) if opts else (variant.get("title") or None)
    gallery: list[str] = []
    for img in product.get("images") or []:
        if isinstance(img, dict):
            src = (img.get("src") or "").strip()
            if src:
                gallery.append(src)
    result = {
        "goodsName": title,
        "salePrice": sale_price,
        "in_stock": in_stock,
        "variantSize": variant_label,
    }
    if gallery:
        result["gallery"] = gallery
    return result


def fetch_shopify_product_via_json(
    url: str,
    *,
    timeout: float = 25.0,
    product: dict[str, Any] | None = None,
    price_hint: float | None = None,
) -> dict[str, Any] | None:
    """Fetch Shopify /products/handle.json and pick the matching variant."""
    try:
        resp = requests.get(
            shopify_product_json_url(url),
            headers=_FETCH_HEADERS,
            timeout=(5.0, timeout),
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            return None
        payload = resp.json()
    except Exception:
        return None

    product_obj = payload.get("product") if isinstance(payload, dict) else None
    if not isinstance(product_obj, dict):
        return None
    variants = product_obj.get("variants") or []
    if not isinstance(variants, list) or not variants:
        return None

    chosen = pick_shopify_variant(
        variants,
        color_hint=color_hint_from_product(product),
        size_hint=size_hint_from_product(product),
        price_hint=price_hint,
    )
    if not chosen:
        return None
    return extract_from_shopify_variant(product_obj, chosen)
