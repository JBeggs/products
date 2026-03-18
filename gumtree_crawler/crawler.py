"""
Gumtree crawler: headless run orchestration.
Fetches search pages, parses cards, fetches detail pages, applies ignore rules, stores in SQLite.
Pagination: crawls all pages until no new cards or time limit reached.
"""
import logging
import re
import time
from typing import Callable

from playwright.sync_api import sync_playwright

from .config import SEARCHES

# Crawl limits: 45 min total, max 50 pages per search (safety cap)
CRAWL_TIME_LIMIT_SECONDS = 45 * 60
MAX_PAGES_PER_SEARCH = 50
from .db import (
    finish_search_job,
    get_active_ignore_rules,
    init_schema,
    insert_search_job,
    listing_matches_ignore,
    upsert_listing,
)
from .parsers import parse_detail_page, parse_search_cards

LOG = logging.getLogger("gumtree_crawler")

# Reuse shared Playwright settings
try:
    from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, PAGE_LOAD_TIMEOUT
except ImportError:
    CHROMIUM_PERFORMANCE_ARGS = ["--disable-blink-features=AutomationControlled"]
    PAGE_LOAD_TIMEOUT = 60000

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def run_crawl(stop_flag=None, progress_cb: Callable[[str], None] | None = None) -> dict:
    """
    Run full crawl: all search URLs, parse cards, fetch details, apply ignore rules, store.
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
        log_progress(f"Crawl job {job_id}: {len(ignore_rules)} ignore rules active (time limit: {CRAWL_TIME_LIMIT_SECONDS // 60} min)")

        crawl_start = time.monotonic()

        def elapsed() -> float:
            return time.monotonic() - crawl_start

        def time_remaining() -> bool:
            return elapsed() < CRAWL_TIME_LIMIT_SECONDS

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
            page = context.new_page()
            page.set_default_timeout(PAGE_LOAD_TIMEOUT)

            try:
                for search in SEARCHES:
                    if stop_flag and stop_flag.is_set():
                        log_progress("Crawl stopped by flag")
                        break
                    if not time_remaining():
                        log_progress(f"Time limit reached, skipping remaining searches")
                        break
                    log_progress(f"Searching: {search.name} ({search.url[:60]}...)")
                    page.goto(search.url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                    time.sleep(2)  # Allow JS to render
                    html = page.content()
                    cards = parse_search_cards(html, search.url, search.category)
                    log_progress(f"  Found {len(cards)} cards on first page")

                    # Paginate all pages until no new cards, max pages, or time limit
                    seen = {c["ad_id"] for c in cards}
                    for page_num in range(2, MAX_PAGES_PER_SEARCH + 1):
                        if stop_flag and stop_flag.is_set():
                            break
                        if not time_remaining():
                            log_progress(f"  Time limit reached after {len(cards)} cards")
                            break
                        # Gumtree uses v1c9199p1, v1c9199p2, etc.
                        next_url = re.sub(r"p\d+", f"p{page_num}", search.url)
                        try:
                            page.goto(next_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                            time.sleep(1)
                            html = page.content()
                            more = parse_search_cards(html, next_url, search.category)
                            new_count = 0
                            for c in more:
                                if c["ad_id"] not in seen:
                                    cards.append(c)
                                    seen.add(c["ad_id"])
                                    new_count += 1
                            if new_count == 0:
                                log_progress(f"  No new cards on page {page_num}, stopping pagination")
                                break
                            log_progress(f"  Page {page_num}: +{new_count} cards ({len(cards)} total)")
                        except Exception as e:
                            LOG.debug("Pagination %s: %s", page_num, e)
                            break

                    log_progress(f"  Collected {len(cards)} cards from {search.name}")

                    search_stored = 0
                    for idx, card in enumerate(cards):
                        if stop_flag and stop_flag.is_set():
                            break
                        if not time_remaining():
                            log_progress(f"  Time limit reached, stopping at {listings_found} listings")
                            break
                        if (idx + 1) % 20 == 0 or idx == 0:
                            log_progress(f"  Fetching details {idx + 1}/{len(cards)} ({search_stored} stored)")
                        if listing_matches_ignore(card, ignore_rules):
                            continue
                        # Fetch detail for full data
                        try:
                            page.goto(card["url"], wait_until="domcontentloaded", timeout=10000)
                            time.sleep(1)
                            detail_html = page.content()
                            detail = parse_detail_page(detail_html, card["url"], search.category)
                            if detail:
                                merged = {**card, **detail}
                                lid, is_new = upsert_listing(
                                    ad_id=merged["ad_id"],
                                    url=merged["url"],
                                    title=merged.get("title"),
                                    price=merged.get("price"),
                                    category=merged.get("category"),
                                    location=merged.get("location"),
                                    seller=merged.get("seller"),
                                    condition=merged.get("condition"),
                                    description=merged.get("description"),
                                    search_job_id=job_id,
                                )
                                listings_found += 1
                                search_stored += 1
                                if is_new:
                                    listings_new += 1
                                else:
                                    listings_updated += 1
                            else:
                                # Fallback: store card data only
                                if not listing_matches_ignore(card, ignore_rules):
                                    lid, is_new = upsert_listing(
                                        ad_id=card["ad_id"],
                                        url=card["url"],
                                        title=card.get("title"),
                                        price=card.get("price"),
                                        category=card.get("category"),
                                        location=card.get("location"),
                                        seller=None,
                                        condition=None,
                                        description=None,
                                        search_job_id=job_id,
                                    )
                                    listings_found += 1
                                    search_stored += 1
                                    if is_new:
                                        listings_new += 1
                                    else:
                                        listings_updated += 1
                        except Exception as e:
                            LOG.debug("Detail fetch %s: %s", card.get("url", "")[:50], e)
                        time.sleep(0.5)  # Be polite

                    log_progress(f"  Finished {search.name}: {search_stored} stored, moving to next search")
                    # Pause between searches to avoid hammering
                    if time_remaining() and search != SEARCHES[-1]:
                        pause = 5
                        log_progress(f"  Pausing {pause}s before next search")
                        time.sleep(pause)

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
