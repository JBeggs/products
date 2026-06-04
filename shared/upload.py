"""Upload products to Django API."""
import os
import re
import time
import uuid
from io import BytesIO
from glob import escape as glob_escape
import requests
from pathlib import Path
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urlparse

try:
    from shared.config import get_supplier_delivery
except ImportError:
    get_supplier_delivery = lambda s: {}


def _extract_temu_goods_id(url: str) -> str | None:
    """Stable Temu listing id from SEO (-g-123.html) or goods.html?goods_id= (any temu host)."""
    if not (url or "").strip():
        return None
    if "temu.com" not in url.lower():
        return None
    parsed = urlparse(url.strip())
    path = parsed.path or ""
    m = re.search(r"-g-(\d+)\.html", path, re.I)
    if m:
        return m.group(1)
    qs = parse_qs(parsed.query)
    gid = (qs.get("goods_id") or [None])[0]
    if gid and str(gid).strip().isdigit():
        return str(gid).strip()
    return None


def _canonical_temu_source_url(listing_url: str) -> str | None:
    """Short URL under CRM URLField(500); always unique per listing."""
    tid = _extract_temu_goods_id(listing_url)
    if tid:
        return f"https://www.temu.com/goods.html?goods_id={tid}"
    return None


def _source_url_for_crm_payload(listing_url: str) -> str:
    """Django source_url: Temu uses canonical goods_id URL; others cap at 500 chars (DB limit)."""
    canon = _canonical_temu_source_url(listing_url)
    if canon:
        return canon
    return ((listing_url or "").split("?")[0].strip())[:500]


def _normalize_source_url(url: str) -> str:
    """Normalize URL for matching. Temu compares by goods_id (avoids 500-char truncation collisions)."""
    if not url:
        return ""
    tid = _extract_temu_goods_id(url)
    if tid:
        return f"temu:goods_id:{tid}"
    return (url.split("?")[0].strip().lower())[:500]


def invalidate_product_list_cache(company_slug: str | None = None) -> None:
    """Clear cached GET /v1/products/ after writes so lookup-by-source sees fresh rows."""
    global _product_list_cache
    if company_slug:
        _product_list_cache.pop((company_slug or "").strip(), None)
    else:
        _product_list_cache.clear()


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


def _ensure_compare_at_gte_price(payload: dict, *, on_api_rejection: bool = False) -> bool:
    """Ensure Django validate_compare_at: compare_at_price >= price when both sent.

    When the API still rejects (edge cases), pass on_api_rejection=True to replace
    compare_at with a fresh markup from get_compare_at_price(price).
    Returns True if compare_at_price was written/changed.
    """
    from shared.utils import get_compare_at_price

    price_s = payload.get("price")
    if price_s is None:
        return False
    try:
        p = float(price_s)
    except (TypeError, ValueError):
        return False

    if on_api_rejection:
        new_c = get_compare_at_price(p)
        payload["compare_at_price"] = str(new_c)
        print(f"  INFO: compare_at_price set to {new_c} after API validation (price={p})")
        return True

    cmp_s = payload.get("compare_at_price")
    if cmp_s is None:
        return False
    try:
        c = float(cmp_s)
    except (TypeError, ValueError):
        return False
    if c >= p:
        return False
    new_c = get_compare_at_price(p)
    payload["compare_at_price"] = str(new_c)
    print(f"  INFO: compare_at_price adjusted from {c} to {new_c} (must be ≥ price {p})")
    return True


def _is_compare_at_price_validation_error(status_code: int | None, body: str) -> bool:
    if status_code != 400:
        return False
    b = (body or "").lower()
    return "compare_at_price" in b and (
        "greater than or equal" in b or "compare at price" in b or "compare_at" in b
    )


