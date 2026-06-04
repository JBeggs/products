"""
Gumtree crawler: headless run orchestration.
Fetches search pages, parses cards, fetches detail pages, evaluates scenario matches,
and stores everything in SQLite. Supports resume from the last checkpoint after interruption.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Callable

from playwright.sync_api import sync_playwright

from .db import (
    abandon_search_job,
    abandon_stale_running_jobs,
    finish_search_job,
    get_active_ignore_rules,
    get_resumable_job,
    init_schema,
    insert_search_job,
    list_scenario_configs,
    listing_matches_ignore,
    save_job_checkpoint,
    upsert_listing,
    upsert_scenario_match,
)
from .parsers import parse_detail_page, parse_search_cards
from .scoring import evaluate_listing_for_scenario

# Crawl limits: 45 min total per run/resume segment, max 50 pages per search (safety cap)
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


def _searches_fingerprint(searches: list[dict[str, Any]]) -> str:
    payload = json.dumps([(s.get("url"), s.get("category")) for s in searches], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _empty_checkpoint(searches_fingerprint: str) -> dict[str, Any]:
    return {
        "searches_fingerprint": searches_fingerprint,
        "search_index": 0,
        "phase": "collect",
        "card_index": 0,
        "processed_ad_ids": [],
    }


def _collect_search_cards(
    page,
    search: dict[str, Any],
    *,
    stop_flag,
    time_remaining: Callable[[], bool],
    log_progress: Callable[[str], None],
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Paginate one Gumtree search and return listing cards.
    Returns (cards, stop_reason) where stop_reason is 'flag', 'time', or None.
    """

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
            return cards, "flag"
        if not time_remaining():
            return cards, "time"

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
    return cards, None


def _process_listing_card(
    page,
    card: dict[str, Any],
    *,
    job_id: int,
    scenarios: list[dict[str, Any]],
    ignore_rules: list[dict],
    category: str,
) -> tuple[int, int, int]:
    """Fetch detail page, upsert listing, score scenarios. Returns (found_delta, new_delta, updated_delta)."""

    if listing_matches_ignore(card, ignore_rules):
        return 0, 0, 0

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

    return 1, (1 if is_new else 0), (0 if is_new else 1)


