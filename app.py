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
from flask import Flask, jsonify, render_template_string, request, send_from_directory

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
LOG_LEVEL = os.environ.get("LOG_LEVEL", "").upper() or (
    "DEBUG" if os.environ.get("SCRAPER_DEBUG", "").lower() in ("1", "true", "yes") else "INFO"
)
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

# Yoco session state (browser for refund login)
yoco_running = False
yoco_close_flag = threading.Event()
yoco_thread = None

# Order-runner state: manual supplier-order workflow per (company, order_id, supplier)
# Key: (company_slug, order_id, supplier_slug). Value: {items, current_index, state, order_number}
_order_run_state: dict = {}
_order_run_lock = threading.Lock()

# Courier workflow state per (company, order_id, supplier)
# Value: {goods_arrived: bool, quote: dict|None, order_placed: dict|None}
_courier_state: dict = {}
_courier_lock = threading.Lock()

# Gumtree crawler state
_gumtree_crawler_running = False
_gumtree_crawler_thread = None
_gumtree_crawler_lock = threading.Lock()
GUMTREE_CRAWLER_SCHEDULER_ENABLED = os.environ.get("GUMTREE_CRAWLER_SCHEDULER", "").lower() in ("1", "true", "yes")

# Makro crawler state
_makro_crawler_running = False
_makro_crawler_thread = None
_makro_crawler_lock = threading.Lock()
MAKRO_CRAWLER_SCHEDULER_ENABLED = os.environ.get("MAKRO_CRAWLER_SCHEDULER", "").lower() in ("1", "true", "yes")


def _order_run_key(company: str, order_id: str, supplier: str) -> tuple:
    return (company.strip(), str(order_id).strip(), supplier.strip().lower())


def _get_order_run(company: str, order_id: str, supplier: str) -> dict | None:
    with _order_run_lock:
        return _order_run_state.get(_order_run_key(company, order_id, supplier))


def _set_order_run(company: str, order_id: str, supplier: str, data: dict) -> None:
    with _order_run_lock:
        _order_run_state[_order_run_key(company, order_id, supplier)] = data


