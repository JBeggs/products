"""WooCommerce variable-product helpers for HTTP verify (size/volume matching)."""
from __future__ import annotations

import html as html_module
import json
import re
from typing import Any


def normalize_volume_ml(text: str) -> int | None:
    """Parse a volume hint like '1 litre', '250ml', '1000ml' to millilitres."""
    if not text:
        return None
    t = _clean_attr(text).lower().replace("\u00a0", " ")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:litre|liter|ltr|l)\b", t, re.I)
    if m:
        val = float(m.group(1))
        if val < 20:
            return int(round(val * 1000))
        return int(round(val))
    m = re.search(r"(\d+(?:\.\d+)?)\s*ml\b", t, re.I)
    if m:
        return int(round(float(m.group(1))))
    return None


def normalize_weight_g(text: str) -> int | None:
    """Parse weight hints like '500g', '1.5kg' to grams."""
    if not text:
        return None
    t = _clean_attr(text).lower().replace("\u00a0", " ")
    m = re.search(r"(\d+(?:\.\d+)?)\s*kg\b", t, re.I)
    if m:
        return int(round(float(m.group(1)) * 1000))
    m = re.search(r"(\d+(?:\.\d+)?)\s*g\b", t, re.I)
    if m:
        return int(round(float(m.group(1))))
    return None


def _clean_attr(value: str) -> str:
    return str(value).strip().strip('"').strip("'")


def product_size_hint(product: dict[str, Any] | None) -> str:
    """Build text used to match WooCommerce volume attributes."""
    if not product:
        return ""
    chunks: list[str] = []
    for key in ("description", "short_description", "name"):
        v = (product.get(key) or "").strip()
        if v:
            chunks.append(v)
    for item in product.get("variants") or []:
        if not isinstance(item, dict):
            continue
        for k in ("option", "volume", "size", "pack"):
            v = item.get(k)
            if v:
                chunks.append(str(v).strip())
    try:
        weight_g = int(product.get("weight") or 0)
        if weight_g > 0:
            chunks.append(f"{weight_g}g")
    except (TypeError, ValueError):
        pass
    return " ".join(chunks)


def parse_woocommerce_variations(page_html: str) -> list[dict[str, Any]]:
    """Parse data-product_variations JSON from a WooCommerce PDP."""
    m = re.search(r'data-product_variations=(["\'])(.*?)\1', page_html, re.S)
    if not m:
        return []
    raw = html_module.unescape(m.group(2))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def pick_woocommerce_variation(
    variations: list[dict[str, Any]],
    size_hint: str,
    *,
    price_hint: float | None = None,
) -> dict[str, Any] | None:
    """Pick the variation matching pack size (or stored supplier price as fallback)."""
    if not variations:
        return None

    target_ml = normalize_volume_ml(size_hint)
    target_g = normalize_weight_g(size_hint)
    if target_ml is not None or target_g is not None:
        for variation in variations:
            attrs = variation.get("attributes") or {}
            for val in attrs.values():
                clean = _clean_attr(str(val))
                vml = normalize_volume_ml(clean)
                if target_ml is not None and vml is not None and vml == target_ml:
                    return variation
                vg = normalize_weight_g(clean)
                if target_g is not None and vg is not None and vg == target_g:
                    return variation

    if price_hint is not None:
        for variation in variations:
            dp = variation.get("display_price")
            if dp is None:
                continue
            try:
                if abs(float(dp) - float(price_hint)) < 0.02:
                    return variation
            except (TypeError, ValueError):
                continue

    return None
