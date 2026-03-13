#!/usr/bin/env python3
"""
Unified CLI for product scrapers.
Usage:
  python -m products scrape temu [--upload]
  python -m products scrape gumtree [--upload]
  python -m products scrape aliexpress [--upload]
  python -m products scrape ubuy [--upload]
  python -m products scrape myrunway [--upload]
  python -m products scrape onedayonly [--upload]
  python -m products list-suppliers
  python -m products list-categories
"""
import argparse
import sys
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified product scraper CLI. Scrape Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    from shared.suppliers import get_suppliers

    supplier_slugs = [s["slug"] for s in get_suppliers()]
    scrape_p = sub.add_parser("scrape", help="Scrape products from a supplier")
    scrape_p.add_argument(
        "supplier",
        choices=supplier_slugs,
        help="Supplier to scrape",
    )
    scrape_p.add_argument("--upload", action="store_true", help="Upload after scraping")
    scrape_p.add_argument(
        "--upload-to",
        default=None,
        help="Upload to specific slug or 'all' (overrides COMPANY_SLUG/COMPANY_SLUGS)",
    )
    scrape_p.add_argument("--urls", default=None, help="URLs file path")
    scrape_p.add_argument(
        "--output-dir",
        "-o",
        default=None,
        help="Output directory for scraped data",
    )
    scrape_p.add_argument("--debug", action="store_true", help="Print debug info")
    scrape_p.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (Temu may block headless)",
    )
    scrape_p.add_argument(
        "--list-categories",
        action="store_true",
        help="List categories for COMPANY_SLUG (requires API env vars)",
    )
    scrape_p.add_argument(
        "--save-session",
        action="store_true",
        help="Open browser, log in, then save session for future runs",
    )
    scrape_p.add_argument("--dry-run", action="store_true", help="Scrape only, no API upload")
    scrape_p.add_argument(
        "--variant-mode",
        "-v",
        action="store_true",
        help="[Temu only] Interactive variant capture mode",
    )

    list_p = sub.add_parser("list-suppliers", help="List available suppliers")
    list_cat_p = sub.add_parser("list-categories", help="List categories from API (requires API_BASE_URL, API_USERNAME, API_PASSWORD, COMPANY_SLUG)")

    args = parser.parse_args()

    if args.cmd == "list-categories":
        import os
        from shared.upload import get_auth_token
        from shared.config import get_target_slugs

        load_dotenv = getattr(__import__("dotenv", fromlist=["load_dotenv"]), "load_dotenv", None)
        if load_dotenv:
            load_dotenv(PRODUCTS_ROOT / ".env")

        base_url = os.environ.get("API_BASE_URL", "").strip()
        username = os.environ.get("API_USERNAME", "").strip()
        password = os.environ.get("API_PASSWORD", "").strip()
        slugs = get_target_slugs()
        if not slugs:
            slug = os.environ.get("COMPANY_SLUG", "").strip()
            slugs = [slug] if slug else []
        if not all([base_url, username, password]) or not slugs:
            print("Set API_BASE_URL, API_USERNAME, API_PASSWORD, COMPANY_SLUG or COMPANY_SLUGS in .env")
            return 1
        for company_slug in slugs:
            token = get_auth_token(base_url, username, password, company_slug=company_slug)
            if not token:
                print(f"Login failed for {company_slug}")
                continue
            try:
                import requests
                r = requests.get(f"{base_url.rstrip('/')}/v1/categories/", headers={"Authorization": f"Bearer {token}", "X-Company-Slug": company_slug}, timeout=15)
                r.raise_for_status()
                data = r.json()
                items = data.get("results") if isinstance(data.get("results"), list) else (data if isinstance(data, list) else data.get("data", []))
                if not items:
                    print(f"No categories for {company_slug}. Create one in the admin first.")
                else:
                    print(f"Categories for {company_slug}:")
                    for c in items:
                        print(f"  {c.get('id')}  {c.get('name', '')} (slug: {c.get('slug', '')})")
                    print()
                    print("Add to .env: CATEGORY_IDS=" + company_slug + ":" + str(items[0].get("id", "")))
            except Exception as e:
                print(f"Failed to list categories for {company_slug}: {e}")
        return 0

    if args.cmd == "list-suppliers":
        from shared.suppliers import get_suppliers

        for s in get_suppliers():
            mode = "browse & save" if s["supports_interactive"] else "URL list"
            print(f"  {s['slug']}: {s['display_name']} ({mode})")
        return 0

    if args.cmd == "scrape":
        from shared.suppliers import get_supplier

        info = get_supplier(args.supplier)
        if not info:
            print(f"Unknown supplier: {args.supplier}")
            return 1
        mod_name = info.module_name
        script_name = f"scrape_{args.supplier}.py"

        # Build argv for the supplier's main() - it uses argparse on sys.argv
        fake_argv = [script_name]
        if args.upload:
            fake_argv.append("--upload")
        if args.upload_to:
            fake_argv.extend(["--upload-to", args.upload_to])
        if args.urls:
            fake_argv.extend(["--urls", args.urls])
        if args.output_dir:
            fake_argv.extend(["--output-dir", args.output_dir])
        if args.debug:
            fake_argv.append("--debug")
        if args.headless:
            fake_argv.append("--headless")
        if args.list_categories:
            fake_argv.append("--list-categories")
        if args.save_session:
            fake_argv.append("--save-session")
        if args.dry_run:
            fake_argv.append("--dry-run")
        if args.variant_mode:
            fake_argv.append("--variant-mode")

        old_argv = sys.argv
        sys.argv = fake_argv
        try:
            import importlib

            mod = importlib.import_module(mod_name)
            return mod.main()
        finally:
            sys.argv = old_argv

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
