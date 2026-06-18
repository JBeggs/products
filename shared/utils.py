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

# Legacy alias — prefer shared.config.is_import_supplier (category == import).
IMPORT_SUPPLIERS = frozenset({"temu", "ubuy", "aliexpress", "shein"})


def calculate_supplier_cost(
    sale_price_cents: int,
    supplier_slug: str | None = None,
    company_slug: str | None = None,
) -> float:
    """Return supplier cost in ZAR; import suppliers use configurable uplift before tier markup."""
    from shared.config import get_import_cost_multiplier, get_scrape_company_slug, is_import_supplier

    base_cost = sale_price_cents / 100
    slug = (supplier_slug or "").strip().lower()
    if is_import_supplier(slug):
        cs = company_slug if company_slug is not None else get_scrape_company_slug()
        mult = get_import_cost_multiplier(slug, cs)
        return round(base_cost * mult, 2)
    return round(base_cost, 2)


def apply_tiered_markup(sale_price_cents: int, supplier_slug: str | None = None, company_slug: str | None = None) -> float:
    """Apply tiered markup over supplier cost. Returns sell price in ZAR. company_slug for company-scoped tiers (or from scrape context)."""
    from shared.config import get_tier_multipliers, get_scrape_company_slug

    cs = company_slug if company_slug is not None else get_scrape_company_slug()
    cost = calculate_supplier_cost(sale_price_cents, supplier_slug, cs)
    tiers = get_tier_multipliers(supplier_slug, cs)
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


