"""Attach Playwright to real Chrome via CDP (Junk Mail, Temu verify, etc.)."""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Browser


def _patch_playwright_skip_download_behavior() -> bool:
    """One-time patch for Playwright <1.60 CDP attach on real Chrome."""
    import playwright

    path = Path(playwright.__file__).resolve().parent / "driver/package/lib/server/chromium/crBrowser.js"
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    needle = 'if (this._browser.options.name !== "clank" && this._options.acceptDownloads !== "internal-browser-default") {'
    patched = 'if (false && this._browser.options.name !== "clank" && this._options.acceptDownloads !== "internal-browser-default") {'
    if patched in text:
        return True
    if needle not in text:
        return False
    path.write_text(text.replace(needle, patched, 1), encoding="utf-8")
    return True


def connect_cdp_browser(playwright, cdp_url: str) -> Browser:
    """
    Attach to real Chrome without Browser.setDownloadBehavior failures.

    Playwright 1.60+ supports no_defaults=True. Older versions are patched once in-place.
    """
    connect = playwright.chromium.connect_over_cdp
    try:
        return connect(cdp_url, no_defaults=True)
    except TypeError:
        try:
            return connect(cdp_url)
        except Exception as exc:
            err = str(exc)
            if "setDownloadBehavior" in err or "context management is not supported" in err:
                if _patch_playwright_skip_download_behavior():
                    return connect(cdp_url)
            raise
    except Exception as exc:
        err = str(exc)
        if "setDownloadBehavior" in err or "context management is not supported" in err:
            if _patch_playwright_skip_download_behavior():
                try:
                    return connect(cdp_url, no_defaults=True)
                except TypeError:
                    return connect(cdp_url)
        raise
