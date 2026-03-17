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
    "onedayonly": SupplierInfo(
        slug="onedayonly",
        display_name="OneDayOnly",
        module_name="onedayonly.scrape_onedayonly",
        output_dir=_path("onedayonly/scraped"),
        urls_file=_path("onedayonly/urls.txt"),
        supports_interactive=True,
    ),
}


def get_suppliers() -> list[dict]:
    """Return list of suppliers for API/UI."""
    return [
        {
            "slug": s.slug,
            "display_name": s.display_name,
            "supports_interactive": s.supports_interactive,
        }
        for s in SUPPLIERS.values()
    ]


def get_supplier(slug: str) -> SupplierInfo | None:
    """Get supplier by slug."""
    return SUPPLIERS.get(slug)


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