def _clear_order_run(company: str, order_id: str, supplier: str) -> None:
    with _order_run_lock:
        _order_run_state.pop(_order_run_key(company, order_id, supplier), None)

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
    .company-bar { margin-bottom: 1.5rem; display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
    .company-bar label { font-size: 0.9rem; color: #888; }
    .company-bar select { padding: 0.5rem 0.75rem; background: #252525; border: 1px solid #444; border-radius: 6px; color: #e0e0e0; min-width: 180px; }
  </style>
</head>
<body>
  <h1>Product Scrapers</h1>
  <p class="sub">Scrape, edit, and upload products from Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly</p>
  <div class="company-bar">
    <label for="companySelect">Company</label>
    <select id="companySelect"><option value="">Loading...</option></select>
  </div>
    <div class="cards">
    <a href="/scrape" class="card">
      <h2>Scrape</h2>
      <p>Select supplier (Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly) and start scraping</p>
    </a>
    <a href="/edit/" class="card">
      <h2>Edit Products</h2>
      <p>Edit name, price, cost, images for Temu, Gumtree, AliExpress, Ubuy, MyRunway, OneDayOnly</p>
    </a>
    <a href="/orders" class="card">
      <h2>Orders</h2>
      <p>View orders from backend, grouped by customer and supplier</p>
    </a>
  </div>
  <div class="cards" style="margin-top: 1.5rem;">
    <a href="/gumtree-crawler" class="card">
      <h2>Gumtree Crawler</h2>
      <p>Daily crawler for laptops and motorcycles, track price changes and ignore rules</p>
    </a>
    <a href="/makro-crawler" class="card">
      <h2>Makro Crawler</h2>
      <p>Daily crawler for food products and preowned mobiles, track price changes and ignore rules</p>
    </a>
  </div>
  <script>
    const COMPANY_STORAGE_KEY = 'edit_products_company_slug';
    async function loadCompanies() {
      try {
        const r = await fetch('/api/companies');
        const d = await r.json();
        const companies = d.companies || [];
        const sel = document.getElementById('companySelect');
        sel.innerHTML = companies.length
          ? '<option value="">Select company</option>' + companies.map(c => '<option value="' + c + '">' + c + '</option>').join('')
          : '<option value="">Set COMPANY_SLUGS in .env</option>';
        const saved = localStorage.getItem(COMPANY_STORAGE_KEY);
        if (saved && companies.includes(saved)) sel.value = saved;
        else if (companies.length === 1) sel.value = companies[0];
        sel.onchange = () => {
          const v = sel.value;
          if (v) localStorage.setItem(COMPANY_STORAGE_KEY, v);
        };
      } catch (e) {
        document.getElementById('companySelect').innerHTML = '<option value="">Error loading companies</option>';
      }
    }
    loadCompanies();
  </script>
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
  <div id="companyBar" style="margin-bottom: 1rem; font-size: 0.9rem; color: #888;"></div>
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
    <label style="display: flex; align-items: center; gap: 0.5rem; margin-left: 1rem; font-size: 0.9rem; color: #888;">
      <input type="checkbox" id="debugMode"> Debug mode (verbose logs)
    </label>
  </div>
  <div id="status" class="status stopped">
    <span id="statusText">Stopped</span>
    <div id="msg" class="msg"></div>
  </div>
  <p class="help" id="helpText">
    Select a supplier above, then click Start scrape. A browser will open. Save products: click the floating <strong>Save product</strong> button, or press <strong>Ctrl+Shift+S</strong>. For AliExpress: add URLs to urls.txt first. Check Debug mode for verbose logs.
  </p>
  <script>
    let suppliers = [];
    async function loadSuppliers() {
      updateCompanyBar();
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
      const debugMode = document.getElementById('debugMode') && document.getElementById('debugMode').checked;
      const payload = { supplier: slug, proxy_enabled: !!proxyEnabled, proxy_country: proxyCountry, debug: !!debugMode };
      const company = getCompany();
      if (company) payload.company = company;
      const res = await fetch('/api/scrape/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
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
    function getCompany() { return (localStorage.getItem('edit_products_company_slug') || '').trim(); }
    function updateCompanyBar() {
      const company = getCompany();
      const bar = document.getElementById('companyBar');
      bar.innerHTML = company ? 'Company: <strong>' + (company.replace(/</g,'&lt;').replace(/>/g,'&gt;')) + '</strong> — <a href="/" style="color:#2a7">Change on Dashboard</a>' : '<a href="/" style="color:#2a7">Select company on Dashboard first</a> (tiered markup is per-company)';
    }
    async function loadPricingConfig(slug) {
      if (!slug) return;
      const company = getCompany();
      let url = '/api/scraper-config?supplier=' + encodeURIComponent(slug);
      if (company) url += '&company=' + encodeURIComponent(company);
      const r = await fetch(url);
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
      const payload = { supplier: slug, tier_multipliers: tiers };
      const company = getCompany();
      if (company) payload.company = company;
      const res = await fetch('/api/scraper-config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
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
        updateCompanyBar();
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


ORDERS_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Orders - Product Scrapers</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #1a1a1a; color: #e0e0e0; }
    .top-nav { margin-bottom: 1.5rem; }
    .top-nav a { color: #2a7; text-decoration: none; }
    .top-nav a:hover { text-decoration: underline; }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    .field { margin-bottom: 1rem; }
    .field label { display: block; font-size: 0.85rem; color: #888; margin-bottom: 0.4rem; }
    select { padding: 0.6rem; background: #252525; border: 1px solid #444; border-radius: 6px; color: #e0e0e0; min-width: 200px; }
    .order-card { background: #252525; border-radius: 8px; border: 1px solid #333; margin-bottom: 1rem; padding: 1rem; }
    .order-header-row { display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; flex-wrap: wrap; margin-bottom: 0.5rem; }
    .order-card h3 { margin: 0; font-size: 1rem; }
    .order-card .refund-btn { padding: 0.35rem 0.75rem; font-size: 0.85rem; border: none; border-radius: 6px; cursor: pointer; background: #444; color: #e0e0e0; }
    .order-card .refund-btn:hover { background: #555; }
    .order-meta { font-size: 0.85rem; color: #888; margin-bottom: 0.75rem; }
    .supplier-group { margin: 0.75rem 0; padding-left: 1rem; border-left: 3px solid #2a7; }
    .supplier-group .supplier-name { font-weight: 600; color: #2a7; font-size: 0.9rem; margin-bottom: 0.5rem; }
    .order-item { font-size: 0.9rem; padding: 0.25rem 0; color: #ccc; }
    .order-item .qty { color: #888; }
    .msg { margin-top: 1rem; color: #888; }
    .msg.err { color: #c66; }
    .run-panel { margin-top: 0.75rem; padding: 0.75rem; background: #1a1a1a; border-radius: 6px; border: 1px solid #333; }
    .run-panel .run-progress { font-size: 0.85rem; color: #aaa; margin-bottom: 0.5rem; }
    .run-panel .run-current { font-size: 0.9rem; color: #e0e0e0; margin-bottom: 0.5rem; }
    .run-panel .run-message { font-size: 0.9rem; color: #2a7; margin: 0.5rem 0; }
    .run-panel .run-buttons { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
    .run-panel button { padding: 0.5rem 1rem; font-size: 0.9rem; border: none; border-radius: 6px; cursor: pointer; }
    .run-panel button.primary { background: #2a7; color: white; }
    .run-panel button.primary:hover { background: #3b8; }
    .run-panel button.secondary { background: #444; color: #e0e0e0; }
    .run-panel button.secondary:hover { background: #555; }
    .run-panel button:disabled { opacity: 0.5; cursor: not-allowed; }
    .run-panel .session-warn { font-size: 0.85rem; color: #c96; margin-bottom: 0.5rem; }
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; }
    .modal-overlay.hidden { display: none; }
    .modal-overlay .modal { background: #252525; border: 1px solid #444; border-radius: 8px; padding: 1.25rem; min-width: 320px; max-width: 90vw; }
    .modal-overlay .modal h3 { margin: 0 0 0.75rem 0; font-size: 1rem; }
    .modal-overlay .modal p { margin: 0 0 1rem 0; color: #aaa; font-size: 0.9rem; }
    .modal-overlay .modal-actions { display: flex; justify-content: flex-end; }
    .modal-overlay .modal-actions button { padding: 0.5rem 1rem; border: none; border-radius: 4px; cursor: pointer; background: #2a7; color: white; }
    .modal-overlay .modal-actions button:hover { background: #3b8; }
    .top-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; align-items: center; }
    .top-actions button { padding: 0.5rem 1rem; font-size: 0.9rem; border: none; border-radius: 6px; cursor: pointer; }
    .top-actions button { background: #444; color: #e0e0e0; }
    .top-actions button:hover { background: #555; }
    .top-actions button.primary { background: #2a7; color: white; }
    .top-actions button.primary:hover { background: #3b8; }
  </style>
</head>
<body>
  <div class="top-nav"><a href="/">← Dashboard</a> · <a href="/scrape">Scrape</a></div>
  <h1>Orders</h1>
  <div id="companyBar" style="margin-bottom: 0.5rem; font-size: 0.9rem; color: #888;"></div>
  <div id="topActions" class="top-actions">
    <button type="button" onclick="saveYocoSession()">Save Yoco session</button>
  </div>
  <div id="orders"></div>
  <div id="msg" class="msg"></div>
  <div id="ordersModalOverlay" class="modal-overlay hidden">
    <div class="modal">
      <h3 id="ordersModalTitle">Error</h3>
      <p id="ordersModalMessage"></p>
      <div class="modal-actions"><button type="button" onclick="closeOrdersModal()">OK</button></div>
    </div>
  </div>
  <script>
    let supplierDisplayNames = {};
    function getCompany() { return (localStorage.getItem('edit_products_company_slug') || '').trim(); }
    function updateCompanyBar() {
      const company = getCompany();
      const bar = document.getElementById('companyBar');
      bar.innerHTML = company ? 'Company: <strong>' + escapeHtml(company) + '</strong> — <a href="/" style="color:#2a7">Change on Dashboard</a>' : '<a href="/" style="color:#2a7">Select company on Dashboard first</a>';
    }
    function supplierDisplay(slug) { return supplierDisplayNames[slug] || slug; }
    async function loadSuppliers() {
      try {
        const r = await fetch('/api/suppliers');
        const list = await r.json();
        supplierDisplayNames = {};
        (list || []).forEach(s => { supplierDisplayNames[(s.slug || '').toLowerCase()] = s.display_name || s.slug; });
      } catch (_) {}
    }
    function initOrders() {
      updateCompanyBar();
      loadSuppliers().then(loadOrders);
    }
    async function loadOrders() {
      const company = getCompany();
      const el = document.getElementById('orders');
      const msg = document.getElementById('msg');
      msg.textContent = '';
      if (!company) {
        el.innerHTML = '';
        return;
      }
      el.innerHTML = '<p>Loading...</p>';
      try {
        const r = await fetch('/api/orders?company_slug=' + encodeURIComponent(company));
        const d = await r.json();
        const orders = d.orders || [];
        if (d.error) {
          msg.textContent = d.error;
          msg.className = 'msg err';
        }
        if (!orders.length) {
          el.innerHTML = '<p>No orders</p>';
          return;
        }
        let html = '';
        for (const o of orders) {
          const cust = o.customer || {};
          const customer = [cust.first_name || o.customer_first_name, cust.last_name || o.customer_last_name].filter(Boolean).join(' ') ||
            [o.customer_first_name, o.customer_last_name].filter(Boolean).join(' ') || cust.email || o.customer_email || 'Guest';
          const items = (o.items || []).filter(i => !i.cancelled);
          const bySupplier = {};
          for (const it of items) {
            const sup = (it.supplier_slug || it.supplierSlug || 'unknown').trim().toLowerCase() || 'unknown';
            if (!bySupplier[sup]) bySupplier[sup] = [];
            bySupplier[sup].push(it);
          }
          html += '<div class="order-card" data-order-id="' + escapeHtml(String(o.id || o.order_number || '')) + '" data-order-number="' + escapeHtml(String(o.order_number || o.id || '')) + '">';
          html += '<div class="order-header-row"><h3>' + escapeHtml(o.order_number || o.id) + ' — ' + escapeHtml(customer) + '</h3>';
          html += '<button type="button" class="refund-btn" onclick="openRefund()">Refund via Yoco</button></div>';
          html += '<div class="order-meta">' + escapeHtml(cust.email || o.customer_email || '') + ' · ' + (o.customer_phone || '') + ' · ' + (o.status || '') + ' · R' + (o.total || 0) + '</div>';
          for (const [sup, its] of Object.entries(bySupplier)) {
            const displayName = supplierDisplay(sup);
            const itemsJson = escapeHtml(JSON.stringify(its));
            html += '<div class="supplier-group" data-supplier="' + escapeHtml(sup) + '" data-items="' + itemsJson + '">';
            html += '<div class="supplier-name">' + escapeHtml(displayName) + '</div>';
            for (const it of its) {
              html += '<div class="order-item"><span class="qty">' + it.quantity + '×</span> ' + escapeHtml(it.product_name || it.productName || 'Item') + ' — R' + (it.subtotal || (it.price || 0) * (it.quantity || 1)) + '</div>';
            }
            html += '<div class="run-panel" data-order-id="' + escapeHtml(String(o.id || o.order_number || '')) + '" data-order-number="' + escapeHtml(String(o.order_number || o.id || '')) + '" data-supplier="' + escapeHtml(sup) + '"></div>';
            html += '</div>';
          }
          html += '</div>';
        }
        el.innerHTML = html;
        attachRunHandlers();
      } catch (e) {
        el.innerHTML = '';
        msg.textContent = 'Error: ' + e.message;
        msg.className = 'msg err';
      }
    }
    function escapeHtml(s) {
      if (s == null) return '';
      const d = document.createElement('div');
      d.textContent = String(s);
      return d.innerHTML;
    }
    function attachRunHandlers() {
      document.querySelectorAll('.supplier-group').forEach(grp => {
        const panel = grp.querySelector('.run-panel');
        if (!panel) return;
        const orderCard = grp.closest('.order-card');
        const orderId = panel.dataset?.orderId || orderCard?.dataset?.orderId || '';
        const orderNumber = panel.dataset?.orderNumber || orderCard?.dataset?.orderNumber || orderId;
        const supplier = panel.dataset?.supplier || grp.dataset?.supplier || '';
        let items = [];
        try { items = JSON.parse(grp.dataset?.items || '[]'); } catch (_) {}
        renderRunPanel(panel, getCompany(), orderId, orderNumber, supplier, items);
      });
    }
    async function renderRunPanel(panel, company, orderId, orderNumber, supplier, items) {
      const base = { company, order_id: orderId, order_number: orderNumber, supplier, items };
      const statusRes = await fetch('/api/orders/run/status?company=' + encodeURIComponent(company) + '&order_id=' + encodeURIComponent(orderId) + '&supplier=' + encodeURIComponent(supplier));
      const statusData = await statusRes.json();
      const sessionRes = await fetch('/api/orders/run/session-check?supplier=' + encodeURIComponent(supplier));
      const sessionData = await sessionRes.json();
      const hasSession = sessionData.has_session === true;
      const courierRes = await fetch('/api/orders/courier/status?company=' + encodeURIComponent(company) + '&order_id=' + encodeURIComponent(orderId) + '&supplier=' + encodeURIComponent(supplier));
      const courierData = await courierRes.json();
      const courier = courierData.ok ? courierData : {};
      const goodsArrived = courier.goods_arrived === true;
      const quote = courier.quote;
      const orderPlaced = courier.order_placed;

      if (statusData.running) {
        const s = statusData;
        const idx = (s.current_index || 0) + 1;
        const total = s.total || items.length;
        const state = s.state || 'adding';
        let inner = '';
        if (!hasSession) {
          inner += '<div class="session-warn">No saved session. <a href="/scrape" style="color:#2a7">Scrape</a> first and Save session after login.</div>';
        }
        inner += '<div class="run-progress">Item ' + idx + ' of ' + total + '</div>';
        if (state === 'paying') {
          inner += '<div class="run-message">All items added. Go to the supplier cart and pay manually.</div>';
          inner += '<div class="run-buttons"><button class="primary" onclick="runFinish(this)">Finish</button></div>';
        } else {
          const cur = s.current_item;
          if (cur) {
            inner += '<div class="run-current">Current: ' + escapeHtml(cur.product_name || cur.productName || 'Item') + ' (' + (cur.quantity || 1) + '×)</div>';
          }
          inner += '<div class="run-buttons">';
          inner += '<button class="secondary" onclick="runNext(this)">Next product</button>';
          if (idx >= total) {
            inner += '<button class="primary" onclick="runGotoCart(this)">Go to cart & pay</button>';
          }
          inner += '</div>';
        }
        panel.innerHTML = inner;
        return;
      }

      let inner = '';
      if (!hasSession) {
        inner += '<div class="session-warn">No saved session. <a href="/scrape" style="color:#2a7">Scrape</a> first and Save session after login.</div>';
      }
      inner += '<div class="run-buttons"><button class="primary" onclick="runStart(this)"' + (!hasSession ? ' title="Session recommended"' : '') + '>Place order by supplier</button></div>';
      inner += '<div class="courier-section" style="margin-top:1rem; padding-top:1rem; border-top:1px solid #333;">';
      inner += '<div class="run-progress" style="margin-bottom:0.5rem;">Courier Guy delivery (after goods arrive)</div>';
      if (orderPlaced && orderPlaced.waybill_number) {
        inner += '<div class="run-message">Waybill: ' + escapeHtml(orderPlaced.waybill_number) + '</div>';
      } else {
        inner += '<div class="run-buttons">';
        if (!goodsArrived) {
          inner += '<button class="secondary" onclick="courierGoodsArrived(this)">Goods arrived</button>';
        } else {
          inner += '<span class="run-message" style="display:inline-block;margin-right:0.5rem;">Goods arrived ✓</span>';
          if (quote && quote.rates && quote.rates.length) {
            const rates = quote.rates.map(function(r) { return r.service_name + ': R' + r.rate.toFixed(2); }).join('; ');
            inner += '<button class="secondary" onclick="courierQuote(this)">Redo quote</button>';
            inner += '<button class="primary" onclick="courierPlaceOrder(this)">Place Courier order</button>';
            inner += '<div class="run-current" style="margin-top:0.5rem;">' + escapeHtml(rates) + '</div>';
          } else {
            inner += '<button class="secondary" onclick="courierQuote(this)">Get quote</button>';
          }
        }
        inner += '</div>';
      }
      inner += '</div>';
      panel.innerHTML = inner;
    }
    function getRunContext(btn) {
      const panel = btn?.closest?.('.run-panel');
      const grp = panel?.closest?.('.supplier-group');
      return {
        panel,
        grp,
        orderId: panel?.dataset?.orderId || '',
        orderNumber: panel?.dataset?.orderNumber || '',
        supplier: panel?.dataset?.supplier || grp?.dataset?.supplier || '',
        items: (() => { try { return JSON.parse(grp?.dataset?.items || '[]'); } catch (_) { return []; } })()
      };
    }
    function showOrdersModal(title, message) {
      const overlay = document.getElementById('ordersModalOverlay');
      const titleEl = document.getElementById('ordersModalTitle');
      const msgEl = document.getElementById('ordersModalMessage');
      if (overlay && titleEl && msgEl) { titleEl.textContent = title || 'Error'; msgEl.textContent = message || ''; overlay.classList.remove('hidden'); }
    }
    function closeOrdersModal() {
      const overlay = document.getElementById('ordersModalOverlay');
      if (overlay) overlay.classList.add('hidden');
    }
    function openRefund() {
      window.open('https://app.yoco.com/login', '_blank', 'noopener,noreferrer');
    }
    async function saveYocoSession() {
      const res = await fetch('/api/yoco/save-session', { method: 'POST' });
      const d = await res.json();
      if (d.ok) showOrdersModal('Success', d.message || 'Yoco session saved.');
      else showOrdersModal('Error', d.error || 'Failed to save session');
    }
    async function runStart(btn) {
      const ctx = getRunContext(btn);
      const { orderId, orderNumber, supplier, items } = ctx;
      const company = getCompany();
      const res = await fetch('/api/orders/run/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: orderId, order_number: orderNumber, supplier, items })
      });
      const d = await res.json();
      if (d.ok) attachRunHandlers();
      else showOrdersModal('Error', d.error || 'Failed to start');
    }
    async function runNext(btn) {
      const ctx = getRunContext(btn);
      const company = getCompany();
      const res = await fetch('/api/orders/run/next', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: ctx.orderId, supplier: ctx.supplier })
      });
      const d = await res.json();
      if (d.ok) attachRunHandlers();
      else showOrdersModal('Error', d.error || 'Failed');
    }
    async function runGotoCart(btn) {
      const ctx = getRunContext(btn);
      const company = getCompany();
      await fetch('/api/orders/run/goto-cart', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: ctx.orderId, supplier: ctx.supplier })
      });
      attachRunHandlers();
    }
    async function runFinish(btn) {
      const ctx = getRunContext(btn);
      const company = getCompany();
      const res = await fetch('/api/orders/run/finish', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: ctx.orderId, supplier: ctx.supplier })
      });
      if (res.ok) attachRunHandlers();
    }
    async function courierGoodsArrived(btn) {
      const ctx = getRunContext(btn);
      const company = getCompany();
      const res = await fetch('/api/orders/courier/goods-arrived', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: ctx.orderId, supplier: ctx.supplier })
      });
      const d = await res.json();
      if (d.ok) attachRunHandlers();
      else showOrdersModal('Error', d.error || 'Failed');
    }
    async function courierQuote(btn) {
      const ctx = getRunContext(btn);
      const company = getCompany();
      const res = await fetch('/api/orders/courier/quote', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: ctx.orderId, supplier: ctx.supplier })
      });
      const d = await res.json();
      if (d.ok) attachRunHandlers();
      else showOrdersModal('Error', d.error || 'Failed');
    }
    async function courierPlaceOrder(btn) {
      const ctx = getRunContext(btn);
      const company = getCompany();
      const res = await fetch('/api/orders/courier/place-order', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ company, order_id: ctx.orderId, supplier: ctx.supplier })
      });
      const d = await res.json();
      if (d.ok) attachRunHandlers();
      else showOrdersModal('Error', d.error || 'Failed');
    }
    initOrders();
  </script>
</body>
</html>
"""


GUMTREE_CRAWLER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Gumtree Crawler - Product Scrapers</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #1a1a1a; color: #e0e0e0; }
    .top-nav { margin-bottom: 1.5rem; }
    .top-nav a { color: #2a7; text-decoration: none; }
    .top-nav a:hover { text-decoration: underline; }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    .panel { background: #252525; border-radius: 8px; border: 1px solid #333; padding: 1rem; margin-bottom: 1rem; }
    .panel h3 { font-size: 1rem; margin: 0 0 0.75rem 0; color: #aaa; }
    .controls { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0; align-items: center; }
    button { padding: 0.6rem 1rem; font-size: 0.9rem; border: none; border-radius: 6px; cursor: pointer; }
    button.primary { background: #2a7; color: white; }
    button.primary:hover { background: #3b8; }
    button.secondary { background: #444; color: #e0e0e0; }
    button.secondary:hover { background: #555; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    select, input { padding: 0.5rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; }
    .list-header { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0.75rem; background: #333; color: #888; font-size: 0.75rem; font-weight: 600; border-radius: 6px 6px 0 0; }
    .list-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0.75rem; background: #1a1a1a; border: 1px solid #333; border-top: none; }
    .list-row:hover { background: #222; }
    .list-row .col-cb { width: 28px; flex-shrink: 0; }
    .list-row .col-title { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .list-row .col-cat { width: 100px; font-size: 0.8rem; color: #888; flex-shrink: 0; }
    .list-row .col-location { width: 120px; font-size: 0.8rem; color: #888; flex-shrink: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .list-row .col-price { width: 70px; font-weight: 600; flex-shrink: 0; }
    .list-row .col-price.down { color: #2a7; }
    .list-row .col-price.up { color: #c66; }
    .list-row .col-day { width: 80px; font-size: 0.85rem; color: #888; flex-shrink: 0; }
    .list-row .col-actions { display: flex; gap: 0.5rem; flex-shrink: 0; }
    .list-row .col-thumbs { display: flex; gap: 0.25rem; flex-wrap: wrap; max-width: 120px; }
    .list-row .col-thumbs img { width: 32px; height: 32px; object-fit: cover; border-radius: 4px; }
    .list-row a { color: #2a7; text-decoration: none; }
    .list-row a:hover { text-decoration: underline; }
    .list-row.expandable { cursor: pointer; }
    .list-row .expand-icon { opacity: 0.6; font-size: 0.8rem; margin-right: 0.25rem; }
    .row-detail { display: none; padding: 1rem 0.75rem; background: #1e1e1e; border: 1px solid #333; border-top: none; font-size: 0.9rem; }
    .row-detail.expanded { display: block; }
    .row-detail .detail-grid { display: grid; grid-template-columns: auto 1fr; gap: 0.5rem 1.5rem; max-width: 600px; }
    .row-detail .detail-label { color: #888; }
    .row-detail .detail-desc { white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; margin-top: 0.5rem; }
    .badge { display: inline-block; padding: 0.2rem 0.5rem; font-size: 0.75rem; border-radius: 4px; margin-right: 0.25rem; }
    .badge.new { background: #2a7; color: white; }
    .badge.changed { background: #c96; color: #111; }
    .group-province { margin-top: 1rem; }
    .group-province:first-child { margin-top: 0; }
    .group-province-header { background: #333; color: #2a7; font-weight: 600; padding: 0.5rem 0.75rem; border-radius: 6px 6px 0 0; cursor: pointer; }
    .group-city { margin-left: 0.5rem; border-left: 2px solid #444; }
    .group-city-header { background: #2a2a2a; color: #aaa; font-size: 0.9rem; font-weight: 500; padding: 0.4rem 0.75rem; cursor: pointer; }
    .group-suburb { margin-left: 1rem; border-left: 2px solid #555; }
    .group-suburb-header { background: #252525; color: #888; font-size: 0.85rem; padding: 0.35rem 0.75rem; cursor: pointer; }
    .filters { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; align-items: center; }
    .filters label { font-size: 0.85rem; color: #888; }
    .status { font-size: 0.9rem; color: #888; }
    .status.running { color: #2a7; }
    .msg { margin-top: 0.5rem; font-size: 0.9rem; color: #888; }
    .rules-list { margin-top: 0.5rem; }
    .rules-list li { margin-bottom: 0.5rem; display: flex; justify-content: space-between; align-items: center; }
    .rules-list button { padding: 0.3rem 0.6rem; font-size: 0.8rem; }
  </style>
</head>
<body>
  <div class="top-nav"><a href="/">← Dashboard</a></div>
  <h1>Gumtree Crawler</h1>
  <p class="status">Daily crawler for laptops and motorcycles. Tracks first-seen, last-seen, and price changes.</p>

  <div class="panel">
    <h3>Scheduler status</h3>
    <div id="status" class="status">Loading...</div>
    <div class="controls">
      <button id="runBtn" class="primary" onclick="runNow()">Run now</button>
    </div>
  </div>

  <div class="panel">
    <h3>Filters</h3>
    <div class="filters">
      <label>Category</label>
      <select id="filterCategory">
        <option value="">All</option>
        <option value="laptops">Laptops</option>
        <option value="motorcycles">Motorcycles</option>
      </select>
      <label>Price min</label>
      <input type="number" id="filterMinPrice" placeholder="Min" style="width:80px">
      <label>Price max</label>
      <input type="number" id="filterMaxPrice" placeholder="Max" style="width:80px">
      <label>Keyword</label>
      <input type="text" id="filterKeyword" placeholder="Search" style="width:120px">
      <label><input type="checkbox" id="filterNewToday"> New today</label>
      <label><input type="checkbox" id="filterPriceChanged"> Price changed</label>
      <button class="secondary" onclick="loadListings()">Apply</button>
    </div>
  </div>

  <div class="panel">
    <h3>Listings</h3>
    <div class="controls" style="margin-bottom:0.5rem;">
      <label>Export to products:</label>
      <select id="exportCompany"><option value="">Select company</option></select>
      <button class="secondary" onclick="exportSelected()">Export selected to Gumtree</button>
    </div>
    <div id="listingsWrap">
      <div class="list-header"><span class="col-cb"></span><span class="col-title">Title</span><span class="col-cat">Category</span><span class="col-location">Location</span><span class="col-price">Price</span><span class="col-day">Day on GT</span><span class="col-thumbs" style="max-width:120px">Images</span><span class="col-actions">Actions</span></div>
      <div id="listings"></div>
    </div>
    <div id="listingsMsg" class="msg"></div>
  </div>

  <div class="panel">
    <h3>Ignore rules</h3>
    <div class="controls">
      <select id="ruleType">
        <option value="url">URL</option>
        <option value="ad_id">Ad ID</option>
        <option value="title_keyword">Title keyword</option>
        <option value="seller">Seller</option>
      </select>
      <input type="text" id="ruleValue" placeholder="Value" style="width:200px">
      <button class="secondary" onclick="addIgnoreRule()">Add rule</button>
    </div>
    <ul id="rulesList" class="rules-list"></ul>
  </div>

  <div class="panel">
    <h3>Recent price changes</h3>
    <div id="changes"></div>
  </div>

  <script>
    async function loadStatus() {
      const r = await fetch('/api/gumtree-crawler/status');
      const d = await r.json();
      const el = document.getElementById('status');
      const btn = document.getElementById('runBtn');
      btn.disabled = d.running;
      el.textContent = d.running ? 'Running...' : (d.last_run ? 'Last run: ' + (d.last_run.finished_at || d.last_run.started_at) + ' (' + (d.last_run.status || '') + ')' : 'Never run');
      el.className = 'status' + (d.running ? ' running' : '');
      if (d.next_scheduled) el.textContent += ' | Next: ' + d.next_scheduled.slice(0,16);
    }
    async function runNow() {
      const r = await fetch('/api/gumtree-crawler/run-now', { method: 'POST' });
      const d = await r.json();
      if (d.ok) loadStatus();
      else alert(d.error || 'Failed');
    }
    async function loadListings() {
      const params = new URLSearchParams();
      const cat = document.getElementById('filterCategory').value;
      const minP = document.getElementById('filterMinPrice').value;
      const maxP = document.getElementById('filterMaxPrice').value;
      const kw = document.getElementById('filterKeyword').value;
      const newToday = document.getElementById('filterNewToday').checked;
      const priceChanged = document.getElementById('filterPriceChanged').checked;
      if (cat) params.set('category', cat);
      if (minP) params.set('min_price', minP);
      if (maxP) params.set('max_price', maxP);
      if (kw) params.set('keyword', kw);
      if (newToday) params.set('new_today', '1');
      if (priceChanged) params.set('price_changed', '1');
      const r = await fetch('/api/gumtree-crawler/listings?' + params);
      const d = await r.json();
      const listEl = document.getElementById('listings');
      const msg = document.getElementById('listingsMsg');
      if (!d.listings || !d.listings.length) {
        listEl.innerHTML = '';
        msg.textContent = 'No listings found. Total: ' + (d.total || 0);
        return;
      }
      const imgBase = '/api/gumtree-crawler/serve-image/';
      function toggleRowDetail(ev, rowEl) {
        if (ev.target.closest('input.listing-cb') || ev.target.closest('a') || ev.target.closest('button')) return;
        const wr = rowEl.closest('.list-row-wrapper');
        if (!wr) return;
        const detail = wr.querySelector('.row-detail');
        const icon = rowEl.querySelector('.expand-icon');
        if (detail && detail.classList.toggle('expanded')) {
          if (icon) icon.textContent = '\u25BC';
        } else {
          if (icon) icon.textContent = '\u25B6';
        }
      }
      function rowHtml(l) {
        const priceCls = (l.trend === 'down' ? ' down' : (l.trend === 'up' ? ' up' : ''));
        const dayStr = l.day_on != null ? (l.day_on + 'd') : '-';
        const thumbs = (l.images || []).slice(0, 4).map(p => '<img src="' + imgBase + encodeURIComponent(p) + '" alt="" loading="lazy">').join('');
        const link = '<a href="' + escapeHtml(l.url) + '" target="_blank">' + escapeHtml(l.title || 'Untitled') + '</a>';
        const newBadge = l.is_new ? '<span class="badge new">New</span> ' : '';
        const detailRows = [
          ['Title', escapeHtml(l.title || '-')],
          ['Price', 'R' + (l.price ?? '?') + (l.prev_price != null && l.prev_price !== l.price ? ' (was R' + l.prev_price + ')' : '')],
          ['Location', escapeHtml(l.location || '-')],
          ['Seller', escapeHtml(l.seller || '-')],
          ['Condition', escapeHtml(l.condition || '-')],
          ['Category', escapeHtml(l.category || '-')],
          ['URL', '<a href="' + escapeHtml(l.url) + '" target="_blank">' + escapeHtml((l.url || '').slice(0, 60) + (l.url && l.url.length > 60 ? '...' : '')) + '</a>'],
          ['First seen', escapeHtml(l.first_seen || '-')],
          ['Last seen', escapeHtml(l.last_seen || '-')],
          ['Notes', escapeHtml(l.notes || '-')]
        ];
        const detailGrid = detailRows.map(([lab, val]) => '<span class="detail-label">' + lab + '</span><span>' + val + '</span>').join('');
        const desc = l.description ? '<div class="detail-desc"><strong>Description</strong><pre>' + escapeHtml(l.description) + '</pre></div>' : '';
        const detailHtml = '<div class="row-detail" id="detail-' + l.id + '"><div class="detail-grid">' + detailGrid + '</div>' + desc + '</div>';
        const rowContent = '<span class="col-cb"><input type="checkbox" class="listing-cb" value="' + l.id + '"></span><span class="col-title"><span class="expand-icon">\u25B6</span>' + newBadge + link + '</span><span class="col-cat">' + escapeHtml(l.category || '') + '</span><span class="col-location">' + escapeHtml(l.location || '') + '</span><span class="col-price' + priceCls + '">R' + (l.price ?? '?') + '</span><span class="col-day">' + dayStr + '</span><span class="col-thumbs">' + thumbs + '</span><span class="col-actions"><button class="secondary" onclick="fetchImages(' + l.id + ')" id="fetchBtn' + l.id + '">Get images</button><button class="secondary" onclick="ignoreListing(' + l.id + ')">Ignore</button></span>';
        return '<div class="list-row-wrapper"><div class="list-row expandable" data-id="' + l.id + '" onclick="toggleRowDetail(event, this)">' + rowContent + '</div>' + detailHtml + '</div>';
      }
      const groups = {};
      for (const l of d.listings) {
        const prov = (l.province || 'Other').trim() || 'Other';
        const city = (l.city || 'Other').trim() || 'Other';
        const sub = (l.suburb || 'Other').trim() || 'Other';
        if (!groups[prov]) groups[prov] = {};
        if (!groups[prov][city]) groups[prov][city] = {};
        if (!groups[prov][city][sub]) groups[prov][city][sub] = [];
        groups[prov][city][sub].push(l);
      }
      const provOrder = Object.keys(groups).sort((a,b) => (a === 'Other' ? 1 : 0) - (b === 'Other' ? 1 : 0) || a.localeCompare(b));
      let html = '';
      for (const prov of provOrder) {
        const cities = groups[prov];
        const cityOrder = Object.keys(cities).sort((a,b) => (a === 'Other' ? 1 : 0) - (b === 'Other' ? 1 : 0) || a.localeCompare(b));
        html += '<div class="group-province"><div class="group-province-header">' + escapeHtml(prov) + '</div>';
        for (const city of cityOrder) {
          const suburbs = cities[city];
          const subOrder = Object.keys(suburbs).sort((a,b) => (a === 'Other' ? 1 : 0) - (b === 'Other' ? 1 : 0) || a.localeCompare(b));
          html += '<div class="group-city"><div class="group-city-header">' + escapeHtml(city) + '</div>';
          for (const sub of subOrder) {
            const items = suburbs[sub];
            html += '<div class="group-suburb"><div class="group-suburb-header">' + escapeHtml(sub) + ' (' + items.length + ')</div>';
            html += items.map(rowHtml).join('');
            html += '</div>';
          }
          html += '</div>';
        }
        html += '</div>';
      }
      listEl.innerHTML = html;
      msg.textContent = 'Total: ' + d.total;
    }
    async function fetchImages(id) {
      const btn = document.getElementById('fetchBtn' + id);
      if (btn) btn.disabled = true;
      try {
        const r = await fetch('/api/gumtree-crawler/listings/' + id + '/fetch-images', { method: 'POST' });
        const d = await r.json();
        if (d.ok) loadListings();
        else alert(d.error || 'Failed to fetch images');
      } finally {
        if (btn) btn.disabled = false;
      }
    }
    function escapeHtml(s) {
      if (s == null) return '';
      const d = document.createElement('div');
      d.textContent = String(s);
      return d.innerHTML;
    }
    async function ignoreListing(id) {
      const r = await fetch('/api/gumtree-crawler/listings/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ignored: 1 }) });
      if (r.ok) loadListings();
    }
    async function loadIgnoreRules() {
      const r = await fetch('/api/gumtree-crawler/ignore-rules');
      const d = await r.json();
      const ul = document.getElementById('rulesList');
      ul.innerHTML = (d.rules || []).map(r => '<li>' + escapeHtml(r.rule_type) + ': ' + escapeHtml(r.value) + ' <button class="secondary" onclick="deleteRule(' + r.id + ')">Delete</button></li>').join('');
    }
    async function addIgnoreRule() {
      const type = document.getElementById('ruleType').value;
      const val = document.getElementById('ruleValue').value.trim();
      if (!val) return;
      const r = await fetch('/api/gumtree-crawler/ignore-rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rule_type: type, value: val }) });
      const d = await r.json();
      if (d.ok) { document.getElementById('ruleValue').value = ''; loadIgnoreRules(); }
      else alert(d.error || 'Failed');
    }
    async function deleteRule(id) {
      await fetch('/api/gumtree-crawler/ignore-rules/' + id, { method: 'DELETE' });
      loadIgnoreRules();
    }
    async function loadChanges() {
      const r = await fetch('/api/gumtree-crawler/changes?limit=20');
      const d = await r.json();
      const el = document.getElementById('changes');
      if (!d.changes || !d.changes.length) { el.innerHTML = '<p class="msg">No price changes yet.</p>'; return; }
      el.innerHTML = '<ul>' + d.changes.map(c => '<li><a href="' + escapeHtml(c.url) + '" target="_blank">' + escapeHtml(c.title || '') + '</a> R' + (c.old_price || '?') + ' → R' + c.new_price + ' (' + (c.changed_at || '').slice(0,10) + ')</li>').join('') + '</ul>';
    }
    async function loadCompanies() {
      const r = await fetch('/api/companies');
      const d = await r.json();
      const companies = d.companies || [];
      const sel = document.getElementById('exportCompany');
      sel.innerHTML = '<option value="">Select company</option>' + companies.map(c => '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>').join('');
    }
    async function exportSelected() {
      const company = document.getElementById('exportCompany').value;
      if (!company) { alert('Select company first'); return; }
      const ids = Array.from(document.querySelectorAll('.listing-cb:checked')).map(cb => parseInt(cb.value, 10)).filter(n => !isNaN(n));
      if (!ids.length) { alert('Select at least one listing'); return; }
      const r = await fetch('/api/gumtree-crawler/export-to-products', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ listing_ids: ids, company_slug: company }) });
      const d = await r.json();
      if (d.ok) alert(d.message || 'Exported ' + d.added + ' listing(s)');
      else alert(d.error || 'Export failed');
    }
    loadCompanies();
    loadStatus();
    loadListings();
    loadIgnoreRules();
    loadChanges();
    setInterval(loadStatus, 5000);
  </script>
</body>
</html>
"""


MAKRO_CRAWLER_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Makro Crawler - Product Scrapers</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: system-ui, sans-serif; margin: 2rem; background: #1a1a1a; color: #e0e0e0; }
    .top-nav { margin-bottom: 1.5rem; }
    .top-nav a { color: #2a7; text-decoration: none; }
    .top-nav a:hover { text-decoration: underline; }
    h1 { font-size: 1.5rem; margin-bottom: 1rem; }
    .panel { background: #252525; border-radius: 8px; border: 1px solid #333; padding: 1rem; margin-bottom: 1rem; }
    .panel h3 { font-size: 1rem; margin: 0 0 0.75rem 0; color: #aaa; }
    .controls { display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1rem 0; align-items: center; }
    button { padding: 0.6rem 1rem; font-size: 0.9rem; border: none; border-radius: 6px; cursor: pointer; }
    button.primary { background: #2a7; color: white; }
    button.primary:hover { background: #3b8; }
    button.secondary { background: #444; color: #e0e0e0; }
    button.secondary:hover { background: #555; }
    button:disabled { opacity: 0.5; cursor: not-allowed; }
    select, input { padding: 0.5rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; }
    .list-header { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0.75rem; background: #333; color: #888; font-size: 0.75rem; font-weight: 600; border-radius: 6px 6px 0 0; }
    .list-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 0.75rem; background: #1a1a1a; border: 1px solid #333; border-top: none; }
    .list-row:hover { background: #222; }
    .list-row .col-cb { width: 28px; flex-shrink: 0; }
    .list-row .col-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .list-row .col-desc { flex: 1; min-width: 0; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.85rem; color: #888; }
    .list-row .col-cat { width: 120px; font-size: 0.8rem; color: #888; flex-shrink: 0; }
    .list-row .col-price { width: 90px; font-weight: 600; flex-shrink: 0; }
    .list-row .col-price.down { color: #2a7; }
    .list-row .col-price.up { color: #c66; }
    .list-row .col-day { width: 70px; font-size: 0.85rem; color: #888; flex-shrink: 0; }
    .list-row .col-actions { display: flex; gap: 0.5rem; flex-shrink: 0; }
    .list-row.expandable { cursor: pointer; }
    .list-row .expand-icon { opacity: 0.6; font-size: 0.8rem; margin-right: 0.25rem; }
    .row-detail { display: none; padding: 1rem 0.75rem; background: #1e1e1e; border: 1px solid #333; border-top: none; font-size: 0.9rem; }
    .row-detail.expanded { display: block; }
    .row-detail .detail-grid { display: grid; grid-template-columns: auto 1fr; gap: 0.5rem 1.5rem; max-width: 600px; }
    .row-detail .detail-label { color: #888; }
    .row-detail .detail-desc { white-space: pre-wrap; word-break: break-word; max-height: 200px; overflow-y: auto; margin-top: 0.5rem; }
    .list-row a { color: #2a7; text-decoration: none; }
    .list-row a:hover { text-decoration: underline; }
    .badge { display: inline-block; padding: 0.2rem 0.5rem; font-size: 0.75rem; border-radius: 4px; margin-right: 0.25rem; }
    .badge.new { background: #2a7; color: white; }
    .badge.changed { background: #c96; color: #111; }
    .filters { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 1rem; align-items: center; }
    .filters label { font-size: 0.85rem; color: #888; }
    .status { font-size: 0.9rem; color: #888; }
    .status.running { color: #2a7; }
    .msg { margin-top: 0.5rem; font-size: 0.9rem; color: #888; }
    .rules-list { margin-top: 0.5rem; }
    .rules-list li { margin-bottom: 0.5rem; display: flex; justify-content: space-between; align-items: center; }
    .rules-list button { padding: 0.3rem 0.6rem; font-size: 0.8rem; }
  </style>
</head>
<body>
  <div class="top-nav"><a href="/">← Dashboard</a></div>
  <h1>Makro Crawler</h1>
  <p class="status">Daily crawler for food products and preowned mobiles. Tracks first-seen, last-seen, and price changes. Supports session cookies for protected URLs.</p>

  <div class="panel">
    <h3>Scheduler status</h3>
    <div id="status" class="status">Loading...</div>
    <div class="controls">
      <button id="runBtn" class="primary" onclick="runNow()">Run now</button>
    </div>
  </div>

  <div class="panel">
    <h3>Filters</h3>
    <div class="filters">
      <label>Category</label>
      <select id="filterCategory">
        <option value="">All</option>
        <option value="food-products">Food Products</option>
        <option value="preowned-mobiles">Preowned Mobiles</option>
      </select>
      <label>Price min</label>
      <input type="number" id="filterMinPrice" placeholder="Min" style="width:80px">
      <label>Price max</label>
      <input type="number" id="filterMaxPrice" placeholder="Max" style="width:80px">
      <label>Keyword</label>
      <input type="text" id="filterKeyword" placeholder="Search" style="width:120px">
      <label><input type="checkbox" id="filterNewToday"> New today</label>
      <label><input type="checkbox" id="filterPriceChanged"> Price changed</label>
      <button class="secondary" onclick="loadListings()">Apply</button>
    </div>
  </div>

  <div class="panel">
    <h3>Listings</h3>
    <div class="controls" style="margin-bottom:0.5rem;">
      <label>Export to products:</label>
      <select id="exportCompany"><option value="">Select company</option></select>
      <button class="secondary" onclick="exportSelected()">Export selected to Makro</button>
    </div>
    <div class="list-header"><span class="col-cb"></span><span class="col-name">Name</span><span class="col-desc">Description</span><span class="col-cat">Category</span><span class="col-price">Price</span><span class="col-day">Day on site</span><span class="col-actions">Actions</span></div>
    <div id="listings"></div>
    <div id="listingsMsg" class="msg"></div>
  </div>

  <div class="panel">
    <h3>Ignore rules</h3>
    <div class="controls">
      <select id="ruleType">
        <option value="url">URL</option>
        <option value="ad_id">Ad ID (pid)</option>
        <option value="title_keyword">Title keyword</option>
        <option value="seller">Seller</option>
      </select>
      <input type="text" id="ruleValue" placeholder="Value" style="width:200px">
      <button class="secondary" onclick="addIgnoreRule()">Add rule</button>
    </div>
    <ul id="rulesList" class="rules-list"></ul>
  </div>

  <div class="panel">
    <h3>Recent price changes</h3>
    <div id="changes"></div>
  </div>

  <script>
    async function loadStatus() {
      const r = await fetch('/api/makro-crawler/status');
      const d = await r.json();
      const el = document.getElementById('status');
      const btn = document.getElementById('runBtn');
      btn.disabled = d.running;
      el.textContent = d.running ? 'Running...' : (d.last_run ? 'Last run: ' + (d.last_run.finished_at || d.last_run.started_at) + ' (' + (d.last_run.status || '') + ')' : 'Never run');
      el.className = 'status' + (d.running ? ' running' : '');
      if (d.next_scheduled) el.textContent += ' | Next: ' + d.next_scheduled.slice(0,16);
    }
    async function runNow() {
      const r = await fetch('/api/makro-crawler/run-now', { method: 'POST' });
      const d = await r.json();
      if (d.ok) loadStatus();
      else alert(d.error || 'Failed');
    }
    async function loadListings() {
      const params = new URLSearchParams();
      const cat = document.getElementById('filterCategory').value;
      const minP = document.getElementById('filterMinPrice').value;
      const maxP = document.getElementById('filterMaxPrice').value;
      const kw = document.getElementById('filterKeyword').value;
      const newToday = document.getElementById('filterNewToday').checked;
      const priceChanged = document.getElementById('filterPriceChanged').checked;
      if (cat) params.set('category', cat);
      if (minP) params.set('min_price', minP);
      if (maxP) params.set('max_price', maxP);
      if (kw) params.set('keyword', kw);
      if (newToday) params.set('new_today', '1');
      if (priceChanged) params.set('price_changed', '1');
      const r = await fetch('/api/makro-crawler/listings?' + params);
      const d = await r.json();
      const grid = document.getElementById('listings');
      const msg = document.getElementById('listingsMsg');
      if (!d.listings || !d.listings.length) {
        grid.innerHTML = '';
        msg.textContent = 'No listings found. Total: ' + (d.total || 0);
        return;
      }
      function toggleRowDetail(ev, rowEl) {
        if (ev.target.closest('input.listing-cb') || ev.target.closest('a') || ev.target.closest('button')) return;
        const wr = rowEl.closest('.list-row-wrapper');
        if (!wr) return;
        const detail = wr.querySelector('.row-detail');
        const icon = rowEl.querySelector('.expand-icon');
        if (detail && detail.classList.toggle('expanded')) {
          if (icon) icon.textContent = '\u25BC';
        } else {
          if (icon) icon.textContent = '\u25B6';
        }
      }
      function rowHtml(l) {
        const priceCls = (l.trend === 'down' ? ' down' : (l.trend === 'up' ? ' up' : ''));
        const dayStr = l.day_on != null ? (l.day_on + 'd') : '-';
        const priceDisplay = l.price != null ? 'R' + (l.price / 100).toFixed(2) : '?';
        const name = l.title || 'Untitled';
        const link = '<a href="' + escapeHtml(l.url) + '" target="_blank">' + escapeHtml(name) + '</a>';
        const newBadge = l.is_new ? '<span class="badge new">New</span> ' : '';
        const desc = (l.description || '').slice(0, 120);
        const descDisplay = desc ? (desc + (l.description && l.description.length > 120 ? '...' : '')) : '-';
        const detailRows = [
          ['Title', escapeHtml(l.title || '-')],
          ['Price', priceDisplay + (l.prev_price != null && l.prev_price !== l.price ? ' (was R' + (l.prev_price / 100).toFixed(2) + ')' : '')],
          ['Seller', escapeHtml(l.seller || '-')],
          ['Category', escapeHtml(l.category || '-')],
          ['URL', '<a href="' + escapeHtml(l.url) + '" target="_blank">' + escapeHtml((l.url || '').slice(0, 60) + (l.url && l.url.length > 60 ? '...' : '')) + '</a>'],
          ['First seen', escapeHtml(l.first_seen || '-')],
          ['Last seen', escapeHtml(l.last_seen || '-')],
          ['Notes', escapeHtml(l.notes || '-')]
        ];
        const detailGrid = detailRows.map(([lab, val]) => '<span class="detail-label">' + lab + '</span><span>' + val + '</span>').join('');
        const descBlock = l.description ? '<div class="detail-desc"><strong>Description</strong><pre>' + escapeHtml(l.description) + '</pre></div>' : '';
        const detailHtml = '<div class="row-detail" id="detail-' + l.id + '"><div class="detail-grid">' + detailGrid + '</div>' + descBlock + '</div>';
        const rowContent = '<span class="col-cb"><input type="checkbox" class="listing-cb" value="' + l.id + '"></span><span class="col-name"><span class="expand-icon">\u25B6</span>' + newBadge + link + '</span><span class="col-desc" title="' + escapeHtml(l.description || '') + '">' + escapeHtml(descDisplay) + '</span><span class="col-cat">' + escapeHtml(l.category || '') + '</span><span class="col-price' + priceCls + '">' + priceDisplay + '</span><span class="col-day">' + dayStr + '</span><span class="col-actions"><button class="secondary" onclick="ignoreListing(' + l.id + ')">Ignore</button></span>';
        return '<div class="list-row-wrapper"><div class="list-row expandable" data-id="' + l.id + '" onclick="toggleRowDetail(event, this)">' + rowContent + '</div>' + detailHtml + '</div>';
      }
      grid.innerHTML = d.listings.map(rowHtml).join('');
      msg.textContent = 'Total: ' + d.total;
    }
    function escapeHtml(s) {
      if (s == null) return '';
      const d = document.createElement('div');
      d.textContent = String(s);
      return d.innerHTML;
    }
    async function ignoreListing(id) {
      const r = await fetch('/api/makro-crawler/listings/' + id, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ignored: 1 }) });
      if (r.ok) loadListings();
    }
    async function loadIgnoreRules() {
      const r = await fetch('/api/makro-crawler/ignore-rules');
      const d = await r.json();
      const ul = document.getElementById('rulesList');
      ul.innerHTML = (d.rules || []).map(r => '<li>' + escapeHtml(r.rule_type) + ': ' + escapeHtml(r.value) + ' <button class="secondary" onclick="deleteRule(' + r.id + ')">Delete</button></li>').join('');
    }
    async function addIgnoreRule() {
      const type = document.getElementById('ruleType').value;
      const val = document.getElementById('ruleValue').value.trim();
      if (!val) return;
      const r = await fetch('/api/makro-crawler/ignore-rules', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rule_type: type, value: val }) });
      const d = await r.json();
      if (d.ok) { document.getElementById('ruleValue').value = ''; loadIgnoreRules(); }
      else alert(d.error || 'Failed');
    }
    async function deleteRule(id) {
      await fetch('/api/makro-crawler/ignore-rules/' + id, { method: 'DELETE' });
      loadIgnoreRules();
    }
    async function loadChanges() {
      const r = await fetch('/api/makro-crawler/changes?limit=20');
      const d = await r.json();
      const el = document.getElementById('changes');
      if (!d.changes || !d.changes.length) { el.innerHTML = '<p class="msg">No price changes yet.</p>'; return; }
      el.innerHTML = '<ul>' + d.changes.map(c => {
        const oldP = c.old_price != null ? 'R' + (c.old_price/100).toFixed(2) : '?';
        const newP = c.new_price != null ? 'R' + (c.new_price/100).toFixed(2) : '?';
        return '<li><a href="' + escapeHtml(c.url) + '" target="_blank">' + escapeHtml(c.title || '') + '</a> ' + oldP + ' → ' + newP + ' (' + (c.changed_at || '').slice(0,10) + ')</li>';
      }).join('') + '</ul>';
    }
    async function loadCompanies() {
      const r = await fetch('/api/companies');
      const d = await r.json();
      const companies = d.companies || [];
      const sel = document.getElementById('exportCompany');
      sel.innerHTML = '<option value="">Select company</option>' + companies.map(c => '<option value="' + escapeHtml(c) + '">' + escapeHtml(c) + '</option>').join('');
    }
    async function exportSelected() {
      const company = document.getElementById('exportCompany').value;
      if (!company) { alert('Select company first'); return; }
      const ids = Array.from(document.querySelectorAll('.listing-cb:checked')).map(cb => parseInt(cb.value, 10)).filter(n => !isNaN(n));
      if (!ids.length) { alert('Select at least one listing'); return; }
      const r = await fetch('/api/makro-crawler/export-to-products', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ listing_ids: ids, company_slug: company }) });
      const d = await r.json();
      if (d.ok) alert(d.message || 'Exported ' + d.added + ' listing(s)');
      else alert(d.error || 'Export failed');
    }
    loadCompanies();
    loadStatus();
    loadListings();
    loadIgnoreRules();
    loadChanges();
    setInterval(loadStatus, 5000);
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/orders")
def orders_page():
    return render_template_string(ORDERS_HTML)


@app.route("/scrape")
def scrape_page():
    return render_template_string(SCRAPE_HTML)


@app.route("/api/suppliers")
def api_suppliers():
    return jsonify(get_suppliers())


@app.route("/api/companies")
def api_companies():
    """Return company slugs for Orders and Edit pages."""
    from shared.config import get_target_slugs
    return jsonify({"companies": get_target_slugs()})


@app.route("/api/orders")
def api_orders():
    """Fetch orders from Django API for the given company. Requires company_slug."""
    company_slug = (request.args.get("company_slug") or "").strip()
    if not company_slug:
        return jsonify({"orders": [], "error": "company_slug required"})
    base_url = (os.environ.get("API_BASE_URL") or "").strip()
    if not base_url:
        return jsonify({"orders": [], "error": "Set API_BASE_URL in .env"})
    try:
        from shared.config import get_credentials_for_company
        username, password = get_credentials_for_company(company_slug)
    except ValueError as e:
        return jsonify({"orders": [], "error": str(e)})
    if not username or not password:
        return jsonify({"orders": [], "error": "No credentials for this company"})
    from shared.upload import get_auth_token
    use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
    token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
    if not token:
        return jsonify({"orders": [], "error": "Login failed"})
    try:
        import requests
        r = requests.get(
            f"{base_url.rstrip('/')}/v1/orders/",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Company-Slug": company_slug,
            },
            params={"limit": 100},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        orders = data.get("data") or data.get("results") or []
        return jsonify({"orders": orders})
    except Exception as e:
        return jsonify({"orders": [], "error": str(e)})


def _fetch_order_from_api(company_slug: str, order_id: str) -> dict | None:
    """Fetch single order from Django API. Returns order dict or None."""
    base_url = (os.environ.get("API_BASE_URL") or "").strip()
    if not base_url:
        return None
    try:
        from shared.config import get_credentials_for_company
        username, password = get_credentials_for_company(company_slug)
    except ValueError:
        return None
    if not username or not password:
        return None
    from shared.upload import get_auth_token
    use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
    token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
    if not token:
        return None
    try:
        import requests
        base = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Company-Slug": company_slug,
        }
        r = requests.get(
            f"{base}/v1/orders/{order_id}/",
            headers=headers,
            timeout=30,
        )
        if r.status_code == 404:
            r = requests.get(
                f"{base}/v1/orders/number/{order_id}/",
                headers=headers,
                timeout=30,
            )
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("data") or data
    except Exception:
        return None


def _get_courier_key(company: str, order_id: str, supplier: str) -> tuple:
    return (company.strip(), str(order_id).strip(), supplier.strip().lower())


def _get_courier_state(company: str, order_id: str, supplier: str) -> dict:
    with _courier_lock:
        return _courier_state.get(_get_courier_key(company, order_id, supplier), {})


def _set_courier_state(company: str, order_id: str, supplier: str, data: dict) -> None:
    with _courier_lock:
        key = _get_courier_key(company, order_id, supplier)
        prev = _courier_state.get(key, {})
        _courier_state[key] = {**prev, **data}


@app.route("/api/orders/courier/goods-arrived", methods=["POST"])
def api_orders_courier_goods_arrived():
    """Mark goods as arrived for courier workflow."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()
    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})
    _set_courier_state(company, order_id, supplier, {"goods_arrived": True})
    return jsonify({"ok": True, "goods_arrived": True})


@app.route("/api/orders/courier/quote", methods=["POST"])
def api_orders_courier_quote():
    """Get Courier Guy quote for order delivery. Requires goods_arrived."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()
    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})
    state = _get_courier_state(company, order_id, supplier)
    if not state.get("goods_arrived"):
        return jsonify({"ok": False, "error": "Confirm goods arrived first"})
    order = _fetch_order_from_api(company, order_id)
    if not order:
        return jsonify({"ok": False, "error": "Could not fetch order"})
    shipping = order.get("shipping_address") or {}
    if not shipping.get("address") and not shipping.get("street_address"):
        return jsonify({"ok": False, "error": "Order has no shipping address"})
    # Collection from env (business address)
    coll = {
        "street_address": os.environ.get("COURIER_COLLECTION_STREET", ""),
        "local_area": os.environ.get("COURIER_COLLECTION_SUBURB", ""),
        "city": os.environ.get("COURIER_COLLECTION_CITY", ""),
        "zone": os.environ.get("COURIER_COLLECTION_PROVINCE", "GP"),
        "code": os.environ.get("COURIER_COLLECTION_POSTAL", ""),
        "country": "ZA",
    }
    if not coll["street_address"] or not coll["city"]:
        return jsonify({"ok": False, "error": "Set COURIER_COLLECTION_* env vars (street, suburb, city, province, postal)"})
    deliv = {
        "street_address": shipping.get("address") or shipping.get("street_address", ""),
        "local_area": shipping.get("suburb", ""),
        "city": shipping.get("city", ""),
        "zone": shipping.get("province", ""),
        "code": shipping.get("postal_code") or shipping.get("postalCode", ""),
        "country": "ZA",
    }
    from courier_guy_client import get_quote
    result = get_quote(coll, deliv)
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error", "Quote failed")})
    _set_courier_state(company, order_id, supplier, {"quote": result})
    return jsonify({"ok": True, "rates": result.get("rates", [])})


@app.route("/api/orders/courier/place-order", methods=["POST"])
def api_orders_courier_place_order():
    """Place Courier Guy shipment for order. Requires quote first."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()
    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})
    state = _get_courier_state(company, order_id, supplier)
    if not state.get("goods_arrived"):
        return jsonify({"ok": False, "error": "Confirm goods arrived first"})
    if not state.get("quote"):
        return jsonify({"ok": False, "error": "Get quote first"})
    order = _fetch_order_from_api(company, order_id)
    if not order:
        return jsonify({"ok": False, "error": "Could not fetch order"})
    shipping = order.get("shipping_address") or {}
    coll = {
        "street_address": os.environ.get("COURIER_COLLECTION_STREET", ""),
        "local_area": os.environ.get("COURIER_COLLECTION_SUBURB", ""),
        "city": os.environ.get("COURIER_COLLECTION_CITY", ""),
        "zone": os.environ.get("COURIER_COLLECTION_PROVINCE", "GP"),
        "code": os.environ.get("COURIER_COLLECTION_POSTAL", ""),
        "country": "ZA",
    }
    deliv = {
        "street_address": shipping.get("address") or shipping.get("street_address", ""),
        "local_area": shipping.get("suburb", ""),
        "city": shipping.get("city", ""),
        "zone": shipping.get("province", ""),
        "code": shipping.get("postal_code") or shipping.get("postalCode", ""),
        "country": "ZA",
    }
    cust = order.get("customer") or {}
    delivery_contact = {
        "name": f"{order.get('customer_first_name', '')} {order.get('customer_last_name', '')}".strip() or "Customer",
        "email": order.get("customer_email", "") or cust.get("email", "customer@example.com"),
        "mobile_number": order.get("customer_phone", "") or "+27000000000",
    }
    collection_contact = {
        "name": os.environ.get("COURIER_COLLECTION_NAME", "Sender"),
        "email": os.environ.get("COURIER_COLLECTION_EMAIL", "sender@example.com"),
        "mobile_number": os.environ.get("COURIER_COLLECTION_PHONE", "+27000000000"),
    }
    order_number = order.get("order_number") or order_id
    quote = state.get("quote") or {}
    rates = quote.get("rates") or []
    service_code = (rates[0].get("service_code") if rates else "") or "ECO"
    from courier_guy_client import create_shipment
    result = create_shipment(
        coll, deliv, delivery_contact, collection_contact,
        customer_reference=order_number,
        declared_value=float(order.get("total", 0)),
        service_level_code=service_code,
    )
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error", "Place order failed")})
    _set_courier_state(company, order_id, supplier, {"order_placed": result})
    return jsonify({"ok": True, "waybill_number": result.get("waybill_number", "")})


@app.route("/api/orders/courier/status")
def api_orders_courier_status():
    """Get courier workflow status for supplier."""
    company = (request.args.get("company") or "").strip()
    order_id = request.args.get("order_id") or request.args.get("orderId") or ""
    supplier = (request.args.get("supplier") or "").strip()
    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})
    state = _get_courier_state(company, order_id, supplier)
    return jsonify({"ok": True, "goods_arrived": state.get("goods_arrived", False), "quote": state.get("quote"), "order_placed": state.get("order_placed")})


@app.route("/api/orders/run/start", methods=["POST"])
def api_orders_run_start():
    """Start manual supplier-order run. Requires company, order_id, supplier, items (from frontend)."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()
    items = data.get("items") or []

    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({"ok": False, "error": "items required (non-empty list)"})

    # Filter items with supplier_slug (normalize for matching)
    supplier_slug = supplier.strip().lower()
    items = [i for i in items if (i.get("supplier_slug") or i.get("supplierSlug") or "").strip().lower() == supplier_slug]
    if not items:
        return jsonify({"ok": False, "error": "No items for this supplier"})

    order_number = data.get("order_number") or data.get("orderNumber") or order_id

    run_data = {
        "items": items,
        "current_index": 0,
        "state": "adding",
        "order_number": order_number,
    }
    _set_order_run(company, order_id, supplier, run_data)
    current = items[0]
    return jsonify({
        "ok": True,
        "current_item": current,
        "current_index": 0,
        "total": len(items),
        "state": "adding",
    })


@app.route("/api/orders/run/next", methods=["POST"])
def api_orders_run_next():
    """Advance to next product in current run."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()

    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})

    run = _get_order_run(company, order_id, supplier)
    if not run:
        return jsonify({"ok": False, "error": "No active run"})

    idx = run["current_index"] + 1
    items = run["items"]
    if idx >= len(items):
        run["state"] = "paying"
        run["current_index"] = len(items) - 1
        _set_order_run(company, order_id, supplier, run)
        return jsonify({
            "ok": True,
            "current_item": None,
            "current_index": idx - 1,
            "total": len(items),
            "state": "paying",
            "message": "All items added. Go to cart and pay on the supplier site.",
        })

    run["current_index"] = idx
    _set_order_run(company, order_id, supplier, run)
    return jsonify({
        "ok": True,
        "current_item": items[idx],
        "current_index": idx,
        "total": len(items),
        "state": "adding",
    })


@app.route("/api/orders/run/goto-cart", methods=["POST"])
def api_orders_run_goto_cart():
    """Mark state as 'paying' (user has gone to cart)."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()

    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})

    run = _get_order_run(company, order_id, supplier)
    if not run:
        return jsonify({"ok": False, "error": "No active run"})

    run["state"] = "paying"
    _set_order_run(company, order_id, supplier, run)
    return jsonify({"ok": True, "state": "paying"})


@app.route("/api/orders/run/finish", methods=["POST"])
def api_orders_run_finish():
    """Finish supplier-order run (manual completion)."""
    data = request.get_json(silent=True) or {}
    company = (data.get("company") or "").strip()
    order_id = data.get("order_id") or data.get("orderId") or ""
    supplier = (data.get("supplier") or "").strip()

    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})

    _clear_order_run(company, order_id, supplier)
    return jsonify({"ok": True, "state": "done"})


@app.route("/api/orders/run/status")
def api_orders_run_status():
    """Get current run status for supplier."""
    company = (request.args.get("company") or "").strip()
    order_id = request.args.get("order_id") or request.args.get("orderId") or ""
    supplier = (request.args.get("supplier") or "").strip()

    if not company or not order_id or not supplier:
        return jsonify({"ok": False, "error": "company, order_id, and supplier required"})

    run = _get_order_run(company, order_id, supplier)
    if not run:
        return jsonify({"ok": False, "running": False})

    items = run["items"]
    idx = run["current_index"]
    return jsonify({
        "ok": True,
        "running": True,
        "state": run["state"],
        "current_index": idx,
        "total": len(items),
        "current_item": items[idx] if 0 <= idx < len(items) else None,
        "order_number": run.get("order_number") or order_id,
    })


def _supplier_has_session(supplier_slug: str) -> bool:
    """Check if supplier has saved session (JSON or chrome_profile)."""
    slug = (supplier_slug or "").strip().lower().replace("-", "").replace("_", "")
    # JSON session files per SUPPLIER_SESSIONS.md
    json_sessions = {
        "makro": "makro/makro_session.json",
        "matrixwarehouse": "matrixwarehouse/matrixwarehouse_session.json",
        "loot": "loot/loot_session.json",
        "perfectdealz": "perfectdealz/perfectdealz_session.json",
        "aliexpress": "aliexpress/aliexpress_session.json",
        "ubuy": "ubuy/ubuy_session.json",
        "myrunway": "myrunway/myrunway_session.json",
        "onedayonly": "onedayonly/onedayonly_session.json",
    }
    # Persistent Chrome profiles
    chrome_profiles = {
        "takealot": "takealot/chrome_profile",
        "game": "game/chrome_profile",
        "constructionhyper": "constructionhyper/chrome_profile",
        "temu": "temu/chrome_profile",
        "gumtree": "gumtree/chrome_profile",
    }
    if slug in json_sessions:
        p = PRODUCTS_ROOT / json_sessions[slug]
        return p.exists() and p.stat().st_size > 0
    if slug in chrome_profiles:
        p = PRODUCTS_ROOT / chrome_profiles[slug]
        return p.exists() and p.is_dir()
    return False


@app.route("/api/orders/run/session-check")
def api_orders_run_session_check():
    """Check if supplier has a saved session for manual ordering."""
    supplier = (request.args.get("supplier") or "").strip()
    if not supplier:
        return jsonify({"ok": False, "error": "supplier required"})
    has_session = _supplier_has_session(supplier)
    return jsonify({"ok": True, "supplier": supplier, "has_session": has_session})


def _run_yoco_session():
    """Run Yoco login browser with persistent profile. Blocks until close flag."""
    global yoco_running
    profile_path = os.environ.get("YOCO_PROFILE_PATH", "").strip()
    if not profile_path:
        profile_path = str(PRODUCTS_ROOT / "yoco" / "chrome_profile")
    Path(profile_path).mkdir(parents=True, exist_ok=True)
    try:
        from playwright.sync_api import sync_playwright
        from shared.playwright_utils import CHROMIUM_PERFORMANCE_ARGS, PAGE_LOAD_TIMEOUT
        launch_opts = {
            "user_data_dir": profile_path,
            "headless": False,
            "args": CHROMIUM_PERFORMANCE_ARGS + ["--disable-blink-features=AutomationControlled"],
            "viewport": None,
            "locale": "en-ZA",
        }
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(**launch_opts)
            try:
                pages = context.pages
                page = pages[0] if pages else context.new_page()
                page.goto("https://app.yoco.com/login", wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
                while not yoco_close_flag.is_set():
                    import time
                    time.sleep(0.3)
            finally:
                context.close()
    except Exception as e:
        LOG.exception("Yoco session error: %s", e)
    finally:
        yoco_running = False
        yoco_close_flag.clear()


@app.route("/api/yoco/save-session", methods=["POST"])
def api_yoco_save_session():
    """Start or close Yoco browser session for refund login. Uses persistent chrome profile."""
    global yoco_thread, yoco_running
    if yoco_running:
        yoco_close_flag.set()
        return jsonify({"ok": True, "message": "Closing browser. Session saved to profile."})
    yoco_close_flag.clear()
    yoco_running = True
    yoco_thread = threading.Thread(target=_run_yoco_session, daemon=True)
    yoco_thread.start()
    return jsonify({
        "ok": True,
        "message": "Browser opened. Log in at app.yoco.com, then click Save Yoco session again to close and save.",
    })


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
    company_slug = (request.args.get("company") or "").strip()
    if slug:
        tiers = get_tier_multipliers(slug, company_slug or None)
        # Return raw format for UI: [{threshold, multiplier}, ...]
        raw = [{"threshold": None if t[0] == float("inf") else t[0], "multiplier": t[1]} for t in tiers]
        return jsonify({"tier_multipliers": raw})
    return jsonify(load_scraper_config())


@app.route("/api/scraper-config", methods=["POST"])
def api_scraper_config_post():
    data = request.get_json(silent=True) or {}
    tiers = data.get("tier_multipliers")
    supplier = (data.get("supplier") or "").strip()
    company_slug = (data.get("company") or "").strip() or None
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
    save_supplier_tiers(supplier, normalized, company_slug)
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
    company_slug = (data.get("company") or "").strip() or None
    if not slug:
        LOG.warning("Scrape start rejected: no supplier selected")
        return jsonify({"ok": False, "error": "Select a supplier"})
    info = get_supplier(slug)
    if not info:
        LOG.warning("Scrape start rejected: unknown supplier %s", slug)
        return jsonify({"ok": False, "error": f"Unknown supplier: {slug}"})

    # Suppliers that use tiered markup must have tiers configured (no fallback)
    if slug in SUPPLIERS_USING_TIERED_MARKUP:
        if not get_tier_multipliers(slug, company_slug):
            return jsonify({
                "ok": False,
                "error": "Configure pricing tiers for this supplier first. Select company on Dashboard, expand Tiered markup, add tiers, and Save.",
            })

    debug_mode = bool(data.get("debug"))
    if debug_mode:
        os.environ["LOG_LEVEL"] = "DEBUG"
        os.environ["SCRAPER_DEBUG"] = "1"
        LOG.setLevel(logging.DEBUG)
        LOG.info("Debug mode enabled for this scrape")
    scrape_options = {
        "proxy_enabled": bool(data.get("proxy_enabled")),
        "proxy_country": (data.get("proxy_country") or "ZA").strip().upper()[:2],
        "proxy_server": None,
        "debug": debug_mode,
        "company_slug": company_slug,
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

    from shared.suppliers import get_company_scoped_dir
    output_dir = get_company_scoped_dir(info.output_dir, company_slug) if company_slug else info.output_dir

    def _run():
        global scrape_running
        try:
            print(f"  [scraper] Starting {slug} -> {output_dir}", flush=True)
            LOG.info("Starting scrape: %s -> %s", slug, output_dir)
            run_supplier_scrape(slug, output_dir, scrape_stop_flag, scrape_save_session_flag, scrape_options)
            print(f"  [scraper] Finished {slug}", flush=True)
            LOG.info("Scrape finished: %s", slug)
        except Exception as e:
            print(f"  [scraper] ERROR: {e}", flush=True)
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


# --- Gumtree Crawler ---
def _run_gumtree_crawler():
    """Run gumtree crawler in background thread."""
    global _gumtree_crawler_running
    try:
        from gumtree_crawler.crawler import run_crawl
        from gumtree_crawler.db import init_schema
        init_schema()
        run_crawl(progress_cb=lambda m: LOG.info("[gumtree-crawler] %s", m))
    except Exception as e:
        LOG.exception("Gumtree crawler error: %s", e)
    finally:
        _gumtree_crawler_running = False


def _gumtree_scheduler_loop():
    """Daily scheduler: run crawler once per day at 06:00 local."""
    import time
    from datetime import datetime, timedelta
    while True:
        if not GUMTREE_CRAWLER_SCHEDULER_ENABLED:
            time.sleep(3600)
            continue
        now = datetime.now()
        next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(days=1)
        wait_secs = (next_run - datetime.now()).total_seconds()
        if wait_secs > 0:
            time.sleep(min(wait_secs, 3600))  # wake every hour to recheck
        with _gumtree_crawler_lock:
            if _gumtree_crawler_running:
                time.sleep(3600)
                continue
            _gumtree_crawler_running = True
        _t = threading.Thread(target=_run_gumtree_crawler, daemon=True)
        _t.start()
        _t.join(timeout=7200)  # wait up to 2h for crawler to finish
        time.sleep(3600)  # cooldown before next cycle


if GUMTREE_CRAWLER_SCHEDULER_ENABLED:
    _gumtree_scheduler_thread = threading.Thread(target=_gumtree_scheduler_loop, daemon=True)
    _gumtree_scheduler_thread.start()
    LOG.info("Gumtree crawler daily scheduler enabled")


# --- Makro Crawler ---
def _run_makro_crawler():
    """Run Makro crawler in background thread."""
    global _makro_crawler_running
    cookie_path = os.environ.get("MAKRO_COOKIE_PATH", "").strip() or None
    if not cookie_path and (PRODUCTS_ROOT / "makro" / "makro_session.json").exists():
        cookie_path = str(PRODUCTS_ROOT / "makro" / "makro_session.json")
    try:
        from makro_crawler.crawler import run_crawl
        from makro_crawler.db import init_schema
        init_schema()
        run_crawl(
            progress_cb=lambda m: LOG.info("[makro-crawler] %s", m),
            cookie_path=cookie_path,
        )
    except Exception as e:
        LOG.exception("Makro crawler error: %s", e)
    finally:
        _makro_crawler_running = False


def _makro_scheduler_loop():
    """Daily scheduler: run Makro crawler once per day at 06:30 local."""
    import time
    from datetime import datetime, timedelta
    while True:
        if not MAKRO_CRAWLER_SCHEDULER_ENABLED:
            time.sleep(3600)
            continue
        now = datetime.now()
        next_run = now.replace(hour=6, minute=30, second=0, microsecond=0)
        if now >= next_run:
            next_run = next_run + timedelta(days=1)
        wait_secs = (next_run - datetime.now()).total_seconds()
        if wait_secs > 0:
            time.sleep(min(wait_secs, 3600))
        with _makro_crawler_lock:
            if _makro_crawler_running:
                time.sleep(3600)
                continue
            _makro_crawler_running = True
        _t = threading.Thread(target=_run_makro_crawler, daemon=True)
        _t.start()
        _t.join(timeout=7200)
        time.sleep(3600)


if MAKRO_CRAWLER_SCHEDULER_ENABLED:
    _makro_scheduler_thread = threading.Thread(target=_makro_scheduler_loop, daemon=True)
    _makro_scheduler_thread.start()
    LOG.info("Makro crawler daily scheduler enabled")


@app.route("/gumtree-crawler")
def gumtree_crawler_page():
    """Gumtree crawler UI page."""
    return render_template_string(GUMTREE_CRAWLER_HTML)


@app.route("/makro-crawler")
def makro_crawler_page():
    """Makro crawler UI page."""
    return render_template_string(MAKRO_CRAWLER_HTML)


@app.route("/api/gumtree-crawler/listings")
def api_gumtree_crawler_listings():
    """List crawler listings with filters."""
    from gumtree_crawler.db import list_listings
    category = request.args.get("category", "").strip() or None
    min_price = request.args.get("min_price", type=int)
    max_price = request.args.get("max_price", type=int)
    keyword = request.args.get("keyword", "").strip() or None
    location = request.args.get("location", "").strip() or None
    seller = request.args.get("seller", "").strip() or None
    new_today = request.args.get("new_today", "").lower() in ("1", "true", "yes")
    price_changed = request.args.get("price_changed", "").lower() in ("1", "true", "yes")
    include_ignored = request.args.get("include_ignored", "").lower() in ("1", "true", "yes")
    sort = request.args.get("sort", "last_seen")
    order = request.args.get("order", "desc")
    limit = min(request.args.get("limit", 50, type=int), 200)
    offset = request.args.get("offset", 0, type=int)
    rows, total = list_listings(
        category=category,
        min_price=min_price,
        max_price=max_price,
        keyword=keyword,
        location=location,
        seller=seller,
        new_today=new_today,
        price_changed=price_changed,
        include_ignored=include_ignored,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    # Convert sqlite Row keys to serializable, add images/trend/day_on/province/city/suburb/is_new
    import json
    import re
    from datetime import datetime, timezone

    SA_PROVINCE_HINTS = ("cape", "natal", "kwa", "gauteng", "free state", "limpopo", "mpumalanga", "north west")

    def _parse_location(loc):
        """Parse Gumtree SA location into province, city, suburb. Handles 'Province > Suburb' and 'A, B, C'."""
        if not loc or not isinstance(loc, str):
            return ("", "", "")
        loc = loc.strip()
        if not loc:
            return ("", "", "")
        province = city = suburb = ""
        if " > " in loc or ">" in loc:
            parts = [p.strip() for p in re.split(r"\s*>\s*", loc) if p.strip()]
            if len(parts) >= 2:
                province, suburb = parts[0], parts[1]
            elif len(parts) == 1:
                province = parts[0]
        else:
            parts = [p.strip() for p in loc.split(",") if p.strip()]
            if len(parts) == 3:
                suburb, city, province = parts[0], parts[1], parts[2]
            elif len(parts) == 2:
                last_lower = parts[1].lower()
                is_province = any(h in last_lower for h in SA_PROVINCE_HINTS)
                if is_province:
                    city, province = parts[0], parts[1]
                else:
                    suburb, city = parts[0], parts[1]
            else:
                city = parts[0] if parts else ""
        return (province or "", city or "", suburb or "")

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        # Parse images_json to array
        ij = d.get("images_json")
        d["images"] = json.loads(ij) if (ij and isinstance(ij, str)) else []
        # Trend: up=red, down=green, else neutral
        price = d.get("price")
        prev = d.get("prev_price")
        if prev is not None and price is not None and prev != price:
            d["trend"] = "up" if price > prev else "down"
        else:
            d["trend"] = None
        # Day on Gumtree from first_seen
        fs = d.get("first_seen")
        if fs:
            try:
                dt = datetime.fromisoformat(str(fs).replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt).days
                d["day_on"] = max(0, days)
            except Exception:
                d["day_on"] = None
        else:
            d["day_on"] = None
        # New: first seen today (added in most recent crawl)
        d["is_new"] = bool(fs and str(fs)[:10] == datetime.now().strftime("%Y-%m-%d"))
        # Province, city, suburb for grouping
        prov, city, sub = _parse_location(d.get("location"))
        d["province"] = prov
        d["city"] = city
        d["suburb"] = sub
        return d
    return jsonify({"listings": [_row_to_dict(r) for r in rows], "total": total})


@app.route("/api/gumtree-crawler/changes")
def api_gumtree_crawler_changes():
    """Recent price changes."""
    from gumtree_crawler.db import get_price_changes
    limit = min(request.args.get("limit", 50, type=int), 100)
    changes = get_price_changes(limit=limit)
    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"changes": [_row_to_dict(c) for c in changes]})


@app.route("/api/gumtree-crawler/run-now", methods=["POST"])
def api_gumtree_crawler_run_now():
    """Trigger crawler run immediately."""
    global _gumtree_crawler_running, _gumtree_crawler_thread
    with _gumtree_crawler_lock:
        if _gumtree_crawler_running:
            return jsonify({"ok": False, "error": "Crawl already running"})
        _gumtree_crawler_running = True
    _gumtree_crawler_thread = threading.Thread(target=_run_gumtree_crawler, daemon=True)
    _gumtree_crawler_thread.start()
    return jsonify({"ok": True, "message": "Crawl started"})


@app.route("/api/gumtree-crawler/status")
def api_gumtree_crawler_status():
    """Crawler run status: last run, next run, running."""
    from gumtree_crawler.db import get_last_search_job
    job = get_last_search_job()
    last_run = None
    if job:
        last_run = {
            "id": job.get("id"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "status": job.get("status"),
            "listings_found": job.get("listings_found"),
            "listings_new": job.get("listings_new"),
            "listings_updated": job.get("listings_updated"),
            "error": job.get("error"),
        }
    from datetime import datetime
    now = datetime.now()
    next_run = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now >= next_run:
        from datetime import timedelta
        next_run = next_run + timedelta(days=1)
    return jsonify({
        "running": _gumtree_crawler_running,
        "last_run": last_run,
        "next_scheduled": next_run.isoformat() if GUMTREE_CRAWLER_SCHEDULER_ENABLED else None,
        "scheduler_enabled": GUMTREE_CRAWLER_SCHEDULER_ENABLED,
    })


@app.route("/api/gumtree-crawler/ignore-rules", methods=["GET"])
def api_gumtree_crawler_ignore_rules_get():
    """List ignore rules."""
    from gumtree_crawler.db import list_ignore_rules
    active_only = request.args.get("active_only", "").lower() in ("1", "true", "yes")
    rules = list_ignore_rules(active_only=active_only)
    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"rules": [_row_to_dict(r) for r in rules]})


@app.route("/api/gumtree-crawler/ignore-rules", methods=["POST"])
def api_gumtree_crawler_ignore_rules_post():
    """Create ignore rule."""
    from gumtree_crawler.db import create_ignore_rule, get_ignore_rule
    data = request.get_json(silent=True) or {}
    rule_type = (data.get("rule_type") or "").strip()
    value = (data.get("value") or "").strip()
    if not rule_type or not value:
        return jsonify({"ok": False, "error": "rule_type and value required"})
    if rule_type not in ("url", "ad_id", "title_keyword", "seller"):
        return jsonify({"ok": False, "error": "rule_type must be url, ad_id, title_keyword, or seller"})
    rid = create_ignore_rule(rule_type, value)
    rule = get_ignore_rule(rid)
    return jsonify({"ok": True, "rule": dict(rule) if rule else {"id": rid}})


@app.route("/api/gumtree-crawler/ignore-rules/<int:rule_id>", methods=["GET"])
def api_gumtree_crawler_ignore_rule_get(rule_id):
    """Get ignore rule."""
    from gumtree_crawler.db import get_ignore_rule
    rule = get_ignore_rule(rule_id)
    if not rule:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(rule))


@app.route("/api/gumtree-crawler/ignore-rules/<int:rule_id>", methods=["PATCH"])
def api_gumtree_crawler_ignore_rule_patch(rule_id):
    """Update ignore rule."""
    from gumtree_crawler.db import update_ignore_rule, get_ignore_rule
    data = request.get_json(silent=True) or {}
    rule = update_ignore_rule(
        rule_id,
        rule_type=data.get("rule_type"),
        value=data.get("value"),
        active=data.get("active"),
    )
    if not rule:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True, "rule": dict(rule)})


@app.route("/api/gumtree-crawler/ignore-rules/<int:rule_id>", methods=["DELETE"])
def api_gumtree_crawler_ignore_rule_delete(rule_id):
    """Delete ignore rule."""
    from gumtree_crawler.db import delete_ignore_rule
    if not delete_ignore_rule(rule_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/gumtree-crawler/filters", methods=["GET"])
def api_gumtree_crawler_filters_get():
    """List crawler filters."""
    from gumtree_crawler.db import list_crawler_filters
    filters = list_crawler_filters()
    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"filters": [_row_to_dict(f) for f in filters]})


@app.route("/api/gumtree-crawler/filters", methods=["POST"])
def api_gumtree_crawler_filters_post():
    """Create or update crawler filter."""
    from gumtree_crawler.db import set_crawler_filter
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    value = (data.get("value") or "").strip() or None
    if not key:
        return jsonify({"ok": False, "error": "key required"})
    set_crawler_filter(key, value)
    return jsonify({"ok": True})


@app.route("/api/gumtree-crawler/filters/<key>", methods=["DELETE"])
def api_gumtree_crawler_filter_delete(key):
    """Delete crawler filter."""
    from gumtree_crawler.db import delete_crawler_filter
    if not delete_crawler_filter(key):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/gumtree-crawler/export-to-products", methods=["POST"])
def api_gumtree_crawler_export_to_products():
    """Export selected crawler listings to Gumtree products.json for a company. Includes fetched images."""
    import json
    import shutil
    from gumtree_crawler.db import get_listing_by_id
    from shared.suppliers import get_sources_for_edit, get_company_scoped_dir
    from edit_products import load_products, save_products

    data = request.get_json(silent=True) or {}
    listing_ids = data.get("listing_ids") or []
    company_slug = (data.get("company_slug") or "").strip()
    if not company_slug:
        return jsonify({"ok": False, "error": "company_slug required"})
    if not listing_ids:
        return jsonify({"ok": False, "error": "listing_ids required (non-empty list)"})

    sources = get_sources_for_edit()
    if "gumtree" not in sources:
        return jsonify({"ok": False, "error": "Gumtree source not configured"})

    gumtree_dir = Path(sources["gumtree"])
    company_dir = get_company_scoped_dir(gumtree_dir, company_slug)
    company_images_dir = company_dir / "images"
    company_images_dir.mkdir(parents=True, exist_ok=True)

    products = load_products("gumtree", sources, company_slug)
    existing_urls = {str(p.get("url") or "").strip() for p in products if p.get("url")}

    try:
        from shared.utils import clean_description, first_n_words, remove_special_chars, truncate_name
        from shared.upload import get_compare_at_price
        from gumtree.scrape_gumtree import apply_gumtree_markup
    except ImportError as e:
        return jsonify({"ok": False, "error": f"Import error: {e}"})

    added = 0
    skipped = 0
    for lid in listing_ids:
        if not isinstance(lid, int):
            try:
                lid = int(lid)
            except (TypeError, ValueError):
                continue
        listing = get_listing_by_id(lid)
        if not listing or listing.get("ignored"):
            skipped += 1
            continue
        url = (listing.get("url") or "").strip()
        if not url or url in existing_urls:
            skipped += 1
            continue
        title = listing.get("title") or "Unknown Listing"
        price = listing.get("price") or 0
        ad_id = listing.get("ad_id") or "unknown"
        name = first_n_words(remove_special_chars(title), 5)
        short_desc = truncate_name(title, 150)
        sell_price = apply_gumtree_markup(price) if price else 0
        compare_at_price = get_compare_at_price(sell_price) if sell_price else None

        # Include fetched images: copy from shared to company dir
        image_paths = []
        ij = listing.get("images_json")
        if ij and isinstance(ij, str):
            try:
                image_paths = json.loads(ij)
            except Exception:
                pass
        product_images = []
        for rel_path in image_paths:
            if ".." in rel_path or rel_path.startswith("/"):
                continue
            src = gumtree_dir / rel_path
            if src.exists() and src.is_file():
                dst = company_images_dir / Path(rel_path).name
                try:
                    shutil.copy2(src, dst)
                    product_images.append(f"images/{dst.name}")
                except Exception as e:
                    LOG.warning("Could not copy image %s: %s", rel_path, e)

        product = {
            "url": url,
            "name": name,
            "description": clean_description(listing.get("description") or title)[:2000],
            "short_description": short_desc,
            "price": sell_price,
            "compare_at_price": compare_at_price,
            "cost": float(price),
            "gumtree_price": price,
            "images": product_images,
            "variants": [],
            "in_stock": True,
            "stock_quantity": 1,
            "status": "active",
            "tags": ["vintage"],
            "ad_id": ad_id,
            "location": listing.get("location"),
        }
        products.append(product)
        existing_urls.add(url)
        added += 1

    if added > 0:
        save_products("gumtree", products, sources, company_slug)

    return jsonify({
        "ok": True,
        "added": added,
        "skipped": skipped,
        "message": f"Exported {added} listing(s) to Gumtree products. Open Edit Products → Gumtree to sync to API.",
    })


@app.route("/api/gumtree-crawler/listings/<int:listing_id>", methods=["PATCH"])
def api_gumtree_crawler_listing_patch(listing_id):
    """Update listing (notes, ignored)."""
    from gumtree_crawler.db import patch_listing, get_listing_by_id
    data = request.get_json(silent=True) or {}
    notes = data.get("notes")
    ignored = data.get("ignored")
    if notes is None and ignored is None:
        return jsonify({"error": "notes or ignored required"}), 400
    listing = patch_listing(listing_id, notes=notes, ignored=ignored)
    if not listing:
        return jsonify({"error": "Not found"}), 404
    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"ok": True, "listing": _row_to_dict(listing)})


@app.route("/api/gumtree-crawler/listings/<int:listing_id>/fetch-images", methods=["POST"])
def api_gumtree_crawler_fetch_images(listing_id):
    """Fetch images from listing detail page, download to gumtree/scraped/images/, persist paths."""
    from gumtree_crawler.db import get_listing_by_id, set_listing_images
    from gumtree_crawler.parsers import extract_detail_images
    from shared.suppliers import get_sources_for_edit
    import requests

    listing = get_listing_by_id(listing_id)
    if not listing:
        return jsonify({"ok": False, "error": "Listing not found"}), 404
    url = (listing.get("url") or "").strip()
    if not url or "gumtree" not in url.lower():
        return jsonify({"ok": False, "error": "Invalid listing URL"}), 400

    sources = get_sources_for_edit()
    gumtree_dir = sources.get("gumtree")
    if not gumtree_dir:
        return jsonify({"ok": False, "error": "Gumtree source not configured"}), 500
    images_dir = Path(gumtree_dir) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return jsonify({"ok": False, "error": "Playwright not installed"}), 500

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            import time
            time.sleep(2)
            html = page.content()
            browser.close()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    image_urls = extract_detail_images(html)
    if not image_urls:
        return jsonify({"ok": False, "error": "No images found on page"}), 404

    ad_id = listing.get("ad_id") or "unknown"
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "image/avif,image/webp,*/*",
    })
    saved_paths = []
    for i, img_url in enumerate(image_urls[:10], 1):
        try:
            resp = session.get(img_url, timeout=15)
            resp.raise_for_status()
            ext = ".jpg"
            ct = resp.headers.get("content-type", "")
            if "png" in ct:
                ext = ".png"
            elif "webp" in ct:
                ext = ".webp"
            fname = f"{ad_id}_{i:02d}{ext}"
            out_path = images_dir / fname
            out_path.write_bytes(resp.content)
            saved_paths.append(f"images/{fname}")
        except Exception as e:
            LOG.warning("Could not download image %s: %s", img_url[:50], e)

    if not saved_paths:
        return jsonify({"ok": False, "error": "Could not download any images"}), 500

    set_listing_images(listing_id, saved_paths)
    return jsonify({"ok": True, "count": len(saved_paths), "images": saved_paths})


@app.route("/api/gumtree-crawler/serve-image/<path:subpath>")
def api_gumtree_crawler_serve_image(subpath):
    """Serve fetched images from gumtree/scraped for preview."""
    from shared.suppliers import get_sources_for_edit
    sources = get_sources_for_edit()
    gumtree_dir = sources.get("gumtree")
    if not gumtree_dir:
        return jsonify({"error": "Not found"}), 404
    base = Path(gumtree_dir)
    if ".." in subpath or subpath.startswith("/"):
        return jsonify({"error": "Invalid path"}), 400
    path = base / subpath
    if not path.exists() or not path.is_file():
        return jsonify({"error": "Not found"}), 404
    return send_from_directory(base, subpath, mimetype=None)


# --- Makro Crawler API ---
@app.route("/api/makro-crawler/listings")
def api_makro_crawler_listings():
    """List Makro crawler listings with filters."""
    from makro_crawler.db import list_listings
    category = request.args.get("category", "").strip() or None
    min_price = request.args.get("min_price", type=int)
    max_price = request.args.get("max_price", type=int)
    keyword = request.args.get("keyword", "").strip() or None
    location = request.args.get("location", "").strip() or None
    seller = request.args.get("seller", "").strip() or None
    new_today = request.args.get("new_today", "").lower() in ("1", "true", "yes")
    price_changed = request.args.get("price_changed", "").lower() in ("1", "true", "yes")
    include_ignored = request.args.get("include_ignored", "").lower() in ("1", "true", "yes")
    sort = request.args.get("sort", "last_seen")
    order = request.args.get("order", "desc")
    limit = min(request.args.get("limit", 50, type=int), 200)
    offset = request.args.get("offset", 0, type=int)
    rows, total = list_listings(
        category=category,
        min_price=min_price,
        max_price=max_price,
        keyword=keyword,
        location=location,
        seller=seller,
        new_today=new_today,
        price_changed=price_changed,
        include_ignored=include_ignored,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )

    from datetime import datetime, timezone

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        # Trend: up=red, down=green, else neutral
        price = d.get("price")
        prev = d.get("prev_price")
        if prev is not None and price is not None and prev != price:
            d["trend"] = "up" if price > prev else "down"
        else:
            d["trend"] = None
        # Day on site from first_seen
        fs = d.get("first_seen")
        if fs:
            try:
                dt = datetime.fromisoformat(str(fs).replace("Z", "+00:00"))
                days = (datetime.now(timezone.utc) - dt).days
                d["day_on"] = max(0, days)
            except Exception:
                d["day_on"] = None
        else:
            d["day_on"] = None
        # New: first seen today
        d["is_new"] = bool(fs and str(fs)[:10] == datetime.now().strftime("%Y-%m-%d"))
        return d
    return jsonify({"listings": [_row_to_dict(r) for r in rows], "total": total})


@app.route("/api/makro-crawler/changes")
def api_makro_crawler_changes():
    """Recent Makro price changes."""
    from makro_crawler.db import get_price_changes
    limit = min(request.args.get("limit", 50, type=int), 100)
    changes = get_price_changes(limit=limit)

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"changes": [_row_to_dict(c) for c in changes]})


@app.route("/api/makro-crawler/run-now", methods=["POST"])
def api_makro_crawler_run_now():
    """Trigger Makro crawler run immediately."""
    global _makro_crawler_running, _makro_crawler_thread
    with _makro_crawler_lock:
        if _makro_crawler_running:
            return jsonify({"ok": False, "error": "Crawl already running"})
        _makro_crawler_running = True
    _makro_crawler_thread = threading.Thread(target=_run_makro_crawler, daemon=True)
    _makro_crawler_thread.start()
    return jsonify({"ok": True, "message": "Crawl started"})


@app.route("/api/makro-crawler/status")
def api_makro_crawler_status():
    """Makro crawler run status."""
    from makro_crawler.db import get_last_search_job
    job = get_last_search_job()
    last_run = None
    if job:
        last_run = {
            "id": job.get("id"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "status": job.get("status"),
            "listings_found": job.get("listings_found"),
            "listings_new": job.get("listings_new"),
            "listings_updated": job.get("listings_updated"),
            "error": job.get("error"),
        }
    from datetime import datetime
    now = datetime.now()
    next_run = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if now >= next_run:
        from datetime import timedelta
        next_run = next_run + timedelta(days=1)
    return jsonify({
        "running": _makro_crawler_running,
        "last_run": last_run,
        "next_scheduled": next_run.isoformat() if MAKRO_CRAWLER_SCHEDULER_ENABLED else None,
        "scheduler_enabled": MAKRO_CRAWLER_SCHEDULER_ENABLED,
    })


@app.route("/api/makro-crawler/ignore-rules", methods=["GET"])
def api_makro_crawler_ignore_rules_get():
    """List Makro ignore rules."""
    from makro_crawler.db import list_ignore_rules
    active_only = request.args.get("active_only", "").lower() in ("1", "true", "yes")
    rules = list_ignore_rules(active_only=active_only)

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"rules": [_row_to_dict(r) for r in rules]})


@app.route("/api/makro-crawler/ignore-rules", methods=["POST"])
def api_makro_crawler_ignore_rules_post():
    """Create Makro ignore rule."""
    from makro_crawler.db import create_ignore_rule, get_ignore_rule
    data = request.get_json(silent=True) or {}
    rule_type = (data.get("rule_type") or "").strip()
    value = (data.get("value") or "").strip()
    if not rule_type or not value:
        return jsonify({"ok": False, "error": "rule_type and value required"})
    if rule_type not in ("url", "ad_id", "title_keyword", "seller"):
        return jsonify({"ok": False, "error": "rule_type must be url, ad_id, title_keyword, or seller"})
    rid = create_ignore_rule(rule_type, value)
    rule = get_ignore_rule(rid)
    return jsonify({"ok": True, "rule": dict(rule) if rule else {"id": rid}})


@app.route("/api/makro-crawler/ignore-rules/<int:rule_id>", methods=["GET"])
def api_makro_crawler_ignore_rule_get(rule_id):
    """Get Makro ignore rule."""
    from makro_crawler.db import get_ignore_rule
    rule = get_ignore_rule(rule_id)
    if not rule:
        return jsonify({"error": "Not found"}), 404
    return jsonify(dict(rule))


@app.route("/api/makro-crawler/ignore-rules/<int:rule_id>", methods=["PATCH"])
def api_makro_crawler_ignore_rule_patch(rule_id):
    """Update Makro ignore rule."""
    from makro_crawler.db import update_ignore_rule, get_ignore_rule
    data = request.get_json(silent=True) or {}
    rule = update_ignore_rule(
        rule_id,
        rule_type=data.get("rule_type"),
        value=data.get("value"),
        active=data.get("active"),
    )
    if not rule:
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True, "rule": dict(rule)})


@app.route("/api/makro-crawler/ignore-rules/<int:rule_id>", methods=["DELETE"])
def api_makro_crawler_ignore_rule_delete(rule_id):
    """Delete Makro ignore rule."""
    from makro_crawler.db import delete_ignore_rule
    if not delete_ignore_rule(rule_id):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/makro-crawler/filters", methods=["GET"])
def api_makro_crawler_filters_get():
    """List Makro crawler filters."""
    from makro_crawler.db import list_crawler_filters
    filters = list_crawler_filters()

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"filters": [_row_to_dict(f) for f in filters]})


@app.route("/api/makro-crawler/filters", methods=["POST"])
def api_makro_crawler_filters_post():
    """Create or update Makro crawler filter."""
    from makro_crawler.db import set_crawler_filter
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    value = (data.get("value") or "").strip() or None
    if not key:
        return jsonify({"ok": False, "error": "key required"})
    set_crawler_filter(key, value)
    return jsonify({"ok": True})


@app.route("/api/makro-crawler/filters/<key>", methods=["DELETE"])
def api_makro_crawler_filter_delete(key):
    """Delete Makro crawler filter."""
    from makro_crawler.db import delete_crawler_filter
    if not delete_crawler_filter(key):
        return jsonify({"error": "Not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/makro-crawler/export-to-products", methods=["POST"])
def api_makro_crawler_export_to_products():
    """Export selected Makro crawler listings to Makro products.json for a company. Text/details only."""
    from makro_crawler.db import get_listing_by_id
    from shared.suppliers import get_sources_for_edit
    from edit_products import load_products, save_products

    data = request.get_json(silent=True) or {}
    listing_ids = data.get("listing_ids") or []
    company_slug = (data.get("company_slug") or "").strip()
    if not company_slug:
        return jsonify({"ok": False, "error": "company_slug required"})
    if not listing_ids:
        return jsonify({"ok": False, "error": "listing_ids required (non-empty list)"})

    sources = get_sources_for_edit()
    if "makro" not in sources:
        return jsonify({"ok": False, "error": "Makro source not configured"})

    products = load_products("makro", sources, company_slug)
    existing_urls = {str(p.get("url") or "").strip() for p in products if p.get("url")}

    try:
        from shared.utils import clean_description, first_n_words, remove_special_chars, truncate_name, apply_tiered_markup, calculate_supplier_cost
        from shared.upload import get_compare_at_price
        from shared.config import get_tier_multipliers
    except ImportError as e:
        return jsonify({"ok": False, "error": f"Import error: {e}"})

    if not get_tier_multipliers("makro", company_slug):
        return jsonify({"ok": False, "error": "Configure Makro pricing tiers for this company first (Scrape page → Tiered markup)"})

    added = 0
    skipped = 0
    for lid in listing_ids:
        if not isinstance(lid, int):
            try:
                lid = int(lid)
            except (TypeError, ValueError):
                continue
        listing = get_listing_by_id(lid)
        if not listing or listing.get("ignored"):
            skipped += 1
            continue
        url = (listing.get("url") or "").strip()
        if not url or url in existing_urls:
            skipped += 1
            continue
        title = listing.get("title") or "Unknown Listing"
        price_cents = listing.get("price") or 0
        ad_id = listing.get("ad_id") or "unknown"
        name = first_n_words(remove_special_chars(title), 5)
        short_desc = truncate_name(title, 150)
        sell_price = apply_tiered_markup(price_cents, "makro", company_slug) if price_cents else 0
        compare_at_price = get_compare_at_price(sell_price) if sell_price else None
        cost = calculate_supplier_cost(price_cents, "makro") if price_cents else 0
        product = {
            "url": url.split("?")[0] if url else "",
            "name": name,
            "description": clean_description(listing.get("description") or title)[:2000],
            "short_description": short_desc,
            "price": sell_price,
            "compare_at_price": compare_at_price,
            "cost": float(cost),
            "makro_price": price_cents / 100 if price_cents else 0,
            "images": [],
            "variants": [],
            "in_stock": True,
            "stock_quantity": 0,
            "status": "active",
            "tags": ["makro"],
            "goods_id": ad_id,
        }
        products.append(product)
        existing_urls.add(url)
        added += 1

    if added > 0:
        save_products("makro", products, sources, company_slug)

    return jsonify({
        "ok": True,
        "added": added,
        "skipped": skipped,
        "message": f"Exported {added} listing(s) to Makro products. Open Edit Products → Makro to sync to API.",
    })


@app.route("/api/makro-crawler/listings/<int:listing_id>", methods=["PATCH"])
def api_makro_crawler_listing_patch(listing_id):
    """Update Makro listing (notes, ignored)."""
    from makro_crawler.db import patch_listing, get_listing_by_id
    data = request.get_json(silent=True) or {}
    notes = data.get("notes")
    ignored = data.get("ignored")
    if notes is None and ignored is None:
        return jsonify({"error": "notes or ignored required"}), 400
    listing = patch_listing(listing_id, notes=notes, ignored=ignored)
    if not listing:
        return jsonify({"error": "Not found"}), 404

    def _row_to_dict(r):
        d = dict(r)
        for k, v in d.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat() if v else None
        return d
    return jsonify({"ok": True, "listing": _row_to_dict(listing)})


# Register edit blueprint at /edit
from edit_products import create_edit_blueprint

app.register_blueprint(create_edit_blueprint(), url_prefix="/edit")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--open", action="store_true", help="Open browser")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging (LOG_LEVEL=DEBUG, SCRAPER_DEBUG=1)")
    args = parser.parse_args()
    if args.debug:
        os.environ["LOG_LEVEL"] = "DEBUG"
        os.environ["SCRAPER_DEBUG"] = "1"
        LOG.setLevel(logging.DEBUG)
        print("Debug mode enabled")
    url = f"http://127.0.0.1:{args.port}"
    print(f"Product scrapers: {url}")
    if args.open:
        webbrowser.open(url)
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
