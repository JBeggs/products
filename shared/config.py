"""Configuration for product scrapers - company slugs, category IDs, pricing."""
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

# Thread-local scrape context: company_slug for tiered markup (set by run_supplier_scrape)
_scrape_ctx = threading.local()


def set_scrape_company_slug(slug: str | None) -> None:
    _scrape_ctx.company_slug = slug


def get_scrape_company_slug() -> str | None:
    return getattr(_scrape_ctx, "company_slug", None)

# Load .env from products/ root
PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PRODUCTS_ROOT / ".env")

SCRAPER_CONFIG_FILE = PRODUCTS_ROOT / "scraper_config.json"

# Suppliers that use apply_tiered_markup - must have tiers configured (no fallback)
SUPPLIERS_USING_TIERED_MARKUP = frozenset({
    "makro", "matrixwarehouse", "temu", "onedayonly", "myrunway", "ubuy",
    "perfectdealz", "takealot", "aliexpress", "game", "loot", "constructionhyper",
    "northernbolt", "builders", "dailydiscounts", "soundselect", "tsawelding", "gimmeonline",
    "nativechild", "blackafrican", "cosmeticconnection",
    "ahm", "outdoorandvelocity", "buythis", "vinylcutters", "shein", "electromann", "robotics", "communica",
})

# Default tier multipliers: (threshold_cents/100 = R, multiplier)
# Under R30: 350%, R30-R99: 300%, R100-R199: 225%, R200+: 150%
DEFAULT_TIER_MULTIPLIERS = [
    {"threshold": 30, "multiplier": 3.5},
    {"threshold": 99, "multiplier": 3.0},
    {"threshold": 199, "multiplier": 2.25},
    {"threshold": None, "multiplier": 1.5},  # None = infinity
]

DEFAULT_SUPPLIER_CATEGORIES: dict[str, str] = {
    "makro": "general",
    "temu": "import",
    "aliexpress": "import",
    "onedayonly": "import",
    "myrunway": "import",
    "ubuy": "import",
    "perfectdealz": "import",
    "takealot": "import",
    "loot": "import",
    "shein": "import",
    "electromann": "electronics",
    "robotics": "electronics",
    "communica": "electronics",
}


def normalize_supplier_category(value: str | None) -> str:
    """Normalize supplier category text to lowercase slug-ish format."""
    if value is None:
        return ""
    text = str(value).strip().lower().replace("_", "-")
    out = []
    prev_dash = False
    for ch in text:
        is_word = ("a" <= ch <= "z") or ("0" <= ch <= "9")
        if is_word:
            out.append(ch)
            prev_dash = False
            continue
        if ch in (" ", "-", "/") and not prev_dash and out:
            out.append("-")
            prev_dash = True
    normalized = "".join(out).strip("-")
    return normalized[:48]


def get_supplier_categories() -> dict[str, str]:
    """Return merged supplier category map (defaults + config overrides)."""
    cfg = load_scraper_config()
    raw = cfg.get("supplier_categories") or {}
    merged = dict(DEFAULT_SUPPLIER_CATEGORIES)
    if isinstance(raw, dict):
        for slug, category in raw.items():
            s = str(slug or "").strip().lower()
            if not s:
                continue
            c = normalize_supplier_category(category)
            if c:
                merged[s] = c
    return merged


def get_supplier_category(slug: str) -> str:
    """Get category for a supplier slug (empty string when unset)."""
    s = str(slug or "").strip().lower()
    if not s:
        return ""
    return get_supplier_categories().get(s, "")


def list_supplier_categories() -> list[str]:
    """Return distinct known supplier categories, sorted."""
    cats = set(DEFAULT_SUPPLIER_CATEGORIES.values())
    for _, category in get_supplier_categories().items():
        c = normalize_supplier_category(category)
        if c:
            cats.add(c)
    return sorted(cats)


def save_supplier_category(slug: str, category: str | None) -> None:
    """Save or clear supplier category override in scraper config."""
    s = str(slug or "").strip().lower()
    if not s:
        raise ValueError("supplier slug required")
    c = normalize_supplier_category(category)
    cfg = load_scraper_config()
    raw = cfg.get("supplier_categories") or {}
    category_map = dict(raw) if isinstance(raw, dict) else {}
    if not c:
        category_map.pop(s, None)
    else:
        category_map[s] = c
    cfg["supplier_categories"] = category_map
    save_scraper_config(cfg)


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