def format_northernbolt_kit_description(text: str) -> str:
    """
    Turn Northern Bolt / INGCO-style single-line kit descriptions into readable blocks.

    Handles: \"Kit includes...:1x ...\", \")1x\" kit lines, \"Product details:\", stuck
    \"...Ingco\" / \"...Product details\", and spec bullets \"- Item\".
    """
    if not text:
        return ""
    t = html.unescape(text)
    t = t.replace("\u00a0", " ").replace("\u2003", " ").replace("\u2002", " ")
    t = re.sub(r"\s+", " ", t).strip()

    # Intro line then first quantity (Kit includes the following:1x …)
    t = re.sub(
        r"(Kit includes[^\d:]{0,50}:|Kit includes:|Combo Kit Includes:|Combo kit includes:)\s*",
        r"\1\n\n",
        t,
        flags=re.I,
    )
    # "Includes:1x" without "Kit" prefix
    t = re.sub(r"((?:Combo\s+)?Kit\s+Includes:)\s*(\d)", r"\1\n\n\2", t, flags=re.I)

    # New kit line after closing paren: ")1x " ")2x "
    t = re.sub(r"\)\s*(\d+x\s)", r")\n\n\1", t, flags=re.I)

    # "20V 1x Ingco", "set 10 x Abrasive" (qty separated from preceding token by spaces)
    t = re.sub(
        r"(?<=[A-Za-z0-9\])])\s+(\d{1,2}\s*x\s+)(?=[A-Za-z0-9\(])",
        r"\n\n\1",
        t,
        flags=re.I,
    )

    # Word run (3+ letters) directly before "Nx " — Bits1x, set10x, charger3x; avoids "20V 2x"
    t = re.sub(r"([A-Za-z]{3,})(\d+x\s)", r"\1\n\2", t)

    # Stuck to "Ingco" (caseIngco, mmIngco); do not use bare digit — would break "M14 Ingco" alone
    t = re.sub(r"([a-z)])(Ingco\s)", r"\1\n\n\2", t, flags=re.I)
    t = re.sub(r"\b([a-z]{4,})\s+(Ingco\s)", r"\1\n\n\2", t, flags=re.I)
    # "M14 Ingco Cordless …" (spec value + next product line)
    t = re.sub(r"(M\d+)\s+(Ingco\s)(?=Cordless)", r"\1\n\n\2", t, flags=re.I)
    # "185mm Ingco Cordless", ") Ingco" — skip "1x Ingco" / "2x Ingco" (x before space)
    t = re.sub(r"(?<![0-9])(?<!x)\s+(Ingco\s)(?=[A-Z][a-z-])", r"\n\n\1", t)

    # "Product details:" / "Product details:" with accidental run-on (BrushlessProduct, 20VProduct)
    t = re.sub(r"\s*(Product\s+details?:)", r"\n\n\1", t, flags=re.I)
    t = re.sub(r"([a-z0-9])(Product\s+details?:)", r"\1\n\n\2", t, flags=re.I)

    # Spec lines: "motor- Voltage"; skip hyphen immediately after m/b (covers mm-, rpm-, bpm-)
    t = re.sub(r"(?<![bmBM])(?<=[a-z])(-\s+)(?=[A-Z(])", r"\n- ", t)

    # "chuck system- Built-in …" (m before hyphen would otherwise be skipped)
    t = re.sub(r"(system)(-\s+)(?=[A-Z])", r"\1\n- ", t, flags=re.I)

    # "22+1+1- Mechanical …"
    t = re.sub(r"(\+\d+)(-\s+)(?=[A-Z])", r"\1\n- ", t)

    # "20V- No-load", "66Nm- Metal", "115mm- Spindle", "2.5J- SDS", "2A- Rated" (space after hyphen)
    t = re.sub(r"((?:V|Nm|mm|rpm|bpm|Ah|Hz|J|W))(-\s+)(?=[A-Z(])", r"\1\n- ", t)
    t = re.sub(r"((?<=\d)A)(-\s+)(?=[A-Z(])", r"\1\n- ", t)

    # "Details:- Something" or "capacity:- Concrete"
    t = re.sub(r":\s*-\s*", ":\n- ", t)

    # Section title stuck after lowercase (lightMax drilling capacity:)
    t = re.sub(
        r"(?<=[a-z0-9)])(Max\s+drilling\s+capacity\s*:)",
        r"\n\n\1",
        t,
        flags=re.I,
    )

    # "…cutting Cutting capacity:" (run-on section)
    t = re.sub(
        r"(?<=[a-z])(\s*)(Cutting\s+capacity\s*:)",
        r"\n\n\2",
        t,
        flags=re.I,
    )

    # Spec lists use " - Field - Field" (spaces around hyphens). Split; skip ". -" (e.g. abbrev.)
    for _ in range(24):
        t2 = re.sub(r"(?<=[^.\n])\s-\s(?=[A-Z0-9(+-])", r"\n- ", t)
        if t2 == t:
            break
        t = t2

    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def format_tsawelding_product_description(text: str) -> str:
    """
    Normalize TSA Welding / Shopify RTE descriptions: strip app chrome, fix flat JSON-LD
    blobs (section headers and bullets run together).
    """
    if not text:
        return ""
    t = html.unescape(text).replace("\u00a0", " ")
    t = re.sub(r"(?:\n|^)\s*Powered by[\s\S]*$", "", t, flags=re.I)
    t = re.sub(r"(?:\n|^)\s*Smart Tabs[\s\S]*$", "", t, flags=re.I)
    t = t.strip()

    if t.count("\n") >= 5:
        t = re.sub(r"[ \t]+\n", "\n", t)
        t = re.sub(r"\n{3,}", "\n\n", t)
        return t.strip()

    one = re.sub(r"[ \t]+", " ", t).strip()
    one = re.sub(r"(?<=[a-z0-9.%])(\s)(Features\s*:)", r"\n\n\2", one, flags=re.I)
    one = re.sub(r"(?<=[a-z0-9.%])(\s)(Specifications\s*:)", r"\n\n\2", one, flags=re.I)
    one = re.sub(r"(?<=[a-z0-9.%])(\s)(What's\s+in\s+the\s+box\s*:)", r"\n\n\2", one, flags=re.I)
    one = re.sub(r"([:;!?])(\s+)(•\s+)", r"\1\n\3", one)
    one = re.sub(r"(?<=[a-zA-Z0-9%])\s+(•\s+)", r"\n\1", one)
    one = re.sub(r"(\.\s+)(-\s+)", r"\1\n\2", one)
    one = re.sub(r"(What's\s+in\s+the\s+box\s*:)\s*(-\s+)", r"\1\n\2", one, flags=re.I)
    one = re.sub(r"(?<=[a-z.])\s+(-\s+(?:earth|removable)\b)", r"\n\1", one, flags=re.I)
    one = re.sub(r"\n{3,}", "\n\n", one)
    return one.strip()


def clean_description(text: str) -> str:
    """Strip unwanted UI text and special chars from description."""
    if not text:
        return ""
    text = remove_special_chars(text)
    for pattern in DESCRIPTION_EXCLUDE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