def run_crawl(
    stop_flag=None,
    progress_cb: Callable[[str], None] | None = None,
    *,
    resume: bool = True,
) -> dict:
    """
    Run full crawl: scenario-driven search URLs, parse cards, fetch details, apply ignore
    rules, evaluate scenario visibility, and store.
    When resume=True, continues the most recent interrupted job from its checkpoint.
    Returns {job_id, listings_found, listings_new, listings_updated, error, resumed}.
    """

    init_schema()
    listings_found = 0
    listings_new = 0
    listings_updated = 0
    error_msg = None
    resumed = False
    job_id: int | None = None
    checkpoint: dict[str, Any] = {}

    def log_progress(msg: str) -> None:
        LOG.info(msg)
        if progress_cb:
            progress_cb(msg)

    def persist_checkpoint(checkpoint: dict[str, Any], msg: str) -> None:
        save_job_checkpoint(
            job_id,
            checkpoint,
            listings_found=listings_found,
            listings_new=listings_new,
            listings_updated=listings_updated,
        )
        log_progress(msg)

    try:
        ignore_rules = get_active_ignore_rules()
        scenarios = list_scenario_configs(enabled_only=True)
        searches = _flatten_searches(scenarios)
        fingerprint = _searches_fingerprint(searches)

        if not searches:
            raise RuntimeError("No enabled Gumtree scenarios configured. Seed or enable at least one scenario.")

        checkpoint = _empty_checkpoint(fingerprint)

        if resume:
            resumable = get_resumable_job()
            if resumable:
                saved = resumable.get("checkpoint") or {}
                if (
                    saved.get("searches_fingerprint") == fingerprint
                    and int(saved.get("search_index") or 0) < len(searches)
                ):
                    job_id = int(resumable["id"])
                    checkpoint = saved
                    listings_found = int(resumable.get("listings_found") or 0)
                    listings_new = int(resumable.get("listings_new") or 0)
                    listings_updated = int(resumable.get("listings_updated") or 0)
                    resumed = True
                    log_progress(
                        f"Resuming crawl job {job_id} from search "
                        f"{checkpoint.get('search_index', 0) + 1}/{len(searches)} "
                        f"(card {checkpoint.get('card_index', 0)})"
                    )
                else:
                    abandon_search_job(int(resumable["id"]))

        if job_id is None:
            if not resume:
                resumable = get_resumable_job()
                if resumable:
                    abandon_search_job(int(resumable["id"]))
            abandon_stale_running_jobs()
            job_id = insert_search_job()
            checkpoint = _empty_checkpoint(fingerprint)

        log_progress(
            f"Crawl job {job_id}: {len(ignore_rules)} ignore rules active, "
            f"{len(scenarios)} scenarios, {len(searches)} searches "
            f"(time limit: {CRAWL_TIME_LIMIT_SECONDS // 60} min"
            f"{', resuming' if resumed else ''})"
        )

        crawl_start = time.monotonic()
        stop_reason: str | None = None

        def time_remaining() -> bool:
            return (time.monotonic() - crawl_start) < CRAWL_TIME_LIMIT_SECONDS

        search_start = int(checkpoint.get("search_index") or 0)
        detail_start = int(checkpoint.get("card_index") or 0)
        processed_ad_ids = set(checkpoint.get("processed_ad_ids") or [])

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
                for search_idx in range(search_start, len(searches)):
                    if stop_flag and stop_flag.is_set():
                        stop_reason = "flag"
                        break
                    if not time_remaining():
                        stop_reason = "time"
                        break

                    search = searches[search_idx]
                    cards, collect_stop = _collect_search_cards(
                        page,
                        search,
                        stop_flag=stop_flag,
                        time_remaining=time_remaining,
                        log_progress=log_progress,
                    )
                    if collect_stop:
                        stop_reason = collect_stop
                        checkpoint = {
                            "searches_fingerprint": fingerprint,
                            "search_index": search_idx,
                            "phase": "collect",
                            "card_index": 0,
                            "processed_ad_ids": sorted(processed_ad_ids),
                        }
                        break

                    card_start = detail_start if search_idx == search_start and resumed else 0
                    detail_start = 0
                    resumed = False
                    search_stored = 0

                    checkpoint = {
                        "searches_fingerprint": fingerprint,
                        "search_index": search_idx,
                        "phase": "details",
                        "card_index": card_start,
                        "processed_ad_ids": sorted(processed_ad_ids),
                    }
                    persist_checkpoint(
                        checkpoint,
                        f"  Starting details for {search['name']} ({len(cards)} cards, from #{card_start + 1})",
                    )

                    for idx in range(card_start, len(cards)):
                        if stop_flag and stop_flag.is_set():
                            stop_reason = "flag"
                            break
                        if not time_remaining():
                            stop_reason = "time"
                            break

                        card = cards[idx]
                        ad_id = card.get("ad_id")
                        if ad_id and ad_id in processed_ad_ids:
                            continue

                        if (idx + 1) % 20 == 0 or idx == card_start:
                            log_progress(
                                f"  Fetching details {idx + 1}/{len(cards)} ({search_stored} stored this search)"
                            )

                        found_delta, new_delta, updated_delta = _process_listing_card(
                            page,
                            card,
                            job_id=job_id,
                            scenarios=scenarios,
                            ignore_rules=ignore_rules,
                            category=search["category"],
                        )
                        listings_found += found_delta
                        listings_new += new_delta
                        listings_updated += updated_delta
                        search_stored += found_delta

                        if ad_id:
                            processed_ad_ids.add(ad_id)

                        checkpoint = {
                            "searches_fingerprint": fingerprint,
                            "search_index": search_idx,
                            "phase": "details",
                            "card_index": idx + 1,
                            "processed_ad_ids": sorted(processed_ad_ids),
                        }
                        save_job_checkpoint(
                            job_id,
                            checkpoint,
                            listings_found=listings_found,
                            listings_new=listings_new,
                            listings_updated=listings_updated,
                        )
                        time.sleep(0.5)

                    if stop_reason:
                        break

                    log_progress(f"  Finished {search['name']}: {search_stored} stored, moving to next search")
                    checkpoint = {
                        "searches_fingerprint": fingerprint,
                        "search_index": search_idx + 1,
                        "phase": "collect",
                        "card_index": 0,
                        "processed_ad_ids": sorted(processed_ad_ids),
                    }
                    persist_checkpoint(checkpoint, f"  Search {search_idx + 1}/{len(searches)} complete")

                    if search_idx < len(searches) - 1 and time_remaining():
                        pause = 5
                        log_progress(f"  Pausing {pause}s before next search")
                        time.sleep(pause)
            finally:
                context.close()
                browser.close()

        if stop_reason:
            finish_search_job(
                job_id,
                status="interrupted",
                listings_found=listings_found,
                listings_new=listings_new,
                listings_updated=listings_updated,
                checkpoint=checkpoint,
            )
            reason_text = "stopped by flag" if stop_reason == "flag" else "time limit reached"
            log_progress(
                f"Crawl interrupted ({reason_text}): {listings_found} found, "
                f"{listings_new} new, {listings_updated} updated — will resume on next run"
            )
        else:
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
        if job_id is not None:
            finish_search_job(
                job_id,
                status="failed",
                listings_found=listings_found,
                listings_new=listings_new,
                listings_updated=listings_updated,
                error=error_msg,
                checkpoint=checkpoint if checkpoint else None,
            )

    return {
        "job_id": job_id,
        "listings_found": listings_found,
        "listings_new": listings_new,
        "listings_updated": listings_updated,
        "error": error_msg,
        "resumed": resumed,
    }