def get_tier_multipliers(supplier_slug: str | None = None, company_slug: str | None = None) -> list[tuple[float, float]]:
    """
    Return tier multipliers as [(threshold, mult), ...] for apply_tiered_markup.
    supplier_slug: required.
    company_slug: when provided, use company-scoped tiers. Else fall back to global supplier_tiers.
    Returns [] if no tiers configured.
    """
    if not supplier_slug:
        return []
    cfg = load_scraper_config()
    # Company-scoped first (like products)
    if company_slug:
        company_tiers = cfg.get("company_tiers") or {}
        by_company = company_tiers.get(company_slug.strip())
        if isinstance(by_company, dict):
            tiers = by_company.get(supplier_slug)
            if tiers and isinstance(tiers, list):
                return _parse_tiers_list(tiers)
    # Fallback: global supplier_tiers (backward compat, CLI)
    supplier_tiers = cfg.get("supplier_tiers") or {}
    tiers = supplier_tiers.get(supplier_slug)
    if not tiers or not isinstance(tiers, list):
        return []
    return _parse_tiers_list(tiers)


def _parse_tiers_list(tiers: list) -> list[tuple[float, float]]:
    result = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        th = t.get("threshold")
        mult = float(t.get("multiplier", 1.5))
        result.append((float("inf") if th is None else float(th), mult))
    return result


def save_supplier_tiers(slug: str, tiers: list[dict], company_slug: str | None = None) -> None:
    """Save tier multipliers for a supplier. When company_slug provided, save to company-scoped config."""
    cfg = load_scraper_config()
    if company_slug:
        company_tiers = cfg.get("company_tiers") or {}
        cs = company_slug.strip()
        if cs not in company_tiers:
            company_tiers[cs] = {}
        company_tiers[cs][slug] = tiers
        cfg["company_tiers"] = company_tiers
    else:
        supplier_tiers = cfg.get("supplier_tiers") or {}
        supplier_tiers[slug] = tiers
        cfg["supplier_tiers"] = supplier_tiers
    save_scraper_config(cfg)


def get_company_suppliers(company_slug: str) -> list[str]:
    """Scraper supplier slugs allowed for this company. Empty list = no restriction (all suppliers on Scrape page).

    ``company_suppliers`` in JSON may be a list of slugs or a legacy single string per company.
    """
    if not (company_slug or "").strip():
        return []
    cfg = load_scraper_config()
    m = cfg.get("company_suppliers") or {}
    if not isinstance(m, dict):
        return []
    raw = m.get(company_slug.strip())
    if raw is None:
        return []
    if isinstance(raw, str):
        from shared.suppliers import get_supplier
        info = get_supplier(raw.strip())
        return [info.slug] if info else ([raw.strip()] if raw.strip() else [])
    if isinstance(raw, list):
        from shared.suppliers import get_supplier
        out: list[str] = []
        seen: set[str] = set()
        for x in raw:
            if x is None:
                continue
            t = str(x).strip()
            if not t:
                continue
            info = get_supplier(t)
            canonical = info.slug if info else t
            low = canonical.lower()
            if low not in seen:
                seen.add(low)
                out.append(canonical)
        return out
    return []


def save_company_suppliers(company_slug: str, supplier_slugs: list[str] | None) -> None:
    """Replace allowed scraper suppliers for a company. Empty list or None clears (show all on Scrape page)."""
    cs = (company_slug or "").strip()
    if not cs:
        raise ValueError("company_slug required")
    cfg = load_scraper_config()
    m = cfg.get("company_suppliers") or {}
    if not isinstance(m, dict):
        m = {}
    if not supplier_slugs:
        m.pop(cs, None)
    else:
        seen: set[str] = set()
        out: list[str] = []
        for x in supplier_slugs:
            if x is None:
                continue
            t = str(x).strip()
            if not t:
                continue
            low = t.lower()
            if low not in seen:
                seen.add(low)
                out.append(t)
        if out:
            m[cs] = out
        else:
            m.pop(cs, None)
    cfg["company_suppliers"] = m
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


