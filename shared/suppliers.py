"""
Supplier registry for product scrapers.
Add new suppliers here to make them available in the UI and CLI.
"""
import logging
from dataclasses import dataclass
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("products.scraper")


@dataclass
class SupplierInfo:
    """Descriptor for a product supplier/scraper."""

    slug: str
    display_name: str
    module_name: str  # e.g. "temu.scrape_temu"
    output_dir: Path
    urls_file: Path
    supports_interactive: bool  # browse-and-save mode via run_scrape_session

    @property
    def scraped_path(self) -> Path:
        return self.output_dir / "products.json"


def _path(rel: str) -> Path:
    return PRODUCTS_ROOT / rel


SUPPLIERS: dict[str, SupplierInfo] = {
    "temu": SupplierInfo(
        slug="temu",
        display_name="Temu",
        module_name="temu.scrape_temu",
        output_dir=_path("temu/scraped"),
        urls_file=_path("temu/urls.txt"),
        supports_interactive=True,
    ),
    "gumtree": SupplierInfo(
        slug="gumtree",
        display_name="Gumtree",
        module_name="gumtree.scrape_gumtree",
        output_dir=_path("gumtree/scraped"),
        urls_file=_path("gumtree/urls.txt"),
        supports_interactive=True,
    ),
    "junkmail": SupplierInfo(
        slug="junkmail",
        display_name="Junk Mail",
        module_name="junkmail.scrape_junkmail",
        output_dir=_path("junkmail/scraped"),
        urls_file=_path("junkmail/urls.txt"),
        supports_interactive=True,
    ),
    "ahm": SupplierInfo(
        slug="ahm",
        display_name="AHM Online",
        module_name="ahm.scrape_ahm",
        output_dir=_path("ahm/scraped"),
        urls_file=_path("ahm/urls.txt"),
        supports_interactive=True,
    ),
    "aliexpress": SupplierInfo(
        slug="aliexpress",
        display_name="AliExpress",
        module_name="aliexpress.scrape_aliexpress",
        output_dir=_path("aliexpress/scraped"),
        urls_file=_path("aliexpress/urls.txt"),
        supports_interactive=True,
    ),
    "makro": SupplierInfo(
        slug="makro",
        display_name="Makro",
        module_name="makro.scrape_makro",
        output_dir=_path("makro/scraped"),
        urls_file=_path("makro/urls.txt"),
        supports_interactive=True,
    ),
    "constructionhyper": SupplierInfo(
        slug="constructionhyper",
        display_name="Construction Hyper",
        module_name="constructionhyper.scrape_constructionhyper",
        output_dir=_path("constructionhyper/scraped"),
        urls_file=_path("constructionhyper/urls.txt"),
        supports_interactive=True,
    ),
    "game": SupplierInfo(
        slug="game",
        display_name="Game",
        module_name="game.scrape_game",
        output_dir=_path("game/scraped"),
        urls_file=_path("game/urls.txt"),
        supports_interactive=True,
    ),
    "matrixwarehouse": SupplierInfo(
        slug="matrixwarehouse",
        display_name="Matrix Warehouse",
        module_name="matrixwarehouse.scrape_matrixwarehouse",
        output_dir=_path("matrixwarehouse/scraped"),
        urls_file=_path("matrixwarehouse/urls.txt"),
        supports_interactive=True,
    ),
    "takealot": SupplierInfo(
        slug="takealot",
        display_name="Takealot",
        module_name="takealot.scrape_takealot",
        output_dir=_path("takealot/scraped"),
        urls_file=_path("takealot/urls.txt"),
        supports_interactive=True,
    ),
    "loot": SupplierInfo(
        slug="loot",
        display_name="Loot",
        module_name="loot.scrape_loot",
        output_dir=_path("loot/scraped"),
        urls_file=_path("loot/urls.txt"),
        supports_interactive=True,
    ),
    "perfectdealz": SupplierInfo(
        slug="perfectdealz",
        display_name="Perfect Dealz",
        module_name="perfectdealz.scrape_perfectdealz",
        output_dir=_path("perfectdealz/scraped"),
        urls_file=_path("perfectdealz/urls.txt"),
        supports_interactive=True,
    ),
    "ubuy": SupplierInfo(
        slug="ubuy",
        display_name="Ubuy",
        module_name="ubuy.scrape_ubuy",
        output_dir=_path("ubuy/scraped"),
        urls_file=_path("ubuy/urls.txt"),
        supports_interactive=True,
    ),
    "myrunway": SupplierInfo(
        slug="myrunway",
        display_name="MyRunway",
        module_name="myrunway.scrape_myrunway",
        output_dir=_path("myrunway/scraped"),
        urls_file=_path("myrunway/urls.txt"),
        supports_interactive=True,
    ),
    "blackafrican": SupplierInfo(
        slug="blackafrican",
        display_name="Black African",
        module_name="blackafrican.scrape_blackafrican",
        output_dir=_path("blackafrican/scraped"),
        urls_file=_path("blackafrican/urls.txt"),
        supports_interactive=True,
    ),
    "cosmeticconnection": SupplierInfo(
        slug="cosmeticconnection",
        display_name="Cosmetic Connection",
        module_name="cosmeticconnection.scrape_cosmeticconnection",
        output_dir=_path("cosmeticconnection/scraped"),
        urls_file=_path("cosmeticconnection/urls.txt"),
        supports_interactive=True,
    ),
    "nativechild": SupplierInfo(
        slug="nativechild",
        display_name="Nativechild",
        module_name="nativechild.scrape_nativechild",
        output_dir=_path("nativechild/scraped"),
        urls_file=_path("nativechild/urls.txt"),
        supports_interactive=True,
    ),
    "onedayonly": SupplierInfo(
        slug="onedayonly",
        display_name="OneDayOnly",
        module_name="onedayonly.scrape_onedayonly",
        output_dir=_path("onedayonly/scraped"),
        urls_file=_path("onedayonly/urls.txt"),
        supports_interactive=True,
    ),
    "northernbolt": SupplierInfo(
        slug="northernbolt",
        display_name="Northern Bolt",
        module_name="northernbolt.scrape_northernbolt",
        output_dir=_path("northernbolt/scraped"),
        urls_file=_path("northernbolt/urls.txt"),
        supports_interactive=True,
    ),
    "builders": SupplierInfo(
        slug="builders",
        display_name="Builders",
        module_name="builders.scrape_builders",
        output_dir=_path("builders/scraped"),
        urls_file=_path("builders/urls.txt"),
        supports_interactive=True,
    ),
    "dailydiscounts": SupplierInfo(
        slug="dailydiscounts",
        display_name="Daily Discounts",
        module_name="dailydiscounts.scrape_dailydiscounts",
        output_dir=_path("dailydiscounts/scraped"),
        urls_file=_path("dailydiscounts/urls.txt"),
        supports_interactive=True,
    ),
    "buythis": SupplierInfo(
        slug="buythis",
        display_name="BuyThis",
        module_name="buythis.scrape_buythis",
        output_dir=_path("buythis/scraped"),
        urls_file=_path("buythis/urls.txt"),
        supports_interactive=True,
    ),
    "vinylcutters": SupplierInfo(
        slug="vinylcutters",
        display_name="Vinyl Cutters",
        module_name="vinylcutters.scrape_vinylcutters",
        output_dir=_path("vinylcutters/scraped"),
        urls_file=_path("vinylcutters/urls.txt"),
        supports_interactive=True,
    ),
    "soundselect": SupplierInfo(
        slug="soundselect",
        display_name="Sound Select",
        module_name="soundselect.scrape_soundselect",
        output_dir=_path("soundselect/scraped"),
        urls_file=_path("soundselect/urls.txt"),
        supports_interactive=True,
    ),
    "tsawelding": SupplierInfo(
        slug="tsawelding",
        display_name="TSA Welding",
        module_name="tsawelding.scrape_tsawelding",
        output_dir=_path("tsawelding/scraped"),
        urls_file=_path("tsawelding/urls.txt"),
        supports_interactive=True,
    ),
    "gimmeonline": SupplierInfo(
        slug="gimmeonline",
        display_name="Gimme Online",
        module_name="gimmeonline.scrape_gimmeonline",
        output_dir=_path("gimmeonline/scraped"),
        urls_file=_path("gimmeonline/urls.txt"),
        supports_interactive=True,
    ),
    "outdoorandvelocity": SupplierInfo(
        slug="outdoorandvelocity",
        display_name="Outdoor and Velocity",
        module_name="outdoorandvelocity.scrape_outdoorandvelocity",
        output_dir=_path("outdoorandvelocity/scraped"),
        urls_file=_path("outdoorandvelocity/urls.txt"),
        supports_interactive=True,
    ),
    "overberghoney": SupplierInfo(
        slug="overberghoney",
        display_name="Overberg Honey Co",
        module_name="overberghoney.scrape_overberghoney",
        output_dir=_path("overberghoney/scraped"),
        urls_file=_path("overberghoney/urls.txt"),
        supports_interactive=True,
    ),
    "hekpoorthoneyfarms": SupplierInfo(
        slug="hekpoorthoneyfarms",
        display_name="Hekpoort Honey Farms",
        module_name="hekpoorthoneyfarms.scrape_hekpoorthoneyfarms",
        output_dir=_path("hekpoorthoneyfarms/scraped"),
        urls_file=_path("hekpoorthoneyfarms/urls.txt"),
        supports_interactive=True,
    ),
    "agrimark": SupplierInfo(
        slug="agrimark",
        display_name="Agrimark",
        module_name="agrimark.scrape_agrimark",
        output_dir=_path("agrimark/scraped"),
        urls_file=_path("agrimark/urls.txt"),
        supports_interactive=True,
    ),
    "bulkseed": SupplierInfo(
        slug="bulkseed",
        display_name="BulkSeed.co.za",
        module_name="bulkseed.scrape_bulkseed",
        output_dir=_path("bulkseed/scraped"),
        urls_file=_path("bulkseed/urls.txt"),
        supports_interactive=True,
    ),
    "seedsandall": SupplierInfo(
        slug="seedsandall",
        display_name="Seeds and All",
        module_name="seedsandall.scrape_seedsandall",
        output_dir=_path("seedsandall/scraped"),
        urls_file=_path("seedsandall/urls.txt"),
        supports_interactive=True,
    ),
    "seedsforafrica": SupplierInfo(
        slug="seedsforafrica",
        display_name="Seeds for Africa",
        module_name="seedsforafrica.scrape_seedsforafrica",
        output_dir=_path("seedsforafrica/scraped"),
        urls_file=_path("seedsforafrica/urls.txt"),
        supports_interactive=True,
    ),
    "britelighting": SupplierInfo(
        slug="britelighting",
        display_name="Britelighting",
        module_name="britelighting.scrape_britelighting",
        output_dir=_path("britelighting/scraped"),
        urls_file=_path("britelighting/urls.txt"),
        supports_interactive=True,
    ),
    "youngsindustrial": SupplierInfo(
        slug="youngsindustrial",
        display_name="Young's Industrial",
        module_name="youngsindustrial.scrape_youngsindustrial",
        output_dir=_path("youngsindustrial/scraped"),
        urls_file=_path("youngsindustrial/urls.txt"),
        supports_interactive=True,
    ),
    "brendas": SupplierInfo(
        slug="brendas",
        display_name="Brenda's Preserves",
        module_name="brendas.scrape_brendas",
        output_dir=_path("brendas/scraped"),
        urls_file=_path("brendas/urls.txt"),
        supports_interactive=True,
    ),
    "oldcapefarmstall": SupplierInfo(
        slug="oldcapefarmstall",
        display_name="Old Cape Farm Stall",
        module_name="oldcapefarmstall.scrape_oldcapefarmstall",
        output_dir=_path("oldcapefarmstall/scraped"),
        urls_file=_path("oldcapefarmstall/urls.txt"),
        supports_interactive=True,
    ),
    "elanas": SupplierInfo(
        slug="elanas",
        display_name="Elana's",
        module_name="elanas.scrape_elanas",
        output_dir=_path("elanas/scraped"),
        urls_file=_path("elanas/urls.txt"),
        supports_interactive=True,
    ),
    "electromann": SupplierInfo(
        slug="electromann",
        display_name="Electromann SA",
        module_name="electromann.scrape_electromann",
        output_dir=_path("electromann/scraped"),
        urls_file=_path("electromann/urls.txt"),
        supports_interactive=True,
    ),
    "robotics": SupplierInfo(
        slug="robotics",
        display_name="Micro Robotics",
        module_name="robotics.scrape_robotics",
        output_dir=_path("robotics/scraped"),
        urls_file=_path("robotics/urls.txt"),
        supports_interactive=True,
    ),
    "communica": SupplierInfo(
        slug="communica",
        display_name="Communica South Africa",
        module_name="communica.scrape_communica",
        output_dir=_path("communica/scraped"),
        urls_file=_path("communica/urls.txt"),
        supports_interactive=True,
    ),
    "shein": SupplierInfo(
        slug="shein",
        display_name="SHEIN",
        module_name="shein.scrape_shein",
        output_dir=_path("shein/scraped"),
        urls_file=_path("shein/urls.txt"),
        supports_interactive=True,
    ),
    "manual": SupplierInfo(
        slug="manual",
        display_name="Manual Entry",
        module_name="",  # no scraper; add products via /manual
        output_dir=_path("manual/scraped"),
        urls_file=_path("manual/urls.txt"),
        supports_interactive=False,
    ),
}


