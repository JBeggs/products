"""SHEIN (za.shein.com) PDP extraction — SPA, no JSON-LD; price renders after JS."""
from __future__ import annotations

import logging
import re
from typing import Any

from shared.dom_product_extract import extract_generic_retail_product

_LOG = logging.getLogger("products.scraper")

# SHEIN ZA PDP: wait for BFF/hydration; price in product-intro__head-mainprice (see SHEIN scraper guides).
SHEIN_PDP_READY_JS = """
() => {
  const title = document.querySelector(
    '.product-intro__head-name, h1.product-intro__head-name, h1'
  );
  const price = document.querySelector(
    '.product-intro__head-mainprice, .product-intro__head-price, [class*="head-mainprice"], .final-price'
  );
  return !!(title || price);
}
"""

SHEIN_PRICE_AND_META_JS = r"""
() => {
  function parseZA(text) {
    if (!text) return null;
    const t = String(text).replace(/\u00a0/g, ' ').trim();
    const m = t.match(/R\s*([\d\s\u202f\.]+(?:[,\.]\d{2})?)/i);
    if (!m) return null;
    let raw = m[1].replace(/\s/g, '').trim();
    const decComma = /^(\d+),(\d{2})$/;
    if (decComma.test(raw)) {
      const p = raw.match(decComma);
      return parseFloat(p[1] + '.' + p[2]);
    }
    const n = parseFloat(raw.replace(',', '.'));
    return (!isNaN(n) && n > 0 && n < 10000000) ? n : null;
  }

  function normalizePrice(n) {
    if (n == null || isNaN(n)) return null;
    const v = Number(n);
    if (v <= 0) return null;
    // Minor units heuristic (e.g. 12500 → 125.00 ZAR)
    if (v >= 10000 && Number.isInteger(v)) return v / 100;
    return v;
  }

  function digPrice(obj, depth) {
    if (!obj || typeof obj !== 'object' || depth > 8) return null;
    const keys = [
      'salePrice', 'retailPrice', 'unitPrice', 'discountPrice',
      'mall_price', 'price', 'retailDiscountPrice', 'usdAmount',
    ];
    for (const k of keys) {
      if (obj[k] != null) {
        const p = normalizePrice(parseFloat(String(obj[k]).replace(/,/g, '')));
        if (p != null) return p;
      }
    }
    for (const v of Object.values(obj)) {
      const found = digPrice(v, depth + 1);
      if (found != null) return found;
    }
    return null;
  }

  let salePrice = null;
  const priceSels = [
    '.product-intro__head-mainprice',
    '.product-intro__head-price',
    '.product-intro__mainprice',
    '[class*="product-intro__head-mainprice"]',
    '[class*="head-mainprice"]',
    '.final-price',
    '.from-bff-price',
  ];
  for (const sel of priceSels) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const p = parseZA(el.textContent || '');
    if (p != null) { salePrice = p; break; }
  }

  if (salePrice == null) {
    const roots = [
      window.gbRawData,
      window.gbProductDetail,
      window.__INITIAL_STATE__,
      window.__NEXT_DATA__,
    ];
    for (const r of roots) {
      const p = digPrice(r, 0);
      if (p != null) { salePrice = p; break; }
    }
  }

  if (salePrice == null) {
    for (const s of document.querySelectorAll('script')) {
      const t = s.textContent || '';
      const m = t.match(/"salePrice"\s*:\s*"?([\d.]+)"?/)
        || t.match(/"retailPrice"\s*:\s*"?([\d.]+)"?/)
        || t.match(/"unitPrice"\s*:\s*"?([\d.]+)"?/);
      if (m) {
        const p = normalizePrice(parseFloat(m[1]));
        if (p != null) { salePrice = p; break; }
      }
    }
  }

  let goodsName = null;
  const nameEl = document.querySelector('.product-intro__head-name, h1.product-intro__head-name, h1');
  if (nameEl) goodsName = (nameEl.textContent || '').replace(/\s+/g, ' ').trim();

  return {
    salePrice: salePrice != null && !isNaN(salePrice) ? salePrice : null,
    goodsName: goodsName || null,
  };
}
"""


def shein_goods_id_from_url(url: str) -> str:
    """Numeric goods id from ...-p-{id}.html."""
    m = re.search(r"-p-(\d+)", url or "", re.I)
    if m:
        return m.group(1)
    from shared.dom_product_extract import goods_id_from_url

    return goods_id_from_url(url)


def wait_for_shein_pdp(page, timeout_ms: int = 25000) -> None:
    """Wait for SHEIN SPA to render title/price before extract."""
    try:
        page.wait_for_function(SHEIN_PDP_READY_JS, timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass


def extract_shein_product_data(page, debug: bool = False) -> dict[str, Any] | None:
    """Merge generic retail extract with SHEIN-specific price/title."""
    wait_for_shein_pdp(page)
    data: dict[str, Any] = extract_generic_retail_product(page, debug=debug) or {}
    try:
        shein = page.evaluate(SHEIN_PRICE_AND_META_JS)
    except Exception as exc:
        if debug:
            _LOG.exception("SHEIN price evaluate failed: %s", exc)
        shein = {}
    if isinstance(shein, dict):
        if shein.get("salePrice") is not None:
            data["salePrice"] = shein["salePrice"]
        if shein.get("goodsName") and not data.get("goodsName"):
            data["goodsName"] = shein["goodsName"]
    if data.get("goodsName") or data.get("salePrice") is not None:
        return data
    return None
