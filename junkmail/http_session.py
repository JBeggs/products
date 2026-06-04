"""
HTTP fetch for Junk Mail using cookies exported from real Chrome.

Playwright re-triggers Cloudflare. After you pass verification in normal Chrome,
we copy cookies (via CDP while Chrome is open, or from the profile after quit)
and crawl with curl_cffi so TLS fingerprint matches Chrome.
"""
from __future__ import annotations

import json
import logging

from curl_cffi import requests as creq

from junkmail.browser_utils import CHROME_PROFILE, DEFAULT_CDP_PORT, SESSION_FILE, is_cdp_available
from junkmail_crawler.parsers import is_cloudflare_challenge

LOG = logging.getLogger("junkmail.http_session")

IMPERSONATE = "chrome120"

SETUP_MSG = (
    "No valid Junk Mail cookies.\n\n"
    "1. Quit ALL Chrome (Cmd+Q)\n"
    "2. Run:  python junkmail/setup_cloudflare.py\n"
    "3. Pass Cloudflare in the Chrome window that opens (real browser, not automation)\n"
    "4. Press Enter when listings load — keep Chrome open until export finishes\n"
    "5. Run now on /junkmail-crawler"
)

CF_MSG = (
    "Cloudflare blocked the crawl — cookies expired or verification was not completed.\n"
    "Re-run: python junkmail/setup_cloudflare.py"
)


def export_cookies_via_cdp(cdp_url: str | None = None) -> int:
    """Read live cookies from open Chrome (best — includes cf_clearance + __cf_bm)."""
    from playwright.sync_api import sync_playwright

    target = cdp_url or f"http://127.0.0.1:{DEFAULT_CDP_PORT}"
    if not is_cdp_available(target):
        raise RuntimeError(f"Chrome CDP not available at {target}. Is Junk Mail Chrome still open?")

    from shared.cdp_connect import connect_cdp_browser

    with sync_playwright() as playwright:
        browser = connect_cdp_browser(playwright, target)
        try:
            if not browser.contexts:
                raise RuntimeError("No Chrome context found on CDP port.")
            context = browser.contexts[0]
            SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(SESSION_FILE))
            cookies = context.cookies()
        finally:
            browser.close()

    junkmail_count = sum(1 for c in cookies if "junkmail" in (c.get("domain") or ""))
    if junkmail_count == 0:
        raise RuntimeError("No junkmail.co.za cookies in Chrome. Pass Cloudflare first.")
    if not any(c.get("name") == "cf_clearance" for c in cookies):
        LOG.warning("cf_clearance missing — Cloudflare may not be cleared yet")
    return junkmail_count


def export_cookies_from_profile() -> int:
    """Read junkmail.co.za cookies from Chrome profile after user quit Chrome."""
    try:
        import browser_cookie3
    except ImportError as exc:
        raise RuntimeError("Install browser-cookie3: pip install browser-cookie3") from exc

    cookie_file = CHROME_PROFILE / "Default" / "Cookies"
    if not cookie_file.exists():
        raise RuntimeError(
            f"No cookie database at {cookie_file}. "
            "Run setup_cloudflare.py and pass Cloudflare in Chrome first."
        )

    playwright_cookies: list[dict] = []
    for c in browser_cookie3.chrome(cookie_file=str(cookie_file)):
        domain = c.domain or ""
        if "junkmail" not in domain:
            continue
        rest = getattr(c, "_rest", None) or {}
        same_site = "Lax"
        if rest.get("SameSite") == "Strict":
            same_site = "Strict"
        elif rest.get("SameSite") == "None":
            same_site = "None"
        playwright_cookies.append(
            {
                "name": c.name,
                "value": c.value,
                "domain": domain,
                "path": c.path or "/",
                "expires": c.expires if c.expires else -1,
                "httpOnly": bool(rest.get("HttpOnly")),
                "secure": bool(c.secure),
                "sameSite": same_site,
            }
        )

    if not any(c["name"] == "cf_clearance" for c in playwright_cookies):
        LOG.warning("cf_clearance cookie not found — Cloudflare may not have been passed")

    if not playwright_cookies:
        raise RuntimeError("No junkmail.co.za cookies in profile. Pass Cloudflare in Chrome first.")

    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(
        json.dumps({"cookies": playwright_cookies, "origins": []}, indent=2),
        encoding="utf-8",
    )
    return len(playwright_cookies)


def _load_cookie_dict() -> dict[str, str]:
    if not SESSION_FILE.exists():
        raise RuntimeError(SETUP_MSG)
    data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    cookies = data.get("cookies") or []
    if not cookies:
        raise RuntimeError(SETUP_MSG)
    return {c["name"]: c["value"] for c in cookies}


def build_requests_session() -> creq.Session:
    """curl_cffi session with Chrome TLS fingerprint + saved cookies."""
    cookie_dict = _load_cookie_dict()
    session = creq.Session(impersonate=IMPERSONATE)
    session.cookies.update(cookie_dict)
    return session


def fetch_html(session: creq.Session, url: str, *, timeout: int = 60) -> str:
    resp = session.get(url, timeout=timeout)
    if resp.status_code == 403 and is_cloudflare_challenge(resp.text):
        raise RuntimeError(CF_MSG)
    resp.raise_for_status()
    html = resp.text
    if is_cloudflare_challenge(html):
        raise RuntimeError(CF_MSG)
    return html
