"""
Junk Mail crawler via real Chrome CDP (checkpoint/resume).

Playwright-launched profiles re-trigger Cloudflare. We connect to normal Chrome
on port 9222 and reuse the session that already passed verification.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Callable

from .crawler_limits import get_crawl_limits, price_in_search_range
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
from .parsers import (
    is_jobmail_listing_url,
    merge_search_card_with_detail,
    parse_detail_page,
    parse_search_cards,
    search_page_url,
)
from .scoring import evaluate_listing_for_scenario

CRAWL_TIME_LIMIT_SECONDS = 45 * 60
MAX_PAGES_PER_SEARCH = 50  # hard safety cap; effective limit comes from crawler_filters

LOG = logging.getLogger("junkmail_crawler")

try:
    from junkmail.cdp_fetch import JunkmailBrowser, fetch_html, open_junkmail_browser
except ImportError:
    JunkmailBrowser = None  # type: ignore[assignment,misc]
    fetch_html = None  # type: ignore[assignment,misc]
    open_junkmail_browser = None  # type: ignore[assignment,misc]


def _flatten_searches(scenarios: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for scenario in scenarios:
        for search in scenario.get("searches") or []:
            url = search.get("url") or ""
            category = search.get("category") or scenario.get("category") or ""
            key = (url, category)
            if not url:
                continue
            min_price = search.get("min_price")
            max_price = search.get("max_price")
            if key in by_key:
                existing = by_key[key]
                if min_price is not None:
                    prev = existing.get("min_price")
                    existing["min_price"] = (
                        min(int(prev), int(min_price)) if prev is not None else min_price
                    )
                if max_price is not None:
                    prev = existing.get("max_price")
                    existing["max_price"] = (
                        max(int(prev), int(max_price)) if prev is not None else max_price
                    )
                continue
            by_key[key] = {
                "name": search.get("name") or scenario.get("name") or "Search",
                "url": url,
                "category": category,
                "path_slugs": search.get("path_slugs") or [],
                "min_price": min_price,
                "max_price": max_price,
                "category_path": search.get("category_path"),
                "query": search.get("query"),
            }
    return list(by_key.values())


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
    jm: JunkmailBrowser,
    search: dict[str, Any],
    *,
    stop_flag,
    time_remaining: Callable[[], bool],
    log_progress: Callable[[str], None],
    max_pages: int,
) -> tuple[list[dict[str, Any]], str | None]:
    search_name = search["name"]
    search_url = search["url"]
    category = search["category"]
    path_slugs = search.get("path_slugs") or []
    page_cap = max(1, min(int(max_pages), MAX_PAGES_PER_SEARCH))
    min_p = search.get("min_price")
    max_p = search.get("max_price")
    range_hint = ""
    if min_p is not None or max_p is not None:
        from junkmail_crawler.parsers import is_junkmail_jobs_category_path

        if not is_junkmail_jobs_category_path(search.get("category_path")) and "/jobs/" not in str(
            search.get("url") or ""
        ).lower():
            lo = min_p if min_p is not None else "?"
            hi = max_p if max_p is not None else "?"
            range_hint = f" [R{lo}–R{hi}]"
    log_progress(f"Searching: {search_name}{range_hint} ({search_url[:70]}...)")

    html = fetch_html(jm, search_url, log=log_progress)
    cards = parse_search_cards(html, search_url, category, path_slugs=path_slugs)
    cards = [c for c in cards if price_in_search_range(c.get("price"), search)]
    log_progress(f"  Found {len(cards)} cards on first page (after price filter)")

    seen = {card["ad_id"] for card in cards}
    for page_num in range(2, page_cap + 1):
        if stop_flag and stop_flag.is_set():
            return cards, "flag"
        if not time_remaining():
            return cards, "time"

        next_url = search_page_url(search_url, page_num)
        try:
            html = fetch_html(jm, next_url, log=log_progress)
            more = parse_search_cards(html, next_url, category, path_slugs=path_slugs)
            new_count = 0
            for card in more:
                if card["ad_id"] not in seen and price_in_search_range(card.get("price"), search):
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
    jm: JunkmailBrowser,
    card: dict[str, Any],
    *,
    job_id: int,
    scenarios: list[dict[str, Any]],
    ignore_rules: list[dict],
    category: str,
    search: dict[str, Any],
    store_only_scenario_matches: bool,
) -> tuple[int, int, int, str | None]:
    if listing_matches_ignore(card, ignore_rules):
        return 0, 0, 0, "ignored"

    if not price_in_search_range(card.get("price"), search):
        return 0, 0, 0, "price"

    merged = dict(card)
    if is_jobmail_listing_url(card.get("url")):
        # Job searches on junkmail.co.za embed JobMail referral cards (different site).
        # Read title/location from the Junk Mail search page — never navigate to jobmail.co.za.
        merged = merge_search_card_with_detail(card, None)
    else:
        try:
            detail_html = fetch_html(jm, card["url"], log=None)
            page_url = jm.last_fetched_url or card["url"]
            detail = parse_detail_page(detail_html, page_url, category)
            merged = merge_search_card_with_detail(card, detail)
        except Exception as exc:
            LOG.debug("Detail fetch %s failed: %s", card.get("url", "")[:80], exc)
            merged = merge_search_card_with_detail(card, None)

    if not price_in_search_range(merged.get("price"), search):
        return 0, 0, 0, "price"

    evaluations = [evaluate_listing_for_scenario(merged, scenario) for scenario in scenarios]
    if store_only_scenario_matches and not any(ev["visible"] for ev in evaluations):
        return 0, 0, 0, "scenario_gate"

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

    for scenario, evaluation in zip(scenarios, evaluations):
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

    return 1, (1 if is_new else 0), (0 if is_new else 1), None


def run_crawl(
    stop_flag=None,
    progress_cb: Callable[[str], None] | None = None,
    *,
    resume: bool = True,
) -> dict:
    """
    Run full Junk Mail crawl with checkpoint/resume support.
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

    def persist_checkpoint(cp: dict[str, Any], msg: str) -> None:
        save_job_checkpoint(
            job_id,
            cp,
            listings_found=listings_found,
            listings_new=listings_new,
            listings_updated=listings_updated,
        )
        log_progress(msg)

    try:
        ignore_rules = get_active_ignore_rules()
        scenarios = list_scenario_configs(enabled_only=True)
        searches = _flatten_searches(scenarios)
        crawl_limits = get_crawl_limits()
        fingerprint = _searches_fingerprint(searches)

        if not searches:
            raise RuntimeError("No enabled Junk Mail scenarios configured. Seed or enable at least one scenario.")

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
            f"(max {crawl_limits['max_pages_per_search']} pages/search, "
            f"max {crawl_limits['max_stored_per_search']} stored/search, "
            f"store_only_matches={crawl_limits['store_only_scenario_matches']}, "
            f"time limit: {CRAWL_TIME_LIMIT_SECONDS // 60} min"
            f"{', resuming' if resumed else ''})"
        )

        crawl_start = time.monotonic()
        stop_reason: str | None = None

        def time_remaining() -> bool:
            return (time.monotonic() - crawl_start) < CRAWL_TIME_LIMIT_SECONDS

        search_start = int(checkpoint.get("search_index") or 0)
        detail_start = int(checkpoint.get("card_index") or 0)
        processed_ad_ids = set(checkpoint.get("processed_ad_ids") or [])

        if not open_junkmail_browser or not fetch_html:
            raise RuntimeError("junkmail.cdp_fetch not available")

        jm = open_junkmail_browser(log=log_progress)
        log_progress("Junk Mail crawler started (real Chrome via CDP).")

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
                    jm,
                    search,
                    stop_flag=stop_flag,
                    time_remaining=time_remaining,
                    log_progress=log_progress,
                    max_pages=crawl_limits["max_pages_per_search"],
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
                scenario_gate_skips = 0

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

                    if search_stored >= crawl_limits["max_stored_per_search"]:
                        log_progress(
                            f"  Reached max stored per search ({crawl_limits['max_stored_per_search']}), "
                            f"moving to next search"
                        )
                        break

                    if not price_in_search_range(card.get("price"), search):
                        continue

                    if (idx + 1) % 20 == 0 or idx == card_start:
                        log_progress(
                            f"  Fetching details {idx + 1}/{len(cards)} ({search_stored} stored this search)"
                        )

                    found_delta, new_delta, updated_delta, skip_reason = _process_listing_card(
                        jm,
                        card,
                        job_id=job_id,
                        scenarios=scenarios,
                        ignore_rules=ignore_rules,
                        category=search["category"],
                        search=search,
                        store_only_scenario_matches=crawl_limits["store_only_scenario_matches"],
                    )
                    if skip_reason == "scenario_gate":
                        scenario_gate_skips += 1
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

                if len(cards) > 0 and search_stored == 0 and scenario_gate_skips > 0:
                    log_progress(
                        f"  Found {len(cards)} cards, stored 0 "
                        f"({scenario_gate_skips} skipped by scenario gate)"
                    )
                elif len(cards) > 0 and search_stored == 0:
                    log_progress(f"  Found {len(cards)} cards, stored 0")

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
            jm.close()

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
