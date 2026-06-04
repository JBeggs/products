"""
Refresh product pricing from source URLs.
Dispatches to supplier-specific fetch, compares with stored values, builds notes.
"""
import json
import sys
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.suppliers import get_supplier
from shared.verify_utils import (
    is_supplier_supported,
    prepend_sold_out_name,
    product_marked_sold_out,
    strip_sold_out_name,
)

_PRICING_MODULE_CACHE: dict[str, object] = {}


def _load_products_file(source: str, company_slug: str) -> tuple[list, Path | None]:
    """Load products list and path from company-scoped products.json."""
    if not (company_slug or "").strip():
        return [], None
    from shared.suppliers import get_sources_for_edit, get_company_scoped_dir

    sources = get_sources_for_edit()
    base = sources.get(source)
    if not base:
        return [], None
    company_dir = get_company_scoped_dir(base, company_slug)
    path = company_dir / "products.json"
    if not path.exists():
        return [], path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("products", []), path
    except Exception:
        return [], path


def _load_product(source: str, index: int, company_slug: str = "") -> dict | None:
    """Load product at index from company-scoped products.json. Returns None if invalid."""
    products, _ = _load_products_file(source, company_slug)
    if 0 <= index < len(products):
        return products[index]
    return None


def _source_price_key(source: str) -> str:
    """Return the source price field name for this supplier."""
    return {
        "temu": "temu_price",
        "gumtree": "gumtree_price",
        "aliexpress": "aliexpress_price",
        "makro": "makro_price",
        "constructionhyper": "constructionhyper_price",
        "game": "game_price",
        "loot": "loot_price",
        "perfectdealz": "perfectdealz_price",
        "matrixwarehouse": "matrixwarehouse_price",
        "takealot": "takealot_price",
        "ubuy": "ubuy_price",
        "myrunway": "myrunway_price",
        "onedayonly": "onedayonly_price",
    }.get(source, f"{source}_price")


def _prices_differ(old, new, tolerance: float = 0.01) -> bool:
    if old is None or new is None:
        return False
    try:
        return abs(float(new) - float(old)) > tolerance
    except (TypeError, ValueError):
        return False


def _build_price_notes(product: dict, source: str, fresh: dict) -> list[str]:
    source_key = _source_price_key(source)
    notes = []
    pairs = (
        ("Source", product.get(source_key), fresh.get("source_price")),
        ("Cost", product.get("cost"), fresh.get("cost")),
        ("Price", product.get("price"), fresh.get("price")),
    )
    for label, old_v, new_v in pairs:
        if _prices_differ(old_v, new_v):
            direction = "up" if float(new_v) > float(old_v) else "down"
            notes.append(f"{label}: R{float(old_v):.2f} → R{float(new_v):.2f} ({direction})")
    return notes


def fetch_current_pricing(source: str, url: str, company_slug: str = "", product: dict | None = None) -> dict | None:
    """
    Fetch current price/cost from supplier URL. No persistence.
    Returns {price, cost, source_price, valid, in_stock, unavailable} or None if invalid/blocked.
    """
    info = get_supplier(source)
    if not info:
        return None

    import importlib
    import inspect

    from shared.config import get_scrape_company_slug, set_scrape_company_slug

    mod = _PRICING_MODULE_CACHE.get(info.module_name)
    if mod is None:
        mod = importlib.import_module(info.module_name)
        _PRICING_MODULE_CACHE[info.module_name] = mod
    if not hasattr(mod, "fetch_current_pricing"):
        return None

    prev_company = get_scrape_company_slug()
    cs = (company_slug or "").strip() or None
    try:
        if cs:
            set_scrape_company_slug(cs)
        fn = mod.fetch_current_pricing
        params = inspect.signature(fn).parameters
        if "product" in params:
            return fn(url, product=product)
        return fn(url)
    finally:
        set_scrape_company_slug(prev_company)