def get_suppliers() -> list[dict]:
    """Return list of suppliers for API/UI (sorted by display name)."""
    rows = [
        {
            "slug": s.slug,
            "display_name": s.display_name,
            "supports_interactive": s.supports_interactive,
            "module_name": s.module_name,
        }
        for s in SUPPLIERS.values()
    ]
    rows.sort(key=lambda r: (r["display_name"] or r["slug"]).lower())
    return rows


# Legacy supplier/domain names → canonical slug
SUPPLIER_SLUG_ALIASES: dict[str, str] = {
    "direct-to-film": "buythis",
    "direct_to_film": "buythis",
    "directtofilm": "buythis",
}


def normalize_supplier_slug(slug: str) -> str:
    """Return canonical supplier slug, or empty string if input is blank."""
    s = (slug or "").strip().lower()
    if not s:
        return ""
    return SUPPLIER_SLUG_ALIASES.get(s, s)


def get_supplier(slug: str) -> SupplierInfo | None:
    """Get supplier by slug (accepts aliases such as direct-to-film)."""
    canonical = normalize_supplier_slug(slug)
    if not canonical:
        return None
    return SUPPLIERS.get(canonical)


def get_sources_for_edit() -> dict[str, Path]:
    """Return {slug: scraped_dir} for edit_products SOURCES."""
    return {s.slug: s.output_dir for s in SUPPLIERS.values()}


