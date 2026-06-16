#!/usr/bin/env python3
"""
Prepare REAL Chrome for Temu Verify all (slider captcha does not work in Playwright).

Usage:
  cd products
  python temu/setup_verify.py
  python setup_browser_sessions.py              # CDP: temu + junkmail
  python setup_browser_sessions.py --all-suppliers  # all 38 session-backed suppliers
  python setup_browser_sessions.py --only temu
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from temu.browser_utils import is_cdp_available, start_manual_chrome
from temu.cdp_verify import open_temu_browser, wait_for_temu_user_ready


def main() -> int:
    print("=" * 60)
    print("Temu verify setup (real Chrome)")
    print("=" * 60)
    print()
    print("Use the Chrome window that opens — NOT an automated browser.")
    print("Complete login and the security slider there.")
    print()

    if is_cdp_available():
        print("Temu Chrome already running on port 9223.")
    else:
        print("Opening Temu Chrome...")
        try:
            start_manual_chrome()
        except Exception as exc:
            print(f"ERROR: {exc}")
            return 1

    print()
    print("1. In that Chrome window, log in to Temu if needed.")
    print("2. Complete the security slider if it appears.")
    print("3. Open any product page and confirm you see prices.")
    print("4. Press Enter here (leave Chrome open for Verify all).")
    print()
    input("Press Enter when Temu works in Chrome...")

    tb = None
    try:
        tb = open_temu_browser(auto_start_chrome=False)
        wait_for_temu_user_ready(tb.page, wait_seconds=5)
        print("OK — Temu Chrome is ready for Verify all.")
    except Exception as exc:
        print(f"Still blocked: {exc}")
        return 1
    finally:
        if tb is not None:
            tb.close()

    print()
    print("Run Verify all on the Temu tab in Edit Products. Keep this Chrome open.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