def apply_verification(product: dict, source: str, fresh: dict) -> dict:
    """
    Apply verification result to product dict (in memory).
    Returns {price_changed: bool, sold_out: bool}.
    """
    changed = {"price_changed": False, "sold_out": False, "restored_stock": False}
    in_stock = fresh.get("in_stock", True)
    if in_stock is False or fresh.get("unavailable"):
        product["in_stock"] = False
        product["stock_quantity"] = 0
        old_name = product.get("name") or ""
        new_name = prepend_sold_out_name(old_name)
        if new_name != old_name:
            product["name"] = new_name
        changed["sold_out"] = True
        return changed

    if product.get("in_stock") is False:
        product["in_stock"] = True
        changed["restored_stock"] = True
    old_name = product.get("name") or ""
    stripped_name = strip_sold_out_name(old_name)
    if stripped_name != old_name:
        product["name"] = stripped_name
        changed["restored_stock"] = True

    source_key = _source_price_key(source)
    if fresh.get("price") is not None:
        if _prices_differ(product.get("price"), fresh.get("price")):
            changed["price_changed"] = True
        product["price"] = fresh.get("price")
    if fresh.get("cost") is not None:
        if _prices_differ(product.get("cost"), fresh.get("cost")):
            changed["price_changed"] = True
        product["cost"] = fresh.get("cost")
    if fresh.get("source_price") is not None:
        if _prices_differ(product.get(source_key), fresh.get("source_price")):
            changed["price_changed"] = True
        product[source_key] = fresh.get("source_price")
    return changed


def verify_product(source: str, index: int, company_slug: str = "") -> dict:
    """
    Verify one product: fetch live data, compare, optionally describe changes.
    Returns status: ok | price_changed | sold_out | unavailable | error | unsupported
    """
    if not is_supplier_supported(source):
        return {
            "status": "unsupported",
            "note": "No price checker for this supplier",
            "source": source,
            "index": index,
        }

    product = _load_product(source, index, company_slug)
    if not product:
        return {
            "status": "error",
            "note": "Product not found",
            "source": source,
            "index": index,
        }

    url = (product.get("url") or "").strip()
    name = (product.get("name") or "").strip()
    if not url:
        return {
            "status": "error",
            "note": "No URL",
            "source": source,
            "index": index,
            "name": name,
        }

    try:
        fresh = fetch_current_pricing(source, url, company_slug, product=product)
    except Exception as e:
        return {
            "status": "error",
            "note": str(e),
            "source": source,
            "index": index,
            "name": name,
        }

    if not fresh:
        return {
            "status": "error",
            "note": "Could not fetch price (page blocked or extraction failed)",
            "source": source,
            "index": index,
            "name": name,
            "apply": False,
        }

    if fresh.get("in_stock") is False or fresh.get("unavailable"):
        notes = _build_price_notes(product, source, fresh)
        note = "Sold out / unavailable"
        if notes:
            note += "; " + "; ".join(notes)
        return {
            "status": "sold_out",
            "note": note,
            "source": source,
            "index": index,
            "name": name,
            "fresh": fresh,
            "apply": True,
        }

    was_sold_out = product_marked_sold_out(product)
    notes = _build_price_notes(product, source, fresh)
    if notes:
        return {
            "status": "price_changed",
            "note": "; ".join(notes),
            "source": source,
            "index": index,
            "name": name,
            "fresh": fresh,
            "apply": True,
        }

    if was_sold_out:
        return {
            "status": "ok",
            "note": "Back in stock",
            "source": source,
            "index": index,
            "name": name,
            "fresh": fresh,
            "apply": True,
        }

    return {
        "status": "ok",
        "note": "No change",
        "source": source,
        "index": index,
        "name": name,
        "fresh": fresh,
        "apply": False,
    }


