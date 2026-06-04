"""Helpers for bulk product verification (stock, price, availability)."""
import sys
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.suppliers import get_supplier

SOLD_OUT_PREFIX = "** SOLD OUT **"

UNAVAILABLE_MARKERS = (
    "ad has been removed",
    "listing is no longer available",
    "this ad is no longer",
    "page not found",
    "404 not found",
    "item is unavailable",
    "product is unavailable",
    "no longer available",
)

SOLD_OUT_MARKERS = (
    "sold out",
    "out of stock",
    "currently unavailable",
    "not available",
    "temporarily out of stock",
)


def prepend_sold_out_name(name: str) -> str:
    """Prepend SOLD OUT prefix to product name (idempotent)."""
    name = (name or "").strip()
    if not name:
        return SOLD_OUT_PREFIX.strip()
    if name.startswith(SOLD_OUT_PREFIX):
        return name
    return f"{SOLD_OUT_PREFIX} {name}"


def strip_sold_out_name(name: str) -> str:
    """Remove SOLD OUT prefix from product name (idempotent)."""
    name = (name or "").strip()
    if name.startswith(SOLD_OUT_PREFIX):
        return name[len(SOLD_OUT_PREFIX) :].strip()
    return name


def product_marked_sold_out(product: dict) -> bool:
    """True when local product was marked sold out by a prior verify run."""
    if product.get("in_stock") is False:
        return True
    return (product.get("name") or "").startswith(SOLD_OUT_PREFIX)


def page_text_indicates_unavailable(text: str) -> bool:
    """True when page text suggests listing is gone."""
    lower = (text or "").lower()
    return any(m in lower for m in UNAVAILABLE_MARKERS)


def page_text_indicates_sold_out(text: str) -> bool:
    """True when page text suggests item is sold out but page exists."""
    lower = (text or "").lower()
    return any(m in lower for m in SOLD_OUT_MARKERS)


def is_supplier_supported(source: str) -> bool:
    """True if supplier module defines fetch_current_pricing (without importing scraper deps)."""
    info = get_supplier(source)
    if not info or not (info.module_name or "").strip():
        return False
    parts = info.module_name.split(".")
    if len(parts) < 2:
        return False
    mod_path = PRODUCTS_ROOT / parts[0] / f"{parts[1]}.py"
    if not mod_path.is_file():
        return False
    try:
        text = mod_path.read_text(encoding="utf-8")
    except Exception:
        return False
    return "def fetch_current_pricing" in text


def pricing_result(
    *,
    price=None,
    cost=None,
    source_price=None,
    in_stock: bool = True,
    unavailable: bool = False,
) -> dict:
    """Standard fetch_current_pricing return shape."""
    return {
        "valid": True,
        "price": price,
        "cost": cost,
        "source_price": source_price,
        "in_stock": in_stock,
        "unavailable": unavailable,
    }


def playwright_page_in_stock(page) -> bool:
    """Check Playwright page for sold-out / unavailable markers."""
    scopes = (
        "form.cart",
        ".summary.entry-summary",
        ".single-product",
        ".product.type-product",
        "[data-section-type='product_page']",
        "main",
    )
    for sel in scopes:
        try:
            text = (page.inner_text(sel) or "").lower()
        except Exception:
            continue
        if not text.strip():
            continue
        if page_text_indicates_unavailable(text) or page_text_indicates_sold_out(text):
            return False
        return True
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return True
    if page_text_indicates_unavailable(body) or page_text_indicates_sold_out(body):
        return False
    return True