def get_company_scoped_dir(output_dir: Path, company_slug: str) -> Path:
    """
    Return company-scoped directory for products: output_dir/companies/{company_slug}/.
    Used for products.json, index.json, images/.
    """
    if not (company_slug or "").strip():
        raise ValueError("company_slug required for company-scoped paths")
    return output_dir / "companies" / company_slug.strip()


def run_supplier_scrape(
    slug: str,
    output_dir: Path,
    stop_flag,
    save_session_flag=None,
    scrape_options: dict | None = None,
) -> None:
    """
    Run the scraper for the given supplier.
    For interactive suppliers (Temu, Gumtree): uses run_scrape_session.
    For URL-only (AliExpress): opens browser and iterates urls.txt.
    scrape_options: optional dict with proxy_enabled, proxy_country, proxy_server.
    """
    import sys

    if str(PRODUCTS_ROOT) not in sys.path:
        sys.path.insert(0, str(PRODUCTS_ROOT))

    info = get_supplier(slug)
    if not info:
        raise ValueError(f"Unknown supplier: {slug}")
    if not (info.module_name or "").strip():
        raise ValueError(f"Supplier {slug!r} has no scraper module; use /manual to add products.")

    opts = scrape_options or {}
    from shared.config import set_scrape_company_slug
    set_scrape_company_slug(opts.get("company_slug"))

    LOG.debug("Loading module: %s", info.module_name)
    import importlib

    try:
        mod = importlib.import_module(info.module_name)
    except Exception as e:
        LOG.exception("Failed to import %s: %s", info.module_name, e)
        raise

    if info.supports_interactive and hasattr(mod, "run_scrape_session"):
        LOG.info("Running interactive scrape for %s", slug)
        mod.run_scrape_session(output_dir, stop_flag, save_session_flag or __noop_event(), opts)
    else:
        LOG.info("Running URL-based scrape for %s", slug)
        _run_url_based_scrape(mod, info, output_dir, stop_flag, opts)


