"""Upload products to Django API."""
import requests
from pathlib import Path
from decimal import Decimal, InvalidOperation

try:
    from shared.config import get_supplier_delivery
except ImportError:
    get_supplier_delivery = lambda s: {}


def _normalize_source_url(url: str) -> str:
    """Normalize URL for matching: strip query, lowercase."""
    if not url:
        return ""
    return (url.split("?")[0].strip().lower())[:500]


def _normalize_non_negative_decimal(value) -> str | None:
    """Return decimal string for non-negative values, else None."""
    if value is None:
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if dec < 0:
        return None
    return str(dec)


def _resolve_free_delivery_threshold(data: dict, supplier_delivery: dict | None) -> str | None:
    """JSON-first threshold resolution with supplier-config fallback."""
    threshold = data.get("free_delivery_threshold")
    if threshold is None and supplier_delivery:
        threshold = supplier_delivery.get("free_delivery_threshold")
    return _normalize_non_negative_decimal(threshold)


def _resolve_bundle_pids(
    bundle_items: list,
    company_slug: str,
    products: list | None = None,
    source: str = "",
    products_by_source: dict | None = None,
) -> list[str] | None:
    """Resolve bundle_items to list of production_ids. Handles single-source (int indices) and cross-supplier (dict {source, index})."""
    if not bundle_items:
        return []
    bundle_pids = []
    is_cross = isinstance(bundle_items[0], dict)
    if is_cross and products_by_source:
        for it in bundle_items:
            src = it.get("source", "")
            idx = it.get("index", -1)
            prods = products_by_source.get(src) or []
            if idx < 0 or idx >= len(prods):
                return None
            pid = (prods[idx].get("production_ids") or {}).get(company_slug)
            if not pid:
                return None
            bundle_pids.append(pid)
    elif not is_cross and products is not None:
        for idx in bundle_items:
            if idx < 0 or idx >= len(products):
                return None
            pid = (products[idx].get("production_ids") or {}).get(company_slug)
            if not pid:
                return None
            bundle_pids.append(pid)
    else:
        return None
    return bundle_pids if len(bundle_pids) == len(bundle_items) else None


def find_product_by_source_url(
    base_url: str, token: str, company_slug: str, source_url: str
) -> str | None:
    """
    Find API product id by source_url. Fetches products and matches.
    Returns product id (UUID string) or None.
    """
    norm = _normalize_source_url(source_url)
    if not norm:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
    }
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/v1/products/",
            headers=headers,
            params={"limit": 9999},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    results = data.get("data") if isinstance(data.get("data"), list) else data.get("results", [])
    for p in results:
        api_url = (p.get("source_url") or "").strip()
        if _normalize_source_url(api_url) == norm:
            pid = p.get("id")
            return str(pid) if pid else None
    return None


def _upload_images(
    images: list,
    output_dir: Path,
    base_url: str,
    token: str,
    company_slug: str,
    sources_dict: dict | None = None,
) -> tuple[str, list] | None:
    """Upload image files, return (main_image_url, extra_urls) or None."""
    if not images:
        return None
    headers = {"Authorization": f"Bearer {token}", "X-Company-Slug": company_slug}
    upload_url = f"{base_url.rstrip('/')}/v1/products/images/upload-multiple/"
    files = []
    for fname in images:
        fname_str = str(fname).split("?")[0] if fname else ""
        if sources_dict and "/" in fname_str:
            parts = fname_str.split("/", 1)
            if len(parts) == 2 and parts[0] in sources_dict:
                path = sources_dict[parts[0]] / parts[1]
            else:
                path = output_dir / fname_str
        else:
            path = output_dir / fname_str
        if path.exists():
            mime = "image/jpeg"
            if path.suffix.lower() in (".png",):
                mime = "image/png"
            elif path.suffix.lower() in (".webp",):
                mime = "image/webp"
            files.append(("files[]", (Path(fname_str).name, path.read_bytes(), mime)))
    if not files:
        return None
    try:
        resp = requests.post(upload_url, headers=headers, files=files, timeout=60)
        resp.raise_for_status()
        result = resp.json()
    except Exception:
        return None
    if not result.get("success") or not result.get("data"):
        return None
    urls = [img.get("url") for img in result["data"] if img.get("url")]
    if not urls:
        return None
    return urls[0], urls[1:]


