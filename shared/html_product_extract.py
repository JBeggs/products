"""Fast verify path: fetch PDP HTML and parse JSON-LD (no browser)."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from shared.shopify_variations import fetch_shopify_product_via_json
from shared.woocommerce_variations import (
    _clean_attr,
    parse_woocommerce_variations,
    pick_woocommerce_variation,
)

_LOG = logging.getLogger("products.scraper")

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

# Fresh GET per call — shared Session keep-alive can hang ~30s when the server
# closes the connection after several rapid verify-all requests.
_FETCH_HEADERS = {**_DEFAULT_HEADERS, "Connection": "close"}

_PRODUCT_TYPES = frozenset({"Product", "ProductGroup", "IndividualProduct"})
_IN_STOCK_MARKERS = ("instock", "in stock", "preorder", "pre-order")
_OUT_OF_STOCK_MARKERS = ("outofstock", "out of stock", "soldout", "sold out", "discontinued")


def _parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        n = float(raw)
        if n < 0 or n >= 10_000_000:
            return None
        return n
    return _parse_display_price(str(raw))


def _parse_display_price(text: str) -> float | None:
    """Parse ZA display prices like R110,00 or 1.234,56."""
    if not text:
        return None
    raw = str(text).strip().replace("\u00a0", " ").replace(" ", "")
    raw = re.sub(r"^R\s*", "", raw, flags=re.I)
    if not raw:
        return None
    if re.match(r"^\d{1,3}(?:\.\d{3})*,\d{2}$", raw):
        raw = raw.replace(".", "").replace(",", ".")
    elif re.match(r"^\d+,\d{2}$", raw):
        raw = raw.replace(",", ".")
    else:
        raw = raw.replace(",", "")
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n < 0 or n >= 10_000_000:
        return None
    return n


def _availability_in_stock(value: Any) -> bool | None:
    if value is None:
        return None
    text = str(value).lower()
    if any(m in text for m in _OUT_OF_STOCK_MARKERS):
        return False
    if any(m in text for m in _IN_STOCK_MARKERS):
        return True
    return None


def _price_from_offer(offer: dict) -> float | None:
    if not isinstance(offer, dict):
        return None
    for key in ("price", "lowPrice", "highPrice"):
        p = _parse_price(offer.get(key))
        if p is not None:
            return p
    spec = offer.get("priceSpecification")
    specs = spec if isinstance(spec, list) else ([spec] if isinstance(spec, dict) else [])
    for sp in specs:
        if not isinstance(sp, dict):
            continue
        p = _parse_price(sp.get("price"))
        if p is not None:
            return p
    return None


def _walk_jsonld(node: Any, found: list[dict]) -> None:
    if isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else ([t] if t else [])
        if any(x in _PRODUCT_TYPES for x in types):
            found.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                _walk_jsonld(item, found)
        for v in node.values():
            _walk_jsonld(v, found)
    elif isinstance(node, list):
        for item in node:
            _walk_jsonld(item, found)


def _extract_jsonld_blocks(html: str) -> list[Any]:
    blocks = []
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        text = raw.strip()
        if not text:
            continue
        try:
            blocks.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return blocks


def _extract_woocommerce_dom_price(html: str) -> dict[str, Any] | None:
    """WooCommerce PDPs without JSON-LD (e.g. Elementor themes)."""
    name = None
    for pat in (
        r'<h2[^>]*class="[^"]*elementor-heading-title[^"]*"[^>]*>([^<]+)',
        r'<h1[^>]*class="[^"]*product_title[^"]*"[^>]*>([^<]+)',
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
    ):
        m = re.search(pat, html, re.I | re.S)
        if m:
            name = re.sub(r"\s+", " ", m.group(1)).strip() or None
            if name:
                break

    sale_price = None
    for block in re.finditer(r'<p class="price">(.*?)</p>', html, re.I | re.S):
        inner = block.group(1)
        if inner.count("woocommerce-Price-amount") > 1 and ("–" in inner or "&ndash;" in inner):
            continue
        ins_m = re.search(r"<ins[^>]*>(.*?)</ins>", inner, re.I | re.S)
        search_inner = ins_m.group(1) if ins_m else inner
        for m in re.finditer(
            r"woocommerce-Price-amount[^>]*>.*?>([\d\s.,]+)</bdi>",
            search_inner,
            re.I | re.S,
        ):
            p = _parse_display_price(m.group(1))
            if p is not None and p > 0:
                sale_price = p
                break
        if sale_price is not None:
            break

    if sale_price is None:
        return None

    in_stock = _woocommerce_product_in_stock(html)
    return {"goodsName": name, "salePrice": sale_price, "in_stock": in_stock}


def _woocommerce_product_in_stock(html: str) -> bool:
    """Stock from main product block only (ignore related-product widgets)."""
    m = re.search(
        r'<div[^>]+class="[^"]*(?:product-type-(?:simple|variable)|single-product)[^"]*"[^>]*>',
        html,
        re.I,
    )
    chunk = html[m.start() : m.start() + 2500] if m else html[:120_000]
    lower = chunk.lower()
    return "outofstock" not in lower and "out-of-stock" not in lower


def extract_product_from_html(
    html: str,
    *,
    size_hint: str | None = None,
    price_hint: float | None = None,
) -> dict[str, Any] | None:
    """Parse Product JSON-LD from HTML. Returns goodsName, salePrice, in_stock."""
    products: list[dict] = []
    for block in _extract_jsonld_blocks(html):
        _walk_jsonld(block, products)

    base_name = None
    base: dict[str, Any] | None = None
    for prod in products:
        name = (prod.get("name") or "").strip() or None
        sale_price = None
        in_stock: bool | None = None

        offers = prod.get("offers") or prod.get("aggregateOffer")
        offer_list: list[dict] = []
        if isinstance(offers, list):
            offer_list = [o for o in offers if isinstance(o, dict)]
        elif isinstance(offers, dict):
            nested = offers.get("offers")
            if isinstance(nested, list):
                offer_list = [o for o in nested if isinstance(o, dict)]
            else:
                offer_list = [offers]

        for offer in offer_list:
            if sale_price is None:
                sale_price = _price_from_offer(offer)
            avail = _availability_in_stock(offer.get("availability"))
            if avail is not None:
                in_stock = avail

        if name or sale_price is not None:
            base_name = name or base_name
            gallery: list[str] = []
            raw_img = prod.get("image")
            if raw_img:
                img_items = raw_img if isinstance(raw_img, list) else [raw_img]
                for item in img_items:
                    if isinstance(item, str) and item.strip():
                        gallery.append(item.strip())
                    elif isinstance(item, dict):
                        u = (item.get("url") or item.get("contentUrl") or "").strip()
                        if u:
                            gallery.append(u)
            base = {
                "goodsName": name,
                "salePrice": sale_price,
                "in_stock": True if in_stock is None else in_stock,
            }
            if gallery:
                base["gallery"] = gallery
            break

    variations = parse_woocommerce_variations(html)
    if variations:
        chosen = pick_woocommerce_variation(
            variations,
            size_hint or "",
            price_hint=price_hint,
        )
        if not chosen:
            pass
        else:
            attrs = chosen.get("attributes") or {}
            variant_label = next(iter(attrs.values()), None)
            try:
                sale_price = float(chosen.get("display_price"))
            except (TypeError, ValueError):
                sale_price = None
            if sale_price is not None:
                return {
                    "goodsName": base_name or (base.get("goodsName") if base else None),
                    "salePrice": sale_price,
                    "in_stock": bool(chosen.get("is_in_stock", True)),
                    "variantSize": _clean_attr(str(variant_label)) if variant_label else None,
                }

    if base:
        return base
    return _extract_woocommerce_dom_price(html)


def fetch_product_via_http(
    url: str,
    *,
    timeout: float = 25.0,
    size_hint: str | None = None,
    price_hint: float | None = None,
    product: dict | None = None,
) -> dict[str, Any] | None:
    """GET product URL and extract JSON-LD product fields."""
    try:
        resp = requests.get(
            url,
            headers=_FETCH_HEADERS,
            timeout=(5.0, timeout),
            allow_redirects=True,
        )
        if resp.status_code >= 400:
            data = None
        else:
            data = extract_product_from_html(
                resp.text,
                size_hint=size_hint,
                price_hint=price_hint,
            )
        if data is None or data.get("salePrice") is None:
            shopify = fetch_shopify_product_via_json(
                url,
                timeout=timeout,
                product=product,
                price_hint=price_hint,
            )
            if shopify:
                if data:
                    merged = dict(data)
                    for key, val in shopify.items():
                        if val is not None or merged.get(key) is None:
                            merged[key] = val
                    data = merged
                else:
                    data = shopify
        elif data:
            shopify = fetch_shopify_product_via_json(
                url,
                timeout=timeout,
                product=product,
                price_hint=price_hint,
            )
            if shopify and shopify.get("gallery"):
                existing = data.get("gallery") or []
                if len(shopify["gallery"]) > len(existing):
                    data = dict(data)
                    data["gallery"] = shopify["gallery"]
        return data
    except Exception as exc:
        _LOG.debug("HTTP product fetch failed for %s: %s", url, exc)
        return None
