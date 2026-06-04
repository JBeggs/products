"""
Save a manually entered product to the same products.json shape as retail scrapers.
"""
from __future__ import annotations

import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

from shared.retail_product_pipeline import IMAGES_DIR, PRODUCTS_FILE, build_scraped_index, load_products
from shared.suppliers import SUPPLIERS, get_company_scoped_dir
from shared.utils import (
    clean_description,
    first_n_words,
    get_compare_at_price,
    image_prefix,
    remove_special_chars,
    truncate_name,
)

SOURCE_PRICE_KEY = "manual_price"
SUPPLIER_SLUG = "manual"
MAX_IMAGES = 10
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _write_products_file(output_dir: Path, products: list) -> None:
    """Write products.json only (no urls.txt sync — company-scoped dirs break parent/urls.txt)."""
    import json

    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / PRODUCTS_FILE
    path.write_text(
        json.dumps({"products": products, "updated": datetime.now().isoformat()}, indent=2),
        encoding="utf-8",
    )


def _ext_from_upload(mimetype: str | None, filename: str) -> str:
    if mimetype:
        mt = mimetype.lower()
        if "png" in mt:
            return ".png"
        if "webp" in mt:
            return ".webp"
        if "jpeg" in mt or "jpg" in mt:
            return ".jpg"
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        if "png" in guessed:
            return ".png"
        if "webp" in guessed:
            return ".webp"
    low = (filename or "").lower()
    for ext in (".png", ".webp", ".jpeg", ".jpg"):
        if low.endswith(ext):
            return ".jpeg" if ext == ".jpeg" else ext
    return ".jpg"


def save_manual_product(
    payload: dict,
    upload_files: list[Any] | None,
    image_urls: list[str] | None,
    company_slug: str | None,
) -> dict:
    """
    Upsert one product under manual/scraped[/companies/<slug>/].

    Raises ValueError on validation errors.
    """
    raw_title = (payload.get("name") or "").strip()
    if not raw_title:
        raise ValueError("Name is required")

    try:
        price = float(payload.get("price") or 0)
    except (TypeError, ValueError):
        raise ValueError("Invalid price")
    if price <= 0:
        raise ValueError("Price must be greater than zero")

    info = SUPPLIERS.get(SUPPLIER_SLUG)
    if not info:
        raise ValueError("Manual supplier is not registered")

    base_dir = info.output_dir
    if (company_slug or "").strip():
        output_dir = get_company_scoped_dir(base_dir, company_slug.strip())
    else:
        output_dir = base_dir

    goods_id = (payload.get("goods_id") or "").strip() or f"manual_{uuid4().hex[:12]}"
    goods_id_safe = re.sub(r"[^\w\-]", "_", str(goods_id)[:30])

    desc_in = (payload.get("description") or "").strip()
    short_in = (payload.get("short_description") or "").strip()
    title_for_prefix = raw_title
    name = first_n_words(remove_special_chars(raw_title), 5) or raw_title[:200]
    short_description = (truncate_name(short_in, 150) if short_in else truncate_name(raw_title, 150))
    description = clean_description(desc_in or raw_title)[:2000]

    cmp_raw = payload.get("compare_at_price")
    compare_at_price: float | None = None
    if cmp_raw is not None and str(cmp_raw).strip() != "":
        try:
            compare_at_price = float(cmp_raw)
        except (TypeError, ValueError):
            compare_at_price = None
    if compare_at_price is not None and compare_at_price <= price:
        compare_at_price = None
    if compare_at_price is None:
        compare_at_price = get_compare_at_price(price)

    try:
        stock_quantity = int(payload.get("stock_quantity") or 0)
    except (TypeError, ValueError):
        stock_quantity = 0
    stock_quantity = max(0, stock_quantity)

    status = (payload.get("status") or "active").strip().lower()
    if status not in ("active", "draft", "inactive"):
        status = "active"

    tags = list(payload.get("tags") or [])
    if "manual" not in [t.lower() for t in tags]:
        tags = [*tags, "manual"]

    source_url = (payload.get("url") or "").strip()
    product_url = source_url or f"https://manual.local/product/{goods_id}"

    files = [f for f in (upload_files or []) if f and getattr(f, "filename", None)]
    urls = [u.strip() for u in (image_urls or []) if (u or "").strip().startswith("http")]
    if not files and not urls:
        raise ValueError("Add at least one image (upload or URL)")

    images_dir = output_dir / IMAGES_DIR
    images_dir.mkdir(parents=True, exist_ok=True)

    base_prefix = f"{image_prefix(title_for_prefix, 20)}_{goods_id_safe}"
    image_files: list[str] = []
    idx = 0

    for f in files:
        if len(image_files) >= MAX_IMAGES:
            break
        data = f.read()
        if not data:
            continue
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError(f"Image too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB per file)")
        idx += 1
        ext = _ext_from_upload(getattr(f, "mimetype", None), getattr(f, "filename", "") or "")
        fname = f"{base_prefix}_{idx:02d}{ext}"
        rel_path = f"{IMAGES_DIR}/{fname}"
        (images_dir / fname).write_bytes(data)
        image_files.append(rel_path)

    for img_url in urls:
        if len(image_files) >= MAX_IMAGES:
            break
        idx += 1
        try:
            u = img_url.strip()
            if not u.startswith("http"):
                u = "https:" + u if u.startswith("//") else u
            resp = requests.get(u, timeout=15)
            resp.raise_for_status()
            ext = ".jpg"
            ct = resp.headers.get("content-type", "")
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            fname = f"{base_prefix}_{idx:02d}{ext}"
            rel_path = f"{IMAGES_DIR}/{fname}"
            (images_dir / fname).write_bytes(resp.content)
            image_files.append(rel_path)
        except Exception:
            idx -= 1
            continue

    if not image_files:
        raise ValueError("No images could be saved (check uploads and URLs)")

    manual_price = round(price, 2)
    product_json = {
        "url": product_url.split("?")[0],
        "name": name,
        "description": description,
        "short_description": short_description,
        "price": round(price, 2),
        "compare_at_price": compare_at_price,
        "cost": round(price, 2),
        SOURCE_PRICE_KEY: manual_price,
        "images": image_files,
        "variants": [],
        "in_stock": stock_quantity > 0,
        "stock_quantity": stock_quantity,
        "status": status,
        "tags": tags,
        "goods_id": goods_id,
    }

    products = load_products(output_dir)
    existing_idx = next((i for i, p in enumerate(products) if p.get("goods_id") == goods_id), None)
    if existing_idx is not None:
        old = products[existing_idx]
        product_json = {**old, **product_json}
        products[existing_idx] = product_json
    else:
        products.append(product_json)

    _write_products_file(output_dir, products)
    build_scraped_index(output_dir, SUPPLIER_SLUG, SOURCE_PRICE_KEY)

    return {
        "ok": True,
        "goods_id": goods_id,
        "images": image_files,
        "path": str(output_dir / PRODUCTS_FILE),
    }