def _resolve_free_delivery_threshold(data: dict, supplier_delivery: dict | None) -> str | None:
    """Value for Django ``free_delivery_threshold`` — never use ``delivery_cost`` here."""
    sup_thresh = supplier_delivery.get("free_delivery_threshold") if supplier_delivery else None
    json_thresh = data.get("free_delivery_threshold")
    if sup_thresh is not None:
        threshold = sup_thresh
    elif json_thresh is not None and json_thresh != "":
        threshold = json_thresh
    else:
        threshold = None
    return _normalize_non_negative_decimal(threshold)


def _crm_supplier_flat_delivery_cost(data: dict, supplier_delivery: dict | None) -> str | None:
    """Value for Django ``supplier_delivery_cost`` — only scraper ``delivery_cost`` + optional JSON override."""
    override = data.get("supplier_delivery_cost")
    if override is not None and override != "":
        norm = _normalize_non_negative_decimal(override)
        return norm if norm is not None else None
    raw = supplier_delivery.get("delivery_cost") if supplier_delivery else None
    try:
        if raw is None or raw == "":
            return None
        dec = Decimal(str(raw))
        if dec <= 0:
            return None
        return str(dec)
    except (InvalidOperation, ValueError, TypeError):
        return None


GUMTREE_REQUIRED_PICKUP_FIELDS = (
    "pickup_street",
    "pickup_suburb",
    "pickup_city",
    "pickup_province",
    "pickup_postal_code",
    "pickup_country",
)


def _is_gumtree_source(source: str | None) -> bool:
    return (source or "").strip().lower() == "gumtree"


def _default_gumtree_pickup_origin_from_env() -> dict:
    """Fallback pickup-origin values for Gumtree products from environment."""
    return {
        "pickup_street": (os.environ.get("GUMTREE_PICKUP_STREET") or "").strip(),
        "pickup_suburb": (os.environ.get("GUMTREE_PICKUP_SUBURB") or "").strip(),
        "pickup_city": (os.environ.get("GUMTREE_PICKUP_CITY") or "").strip(),
        "pickup_province": (os.environ.get("GUMTREE_PICKUP_PROVINCE") or "").strip(),
        "pickup_postal_code": (os.environ.get("GUMTREE_PICKUP_POSTAL_CODE") or "").strip(),
        "pickup_country": (os.environ.get("GUMTREE_PICKUP_COUNTRY") or "").strip(),
    }


def _extract_pickup_origin(data: dict, with_env_fallback: bool = False) -> dict:
    pickup = {
        "pickup_street": (data.get("pickup_street") or "").strip(),
        "pickup_suburb": (data.get("pickup_suburb") or "").strip(),
        "pickup_city": (data.get("pickup_city") or "").strip(),
        "pickup_province": (data.get("pickup_province") or "").strip(),
        "pickup_postal_code": (data.get("pickup_postal_code") or "").strip(),
        "pickup_country": (data.get("pickup_country") or "").strip(),
    }
    if with_env_fallback:
        defaults = _default_gumtree_pickup_origin_from_env()
        for field in GUMTREE_REQUIRED_PICKUP_FIELDS:
            if not pickup[field]:
                pickup[field] = defaults[field]
    return pickup


def _validate_gumtree_pickup_origin(data: dict) -> tuple[bool, list[str]]:
    pickup = _extract_pickup_origin(data, with_env_fallback=True)
    missing = [field for field in GUMTREE_REQUIRED_PICKUP_FIELDS if not pickup[field]]
    return (len(missing) == 0, missing)


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


_product_list_cache: dict[str, tuple[float, list]] = {}
_PRODUCT_LIST_CACHE_TTL = 120

