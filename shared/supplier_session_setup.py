"""Interactive browser session setup for non-CDP suppliers."""
from __future__ import annotations

import sys
from pathlib import Path

from shared.generic_session_scraper import LAUNCH_ARGS, USER_AGENT
from shared.playwright_utils import PAGE_LOAD_TIMEOUT
from shared.supplier_session_registry import (
    SupplierSessionEntry,
    SessionKind,
    session_present,
)

PRODUCTS_ROOT = Path(__file__).resolve().parent.parent


def setup_json_session(entry: SupplierSessionEntry) -> int:
    if entry.session_path is None or not entry.login_url:
        print(f"ERROR: {entry.slug} missing session path or login URL")
        return 1

    from playwright.sync_api import sync_playwright

    session_file = entry.session_path
    session_file.parent.mkdir(parents=True, exist_ok=True)
    login_url = entry.login_url

    print(f"Opening browser for {entry.display_name} ({entry.slug})…")
    print("  Log in if needed, then press Enter here to save session JSON.")
    if session_present(entry):
        print(f"  Note: session file already exists: {session_file}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=LAUNCH_ARGS)
        ctx_opts: dict = {
            "user_agent": USER_AGENT,
            "viewport": None,
            "locale": "en-ZA",
        }
        if session_file.exists() and session_file.stat().st_size > 0:
            ctx_opts["storage_state"] = str(session_file)
        context = browser.new_context(**ctx_opts)
        page = context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        except Exception as exc:
            print(f"  Warning: navigation: {exc}")
        try:
            input("Press Enter when logged in and ready to save session… ")
        except KeyboardInterrupt:
            print("\nStopped.")
            return 130
        try:
            context.storage_state(path=str(session_file))
            print(f"  Saved: {session_file}")
        except Exception as exc:
            print(f"ERROR saving session: {exc}")
            return 1
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    return 0


def setup_chrome_profile_session(entry: SupplierSessionEntry) -> int:
    if entry.session_path is None or not entry.login_url:
        print(f"ERROR: {entry.slug} missing profile path or login URL")
        return 1

    from playwright.sync_api import sync_playwright

    profile_dir = entry.session_path
    profile_dir.mkdir(parents=True, exist_ok=True)
    login_url = entry.login_url

    print(f"Opening persistent Chrome profile for {entry.display_name} ({entry.slug})…")
    print(f"  Profile: {profile_dir}")
    print("  Log in in the browser; profile saves automatically. Press Enter when done.")
    if session_present(entry):
        print("  Note: profile directory already has data.")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            args=LAUNCH_ARGS,
            user_agent=USER_AGENT,
            viewport=None,
            locale="en-ZA",
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
        except Exception as exc:
            print(f"  Warning: navigation: {exc}")
        try:
            input("Press Enter when login is complete (leave profile on disk)… ")
        except KeyboardInterrupt:
            print("\nStopped.")
            return 130
        finally:
            try:
                context.close()
            except Exception:
                pass
    return 0


def setup_entry(entry: SupplierSessionEntry) -> int:
    if entry.kind == SessionKind.CDP:
        if entry.slug == "temu":
            from temu.setup_verify import main

            return main()
        if entry.slug == "junkmail":
            from junkmail.setup_cloudflare import main

            return main()
        print(f"ERROR: no CDP setup for {entry.slug}")
        return 1
    if entry.kind == SessionKind.JSON:
        return setup_json_session(entry)
    if entry.kind == SessionKind.CHROME_PROFILE:
        return setup_chrome_profile_session(entry)
    print(f"Skip {entry.slug}: no browser session required")
    return 0
