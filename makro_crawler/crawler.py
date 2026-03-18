"""
Makro crawler: headless run orchestration.
Fetches search pages, parses cards, fetches detail pages, applies ignore rules, stores in SQLite.
Supports optional session cookies for human-verification protected URLs.
"""
import logging
import re
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

from .config import SEARCHES
from .db import (
    finish_search_job,
    get_active_ignore_rules,
    init_schema,
    insert_search_job,
    listing_matches_ignore,
    upsert_listing,
)
from .parsers import parse_detail_page, parse_search_cards

LOG = logging.getLogger("makro_crawler")

try:
    from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, PAGE_LOAD_TIMEOUT
except ImportError:
    CHROMIUM_PERFORMANCE_ARGS = ["--disable-blink-features=AutomationControlled"]
    PAGE_LOAD_TIMEOUT = 60000

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _load_cookies_from_file(cookie_path: Path) -> list[dict] | None:
    """Load cookies from JSON file (Playwright format). Returns None if file missing/invalid."""
    if not cookie_path or not cookie_path.exists() or cookie_path.stat().st_size == 0:
        return None
    try:
        import json
        data = json.loads(cookie_path.read_text())
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and "cookies" in data:
            return data["cookies"]
        return None
    except Exception as e:
        LOG.warning("Could not load cookies from %s: %s", cookie_path, e)
        return None


def run_crawl(
    stop_flag=None,
    progress_cb: Callable[[str], None] | None = None,
    cookie_path: str | Path | None = None,
) -> dict:
    """
    Run full crawl: all search URLs, parse cards, fetch details, apply ignore rules, store.
    If a search has requires_session=True and cookie_path is set, loads cookies before visiting.
    Returns {job_id, listings_found, listings_new, listings_updated, error}.
    """
    init_schema()
    job_id = insert_search_job()
    listings_found = 0
    listings_new = 0
    listings_updated = 0
    error_msg = None

    def log_progress(msg: str) -> None:
        LOG.info(msg)
        if progress_cb:
            progress_cb(msg)

    try:
        ignore_rules = get_active_ignore_rules()
        log_progress(f"Crawl job {job_id}: {len(ignore_rules)} ignore rules active")

        cookie_file = Path(cookie_path) if cookie_path else None
        cookies = _load_cookies_from_file(cookie_file) if cookie_file else None

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=CHROMIUM_PERFORMANCE_ARGS + ["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="en-ZA",
                viewport={"width": 1280, "height": 720},
            )
            if cookies:
                context.add_cookies(cookies)
                log_progress("Loaded session cookies")

            page = context.new_page()
            page.set_default_timeout(PAGE_LOAD_TIMEOUT)

            try:
                for search in SEARCHES:
                    if stop_flag and stop_flag.is_set():
                        log_progress("Crawl stopped by flag")
                        break
                    if search.requires_session and not cookies:
                        log_progress(f"Skipping {search.name}: requires session cookies (set MAKRO_COOKIE_PATH)")
                        continue
                    log_progress(f"Searching: {search.name} ({search.url[:60]}...)")
                    try:
                        page.goto(search.url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                    except Exception as e:
                        if search.requires_session:
                            error_msg = f"{search.name}: human verification or session expired. Set MAKRO_COOKIE_PATH with valid cookies."
                            log_progress(error_msg)
                        else:
                            log_progress(f"  Error loading {search.name}: {e}")
                        continue
                    time.sleep(2)
                    html = page.content()
                    if "Are you a human" in html or "human verification" in html.lower():
                        if search.requires_session:
                            error_msg = f"{search.name}: human verification challenge. Provide valid session cookies via MAKRO_COOKIE_PATH."
                            log_progress(error_msg)
                        continue
                    cards = parse_search_cards(html, search.url, search.category)
                    log_progress(f"  Found {len(cards)} cards on first page")

                    # Pagination: Makro uses page=2, page=3 etc.
                    for page_num in range(2, 4):
                        if stop_flag and stop_flag.is_set():
                            break
                        next_url = re.sub(r"([?&])page=\d+", rf"\g<1>page={page_num}", search.url)
                        if "page=" not in next_url:
                            next_url = search.url + ("&" if "?" in search.url else "?") + f"page={page_num}"
                        try:
                            page.goto(next_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                            time.sleep(1)
                            html = page.content()
                            more = parse_search_cards(html, next_url, search.category)
                            seen = {c["ad_id"] for c in cards}
                            for c in more:
                                if c["ad_id"] not in seen:
                                    cards.append(c)
                                    seen.add(c["ad_id"])
                        except Exception as e:
                            LOG.debug("Pagination %s: %s", page_num, e)
                            break

                    for card in cards:
                        if stop_flag and stop_flag.is_set():
                            break
                        if listing_matches_ignore(card, ignore_rules):
                            continue
                        try:
                            page.goto(card["url"], wait_until="domcontentloaded", timeout=15000)
                            time.sleep(1)
                            detail_html = page.content()
                            detail = parse_detail_page(detail_html, card["url"], search.category)
                            if detail:
                                merged = {**card, **detail}
                                # Prefer first non-empty title; avoid overwriting with None
                                title = merged.get("title") or card.get("title")
                                lid, is_new = upsert_listing(
                                    ad_id=merged["ad_id"],
                                    url=merged["url"],
                                    title=title,
                                    price=merged.get("price"),
                                    category=merged.get("category"),
                                    location=merged.get("location"),
                                    seller=merged.get("seller"),
                                    condition=merged.get("condition"),
                                    description=merged.get("description"),
                                    search_job_id=job_id,
                                )
                                listings_found += 1
                                if is_new:
                                    listings_new += 1
                                else:
                                    listings_updated += 1
                            else:
                                if not listing_matches_ignore(card, ignore_rules):
                                    # Card-only: use card title (may be None)
                                    lid, is_new = upsert_listing(
                                        ad_id=card["ad_id"],
                                        url=card["url"],
                                        title=card.get("title"),
                                        price=card.get("price"),
                                        category=card.get("category"),
                                        location=None,
                                        seller=None,
                                        condition=None,
                                        description=None,
                                        search_job_id=job_id,
                                    )
                                    listings_found += 1
                                    if is_new:
                                        listings_new += 1
                                    else:
                                        listings_updated += 1
                        except Exception as e:
                            LOG.debug("Detail fetch %s: %s", card.get("url", "")[:50], e)
                        time.sleep(0.5)

            finally:
                context.close()
                browser.close()

        finish_search_job(
            job_id,
            status="completed",
            listings_found=listings_found,
            listings_new=listings_new,
            listings_updated=listings_updated,
        )
        log_progress(f"Crawl completed: {listings_found} found, {listings_new} new, {listings_updated} updated")
    except Exception as e:
        LOG.exception("Crawl error: %s", e)
        error_msg = str(e)
        finish_search_job(job_id, status="failed", error=error_msg)

    return {
        "job_id": job_id,
        "listings_found": listings_found,
        "listings_new": listings_new,
        "listings_updated": listings_updated,
        "error": error_msg,
    }