def find_product_by_source_url(
    base_url: str, token: str, company_slug: str, source_url: str
) -> str | None:
    """
    Find API product id by source_url. Caches the product list per company
    for 2 minutes to avoid re-downloading the entire catalog on every sync.
    """
    norm = _normalize_source_url(source_url)
    if not norm:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
    }

    cache_key = company_slug
    cached = _product_list_cache.get(cache_key)
    results = None
    if cached and (time.time() - cached[0]) < _PRODUCT_LIST_CACHE_TTL:
        results = cached[1]
    else:
        try:
            t0 = time.time()
            r = requests.get(
                f"{base_url.rstrip('/')}/v1/products/",
                headers=headers,
                params={"limit": 9999},
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
            results = data.get("data") if isinstance(data.get("data"), list) else data.get("results", [])
            _product_list_cache[cache_key] = (time.time(), results)
            elapsed = time.time() - t0
            if elapsed > 2:
                print(f"  INFO: Fetched {len(results)} products from API in {elapsed:.1f}s")
        except Exception:
            return None

    if results is None:
        return None
    for p in results:
        api_url = (p.get("source_url") or "").strip()
        if _normalize_source_url(api_url) == norm:
            pid = p.get("id")
            return str(pid) if pid else None
    # Legacy CRM rows: truncated SEO URLs lost -g-ID; match substring if goods id still present
    tid = _extract_temu_goods_id(source_url)
    if tid:
        needle = f"goods_id={tid}"
        needle2 = f"-g-{tid}.html"
        for p in results:
            api_url = (p.get("source_url") or "").strip()
            if not api_url:
                continue
            au = api_url.lower()
            if needle in au or needle2 in au.lower():
                pid = p.get("id")
                return str(pid) if pid else None
    return None


def _resolve_local_image_path(
    fname_str: str,
    output_dir: Path,
    base_dir: Path,
    sources_dict: dict | None,
    company_slug: str,
) -> Path | None:
    """
    Resolve a scraped image path to an on-disk file.

    Bundle cross-supplier entries look like ``northernbolt/images/foo.jpg``; files usually live under
    ``{supplier}/scraped/companies/{company_slug}/images/``, not only ``{supplier}/scraped/images/``.
    """
    from shared.suppliers import get_company_scoped_dir

    rel = fname_str.lstrip("/")
    cs = (company_slug or "").strip()

    if sources_dict and "/" in fname_str:
        parts = fname_str.split("/", 1)
        if len(parts) == 2 and parts[0] in sources_dict:
            supplier_root = sources_dict[parts[0]]
            sub = parts[1].lstrip("/")
            candidates: list[Path] = []
            if cs:
                try:
                    candidates.append(get_company_scoped_dir(supplier_root, cs) / sub)
                except ValueError:
                    pass
            candidates.append(supplier_root / sub)
            for p in candidates:
                if p.exists():
                    return p
            return None

    candidates = [output_dir / rel]
    if base_dir != output_dir:
        candidates.append(base_dir / rel)
    if cs and base_dir != output_dir:
        try:
            candidates.insert(1, get_company_scoped_dir(base_dir, cs) / rel)
        except ValueError:
            pass
    for p in candidates:
        if p.exists():
            return p
    return None


# Smaller batches shorten each multipart write (avoids client write timeouts on slow uplinks).
_UPLOAD_IMAGE_BATCH = 1
_UPLOAD_CONNECT_TIMEOUT = 20
_UPLOAD_READ_TIMEOUT = 60
# Re-encode to JPEG when larger: huge PNGs (e.g. AHM PDP screenshots) cause client write timeouts;
# API also rejects files over 5MB (django-crm ecommerce/views.py).
_REENCODE_THRESHOLD_BYTES = 250_000

_ALLOWED_IMAGE_SUFFIX = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _natural_image_sort_key(p: Path) -> tuple:
    stem = p.stem
    m = re.match(r"^(.+)_(\d+)$", stem)
    if m:
        return (m.group(1), int(m.group(2)))
    return (stem, 0)


def _guess_image_mime(p: Path) -> str:
    if p.suffix.lower() in (".png",):
        return "image/png"
    if p.suffix.lower() in (".webp",):
        return "image/webp"
    if p.suffix.lower() in (".gif",):
        return "image/gif"
    return "image/jpeg"


def _prepare_upload_image_body(path: Path) -> tuple[bytes, str, str]:
    """
    Bytes + multipart filename suffix + MIME for one image.

    Files over ~900KB are resized and saved as JPEG so uploads finish on slow links and
    stay under the CRM's per-file 5MB cap (some scraped PNGs are multi‑MB).
    """
    mime = _guess_image_mime(path)
    try:
        sz = path.stat().st_size
    except OSError:
        return path.read_bytes(), path.name, mime

    if path.suffix.lower() == ".gif" or sz <= _REENCODE_THRESHOLD_BYTES:
        return path.read_bytes(), path.name, mime

    try:
        from PIL import Image
    except ImportError:
        print(
            f"  WARNING: {path.name} is {sz // 1024}KB; install Pillow to compress before upload "
            f"(pip install Pillow)"
        )
        return path.read_bytes(), path.name, mime

    try:
        with Image.open(path) as raw:
            if raw.mode in ("RGBA", "LA"):
                im = raw.convert("RGBA")
                rgb = Image.new("RGB", im.size, (255, 255, 255))
                rgb.paste(im, mask=im.split()[-1])
                im = rgb
            elif raw.mode == "P" and "transparency" in raw.info:
                im = raw.convert("RGBA")
                rgb = Image.new("RGB", im.size, (255, 255, 255))
                rgb.paste(im, mask=im.split()[3])
                im = rgb
            else:
                im = raw.convert("RGB")
            w, h = im.size
            max_dim = 1600
            if max(w, h) > max_dim:
                ratio = max_dim / float(max(w, h))
                nw, nh = max(1, int(w * ratio)), max(1, int(h * ratio))
                im = im.resize((nw, nh), Image.Resampling.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=80, optimize=True)
            out = buf.getvalue()
        out_name = f"{path.stem}_upload.jpg"
        if len(out) >= sz:
            return path.read_bytes(), path.name, mime
        print(f"  INFO: Compressed {path.name} ({sz // 1024}KB → {len(out) // 1024}KB) for upload")
        return out, out_name, "image/jpeg"
    except Exception as e:
        print(f"  WARNING: Could not compress {path.name} ({e!s}); uploading original")
        return path.read_bytes(), path.name, mime


def _fallback_scraped_images_by_prefix(images: list, output_dir: Path) -> list[tuple[Path, str]]:
    """
    When products.json lists stale paths (wrong index/extension) but files exist under
    output_dir/images/ with the same basename prefix (e.g. ..._01.png missing, ..._05.jpg present).
    """
    img_dir = output_dir / "images"
    if not img_dir.is_dir():
        return []
    for fname in images:
        fname_str = (str(fname).split("?")[0] or "").strip().lstrip("/")
        if not fname_str or fname_str.lower().startswith("http"):
            continue
        if ".." in fname_str:
            continue
        stem = Path(fname_str).stem
        m = re.match(r"^(.+)_\d+$", stem)
        prefix = m.group(1) if m else stem
        matches = sorted(
            (
                p
                for p in img_dir.glob(f"{glob_escape(prefix)}_*")
                if p.suffix.lower() in _ALLOWED_IMAGE_SUFFIX
            ),
            key=_natural_image_sort_key,
        )
        rows = [(p, f"images/{p.name}") for p in matches if p.is_file()]
        if rows:
            print(
                f"  INFO: JSON image path(s) missing on disk; uploading {len(rows)} file(s) "
                f"from images/ matching prefix {prefix!r}"
            )
            return rows
    return []


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
    base_dir = output_dir
    if "companies" in output_dir.parts:
        base_dir = output_dir.parent.parent
    headers = {"Authorization": f"Bearer {token}", "X-Company-Slug": company_slug}
    upload_url = f"{base_url.rstrip('/')}/v1/products/images/upload-multiple/"

    resolved: list[tuple[Path, str]] = []
    for fname in images:
        fname_str = str(fname).split("?")[0] if fname else ""
        if not fname_str:
            continue
        path = _resolve_local_image_path(fname_str, output_dir, base_dir, sources_dict, company_slug)
        if path and path.is_file():
            resolved.append((path, fname_str))

    if not resolved:
        resolved = _fallback_scraped_images_by_prefix(images, output_dir)

    if not resolved:
        print("  ERROR: No image files found on disk for upload. Catalog paths:")
        for fname in images[:12]:
            fs = str(fname).split("?")[0] if fname else ""
            if not fs:
                continue
            tried = _resolve_local_image_path(fs, output_dir, base_dir, sources_dict, company_slug)
            exists = tried.is_file() if tried else False
            print(f"    - {fs!r} → resolved={tried!s} exists={exists}")
        if len(images) > 12:
            print(f"    ... and {len(images) - 12} more")
        img_dir = output_dir / "images"
        if img_dir.is_dir():
            try:
                on_disk = sorted(p.name for p in img_dir.iterdir() if p.is_file())[:20]
                if on_disk:
                    print(f"  HINT: files present under {img_dir}: {on_disk}{' ...' if len(list(img_dir.iterdir())) > 20 else ''}")
            except OSError:
                pass
        return None

    missing = len(images) - len(resolved)
    if missing > 0:
        print(f"  WARNING: {missing} image path(s) not found on disk (bundle/cross-supplier: use company-scoped paths)")

    upload_entries: list[tuple[bytes, str, str]] = []
    for path, fname_str in resolved:
        img_body, part_suffix, mime = _prepare_upload_image_body(path)
        upload_entries.append((img_body, part_suffix, mime))

    all_urls: list[str] = []
    for start in range(0, len(upload_entries), _UPLOAD_IMAGE_BATCH):
        batch = upload_entries[start : start + _UPLOAD_IMAGE_BATCH]
        batch_kb = sum(len(b) for b, _, _ in batch) // 1024
        batch_names = [s for _, s, _ in batch]
        files = []
        for j, (img_body, part_suffix, mime) in enumerate(batch):
            unique_name = f"{start + j:03d}_{uuid.uuid4().hex[:8]}_{part_suffix}"
            files.append(("files[]", (unique_name, img_body, mime)))
        result = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    upload_url,
                    headers=headers,
                    files=files,
                    timeout=(_UPLOAD_CONNECT_TIMEOUT, _UPLOAD_READ_TIMEOUT),
                )
                resp.raise_for_status()
                result = resp.json()
                break
            except requests.Timeout as e:
                if attempt < 2:
                    wait = 2**attempt
                    print(
                        f"  WARNING: upload timeout (attempt {attempt + 1}/3, {len(batch)} files, "
                        f"{batch_kb}KB) → {upload_url}  files={batch_names}; retrying in {wait}s…"
                    )
                    time.sleep(wait)
                    continue
                resp_body = ""
                if getattr(e, "response", None) is not None:
                    resp_body = (e.response.text or "")[:400]
                print(
                    f"  ERROR: upload failed after 3 attempts ({len(batch)} files, {batch_kb}KB)\n"
                    f"    URL: {upload_url}\n"
                    f"    Files: {batch_names}\n"
                    f"    Error: {e}\n"
                    f"    Response: {resp_body or '(none)'}"
                )
                return None
            except requests.ConnectionError as e:
                cause = getattr(e, "__cause__", None)
                transient = isinstance(cause, TimeoutError) or (
                    "timed out" in str(e).lower() or "timeout" in str(e).lower()
                )
                if transient and attempt < 2:
                    wait = 2**attempt
                    print(
                        f"  WARNING: upload write stalled (attempt {attempt + 1}/3, {len(batch)} files, "
                        f"{batch_kb}KB) → {upload_url}  files={batch_names}; retrying in {wait}s…"
                    )
                    time.sleep(wait)
                    continue
                resp_body = ""
                if getattr(e, "response", None) is not None:
                    resp_body = (e.response.text or "")[:400]
                print(
                    f"  ERROR: upload failed after 3 attempts ({len(batch)} files, {batch_kb}KB)\n"
                    f"    URL: {upload_url}\n"
                    f"    Files: {batch_names}\n"
                    f"    Error: {e}\n"
                    f"    Response: {resp_body or '(none)'}"
                )
                return None
            except requests.RequestException as e:
                resp_body = ""
                if getattr(e, "response", None) is not None:
                    resp_body = (e.response.text or "")[:400]
                print(
                    f"  ERROR: upload failed ({len(batch)} files, {batch_kb}KB)\n"
                    f"    URL: {upload_url}\n"
                    f"    Files: {batch_names}\n"
                    f"    Error: {e}\n"
                    f"    Response: {resp_body or '(none)'}"
                )
                return None
            except Exception as e:
                print(
                    f"  ERROR: upload unexpected ({len(batch)} files, {batch_kb}KB)\n"
                    f"    URL: {upload_url}\n"
                    f"    Files: {batch_names}\n"
                    f"    Error: {type(e).__name__}: {e}"
                )
                return None
        if result is None:
            return None
        if not result.get("success") or not result.get("data"):
            print(f"  ERROR: upload-multiple rejected: success={result.get('success')!r} data={str(result)[:400]}")
            return None
        batch_urls = []
        for img in result["data"]:
            if not isinstance(img, dict):
                continue
            u = img.get("url") or img.get("file_url")
            if u:
                batch_urls.append(u)
        if len(batch_urls) < len(batch):
            print(
                f"  WARNING: upload-multiple returned {len(batch_urls)}/{len(batch)} URLs "
                "(check API errors / file size limits)"
            )
        if not batch_urls:
            return None
        all_urls.extend(batch_urls)

    if not all_urls:
        return None
    return all_urls[0], all_urls[1:]


