#!/usr/bin/env python3
"""
Unified product scraper app: Scrape, Edit, Upload.
Run: python app.py [--port 5001]
Open: http://127.0.0.1:5001
"""
import argparse
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request

# Ensure products/ is on path for supplier imports
PRODUCTS_ROOT = Path(__file__).resolve().parent
if str(PRODUCTS_ROOT) not in sys.path:
    sys.path.insert(0, str(PRODUCTS_ROOT))

load_dotenv(PRODUCTS_ROOT / ".env")

from shared.config import (
    load_scraper_config,
    save_scraper_config,
    get_supplier_delivery,
    save_supplier_delivery,
    get_tier_multipliers,
    save_supplier_tiers,
    SUPPLIERS_USING_TIERED_MARKUP,
)
from shared.suppliers import get_supplier, get_suppliers, run_supplier_scrape

logging.getLogger("werkzeug").setLevel(logging.ERROR)

# Scraper logger - visible in terminal when running app
LOG = logging.getLogger("products.scraper")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
if not LOG.handlers:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"))
    LOG.addHandler(h)

app = Flask(__name__)

# Scrape session state
scrape_stop_flag = threading.Event()
scrape_save_session_flag = threading.Event()
scrape_thread = None
scrape_running = False
scrape_supplier = None
scrape_options = {}

INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Product Scrapers</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #1a1a1a; color: #e0e0e0; }
    h1 { font-size: 1.5rem; margin-bottom: 0.5rem; }
    .sub { color: #888; margin-bottom: 2rem; }
    .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; }
    .card { display: block; padding: 1.5rem; background: #252525; border-radius: 8px; border: 1px solid #444;
      text-decoration: none; color: inherit; transition: background 0.2s, border-color 0.2s; }
    .card:hover { background: #2a2a2a; border-color: #2a7; }
    .card h2 { font-size: 1.1rem; margin: 0 0 0.5rem 0; }
    .card p { margin: 0; font-size: 0.9rem; color: #aaa; }
    .sources { margin-top: 2rem; }
    .sources h3 { font-size: 1rem; margin-bottom: 0.75rem; }
    .sources ul { list-style: none; padding: 0; margin: 0; }
    .sources li { margin-bottom: 0.5rem; }
    .sources code { background: #333; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.85rem; }
  </style>
</head>
<body>
  <h1>Product Scrapers</h1>
  <p class="sub">Scrape, edit, and upload products from Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly</p>

    <div class="cards">
    <a href="/scrape" class="card">
      <h2>Scrape</h2>
      <p>Select supplier (Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly) and start scraping</p>
    </a>
    <a href="/edit/" class="card">
      <h2>Edit Products</h2>
      <p>Edit name, price, cost, images for Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly</p>
    </a>
  </div>

  <div class="sources">
    <h3>CLI commands (run from products/ folder)</h3>
    <ul>
      <li><code>python -m products scrape temu</code> — <code>python -m products scrape temu --upload</code></li>
      <li><code>python -m products scrape gumtree</code> — <code>python -m products scrape gumtree --upload</code></li>
      <li><code>python -m products scrape aliexpress</code> — <code>python -m products scrape aliexpress --upload</code></li>
      <li><code>python -m products scrape ubuy</code> — <code>python -m products scrape ubuy --upload</code></li>
      <li><code>python -m products scrape myrunway</code> — <code>python -m products scrape myrunway --upload</code></li>
      <li><code>python -m products scrape onedayonly</code> — <code>python -m products scrape onedayonly --upload</code></li>
    </ul>
    <p style="margin-top: 1rem; color: #888; font-size: 0.9rem;">
      Upload to multiple companies: set <code>COMPANY_SLUGS=past-and-present,javamellow,plant-sanctuary</code> in .env, then <code>--upload --upload-to all</code>
    </p>
  </div>
</body>
</html>
"""

SCRAPE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Scrape - Product Scrapers</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #1a1a1a; color: #e0e0e0; }
    .top-nav { margin-bottom: 1.5rem; }
    .top-nav a { color: #2a7; text-decoration: none; }
    .top-nav a:hover { text-decoration: underline; }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    .field { margin-bottom: 1rem; }
    .field label { display: block; font-size: 0.85rem; color: #888; margin-bottom: 0.4rem; }
    select { padding: 0.6rem; background: #252525; border: 1px solid #444; border-radius: 6px; color: #e0e0e0; font-size: 1rem; min-width: 200px; }
    .controls { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0; }
    .controls button { padding: 0.8rem 1.25rem; font-size: 1rem; border: none; border-radius: 6px; cursor: pointer; }
    button.primary { background: #2a7; color: white; }
    button.primary:hover { background: #3b8; }
    button.danger { background: #c44; color: white; }
    button.danger:hover { background: #d55; }
    button.secondary { background: #444; color: #e0e0e0; }
    button.secondary:hover { background: #555; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    .status { margin: 1rem 0; padding: 1rem; background: #252525; border-radius: 8px; border: 1px solid #333; }
    .status.running { border-left: 4px solid #2a7; }
    .status.stopped { border-left: 4px solid #444; }
    .msg { margin-top: 0.5rem; font-size: 0.9rem; color: #888; }
    .help { font-size: 0.9rem; color: #888; line-height: 1.5; margin-top: 1rem; }
    .config-section { margin: 1.5rem 0; padding: 1rem; background: #252525; border-radius: 8px; border: 1px solid #333; }
    .config-section h3 { font-size: 1rem; margin: 0 0 0.75rem 0; color: #aaa; cursor: pointer; user-select: none; }
    .config-section h3:hover { color: #e0e0e0; }
    .config-section.collapsed .config-body { display: none; }
    .tier-row { display: flex; gap: 0.75rem; align-items: center; margin-bottom: 0.5rem; }
    .tier-row input { width: 90px; padding: 0.4rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; }
    .tier-row input[type="number"] { width: 70px; }
    .tier-row .hint { font-size: 0.8rem; color: #666; }
    .tier-row button.remove { padding: 0.2rem 0.5rem; font-size: 0.85rem; background: #444; color: #e0e0e0; border: none; border-radius: 4px; cursor: pointer; }
    .tier-row button.remove:hover { background: #c44; }
    .config-actions { margin-top: 0.75rem; display: flex; gap: 0.5rem; }
    .quicklink:hover { text-decoration: underline; }
    .delivery-section { margin: 1rem 0; padding: 1rem; background: #252525; border-radius: 8px; border: 1px solid #333; }
    .delivery-section h3 { font-size: 1rem; margin: 0 0 0.75rem 0; color: #aaa; cursor: pointer; user-select: none; }
    .delivery-section h3:hover { color: #e0e0e0; }
    .delivery-section .delivery-fields { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 1rem; }
    .delivery-section .field input { width: 100%; padding: 0.5rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; }
    .delivery-section .field input::placeholder { color: #666; }
    .delivery-section .save-delivery { margin-top: 0.75rem; padding: 0.4rem 0.8rem; font-size: 0.9rem; }
  </style>
</head>
<body>
  <div class="top-nav"><a href="/">← Dashboard</a></div>
  <h1>Scrape Products</h1>
  <div class="field">
    <label>Supplier</label>
    <div style="display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;">
      <select id="supplierSelect">
        <option value="">Loading...</option>
      </select>
      <a href="/edit/" id="editQuicklink" class="quicklink" style="font-size: 0.9rem; color: #2a7; text-decoration: none;">Edit products →</a>
    </div>
  </div>
  <div class="delivery-section config-section collapsed" id="deliverySection" style="display: none;">
    <h3 onclick="toggleDeliveryConfig()">▸ Supplier delivery details</h3>
    <div class="config-body">
      <p class="help" style="margin-bottom: 0.75rem;">Delivery time, cost, and free-delivery threshold. Editable; scraped when available.</p>
      <div class="delivery-fields">
        <div class="field">
          <label>Delivery time</label>
          <input type="text" id="deliveryTime" placeholder="e.g. 7-13 business days">
        </div>
        <div class="field">
          <label>Delivery cost (R)</label>
          <input type="number" id="deliveryCost" placeholder="0" step="0.01" min="0">
        </div>
        <div class="field">
          <label>Free delivery over (R)</label>
          <input type="number" id="freeDeliveryThreshold" placeholder="e.g. 200" step="0.01" min="0">
        </div>
      </div>
      <button type="button" class="secondary save-delivery" onclick="saveDeliveryConfig()">Save delivery</button>
    </div>
  </div>
  <div class="config-section collapsed" id="pricingConfig" style="display: none;">
    <h3 onclick="toggleConfig()">▸ Tiered markup (pricing tiers)</h3>
    <div class="config-body">
      <p class="help" style="margin-bottom: 0.75rem;">Cost tiers: if cost &lt; threshold (R), use multiplier. Last row: threshold empty = R200+.</p>
      <div id="tierRows"></div>
      <button type="button" class="secondary" onclick="addTierRow()" style="margin-top: 0.5rem; padding: 0.4rem 0.8rem;">+ Add tier</button>
      <div class="config-actions">
        <button type="button" class="secondary" onclick="savePricingConfig()">Save</button>
        <button type="button" class="secondary" onclick="resetPricingConfig()">Reset to defaults</button>
      </div>
    </div>
  </div>
  <div class="config-section collapsed" id="proxySection" style="display: none;">
    <h3 onclick="toggleProxyConfig()">▸ Proxy (optional)</h3>
    <div class="config-body">
      <p class="help" style="margin-bottom: 0.75rem;">Free proxies may be slow or fail. For testing only.</p>
      <div class="delivery-fields">
        <div class="field">
          <label><input type="checkbox" id="proxyEnabled"> Use proxy (change country)</label>
        </div>
        <div class="field">
          <label>Country</label>
          <select id="proxyCountry">
            <option value="ZA">South Africa</option>
            <option value="US">United States</option>
            <option value="GB">United Kingdom</option>
            <option value="DE">Germany</option>
            <option value="FR">France</option>
            <option value="AU">Australia</option>
            <option value="CA">Canada</option>
            <option value="NL">Netherlands</option>
            <option value="IN">India</option>
            <option value="IT">Italy</option>
            <option value="ES">Spain</option>
            <option value="JP">Japan</option>
            <option value="BR">Brazil</option>
            <option value="PL">Poland</option>
            <option value="MX">Mexico</option>
          </select>
        </div>
      </div>
    </div>
  </div>
  <div class="controls">
    <button id="startBtn" class="primary" onclick="startScrape()">Start scrape</button>
    <button id="stopBtn" class="danger" onclick="stopScrape()" disabled>Stop scrape</button>
    <button id="saveSessionBtn" class="secondary" onclick="saveSession()" disabled>Save session (after login)</button>
  </div>
  <div id="status" class="status stopped">
    <span id="statusText">Stopped</span>
    <div id="msg" class="msg"></div>
  </div>
  <p class="help" id="helpText">
    Select a supplier above, then click Start scrape. A browser will open. For Temu/Gumtree: browse and click the floating Save button to add products. For AliExpress: add URLs to urls.txt first, then start. If Temu pages stop loading: set SCRAPER_DEBUG=1 and restart – failed pages are saved to scraped/debug_html/.
  </p>
  <script>
    let suppliers = [];
    async function loadSuppliers() {
      const r = await fetch('/api/suppliers');
      suppliers = await r.json();
      const sel = document.getElementById('supplierSelect');
      sel.innerHTML = '<option value="">Select supplier</option>' + suppliers.map(s => 
        '<option value="' + s.slug + '">' + s.display_name + (s.supports_interactive ? ' (browse & save)' : ' (URL list)') + '</option>'
      ).join('');
    }
    function setStatus(running, text) {
      const el = document.getElementById('status');
      const txt = document.getElementById('statusText');
      const startBtn = document.getElementById('startBtn');
      const stopBtn = document.getElementById('stopBtn');
      const saveBtn = document.getElementById('saveSessionBtn');
      el.className = 'status ' + (running ? 'running' : 'stopped');
      txt.textContent = text || (running ? 'Running' : 'Stopped');
      startBtn.disabled = running;
      stopBtn.disabled = !running;
      const supplier = suppliers.find(s => s.slug === document.getElementById('supplierSelect').value);
      saveBtn.disabled = !running || !supplier || !supplier.supports_interactive;
    }
    function setMsg(msg) { document.getElementById('msg').textContent = msg || ''; }
    async function startScrape() {
      const slug = document.getElementById('supplierSelect').value;
      if (!slug) { setMsg('Select a supplier first.'); return; }
      setMsg('');
      const proxyEnabled = document.getElementById('proxyEnabled') && document.getElementById('proxyEnabled').checked;
      const proxyCountry = (document.getElementById('proxyCountry') && document.getElementById('proxyCountry').value) || 'ZA';
      const res = await fetch('/api/scrape/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ supplier: slug, proxy_enabled: !!proxyEnabled, proxy_country: proxyCountry })
      });
      const data = await res.json();
      if (data.ok) { setStatus(true, 'Running'); }
      else { setMsg(data.error || 'Failed to start'); }
    }
    async function stopScrape() {
      setMsg('');
      await fetch('/api/scrape/stop', { method: 'POST' });
      setStatus(false, 'Stopping...');
      setTimeout(pollStatus, 500);
    }
    async function saveSession() {
      setMsg('');
      const res = await fetch('/api/scrape/save-session', { method: 'POST' });
      const data = await res.json();
      if (data.ok) setMsg('Session saved.');
      else setMsg(data.error || 'Failed');
    }
    async function pollStatus() {
      const res = await fetch('/api/scrape/status');
      const data = await res.json();
      setStatus(data.running, data.running ? 'Running' : 'Stopped');
      if (data.running) setTimeout(pollStatus, 2000);
    }
    function toggleConfig() {
      document.getElementById('pricingConfig').classList.toggle('collapsed');
      const h3 = document.querySelector('#pricingConfig h3');
      h3.textContent = document.getElementById('pricingConfig').classList.contains('collapsed') ? '▸ Tiered markup (pricing tiers)' : '▾ Tiered markup (pricing tiers)';
    }
    function toggleDeliveryConfig() {
      document.getElementById('deliverySection').classList.toggle('collapsed');
      const h3 = document.querySelector('#deliverySection h3');
      h3.textContent = document.getElementById('deliverySection').classList.contains('collapsed') ? '▸ Supplier delivery details' : '▾ Supplier delivery details';
    }
    function toggleProxyConfig() {
      document.getElementById('proxySection').classList.toggle('collapsed');
      const h3 = document.querySelector('#proxySection h3');
      h3.textContent = document.getElementById('proxySection').classList.contains('collapsed') ? '▸ Proxy (optional)' : '▾ Proxy (optional)';
    }
    function addTierRow(threshold = '', multiplier = '1.5') {
      const div = document.getElementById('tierRows');
      const row = document.createElement('div');
      row.className = 'tier-row';
      row.innerHTML = '<input type="number" placeholder="R max" step="1" min="0" value="' + (threshold === null ? '' : threshold) + '">' +
        '<input type="number" placeholder="mult" step="0.01" min="0.1" value="' + multiplier + '">' +
        '<span class="hint">× multiplier</span>' +
        '<button type="button" class="remove" onclick="this.parentElement.remove()">Remove</button>';
      div.appendChild(row);
    }
    async function loadPricingConfig(slug) {
      if (!slug) return;
      const r = await fetch('/api/scraper-config?supplier=' + encodeURIComponent(slug));
      const data = await r.json();
      const div = document.getElementById('tierRows');
      div.innerHTML = '';
      const tiers = data.tier_multipliers || [];
      if (tiers.length === 0) {
        resetPricingConfig();
        return;
      }
      tiers.forEach(t => {
        addTierRow(t.threshold == null ? null : t.threshold, t.multiplier);
      });
    }
    async function savePricingConfig() {
      const slug = document.getElementById('supplierSelect').value;
      if (!slug) return;
      const rows = document.querySelectorAll('#tierRows .tier-row');
      const tiers = [];
      rows.forEach(row => {
        const inputs = row.querySelectorAll('input');
        const th = inputs[0].value.trim();
        const mult = parseFloat(inputs[1].value);
        if (!isNaN(mult)) {
          tiers.push({ threshold: th === '' ? null : parseFloat(th), multiplier: mult });
        }
      });
      const res = await fetch('/api/scraper-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ supplier: slug, tier_multipliers: tiers })
      });
      const data = await res.json();
      if (data.ok) document.getElementById('msg').textContent = 'Pricing config saved.';
      else document.getElementById('msg').textContent = data.error || 'Save failed.';
    }
    function resetPricingConfig() {
      document.getElementById('tierRows').innerHTML = '';
      addTierRow(30, 3.5);
      addTierRow(99, 3.0);
      addTierRow(199, 2.25);
      addTierRow(null, 1.5);
    }
    function updateEditQuicklink() {
      const slug = document.getElementById('supplierSelect').value;
      const link = document.getElementById('editQuicklink');
      if (link) {
        link.href = slug ? '/edit/?source=' + encodeURIComponent(slug) : '/edit/';
        link.textContent = slug ? 'Edit ' + (suppliers.find(s => s.slug === slug)?.display_name || slug) + ' products →' : 'Edit products →';
      }
      const deliverySection = document.getElementById('deliverySection');
      const pricingSection = document.getElementById('pricingConfig');
      const proxySection = document.getElementById('proxySection');
      if (deliverySection) deliverySection.style.display = slug ? 'block' : 'none';
      if (pricingSection) pricingSection.style.display = slug ? 'block' : 'none';
      if (proxySection) proxySection.style.display = slug ? 'block' : 'none';
      if (slug) {
        loadDeliveryConfig(slug);
        loadPricingConfig(slug);
      }
    }
    async function loadDeliveryConfig(slug) {
      if (!slug) return;
      const r = await fetch('/api/supplier-delivery?supplier=' + encodeURIComponent(slug));
      const d = await r.json();
      const timeEl = document.getElementById('deliveryTime');
      const costEl = document.getElementById('deliveryCost');
      const threshEl = document.getElementById('freeDeliveryThreshold');
      if (timeEl) timeEl.value = d.delivery_time || '';
      if (costEl) costEl.value = d.delivery_cost !== undefined && d.delivery_cost !== null ? d.delivery_cost : '';
      if (threshEl) threshEl.value = d.free_delivery_threshold !== undefined && d.free_delivery_threshold !== null ? d.free_delivery_threshold : '';
    }
    async function saveDeliveryConfig() {
      const slug = document.getElementById('supplierSelect').value;
      if (!slug) return;
      const r = await fetch('/api/supplier-delivery', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          supplier: slug,
          delivery_time: document.getElementById('deliveryTime').value.trim(),
          delivery_cost: document.getElementById('deliveryCost').value.trim() ? parseFloat(document.getElementById('deliveryCost').value) : null,
          free_delivery_threshold: document.getElementById('freeDeliveryThreshold').value.trim() ? parseFloat(document.getElementById('freeDeliveryThreshold').value) : null
        })
      });
      const data = await r.json();
      document.getElementById('msg').textContent = data.ok ? 'Delivery config saved.' : (data.error || 'Save failed.');
    }
    loadSuppliers().then(() => {
      pollStatus();
      document.getElementById('supplierSelect').addEventListener('change', updateEditQuicklink);
      updateEditQuicklink();
    });
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/scrape")
def scrape_page():
    return render_template_string(SCRAPE_HTML)


@app.route("/api/suppliers")
def api_suppliers():
    return jsonify(get_suppliers())


@app.route("/api/supplier-delivery", methods=["GET"])
def api_supplier_delivery_get():
    """Get delivery config for a supplier."""
    slug = (request.args.get("supplier") or "").strip()
    if not slug:
        return jsonify({})
    return jsonify(get_supplier_delivery(slug))


@app.route("/api/supplier-delivery", methods=["POST"])
def api_supplier_delivery_post():
    """Save delivery config for a supplier."""
    data = request.get_json(silent=True) or {}
    slug = (data.get("supplier") or "").strip()
    if not slug:
        return jsonify({"ok": False, "error": "supplier required"})
    save_supplier_delivery(slug, data)
    return jsonify({"ok": True})


@app.route("/api/scraper-config", methods=["GET"])
def api_scraper_config_get():
    slug = (request.args.get("supplier") or "").strip()
    if slug:
        cfg = load_scraper_config()
        supplier_tiers = cfg.get("supplier_tiers") or {}
        raw = supplier_tiers.get(slug)
        if raw and isinstance(raw, list):
            return jsonify({"tier_multipliers": raw})
        return jsonify({"tier_multipliers": []})
    return jsonify(load_scraper_config())


@app.route("/api/scraper-config", methods=["POST"])
def api_scraper_config_post():
    data = request.get_json(silent=True) or {}
    tiers = data.get("tier_multipliers")
    supplier = (data.get("supplier") or "").strip()
    if not isinstance(tiers, list):
        return jsonify({"ok": False, "error": "tier_multipliers must be a list"})
    if not supplier:
        return jsonify({"ok": False, "error": "supplier required for tier config"})
    # Validate and normalize
    normalized = []
    for t in tiers:
        if not isinstance(t, dict):
            continue
        th = t.get("threshold")
        mult = t.get("multiplier", 1.5)
        try:
            mult = float(mult)
        except (TypeError, ValueError):
            mult = 1.5
        if th is None:
            normalized.append({"threshold": None, "multiplier": mult})
        else:
            try:
                normalized.append({"threshold": float(th), "multiplier": mult})
            except (TypeError, ValueError):
                continue
    if not normalized:
        return jsonify({"ok": False, "error": "At least one tier required"})
    save_supplier_tiers(supplier, normalized)
    return jsonify({"ok": True})


@app.route("/api/scrape/status")
def api_scrape_status():
    info = get_supplier(scrape_supplier) if scrape_supplier else None
    has_session = False
    if info and info.supports_interactive:
        session_files = {
            "temu": PRODUCTS_ROOT / "temu/temu_session.json",
            "gumtree": PRODUCTS_ROOT / "gumtree/gumtree_session.json",
            "ubuy": PRODUCTS_ROOT / "ubuy/ubuy_session.json",
        }
        has_session = session_files.get(scrape_supplier, Path()).exists()
    return jsonify({"running": scrape_running, "supplier": scrape_supplier, "has_session": has_session})


@app.route("/api/scrape/start", methods=["POST"])
def api_scrape_start():
    global scrape_thread, scrape_running, scrape_supplier, scrape_options
    if scrape_running:
        LOG.warning("Scrape start rejected: already running")
        return jsonify({"ok": False, "error": "Scrape already running"})
    data = request.get_json(silent=True) or {}
    slug = (data.get("supplier") or "").strip()
    if not slug:
        LOG.warning("Scrape start rejected: no supplier selected")
        return jsonify({"ok": False, "error": "Select a supplier"})
    info = get_supplier(slug)
    if not info:
        LOG.warning("Scrape start rejected: unknown supplier %s", slug)
        return jsonify({"ok": False, "error": f"Unknown supplier: {slug}"})

    # Suppliers that use tiered markup must have tiers configured (no fallback)
    if slug in SUPPLIERS_USING_TIERED_MARKUP:
        if not get_tier_multipliers(slug):
            return jsonify({
                "ok": False,
                "error": "Configure pricing tiers for this supplier first. Expand the Tiered markup section, add tiers, and Save.",
            })

    scrape_options = {
        "proxy_enabled": bool(data.get("proxy_enabled")),
        "proxy_country": (data.get("proxy_country") or "ZA").strip().upper()[:2],
        "proxy_server": None,
    }
    if scrape_options["proxy_enabled"] and scrape_options["proxy_country"]:
        from shared.proxy_utils import fetch_free_proxy
        proxy_server = fetch_free_proxy(scrape_options["proxy_country"])
        if proxy_server:
            scrape_options["proxy_server"] = proxy_server
            LOG.info("Using proxy for %s: %s", scrape_options["proxy_country"], proxy_server)
        else:
            LOG.warning("No proxy found for %s, continuing without proxy", scrape_options["proxy_country"])

    scrape_stop_flag.clear()
    scrape_save_session_flag.clear()
    scrape_supplier = slug
    scrape_running = True

    def _run():
        global scrape_running
        try:
            LOG.info("Starting scrape: %s -> %s", slug, info.output_dir)
            run_supplier_scrape(slug, info.output_dir, scrape_stop_flag, scrape_save_session_flag, scrape_options)
            LOG.info("Scrape finished: %s", slug)
        except Exception as e:
            LOG.exception("Scrape error for %s: %s", slug, e)
        finally:
            scrape_running = False

    scrape_thread = threading.Thread(target=_run, daemon=True)
    scrape_thread.start()
    LOG.info("Scrape thread started for %s", slug)
    return jsonify({"ok": True})


@app.route("/api/scrape/stop", methods=["POST"])
def api_scrape_stop():
    scrape_stop_flag.set()
    return jsonify({"ok": True})


@app.route("/api/scrape/save-session", methods=["POST"])
def api_scrape_save_session():
    if not scrape_running:
        return jsonify({"ok": False, "error": "No scrape session running"})
    scrape_save_session_flag.set()
    return jsonify({"ok": True})


# Register edit blueprint at /edit
from edit_products import create_edit_blueprint

app.register_blueprint(create_edit_blueprint(), url_prefix="/edit")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--open", action="store_true", help="Open browser")
    args = parser.parse_args()
    url = f"http://127.0.0.1:{args.port}"
    print(f"Product scrapers: {url}")
    if args.open:
        webbrowser.open(url)
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