def verify_and_save_product(source: str, index: int, company_slug: str = "") -> dict:
    """Verify one product and persist local JSON updates when needed."""
    result = verify_product(source, index, company_slug)
    status = result.get("status")
    if status == "unsupported":
        return result

    should_apply = result.get("apply") or status == "unavailable"
    if not should_apply:
        return result

    products, path = _load_products_file(source, company_slug)
    if not path or not (0 <= index < len(products)):
        result["status"] = "error"
        result["note"] = "Product not found for save"
        return result

    product = products[index]
    if status == "unavailable":
        product["in_stock"] = False
        product["stock_quantity"] = 0
        old_name = product.get("name") or ""
        product["name"] = prepend_sold_out_name(old_name)
        result["status"] = "sold_out"
        result["note"] = "Listing unavailable — marked sold out"
    elif result.get("fresh"):
        applied = apply_verification(product, source, result["fresh"])
        if applied.get("sold_out"):
            result["status"] = "sold_out"
        elif applied.get("price_changed"):
            result["status"] = "price_changed"
        elif applied.get("restored_stock") and result.get("status") == "ok":
            result["note"] = "Back in stock"

    from datetime import datetime

    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    data["products"] = products
    data["updated"] = datetime.now().isoformat()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return result


def refresh_product(source: str, index: int, company_slug: str = "") -> dict:
    """
    Refresh one product: fetch current pricing, compare, build note.
    Returns {valid, new_price, new_cost, price_change_note, error?}
    """
    product = _load_product(source, index, company_slug)
    if not product:
        return {"valid": False, "error": "Product not found"}

    url = (product.get("url") or "").strip()
    if not url:
        return {"valid": False, "error": "No URL"}

    fresh = fetch_current_pricing(source, url, company_slug, product=product)
    if not fresh:
        return {
            "valid": False,
            "price_change_note": "Product no longer available or page blocked",
        }

    if fresh.get("in_stock") is False or fresh.get("unavailable"):
        return {
            "valid": False,
            "price_change_note": "Product sold out or unavailable",
            "in_stock": False,
        }

    source_key = _source_price_key(source)
    old_source_price = product.get(source_key)
    old_price = product.get("price")
    old_cost = product.get("cost")

    new_price = fresh.get("price")
    new_cost = fresh.get("cost")
    new_source_price = fresh.get("source_price")

    notes = []
    if _prices_differ(old_source_price, new_source_price):
        old_v = float(old_source_price)
        new_v = float(new_source_price)
        direction = "up" if new_v > old_v else "down"
        notes.append(f"Source: R{old_v:.2f} → R{new_v:.2f} ({direction})")

    if _prices_differ(old_cost, new_cost):
        old_v = float(old_cost)
        new_v = float(new_cost)
        direction = "up" if new_v > old_v else "down"
        notes.append(f"Cost: R{old_v:.2f} → R{new_v:.2f} ({direction})")

    if _prices_differ(old_price, new_price):
        old_v = float(old_price)
        new_v = float(new_price)
        direction = "up" if new_v > old_v else "down"
        notes.append(f"Price: R{old_v:.2f} → R{new_v:.2f} ({direction})")

    price_change_note = "; ".join(notes) if notes else "No change"
    return {
        "valid": True,
        "new_price": new_price,
        "new_cost": new_cost,
        "new_source_price": new_source_price,
        "price_change_note": price_change_note,
    }


def collect_verify_targets(company_slug: str, scope: str = "all", source: str | None = None) -> list[dict]:
    """Build list of {source, index, name, url} to verify."""
    from shared.suppliers import get_sources_for_edit

    sources = get_sources_for_edit()
    slugs = [source] if scope == "source" and source else list(sources.keys())
    targets = []
    for slug in slugs:
        products, _ = _load_products_file(slug, company_slug)
        for idx, prod in enumerate(products):
            url = (prod.get("url") or "").strip()
            if not url:
                continue
            targets.append({
                "source": slug,
                "index": idx,
                "name": (prod.get("name") or "").strip(),
                "url": url,
            })
    return targets