def update_product(
    data: dict,
    base_url: str,
    token: str,
    company_slug: str,
    product_id: str,
    source: str = "",
    products: list | None = None,
    products_by_source: dict | None = None,
    output_dir: Path | None = None,
    sources_dict: dict | None = None,
    category_id: str | None = None,
) -> bool:
    """
    Update existing product via PATCH. When output_dir is passed and data has images,
    re-uploads images and updates image/images on the API.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
        "Content-Type": "application/json",
    }

    description = (data.get("description") or "")[:2000]
    variants = data.get("variants") or []
    if variants:
        variants_text = "Available in: " + ", ".join(variants)
        description = f"{description}\n\n{variants_text}".strip()[:2000]

    payload = {
        "name": data.get("name", ""),
        "description": description,
        "short_description": (data.get("short_description") or "")[:300],
        "price": str(data.get("price", 0)),
        "cost_price": str(data["cost"]) if data.get("cost") is not None else None,
        "compare_at_price": str(data["compare_at_price"]) if data.get("compare_at_price") else None,
    }
    if data.get("stock_quantity") is not None:
        payload["stock_quantity"] = int(data["stock_quantity"])
    if "in_stock" in data:
        payload["in_stock"] = bool(data["in_stock"])
    if source and source.lower() == "gumtree":
        payload["tags"] = ["imports", "vintage"]
    if data.get("delivery_time"):
        payload["delivery_time"] = str(data["delivery_time"])[:100]
    elif source:
        supp = get_supplier_delivery(source)
        if supp.get("delivery_time"):
            payload["delivery_time"] = str(supp["delivery_time"])[:100]
    if source:
        payload["supplier_slug"] = source
        supp = get_supplier_delivery(source)
        dc = supp.get("delivery_cost")
        payload["supplier_delivery_cost"] = str(dc) if dc is not None and dc > 0 else None
        payload["free_delivery_threshold"] = _resolve_free_delivery_threshold(data, supp)
    if data.get("min_quantity") is not None:
        payload["min_quantity"] = int(data["min_quantity"])
    if data.get("weight") is not None:
        payload["weight"] = int(data["weight"])
    if data.get("dimension_length") is not None:
        payload["dimension_length"] = str(data["dimension_length"])
    if data.get("dimension_width") is not None:
        payload["dimension_width"] = str(data["dimension_width"])
    if data.get("dimension_height") is not None:
        payload["dimension_height"] = str(data["dimension_height"])
    cat = (category_id or "").strip() or (data.get("category_id") or "").strip() or (data.get("category") or "").strip()
    if cat:
        payload["category"] = cat
    bundle_items = data.get("bundle_items")
    if bundle_items:
        bundle_pids = _resolve_bundle_pids(
            bundle_items, company_slug,
            products=products, source=source,
            products_by_source=products_by_source,
        )
        if bundle_pids:
            payload["bundle_product_ids"] = bundle_pids
    else:
        # Non-bundle products must have empty bundle_product_ids (never inherit from elsewhere)
        payload["bundle_product_ids"] = []

    images = data.get("images") or []
    if output_dir and images:
        uploaded = _upload_images(images, output_dir, base_url, token, company_slug, sources_dict)
        if uploaded:
            main_url, extra_urls = uploaded
            payload["image"] = main_url
            payload["images"] = extra_urls

    payload = {k: v for k, v in payload.items() if v is not None}
    if "timed_duration_minutes" in data:
        val = data["timed_duration_minutes"]
        payload["timed_duration_minutes"] = int(val) if val is not None else None  # Include None to clear

    try:
        r = requests.patch(
            f"{base_url.rstrip('/')}/v1/products/{product_id}/",
            headers=headers,
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        status = None
        body = str(e)
        if hasattr(e, "response") and getattr(e, "response", None) is not None:
            status = getattr(e.response, "status_code", None)
            body = (e.response.text or "")[:200]
        print(f"  WARNING: Update failed for {data.get('name', '')[:40]}... (status={status}): {body}")
        return False


def get_auth_token(base_url: str, username: str, password: str, company_slug: str = "", use_email: bool = False) -> str | None:
    """JWT login. Returns access token or None."""
    payload = {"password": password}
    payload["email" if use_email else "username"] = username
    if company_slug:
        payload["company_slug"] = company_slug
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/auth/login/",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        token = data.get("access") or data.get("token") or data.get("access_token")
        if not token and isinstance(data.get("tokens"), dict):
            token = data["tokens"].get("access") or data["tokens"].get("token")
        return token
    except Exception:
        return None


def get_or_create_category(base_url: str, token: str, company_slug: str, name: str, slug: str) -> str | None:
    """Get category by slug, or create if not exists. Returns category id or None."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
    }
    try:
        r = requests.get(f"{base_url.rstrip('/')}/v1/categories/", headers=headers, timeout=15)
        r.raise_for_status()
        resp = r.json()
        items = resp.get("results") if isinstance(resp.get("results"), list) else (resp if isinstance(resp, list) else resp.get("data", []))
        for c in items:
            if (c.get("slug") or "").lower() == slug.lower():
                return str(c.get("id"))
            if (c.get("name") or "").lower() == name.lower():
                return str(c.get("id"))
        cr = requests.post(
            f"{base_url.rstrip('/')}/v1/categories/",
            headers={**headers, "Content-Type": "application/json"},
            json={"name": name, "slug": slug},
            timeout=15,
        )
        cr.raise_for_status()
        created = cr.json()
        cid = created.get("id") or created.get("data", {}).get("id")
        return str(cid) if cid else None
    except Exception as e:
        print(f"  WARNING: Could not get/create category {name!r}: {e}")
        return None