def __noop_event():
    """Return a threading.Event that is never set (for save_session when not used)."""
    import threading
    return threading.Event()


def _run_url_based_scrape(
    mod, info: SupplierInfo, output_dir: Path, stop_flag, scrape_options: dict | None = None
) -> None:
    """Run URL-based scrape (e.g. AliExpress) - open browser, iterate urls.txt."""
    from playwright.sync_api import sync_playwright
    from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS
    import time

    opts = scrape_options or {}
    proxy = {"server": opts["proxy_server"]} if opts.get("proxy_server") else None

    LOG.debug("Loading URLs from %s", info.urls_file)
    urls = []
    if info.urls_file.exists():
        for line in info.urls_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ("http" in line or "www." in line):
                urls.append(line)

    if not urls:
        LOG.warning("No URLs in %s. Add product URLs, one per line.", info.urls_file)
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    LOG.info("Opening browser for %d URL(s)", len(urls))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=CHROMIUM_PERFORMANCE_ARGS)
        context_opts = {
            "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "locale": "en-ZA",
        }
        if proxy:
            context_opts["proxy"] = proxy
        context = browser.new_context(**context_opts)
        page = context.new_page()
        try:
            for i, url in enumerate(urls):
                if stop_flag.is_set():
                    break
                print(f"  [{i + 1}/{len(urls)}] {url[:70]}...")
                mod.scrape_url(page, url, output_dir, debug=opts.get("debug", False))
                if i < len(urls) - 1:
                    time.sleep(3)
        finally:
            browser.close()

    if hasattr(mod, "build_scraped_index"):
        mod.build_scraped_index(output_dir)
