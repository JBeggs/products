"""
Refresh product pricing from source URLs.
Dispatches to supplier-specific fetch, compares with stored values, builds notes.
"""
import sys
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.suppliers import get_supplier


def _load_product(source: str, index: int) -> dict | None:
    """Load product at index from products.json. Returns None if invalid."""
    from shared.suppliers import get_sources_for_edit
    sources = get_sources_for_edit()
    base = sources.get(source)
    if not base:
        return None
    path = base / "products.json"
    if not path.exists():
        return None
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        products = data.get("products", [])
        if 0 <= index < len(products):
            return products[index]
    except Exception:
        pass
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


def fetch_current_pricing(source: str, url: str) -> dict | None:
    """
    Fetch current price/cost from supplier URL. No persistence.
    Returns {price, cost, source_price, valid: True} or None if invalid/blocked.
    """
    info = get_supplier(source)
    if not info:
        return None

    import importlib
    mod = importlib.import_module(info.module_name)
    if not hasattr(mod, "fetch_current_pricing"):
        return None

    return mod.fetch_current_pricing(url)


def refresh_product(source: str, index: int) -> dict:
    """
    Refresh one product: fetch current pricing, compare, build note.
    Returns {valid, new_price, new_cost, price_change_note, error?}
    """
    product = _load_product(source, index)
    if not product:
        return {"valid": False, "error": "Product not found"}

    url = (product.get("url") or "").strip()
    if not url:
        return {"valid": False, "error": "No URL"}

    fresh = fetch_current_pricing(source, url)
    if not fresh:
        return {
            "valid": False,
            "price_change_note": "Product no longer available or page blocked",
        }

    source_key = _source_price_key(source)
    old_source_price = product.get(source_key)
    old_price = product.get("price")
    old_cost = product.get("cost")

    new_price = fresh.get("price")
    new_cost = fresh.get("cost")
    new_source_price = fresh.get("source_price")

    notes = []
    if old_source_price is not None and new_source_price is not None:
        try:
            old_v = float(old_source_price)
            new_v = float(new_source_price)
            if abs(new_v - old_v) > 0.01:
                direction = "up" if new_v > old_v else "down"
                notes.append(f"Source: R{old_v:.2f} → R{new_v:.2f} ({direction})")
        except (TypeError, ValueError):
            pass

    if old_cost is not None and new_cost is not None:
        try:
            old_v = float(old_cost)
            new_v = float(new_cost)
            if abs(new_v - old_v) > 0.01:
                direction = "up" if new_v > old_v else "down"
                notes.append(f"Cost: R{old_v:.2f} → R{new_v:.2f} ({direction})")
        except (TypeError, ValueError):
            pass

    if old_price is not None and new_price is not None:
        try:
            old_v = float(old_price)
            new_v = float(new_price)
            if abs(new_v - old_v) > 0.01:
                direction = "up" if new_v > old_v else "down"
                notes.append(f"Price: R{old_v:.2f} → R{new_v:.2f} ({direction})")
        except (TypeError, ValueError):
            pass

    price_change_note = "; ".join(notes) if notes else "No change"
    return {
        "valid": True,
        "new_price": new_price,
        "new_cost": new_cost,
        "new_source_price": new_source_price,
        "price_change_note": price_change_note,
    }