def upload_product(
    data: dict,
    output_dir: Path,
    base_url: str,
    token: str,
    company_slug: str,
    category_id: str,
    products: list | None = None,
    products_by_source: dict | None = None,
    sources_dict: dict | None = None,
    bundle_source: str = "",
) -> str | None:
    """Upload one product from products.json to Django API. Returns product UUID or None on failure.
    For bundles: pass products (single-source) or products_by_source (cross-supplier).
    For cross-supplier image paths (source/images/...), pass sources_dict."""
    bundle_items = data.get("bundle_items")
    bundle_pids = None
    if bundle_items:
        bundle_pids = _resolve_bundle_pids(
            bundle_items, company_slug,
            products=products, source=bundle_source,
            products_by_source=products_by_source,
        )
        if not bundle_pids:
            print(f"  SKIP: Bundle {data.get('name', '')[:50]}... needs products/products_by_source; sync child products first")
            return None
    images = data.get("images") or []
    if not images:
        print(f"  SKIP: No images for {data.get('name', '')[:50]}")
        return None

    uploaded = _upload_images(images, output_dir, base_url, token, company_slug, sources_dict)
    if not uploaded:
        print(f"  SKIP: Could not upload images for {data.get('name', '')[:50]}")
        return None
    main_image, extra_images = uploaded

    # Always use the passed category_id (from API/system). Do not use supplier categories.
    cat_id = category_id

    description = data.get("description", "") or ""
    variants = data.get("variants") or []
    if variants:
        variants_text = "Available in: " + ", ".join(variants)
        description = f"{description}\n\n{variants_text}".strip()[:2000]
    else:
        description = description[:2000]

    stock_qty = data.get("stock_quantity")
    in_stock = data.get("in_stock") if "in_stock" in data else (bool(stock_qty) if stock_qty is not None else False)
    payload = {
        "name": data["name"],
        "description": description,
        "short_description": data.get("short_description", "")[:300],
        "price": str(data["price"]),
        "image": main_image,
        "images": extra_images,
        "category": cat_id,
        "in_stock": in_stock,
        "stock_quantity": int(stock_qty) if stock_qty is not None else 0,
        "status": "active",
        "tags": ["imports", "vintage"] if "gumtree" in str(output_dir).lower() else ["imports"],
        "source_url": (data.get("url") or "").split("?")[0][:500],
    }
    if data.get("compare_at_price"):
        payload["compare_at_price"] = str(data["compare_at_price"])
    if data.get("cost") is not None:
        payload["cost_price"] = str(data["cost"])
    supplier_slug = output_dir.parent.name if output_dir else (bundle_source or "")
    if data.get("delivery_time"):
        payload["delivery_time"] = str(data["delivery_time"])[:100]
    elif supplier_slug:
        supp = get_supplier_delivery(supplier_slug)
        if supp.get("delivery_time"):
            payload["delivery_time"] = str(supp["delivery_time"])[:100]
    if supplier_slug:
        payload["supplier_slug"] = supplier_slug
        supp = get_supplier_delivery(supplier_slug)
        dc = supp.get("delivery_cost")
        payload["supplier_delivery_cost"] = str(dc) if dc is not None and dc > 0 else None
        payload["free_delivery_threshold"] = _resolve_free_delivery_threshold(data, supp)
    min_qty = data.get("min_quantity")
    if min_qty is not None:
        payload["min_quantity"] = int(min_qty)
    if data.get("weight") is not None:
        payload["weight"] = int(data["weight"])
    if data.get("dimension_length") is not None:
        payload["dimension_length"] = str(data["dimension_length"])
    if data.get("dimension_width") is not None:
        payload["dimension_width"] = str(data["dimension_width"])
    if data.get("dimension_height") is not None:
        payload["dimension_height"] = str(data["dimension_height"])
    if bundle_pids:
        payload["bundle_product_ids"] = bundle_pids
    if data.get("timed_duration_minutes") is not None:
        payload["timed_duration_minutes"] = int(data["timed_duration_minutes"])

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/v1/products/",
            headers=headers,
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        data_resp = r.json()
        pid = data_resp.get("id") or (data_resp.get("data") or {}).get("id")
        print(f"  Uploaded product: {data['name'][:50]}...")
        return str(pid) if pid else None
    except requests.RequestException as e:
        body = e.response.text[:800] if e.response is not None else str(e)
        print(f"  ERROR creating product: {e}")
        print(f"  Response: {body}")
        if "does_not_exist" in body and "category" in body.lower():
            print(f"  → Category {cat_id!r} not found on API. Run: python -m products list-categories")
        return None


def delete_product_from_api(base_url: str, token: str, company_slug: str, product_id: str) -> bool:
    """Soft-delete (archive) product via DELETE. Returns True on success."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
    }
    try:
        r = requests.delete(
            f"{base_url.rstrip('/')}/v1/products/{product_id}/",
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False


def deactivate_product_from_api(base_url: str, token: str, company_slug: str, product_id: str) -> bool:
    """Set product status to archived (inactive) via PATCH. Keeps product locally. Returns True on success."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
        "Content-Type": "application/json",
    }
    try:
        r = requests.patch(
            f"{base_url.rstrip('/')}/v1/products/{product_id}/",
            headers=headers,
            json={"status": "archived"},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False


def reactivate_product_from_api(base_url: str, token: str, company_slug: str, product_id: str) -> bool:
    """Set product status to active via PATCH. Returns True on success."""
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
        "Content-Type": "application/json",
    }
    try:
        r = requests.patch(
            f"{base_url.rstrip('/')}/v1/products/{product_id}/",
            headers=headers,
            json={"status": "active"},
            timeout=30,
        )
        r.raise_for_status()
        return True
    except requests.RequestException:
        return False
