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

# Order-runner state: manual supplier-order workflow per (company, order_id, supplier)
# Key: (company_slug, order_id, supplier_slug). Value: {items, current_index, state, order_number}
_order_run_state: dict = {}
_order_run_lock = threading.Lock()


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
    .order-card h3 { margin: 0 0 0.5rem 0; font-size: 1rem; }
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
  </style>
</head>
<body>
  <div class="top-nav"><a href="/">← Dashboard</a> · <a href="/scrape">Scrape</a></div>
  <h1>Orders</h1>
  <div id="companyBar" style="margin-bottom: 1rem; font-size: 0.9rem; color: #888;"></div>
  <div id="orders"></div>
  <div id="msg" class="msg"></div>
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
          html += '<h3>' + escapeHtml(o.order_number || o.id) + ' — ' + escapeHtml(customer) + '</h3>';
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
      else alert(d.error || 'Failed to start');
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
      else alert(d.error || 'Failed');
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
    initOrders();
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
