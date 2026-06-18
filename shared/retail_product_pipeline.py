"""
Shared load/save/build for session-based retail scrapers (Loot-like JSON shape).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from shared.dom_product_extract import goods_id_from_url
from shared.scraped_image import save_scraped_gallery_image
from shared.utils import (
    apply_tiered_markup,
    calculate_supplier_cost,
    clean_description,
    first_n_words,
    format_northernbolt_kit_description,
    format_tsawelding_product_description,
    get_compare_at_price,
    image_prefix,
    remove_special_chars,
    truncate_name,
)

PRODUCTS_FILE = "products.json"
IMAGES_DIR = "images"


def load_products(output_dir: Path) -> list:
    path = output_dir / PRODUCTS_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("products", [])
    except Exception:
        return []


def save_products(output_dir: Path, products: list, hostname_substring: str, urls_header: str) -> None:
    path = output_dir / PRODUCTS_FILE
    path.write_text(
        json.dumps({"products": products, "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )
    _sync_urls_from_products(products, output_dir, hostname_substring, urls_header)


def _sync_urls_from_products(products: list, output_dir: Path, hostname_substring: str, urls_header: str) -> None:
    urls_path = output_dir.parent / "urls.txt"
    seen: set[str] = set()
    urls: list[str] = []
    h = hostname_substring.lower()
    for p in products:
        url = (p.get("url") or "").strip()
        if not url or h not in url.lower():
            continue
        base = url.split("?")[0].strip()
        if base and base not in seen:
            seen.add(base)
            urls.append(base)
    urls_path.parent.mkdir(parents=True, exist_ok=True)
    urls_path.write_text(urls_header + "\n".join(urls) + ("\n" if urls else ""), encoding="utf-8")


def default_is_product_url(url: str, hostname_substring: str) -> bool:
    """Heuristic: correct host, not cart/search, path looks like a PDP."""
    u = (url or "").lower()
    h = hostname_substring.lower()
    if h not in u:
        return False
    # Shopify: /collections/{collection}/products/{handle} is a valid PDP (not a listing page).
    if "/collections/" in u and "/products/" in u:
        return True
    banned = (
        "/cart",
        "/checkout",
        "/account",
        "/login",
        "/search",
        "/basket",
        "/my-account",
        "/wishlist",
        "/stores",
        "/store-locator",
        "/contact",
        "/blog",
        "/news",
        "/product-category",
        "/product-cat",
        "/collections/",
        "/category/",
    )
    if any(b in u for b in banned):
        return False
    markers = (
        "/product",
        "/products/",
        "/p/",
        "/item",
        "/shop/",
        "/buy",
        "/catalogue",
        "/plp",
        "/collection",
        "/sku",
        "/pid",
    )
    if any(m in u for m in markers):
        return True
    path = urlparse(url).path or ""
    segs = [x for x in path.split("/") if x]
    return len(segs) >= 3


def build_scraped_index(output_dir: Path, supplier_slug: str, source_price_key: str) -> None:
    products = load_products(output_dir)
    if not products:
        return
    index_items = [
        {
            "name": p.get("name", ""),
            "price": p.get("price"),
            source_price_key: p.get(source_price_key),
            "goods_id": p.get("goods_id", ""),
        }
        for p in products
    ]
    index = {
        "updated": datetime.now().isoformat(),
        "product_count": len(products),
        "products": index_items,
    }
    (output_dir / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")


def build_and_save_product(
    data: dict,
    url: str,
    output_dir: Path,
    supplier_slug: str,
    hostname_substring: str,
    tags: list[str],
    source_price_key: str,
    urls_header: str,
    goods_id_fn: Callable[[str], str] | None = None,
) -> dict | None:
    """Append one product to products.json; download images."""
    title = data.get("goodsName") or "Unknown Product"
    gid_fn = goods_id_fn or goods_id_from_url
    goods_id = data.get("goodsId") or gid_fn(url) or "unknown"
    sale_price_zar = data.get("salePrice")
    sale_price_cents = int(sale_price_zar * 100) if sale_price_zar is not None else 0

    name = first_n_words(remove_special_chars(title), 5)
    short_desc = truncate_name(title, 150)
    desc_raw = data.get("desc") or title
    slug_l = (supplier_slug or "").strip().lower()
    if slug_l == "northernbolt":
        desc_raw = format_northernbolt_kit_description(desc_raw)
    elif slug_l == "tsawelding":
        desc_raw = format_tsawelding_product_description(desc_raw)
    description = clean_description(desc_raw)

    vs = (data.get("variantSize") or "").strip()
    sk = (data.get("supplierSku") or "").strip()
    wt = (data.get("weightText") or "").strip()
    dt = (data.get("dimensionsText") or "").strip()
    extras: list[str] = []
    if vs:
        extras.append(f"Pack size / option: {vs}")
    if sk:
        extras.append(f"Supplier SKU: {sk}")
    if wt:
        extras.append(f"Weight: {wt}")
    if dt:
        extras.append(f"Dimensions: {dt}")
    if extras:
        description = clean_description(description + "\n\n" + "\n".join(extras))
    description = description[:2000]

    variants = list(data.get("variants") or [])
    if not variants and (vs or sk or wt or dt):
        entry = {k: v for k, v in [
            ("option", vs), ("sku", sk), ("weight", wt), ("dimensions", dt),
        ] if v}
        if entry:
            variants = [entry]

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)
    products = load_products(output_dir)
    image_urls = list(data.get("gallery") or [])[:10]
    image_files: list[str] = []
    base_prefix = f"{image_prefix(title, 20)}_{re.sub(r'[^a-zA-Z0-9_-]', '_', str(goods_id)[:30])}"
    for i, img_url in enumerate(image_urls, 1):
        try:
            if not img_url.startswith("http"):
                img_url = "https:" + img_url if img_url.startswith("//") else img_url
            resp = requests.get(img_url, timeout=15)
            resp.raise_for_status()
            rel_path = save_scraped_gallery_image(
                resp.content,
                images_dir,
                base_prefix,
                i,
                content_type=resp.headers.get("content-type"),
            )
            if rel_path:
                image_files.append(rel_path)
        except Exception:
            continue

    if not image_files:
        try:
            resp = requests.get("https://via.placeholder.com/400x400?text=No+Image", timeout=10)
            resp.raise_for_status()
            fname = f"{base_prefix}_01.jpg"
            (images_dir / fname).write_bytes(resp.content)
            image_files = [f"{IMAGES_DIR}/{fname}"]
        except Exception:
            return None

    sell_price = apply_tiered_markup(sale_price_cents, supplier_slug)
    cost = calculate_supplier_cost(sale_price_cents, supplier_slug)
    compare_at_price = get_compare_at_price(sell_price)
    sale_zar = sale_price_zar if sale_price_zar is not None else (sale_price_cents / 100)

    product_json = {
        "url": url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_desc,
        "price": sell_price,
        "compare_at_price": compare_at_price,
        "cost": round(cost, 2),
        source_price_key: sale_zar,
        "images": image_files,
        "variants": variants,
        "in_stock": True,
        "stock_quantity": 0,
        "status": "active",
        "tags": tags,
        "goods_id": goods_id,
    }
    products.append(product_json)
    save_products(output_dir, products, hostname_substring, urls_header)
    return product_json
