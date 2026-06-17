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
    '.product-intro__head-name h1, .product-info h1, h1.product-intro__head-name, h1'
  );
  const price = document.querySelector(
    '#productMainPriceId[aria-label], .productPrice__main[aria-label], .product-intro__head-mainprice, .product-intro__head-price'
  );
  const purchase = document.querySelector(
    '.purchase-control, .product-intro__size-choose, .main-sales-attr__color-container'
  );
  return !!((title || price) && (purchase || price));
}
"""

SHEIN_VARIANTS_JS = r"""
() => {
  function clean(text) {
    return String(text || '').replace(/\s+/g, ' ').trim();
  }

  function addUnique(list, seen, label) {
    const t = clean(label);
    if (!t || t.length < 2 || t.length > 120 || seen.has(t.toLowerCase())) return;
    seen.add(t.toLowerCase());
    list.push(t);
  }

  function digSkuList(obj, depth) {
    if (!obj || typeof obj !== 'object' || depth > 10) return [];
    if (Array.isArray(obj.sku_list) && obj.sku_list.length) {
      const out = [];
      const seen = new Set();
      for (const sku of obj.sku_list) {
        if (!sku || typeof sku !== 'object') continue;
        const attrs = sku.sku_sale_attr || sku.skuSaleAttr || sku.sale_attr || [];
        const parts = [];
        if (Array.isArray(attrs)) {
          for (const a of attrs) {
            if (!a || typeof a !== 'object') continue;
            const v = a.value_en || a.attr_value_name || a.value_name || a.value;
            const part = clean(v);
            if (part) parts.push(part);
          }
        }
        const label = parts.join(' / ');
        if (label) addUnique(out, seen, label);
      }
      if (out.length) return out;
    }
    for (const v of Object.values(obj)) {
      const found = digSkuList(v, depth + 1);
      if (found.length) return found;
    }
    return [];
  }

  const variants = [];
  const seen = new Set();

  const styleOptions = [];
  document.querySelectorAll(
    '.main-sales-attr__color-container .radio-container[role="radio"], .product-intro__color .radio-container[role="radio"]'
  ).forEach(function (el) {
    const label = clean(el.getAttribute('aria-label') || el.textContent);
    if (label) styleOptions.push(label);
  });
  if (styleOptions.length > 1) {
    styleOptions.forEach(function (label) { addUnique(variants, seen, label); });
  }

  document.querySelectorAll('.product-intro__size-radio[data-attr_value_name]').forEach(function (el) {
    addUnique(variants, seen, el.getAttribute('data-attr_value_name'));
  });
  document.querySelectorAll('.product-intro__size-radio-inner').forEach(function (el) {
    addUnique(variants, seen, el.textContent);
  });

  if (variants.length === 0) {
    document.querySelectorAll(
      '.product-intro__size-radio, [class*="size-radio"], .sales-attr__item, .product-intro__attr-radio'
    ).forEach(function (el) {
      addUnique(
        variants,
        seen,
        el.getAttribute('data-attr_value_name')
          || el.getAttribute('aria-label')
          || el.getAttribute('title')
          || el.textContent
      );
    });
  }

  if (variants.length === 0) {
    const roots = [window.gbRawData, window.gbProductDetail, window.__INITIAL_STATE__, window.__NEXT_DATA__];
    for (let i = 0; i < roots.length; i++) {
      const fromState = digSkuList(roots[i], 0);
      if (fromState.length) {
        fromState.forEach(function (label) { addUnique(variants, seen, label); });
        break;
      }
    }
  }

  const selectedParts = [];
  const activeStyle = document.querySelector(
    '.main-sales-attr__color-container .radio-container.active[aria-checked="true"], .main-sales-attr__color-container .radio-container[aria-checked="true"]'
  );
  if (activeStyle) {
    const styleLabel = clean(activeStyle.getAttribute('aria-label') || activeStyle.textContent);
    if (styleLabel && styleOptions.length > 1) selectedParts.push(styleLabel);
  }

  const activeSize = document.querySelector(
    '.product-intro__size-radio_active, .product-intro__size-radio[aria-checked="true"]'
  );
  if (activeSize) {
    selectedParts.push(
      clean(
        activeSize.getAttribute('data-attr_value_name')
          || activeSize.getAttribute('aria-label')
          || activeSize.textContent
      )
    );
  }

  const selectedVariant = selectedParts.filter(Boolean).join(' / ') || null;
  const variantSize = selectedParts.length ? selectedParts[selectedParts.length - 1] : null;

  return {
    variants: variants,
    selectedVariant: selectedVariant,
    variantSize: variantSize,
  };
}
"""

SHEIN_PRICE_AND_META_JS = r"""
() => {
  function parseZA(text) {
    if (!text) return null;
    const t = String(text).replace(/\u00a0/g, ' ').trim();
    const m = t.match(/R\s*([\d\s\u202f\.]+(?:[,\.]\d{2})?)/i)
      || t.match(/ZAR\s*([\d\s\u202f\.]+(?:[,\.]\d{2})?)/i);
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

  function priceFromEl(el) {
    if (!el) return null;
    return parseZA(el.getAttribute('aria-label') || '')
      || parseZA(el.textContent || '');
  }

  const priceRoot = document.querySelector(
    '#priceContainer, #productPriceId, .atf-right .product-info, .atf-right'
  ) || document;

  let salePrice = null;
  let retailPrice = null;

  const mainSelectors = [
    '#productMainPriceId',
    '.productPrice__main',
    '.product-intro__head-mainprice',
    '.product-intro__head-price',
    '.product-intro__mainprice',
    '[class*="product-intro__head-mainprice"]',
    '[class*="head-mainprice"]',
    '.final-price',
    '.from-bff-price',
  ];
  for (let i = 0; i < mainSelectors.length; i++) {
    const el = priceRoot.querySelector(mainSelectors[i]);
    const p = priceFromEl(el);
    if (p != null) { salePrice = p; break; }
  }

  const retailSelectors = [
    '.productEstimatedTagNewRetail__retail',
    '.productDiscountInfo__retail',
    '[aria-label^="Original Price"]',
    '[aria-label*="Retail Price"]',
  ];
  for (let i = 0; i < retailSelectors.length; i++) {
    const el = priceRoot.querySelector(retailSelectors[i]);
    const p = priceFromEl(el);
    if (p != null) { retailPrice = p; break; }
  }

  let goodsName = null;
  const nameEl = document.querySelector(
    '.product-intro__head-name h1, .product-info h1.fsp-element, .product-info h1, h1.product-intro__head-name, h1'
  );
  if (nameEl) goodsName = (nameEl.textContent || '').replace(/\s+/g, ' ').trim();

  let supplierSku = null;
  const skuEl = document.querySelector('.product-intro__head-sku-text');
  if (skuEl) {
    const m = (skuEl.textContent || '').match(/SKU:\s*(\S+)/i);
    if (m) supplierSku = m[1].trim();
  }

  return {
    salePrice: salePrice != null && !isNaN(salePrice) ? salePrice : null,
    retailPrice: retailPrice != null && !isNaN(retailPrice) ? retailPrice : null,
    goodsName: goodsName || null,
    supplierSku: supplierSku || null,
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


def parse_shein_price_from_html(html: str) -> float | None:
    """Parse visible SHEIN sale price from saved PDP HTML."""
    if not html:
        return None
    for pattern in (
        r'id="productMainPriceId"[^>]*aria-label="R\s*([\d\s.,]+)"',
        r'class="productPrice__main"[^>]*aria-label="R\s*([\d\s.,]+)"',
    ):
        m = re.search(pattern, html, flags=re.I)
        if not m:
            continue
        raw = m.group(1).replace(" ", "").replace(",", ".")
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            continue
    return None


def parse_shein_variants_from_html(html: str) -> list[str]:
    """Parse size/style variant labels from saved SHEIN PDP HTML (for tests/offline checks)."""
    if not html:
        return []
    seen: set[str] = set()
    variants: list[str] = []

    def add(label: str) -> None:
        text = re.sub(r"\s+", " ", (label or "").strip())
        key = text.lower()
        if not text or len(text) < 2 or len(text) > 120 or key in seen:
            return
        seen.add(key)
        variants.append(text)

    style_labels = re.findall(
        r'class="radio-container[^"]*"[^>]*role="radio"[^>]*aria-label="([^"]+)"',
        html,
        flags=re.I,
    )
    if len(style_labels) > 1:
        for label in style_labels:
            add(label)

    for label in re.findall(r'data-attr_value_name="([^"]+)"', html, flags=re.I):
        add(label)

    if not variants:
        for label in re.findall(
            r'class="product-intro__size-radio-inner[^"]*"[^>]*>([^<]+)<',
            html,
            flags=re.I,
        ):
            add(label)

    return variants


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
    """Merge generic retail extract with SHEIN-specific price/title/variants."""
    wait_for_shein_pdp(page)
    data: dict[str, Any] = extract_generic_retail_product(page, debug=debug) or {}
    # Generic retail extract scans the whole page and often picks recommendation prices.
    data.pop("salePrice", None)

    try:
        shein = page.evaluate(SHEIN_PRICE_AND_META_JS)
    except Exception as exc:
        if debug:
            _LOG.exception("SHEIN price evaluate failed: %s", exc)
        shein = {}
    if isinstance(shein, dict):
        if shein.get("salePrice") is not None:
            data["salePrice"] = shein["salePrice"]
        if shein.get("goodsName"):
            data["goodsName"] = shein["goodsName"]
        sku = (shein.get("supplierSku") or "").strip()
        if sku:
            data["supplierSku"] = sku

    try:
        variant_data = page.evaluate(SHEIN_VARIANTS_JS)
    except Exception as exc:
        if debug:
            _LOG.exception("SHEIN variant evaluate failed: %s", exc)
        variant_data = {}
    if isinstance(variant_data, dict):
        variants = variant_data.get("variants") or []
        if variants:
            data["variants"] = variants
        selected = (variant_data.get("selectedVariant") or "").strip()
        if selected:
            data["selectedVariant"] = selected
        variant_size = (variant_data.get("variantSize") or "").strip()
        if variant_size and not data.get("variantSize"):
            data["variantSize"] = variant_size

    if data.get("goodsName") or data.get("salePrice") is not None:
        return data
    return None
