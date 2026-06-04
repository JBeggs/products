"""
Generic product extraction from a retail PDP in the browser.

Targets JSON-LD (schema.org Product / ProductGroup), Open Graph, microdata,
and light DOM fallbacks. Tuned for South African sites (R … prices).
"""
from __future__ import annotations

import logging
import re
from typing import Any

_LOG = logging.getLogger("products.scraper")

EXTRACT_JS = r"""
() => {
  /** ZA shops often use comma as decimal separator (e.g. R240,00). Do not strip comma before decimals. */
  function parseSouthAfricanRand(text) {
    if (!text) return null;
    const t = String(text).replace(/\u00a0/g, ' ').trim();
    const m = t.match(/R\s*([\d\s\u202f\.]+(?:[,\.]\d{2})?)/i) || t.match(/ZAR\s*([\d\s\.]+(?:[,\.]\d{2})?)/i);
    if (!m) return null;
    let raw = m[1].replace(/\s/g, '').trim();
    if (!raw.length) return null;
    /** If last comma/dot is decimal (x,yy or x.yy with 2 digits) treat as decimal. */
    const decComma = /^(\d+),(\d{2})$/;
    const decDot = /^(\d+)\.(\d{2})$/;
    let n;
    if (decComma.test(raw)) {
      const p = raw.match(decComma);
      n = parseFloat(p[1] + '.' + p[2]);
    } else if (decDot.test(raw)) {
      n = parseFloat(raw);
    } else {
      raw = raw.replace(',', '.');
      n = parseFloat(raw.replace(/\s/g, ''));
    }
    if (!isNaN(n) && n >= 0 && n < 10000000) return n;
    return null;
  }

  function parsePriceText(text) {
    if (!text) return null;
    const za = parseSouthAfricanRand(text);
    if (za != null) return za;
    const t = String(text).replace(/\u00a0/g, ' ').trim();
    const m = t.match(/R\s*([\d\s,]+(?:\.\d{1,2})?)/i) || t.match(/ZAR\s*([\d\s,]+(?:\.\d{1,2})?)/i);
    if (m) return parseFloat(m[1].replace(/[\s,]/g, ''));
    const m2 = t.match(/([\d]{1,3}(?:[\s,]\d{3})*(?:\.\d{1,2})?)/);
    if (m2) {
      const n = parseFloat(m2[1].replace(/[\s,]/g, ''));
      if (!isNaN(n) && n > 0 && n < 10000000) return n;
    }
    return null;
  }

  const gallery = [];
  const seen = new Set();
  function addImg(src) {
    if (!src || typeof src !== 'string') return;
    let u = src.trim();
    if (u.startsWith('//')) u = 'https:' + u;
    if (!u.startsWith('http')) return;
    if (u.includes('data:image') || u.toLowerCase().includes('pixel') || u.toLowerCase().endsWith('.svg')) return;
    const base = u.split('?')[0];
    if (seen.has(base)) return;
    seen.add(base);
    gallery.push(u.split('?')[0] === base ? base : u);
  }

  let goodsName = null;
  let salePrice = null;
  let desc = null;

  const scripts = document.querySelectorAll('script[type="application/ld+json"]');
  for (const s of scripts) {
    let raw = (s.textContent || '').trim();
    if (!raw) continue;
    try {
      let j = JSON.parse(raw);
      const queue = Array.isArray(j) ? [...j] : [j];
      while (queue.length) {
        const o = queue.pop();
        if (!o || typeof o !== 'object') continue;
        if (o['@graph']) {
          const g = o['@graph'];
          if (Array.isArray(g)) queue.push(...g);
          else queue.push(g);
          continue;
        }
        const t = o['@type'];
        const types = Array.isArray(t) ? t : (t ? [t] : []);
        const isProduct = types.some((x) => x === 'Product' || x === 'ProductGroup' || x === 'IndividualProduct');
        if (isProduct) {
          if (o.name && !goodsName) goodsName = String(o.name).trim();
          if (o.description && !desc) {
            desc = String(o.description).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 4000);
          }
          const img = o.image;
          if (img) {
            if (typeof img === 'string') addImg(img);
            else if (Array.isArray(img)) img.forEach((x) => { if (typeof x === 'string') addImg(x); else if (x && x.url) addImg(x.url); });
            else if (img.url) addImg(img.url);
          }
          const off = o.offers || o.aggregateOffer;
          if (off && salePrice == null) {
            const list = Array.isArray(off) ? off : (off.offers ? (Array.isArray(off.offers) ? off.offers : [off.offers]) : [off]);
            for (const ofr of list) {
              if (!ofr || typeof ofr !== 'object') continue;
              const p = ofr.price || ofr.lowPrice || ofr.highPrice;
              if (p != null) {
                const n = parseFloat(String(p).replace(/,/g, ''));
                if (!isNaN(n)) { salePrice = n; break; }
              }
              const pt = ofr.priceSpecification && ofr.priceSpecification.price;
              if (pt != null) {
                const n = parseFloat(String(pt).replace(/,/g, ''));
                if (!isNaN(n)) { salePrice = n; break; }
              }
            }
          }
        }
      }
    } catch (e) { /* ignore bad JSON-LD */ }
  }

  /** Agrimark / Vue PDPs: schema.org Offer often matches a base SKU; trust the visible cart price. */
  let variantSize = null;
  let supplierSku = null;
  let weightText = null;
  let dimensionsText = null;
  const atcRoot = document.querySelector('.add-to-cart-container .add-to-cart');
  if (atcRoot) {
    const ph2 = atcRoot.querySelector('h2');
    if (ph2) {
      const vis = parseSouthAfricanRand(ph2.textContent || '');
      if (vis != null) salePrice = vis;
    }
    const vsel = atcRoot.querySelector('.vs__selected');
    if (vsel) variantSize = (vsel.textContent || '').replace(/\s+/g, ' ').trim();
    const skuCand = atcRoot.querySelector('form span.text-muted');
    if (skuCand) supplierSku = (skuCand.textContent || '').replace(/\s+/g, ' ').trim();
  }
  const specTable =
    document.querySelector('.product-info-container table.tech-specs') ||
    document.querySelector('table.tech-specs');
  if (specTable) {
    for (const tr of specTable.querySelectorAll('tr')) {
      const tds = tr.querySelectorAll('td');
      if (tds.length < 2) continue;
      const title = (tds[0].textContent || '').replace(/\s+/g, ' ').trim();
      const val = (tds[tds.length - 1].textContent || '').replace(/\s+/g, ' ').trim();
      if (/weight/i.test(title)) weightText = val;
      if (/dimension/i.test(title)) dimensionsText = val;
    }
  }

  const ogT = document.querySelector('meta[property="og:title"]');
  if (!goodsName && ogT) goodsName = (ogT.getAttribute('content') || '').trim();
  const ogD = document.querySelector('meta[property="og:description"]');
  if (!desc && ogD) desc = (ogD.getAttribute('content') || '').trim();
  const ogImg = document.querySelector('meta[property="og:image"]');
  if (ogImg) addImg(ogImg.getAttribute('content') || '');

  const wcForm =
    document.querySelector('form.variations_form.cart') ||
    document.querySelector('form.cart.variations_form');
  const wcVariationData = wcForm && wcForm.getAttribute('data-product_variations');
  let wcVariationMainImg = null;

  /** Shopify themes (e.g. AHM): full gallery in ProductJson script; JSON-LD often has only one image. */
  try {
    const pj = document.querySelector('script[id^="ProductJson-"]');
    if (pj && pj.textContent) {
      const pjdata = JSON.parse(pj.textContent);
      const info = pjdata.product_info || pjdata;
      if (info && Array.isArray(info.images)) {
        info.images.forEach(function (x) {
          if (typeof x === 'string') addImg(x);
        });
      }
      if (info && Array.isArray(info.media)) {
        for (let mi = 0; mi < info.media.length; mi++) {
          const m = info.media[mi];
          if (!m || m.media_type !== 'image') continue;
          if (m.src) addImg(m.src);
          if (m.preview_image && m.preview_image.src) addImg(m.preview_image.src);
        }
      }
    }
  } catch (e) { /* ignore */ }

  const titleEl =
    document.querySelector('h1.product_title') ||
    document.querySelector('.product_title.entry-title') ||
    document.querySelector('h1.summary .product-title') ||
    document.querySelector('.product_title');
  if (!goodsName && titleEl) goodsName = (titleEl.textContent || titleEl.innerText || '').trim();
  if (!goodsName && wcForm) {
    const near = wcForm.closest('.elementor-element.e-con-inner, .summary.entry-summary, .product, article');
    const h2near = near && near.querySelector('h2.elementor-heading-title');
    if (h2near) goodsName = (h2near.textContent || h2near.innerText || '').trim();
  }
  if (!goodsName) {
    const h2el = document.querySelector('h2.elementor-heading-title');
    if (h2el) goodsName = (h2el.textContent || h2el.innerText || '').trim();
  }
  const h1 = document.querySelector('h1');
  if (!goodsName && h1) goodsName = (h1.textContent || h1.innerText || '').trim();

  /** Meta/itemprop prices on Woo variable PDPs often match min/max range, not the selected variation. */
  const pm = document.querySelector('meta[itemprop="price"]');
  if (pm && salePrice == null && !wcVariationData) {
    const c = pm.getAttribute('content');
    if (c) {
      const n = parseFloat(String(c).replace(/,/g, ''));
      if (!isNaN(n)) salePrice = n;
    }
  }

  if (wcForm && wcVariationData) {
    /** JSON-LD aggregateOffer/lowPrice on variable PDPs is often the range min (e.g. R95), not the selected SKU. */
    salePrice = null;
    const vpbdi = document.querySelector('.woocommerce-variation-price .woocommerce-Price-amount bdi');
    if (vpbdi) {
      const vFromDom = parseSouthAfricanRand(vpbdi.textContent || '');
      if (vFromDom != null) salePrice = vFromDom;
    }
    if (salePrice == null) {
      const vPriceEl =
        document.querySelector('.woocommerce-variation-price .price') ||
        document.querySelector('.single_variation_wrap .price');
      if (vPriceEl) {
        const vfd = parseSouthAfricanRand(vPriceEl.textContent || '');
        if (vfd != null) salePrice = vfd;
      }
    }
    try {
      const variants = JSON.parse(wcVariationData);
      if (Array.isArray(variants) && variants.length) {
        let vid = 0;
        const vidInp = wcForm.querySelector('input.variation_id');
        if (vidInp && vidInp.value) vid = parseInt(String(vidInp.value), 10) || 0;
        let chosen = vid ? variants.find(function (v) { return v && Number(v.variation_id) === vid; }) : null;
        if (!chosen) {
          const instock = variants.filter(function (v) { return v && v.is_in_stock; });
          const pool = instock.length ? instock : variants;
          pool.sort(function (a, b) { return (Number(a.display_price) || 0) - (Number(b.display_price) || 0); });
          chosen = pool[0];
        }
        if (chosen) {
          if (salePrice == null && chosen.display_price != null) {
            const n = Number(chosen.display_price);
            if (!isNaN(n)) salePrice = n;
          }
          if (chosen.image) {
            const im = chosen.image;
            const u = im.full_src || im.url || im.src;
            if (u && typeof u === 'string') wcVariationMainImg = u.trim().split('?')[0];
          }
        }
      }
    } catch (e) { /* ignore bad JSON */ }
  }

  if (salePrice == null && !wcVariationData) {
    const insBdi = document.querySelector('p.price ins .woocommerce-Price-amount bdi, p.price ins .woocommerce-Price-amount');
    if (insBdi) {
      const insPrice = parseSouthAfricanRand(insBdi.textContent || '');
      if (insPrice != null) salePrice = insPrice;
    }
    const cand = document.querySelector('[itemprop="price"], [data-product-price], [data-price], .price, .product-price, .ProductPrice');
    if (cand) {
      const pv = parsePriceText(cand.textContent || cand.getAttribute('content') || '');
      if (pv != null) salePrice = pv;
    }
  }

  const wcGallery = document.querySelector('.woocommerce-product-gallery');
  if (wcGallery) {
    wcGallery.querySelectorAll('[data-large_image]').forEach(function (el) {
      addImg(el.getAttribute('data-large_image'));
    });
    wcGallery.querySelectorAll('a[href*="/wp-content/uploads/"]').forEach(function (a) {
      addImg(a.getAttribute('href'));
    });
  }

  /** Prefer the main product section only; <main> often wraps recommendations. */
  const pdpRoot =
    document.querySelector('[data-section-type="product_page"]') ||
    document.querySelector('.product-container.product-id') ||
    document.querySelector('main#content.product-page, main.product-page') ||
    document.body;
  const imgs = pdpRoot.querySelectorAll('img[src], img[data-src], img[data-source]');
  for (const img of imgs) {
    const src = (
      img.getAttribute('data-source') ||
      img.getAttribute('data-src') ||
      img.getAttribute('src') ||
      ''
    ).trim();
    if (!src || src.startsWith('data:')) continue;
    const w = img.naturalWidth || img.width || 0;
    if (w > 0 && w < 40) continue;
    addImg(src);
  }

  function decodeHtmlEntities(html) {
    const ta = document.createElement('textarea');
    ta.innerHTML = html;
    return ta.value;
  }
  /** Prefer visible RTE (Shopify + Smart Tabs app) over JSON-LD, which strips tags to one line. */
  function richDescriptionFromDom() {
    const pickers = [
      () => document.querySelector('.elementor-widget-woocommerce-product-content .elementor-widget-container'),
      () => document.querySelector('.woocommerce-product-details__short-description'),
      () => document.querySelector('.smart-tabs-content-block-active'),
      () => document.querySelector('.product__description .rte'),
      () => document.querySelector('.product__description'),
      () => document.querySelector('[id*="product-description"] .rte'),
    ];
    for (let pi = 0; pi < pickers.length; pi++) {
      const el = pickers[pi]();
      if (!el) continue;
      const clone = el.cloneNode(true);
      clone.querySelectorAll(
        '.smart-tabs-branding, .smart-tabs-navigation-wrapper, .smart-tabs-navigation-links, script, style, noscript'
      ).forEach(function (n) { n.remove(); });
      const innerTxt = (clone.innerText || clone.textContent || '').replace(/\s+/g, ' ').trim();
      if (innerTxt.length < 50) continue;
      let h = clone.innerHTML;
      h = h.replace(/<br\s*\/?>/gi, '{{NL}}');
      h = h.replace(/<\/(p|li|h[1-6]|tr)>/gi, '{{NL}}');
      h = h.replace(/<[^>]+>/g, ' ');
      let t = decodeHtmlEntities(h).split('{{NL}}').join('\n');
      t = t.replace(/\u00a0/g, ' ').replace(/[ \t\f\v]+/g, ' ');
      t = t.replace(/ *\n */g, '\n');
      t = t.replace(/\n{3,}/g, '\n\n').trim();
      if (t.length >= 80) return t.slice(0, 8000);
    }
    return null;
  }
  const domRich = richDescriptionFromDom();
  if (domRich) {
    const flatLen = (desc || '').replace(/\s+/g, ' ').trim().length;
    const domLines = domRich.split(/\n/).filter(function (x) { return x.trim().length; }).length;
    if (!desc || domRich.length >= flatLen * 0.72 || domLines >= 4) desc = domRich;
  }

  if (!desc) desc = goodsName;
  /** Put the selected variation hero first when gallery defaults to another SKU (mixed data-o_* on The7). */
  if (wcVariationMainImg) {
    const b = wcVariationMainImg.split('?')[0];
    for (let gi = gallery.length - 1; gi >= 0; gi--) {
      const gb = String(gallery[gi] || '').split('?')[0];
      if (gb === b) gallery.splice(gi, 1);
    }
    gallery.unshift(b);
  }
  /** Agrimark: main image reflects the selected pack size; JSON-LD image may be another variant. */
  const mainHero =
    document.querySelector('.product-images-container img.product-image-toggle') ||
    document.querySelector('.product-image-container img.product-image-toggle');
  if (mainHero) {
    let u = (mainHero.getAttribute('src') || '').trim();
    if (u.startsWith('//')) u = 'https:' + u;
    if (u.startsWith('http')) {
      const b = u.split('?')[0];
      for (let gi = gallery.length - 1; gi >= 0; gi--) {
        const gb = String(gallery[gi] || '').split('?')[0];
        if (gb === b) gallery.splice(gi, 1);
      }
      gallery.unshift(b);
    }
  }
  return {
    goodsName: goodsName || null,
    salePrice: salePrice != null && !isNaN(salePrice) ? salePrice : null,
    gallery: gallery.slice(0, 20),
    desc: desc || null,
    variantSize: variantSize || null,
    supplierSku: supplierSku || null,
    weightText: weightText || null,
    dimensionsText: dimensionsText || null,
  };
}
"""


def extract_generic_retail_product(page, debug: bool = False) -> dict[str, Any] | None:
    """Run browser-side extraction. Returns dict with goodsName, salePrice, gallery, desc or None."""
    try:
        data = page.evaluate(EXTRACT_JS)
        if isinstance(data, dict) and (data.get("goodsName") or data.get("salePrice") is not None):
            return data
    except Exception as e:
        if debug:
            _LOG.exception("extract_generic_retail_product evaluate failed: %s", e)
    return None


def goods_id_from_url(url: str) -> str:
    """Stable-enough id from URL path for tagging."""
    from urllib.parse import urlparse

    p = urlparse(url)
    parts = [x for x in (p.path or "").split("/") if x]
    if parts:
        tail = parts[-1].split("?")[0]
        tail = re.sub(r"[^\w\-]+", "-", tail).strip("-")[:80]
        if tail:
            return tail
    return "unknown"
