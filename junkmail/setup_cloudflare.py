#!/usr/bin/env python3
"""
Verify Junk Mail access in REAL Chrome and leave the session ready to crawl.

Usage:
  cd products
  python junkmail/setup_cloudflare.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from junkmail.browser_utils import is_cdp_available, start_manual_chrome
from junkmail.cdp_fetch import fetch_html, open_junkmail_browser
from junkmail.http_session import export_cookies_via_cdp
from junkmail_crawler.parsers import parse_search_cards


def main() -> int:
    print("=" * 60)
    print("Junk Mail Cloudflare setup")
    print("=" * 60)
    print()
    print("REAL Chrome only. Leave Chrome OPEN — do NOT Cmd+Q before Enter.")
    print()

    if is_cdp_available():
        print("Junk Mail Chrome already open — use that window.")
    else:
        print("Opening Chrome...")
        try:
            start_manual_chrome()
        except Exception as exc:
            print(f"ERROR: {exc}")
            return 1

    print()
    print("1. In that Chrome window, wait until junkmail.co.za shows LISTINGS")
    print("   (not 'Performing security verification').")
    print("2. Press Enter here — Chrome stays open.")
    print()
    input("Press Enter when the site works...")

    print("Saving cookies and testing fetch through Chrome...")
    try:
        export_cookies_via_cdp()
    except Exception as exc:
        print(f"Warning: cookie save failed ({exc}) — crawl can still work via Chrome.")

    jm = None
    try:
        jm = open_junkmail_browser(auto_start_chrome=False)
        test_url = "https://www.junkmail.co.za/q-laser%20cutter/so-latest"
        html = fetch_html(jm, test_url)
        cards = parse_search_cards(html, test_url, "test", path_slugs=["laser"])
    except Exception as exc:
        print(f"ERROR: {exc}")
        print()
        print("If verification never clears, delete junkmail/chrome_profile/ and run again.")
        return 1
    finally:
        if jm is not None:
            jm.close()

    print(f"OK — {len(cards)} listing card(s) parsed.")
    print()
    print("Done. Leave Chrome open for Run now, or quit it — crawl will reopen Chrome if needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
