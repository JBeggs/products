"""Shared utilities for product scrapers."""
import html
import random
import re

# Compare-at price: random 8%–25% above sell price (per product)
COMPARE_AT_PRICE_MIN = 1.08  # 8% above
COMPARE_AT_PRICE_MAX = 1.25  # 25% above


def get_compare_at_price(sell_price: float) -> float:
    """Return compare_at_price as sell_price × random(1.08, 1.25)."""
    mult = random.uniform(COMPARE_AT_PRICE_MIN, COMPARE_AT_PRICE_MAX)
    return round(sell_price * mult, 2)


# Tiered markup uses supplier cost (with optional import uplift), then multiplier.
# Configurable via scraper_config.json (see shared.config.get_tier_multipliers)

# Only import suppliers should carry the +20% uplift.
IMPORT_SUPPLIERS = frozenset({"temu", "ubuy", "aliexpress"})


def calculate_supplier_cost(sale_price_cents: int, supplier_slug: str | None = None) -> float:
    """Return supplier cost in ZAR; +20% uplift only for configured import suppliers."""
    base_cost = sale_price_cents / 100
    slug = (supplier_slug or "").strip().lower()
    if slug in IMPORT_SUPPLIERS:
        return round(base_cost * 1.2, 2)
    return round(base_cost, 2)


def apply_tiered_markup(sale_price_cents: int, supplier_slug: str | None = None) -> float:
    """Apply tiered markup over supplier cost. Returns sell price in ZAR."""
    from shared.config import get_tier_multipliers

    cost = calculate_supplier_cost(sale_price_cents, supplier_slug)
    tiers = get_tier_multipliers(supplier_slug)
    if not tiers:
        raise ValueError(
            f"No pricing tiers configured for supplier {supplier_slug!r}. "
            "Configure tiers in the scraper page (Tiered markup section) before scraping."
        )
    for threshold, mult in tiers:
        if cost < threshold:
            return round(cost * mult, 2)
    _, last_mult = tiers[-1]
    return round(cost * last_mult, 2)


def slugify(text: str) -> str:
    """Create URL-safe slug from product title."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text[:80].rstrip("-") if text else "product"


def image_prefix(name: str, max_chars: int = 20) -> str:
    """First N chars of product name, sanitized for filenames."""
    s = re.sub(r"[^\w\s-]", "", name.lower().strip())[:max_chars]
    s = re.sub(r"[-\s]+", "_", s).strip("_")
    return s or "product"


def truncate_name(name: str, max_len: int = 80) -> str:
    """Shorter product name for display."""
    name = name.strip()
    if len(name) <= max_len:
        return name
    return name[: max_len - 2].rsplit(" ", 1)[0] + ".."


def first_n_words(text: str, n: int = 5) -> str:
    """Return first N words of text."""
    words = text.strip().split()
    return " ".join(words[:n]) if words else ""


def remove_special_chars(text: str) -> str:
    """Remove HTML entities and other special chars."""
    if not text:
        return ""
    text = html.unescape(text)
    text = text.replace("\u00a0", " ").replace("\u2003", " ").replace("\u2002", " ")
    text = re.sub(r" +", " ", text)
    return text.strip()


DESCRIPTION_EXCLUDE_PATTERNS = [
    r"Product details\s*\n",
    r"Save\s*\n",
    r"Report this item\s*\n",
    r"See all details(?:\s+and\s+dimensions)?\s*\n",
    r"Store Information\s*",
]


def clean_description(text: str) -> str:
    """Strip unwanted UI text and special chars from description."""
    if not text:
        return ""
    text = remove_special_chars(text)
    for pattern in DESCRIPTION_EXCLUDE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
