#!/usr/bin/env python3
"""
Prepare browser sessions for product scrapers and verify flows.

Usage (from products root):
  python setup_browser_sessions.py
      # CDP only: temu (9223) + junkmail (9222) — same as before

  python setup_browser_sessions.py --all-suppliers
      # Every session-backed supplier (CDP + chrome profiles + JSON)

  python setup_browser_sessions.py --all-suppliers --status
      # Table of all suppliers: kind, session on disk, CDP port up

  python setup_browser_sessions.py --list
  python setup_browser_sessions.py --only makro,gumtree,temu
  python setup_browser_sessions.py --continue-on-error
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PRODUCTS_ROOT = Path(__file__).resolve().parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

from shared.supplier_session_registry import (
    SessionKind,
    all_interactive_entries,
    build_registry,
    cdp_entries,
    session_backed_entries,
    session_present,
)
def _parse_only(raw: str | None, allowed: set[str]) -> set[str] | None:
    if not raw:
        return None
    ids = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = ids - allowed
    if unknown:
        raise SystemExit(f"Unknown supplier slug(s): {', '.join(sorted(unknown))}")
    return ids


def _filter_entries(entries: list, only: set[str] | None) -> list:
    if only is None:
        return entries
    return [e for e in entries if e.slug in only]


def _print_status_table(entries: list) -> None:
    total = len(entries)
    present = sum(1 for e in entries if session_present(e))
    print(f"Supplier sessions ({present}/{total} ready):\n")
    print(f"  {'slug':<22} {'kind':<16} {'session':<8} {'detail'}")
    print(f"  {'-' * 22} {'-' * 16} {'-' * 8} {'-' * 40}")
    for e in entries:
        ok = session_present(e)
        status = "yes" if ok else "no"
        detail = ""
        if e.kind == SessionKind.CDP and e.port:
            detail = f"http://127.0.0.1:{e.port}"
        elif e.session_path:
            detail = str(e.session_path.relative_to(PRODUCTS_ROOT))
        print(f"  {e.slug:<22} {e.kind.value:<16} {status:<8} {detail}")


def _run_setups(entries: list, *, continue_on_error: bool) -> int:
    from shared.supplier_session_setup import setup_entry

    if not entries:
        print("No suppliers selected.", file=sys.stderr)
        return 2

    print("=" * 60)
    print("Browser session setup (products)")
    print("=" * 60)
    print()
    print(f"Will run {len(entries)} setup(s): {', '.join(e.slug for e in entries)}")
    print("Complete each browser step when prompted, then press Enter in this terminal.")
    print()

    failures: list[str] = []
    for i, entry in enumerate(entries, start=1):
        print()
        print("-" * 60)
        print(f"[{i}/{len(entries)}] {entry.display_name} ({entry.slug}) — {entry.kind.value}")
        if entry.kind == SessionKind.CDP and entry.port:
            print(f"  CDP port {entry.port}")
        elif entry.session_path:
            print(f"  Path: {entry.session_path.relative_to(PRODUCTS_ROOT)}")
        if session_present(entry):
            print("  Note: session already looks present (will refresh if you continue).")
        print("-" * 60)
        print()

        try:
            code = setup_entry(entry)
        except KeyboardInterrupt:
            print("\nStopped.")
            return 130
        except Exception as exc:
            code = 1
            print(f"ERROR: {exc}")

        if code != 0:
            failures.append(entry.slug)
            print(f"\n*** {entry.slug} setup failed (exit {code}) ***")
            if not continue_on_error and i < len(entries):
                print("Stopping. Use --continue-on-error to run the rest.")
                break
        else:
            print(f"\n*** {entry.slug} setup OK ***")

    print()
    print("=" * 60)
    if failures:
        print(f"Finished with failures: {', '.join(failures)}")
        print("=" * 60)
        return 1

    print("All selected browser sessions are ready.")
    print("=" * 60)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set up browser sessions for product suppliers (CDP, profiles, JSON).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all suppliers in registry and session kind",
    )
    parser.add_argument(
        "--only",
        metavar="SLUGS",
        help="Comma-separated supplier slugs",
    )
    parser.add_argument(
        "--all-suppliers",
        action="store_true",
        help="All session-backed suppliers (not just CDP temu+junkmail)",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Run remaining setups after one fails",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show session status and exit (no browser launch)",
    )
    args = parser.parse_args(argv)

    all_slugs = {e.slug for e in build_registry()}
    try:
        only = _parse_only(args.only, all_slugs)
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.list:
        registry = build_registry()
        backed = len(session_backed_entries())
        print(f"Suppliers in registry: {len(registry)} ({backed} session-backed, 1 manual)\n")
        for e in registry:
            path = ""
            if e.session_path:
                path = f"  → {e.session_path.relative_to(PRODUCTS_ROOT)}"
            port = f"  port {e.port}" if e.port else ""
            print(f"  {e.slug:<22} {e.kind.value:<16}{port}{path}")
        print()
        print("Default (no flags):  CDP only — temu + junkmail")
        print("Full walk:         python setup_browser_sessions.py --all-suppliers")
        print("Status all:        python setup_browser_sessions.py --all-suppliers --status")
        return 0

    if args.all_suppliers:
        entries = all_interactive_entries()
    else:
        entries = cdp_entries()

    entries = _filter_entries(entries, only)

    if args.status:
        _print_status_table(entries if entries else all_interactive_entries())
        return 0

    return _run_setups(entries, continue_on_error=args.continue_on_error)


if __name__ == "__main__":
    raise SystemExit(main())