def _backend_has_images_from_snapshot(prod_obj: dict | None) -> bool | None:
    """
    True if CRM product payload clearly has a main image or non-empty gallery.
    False if dict exists but both are empty.
    None if prod_obj is missing / not a dict (caller treats as unknown → upload when skipping forced sync).
    """
    if not isinstance(prod_obj, dict):
        return None
    main = prod_obj.get("image")
    if isinstance(main, str) and main.strip():
        return True
    if isinstance(main, dict):
        url = (main.get("url") or main.get("file_url") or "").strip()
        if url:
            return True
    extras = prod_obj.get("images") or []
    if isinstance(extras, list) and len(extras) > 0:
        return True
    return False


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
    sync_images: bool = True,
) -> bool:
    """
    Update existing product via PATCH.

    When ``sync_images`` is True and local ``images`` exist, re-uploads and PATCHes ``image`` / ``images``.
    When ``sync_images`` is False, uploads only if the CRM snapshot shows no usable images (or state unknown).
    """
    if _is_gumtree_source(source):
        ok, missing = _validate_gumtree_pickup_origin(data)
        if not ok:
            print(
                "  WARNING: Update skipped for Gumtree product "
                f"{data.get('name', '')[:40]}... missing pickup-origin fields: {', '.join(missing)} "
                "(set GUMTREE_PICKUP_* in products/.env or populate fields in product JSON)"
            )
            return False

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Company-Slug": company_slug,
        "Content-Type": "application/json",
    }

    # Existence check before image upload; skip when updating fields-only (PATCH handles 404).
    check_url = f"{base_url.rstrip('/')}/v1/products/{product_id}/"
    prod_snapshot: dict | None = None
    if sync_images:
        try:
            check = requests.get(check_url, headers=headers, timeout=15)
            if check.status_code == 404:
                print(f"  INFO: Product {product_id[:12]}… gone from prod (404) – will re-create")
                return "not_found"
            if check.status_code == 200:
                prod_data = check.json()
                prod_obj = prod_data.get("data", prod_data) if isinstance(prod_data, dict) else prod_data
                if isinstance(prod_obj, dict):
                    prod_snapshot = prod_obj
                status_val = (prod_obj.get("status") or "").lower() if isinstance(prod_obj, dict) else ""
                if status_val in ("archived", "deleted", "inactive"):
                    print(f"  INFO: Product {product_id[:12]}… is {status_val} on prod – will re-create")
                    return "not_found"
        except requests.RequestException:
            pass

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
        payload["supplier_delivery_cost"] = _crm_supplier_flat_delivery_cost(data, supp)
        payload["free_delivery_threshold"] = _resolve_free_delivery_threshold(data, supp)
    if _is_gumtree_source(source):
        payload.update(_extract_pickup_origin(data, with_env_fallback=True))
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
    su = _source_url_for_crm_payload(data.get("url") or "")
    if su:
        payload["source_url"] = su
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
    should_upload_images = False
    if output_dir and images:
        if sync_images:
            should_upload_images = True
        elif prod_snapshot is not None:
            should_upload_images = _backend_has_images_from_snapshot(prod_snapshot) is not True

    if should_upload_images:
        uploaded = _upload_images(images, output_dir, base_url, token, company_slug, sources_dict)
        if uploaded:
            main_url, extra_urls = uploaded
            payload["image"] = main_url
            payload["images"] = extra_urls

    # PATCH partial updates ignore missing keys. After changing supplier_delivery in
    # scraper_config, we must send explicit null to clear stale CRM decimals — so keep
    # these through the None-strip (same idea as timed_duration_minutes below).
    _supplier_delivery_for_patch = None
    if source:
        _supplier_delivery_for_patch = {
            "supplier_delivery_cost": payload.get("supplier_delivery_cost"),
            "free_delivery_threshold": payload.get("free_delivery_threshold"),
        }

    payload = {k: v for k, v in payload.items() if v is not None}
    if _supplier_delivery_for_patch is not None:
        payload["supplier_delivery_cost"] = _supplier_delivery_for_patch["supplier_delivery_cost"]
        payload["free_delivery_threshold"] = _supplier_delivery_for_patch["free_delivery_threshold"]
    if "timed_duration_minutes" in data:
        val = data["timed_duration_minutes"]
        payload["timed_duration_minutes"] = int(val) if val is not None else None  # Include None to clear

    try:
        _ensure_compare_at_gte_price(payload)
        r = requests.patch(
            f"{base_url.rstrip('/')}/v1/products/{product_id}/",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if not r.ok and _is_compare_at_price_validation_error(r.status_code, r.text):
            _ensure_compare_at_gte_price(payload, on_api_rejection=True)
            r = requests.patch(
                f"{base_url.rstrip('/')}/v1/products/{product_id}/",
                headers=headers,
                json=payload,
                timeout=30,
            )
        r.raise_for_status()
        invalidate_product_list_cache(company_slug)
        return True
    except requests.RequestException as e:
        err_status = None
        body = str(e)
        if hasattr(e, "response") and getattr(e, "response", None) is not None:
            err_status = getattr(e.response, "status_code", None)
            body = (e.response.text or "")[:200]
        body_lower = (body or "").lower()
        if err_status in (400, 404) and "no ecommerceproduct matches the given query" in body_lower:
            print(f"  INFO: Product deleted from prod – will re-create: {data.get('name', '')[:40]}...")
            return "not_found"
        else:
            print(f"  WARNING: Update failed for {data.get('name', '')[:40]}... (status={err_status}): {body}")
        return False


_auth_token_cache: dict[str, tuple[float, str]] = {}
_AUTH_TOKEN_TTL = 600.0


def invalidate_auth_token_cache(company_slug: str | None = None, username: str | None = None) -> None:
    """Clear cached JWT after credential changes or auth failures."""
    global _auth_token_cache
    if not company_slug and not username:
        _auth_token_cache.clear()
        return
    cs = (company_slug or "").strip()
    user = (username or "").strip()
    drop = [k for k in _auth_token_cache if (not cs or f"|{cs}|" in k) and (not user or k.endswith(f"|{user}"))]
    for k in drop:
        _auth_token_cache.pop(k, None)


def get_auth_token(base_url: str, username: str, password: str, company_slug: str = "", use_email: bool = False) -> str | None:
    """JWT login. Returns access token or None.

    Django CRM ``CustomTokenObtainPairView`` only reads the ``username`` key from JSON
    (value may be django username or email if the server resolves email). ``use_email``
    is kept for callers but no longer switches the JSON key to ``email`` (that caused
    400 Username required → login failure).
    """
    cache_key = f"{base_url.rstrip('/')}|{(company_slug or '').strip()}|{username}"
    cached = _auth_token_cache.get(cache_key)
    if cached and time.time() < cached[0]:
        return cached[1]

    payload = {"password": password, "username": username}
    if company_slug:
        payload["company_slug"] = company_slug
    try:
        r = requests.post(
            f"{base_url.rstrip('/')}/auth/login/",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=(5.0, 45.0),
        )
        if r.status_code != 200:
            invalidate_auth_token_cache(company_slug, username)
            return None
        data = r.json()
        token = data.get("access") or data.get("token") or data.get("access_token")
        if not token and isinstance(data.get("tokens"), dict):
            token = data["tokens"].get("access") or data["tokens"].get("token")
        if token:
            _auth_token_cache[cache_key] = (time.time() + _AUTH_TOKEN_TTL, token)
        return token
    except Exception:
        invalidate_auth_token_cache(company_slug, username)
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
        "source_url": _source_url_for_crm_payload(data.get("url") or ""),
    }
    if data.get("compare_at_price"):
        payload["compare_at_price"] = str(data["compare_at_price"])
    if data.get("cost") is not None:
        payload["cost_price"] = str(data["cost"])
    # Derive supplier_slug: prefer bundle_source; when output_dir is company-scoped (path/.../companies/xxx), use parent of scraped dir
    if bundle_source:
        supplier_slug = bundle_source
    elif output_dir:
        if "companies" in output_dir.parts:
            idx = list(output_dir.parts).index("companies")
            supplier_slug = output_dir.parts[idx - 2] if idx >= 2 else output_dir.parent.name
        else:
            supplier_slug = output_dir.parent.name
    else:
        supplier_slug = ""
    if _is_gumtree_source(supplier_slug):
        ok, missing = _validate_gumtree_pickup_origin(data)
        if not ok:
            print(
                "  SKIP: Gumtree product missing pickup-origin fields "
                f"({', '.join(missing)}): {data.get('name', '')[:50]} "
                "(set GUMTREE_PICKUP_* in products/.env or populate fields in product JSON)"
            )
            return None
    if data.get("delivery_time"):
        payload["delivery_time"] = str(data["delivery_time"])[:100]
    elif supplier_slug:
        supp = get_supplier_delivery(supplier_slug)
        if supp.get("delivery_time"):
            payload["delivery_time"] = str(supp["delivery_time"])[:100]
    if supplier_slug:
        payload["supplier_slug"] = supplier_slug
        supp = get_supplier_delivery(supplier_slug)
        payload["supplier_delivery_cost"] = _crm_supplier_flat_delivery_cost(data, supp)
        payload["free_delivery_threshold"] = _resolve_free_delivery_threshold(data, supp)
    if _is_gumtree_source(supplier_slug):
        payload.update(_extract_pickup_origin(data, with_env_fallback=True))
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
        _ensure_compare_at_gte_price(payload)
        r = requests.post(
            f"{base_url.rstrip('/')}/v1/products/",
            headers=headers,
            json=payload,
            timeout=30,
        )
        if not r.ok and _is_compare_at_price_validation_error(r.status_code, r.text):
            _ensure_compare_at_gte_price(payload, on_api_rejection=True)
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
        invalidate_product_list_cache(company_slug)
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
