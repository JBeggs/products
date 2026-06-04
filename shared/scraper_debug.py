"""
Shared debug helpers for supplier scrapers.

Use when SCRAPER_DEBUG is set or when scrape_options["debug"] is True.
Writes HTML, optional screenshots, and a small JSON sidecar with page metadata.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

LOG = logging.getLogger("products.scraper")


def env_debug_enabled() -> bool:
    v = (os.environ.get("SCRAPER_DEBUG") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def combined_debug(debug_flag: bool | None) -> bool:
    """True if explicit debug flag or SCRAPER_DEBUG env."""
    return bool(debug_flag) or env_debug_enabled()


def _safe_label(label: str, max_len: int = 60) -> str:
    s = "".join(c if c.isalnum() or c in "-_" else "_" for c in (label or "page"))
    return s[:max_len] or "page"


def capture_page_debug(
    page,
    output_dir: Path,
    supplier_slug: str,
    label: str,
    debug: bool,
    error: str = "",
) -> Path | None:
    """
    Save HTML + screenshot + meta JSON under output_dir/debug_capture/.
    Returns base path prefix (without suffix) if anything was written, else None.
    """
    if not combined_debug(debug):
        return None
    dd = output_dir / "debug_capture"
    dd.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = dd / f"{ts}_{supplier_slug}_{_safe_label(label)}"
    meta: dict[str, Any] = {
        "supplier": supplier_slug,
        "label": label,
        "error": error,
        "url": "",
        "title": "",
    }
    try:
        meta["url"] = page.url or ""
    except Exception:
        pass
    try:
        meta["title"] = page.title() or ""
    except Exception:
        pass
    try:
        base.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as e:
        LOG.warning("debug meta write failed: %s", e)
    try:
        base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        LOG.warning("debug html write failed: %s", e)
    try:
        page.screenshot(path=str(base.with_suffix(".png")), full_page=False)
    except Exception as e:
        LOG.debug("debug screenshot skipped: %s", e)
    LOG.info("debug_capture saved prefix=%s", base)
    return base


def log_extract_probe(supplier_slug: str, data: dict[str, Any] | None, debug: bool) -> None:
    """Log which core fields were found (for tuning selectors)."""
    if not combined_debug(debug):
        return
    if not data:
        LOG.warning("[%s] extract: no data", supplier_slug)
        return
    title = (data.get("goodsName") or "")[:120]
    price = data.get("salePrice")
    gcount = len(data.get("gallery") or [])
    desc_len = len((data.get("desc") or ""))
    LOG.debug(
        "[%s] extract probe: title=%r price=%s gallery_n=%d desc_len=%d",
        supplier_slug,
        title,
        price,
        gcount,
        desc_len,
    )


def dump_normalized_fields(
    output_dir: Path,
    supplier_slug: str,
    label: str,
    fields: dict[str, Any],
    debug: bool,
) -> None:
    """Write normalized field candidates as JSON (debug only)."""
    if not combined_debug(debug):
        return
    dd = output_dir / "debug_capture"
    dd.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = dd / f"{ts}_{supplier_slug}_{_safe_label(label)}_fields.json"
    try:
        path.write_text(json.dumps(fields, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        LOG.warning("fields dump failed: %s", e)
