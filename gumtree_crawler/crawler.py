"""
Gumtree crawler: headless run orchestration.
Fetches search pages, parses cards, fetches detail pages, evaluates scenario matches,
and stores everything in SQLite.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable

from playwright.sync_api import sync_playwright

from .db import (
    finish_search_job,
    get_active_ignore_rules,
    init_schema,
    insert_search_job,
    list_scenario_configs,
    listing_matches_ignore,
    upsert_listing,
    upsert_scenario_match,
)
from .parsers import parse_detail_page, parse_search_cards
from .scoring import evaluate_listing_for_scenario

# Crawl limits: 45 min total, max 50 pages per search (safety cap)
CRAWL_TIME_LIMIT_SECONDS = 45 * 60
MAX_PAGES_PER_SEARCH = 50

LOG = logging.getLogger("gumtree_crawler")

try:
    from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, PAGE_LOAD_TIMEOUT
except ImportError:
    CHROMIUM_PERFORMANCE_ARGS = ["--disable-blink-features=AutomationControlled"]
    PAGE_LOAD_TIMEOUT = 60000

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _flatten_searches(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand scenarios into concrete search targets for the crawler."""

    searches: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for scenario in scenarios:
        for search in scenario.get("searches") or []:
            key = (search.get("url") or "", search.get("category") or "")
            if not key[0] or key in seen_keys:
                continue
            seen_keys.add(key)
            searches.append(
                {
                    "name": search.get("name") or scenario.get("name") or "Search",
                    "url": search.get("url") or "",
                    "category": search.get("category") or scenario.get("category") or "",
                    "path_slugs": search.get("path_slugs") or [],
                }
            )
    return searches


def run_crawl(stop_flag=None, progress_cb: Callable[[str], None] | None = None) -> dict:
    """
    Run full crawl: scenario-driven search URLs, parse cards, fetch details, apply ignore
    rules, evaluate scenario visibility, and store.
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
        scenarios = list_scenario_configs(enabled_only=True)
        searches = _flatten_searches(scenarios)
        log_progress(
            f"Crawl job {job_id}: {len(ignore_rules)} ignore rules active, "
            f"{len(scenarios)} scenarios, {len(searches)} searches "
            f"(time limit: {CRAWL_TIME_LIMIT_SECONDS // 60} min)"
        )
        if not searches:
            raise RuntimeError("No enabled Gumtree scenarios configured. Seed or enable at least one scenario.")

        crawl_start = time.monotonic()

        def time_remaining() -> bool:
            return (time.monotonic() - crawl_start) < CRAWL_TIME_LIMIT_SECONDS

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
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
                for search in searches:
                    if stop_flag and stop_flag.is_set():
                        log_progress("Crawl stopped by flag")
                        break
                    if not time_remaining():
                        log_progress("Time limit reached, skipping remaining searches")
                        break

                    search_name = search["name"]
                    search_url = search["url"]
                    category = search["category"]
                    path_slugs = search.get("path_slugs") or []
                    log_progress(f"Searching: {search_name} ({search_url[:60]}...)")

                    page.goto(search_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                    time.sleep(2)
                    html = page.content()
                    cards = parse_search_cards(html, search_url, category, path_slugs=path_slugs)
                    log_progress(f"  Found {len(cards)} cards on first page")

                    seen = {card["ad_id"] for card in cards}
                    for page_num in range(2, MAX_PAGES_PER_SEARCH + 1):
                        if stop_flag and stop_flag.is_set():
                            break
                        if not time_remaining():
                            log_progress(f"  Time limit reached after {len(cards)} cards")
                            break
                        next_url = re.sub(r"p\d+", f"p{page_num}", search_url)
                        try:
                            page.goto(next_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                            time.sleep(1)
                            html = page.content()
                            more = parse_search_cards(html, next_url, category, path_slugs=path_slugs)
                            new_count = 0
                            for card in more:
                                if card["ad_id"] not in seen:
                                    cards.append(card)
                                    seen.add(card["ad_id"])
                                    new_count += 1
                            if new_count == 0:
                                log_progress(f"  No new cards on page {page_num}, stopping pagination")
                                break
                            log_progress(f"  Page {page_num}: +{new_count} cards ({len(cards)} total)")
                        except Exception as exc:
                            LOG.debug("Pagination %s failed for %s: %s", page_num, search_url, exc)
                            break

                    log_progress(f"  Collected {len(cards)} cards from {search_name}")
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

                        merged = dict(card)
                        try:
                            page.goto(card["url"], wait_until="domcontentloaded", timeout=10000)
                            time.sleep(1)
                            detail_html = page.content()
                            detail = parse_detail_page(detail_html, card["url"], category)
                            if detail:
                                merged.update(detail)
                        except Exception as exc:
                            LOG.debug("Detail fetch %s failed: %s", card.get("url", "")[:80], exc)

                        listing_id, is_new = upsert_listing(
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
                            posted_at=merged.get("posted_at"),
                            attributes=merged.get("attributes") or {},
                            signals=merged.get("signals") or {},
                        )

                        for scenario in scenarios:
                            evaluation = evaluate_listing_for_scenario(merged, scenario)
                            upsert_scenario_match(
                                listing_id=listing_id,
                                scenario_slug=scenario["slug"],
                                search_job_id=job_id,
                                visible=evaluation["visible"],
                                match_score=evaluation["match_score"],
                                price_score=evaluation["price_score"],
                                urgency_score=evaluation["urgency_score"],
                                special_state=evaluation["special_state"],
                                reasons=evaluation["reasons"],
                            )
                            if evaluation["reasons"] and evaluation["visible"] is False:
                                reasons = ", ".join(evaluation["reasons"][:3])
                                LOG.debug(
                                    "Scenario %s rejected %s: %s",
                                    scenario["slug"],
                                    merged.get("url", "")[:90],
                                    reasons,
                                )

                        listings_found += 1
                        search_stored += 1
                        if is_new:
                            listings_new += 1
                        else:
                            listings_updated += 1
                        time.sleep(0.5)

                    log_progress(f"  Finished {search_name}: {search_stored} stored, moving to next search")
                    if time_remaining() and search != searches[-1]:
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
    except Exception as exc:
        LOG.exception("Crawl error: %s", exc)
        error_msg = str(exc)
        finish_search_job(job_id, status="failed", error=error_msg)

    return {
        "job_id": job_id,
        "listings_found": listings_found,
        "listings_new": listings_new,
        "listings_updated": listings_updated,
        "error": error_msg,
    }
