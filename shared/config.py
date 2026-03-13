"""Configuration for product scrapers - company slugs, category IDs, pricing."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from products/ root
PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PRODUCTS_ROOT / ".env")

SCRAPER_CONFIG_FILE = PRODUCTS_ROOT / "scraper_config.json"

# Suppliers that use apply_tiered_markup - must have tiers configured (no fallback)
SUPPLIERS_USING_TIERED_MARKUP = frozenset({
    "makro", "matrixwarehouse", "temu", "onedayonly", "myrunway", "ubuy",
    "perfectdealz", "takealot", "aliexpress", "game", "loot", "constructionhyper",
})

# Default tier multipliers: (threshold_cents/100 = R, multiplier)
# Under R30: 350%, R30-R99: 300%, R100-R199: 225%, R200+: 150%
DEFAULT_TIER_MULTIPLIERS = [
    {"threshold": 30, "multiplier": 3.5},
    {"threshold": 99, "multiplier": 3.0},
    {"threshold": 199, "multiplier": 2.25},
    {"threshold": None, "multiplier": 1.5},  # None = infinity
]


def load_scraper_config() -> dict:
    """Load scraper config from JSON file. Returns defaults if missing/invalid."""
    if not SCRAPER_CONFIG_FILE.exists():
        return {"tier_multipliers": DEFAULT_TIER_MULTIPLIERS.copy()}
    try:
        data = json.loads(SCRAPER_CONFIG_FILE.read_text())
        tiers = data.get("tier_multipliers")
        if tiers and isinstance(tiers, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"tier_multipliers": DEFAULT_TIER_MULTIPLIERS.copy()}


def save_scraper_config(config: dict) -> None:
    """Save scraper config to JSON file."""
    SCRAPER_CONFIG_FILE.write_text(json.dumps(config, indent=2))


def get_tier_multipliers(supplier_slug: str | None = None) -> list[tuple[float, float]]:
    """
    Return tier multipliers as [(threshold, mult), ...] for apply_tiered_markup.
    supplier_slug: required; returns only that supplier's tiers. No fallback.
    Returns [] if supplier has no tiers configured.
    """
    if not supplier_slug:
        return []
    cfg = load_scraper_config()
    supplier_tiers = cfg.get("supplier_tiers") or {}
    tiers = supplier_tiers.get(supplier_slug)
    if not tiers or not isinstance(tiers, list):
        return []
    result = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        th = t.get("threshold")
        mult = float(t.get("multiplier", 1.5))
        result.append((float("inf") if th is None else float(th), mult))
    return result


def save_supplier_tiers(slug: str, tiers: list[dict]) -> None:
    """Save tier multipliers for a supplier. Merges into existing config."""
    cfg = load_scraper_config()
    supplier_tiers = cfg.get("supplier_tiers") or {}
    supplier_tiers[slug] = tiers
    cfg["supplier_tiers"] = supplier_tiers
    save_scraper_config(cfg)


def get_target_slugs(upload_to: str | None = None) -> list[str]:
    """
    Return list of company slugs to upload to.
    upload_to: override from CLI --upload-to (slug or 'all')
    """
    slugs = os.environ.get("COMPANY_SLUGS", "").strip()
    if slugs:
        return [s.strip() for s in slugs.split(",") if s.strip()]
    slug = os.environ.get("COMPANY_SLUG", "").strip()
    if slug:
        return [slug]
    return []


def get_category_ids() -> dict[str, str]:
    """Parse CATEGORY_IDS into {slug: uuid} map."""
    raw = os.environ.get("CATEGORY_IDS", "").strip()
    if not raw:
        return {}
    result = {}
    for part in raw.split(","):
        part = part.strip()
        if ":" in part:
            slug, uuid = part.split(":", 1)
            result[slug.strip()] = uuid.strip()
    return result


def get_category_for_slug(slug: str) -> str | None:
    """Get category UUID for a company slug."""
    return get_category_ids().get(slug)


def get_supplier_delivery(slug: str) -> dict:
    """Get delivery config for a supplier. Returns {delivery_time, delivery_cost, free_delivery_threshold}."""
    cfg = load_scraper_config()
    supplier_delivery = cfg.get("supplier_delivery") or {}
    return supplier_delivery.get(slug) or {}


def add_delivery_to_price(sell_price_zar: float, supplier_slug: str) -> float:
    """Add delivery cost to sell price if configured. Returns price in ZAR."""
    d = get_supplier_delivery(supplier_slug)
    dc = d.get("delivery_cost")
    if dc is not None and dc > 0:
        return round(sell_price_zar + dc, 2)
    return sell_price_zar


def save_supplier_delivery(slug: str, data: dict) -> None:
    """Save delivery config for a supplier. Merges into existing config."""
    cfg = load_scraper_config()
    supplier_delivery = cfg.get("supplier_delivery") or {}
    out = {}
    dt = (data.get("delivery_time") or "").strip()
    if dt:
        out["delivery_time"] = dt
    dc = data.get("delivery_cost")
    if dc is not None and dc != "":
        try:
            out["delivery_cost"] = float(dc)
        except (TypeError, ValueError):
            pass
    fd = data.get("free_delivery_threshold")
    if fd is not None and fd != "":
        try:
            out["free_delivery_threshold"] = float(fd)
        except (TypeError, ValueError):
            pass
    supplier_delivery[slug] = out
    cfg["supplier_delivery"] = supplier_delivery
    save_scraper_config(cfg)


def merge_supplier_delivery_from_scrape(slug: str, scraped: dict) -> None:
    """Merge scraped delivery info into config. Only fills in empty fields."""
    current = get_supplier_delivery(slug)
    merged = dict(current)
    if (scraped.get("delivery_time") or "").strip() and not (current.get("delivery_time") or "").strip():
        merged["delivery_time"] = (scraped["delivery_time"] or "").strip()
    if scraped.get("delivery_cost") is not None and current.get("delivery_cost") is None:
        try:
            merged["delivery_cost"] = float(scraped["delivery_cost"])
        except (TypeError, ValueError):
            pass
    if scraped.get("free_delivery_threshold") is not None and current.get("free_delivery_threshold") is None:
        try:
            merged["free_delivery_threshold"] = float(scraped["free_delivery_threshold"])
        except (TypeError, ValueError):
            pass
    if merged != current:
        save_supplier_delivery(slug, merged)


def resolve_upload_targets(upload_to: str | None) -> list[str]:
    """
    Resolve which slugs to upload to.
    upload_to: from --upload-to (None, 'all', or specific slug)
    """
    configured = get_target_slugs()
    if not configured:
        return []
    if upload_to is None:
        return configured
    if upload_to.lower() == "all":
        return configured
    # Specific slug - must be in configured
    if upload_to in configured:
        return [upload_to]
    # Allow upload even if not in configured (user override)
    return [upload_to]
