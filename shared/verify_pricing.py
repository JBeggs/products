"""Fast verify-all pricing: HTTP + JSON-LD (no browser per product)."""
from __future__ import annotations

from shared.html_product_extract import fetch_product_via_http
from shared.utils import apply_tiered_markup, calculate_supplier_cost
from shared.shopify_variations import fetch_shopify_product_via_json
from shared.woocommerce_variations import product_size_hint


def pricing_result_from_data(data: dict | None, supplier_slug: str) -> dict | None:
    if not data or (not data.get("goodsName") and data.get("salePrice") is None):
        return None

    sale_price_zar = data.get("salePrice")
    sale_price_cents = int(round(float(sale_price_zar) * 100)) if sale_price_zar is not None else 0
    in_stock = data.get("in_stock", True)
    if sale_price_cents <= 0 and not in_stock:
        return {
            "price": None,
            "cost": None,
            "source_price": None,
            "valid": True,
            "in_stock": False,
            "unavailable": False,
        }
    if sale_price_cents <= 0:
        return None
    sell_price = apply_tiered_markup(sale_price_cents, supplier_slug)
    cost = calculate_supplier_cost(sale_price_cents, supplier_slug)
    source_price = sale_price_zar if sale_price_zar is not None else (sale_price_cents / 100)
    return {
        "price": round(sell_price, 2),
        "cost": round(cost, 2),
        "source_price": round(float(source_price), 2),
        "valid": True,
        "in_stock": in_stock,
        "unavailable": False,
    }


def fetch_retail_pricing_http(
    url: str,
    supplier_slug: str,
    *,
    timeout: float = 25.0,
    product: dict | None = None,
) -> dict | None:
    """Fetch PDP via HTTP and return standard verify pricing dict."""
    try:
        size_hint = product_size_hint(product)
        price_hint = None
        if product:
            key = f"{supplier_slug}_price"
            raw = product.get(key)
            if raw is not None:
                try:
                    price_hint = float(raw)
                except (TypeError, ValueError):
                    price_hint = None
        data = fetch_product_via_http(
            url,
            timeout=timeout,
            size_hint=size_hint or None,
            price_hint=price_hint,
            product=product,
        )
        return pricing_result_from_data(data, supplier_slug)
    except Exception:
        return None
