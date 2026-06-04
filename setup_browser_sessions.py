#!/usr/bin/env python3
"""
Prepare all real-Chrome (CDP) browser sessions used by products verify/crawl.

Runs each supplier's setup script in turn so you do not start them one by one.

Usage (from products root):
  python setup_browser_sessions.py
  python setup_browser_sessions.py --list
  python setup_browser_sessions.py --only temu
  python setup_browser_sessions.py --only temu,junkmail
  python setup_browser_sessions.py --continue-on-error

Sessions:
  temu      — port 9223 — Temu Verify all (login + security slider)
  junkmail  — port 9222 — Junk Mail crawler / Cloudflare
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PRODUCTS_ROOT = Path(__file__).resolve().parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))


@dataclass(frozen=True)
class BrowserSessionSetup:
    id: str
    name: str
    port: int
    description: str
    run: Callable[[], int]
    is_cdp_up: Callable[[], bool]


def _temu_cdp_up() -> bool:
    from temu.browser_utils import is_cdp_available

    return is_cdp_available()


def _junkmail_cdp_up() -> bool:
    from junkmail.browser_utils import is_cdp_available

    return is_cdp_available()


def _run_temu_setup() -> int:
    from temu.setup_verify import main

    return main()


def _run_junkmail_setup() -> int:
    from junkmail.setup_cloudflare import main

    return main()


SESSIONS: tuple[BrowserSessionSetup, ...] = (
    BrowserSessionSetup(
        id="temu",
        name="Temu Verify",
        port=9223,
        description="Edit Products → Verify all on Temu tab (real Chrome, not Playwright)",
        run=_run_temu_setup,
        is_cdp_up=_temu_cdp_up,
    ),
    BrowserSessionSetup(
        id="junkmail",
        name="Junk Mail",
        port=9222,
        description="Junk Mail crawler + fetch images (Cloudflare via real Chrome)",
        run=_run_junkmail_setup,
        is_cdp_up=_junkmail_cdp_up,
    ),
)


def _parse_only(raw: str | None) -> set[str] | None:
    if not raw:
        return None
    ids = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = ids - {s.id for s in SESSIONS}
    if unknown:
        raise SystemExit(f"Unknown session id(s): {', '.join(sorted(unknown))}")
    return ids


def _selected_sessions(only: set[str] | None) -> list[BrowserSessionSetup]:
    if only is None:
        return list(SESSIONS)
    return [s for s in SESSIONS if s.id in only]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run all real-Chrome browser session setup scripts (Temu, Junk Mail, …).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured sessions and exit",
    )
    parser.add_argument(
        "--only",
        metavar="IDS",
        help="Comma-separated session ids (e.g. temu or temu,junkmail)",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Run remaining setups after one fails",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show whether each CDP port is up, then exit (no setup)",
    )
    args = parser.parse_args(argv)

    try:
        only = _parse_only(args.only)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    sessions = _selected_sessions(only)

    if args.list:
        print("Browser session setups:\n")
        for s in SESSIONS:
            print(f"  {s.id:10}  port {s.port}  {s.name}")
            print(f"             {s.description}")
        print()
        print("Run: python setup_browser_sessions.py")
        print("Or:  python setup_browser_sessions.py --only temu")
        return 0

    if args.status:
        print("CDP status:\n")
        for s in SESSIONS:
            up = "up" if s.is_cdp_up() else "down"
            print(f"  {s.id:10}  http://127.0.0.1:{s.port}  {up}")
        return 0

    if not sessions:
        print("No sessions selected.", file=sys.stderr)
        return 2

    print("=" * 60)
    print("Browser session setup (products)")
    print("=" * 60)
    print()
    print(f"Will run {len(sessions)} setup(s): {', '.join(s.id for s in sessions)}")
    print("Complete each Chrome window when prompted, then press Enter in this terminal.")
    print()

    failures: list[str] = []
    for i, session in enumerate(sessions, start=1):
        print()
        print("-" * 60)
        print(f"[{i}/{len(sessions)}] {session.name} (port {session.port})")
        print(session.description)
        if session.is_cdp_up():
            print(f"Note: Chrome already listening on port {session.port}.")
        print("-" * 60)
        print()

        try:
            code = session.run()
        except KeyboardInterrupt:
            print("\nStopped.")
            return 130
        except Exception as exc:
            code = 1
            print(f"ERROR: {exc}")

        if code != 0:
            failures.append(session.id)
            print(f"\n*** {session.id} setup failed (exit {code}) ***")
            if not args.continue_on_error and i < len(sessions):
                print("Stopping. Use --continue-on-error to run the rest.")
                break
        else:
            print(f"\n*** {session.id} setup OK ***")

    print()
    print("=" * 60)
    if failures:
        print(f"Finished with failures: {', '.join(failures)}")
        print("=" * 60)
        return 1

    print("All selected browser sessions are ready.")
    print()
    print("Keep Chrome open while using:")
    for s in sessions:
        if s.id == "temu":
            print("  • Edit Products → Verify all (Temu tab)")
        elif s.id == "junkmail":
            print("  • Junk Mail crawler / Fetch images")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