def _parse_credential_lists() -> tuple[list[str], list[str], list[str]]:
    """
    Parse COMPANY_SLUGS, API_USERNAMES, API_PASSWORDS as aligned comma-separated lists.
    Returns (slugs, usernames, passwords). Validates 1:1 mapping by index.
    Raises ValueError on length mismatch.
    """
    slugs_raw = os.environ.get("COMPANY_SLUGS", "").strip()
    usernames_raw = os.environ.get("API_USERNAMES", "").strip()
    passwords_raw = os.environ.get("API_PASSWORDS", "").strip()
    if not slugs_raw:
        return [], [], []
    slugs = [s.strip() for s in slugs_raw.split(",") if s.strip()]
    if not usernames_raw or not passwords_raw:
        return slugs, [], []
    usernames = [u.strip() for u in usernames_raw.split(",")]
    passwords = [p.strip() for p in passwords_raw.split(",")]
    if len(usernames) != len(slugs) or len(passwords) != len(slugs):
        raise ValueError(
            f"COMPANY_SLUGS, API_USERNAMES, API_PASSWORDS must have same count. "
            f"Got {len(slugs)} slugs, {len(usernames)} usernames, {len(passwords)} passwords"
        )
    return slugs, usernames, passwords


def get_credentials_for_company(company_slug: str) -> tuple[str | None, str | None]:
    """
    Return (username, password) for the given company_slug.
    Uses COMPANY_SLUGS/API_USERNAMES/API_PASSWORDS when set (1:1 by index).
    Falls back to API_USERNAME/API_PASSWORD when single company or COMPANY_SLUG.
    Returns (None, None) if not found.
    """
    slugs, usernames, passwords = _parse_credential_lists()
    if slugs and usernames and passwords:
        try:
            idx = slugs.index(company_slug.strip())
            return usernames[idx], passwords[idx]
        except ValueError:
            return None, None
    single_user = os.environ.get("API_USERNAME", "").strip()
    single_pass = os.environ.get("API_PASSWORD", "").strip()
    target = get_target_slugs()
    if target and company_slug in target and single_user and single_pass:
        return single_user, single_pass
    return single_user or None, single_pass or None


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
    """Supplier-wide delivery overrides from ``scraper_config.json``.

    Keys (do not confuse):
    - ``delivery_cost``: flat ZAR shipping fee → syncs to Django ``supplier_delivery_cost``
    - ``free_delivery_threshold``: basket subtotal to waive that fee → Django ``free_delivery_threshold``
    """
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
    """Persist delivery_* for one supplier slug.

    Keys present in ``data`` update or remove that field; omitted keys keep the
    previous value under ``supplier_delivery[slug]`` (avoids wiping unrelated
    fields when only one value is saved).

    The Flask UI POSTs ``delivery_time``, ``delivery_cost``, and
    ``free_delivery_threshold`` every time. Scrape merge passes a full ``dict(current)``
    plus edits so every intended field is present.
    """
    cfg = load_scraper_config()
    supplier_delivery = cfg.get("supplier_delivery") or {}
    merged = dict(supplier_delivery.get(slug) or {})

    if "delivery_time" in data:
        dt = data.get("delivery_time")
        if dt is not None and str(dt).strip():
            merged["delivery_time"] = str(dt).strip()
        else:
            merged.pop("delivery_time", None)

    if "delivery_cost" in data:
        dc = data.get("delivery_cost")
        if dc is not None and dc != "":
            try:
                merged["delivery_cost"] = float(dc)
            except (TypeError, ValueError):
                pass
        else:
            merged.pop("delivery_cost", None)

    if "free_delivery_threshold" in data:
        fd = data.get("free_delivery_threshold")
        if fd is not None and fd != "":
            try:
                merged["free_delivery_threshold"] = float(fd)
            except (TypeError, ValueError):
                pass
        else:
            merged.pop("free_delivery_threshold", None)

    supplier_delivery[slug] = merged
    cfg["supplier_delivery"] = supplier_delivery
    save_scraper_config(cfg)


def merge_supplier_delivery_from_scrape(slug: str, scraped: dict) -> None:
    """Merge scraped delivery info into config. Fresh scrape values overwrite."""
    current = get_supplier_delivery(slug)
    merged = dict(current)
    scrap_time = (scraped.get("delivery_time") or "").strip()
    if scrap_time:
        merged["delivery_time"] = scrap_time
    if scraped.get("delivery_cost") is not None:
        try:
            merged["delivery_cost"] = float(scraped["delivery_cost"])
        except (TypeError, ValueError):
            pass
    if scraped.get("free_delivery_threshold") is not None:
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
