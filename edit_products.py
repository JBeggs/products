#!/usr/bin/env python3
"""
Web UI to edit scraped products (Temu, Gumtree, AliExpress).
Run: python edit_products.py [--port 5001]
Open: http://127.0.0.1:5001
"""
import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv
from flask import Blueprint, Flask, jsonify, make_response, render_template_string, request, send_from_directory

load_dotenv(Path(__file__).parent / ".env")

logging.getLogger("werkzeug").setLevel(logging.ERROR)

PRODUCTS_ROOT = Path(__file__).parent

_verify_lock = threading.Lock()
_verify_stop = threading.Event()
_verify_thread = None
_verify_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": None,
    "summary": {"ok": 0, "price_changed": 0, "sold_out": 0, "error": 0, "unsupported": 0},
    "rows": [],
    "sold_out_items": [],
}

_sync_lock = threading.Lock()
_sync_stop = threading.Event()
_sync_thread = None
_sync_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "checking": 0,
    "current": None,
    "summary": {"ok": 0, "error": 0, "skipped": 0},
    "rows": [],
    "synced_items": [],
}


def _valid_sync_dim(x) -> bool:
    if x is None:
        return False
    try:
        return float(x) > 0
    except (TypeError, ValueError):
        return False


def _validate_product_for_sync(prod: dict) -> str | None:
    """Return error message if product cannot sync, else None."""
    if not (prod.get("url") or "").strip():
        return "No URL"
    if prod.get("bundle_items"):
        return None
    if not all(_valid_sync_dim(prod.get(k)) for k in ("dimension_length", "dimension_width", "dimension_height")):
        return "Missing packaging dimensions"
    try:
        w = prod.get("weight")
        if w is None or int(w) <= 0:
            return "Missing weight"
    except (TypeError, ValueError):
        return "Missing weight"
    return None


def _sync_one_product(
    sources: dict,
    source: str,
    index: int,
    company_slug: str,
    category_id: str,
    token: str,
    base_url: str,
    sync_images: bool,
    products_cache: dict[str, list],
    dirty_sources: set[str],
) -> dict:
    """Sync one product. Updates products_cache in place. Returns {status, note, product_id?}."""
    from shared.suppliers import get_company_scoped_dir
    from shared.upload import find_product_by_source_url, update_product, upload_product

    if source not in sources:
        return {"status": "error", "note": "Invalid source", "source": source, "index": index}

    products = products_cache.get(source)
    if products is None:
        products = load_products(source, sources, company_slug)
        products_cache[source] = products

    if index < 0 or index >= len(products):
        return {"status": "error", "note": "Invalid index", "source": source, "index": index}

    prod = products[index]
    name = (prod.get("name") or "").strip()
    err = _validate_product_for_sync(prod)
    if err:
        return {"status": "skipped", "note": err, "source": source, "index": index, "name": name}

    cat = (prod.get("category_id") or "").strip() or category_id
    if not cat:
        return {"status": "skipped", "note": "No category", "source": source, "index": index, "name": name}

    url = (prod.get("url") or "").strip()
    output_dir = sources[source]
    company_dir = get_company_scoped_dir(output_dir, company_slug)

    bundle_items = prod.get("bundle_items")
    products_by_source = None
    if bundle_items:
        is_cross = len(bundle_items) > 0 and isinstance(bundle_items[0], dict)
        if is_cross:
            ref_sources = {it.get("source") for it in bundle_items if it.get("source") in sources}
            products_by_source = {}
            for src in ref_sources:
                if src not in products_cache:
                    products_cache[src] = load_products(src, sources, company_slug)
                products_by_source[src] = products_cache[src]
            for it in bundle_items:
                src, idx = it.get("source"), it.get("index")
                prods = products_by_source.get(src) or []
                if idx is None or idx < 0 or idx >= len(prods):
                    return {"status": "error", "note": f"Bundle invalid item {it}", "source": source, "index": index, "name": name}
                child_pid = (prods[idx].get("production_ids") or {}).get(company_slug)
                if not child_pid:
                    return {"status": "error", "note": "Sync child products first", "source": source, "index": index, "name": name}
        else:
            for idx in bundle_items:
                if idx < 0 or idx >= len(products):
                    return {"status": "error", "note": f"Bundle invalid index {idx}", "source": source, "index": index, "name": name}
                child_pid = (products[idx].get("production_ids") or {}).get(company_slug)
                if not child_pid:
                    return {"status": "error", "note": "Sync child products first", "source": source, "index": index, "name": name}

    production_ids = prod.get("production_ids") or {}
    existing_id = production_ids.get(company_slug)

    if existing_id:
        result = update_product(
            prod, base_url, token, company_slug, existing_id,
            source=source, products=products, products_by_source=products_by_source,
            output_dir=company_dir, sources_dict=sources,
            category_id=cat, sync_images=sync_images,
        )
        if result is True:
            dirty_sources.add(source)
            return {"status": "ok", "note": "Updated", "source": source, "index": index, "name": name, "product_id": existing_id}
        if result == "not_found":
            del production_ids[company_slug]
            prod["production_ids"] = production_ids
            dirty_sources.add(source)
            existing_id = None
        else:
            return {"status": "error", "note": "Update failed", "source": source, "index": index, "name": name}

    if not existing_id:
        found_id = find_product_by_source_url(base_url, token, company_slug, url)
        if found_id:
            result = update_product(
                prod, base_url, token, company_slug, found_id,
                source=source, products=products, products_by_source=products_by_source,
                output_dir=company_dir, sources_dict=sources,
                category_id=cat, sync_images=sync_images,
            )
            if result is True:
                production_ids[company_slug] = found_id
                prod["production_ids"] = production_ids
                dirty_sources.add(source)
                return {"status": "ok", "note": "Updated (matched URL)", "source": source, "index": index, "name": name, "product_id": found_id}
            if result == "not_found":
                del production_ids[company_slug]
                prod["production_ids"] = production_ids
                dirty_sources.add(source)
            else:
                return {"status": "error", "note": "Update failed", "source": source, "index": index, "name": name}

        pid = upload_product(
            prod, company_dir, base_url, token, company_slug, cat,
            products=products, products_by_source=products_by_source,
            sources_dict=sources if products_by_source else None,
            bundle_source=source,
        )
        if pid:
            production_ids[company_slug] = pid
            prod["production_ids"] = production_ids
            dirty_sources.add(source)
            return {"status": "ok", "note": "Created", "source": source, "index": index, "name": name, "product_id": pid}
        return {"status": "error", "note": "Create failed", "source": source, "index": index, "name": name}

    return {"status": "error", "note": "Sync failed", "source": source, "index": index, "name": name}

# Source from supplier registry (single source of truth) - fetched fresh per request
def _get_sources():
    from shared.suppliers import get_sources_for_edit
    return get_sources_for_edit()


def create_edit_blueprint():
    """Create blueprint for edit UI (used by app.py at /edit)."""
    bp = Blueprint("edit", __name__)

    @bp.route("/")
    def index():
        resp = make_response(render_template_string(HTML))
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    def _no_cache(resp):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        return resp

    @bp.route("/api/countries")
    def api_countries():
        """Return countries from Django CRM (public endpoint, no auth)."""
        base_url = (os.environ.get("API_BASE_URL") or "").strip()
        if not base_url:
            return _no_cache(jsonify({"countries": [], "error": "Set API_BASE_URL in .env"}))
        try:
            import requests
            r = requests.get(f"{base_url.rstrip('/')}/v1/countries/", timeout=10)
            r.raise_for_status()
            data = r.json()
            items = data.get("data") or []
            countries = [{"id": c.get("id"), "name": c.get("name", ""), "url_name": c.get("url_name", "")} for c in items if c.get("name")]
            return _no_cache(jsonify({"countries": countries}))
        except Exception as e:
            return _no_cache(jsonify({"countries": [], "error": str(e)}))

    @bp.route("/api/sources")
    def api_sources():
        """Return all suppliers for Edit Products tabs - from get_sources_for_edit (same source as product loading)."""
        from shared.suppliers import get_supplier, get_sources_for_edit
        sources_dict = get_sources_for_edit()
        result = []
        for slug in sources_dict:
            info = get_supplier(slug)
            result.append({"slug": slug, "display_name": (info.display_name if info else slug)})
        return _no_cache(jsonify(result))

    @bp.route("/api/products")
    def api_products():
        sources = _get_sources()
        s = request.args.get("source", "temu")
        company_slug = (request.args.get("company_slug") or "").strip()
        if s not in sources:
            return _no_cache(jsonify({"products": [], "updated": None}))
        if not company_slug:
            return _no_cache(jsonify({"products": [], "updated": None, "error": "company_slug required"}))
        products, updated = load_products_with_meta(s, sources, company_slug)
        return _no_cache(jsonify({"products": products, "updated": updated}))

    def _sync_source_to_company(sources_dict, source: str, company_slug: str, category_id: str) -> tuple[int, list[str]]:
        """Sync all products from one source to company. Returns (synced_count, errors). category_id required."""
        from shared.upload import find_product_by_source_url, get_auth_token, update_product, upload_product

        if not (category_id or "").strip():
            return 0, ["Select a category"]
        base_url = (os.environ.get("API_BASE_URL") or "").strip()
        try:
            from shared.config import get_credentials_for_company
            username, password = get_credentials_for_company(company_slug)
        except ValueError as e:
            return 0, [str(e)]
        use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
        if not base_url or not username or not password:
            return 0, ["Set API_BASE_URL and COMPANY_SLUGS/API_USERNAMES/API_PASSWORDS (or API_USERNAME/API_PASSWORD) in .env"]
        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
        if not token:
            return 0, ["Login failed"]

        synced = 0
        errors = []
        output_dir = sources_dict.get(source)
        if not output_dir:
            return 0, ["Invalid source"]
        from shared.suppliers import get_company_scoped_dir
        company_dir = get_company_scoped_dir(output_dir, company_slug)
        products = load_products(source, sources_dict, company_slug)
        changed = False

        def sync_one(prod):
            nonlocal synced, changed
            url = (prod.get("url") or "").strip()
            if not url:
                return
            bundle_items = prod.get("bundle_items")
            products_by_source = None
            if bundle_items and len(bundle_items) > 0 and isinstance(bundle_items[0], dict):
                ref_sources = {it.get("source") for it in bundle_items if it.get("source") in sources_dict}
                products_by_source = {src: load_products(src, sources_dict, company_slug) for src in ref_sources}
            production_ids = prod.get("production_ids") or {}
            existing_id = production_ids.get(company_slug)
            if existing_id:
                cat = (prod.get("category_id") or "").strip() or category_id
                result = update_product(
                    prod, base_url, token, company_slug, existing_id,
                    source=source, products=products, products_by_source=products_by_source,
                    output_dir=company_dir, sources_dict=sources_dict,
                    category_id=cat,
                )
                if result is True:
                    synced += 1
                elif result == "not_found":
                    del production_ids[company_slug]
                    prod["production_ids"] = production_ids
                    changed = True
                    existing_id = None
                else:
                    return
            if not existing_id:
                # Fallback: try to find existing product by source_url on API (avoids duplicates when production_ids was lost)
                found_id = find_product_by_source_url(base_url, token, company_slug, prod.get("url"))
                if found_id:
                    cat = (prod.get("category_id") or "").strip() or category_id
                    ok = update_product(
                        prod, base_url, token, company_slug, found_id,
                        source=source, products=products, products_by_source=products_by_source,
                        output_dir=company_dir, sources_dict=sources_dict,
                        category_id=cat,
                    )
                    if ok:
                        production_ids[company_slug] = found_id
                        prod["production_ids"] = production_ids
                        changed = True
                        synced += 1
                        # Skip upload - we updated instead
                        return
                cat = (prod.get("category_id") or "").strip() or category_id
                pid = upload_product(
                    prod, company_dir, base_url, token, company_slug, cat,
                    products=products, products_by_source=products_by_source,
                    sources_dict=sources_dict if products_by_source else None,
                    bundle_source=source,
                )
                if pid:
                    production_ids[company_slug] = pid
                    prod["production_ids"] = production_ids
                    changed = True
                    synced += 1
                else:
                    errors.append(f"{prod.get('name', '')[:30]}: Create failed")

        non_bundles = [p for p in products if not p.get("bundle_items")]
        bundles = [p for p in products if p.get("bundle_items")]
        for prod in non_bundles:
            sync_one(prod)
        for prod in bundles:
            sync_one(prod)

        if changed:
            save_products(source, products, sources_dict, company_slug)
        return synced, errors

    @bp.route("/api/save", methods=["POST"])
    def api_save():
        try:
            sources = _get_sources()
            data = request.get_json()
            s = data.get("source", "temu")
            prods = data.get("products", [])
            company_slug = (data.get("company_slug") or "").strip()
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            category_id = (data.get("category_id") or "").strip()
            updated = save_products(s, prods, sources, company_slug)
            result = {"ok": True, "updated": updated}
            return jsonify(result)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/sync-product", methods=["POST"])
    def api_sync_product():
        """Sync single product to production. Creates or updates, stores product_id in production_ids. Saves products first if provided."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            s = data.get("source", "temu")
            index = data.get("index", 0)
            company_slug = (data.get("company_slug") or "").strip()
            category_id = (data.get("category_id") or "").strip()
            prods = data.get("products", [])
            raw_sync_images = data.get("sync_images", True)
            if isinstance(raw_sync_images, str):
                sync_images = raw_sync_images.strip().lower() in ("1", "true", "yes")
            else:
                sync_images = bool(raw_sync_images)
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not company_slug:
                return jsonify({"ok": False, "error": "Select a company"})
            if not category_id:
                return jsonify({"ok": False, "error": "Select a category"})

            if prods:
                save_products(s, prods, sources, company_slug)

            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            try:
                from shared.config import get_credentials_for_company
                username, password = get_credentials_for_company(company_slug)
            except ValueError as e:
                return jsonify({"ok": False, "error": str(e)})
            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            if not base_url or not username or not password:
                return jsonify({"ok": False, "error": "Set API_BASE_URL and credentials for this company in .env"})

            from shared.upload import find_product_by_source_url, get_auth_token, update_product, upload_product
            from shared.suppliers import get_company_scoped_dir

            output_dir = sources[s]
            company_dir = get_company_scoped_dir(output_dir, company_slug)
            products = load_products(s, sources, company_slug)
            if index < 0 or index >= len(products):
                return jsonify({"ok": False, "error": "Invalid index"})
            prod = products[index]
            url = (prod.get("url") or "").strip()
            if not url:
                return jsonify({"ok": False, "error": "Product has no URL"})
            # Require packaging dimensions for sync
            if not prod.get("bundle_items"):
                def _valid_dim(x):
                    if x is None: return False
                    try: return float(x) > 0
                    except (TypeError, ValueError): return False
                if not all(_valid_dim(prod.get(k)) for k in ("dimension_length", "dimension_width", "dimension_height")):
                    return jsonify({"ok": False, "error": "Add packaging dimensions before syncing (Packaging size or Length/Width/Height)"})
                # Require weight for sync (non-bundles)
                try:
                    w = prod.get("weight")
                    if w is None or int(w) <= 0:
                        return jsonify({"ok": False, "error": "Add weight (grams) before syncing"})
                except (TypeError, ValueError):
                    return jsonify({"ok": False, "error": "Add weight (grams) before syncing"})

            bundle_items = prod.get("bundle_items")
            products_by_source = None
            if bundle_items:
                is_cross = len(bundle_items) > 0 and isinstance(bundle_items[0], dict)
                if is_cross:
                    ref_sources = {it.get("source") for it in bundle_items if it.get("source") in sources}
                    products_by_source = {src: load_products(src, sources, company_slug) for src in ref_sources}
                    for it in bundle_items:
                        src, idx = it.get("source"), it.get("index")
                        prods = products_by_source.get(src) or []
                        if idx is None or idx < 0 or idx >= len(prods):
                            return jsonify({"ok": False, "error": f"Bundle has invalid item {it}"})
                        child_pid = (prods[idx].get("production_ids") or {}).get(company_slug)
                        if not child_pid:
                            return jsonify({"ok": False, "error": "Sync child products first"})
                else:
                    for idx in bundle_items:
                        if idx < 0 or idx >= len(products):
                            return jsonify({"ok": False, "error": f"Bundle has invalid index {idx}"})
                        child_pid = (products[idx].get("production_ids") or {}).get(company_slug)
                        if not child_pid:
                            return jsonify({"ok": False, "error": "Sync child products first"})

            import time as _time
            _t0 = _time.time()
            pname = prod.get("name", "")[:40]
            print(f"  [sync] Starting: {pname}...")

            token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
            if not token:
                return jsonify({"ok": False, "error": "Login failed"})
            print(f"  [sync] Auth OK ({_time.time() - _t0:.1f}s)")

            production_ids = prod.get("production_ids") or {}
            existing_id = production_ids.get(company_slug)
            if existing_id:
                print(f"  [sync] Updating existing {existing_id}...")
                cat = (prod.get("category_id") or "").strip() or category_id
                result = update_product(
                    prod, base_url, token, company_slug, existing_id,
                    source=s, products=products, products_by_source=products_by_source,
                    output_dir=company_dir, sources_dict=sources,
                    category_id=cat,
                    sync_images=sync_images,
                )
                if result is True:
                    print(f"  [sync] Done: updated in {_time.time() - _t0:.1f}s")
                    return jsonify({"ok": True, "product_id": existing_id})
                if result == "not_found":
                    print(f"  [sync] Product gone from prod, will re-create")
                    del production_ids[company_slug]
                    prod["production_ids"] = production_ids
                    save_products(s, products, sources, company_slug)
                    existing_id = None
                else:
                    print(f"  [sync] Update failed after {_time.time() - _t0:.1f}s")
                    return jsonify({"ok": False, "error": "Update failed (API error, not deleted). Check logs."})
            if not existing_id:
                print(f"  [sync] Looking up by source URL...")
                found_id = find_product_by_source_url(base_url, token, company_slug, url)
                print(f"  [sync] Lookup done ({_time.time() - _t0:.1f}s) found={found_id}")
                if found_id:
                    cat = (prod.get("category_id") or "").strip() or category_id
                    if update_product(
                        prod, base_url, token, company_slug, found_id,
                        source=s, products=products, products_by_source=products_by_source,
                        output_dir=company_dir, sources_dict=sources,
                        category_id=cat,
                        sync_images=sync_images,
                    ):
                        production_ids[company_slug] = found_id
                        prod["production_ids"] = production_ids
                        save_products(s, products, sources, company_slug)
                        return jsonify({"ok": True, "product_id": found_id})
                print(f"  [sync] Creating new product...")
                pid = upload_product(
                    prod, company_dir, base_url, token, company_slug, category_id,
                    products=products, products_by_source=products_by_source,
                    sources_dict=sources if products_by_source else None,
                    bundle_source=s,
                )
                if pid:
                    production_ids[company_slug] = pid
                    prod["production_ids"] = production_ids
                    save_products(s, products, sources, company_slug)
                    print(f"  [sync] Done: created {pid} in {_time.time() - _t0:.1f}s")
                    return jsonify({"ok": True, "product_id": pid})
                print(f"  [sync] FAILED after {_time.time() - _t0:.1f}s")
                return jsonify({"ok": False, "error": "Create failed (image upload failed). Check terminal."})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    def _get_creds_for_company(cs: str):
        from shared.config import get_credentials_for_company
        return get_credentials_for_company(cs)

    @bp.route("/api/delete-from-production", methods=["POST"])
    def api_delete_from_production():
        """Archive (soft-delete) products on production API, then remove locally. Uses PATCH status=archived. Supports items: [{source, index}, ...] for view-all."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            if not base_url:
                return jsonify({"ok": False, "error": "Set API_BASE_URL in .env"})

            from shared.upload import get_auth_token, deactivate_product_from_api

            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            items = data.get("items")
            if items:
                deactivated = 0
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    products = load_products(s, sources, company_slug)
                    if idx < 0 or idx >= len(products):
                        continue
                    prod = products[idx]
                    production_ids = prod.get("production_ids") or {}
                    for cs, pid in production_ids.items():
                        try:
                            un, pw = _get_creds_for_company(cs)
                        except ValueError:
                            continue
                        if not un or not pw:
                            continue
                        token = get_auth_token(base_url, un, pw, company_slug=cs, use_email=use_email)
                        if token and deactivate_product_from_api(base_url, token, cs, pid):
                            deactivated += 1
                return jsonify({"ok": True, "deleted": deactivated})
            s = data.get("source", "temu")
            indices = data.get("indices", [])
            if isinstance(indices, int):
                indices = [indices]
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not indices:
                return jsonify({"ok": True, "deleted": 0})

            products = load_products(s, sources, company_slug)
            deactivated = 0
            for idx in indices:
                if idx < 0 or idx >= len(products):
                    continue
                prod = products[idx]
                production_ids = prod.get("production_ids") or {}
                for cs, pid in production_ids.items():
                    try:
                        un, pw = _get_creds_for_company(cs)
                    except ValueError:
                        continue
                    if not un or not pw:
                        continue
                    token = get_auth_token(base_url, un, pw, company_slug=cs, use_email=use_email)
                    if token and deactivate_product_from_api(base_url, token, cs, pid):
                        deactivated += 1
            return jsonify({"ok": True, "deleted": deactivated})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/deactivate-from-production", methods=["POST"])
    def api_deactivate_from_production():
        """Set product status to archived (inactive) on production. Keeps products locally."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            if not base_url:
                return jsonify({"ok": False, "error": "Set API_BASE_URL in .env"})

            from shared.upload import get_auth_token, deactivate_product_from_api

            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            items = data.get("items")
            if items:
                deactivated = 0
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    products = load_products(s, sources, company_slug)
                    if idx < 0 or idx >= len(products):
                        continue
                    prod = products[idx]
                    production_ids = prod.get("production_ids") or {}
                    for cs, pid in production_ids.items():
                        try:
                            un, pw = _get_creds_for_company(cs)
                        except ValueError:
                            continue
                        if not un or not pw:
                            continue
                        token = get_auth_token(base_url, un, pw, company_slug=cs, use_email=use_email)
                        if token and deactivate_product_from_api(base_url, token, cs, pid):
                            deactivated += 1
                return jsonify({"ok": True, "deactivated": deactivated})
            s = data.get("source", "temu")
            indices = data.get("indices", [])
            if isinstance(indices, int):
                indices = [indices]
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not indices:
                return jsonify({"ok": True, "deactivated": 0})

            products = load_products(s, sources, company_slug)
            deactivated = 0
            for idx in indices:
                if idx < 0 or idx >= len(products):
                    continue
                prod = products[idx]
                production_ids = prod.get("production_ids") or {}
                for cs, pid in production_ids.items():
                    try:
                        un, pw = _get_creds_for_company(cs)
                    except ValueError:
                        continue
                    if not un or not pw:
                        continue
                    token = get_auth_token(base_url, un, pw, company_slug=cs, use_email=use_email)
                    if token and deactivate_product_from_api(base_url, token, cs, pid):
                        deactivated += 1
            return jsonify({"ok": True, "deactivated": deactivated})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/reactivate-from-production", methods=["POST"])
    def api_reactivate_from_production():
        """Set product status to active on production."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            if not base_url:
                return jsonify({"ok": False, "error": "Set API_BASE_URL in .env"})

            from shared.upload import get_auth_token, reactivate_product_from_api

            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            items = data.get("items")
            if items:
                reactivated = 0
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    products = load_products(s, sources, company_slug)
                    if idx < 0 or idx >= len(products):
                        continue
                    prod = products[idx]
                    production_ids = prod.get("production_ids") or {}
                    for cs, pid in production_ids.items():
                        try:
                            un, pw = _get_creds_for_company(cs)
                        except ValueError:
                            continue
                        if not un or not pw:
                            continue
                        token = get_auth_token(base_url, un, pw, company_slug=cs, use_email=use_email)
                        if token and reactivate_product_from_api(base_url, token, cs, pid):
                            reactivated += 1
                return jsonify({"ok": True, "reactivated": reactivated})
            s = data.get("source", "temu")
            indices = data.get("indices", [])
            if isinstance(indices, int):
                indices = [indices]
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not indices:
                return jsonify({"ok": True, "reactivated": 0})

            products = load_products(s, sources, company_slug)
            reactivated = 0
            for idx in indices:
                if idx < 0 or idx >= len(products):
                    continue
                prod = products[idx]
                production_ids = prod.get("production_ids") or {}
                for cs, pid in production_ids.items():
                    try:
                        un, pw = _get_creds_for_company(cs)
                    except ValueError:
                        continue
                    if not un or not pw:
                        continue
                    token = get_auth_token(base_url, un, pw, company_slug=cs, use_email=use_email)
                    if token and reactivate_product_from_api(base_url, token, cs, pid):
                        reactivated += 1
            return jsonify({"ok": True, "reactivated": reactivated})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/reset-production-ids", methods=["POST"])
    def api_reset_production_ids():
        """Clear production_ids for the selected company so products show as unsynced. Does not delete on prod."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            items = data.get("items")
            if items:
                by_source = {}
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    if s not in by_source:
                        by_source[s] = []
                    by_source[s].append(idx)
                reset_count = 0
                for s, indices in by_source.items():
                    products = load_products(s, sources, company_slug)
                    for idx in indices:
                        if idx < 0 or idx >= len(products):
                            continue
                        prod = products[idx]
                        pids = prod.get("production_ids") or {}
                        if company_slug in pids:
                            del pids[company_slug]
                            prod["production_ids"] = pids if pids else {}
                            reset_count += 1
                    save_products(s, products, sources, company_slug)
                return jsonify({"ok": True, "reset": reset_count})
            s = data.get("source", "temu")
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            products = load_products(s, sources, company_slug)
            reset_count = 0
            for prod in products:
                pids = prod.get("production_ids") or {}
                if company_slug in pids:
                    del pids[company_slug]
                    prod["production_ids"] = pids if pids else {}
                    reset_count += 1
            if reset_count > 0:
                save_products(s, products, sources, company_slug)
            return jsonify({"ok": True, "reset": reset_count})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/refresh-product", methods=["POST"])
    def api_refresh_product():
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            s = data.get("source", "temu")
            index = data.get("index", 0)
            company_slug = (data.get("company_slug") or "").strip()
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            from shared.refresh import refresh_product
            result = refresh_product(s, index, company_slug)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    def _verify_timeout_note(source: str, timeout_s: float) -> str:
        note = f"Timed out after {int(timeout_s)}s"
        if source == "temu":
            note += (
                " — Temu uses real Chrome (port 9223). Keep that window open, complete login/slider "
                "if shown, then retry. Setup: cd products && python temu/setup_verify.py"
            )
        return note

    def _prewarm_temu_verify() -> None:
        from temu.scrape_temu import _get_temu_verify_page

        _get_temu_verify_page()

    def _run_verify_job(targets: list, company_slug: str, delay: float = 0.1) -> None:
        global _verify_state
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

        from shared.refresh import verify_and_save_product
        from shared.verify_timeouts import TEMU_PREWARM_TIMEOUT_S, verify_timeout_for
        from shared.verify_utils import is_supplier_supported

        has_temu = any(t.get("source") == "temu" for t in targets)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="verify-one") as pool:
            if has_temu and not _verify_stop.is_set():
                with _verify_lock:
                    _verify_state["current"] = {
                        "source": "temu",
                        "index": -1,
                        "name": "Opening Temu Chrome (port 9223)…",
                    }
                try:
                    pool.submit(_prewarm_temu_verify).result(timeout=TEMU_PREWARM_TIMEOUT_S)
                except FuturesTimeout:
                    with _verify_lock:
                        _verify_state["rows"].append(
                            {
                                "status": "error",
                                "source": "temu",
                                "index": -1,
                                "name": "Temu Chrome",
                                "note": (
                                    f"Temu Chrome did not become ready within {int(TEMU_PREWARM_TIMEOUT_S)}s. "
                                    "Run: cd products && python temu/setup_verify.py — then complete login/slider."
                                ),
                            }
                        )
                        _verify_state["summary"]["error"] += 1
                except Exception as e:
                    with _verify_lock:
                        _verify_state["rows"].append(
                            {
                                "status": "error",
                                "source": "temu",
                                "index": -1,
                                "name": "Temu Chrome",
                                "note": str(e),
                            }
                        )
                        _verify_state["summary"]["error"] += 1
                finally:
                    with _verify_lock:
                        _verify_state["current"] = None

            for i, target in enumerate(targets):
                if _verify_stop.is_set():
                    break
                src = target["source"]
                idx = target["index"]
                name = target.get("name") or ""
                per_product_timeout = verify_timeout_for(src)
                with _verify_lock:
                    _verify_state["current"] = {"source": src, "index": idx, "name": name}
                    _verify_state["checking"] = i + 1

                if not is_supplier_supported(src):
                    result = {
                        "status": "unsupported",
                        "note": "No price checker for this supplier",
                        "source": src,
                        "index": idx,
                        "name": name,
                    }
                else:
                    try:
                        future = pool.submit(verify_and_save_product, src, idx, company_slug)
                        result = future.result(timeout=per_product_timeout)
                    except FuturesTimeout:
                        result = {
                            "status": "error",
                            "note": _verify_timeout_note(src, per_product_timeout),
                            "source": src,
                            "index": idx,
                            "name": name,
                        }
                    except Exception as e:
                        result = {
                            "status": "error",
                            "note": str(e),
                            "source": src,
                            "index": idx,
                            "name": name,
                        }

                status = result.get("status") or "error"
                with _verify_lock:
                    _verify_state["done"] = i + 1
                    if status in _verify_state["summary"]:
                        _verify_state["summary"][status] += 1
                    row = {
                        "status": status,
                        "source": src,
                        "index": idx,
                        "name": result.get("name") or name,
                        "note": result.get("note") or "",
                    }
                    _verify_state["rows"].append(row)
                    if len(_verify_state["rows"]) > 200:
                        _verify_state["rows"] = _verify_state["rows"][-200:]
                    if status == "sold_out":
                        _verify_state["sold_out_items"].append({"source": src, "index": idx})
                    _verify_state["current"] = None

                if delay > 0 and i + 1 < len(targets) and not _verify_stop.is_set():
                    time.sleep(delay)

        try:
            from temu.scrape_temu import close_temu_verify_session
            from shared.playwright_verify import close_verify_browser

            close_temu_verify_session()
            close_verify_browser()
        except Exception:
            pass

        with _verify_lock:
            _verify_state["running"] = False
            _verify_state["current"] = None

    @bp.route("/api/verify-start", methods=["POST"])
    def api_verify_start():
        global _verify_thread, _verify_state
        try:
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            scope = (data.get("scope") or "all").strip()
            src = (data.get("source") or "").strip()
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            sources = _get_sources()
            if scope == "source":
                if src not in sources:
                    return jsonify({"ok": False, "error": "Invalid source"})
            with _verify_lock:
                if _verify_state.get("running"):
                    return jsonify({"ok": False, "error": "Verification already running"})
            from shared.refresh import collect_verify_targets

            targets = collect_verify_targets(company_slug, scope=scope, source=src or None)
            if not targets:
                return jsonify({"ok": False, "error": "No products with URLs to verify"})

            _verify_stop.clear()
            with _verify_lock:
                _verify_state = {
                    "running": True,
                    "total": len(targets),
                    "done": 0,
                    "checking": 0,
                    "current": None,
                    "summary": {"ok": 0, "price_changed": 0, "sold_out": 0, "error": 0, "unsupported": 0},
                    "rows": [],
                    "sold_out_items": [],
                }

            def _worker():
                _run_verify_job(targets, company_slug)

            _verify_thread = threading.Thread(target=_worker, daemon=True)
            _verify_thread.start()
            return jsonify({"ok": True, "total": len(targets)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/verify-status")
    def api_verify_status():
        with _verify_lock:
            return jsonify(dict(_verify_state))

    @bp.route("/api/verify-stop", methods=["POST"])
    def api_verify_stop():
        _verify_stop.set()
        return jsonify({"ok": True})

    def _run_sync_job(
        targets: list,
        company_slug: str,
        sync_images: bool,
        products_by_source: dict | None = None,
    ) -> None:
        global _sync_state
        from shared.config import get_credentials_for_company
        from shared.upload import get_auth_token

        sources = _get_sources()
        products_cache: dict[str, list] = {}
        dirty_sources: set[str] = set()

        if products_by_source:
            for src, prods in products_by_source.items():
                if src in sources and isinstance(prods, list):
                    save_products(src, prods, sources, company_slug)
                    products_cache[src] = list(prods)

        base_url = (os.environ.get("API_BASE_URL") or "").strip()
        try:
            username, password = get_credentials_for_company(company_slug)
        except ValueError as e:
            with _sync_lock:
                _sync_state["running"] = False
                _sync_state["rows"].append({"status": "error", "note": str(e), "name": "", "source": ""})
            return

        use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
        if not base_url or not username or not password:
            with _sync_lock:
                _sync_state["running"] = False
                _sync_state["rows"].append({"status": "error", "note": "Missing API credentials", "name": "", "source": ""})
            return

        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
        if not token:
            with _sync_lock:
                _sync_state["running"] = False
                _sync_state["rows"].append({"status": "error", "note": "Login failed", "name": "", "source": ""})
            return

        for i, target in enumerate(targets):
            if _sync_stop.is_set():
                break
            src = target["source"]
            idx = target["index"]
            cat = (target.get("category_id") or "").strip()
            name = target.get("name") or ""
            with _sync_lock:
                _sync_state["current"] = {"source": src, "index": idx, "name": name}
                _sync_state["checking"] = i + 1

            result = _sync_one_product(
                sources, src, idx, company_slug, cat, token, base_url, sync_images,
                products_cache, dirty_sources,
            )
            status = result.get("status") or "error"
            with _sync_lock:
                _sync_state["done"] = i + 1
                if status in _sync_state["summary"]:
                    _sync_state["summary"][status] += 1
                row = {
                    "status": status,
                    "source": src,
                    "index": idx,
                    "name": result.get("name") or name,
                    "note": result.get("note") or "",
                }
                _sync_state["rows"].append(row)
                if len(_sync_state["rows"]) > 200:
                    _sync_state["rows"] = _sync_state["rows"][-200:]
                if status == "ok" and result.get("product_id"):
                    _sync_state["synced_items"].append({
                        "source": src,
                        "index": idx,
                        "product_id": result["product_id"],
                    })
                _sync_state["current"] = None

        for src in dirty_sources:
            prods = products_cache.get(src)
            if prods is not None:
                save_products(src, prods, sources, company_slug)

        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["current"] = None

    @bp.route("/api/sync-start", methods=["POST"])
    def api_sync_start():
        global _sync_thread, _sync_state
        try:
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            targets = data.get("targets") or []
            products_by_source = data.get("products_by_source") or {}
            raw_sync_images = data.get("sync_images", True)
            if isinstance(raw_sync_images, str):
                sync_images = raw_sync_images.strip().lower() in ("1", "true", "yes")
            else:
                sync_images = bool(raw_sync_images)
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            if not targets:
                return jsonify({"ok": False, "error": "No products to sync"})
            sources = _get_sources()
            normalized = []
            for t in targets:
                src = (t.get("source") or "").strip()
                if src not in sources:
                    continue
                normalized.append({
                    "source": src,
                    "index": int(t.get("index", 0)),
                    "category_id": (t.get("category_id") or "").strip(),
                    "name": (t.get("name") or "").strip(),
                })
            if not normalized:
                return jsonify({"ok": False, "error": "No valid sync targets"})
            with _sync_lock:
                if _sync_state.get("running"):
                    return jsonify({"ok": False, "error": "Sync already running"})
            _sync_stop.clear()
            with _sync_lock:
                _sync_state = {
                    "running": True,
                    "total": len(normalized),
                    "done": 0,
                    "checking": 0,
                    "current": None,
                    "summary": {"ok": 0, "error": 0, "skipped": 0},
                    "rows": [],
                    "synced_items": [],
                }

            def _worker():
                _run_sync_job(normalized, company_slug, sync_images, products_by_source or None)

            _sync_thread = threading.Thread(target=_worker, daemon=True)
            _sync_thread.start()
            return jsonify({"ok": True, "total": len(normalized)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/sync-status")
    def api_sync_status():
        with _sync_lock:
            return jsonify(dict(_sync_state))

    @bp.route("/api/sync-stop", methods=["POST"])
    def api_sync_stop():
        _sync_stop.set()
        return jsonify({"ok": True})

    @bp.route("/api/companies")
    def api_companies():
        """Return company slugs for Update Production dropdown."""
        from shared.config import get_target_slugs
        slugs = get_target_slugs()
        return jsonify({"companies": slugs})

    @bp.route("/api/categories")
    def api_categories():
        """Return categories from API for the given company_slug. Required for sync/save."""
        company_slug = (request.args.get("company_slug") or "").strip()
        if not company_slug:
            return jsonify({"categories": [], "error": "company_slug required"})

        base_url = (os.environ.get("API_BASE_URL") or "").strip()
        try:
            from shared.config import get_credentials_for_company
            username, password = get_credentials_for_company(company_slug)
        except ValueError as e:
            return jsonify({"categories": [], "error": str(e)})
        use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
        if not base_url or not username or not password:
            return jsonify({"categories": [], "error": "Set API_BASE_URL and credentials for this company in .env"})

        from shared.upload import get_auth_token
        import requests

        def _rows_from_payload(data):
            """DRF paginated {results, next}, bare list, or {success, data: list|{results}}."""
            if isinstance(data, list):
                return data
            if not isinstance(data, dict):
                return []
            if isinstance(data.get("results"), list):
                return data["results"]
            inner = data.get("data")
            if isinstance(inner, list):
                return inner
            if isinstance(inner, dict):
                if isinstance(inner.get("results"), list):
                    return inner["results"]
                if isinstance(inner.get("data"), list):
                    return inner["data"]
            return []

        def _row_id(row):
            if not isinstance(row, dict):
                return None
            rid = row.get("id")
            if rid is None:
                rid = row.get("pk")
            if rid is None:
                return None
            s = str(rid).strip()
            return s or None

        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
        if not token:
            return jsonify({"categories": [], "error": "Login failed (check credentials or try again — remote API can be slow)"})
        try:
            headers = {"Authorization": f"Bearer {token}", "X-Company-Slug": company_slug}
            base = base_url.rstrip("/")
            next_url = f"{base}/v1/categories/"
            all_rows = []
            seen = set()
            while next_url:
                if next_url in seen:
                    break
                seen.add(next_url)
                r = requests.get(next_url, headers=headers, timeout=30)
                r.raise_for_status()
                payload = r.json()
                all_rows.extend(_rows_from_payload(payload))
                nxt = payload.get("next") if isinstance(payload, dict) else None
                if not nxt:
                    break
                next_url = urljoin(r.url, nxt) if isinstance(nxt, str) else None

            categories = []
            for c in all_rows:
                rid = _row_id(c)
                if not rid:
                    continue
                categories.append(
                    {"id": rid, "name": str(c.get("name") or ""), "slug": str(c.get("slug") or "")}
                )
            return jsonify({"categories": categories})
        except Exception as e:
            return jsonify({"categories": [], "error": str(e)})

    @bp.route("/images/<source>/<path:filename>")
    def serve_image(source, filename):
        sources = _get_sources()
        if source not in sources:
            return "", 404
        base = sources[source]
        company_slug = (request.args.get("company_slug") or "").strip()
        if company_slug:
            from shared.suppliers import get_company_scoped_dir
            company_base = get_company_scoped_dir(base, company_slug)
            path = company_base / filename
            if path.exists():
                return send_from_directory(str(path.parent), path.name)
        path = base / filename
        if path.exists():
            return send_from_directory(str(path.parent), path.name)
        return "", 404

    @bp.route("/api/create-bundle", methods=["POST"])
    def api_create_bundle():
        """Create a bundle product. Accepts either (source, indices) for single-source or items=[{source,index},...] for cross-supplier."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            company_slug = (data.get("company_slug") or "").strip()
            if not company_slug:
                return jsonify({"ok": False, "error": "company_slug required"})
            items_input = data.get("items")
            if items_input:
                if not isinstance(items_input, list) or len(items_input) < 2:
                    return jsonify({"ok": False, "error": "items required (array of {source, index})"})
                items_data = []
                for it in items_input:
                    if not isinstance(it, dict):
                        return jsonify({"ok": False, "error": "Each item must be {source, index}"})
                    src, idx = it.get("source"), it.get("index")
                    if src not in sources or not isinstance(idx, int) or idx < 0:
                        return jsonify({"ok": False, "error": f"Invalid item {it}"})
                    prods = load_products(src, sources, company_slug)
                    if idx >= len(prods):
                        return jsonify({"ok": False, "error": f"Index {idx} out of range for {src}"})
                    if prods[idx].get("bundle_items"):
                        return jsonify({"ok": False, "error": "Cannot bundle a product that is already a bundle"})
                    items_data.append((src, idx, prods[idx]))
                s = items_data[0][0]
                items = [x[2] for x in items_data]
            else:
                s = data.get("source", "temu")
                indices = data.get("indices", [])
                if s not in sources:
                    return jsonify({"ok": False, "error": "Invalid source"})
                if not indices or not isinstance(indices, list):
                    return jsonify({"ok": False, "error": "indices or items required"})
                products = load_products(s, sources, company_slug)
                n = len(products)
                for idx in indices:
                    if not isinstance(idx, int) or idx < 0 or idx >= n:
                        return jsonify({"ok": False, "error": f"Invalid index {idx}"})
                    if products[idx].get("bundle_items"):
                        return jsonify({"ok": False, "error": "Cannot bundle a product that is already a bundle"})
                seen = set()
                unique_indices = [i for i in indices if i not in seen and not seen.add(i)]
                if len(unique_indices) < 2:
                    return jsonify({"ok": False, "error": "Bundle needs at least 2 products"})
                items = [products[i] for i in unique_indices]
                items_data = [(s, i, products[i]) for i in unique_indices]
            total_price = sum(float(p.get("price") or 0) for p in items)
            total_cost = sum(float(p.get("cost") or 0) for p in items)
            # Ensure bundle has margin: cost must be less than price
            if total_price > 0 and (total_cost <= 0 or total_cost >= total_price):
                ratios = []
                for p in items:
                    pr = float(p.get("price") or 0)
                    co = float(p.get("cost") or 0)
                    if pr > 0 and 0 < co < pr:
                        ratios.append(co / pr)
                default_ratio = sum(ratios) / len(ratios) if ratios else 0.6
                total_cost = round(total_price * default_ratio, 2)
            names = [p.get("name") or "Item" for p in items]
            bundle_name = " + ".join(names)[:200]
            images_seen = set()
            combined_images = []
            cross_supplier = items_input is not None
            for src, idx, p in items_data:
                child_paths = []
                main_img = p.get("image")
                if main_img and isinstance(main_img, str) and not str(main_img).lower().startswith("http"):
                    child_paths.append(main_img)
                for img in p.get("images") or []:
                    if img:
                        child_paths.append(img)
                for img in child_paths:
                    raw = img.split("?")[0] if isinstance(img, str) else str(img)
                    raw = raw.strip()
                    if not raw:
                        continue
                    # Dedupe by (supplier, child index, path) so two children never collapse when paths match.
                    key = (src, idx, raw) if cross_supplier else (idx, raw)
                    if key not in images_seen:
                        images_seen.add(key)
                        combined_images.append(f"{src}/{raw}" if cross_supplier else raw)
            if not combined_images:
                return jsonify({"ok": False, "error": "No images in selected products"})
            desc_parts = []
            for i, p in enumerate(items):
                d = (p.get("description") or p.get("name") or "").strip()
                if d and d not in desc_parts:
                    desc_parts.append(d)
            description = "\n\n".join(desc_parts)[:2000] if desc_parts else (items[0].get("description") or bundle_name)[:2000]
            short_desc = (items[0].get("short_description") or bundle_name)[:300]
            url = (items[0].get("url") or "").strip()
            bundle_items_val = [{"source": src, "index": idx} for src, idx, _ in items_data] if cross_supplier else unique_indices
            from shared.suppliers import get_suppliers
            slug_to_name = {x["slug"]: x.get("display_name") or x["slug"] for x in get_suppliers()}
            bundle_item_details = [
                {"source": src, "index": idx, "url": (p.get("url") or "").strip(), "name": (p.get("name") or "Item")[:60]}
                for src, idx, p in items_data
            ]
            for d in bundle_item_details:
                d["supplier_name"] = slug_to_name.get(d["source"], d["source"])
            bundle_product = {
                "name": bundle_name,
                "description": description,
                "short_description": short_desc,
                "price": round(total_price, 2),
                "cost": round(total_cost, 2),
                "compare_at_price": round(total_price * 1.2, 2),
                "images": combined_images,
                "url": url,
                "bundle_items": bundle_items_val,
                "bundle_item_details": bundle_item_details,
                "in_stock": all(p.get("in_stock", True) for p in items),
                "stock_quantity": 0,
                "status": "active",
                "tags": items[0].get("tags", ["imports"]),
                "variants": [],
            }
            if items[0].get("category_name"):
                bundle_product["category_name"] = items[0]["category_name"]
            if items[0].get("category_slug"):
                bundle_product["category_slug"] = items[0]["category_slug"]
            if items[0].get("category_id"):
                bundle_product["category_id"] = items[0]["category_id"]
            products_to_save = load_products(s, sources, company_slug)
            products_to_save.append(bundle_product)
            save_products(s, products_to_save, sources, company_slug)
            return jsonify({"ok": True, "product": bundle_product, "index": len(products_to_save) - 1, "source": s})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    return bp


app = Flask(__name__)
app.register_blueprint(create_edit_blueprint())


def _normalize_pdp_url(url: str) -> str:
    """Stable key for matching the same product across saves."""
    return (url or "").strip().split("?")[0].rstrip("/").lower()


def _merge_sync_state_into_incoming_products(incoming: list, previous: list) -> list:
    """
    Preserve production_ids from the last on-disk snapshot when the client payload
    dropped them (undefined omitted from JSON, partial state, race). Per-URL match.
    Incoming non-empty values win over previous for the same company slug.
    """
    by_url: dict[str, dict] = {}
    for prev in previous:
        if not isinstance(prev, dict):
            continue
        u = _normalize_pdp_url(prev.get("url") or "")
        if u:
            by_url[u] = prev
    out: list = []
    for p in incoming:
        if not isinstance(p, dict):
            out.append(p)
            continue
        merged = dict(p)
        u = _normalize_pdp_url(merged.get("url") or "")
        prev = by_url.get(u) if u else None
        if not prev:
            out.append(merged)
            continue
        prev_ids = prev.get("production_ids")
        if not prev_ids or not isinstance(prev_ids, dict):
            out.append(merged)
            continue
        cur_ids = merged.get("production_ids")
        if cur_ids is None:
            merged["production_ids"] = dict(prev_ids)
        elif isinstance(cur_ids, dict):
            if not cur_ids:
                merged["production_ids"] = dict(prev_ids)
            else:
                combined = dict(prev_ids)
                combined.update(cur_ids)
                merged["production_ids"] = combined
        out.append(merged)
    return out


def _get_products_path(source: str, sources: dict, company_slug: str) -> Path | None:
    """Return path to products.json for company-scoped storage. Returns None if company_slug empty."""
    if not (company_slug or "").strip():
        return None
    from shared.suppliers import get_company_scoped_dir
    base = sources.get(source)
    if not base:
        return None
    return get_company_scoped_dir(base, company_slug) / "products.json"


def load_products(source: str, sources: dict | None = None, company_slug: str = "") -> list:
    products, _ = load_products_with_meta(source, sources, company_slug)
    return products


def _migrate_legacy_to_company_scoped(legacy_path: Path, company_path: Path) -> bool:
    """Copy legacy products.json to company-scoped path once. Returns True if copied."""
    if not legacy_path.exists() or company_path.exists():
        return False
    try:
        data = json.loads(legacy_path.read_text(encoding="utf-8"))
        company_path.parent.mkdir(parents=True, exist_ok=True)
        company_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_products_with_meta(source: str, sources: dict | None = None, company_slug: str = "") -> tuple[list, str | None]:
    """Return (products list, updated timestamp or None). Uses company-scoped path when company_slug set."""
    sources = sources or _get_sources()
    base = sources.get(source)
    if not base:
        return [], None
    path = _get_products_path(source, sources, company_slug) if company_slug else None
    if not path:
        return [], None
    if not path.exists():
        legacy_path = base / "products.json"
        if _migrate_legacy_to_company_scoped(legacy_path, path):
            pass
        else:
            return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("products", []), data.get("updated")
    except Exception:
        return [], None


def save_products(source: str, products: list, sources: dict | None = None, company_slug: str = "") -> str:
    """Save products to company-scoped path. company_slug required."""
    sources = sources or _get_sources()
    base = sources.get(source)
    if not base:
        raise ValueError(f"Invalid source: {source}")
    if not (company_slug or "").strip():
        raise ValueError("company_slug required for save")
    from shared.suppliers import get_company_scoped_dir
    company_dir = get_company_scoped_dir(base, company_slug)
    path = company_dir / "products.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    previous_products = list(data.get("products") or [])
    products = _merge_sync_state_into_incoming_products(products, previous_products)
    data["products"] = products
    from datetime import datetime
    data["updated"] = datetime.now().isoformat()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data["updated"]


HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>Edit Products</title>
  <style>
    * { box-sizing: border-box; }
    html { overflow-x: hidden; }
    body { font-family: system-ui, sans-serif; margin: 1rem; background: #1a1a1a; color: #e0e0e0; max-width: 100vw; overflow-x: hidden; }
    h1 { font-size: 1.25rem; margin-bottom: 1rem; }
    .supplier-panel { margin-bottom: 1rem; max-width: 100%; }
    .supplier-panel-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.4rem; flex-wrap: wrap; }
    .supplier-jump-label { font-size: 0.8rem; color: #888; flex-shrink: 0; }
    .supplier-jump {
      flex: 1 1 12rem;
      min-width: 0;
      max-width: min(28rem, 100%);
      padding: 0.45rem 0.6rem;
      background: #252525;
      border: 1px solid #444;
      border-radius: 6px;
      color: #e0e0e0;
      font-size: 0.9rem;
    }
    .tabs-scroll {
      max-width: 100%;
      overflow-x: auto;
      overflow-y: hidden;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: thin;
      padding-bottom: 0.35rem;
      border-bottom: 1px solid #333;
    }
    .tabs-scroll::-webkit-scrollbar { height: 6px; }
    .tabs-scroll::-webkit-scrollbar-thumb { background: #444; border-radius: 3px; }
    .tabs { display: flex; gap: 0.35rem; flex-wrap: nowrap; margin-bottom: 0; width: max-content; padding: 0.1rem 0; }
    .tabs button {
      padding: 0.4rem 0.65rem;
      background: #333;
      border: 1px solid #555;
      border-radius: 4px;
      color: #e0e0e0;
      cursor: pointer;
      flex-shrink: 0;
      white-space: nowrap;
      font-size: 0.82rem;
    }
    .tabs button.active { background: #2a7; border-color: #2a7; }
    .tabs button:hover { background: #444; }
    .tab-placeholder { color: #888; font-size: 0.9rem; white-space: nowrap; }
    .filter-bar { display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; max-width: 100%; align-items: center; }
    .filter-bar input { padding: 0.4rem 0.6rem; background: #252525; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; min-width: 180px; }
    .last-updated { font-size: 0.85rem; color: #888; align-self: center; }
    .product-count { font-size: 0.85rem; color: #aaa; align-self: center; }
    .source-badge { font-size: 0.7rem; font-weight: 600; color: #2a7; }
    .row { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 0.75rem; background: #252525; border-radius: 6px; margin-bottom: 0.25rem; cursor: pointer; min-width: 0; max-width: 100%; }
    .row:hover { background: #2a2a2a; }
    .row.expanded { flex-wrap: wrap; }
    #products { max-width: 100%; overflow-x: auto; -webkit-overflow-scrolling: touch; }
    /* Floor width so columns do not crush; narrow viewports scroll #products horizontally */
    #products .row { min-width: 32rem; }
    #products .row.list-header { min-width: 32rem; }
    .row .col-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .row .col-cost { width: 70px; font-size: 0.85rem; color: #888; }
    .row .col-price { width: 70px; }
    .row .col-goods { width: 100px; font-size: 0.8rem; color: #666; }
    .row .col-category { width: 120px; flex-shrink: 0; }
    .row .col-category select { padding: 0.25rem 0.4rem; font-size: 0.75rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; width: 100%; max-width: 120px; }
    .row .col-sync { width: 70px; flex-shrink: 0; }
    .row .expand-btn { width: 24px; text-align: center; }
    .list-header { background: #333; color: #888; font-size: 0.75rem; font-weight: 600; cursor: default; margin-bottom: 0.5rem; }
    .list-header:hover { background: #333; }
    .row .expand-panel { width: 100%; padding-top: 1rem; border-top: 1px solid #333; margin-top: 0.5rem; display: none; }
    .row.expanded .expand-panel { display: block; }
    .field { margin-bottom: 0.75rem; }
    .field label { display: block; font-size: 0.75rem; color: #888; margin-bottom: 0.25rem; }
    input, textarea { width: 100%; padding: 0.5rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; }
    .save-btn { padding: 0.5rem 1.5rem; background: #2a7; color: white; border: none; border-radius: 6px; cursor: pointer; margin-top: 1rem; }
    .save-btn:hover { background: #3b8; }
    .reset-btn { padding: 0.5rem 1rem; background: #844; color: white; border: none; border-radius: 6px; cursor: pointer; margin-top: 1rem; margin-left: 0.5rem; font-size: 0.9rem; }
    .reset-btn:hover { background: #a55; }
    .msg { margin-top: 0.5rem; }
    .msg.ok { color: #6c6; }
    .msg.err { color: #c66; }
    .row-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.75rem; }
    .row-actions button { padding: 0.4rem 0.8rem; font-size: 0.85rem; border: none; border-radius: 4px; cursor: pointer; }
    .delete-btn { background: #c44; color: white; }
    .delete-btn:hover { background: #e55; }
    .deactivate-btn { background: #844; color: white; }
    .deactivate-btn:hover { background: #a55; }
    .deactivate-selected { background: #844; color: white; }
    .deactivate-selected:hover { background: #a55; }
    .reactivate-btn { background: #284; color: white; }
    .reactivate-btn:hover { background: #3a5; }
    .reactivate-selected { background: #284; color: white; }
    .reactivate-selected:hover { background: #3a5; }
    .sync-selected { background: #2a5; color: white; }
    .sync-selected:hover { background: #3b6; }
    .sync-selected:disabled { opacity: 0.5; cursor: not-allowed; pointer-events: none; }
    .sync-btn { background: #2a5; color: white; font-size: 0.8rem; padding: 0.3rem 0.6rem; }
    .sync-btn:hover { background: #3b6; }
    .sync-btn:disabled { opacity: 0.5; cursor: not-allowed; pointer-events: none; }
    .sync-status { font-size: 0.75rem; color: #6c6; margin-top: 0.25rem; }
    .refresh-btn { background: #444; color: #e0e0e0; }
    .refresh-btn:hover { background: #555; }
    .refresh-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .price-note { font-size: 0.85rem; padding: 0.3rem 0.5rem; background: #333; border-radius: 4px; margin-top: 0.5rem; }
    .price-note.up { color: #f96; }
    .price-note.down { color: #6cf; }
    .price-note.invalid { color: #c66; }
    .update-section { margin-top: 1.5rem; padding: 1rem; background: #252525; border-radius: 8px; border: 1px solid #333; }
    .update-section label { display: block; font-size: 0.85rem; color: #888; margin-bottom: 0.4rem; }
    .image-thumbs { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.5rem; }
    .image-thumbs .thumb-wrap { position: relative; display: inline-block; }
    .image-thumbs .thumb { width: 64px; height: 64px; object-fit: cover; border-radius: 4px; border: 1px solid #444; }
    .image-thumbs.bundle-images .thumb { width: 128px; height: 128px; }
    .image-thumbs .thumb-remove { position: absolute; top: 2px; right: 2px; width: 20px; height: 20px; padding: 0; font-size: 14px; line-height: 1; background: #c66; color: white; border: none; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; }
    .image-thumbs .thumb-remove:hover { background: #e77; }
    .update-section select { padding: 0.5rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; min-width: 200px; margin-bottom: 0.5rem; }
    .top-nav { margin-bottom: 1rem; }
    .top-nav a { color: #2a7; text-decoration: none; }
    .top-nav a:hover { text-decoration: underline; }
    .supplier-link { display: block; margin-bottom: 0.5rem; color: #2a7; text-decoration: none; font-size: 0.9rem; }
    .supplier-link:hover { text-decoration: underline; }
    .supplier-links { margin-bottom: 0.75rem; }
    .col-select { width: 28px; flex-shrink: 0; }
    .col-select input { cursor: pointer; }
    .select-actions { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; row-gap: 0.35rem; max-width: 100%; }
    .select-actions button { padding: 0.35rem 0.7rem; font-size: 0.85rem; border: none; border-radius: 4px; cursor: pointer; }
    .select-actions .delete-selected { background: #c44; color: white; }
    .select-actions .delete-selected:hover { background: #e55; }
    .select-actions .delete-selected:disabled { opacity: 0.5; cursor: not-allowed; }
    .select-actions .create-bundle-btn { background: #2a7; color: white; }
    .select-actions .create-bundle-btn:hover { background: #3b8; }
    .select-actions .create-bundle-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .view-all-btn { padding: 0.4rem 0.75rem; font-size: 0.9rem; background: #444; color: #e0e0e0; border: 1px solid #555; border-radius: 4px; cursor: pointer; -webkit-tap-highlight-color: transparent; }
    .view-all-btn:hover { background: #555; }
    .verify-all-btn { padding: 0.4rem 0.75rem; font-size: 0.9rem; background: #284; color: white; border: 1px solid #396; border-radius: 4px; cursor: pointer; -webkit-tap-highlight-color: transparent; }
    .verify-all-btn:hover { background: #395; }
    .verify-all-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .verify-modal { background: #252525; border: 1px solid #444; border-radius: 8px; padding: 1.25rem; min-width: 520px; max-width: 95vw; max-height: 85vh; display: flex; flex-direction: column; }
    .verify-modal h3 { margin: 0 0 0.75rem 0; font-size: 1rem; }
    .verify-progress { margin-bottom: 0.75rem; }
    .verify-progress-bar { height: 8px; background: #333; border-radius: 4px; overflow: hidden; margin-top: 0.35rem; }
    .verify-progress-fill { height: 100%; background: #2a7; width: 0%; transition: width 0.3s; }
    .verify-current { font-size: 0.85rem; color: #aaa; margin-bottom: 0.75rem; min-height: 1.2em; }
    .verify-log { overflow-y: auto; max-height: 320px; border: 1px solid #333; border-radius: 4px; font-size: 0.82rem; }
    .verify-log table { width: 100%; border-collapse: collapse; }
    .verify-log th, .verify-log td { padding: 0.35rem 0.5rem; text-align: left; border-bottom: 1px solid #333; }
    .verify-log th { position: sticky; top: 0; background: #2a2a2a; color: #aaa; font-weight: 600; }
    .verify-status-ok { color: #6c6; }
    .verify-status-price_changed { color: #fc6; }
    .verify-status-sold_out { color: #f88; }
    .verify-status-error { color: #f66; }
    .verify-status-unsupported { color: #888; }
    .verify-summary { margin-top: 0.75rem; font-size: 0.9rem; color: #ccc; }
    .verify-modal-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
    .verify-modal-actions button { padding: 0.5rem 1rem; border: none; border-radius: 4px; cursor: pointer; background: #444; color: #e0e0e0; }
    .verify-modal-actions button.primary { background: #2a7; color: white; }
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 1000; }
    .modal-overlay.hidden { display: none; }
    .modal { background: #252525; border: 1px solid #444; border-radius: 8px; padding: 1.25rem; min-width: 320px; max-width: 90vw; }
    .modal h3 { margin: 0 0 0.75rem 0; font-size: 1rem; }
    .modal p { margin: 0 0 1rem 0; color: #aaa; font-size: 0.9rem; }
    .modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
    .modal-actions button { padding: 0.5rem 1rem; border: none; border-radius: 4px; cursor: pointer; }
    .modal-actions .btn-cancel { background: #444; color: #e0e0e0; }
    .modal-actions .btn-cancel:hover { background: #555; }
    .modal-actions .btn-confirm { background: #c44; color: white; }
    .modal-actions .btn-confirm:hover { background: #e55; }
    .modal-actions .btn-skip-images { background: #444; color: #e0e0e0; border: 1px solid #555; }
    .modal-actions .btn-skip-images:hover { background: #555; }
    .modal-actions .btn-sync-images { background: #2a5; color: white; }
    .modal-actions .btn-sync-images:hover { background: #3b6; }
    .modal-extra { margin: 0.75rem 0 0 0; }
    .modal-link { color: #6af; text-decoration: none; }
    .packaging-field .packaging-row { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
    .packaging-field .packaging-select { flex: 1; min-width: 180px; }
    .packaging-field .dimension-preview { display: flex; align-items: center; gap: 0.5rem; }
    .packaging-field .dimension-preview svg { flex-shrink: 0; }
    .packaging-field .dimension-text { font-size: 0.8rem; color: #888; }
    .packaging-field .dimension-text.empty { color: #555; font-style: italic; }
    .modal-link:hover { text-decoration: underline; }
    .company-bar a { color: #2a7; text-decoration: none; }
    .company-bar a:hover { text-decoration: underline; }
    @media (max-width: 640px) {
      body { margin: 0.5rem; padding-bottom: max(2rem, env(safe-area-inset-bottom, 0px)); -webkit-text-size-adjust: 100%; }
      h1 { font-size: 1.1rem; }
      .tabs button { padding: 0.5rem 0.75rem; font-size: 0.9rem; min-height: 44px; -webkit-tap-highlight-color: transparent; }
      .supplier-jump { font-size: 16px; min-height: 44px; max-width: 100%; }
      .filter-bar { flex-direction: column; align-items: stretch; gap: 0.5rem; }
      .filter-bar input { min-width: 0; width: 100%; font-size: 16px; min-height: 44px; }
      .last-updated { font-size: 0.8rem; }
      .select-actions { flex-wrap: wrap; gap: 0.4rem; }
      .select-actions button { min-height: 44px; padding: 0.5rem 0.75rem; -webkit-tap-highlight-color: transparent; }
      .select-actions .delete-selected { flex: 1; min-width: 140px; }
      .view-all-btn { min-height: 44px; -webkit-tap-highlight-color: transparent; }
      .row { flex-wrap: wrap; gap: 0.5rem; padding: 0.6rem 0.5rem; min-height: 44px; }
      .row .col-name { min-width: 0; flex: 1; }
      .row .col-cost { width: auto; }
      .row .col-price { width: auto; }
      .row .col-goods { width: auto; font-size: 0.75rem; }
      .row .col-category { width: 100px; }
      .row .col-category select { font-size: 0.7rem; max-width: 100px; }
      .row .col-sync { width: 60px; }
      .col-select { width: 36px; }
      .col-select input { width: 24px; height: 24px; -webkit-tap-highlight-color: transparent; }
      .row .expand-btn { width: 28px; font-size: 0.9rem; }
      .field input, .field textarea { font-size: 16px; min-height: 44px; padding: 0.6rem; }
      .row-actions button { min-height: 44px; padding: 0.5rem 0.9rem; -webkit-tap-highlight-color: transparent; }
      .save-btn { width: 100%; min-height: 48px; font-size: 1rem; -webkit-tap-highlight-color: transparent; }
      .update-section select { width: 100%; min-width: 0; min-height: 44px; font-size: 16px; }
      .modal { min-width: 0; width: calc(100% - 1rem); margin: 0.5rem; max-width: none; }
      .modal-actions { flex-direction: column-reverse; }
      .modal-actions button { width: 100%; min-height: 48px; font-size: 1rem; -webkit-tap-highlight-color: transparent; }
      .image-thumbs .thumb { width: 56px; height: 56px; }
      .image-thumbs.bundle-images .thumb { width: 112px; height: 112px; }
      .image-thumbs .thumb-remove { width: 24px; height: 24px; font-size: 16px; top: 4px; right: 4px; }
      .supplier-link { padding: 0.5rem 0; min-height: 44px; display: flex; align-items: center; }
    }
  </style>
</head>
<body>
  <div class="top-nav"><a href="/">← Dashboard</a></div>
  <h1>Edit Products</h1>
  <div id="companyBar" class="company-bar" style="margin-bottom: 1rem; font-size: 0.9rem; color: #888;"></div>
  <div id="editSupplierScopeNote" style="font-size:0.85rem;color:#a98;margin:0 0 0.75rem 0;display:none;"></div>
  <div class="supplier-panel">
    <div class="supplier-panel-row">
      <label for="supplierJump" class="supplier-jump-label">Supplier</label>
      <select id="supplierJump" class="supplier-jump" title="Jump to supplier" aria-label="Supplier"></select>
    </div>
    <div class="tabs-scroll">
      <div class="tabs" id="tabs">
        <span class="tab-placeholder">Loading suppliers...</span>
      </div>
    </div>
  </div>
  <div class="filter-bar">
    <input type="text" id="searchName" placeholder="Search name..." oninput="viewAllSuppliers=false; render()">
    <button type="button" class="view-all-btn" id="viewAllBtn">View all</button>
    <button type="button" class="verify-all-btn" id="verifyAllBtn" onclick="startVerifyAll()" title="Check stock and prices from supplier URLs">Verify all</button>
    <button type="button" class="view-all-btn" id="refreshBtn" onclick="refreshProducts()" title="Reload products">Refresh</button>
    <span class="last-updated" id="lastUpdated"></span>
    <span class="product-count" id="productCount"></span>
    <div class="select-actions">
      <button type="button" onclick="selectAll()">Select all</button>
      <button type="button" onclick="deselectAll()">Deselect</button>
      <span id="selectedCount">0</span> selected
      <button type="button" class="create-bundle-btn" id="createBundleBtn" onclick="createBundle()" disabled>Create bundle</button>
      <button type="button" class="deactivate-selected" id="deactivateSelectedBtn" onclick="deactivateSelected()" disabled>Deactivate</button>
      <button type="button" class="reactivate-selected" id="reactivateSelectedBtn" onclick="reactivateSelected()" disabled>Reactivate</button>
      <button type="button" class="sync-selected" id="syncSelectedBtn" onclick="syncSelected()" disabled>Sync</button>
      <button type="button" class="delete-selected" id="deleteSelectedBtn" onclick="deleteSelected()" disabled>Delete</button>
    </div>
  </div>
  <div id="products"></div>
  <div id="modalOverlay" class="modal-overlay hidden">
    <div class="modal">
      <h3 id="modalTitle">Confirm</h3>
      <p id="modalMessage"></p>
      <div id="modalExtra" class="modal-extra" style="display:none;"></div>
      <div class="modal-actions">
        <button type="button" class="btn-cancel" onclick="closeModal()">Cancel</button>
        <button type="button" class="btn-skip-images" id="modalSkipImagesBtn" style="display:none;">Skip images</button>
        <button type="button" class="btn-sync-images" id="modalSyncImagesBtn" style="display:none;">Sync images</button>
        <button type="button" class="btn-confirm" id="modalConfirmBtn">Confirm</button>
      </div>
    </div>
  </div>
  <div id="verifyModalOverlay" class="modal-overlay hidden">
    <div class="verify-modal">
      <h3 id="verifyModalTitle">Checking products…</h3>
      <div class="verify-progress">
        <span id="verifyProgressText">Starting…</span>
        <div class="verify-progress-bar"><div class="verify-progress-fill" id="verifyProgressFill"></div></div>
      </div>
      <div class="verify-current" id="verifyCurrent"></div>
      <div class="verify-log">
        <table>
          <thead><tr><th>Status</th><th>Supplier</th><th>Name</th><th>Note</th></tr></thead>
          <tbody id="verifyLogBody"></tbody>
        </table>
      </div>
      <div class="verify-summary" id="verifySummary"></div>
      <div class="verify-modal-actions">
        <button type="button" id="verifyStopBtn" onclick="stopVerifyAll()">Stop</button>
        <button type="button" class="primary" id="verifyCloseBtn" onclick="closeVerifyModal()" disabled>Close</button>
      </div>
    </div>
  </div>
  <div id="syncModalOverlay" class="modal-overlay hidden">
    <div class="verify-modal">
      <h3 id="syncModalTitle">Syncing products…</h3>
      <div class="verify-progress">
        <span id="syncProgressText">Starting…</span>
        <div class="verify-progress-bar"><div class="verify-progress-fill" id="syncProgressFill"></div></div>
      </div>
      <div class="verify-current" id="syncCurrent"></div>
      <div class="verify-log">
        <table>
          <thead><tr><th>Status</th><th>Supplier</th><th>Name</th><th>Note</th></tr></thead>
          <tbody id="syncLogBody"></tbody>
        </table>
      </div>
      <div class="verify-summary" id="syncSummary"></div>
      <div class="verify-modal-actions">
        <button type="button" id="syncStopBtn" onclick="stopSyncBatch()">Stop</button>
        <button type="button" class="primary" id="syncCloseBtn" onclick="closeSyncModal()" disabled>Close</button>
      </div>
    </div>
  </div>
  <button class="save-btn" id="saveBtn">Save</button>
  <button class="reset-btn" id="resetBtn" title="Clear sync status for selected company — products will show as unsynced">Reset to unsynced</button>
  <div class="msg" id="msg"></div>

  <script>
    let source = 'temu';
    let products = [];
    let lastUpdated = null;
    let expandedIndex = null;
    let selectedIndices = new Set();
    let viewAllSuppliers = false;
    let refreshNotes = {};
    let refreshLoading = {};
    let syncLoading = {};
    let syncNotes = {};
    let syncSelectedRunning = false;
    let sources = [];
    /** Full supplier list from /edit/api/sources before Dashboard company-supplier filter */
    let allEditSources = [];
    let categories = [];
    let verifyPolling = null;
    let verifySoldOutItems = [];
    let syncPolling = null;

    function resetSyncUiState() {
      syncLoading = {};
      syncSelectedRunning = false;
    }

    /**
     * @returns {Promise<boolean|null>} true = sync images, false = skip unless backend missing, null = cancel
     */
    function showImageSyncChoice(title, message) {
      return new Promise(function (resolve) {
        var overlay = document.getElementById('modalOverlay');
        var titleEl = document.getElementById('modalTitle');
        var msgEl = document.getElementById('modalMessage');
        var extraEl = document.getElementById('modalExtra');
        var cancelBtn = overlay && overlay.querySelector('.btn-cancel');
        var confirmBtn = document.getElementById('modalConfirmBtn');
        var skipBtn = document.getElementById('modalSkipImagesBtn');
        var syncBtn = document.getElementById('modalSyncImagesBtn');
        if (!overlay || !titleEl || !msgEl || !cancelBtn || !confirmBtn || !skipBtn || !syncBtn) {
          resolve(true);
          return;
        }
        titleEl.textContent = title || 'Images';
        msgEl.textContent = message || '';
        if (extraEl) {
          extraEl.innerHTML = '';
          extraEl.style.display = 'none';
        }
        confirmBtn.style.display = 'none';
        skipBtn.style.display = '';
        syncBtn.style.display = '';
        overlay.classList.remove('hidden');

        function finish(val) {
          skipBtn.onclick = null;
          syncBtn.onclick = null;
          cancelBtn.onclick = null;
          overlay.onclick = null;
          document.removeEventListener('keydown', onKey);
          confirmBtn.style.display = '';
          skipBtn.style.display = 'none';
          syncBtn.style.display = 'none';
          overlay.classList.add('hidden');
          resolve(val);
        }
        function onKey(e) {
          if (e.key === 'Escape') finish(null);
        }
        skipBtn.onclick = function () { finish(false); };
        syncBtn.onclick = function () { finish(true); };
        cancelBtn.onclick = function () { finish(null); };
        overlay.onclick = function (e) { if (e.target === overlay) finish(null); };
        document.addEventListener('keydown', onKey);
      });
    }

    const TIMED_DURATION_OPTIONS = [
      { value: '', label: '— Not timed' },
      { value: 120, label: '2 hours' },
      { value: 150, label: '2h 30m' },
      { value: 180, label: '3 hours' },
      { value: 210, label: '3h 30m' },
      { value: 240, label: '4 hours' },
      { value: 270, label: '4h 30m' },
      { value: 300, label: '5 hours' },
      { value: 330, label: '5h 30m' },
      { value: 360, label: '6 hours' },
      { value: 390, label: '6h 30m' },
      { value: 420, label: '7 hours' },
      { value: 450, label: '7h 30m' },
      { value: 480, label: '8 hours' },
      { value: 510, label: '8h 30m' },
      { value: 540, label: '9 hours' },
      { value: 570, label: '9h 30m' },
      { value: 600, label: '10 hours' },
    ];

    const SA_PROVINCES = ['Eastern Cape', 'Free State', 'Gauteng', 'KwaZulu-Natal', 'Limpopo', 'Mpumalanga', 'Northern Cape', 'North West', 'Western Cape'];
    let countries = [];

    const PACKAGING_PRESETS = [
      { value: '', label: '— Select or enter manually' },
      { value: '1:10:5', label: '1cm × 10cm × 5cm (thin)', l: 1, w: 10, h: 5 },
      { value: '1:15:10', label: '1cm × 15cm × 10cm (thin)', l: 1, w: 15, h: 10 },
      { value: '2:20:30', label: '2cm × 20cm × 30cm (flat)', l: 2, w: 20, h: 30 },
      { value: '5:10:15', label: '5cm × 10cm × 15cm', l: 5, w: 10, h: 15 },
      { value: '10:10:10', label: '10cm × 10cm × 10cm (square)', l: 10, w: 10, h: 10 },
      { value: '10:15:20', label: '10cm × 15cm × 20cm (rectangular)', l: 10, w: 15, h: 20 },
      { value: '15:15:15', label: '15cm × 15cm × 15cm (square)', l: 15, w: 15, h: 15 },
      { value: '15:20:25', label: '15cm × 20cm × 25cm (rectangular)', l: 15, w: 20, h: 25 },
      { value: '20:25:30', label: '20cm × 25cm × 30cm (large)', l: 20, w: 25, h: 30 },
    ];

    function hasPackagingDimensions(p) {
      const l = p.dimension_length, w = p.dimension_width, h = p.dimension_height;
      return l != null && l > 0 && w != null && w > 0 && h != null && h > 0;
    }

    function getPackagingPresetForProduct(p) {
      if (!hasPackagingDimensions(p)) return '';
      const l = parseFloat(p.dimension_length), w = parseFloat(p.dimension_width), h = parseFloat(p.dimension_height);
      const found = PACKAGING_PRESETS.find(pr => pr.l != null && Math.abs((pr.l || 0) - l) < 0.01 && Math.abs((pr.w || 0) - w) < 0.01 && Math.abs((pr.h || 0) - h) < 0.01);
      return found ? found.value : '';
    }

    function hasWeight(p) {
      if (p.bundle_items) return true;
      const w = p.weight;
      return w != null && !isNaN(parseInt(w, 10)) && parseInt(w, 10) > 0;
    }

    const COMPANY_STORAGE_KEY = 'edit_products_company_slug';
    /** Company slug: prefer the bar dropdown (source of truth in UI), then localStorage. Keeps storage in sync when the select has a value. */
    function getCompany() {
      const sel = document.getElementById('companySelectEdit');
      if (sel) {
        const v = (sel.value || '').trim();
        if (v) {
          try { localStorage.setItem(COMPANY_STORAGE_KEY, v); } catch (e) {}
          return v;
        }
      }
      return (localStorage.getItem(COMPANY_STORAGE_KEY) || '').trim();
    }

    async function fetchCompanySupplierAllowSet(company) {
      const c = (company || '').trim();
      if (!c) return null;
      try {
        const cr = await fetch('/api/company-suppliers?company=' + encodeURIComponent(c));
        const cd = await cr.json();
        const arr = Array.isArray(cd.suppliers) ? cd.suppliers : [];
        if (!arr.length) return null;
        const set = new Set();
        arr.forEach(function (x) {
          const t = String(x || '').trim().toLowerCase();
          if (t) set.add(t);
        });
        return set.size ? set : null;
      } catch (e) {
        return null;
      }
    }

    function applyEditSupplierScopeNote(text) {
      const el = document.getElementById('editSupplierScopeNote');
      if (!el) return;
      el.textContent = text || '';
      el.style.display = text ? 'block' : 'none';
    }

    function filterEditSourcesForCompany(full, allowSet) {
      if (!allowSet) return { list: full.slice(), warn: '' };
      const filtered = full.filter(function (s) {
        return allowSet.has(String(s.slug || '').toLowerCase());
      });
      if (filtered.length) return { list: filtered, warn: '' };
      return {
        list: full.slice(),
        warn: 'Configured suppliers for this company do not match any supplier with saved data; showing all tabs. Update on Dashboard → Configure suppliers.',
      };
    }

    async function rebuildSupplierTabsForCompany() {
      const company = getCompany();
      const allowSet = await fetchCompanySupplierAllowSet(company);
      const { list, warn } = filterEditSourcesForCompany(allEditSources, allowSet);
      sources = list;
      const parts = [];
      if (allowSet && sources.length && !warn) {
        parts.push('Tabs show ' + sources.length + ' supplier(s) configured for this company (Dashboard → Configure suppliers).');
      }
      if (warn) parts.push(warn);
      applyEditSupplierScopeNote(parts.join(' '));

      const tabsEl = document.getElementById('tabs');
      const jump = document.getElementById('supplierJump');
      if (!sources.length) {
        tabsEl.innerHTML = '<span class="tab-placeholder">No suppliers</span>';
        if (jump) { jump.innerHTML = ''; jump.disabled = true; }
        return;
      }
      if (jump) jump.disabled = false;

      const urlSource = new URLSearchParams(window.location.search).get('source');
      let pick = source;
      if (!sources.some(function (s) { return s.slug === pick; })) {
        pick = (urlSource && sources.some(function (s) { return s.slug === urlSource; })) ? urlSource : sources[0].slug;
      }
      source = pick;

      tabsEl.innerHTML = sources.map(function (s) {
        return '<button type="button" class="tab' + (s.slug === source ? ' active' : '') + '" data-source="' + escapeAttr(s.slug) + '">' + escapeHtml(s.display_name || s.slug) + '</button>';
      }).join('');
      tabsEl.querySelectorAll('.tab').forEach(function (b) {
        b.onclick = function () { loadSource(b.dataset.source); };
      });
      if (jump) {
        jump.innerHTML = sources.map(function (s) {
          return '<option value="' + escapeAttr(s.slug) + '">' + escapeHtml(s.display_name || s.slug) + '</option>';
        }).join('');
        jump.value = source;
        jump.onchange = function () { if (jump.value) loadSource(jump.value); };
      }

      if (company) {
        await loadSource(source);
      } else {
        products = [];
        lastUpdated = null;
        resetSyncUiState();
        render();
      }
    }

    async function initTabs() {
      const r = await fetch(cacheBust(apiUrl('api/sources')));
      allEditSources = await r.json();
      loadCategories();
      await rebuildSupplierTabsForCompany();
      loadCountries();
      const viewAllBtn = document.getElementById('viewAllBtn');
      if (viewAllBtn) viewAllBtn.addEventListener('click', viewAll);
    }

    async function loadCountries() {
      try {
        const r = await fetch(apiUrl('api/countries'));
        const d = await r.json();
        const list = d.countries || [];
        const sa = list.find(c => (c.name || '').toLowerCase() === 'south africa');
        countries = sa ? [sa, ...list.filter(c => (c.name || '').toLowerCase() !== 'south africa')] : list;
        render();
      } catch (e) {
        countries = [];
      }
    }

    function apiUrl(path) {
      const base = (document.location.pathname || '/edit').replace(/\\/$/, '') || '/edit';
      return base + (path.startsWith('/') ? path : '/' + path);
    }
    function cacheBust(url) { return url + (url.includes('?') ? '&' : '?') + '_=' + Date.now(); }

    async function loadSource(s) {
      resetSyncUiState();
      source = s;
      viewAllSuppliers = false;
      selectedIndices.clear();
      refreshNotes = {};
      syncNotes = {};
      document.querySelectorAll('.tab').forEach(b => { b.classList.toggle('active', b.dataset.source === source); });
      const jump = document.getElementById('supplierJump');
      if (jump && jump.value !== source) jump.value = source;
      const company = getCompany();
      if (!company) {
        products = [];
        lastUpdated = null;
        resetSyncUiState();
        render();
        return;
      }
      const r = await fetch(cacheBust(apiUrl('api/products?source=' + source + '&company_slug=' + encodeURIComponent(company))));
      const data = await r.json();
      products = data.products || [];
      lastUpdated = data.updated || null;
      render();
    }

    async function loadCategories() {
      const company = getCompany();
      if (!company) {
        categories = [];
        resetSyncUiState();
        render();
        return;
      }
      try {
        const r = await fetch(cacheBust(apiUrl('api/categories?company_slug=' + encodeURIComponent(company))));
        const d = await r.json();
        const cats = d.categories || [];
        if (cats.length) {
          categories = cats;
        } else {
          categories = [];
          const msgEl = document.getElementById('msg');
          if (msgEl && d.error) {
            msgEl.textContent = 'Categories: ' + d.error;
            msgEl.className = 'msg err';
          }
        }
      } catch (e) {
        categories = [];
        const msgEl = document.getElementById('msg');
        if (msgEl) {
          msgEl.textContent = 'Categories failed to load: ' + (e.message || String(e));
          msgEl.className = 'msg err';
        }
      }
      render();
    }


    function getFiltered() {
      const search = ((document.getElementById('searchName') || {}).value || '').toLowerCase().trim();
      return products
        .map((p, i) => ({ p, i }))
        .filter(({ p }) => !search || (p.name || '').toLowerCase().includes(search));
    }

    function escapeHtml(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    function escapeAttr(s) { return escapeHtml(s).replace(/"/g, '&quot;'); }

    async function loadCompanySelector() {
      const bar = document.getElementById('companyBar');
      try {
        const r = await fetch(cacheBust(apiUrl('api/companies')));
        const d = await r.json();
        const companies = d.companies || [];
        if (!companies.length) {
          bar.innerHTML = '<span style="color:#888;">No companies configured (COMPANY_SLUGS). <a href="/">Dashboard</a></span>';
          return;
        }
        bar.innerHTML = '<label for="companySelectEdit" style="margin-right:0.5rem;color:#888;">Company</label>' +
          '<select id="companySelectEdit" style="padding:0.45rem 0.65rem;background:#252525;border:1px solid #444;border-radius:6px;color:#e0e0e0;min-width:160px;font-size:0.95rem;">' +
          '<option value="">Select company</option>' +
          companies.map(function (c) { return '<option value="' + escapeAttr(c) + '">' + escapeHtml(c) + '</option>'; }).join('') +
          '</select> <span style="color:#666;font-size:0.85rem;">(<a href="/" class="modal-link">Dashboard</a>)</span>';
        const sel = document.getElementById('companySelectEdit');
        const saved = getCompany();
        if (saved && companies.indexOf(saved) >= 0) sel.value = saved;
        else if (companies.length === 1) sel.value = companies[0];
        if (saved && companies.indexOf(saved) < 0) localStorage.removeItem(COMPANY_STORAGE_KEY);
        if (sel.value) localStorage.setItem(COMPANY_STORAGE_KEY, sel.value);
        sel.onchange = async function () {
          var v = (sel.value || '').trim();
          if (v) localStorage.setItem(COMPANY_STORAGE_KEY, v); else localStorage.removeItem(COMPANY_STORAGE_KEY);
          loadCategories();
          await rebuildSupplierTabsForCompany();
        };
      } catch (e) {
        bar.innerHTML = '<span style="color:#c66;">Could not load companies. <a href="/" class="modal-link">Dashboard</a></span>';
      }
    }

    function formatTimestamp(iso) {
      if (!iso) return '';
      try {
        const d = new Date(iso);
        return d.toLocaleString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
      } catch (e) { return iso; }
    }

    async function refreshProducts() {
      const btn = document.getElementById('refreshBtn');
      if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
      try {
        if (viewAllSuppliers) await viewAll();
        else await loadSource(source);
      } finally {
        if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
      }
    }

    async function viewAll(e) {
      if (e) { e.preventDefault(); e.stopPropagation(); }
      const company = getCompany();
      if (!company) {
        products = [];
        resetSyncUiState();
        render();
        return;
      }
      resetSyncUiState();
      const btn = document.getElementById('viewAllBtn');
      if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
      viewAllSuppliers = true;
      const search = document.getElementById('searchName');
      if (search) search.value = '';
      const combined = [];
      for (const s of sources) {
        try {
          const r = await fetch(cacheBust(apiUrl('api/products?source=' + encodeURIComponent(s.slug) + '&company_slug=' + encodeURIComponent(company))));
          const data = await r.json();
          const prods = data.products || [];
          prods.forEach((p, j) => combined.push({ ...p, _source: s.slug, _sourceIndex: j }));
        } catch (err) {}
      }
      products = combined;
      lastUpdated = null;
      expandedIndex = null;
      selectedIndices.clear();
      refreshNotes = {};
      document.querySelectorAll('.tab').forEach(b => { b.classList.remove('active'); });
      if (btn) { btn.disabled = false; btn.textContent = 'View all'; }
      render();
    }

    function render() {
      const lu = document.getElementById('lastUpdated');
      if (lu) lu.textContent = lastUpdated ? 'Last updated: ' + formatTimestamp(lastUpdated) : '';
      const sc = document.getElementById('selectedCount');
      const dsb = document.getElementById('deleteSelectedBtn');
      const deactBtn = document.getElementById('deactivateSelectedBtn');
      const reactBtn = document.getElementById('reactivateSelectedBtn');
      if (sc) sc.textContent = selectedIndices.size;
      if (dsb) dsb.disabled = selectedIndices.size === 0;
      if (deactBtn) deactBtn.disabled = selectedIndices.size === 0;
      if (reactBtn) reactBtn.disabled = selectedIndices.size === 0;
      const syncSelBtn = document.getElementById('syncSelectedBtn');
      if (syncSelBtn) syncSelBtn.disabled = selectedIndices.size === 0 || syncSelectedRunning;
      const createBundleBtn = document.getElementById('createBundleBtn');
      if (createBundleBtn) createBundleBtn.disabled = selectedIndices.size < 2;
      const filtered = getFiltered();
      const pc = document.getElementById('productCount');
      if (pc) pc.textContent = filtered.length + ' of ' + products.length + ' products';
      const el = document.getElementById('products');
      const goodsColHeader = viewAllSuppliers ? 'Supplier' : 'ID';
      const headerRow = '<div class="row list-header"><span class="col-select"></span><span class="expand-btn"></span><span class="col-name">Name</span><span class="col-cost">Cost</span><span class="col-price">Price</span><span class="col-goods">' + goodsColHeader + '</span><span class="col-category">Category</span><span class="col-sync">Sync</span></div>';
      el.innerHTML = headerRow + filtered.map(({ p, i }) => {
        const isExpanded = expandedIndex === i;
        const name = escapeAttr(p.name || '');
        const shortDesc = escapeAttr(p.short_description || '');
        const desc = escapeAttr(p.description || '');
        const note = refreshNotes[i];
        const loading = refreshLoading[i];
        const syncLoad = syncLoading[i];
        const syncNote = syncNotes[i];
        const company = getCompany() || '';
        const isSynced = company && (p.production_ids || {})[company];
        const noteClass = note && !note.valid ? 'invalid' : (note && note.price_change_note && note.price_change_note !== 'No change' ? 'up' : '');
        const checked = selectedIndices.has(i) ? ' checked' : '';
        const pSource = p._source || source;
        const supplierName = escapeHtml((sources.find(s => s.slug === pSource) || {}).display_name || pSource);
        const prodCat = p.category_id || '';
        const catOpts = categories.length ? '<option value=""' + (!prodCat ? ' selected' : '') + '>—</option>' + categories.map(c => '<option value="' + escapeAttr(c.id) + '"' + (prodCat === c.id ? ' selected' : '') + '>' + escapeHtml((c.name || c.slug || c.id).slice(0, 20)) + ((c.name || c.slug || '').length > 20 ? '…' : '') + '</option>').join('') : ('<option value="">' + (company ? 'No categories — refresh or check API' : 'Pick company in bar above') + '</option>');
        let linksHtml = '';
        if (p.bundle_items && p.bundle_item_details && p.bundle_item_details.length) {
          linksHtml = p.bundle_item_details.filter(d => (d.url || '').trim()).map(d =>
            '<a href="' + escapeAttr((d.url || '').trim()) + '" target="_blank" rel="noopener" class="supplier-link">View ' + escapeHtml((d.name || 'item').slice(0, 40)) + ' on ' + escapeHtml(d.supplier_name || d.source) + ' →</a>'
          ).join('');
        } else if (p.bundle_items && p.bundle_items.length) {
          const items = p.bundle_items;
          const isCross = typeof items[0] === 'object';
          if (isCross) {
            items.forEach(it => {
              const child = products.find(pp => pp._source === it.source && pp._sourceIndex === it.index);
              if (child && (child.url || '').trim()) {
                const sn = (sources.find(s => s.slug === it.source) || {}).display_name || it.source;
                linksHtml += '<a href="' + escapeAttr((child.url || '').trim()) + '" target="_blank" rel="noopener" class="supplier-link">View on ' + escapeHtml(sn) + ' →</a>';
              }
            });
          } else {
            items.forEach(idx => {
              const child = products[idx];
              if (child && (child.url || '').trim()) {
                linksHtml += '<a href="' + escapeAttr((child.url || '').trim()) + '" target="_blank" rel="noopener" class="supplier-link">View ' + escapeHtml((child.name || 'item').slice(0, 40)) + ' on ' + supplierName + ' →</a>';
              }
            });
          }
        } else if ((p.url || '').trim()) {
          linksHtml = '<a href="' + escapeAttr((p.url || '').trim()) + '" target="_blank" rel="noopener" class="supplier-link">View on ' + supplierName + ' →</a>';
        }
        return `
          <div class="row ${isExpanded ? 'expanded' : ''}" data-index="${i}" onclick="toggleExpand(${i})">
            <span class="col-select" onclick="event.stopPropagation()"><input type="checkbox" ${checked} onchange="toggleSelect(${i}, this.checked)"></span>
            <span class="expand-btn">▶</span>
            <span class="col-name" title="${name}">${p.bundle_items ? '<span class="source-badge" style="margin-right:0.3rem">Bundle</span>' : ''}${(p.timed_duration_minutes != null && p.timed_duration_minutes !== '') ? '<span class="source-badge" style="margin-right:0.3rem;background:#a52">Timed</span>' : ''}${escapeHtml((p.name || '').slice(0, 50))}${(p.name || '').length > 50 ? '…' : ''}</span>
            <span class="col-cost">R${p.cost ?? '?'}</span>
            <span class="col-price">R${p.price ?? '?'}</span>
            <span class="col-goods">${viewAllSuppliers ? '<span class="source-badge">' + supplierName + '</span>' : escapeHtml((p.goods_id || p.ad_id || '').slice(0, 12))}</span>
            <span class="col-category" onclick="event.stopPropagation()"><select onchange="updateField(${i}, 'category_id', this.value || null)">${catOpts}</select></span>
            <span class="col-sync" onclick="event.stopPropagation()"><button class="sync-btn" onclick="syncProduct(${i})" ${syncLoad ? 'disabled' : ''}>${syncLoad ? '…' : (isSynced ? '✓' : 'Sync')}</button></span>
            <div class="expand-panel" onclick="event.stopPropagation()">
              ${linksHtml ? '<div class="supplier-links">' + linksHtml + '</div>' : ''}
              ${(p.images || []).length ? '<div class="field"><label>Images</label><div class="image-thumbs' + (p.bundle_items ? ' bundle-images' : '') + '">' + (p.images || []).map((img, imgIdx) => { const hasSourcePrefix = sources.some(s => (img || '').startsWith(s.slug + '/')); const imgPath = hasSourcePrefix ? 'images/' + img : 'images/' + pSource + '/' + img; const imgSrc = company ? imgPath + (imgPath.includes('?') ? '&' : '?') + 'company_slug=' + encodeURIComponent(company) : imgPath; return '<div class="thumb-wrap"><img src="' + escapeAttr(imgSrc) + '" alt="" class="thumb" onerror="this.style.display=\\'none\\'"><button type="button" class="thumb-remove" onclick="event.stopPropagation(); removeImage(' + i + ',' + imgIdx + ')">×</button></div>'; }).join('') + '</div></div>' : ''}
              <div class="field"><label>Name</label><input type="text" value="${name}" oninput="updateField(${i}, 'name', this.value)"></div>
              <div class="field"><label>Short description</label><input type="text" value="${shortDesc}" oninput="updateField(${i}, 'short_description', this.value)"></div>
              <div class="field"><label>Description</label><textarea rows="4" oninput="updateField(${i}, 'description', this.value)">${desc}</textarea></div>
              <div class="field"><label>Price</label><input type="number" step="0.01" value="${p.price ?? ''}" oninput="updateField(${i}, 'price', parseFloat(this.value) || 0)"></div>
              <div class="field"><label>Cost</label><input type="number" step="0.01" value="${p.cost ?? ''}" oninput="updateField(${i}, 'cost', parseFloat(this.value) || 0)"></div>
              <div class="field"><label>Units (stock)</label><input type="number" min="0" step="1" value="${p.stock_quantity ?? ''}" placeholder="0" oninput="const v = parseInt(this.value, 10); updateField(${i}, 'stock_quantity', isNaN(v) ? 0 : v); updateField(${i}, 'in_stock', !isNaN(v) && v > 0)"></div>
              <div class="field"><label>Delivery time</label><input type="text" value="${escapeAttr(p.delivery_time || '')}" placeholder="e.g. 7-13 days (from supplier)" oninput="updateField(${i}, 'delivery_time', this.value.trim() || null)"></div>
              ${pSource === 'gumtree' ? (function() {
                if (!(p.pickup_province || '').trim()) p.pickup_province = 'Gauteng';
                if (!(p.pickup_country || '').trim()) p.pickup_country = 'South Africa';
                var rawCountry = (p.pickup_country || 'South Africa').trim();
                var defCountry = (rawCountry === 'ZA' || rawCountry === 'ZAF') ? 'South Africa' : rawCountry;
                var countryOpts = countries.length ? countries.map(function(c) { return '<option value="' + escapeAttr(c.name) + '"' + (defCountry === (c.name || '').trim() ? ' selected' : '') + '>' + escapeHtml(c.name || '') + '</option>'; }).join('') : '<option value="South Africa" selected>South Africa</option>';
                var q = String.fromCharCode(39);
                return '<div class="field" style="margin-top:0.75rem;padding-top:0.75rem;border-top:1px solid #333;"><label style="color:#8af;">Gumtree pickup address (required for sync)</label></div>' +
                  '<div class="field"><label>Street</label><input type="text" value="' + escapeAttr(p.pickup_street || '') + '" placeholder="e.g. 123 Main Rd" oninput="updateField(' + i + ', ' + q + 'pickup_street' + q + ', this.value.trim() || null)"></div>' +
                  '<div class="field"><label>Suburb</label><input type="text" value="' + escapeAttr(p.pickup_suburb || '') + '" placeholder="e.g. Fochville" oninput="updateField(' + i + ', ' + q + 'pickup_suburb' + q + ', this.value.trim() || null)"></div>' +
                  '<div class="field"><label>City</label><input type="text" value="' + escapeAttr(p.pickup_city || '') + '" placeholder="e.g. Johannesburg" oninput="updateField(' + i + ', ' + q + 'pickup_city' + q + ', this.value.trim() || null)"></div>' +
                  '<div class="field"><label>Province</label><select onchange="updateField(' + i + ', ' + q + 'pickup_province' + q + ', this.value || null)">' + SA_PROVINCES.map(function(prov) { return '<option value="' + escapeAttr(prov) + '"' + ((p.pickup_province || 'Gauteng') === prov ? ' selected' : '') + '>' + escapeHtml(prov) + '</option>'; }).join('') + '</select></div>' +
                  '<div class="field"><label>Postal code</label><input type="text" value="' + escapeAttr(p.pickup_postal_code || '') + '" placeholder="e.g. 2515" oninput="updateField(' + i + ', ' + q + 'pickup_postal_code' + q + ', this.value.trim() || null)"></div>' +
                  '<div class="field"><label>Country</label><select onchange="updateField(' + i + ', ' + q + 'pickup_country' + q + ', this.value || null)">' + countryOpts + '</select></div>';
              })() : ''}
              <div class="field"><label>Min quantity (lock amount)</label><input type="number" min="1" step="1" value="${p.min_quantity ?? 1}" placeholder="1" oninput="const v = parseInt(this.value, 10); updateField(${i}, 'min_quantity', isNaN(v) || v < 1 ? 1 : v)"></div>
              <div class="field">
                <label>Timed product</label>
                <select onchange="const v = this.value; updateField(${i}, 'timed_duration_minutes', v === '' ? null : parseInt(v, 10)); render()">
                  ${TIMED_DURATION_OPTIONS.map(opt => {
                    const val = opt.value === '' ? '' : String(opt.value);
                    const sel = (val === '' && (p.timed_duration_minutes == null || p.timed_duration_minutes === '')) || (val !== '' && p.timed_duration_minutes != null && String(p.timed_duration_minutes) === val);
                    return '<option value="' + val + '"' + (sel ? ' selected' : '') + '>' + opt.label + '</option>';
                  }).join('')}
                </select>
                <span class="field-hint" style="font-size:0.75rem;color:#888;">Product expires after duration from sync</span>
              </div>
              <div class="field"><label>Weight (g)</label><input type="number" min="0" step="1" value="${p.weight ?? ''}" placeholder="" oninput="const v = parseInt(this.value, 10); updateField(${i}, 'weight', isNaN(v) || v < 0 ? null : v)"></div>
              <div class="field packaging-field">
                <label>Packaging size</label>
                <div class="packaging-row">
                  <select class="packaging-select" onchange="applyPackagingPreset(${i}, this.value)">
                    ${PACKAGING_PRESETS.map(pr => '<option value="' + escapeAttr(pr.value) + '"' + (getPackagingPresetForProduct(p) === pr.value ? ' selected' : '') + '>' + escapeHtml(pr.label) + '</option>').join('')}
                  </select>
                  <div class="dimension-preview" title="L × W × H">${hasPackagingDimensions(p) ? '<svg viewBox="0 0 48 32" width="48" height="32"><rect x="2" y="8" width="20" height="14" fill="none" stroke="#666" stroke-width="1"/><rect x="12" y="2" width="20" height="14" fill="none" stroke="#888" stroke-width="1"/><text x="6" y="18" font-size="6" fill="#aaa">L</text><text x="18" y="12" font-size="6" fill="#aaa">W</text><text x="28" y="8" font-size="6" fill="#aaa">H</text></svg><span class="dimension-text">' + (p.dimension_length || '') + '×' + (p.dimension_width || '') + '×' + (p.dimension_height || '') + ' cm</span>' : '<span class="dimension-text empty">No dimensions</span>'}</div>
                </div>
              </div>
              <div class="field"><label>Length (cm)</label><input type="number" min="0" step="0.01" value="${p.dimension_length ?? ''}" placeholder="" oninput="const v = parseFloat(this.value); updateField(${i}, 'dimension_length', isNaN(v) || v < 0 ? null : v)"></div>
              <div class="field"><label>Width (cm)</label><input type="number" min="0" step="0.01" value="${p.dimension_width ?? ''}" placeholder="" oninput="const v = parseFloat(this.value); updateField(${i}, 'dimension_width', isNaN(v) || v < 0 ? null : v)"></div>
              <div class="field"><label>Height (cm)</label><input type="number" min="0" step="0.01" value="${p.dimension_height ?? ''}" placeholder="" oninput="const v = parseFloat(this.value); updateField(${i}, 'dimension_height', isNaN(v) || v < 0 ? null : v)"></div>
              <div class="row-actions">
                <button class="refresh-btn" onclick="refreshProduct(${i})" ${loading ? 'disabled' : ''}>${loading ? 'Refreshing…' : 'Refresh'}</button>
                ${note && note.valid && (note.new_price !== undefined || note.new_cost !== undefined) ? '<button class="refresh-btn" onclick="applyRefresh(' + i + ')">Apply</button>' : ''}
                ${isSynced ? '<button class="deactivate-btn" onclick="event.stopPropagation(); deactivateProduct(' + i + ')">Deactivate</button><button class="reactivate-btn" onclick="event.stopPropagation(); reactivateProduct(' + i + ')">Reactivate</button>' : ''}
                <button class="delete-btn" onclick="event.stopPropagation(); deleteProduct(${i})">Delete</button>
              </div>
              ${syncNote ? '<div class="sync-status">' + escapeHtml(syncNote) + '</div>' : ''}
              ${note ? '<div class="price-note ' + noteClass + '">' + escapeHtml(note.price_change_note || note.error || '') + '</div>' : ''}
            </div>
          </div>
        `;
      }).join('');
    }

    async function refreshProduct(i) {
      refreshLoading[i] = true;
      render();
      try {
        const p = products[i];
        const src = p && p._source ? p._source : source;
        const idx = p && p._sourceIndex !== undefined ? p._sourceIndex : i;
        const company = getCompany();
        const r = await fetch(apiUrl('api/refresh-product'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: src, index: idx, company_slug: company })
        });
        const data = await r.json();
        const note = data.ok ? data : { valid: false, price_change_note: data.error || 'Failed' };
        refreshNotes[i] = note;
        if (!note.valid) {
          const msg = note.price_change_note || note.error || 'Could not fetch current price.';
          const url = (p && (p.url || '').trim()) || '';
          const supplierName = (sources.find(s => s.slug === src) || {}).display_name || src;
          showRefreshError(msg, url, supplierName);
        }
      } catch (e) {
        refreshNotes[i] = { valid: false, price_change_note: 'Error: ' + e.message };
        showRefreshError('Error: ' + e.message, (products[i] && (products[i].url || '').trim()) || '', (sources.find(s => s.slug === source) || {}).display_name || source);
      }
      refreshLoading[i] = false;
      render();
    }

    function applyRefresh(i) {
      const note = refreshNotes[i];
      if (!note || !products[i]) return;
      if (note.new_price !== undefined) products[i].price = note.new_price;
      if (note.new_cost !== undefined) products[i].cost = note.new_cost;
      const src = products[i]._source || source;
      const sk = (src || '') + '_price';
      if (note.new_source_price !== undefined) products[i][sk] = note.new_source_price;
      delete refreshNotes[i];
      render();
    }

    function stripProductMeta(p) {
      const out = Object.assign({}, p);
      delete out._source;
      delete out._sourceIndex;
      return out;
    }

    function buildProductsBySourceForSync(indices) {
      const bySource = {};
      indices.forEach(function (i) {
        const p = products[i];
        if (!p) return;
        const src = p._source || source;
        if (bySource[src]) return;
        bySource[src] = viewAllSuppliers
          ? products.filter(function (pp) { return pp._source === src; })
              .sort(function (a, b) { return (a._sourceIndex ?? 0) - (b._sourceIndex ?? 0); })
              .map(stripProductMeta)
          : products.map(stripProductMeta);
      });
      return bySource;
    }

    function openSyncModal() {
      const overlay = document.getElementById('syncModalOverlay');
      if (overlay) overlay.classList.remove('hidden');
      document.getElementById('syncModalTitle').textContent = 'Syncing products…';
      document.getElementById('syncProgressText').textContent = 'Starting…';
      document.getElementById('syncProgressFill').style.width = '0%';
      document.getElementById('syncCurrent').textContent = '';
      document.getElementById('syncLogBody').innerHTML = '';
      document.getElementById('syncSummary').textContent = '';
      document.getElementById('syncCloseBtn').disabled = true;
      document.getElementById('syncStopBtn').disabled = false;
    }

    function closeSyncModal() {
      const overlay = document.getElementById('syncModalOverlay');
      if (overlay) overlay.classList.add('hidden');
      if (syncPolling) {
        clearTimeout(syncPolling);
        syncPolling = null;
      }
    }

    function renderSyncStatus(data) {
      const total = data.total || 0;
      const done = data.done || 0;
      const checking = data.checking || 0;
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;
      let progressLabel = done + ' / ' + total + ' synced';
      if (data.running && checking > 0 && done < checking) {
        progressLabel = done + ' / ' + total + ' synced — working on #' + checking;
      }
      document.getElementById('syncProgressText').textContent = progressLabel;
      document.getElementById('syncProgressFill').style.width = pct + '%';
      const cur = data.current;
      const curEl = document.getElementById('syncCurrent');
      if (cur && cur.name) {
        curEl.textContent = 'Syncing: ' + (cur.source || '') + ' — ' + (cur.name || '').slice(0, 60);
      } else if (data.running) {
        curEl.textContent = 'Waiting…';
      } else {
        curEl.textContent = '';
      }
      const tbody = document.getElementById('syncLogBody');
      const rows = data.rows || [];
      tbody.innerHTML = rows.map(function (r) {
        const st = r.status || 'error';
        const cls = 'verify-status-' + st.replace(/[^a-z_]/g, '_');
        return '<tr><td class="' + cls + '">' + escapeHtml(st) + '</td><td>' + escapeHtml(r.source || '') + '</td><td>' + escapeHtml((r.name || '').slice(0, 40)) + '</td><td>' + escapeHtml(r.note || '') + '</td></tr>';
      }).join('');
      if (tbody.lastElementChild) tbody.lastElementChild.scrollIntoView({ block: 'nearest' });
      const s = data.summary || {};
      if (!data.running) {
        document.getElementById('syncModalTitle').textContent = 'Sync complete';
        document.getElementById('syncSummary').textContent =
          'OK: ' + (s.ok || 0) + ', skipped: ' + (s.skipped || 0) + ', errors: ' + (s.error || 0);
        document.getElementById('syncCloseBtn').disabled = false;
        document.getElementById('syncStopBtn').disabled = true;
      }
    }

    async function pollSyncStatus() {
      try {
        const r = await fetch(apiUrl('api/sync-status'));
        const data = await r.json();
        renderSyncStatus(data);
        if (data.running) {
          syncPolling = setTimeout(pollSyncStatus, 500);
        } else {
          syncPolling = null;
          await finishSyncJob(data);
        }
      } catch (e) {
        syncPolling = setTimeout(pollSyncStatus, 2000);
      }
    }

    async function finishSyncJob(data) {
      const items = data.synced_items || [];
      items.forEach(function (item) {
        const src = item.source;
        const idx = item.index;
        const pid = item.product_id;
        let p = null;
        if (viewAllSuppliers) {
          p = products.find(function (pp) { return pp._source === src && pp._sourceIndex === idx; });
        } else if (src === source) {
          p = products[idx];
        }
        if (p && pid) {
          if (!p.production_ids) p.production_ids = {};
          p.production_ids[getCompany()] = pid;
        }
      });
      const msgEl = document.getElementById('msg');
      const s = data.summary || {};
      if (msgEl) {
        msgEl.textContent = 'Synced ' + (s.ok || 0) + '/' + (data.total || 0) +
          ((s.error || 0) ? ('; errors: ' + s.error) : '') +
          ((s.skipped || 0) ? ('; skipped: ' + s.skipped) : '');
        msgEl.className = (s.error || 0) ? 'msg err' : 'msg ok';
      }
      syncSelectedRunning = false;
      render();
    }

    async function stopSyncBatch() {
      try {
        await fetch(apiUrl('api/sync-stop'), { method: 'POST' });
        document.getElementById('syncStopBtn').disabled = true;
      } catch (e) {}
    }

    function openVerifyModal() {
      const overlay = document.getElementById('verifyModalOverlay');
      if (overlay) overlay.classList.remove('hidden');
      document.getElementById('verifyModalTitle').textContent = 'Checking products…';
      document.getElementById('verifyProgressText').textContent = 'Starting…';
      document.getElementById('verifyProgressFill').style.width = '0%';
      document.getElementById('verifyCurrent').textContent = '';
      document.getElementById('verifyLogBody').innerHTML = '';
      document.getElementById('verifySummary').textContent = '';
      document.getElementById('verifyCloseBtn').disabled = true;
      document.getElementById('verifyStopBtn').disabled = false;
    }

    function closeVerifyModal() {
      const overlay = document.getElementById('verifyModalOverlay');
      if (overlay) overlay.classList.add('hidden');
      if (verifyPolling) {
        clearTimeout(verifyPolling);
        verifyPolling = null;
      }
    }

    function renderVerifyStatus(data) {
      const total = data.total || 0;
      const done = data.done || 0;
      const checking = data.checking || 0;
      const pct = total > 0 ? Math.round((done / total) * 100) : 0;
      let progressLabel = done + ' / ' + total + ' checked';
      if (data.running && checking > 0 && done < checking) {
        progressLabel = done + ' / ' + total + ' checked — working on #' + checking;
      }
      document.getElementById('verifyProgressText').textContent = progressLabel;
      document.getElementById('verifyProgressFill').style.width = pct + '%';
      const cur = data.current;
      const curEl = document.getElementById('verifyCurrent');
      if (cur && cur.name) {
        curEl.textContent = 'Checking: ' + (cur.source || '') + ' — ' + (cur.name || '').slice(0, 60);
      } else if (data.running) {
        curEl.textContent = 'Waiting…';
      } else {
        curEl.textContent = '';
      }
      const tbody = document.getElementById('verifyLogBody');
      const rows = data.rows || [];
      tbody.innerHTML = rows.map(function (r) {
        const st = r.status || 'error';
        const cls = 'verify-status-' + st.replace(/[^a-z_]/g, '_');
        return '<tr><td class="' + cls + '">' + escapeHtml(st) + '</td><td>' + escapeHtml(r.source || '') + '</td><td>' + escapeHtml((r.name || '').slice(0, 40)) + '</td><td>' + escapeHtml(r.note || '') + '</td></tr>';
      }).join('');
      if (tbody.lastElementChild) tbody.lastElementChild.scrollIntoView({ block: 'nearest' });
      const s = data.summary || {};
      if (!data.running) {
        document.getElementById('verifyModalTitle').textContent = 'Verification complete';
        document.getElementById('verifySummary').textContent =
          'OK: ' + (s.ok || 0) + ', price changed: ' + (s.price_changed || 0) +
          ', sold out: ' + (s.sold_out || 0) + ', errors: ' + (s.error || 0) +
          ', unsupported: ' + (s.unsupported || 0);
        document.getElementById('verifyCloseBtn').disabled = false;
        document.getElementById('verifyStopBtn').disabled = true;
      }
    }

    async function pollVerifyStatus() {
      try {
        const r = await fetch(apiUrl('api/verify-status'));
        const data = await r.json();
        renderVerifyStatus(data);
        if (data.running) {
          verifyPolling = setTimeout(pollVerifyStatus, 500);
        } else {
          verifyPolling = null;
          verifySoldOutItems = data.sold_out_items || [];
          await finishVerifyJob();
        }
      } catch (e) {
        verifyPolling = setTimeout(pollVerifyStatus, 2000);
      }
    }

    async function finishVerifyJob() {
      const nSold = verifySoldOutItems.length;
      if (viewAllSuppliers) {
        await viewAll();
      } else {
        await refreshProducts();
      }
      selectedIndices.clear();
      verifySoldOutItems.forEach(function (item) {
        for (let i = 0; i < products.length; i++) {
          const p = products[i];
          if (!p) continue;
          const src = p._source || source;
          const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
          if (src === item.source && idx === item.index) {
            selectedIndices.add(i);
            break;
          }
        }
      });
      render();
      const msgEl = document.getElementById('msg');
      if (msgEl && nSold > 0) {
        msgEl.textContent = nSold + ' product(s) marked sold out and selected — use Deactivate or Sync when ready.';
        msgEl.className = 'msg err';
      }
    }

    async function startVerifyAll() {
      const company = getCompany();
      if (!company) {
        showSyncError('Choose a company in the Company dropdown at the top of this page, then try again.');
        return;
      }
      const scope = viewAllSuppliers ? 'all' : 'source';
      const body = { company_slug: company, scope: scope };
      if (scope === 'source') body.source = source;
      openVerifyModal();
      const btn = document.getElementById('verifyAllBtn');
      if (btn) btn.disabled = true;
      try {
        const r = await fetch(apiUrl('api/verify-start'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await r.json();
        if (!data.ok) {
          closeVerifyModal();
          showSyncError(data.error || 'Could not start verification');
          return;
        }
        verifySoldOutItems = [];
        pollVerifyStatus();
      } catch (e) {
        closeVerifyModal();
        showSyncError('Error: ' + e.message);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function stopVerifyAll() {
      try {
        await fetch(apiUrl('api/verify-stop'), { method: 'POST' });
        document.getElementById('verifyStopBtn').disabled = true;
      } catch (e) {}
    }

    async function syncProduct(i) {
      if (syncLoading[i]) return;
      const p = products[i];
      if (!p) return;
      const src = p._source || source;
      const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
      const company = getCompany();
      const category = p.category_id;
      if (!company) {
        showSyncError('Choose a company in the Company dropdown at the top of this page, then try again.');
        return;
      }
      if (!category) {
        showSyncError('Select a category for this product.');
        return;
      }
      if (!hasPackagingDimensions(p)) {
        showSyncError('Add packaging dimensions before syncing. Select a size from the Packaging size dropdown or enter Length, Width, and Height manually.');
        return;
      }
      if (!hasWeight(p)) {
        showSyncError('Add weight (grams) before syncing.');
        return;
      }
      if (src === 'gumtree') {
        const pickup = ['pickup_street', 'pickup_suburb', 'pickup_city', 'pickup_province', 'pickup_postal_code', 'pickup_country'];
        const missing = pickup.filter(f => !(p[f] || '').trim());
        if (missing.length) {
          showSyncError('Gumtree products need pickup address. Expand the product and fill Street, Suburb, City, Province, Postal code, Country.');
          return;
        }
      }
      const imgChoice = await showImageSyncChoice(
        'Sync images?',
        'Sync images: re-upload local photos and replace CRM gallery. Skip images: update fields only; images upload only if CRM has none.'
      );
      if (imgChoice === null) return;

      syncLoading[i] = true;
      syncNotes[i] = null;
      render();
      try {
        const prodsForSource = viewAllSuppliers
          ? products.filter(pp => pp._source === src).sort((a, b) => (a._sourceIndex ?? 0) - (b._sourceIndex ?? 0)).map(pp => { const { _source, _sourceIndex, ...rest } = pp; return rest; })
          : products.map(pp => { const { _source, _sourceIndex, ...rest } = pp; return rest; });
        const r = await fetch(apiUrl('api/sync-product'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: src, index: idx, company_slug: company, category_id: category, products: prodsForSource, sync_images: imgChoice })
        });
        const data = await r.json();
        if (data.ok) {
          if (!p.production_ids) p.production_ids = {};
          p.production_ids[company] = data.product_id;
          syncNotes[i] = 'Synced';
        } else {
          syncNotes[i] = data.error || 'Sync failed';
          showSyncError(data.error || 'Sync failed');
        }
      } catch (e) {
        syncNotes[i] = 'Error: ' + e.message;
        showSyncError('Error: ' + e.message);
      } finally {
        syncLoading[i] = false;
        render();
        if (syncNotes[i] === 'Synced') {
          setTimeout(() => { delete syncNotes[i]; render(); }, 2000);
        }
      }
    }

    function toggleExpand(i) {
      expandedIndex = expandedIndex === i ? null : i;
      render();
    }

    function updateField(i, key, val) {
      if (products[i]) products[i][key] = val;
    }

    function applyPackagingPreset(i, value) {
      if (!products[i] || !value) return;
      const preset = PACKAGING_PRESETS.find(pr => pr.value === value);
      if (preset && preset.l != null) {
        products[i].dimension_length = preset.l;
        products[i].dimension_width = preset.w;
        products[i].dimension_height = preset.h;
        render();
      }
    }

    function removeImage(i, imgIndex) {
      if (!products[i] || !products[i].images) return;
      products[i].images.splice(imgIndex, 1);
      if (!products[i].images.length) delete products[i].images;
      render();
    }

    function toggleSelect(i, checked) {
      if (checked) selectedIndices.add(i); else selectedIndices.delete(i);
      render();
    }

    function selectAll() {
      /* All rows in this supplier tab (or view-all list), not only search-visible — avoids “58/61” confusion */
      for (let i = 0; i < products.length; i++) selectedIndices.add(i);
      render();
    }

    function deselectAll() {
      selectedIndices.clear();
      render();
    }

    function showConfirm(title, message, onConfirm) {
      const overlay = document.getElementById('modalOverlay');
      const titleEl = document.getElementById('modalTitle');
      const msgEl = document.getElementById('modalMessage');
      const btn = document.getElementById('modalConfirmBtn');
      if (!overlay || !titleEl || !msgEl || !btn) return;
      titleEl.textContent = title || 'Confirm';
      msgEl.textContent = message || '';
      const skipBtnIc = document.getElementById('modalSkipImagesBtn');
      const syncImgBtnIc = document.getElementById('modalSyncImagesBtn');
      if (skipBtnIc) { skipBtnIc.style.display = 'none'; skipBtnIc.onclick = null; }
      if (syncImgBtnIc) { syncImgBtnIc.style.display = 'none'; syncImgBtnIc.onclick = null; }
      btn.style.display = '';
      overlay.classList.remove('hidden');
      const close = (runConfirm) => {
        btn.onclick = null;
        cancelBtn.onclick = null;
        overlay.classList.add('hidden');
        document.removeEventListener('keydown', handleKey);
        if (runConfirm && typeof onConfirm === 'function') onConfirm();
      };
      const handleKey = (e) => { if (e.key === 'Escape') close(false); };
      const cancelBtn = overlay.querySelector('.btn-cancel');
      document.addEventListener('keydown', handleKey);
      btn.onclick = () => close(true);
      if (cancelBtn) cancelBtn.onclick = () => close(false);
      overlay.onclick = (e) => { if (e.target === overlay) close(false); };
    }

    function closeModal() {
      const overlay = document.getElementById('modalOverlay');
      if (overlay) overlay.classList.add('hidden');
    }

    function showSyncError(message) {
      const overlay = document.getElementById('modalOverlay');
      const titleEl = document.getElementById('modalTitle');
      const msgEl = document.getElementById('modalMessage');
      const extraEl = document.getElementById('modalExtra');
      const btn = document.getElementById('modalConfirmBtn');
      const cancelBtn = overlay && overlay.querySelector('.btn-cancel');
      if (!overlay || !titleEl || !msgEl || !btn) return;
      const skipBtn = document.getElementById('modalSkipImagesBtn');
      const syncImgBtn = document.getElementById('modalSyncImagesBtn');
      if (skipBtn) { skipBtn.style.display = 'none'; skipBtn.onclick = null; }
      if (syncImgBtn) { syncImgBtn.style.display = 'none'; syncImgBtn.onclick = null; }
      btn.style.display = '';
      titleEl.textContent = 'Sync failed';
      msgEl.textContent = message || 'Could not sync to production.';
      extraEl.innerHTML = '';
      extraEl.style.display = 'none';
      if (cancelBtn) cancelBtn.style.display = 'none';
      btn.textContent = 'OK';
      overlay.classList.remove('hidden');
      const close = () => {
        btn.onclick = null;
        document.removeEventListener('keydown', handleKey);
        if (cancelBtn) cancelBtn.style.display = '';
        btn.textContent = 'Confirm';
        overlay.classList.add('hidden');
      };
      const handleKey = (e) => { if (e.key === 'Escape') close(); };
      document.addEventListener('keydown', handleKey);
      btn.onclick = close;
      overlay.onclick = (e) => { if (e.target === overlay) close(); };
    }

    function showRefreshError(message, url, supplierName) {
      const overlay = document.getElementById('modalOverlay');
      const titleEl = document.getElementById('modalTitle');
      const msgEl = document.getElementById('modalMessage');
      const extraEl = document.getElementById('modalExtra');
      const btn = document.getElementById('modalConfirmBtn');
      const cancelBtn = overlay && overlay.querySelector('.btn-cancel');
      if (!overlay || !titleEl || !msgEl || !btn) return;
      const skipBtn = document.getElementById('modalSkipImagesBtn');
      const syncImgBtn = document.getElementById('modalSyncImagesBtn');
      if (skipBtn) { skipBtn.style.display = 'none'; skipBtn.onclick = null; }
      if (syncImgBtn) { syncImgBtn.style.display = 'none'; syncImgBtn.onclick = null; }
      btn.style.display = '';
      titleEl.textContent = 'Refresh failed';
      msgEl.textContent = message || 'Could not fetch current price.';
      if (url && supplierName) {
        extraEl.innerHTML = '<a href="' + escapeAttr(url) + '" target="_blank" rel="noopener" class="modal-link">View on ' + escapeHtml(supplierName) + ' →</a>';
        extraEl.style.display = 'block';
      } else {
        extraEl.innerHTML = '';
        extraEl.style.display = 'none';
      }
      if (cancelBtn) cancelBtn.style.display = 'none';
      btn.textContent = 'OK';
      overlay.classList.remove('hidden');
      const close = () => {
        btn.onclick = null;
        document.removeEventListener('keydown', handleKey);
        if (cancelBtn) cancelBtn.style.display = '';
        btn.textContent = 'Confirm';
        overlay.classList.add('hidden');
      };
      const handleKey = (e) => { if (e.key === 'Escape') close(); };
      document.addEventListener('keydown', handleKey);
      btn.onclick = close;
      overlay.onclick = (e) => { if (e.target === overlay) close(); };
    }

    async function doSave() {
      const btn = document.getElementById('saveBtn');
      const msg = document.getElementById('msg');
      const company = getCompany() || '';
      btn.disabled = true;
      msg.textContent = '';
      try {
        if (viewAllSuppliers) {
          const bySource = {};
          products.forEach(p => {
            const src = p._source;
            if (!src) return;
            if (!bySource[src]) bySource[src] = [];
            const { _source, _sourceIndex, ...rest } = p;
            bySource[src].push({ ...rest, _sortIdx: p._sourceIndex ?? 999 });
          });
          let ok = true;
          for (const [src, prods] of Object.entries(bySource)) {
            const sorted = prods.sort((a, b) => (a._sortIdx ?? 999) - (b._sortIdx ?? 999));
            const toSave = sorted.map(({ _sortIdx, ...rest }) => rest);
            const cat = toSave.find(p => p.category_id)?.category_id || '';
            const r = await fetch(apiUrl('api/save'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source: src, products: toSave, company_slug: company || undefined, category_id: cat || undefined })
            });
            let data;
            try {
              data = await r.json();
            } catch (e) {
              const errMsg = 'Save failed: invalid response';
              msg.textContent = errMsg;
              msg.className = 'msg err';
              showSyncError(errMsg);
              ok = false;
              break;
            }
            if (!data.ok) {
              const errMsg = data.error || 'Save failed';
              msg.textContent = errMsg;
              msg.className = 'msg err';
              showSyncError(errMsg);
              ok = false;
              break;
            }
          }
          if (ok) {
            msg.textContent = 'Saved.';
            msg.className = 'msg ok';
            if (products.length > 0) viewAll().catch(() => {}); else render();
            return true;
          }
          return false;
        } else {
          const prods = products.map(p => { const { _source, _sourceIndex, ...rest } = p; return rest; });
          const cat = prods.find(p => p.category_id)?.category_id || '';
          const r = await fetch(apiUrl('api/save'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ source, products: prods, company_slug: company || undefined, category_id: cat || undefined })
          });
          let data;
          try {
            data = await r.json();
          } catch (e) {
            const errMsg = 'Save failed: invalid response';
            msg.textContent = errMsg;
            msg.className = 'msg err';
            showSyncError(errMsg);
            return false;
          }
          if (data.ok) {
            msg.textContent = 'Saved.';
            msg.className = 'msg ok';
            if (data.updated) lastUpdated = data.updated;
            loadSource(source);
            return true;
          } else {
            const errMsg = data.error || 'Save failed';
            msg.textContent = errMsg;
            msg.className = 'msg err';
            showSyncError(errMsg);
            return false;
          }
        }
      } catch (e) {
        const errMsg = 'Error: ' + e.message;
        msg.textContent = errMsg;
        msg.className = 'msg err';
        showSyncError(errMsg);
        return false;
      } finally {
        btn.disabled = false;
      }
    }

    async function saveAfterDelete(affectedSources) {
      const company = getCompany() || '';
      if (viewAllSuppliers && affectedSources && affectedSources.size > 0) {
        const bySource = {};
        products.forEach(p => {
          const src = p._source;
          if (!src || !affectedSources.has(src)) return;
          if (!bySource[src]) bySource[src] = [];
          const { _source, _sourceIndex, ...rest } = p;
          bySource[src].push({ ...rest, _sortIdx: p._sourceIndex ?? 999 });
        });
        for (const src of affectedSources) {
          const prods = bySource[src] || [];
          const sorted = prods.sort((a, b) => (a._sortIdx ?? 999) - (b._sortIdx ?? 999));
          const toSave = sorted.map(({ _sortIdx, ...r }) => r);
          const cat = toSave.find(p => p.category_id)?.category_id || '';
          try {
            const r = await fetch(apiUrl('api/save'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source: src, products: toSave, company_slug: company || undefined, category_id: cat || undefined })
            });
            const d = await r.json();
            if (!d.ok) break;
          } catch (e) { break; }
        }
        viewAll().catch(() => {});
      } else {
        await doSave();
      }
    }

    async function deleteFromProductionThenLocal(doDelete) {
      const company = getCompany();
      if (viewAllSuppliers) {
        const items = [];
        getFiltered().forEach(({ p, i }) => {
          if (selectedIndices.has(i)) items.push({ source: p._source, index: p._sourceIndex });
        });
        if (items.length > 0) {
          try {
            const company = getCompany();
            await fetch(apiUrl('api/delete-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ items, company_slug: company })
            });
          } catch (e) {}
        }
      } else {
        const indices = Array.from(selectedIndices);
        if (indices.length > 0) {
          try {
            const company = getCompany();
            await fetch(apiUrl('api/delete-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source, indices, company_slug: company })
            });
          } catch (e) {}
        }
      }
      await doDelete();
    }

    async function deactivateProduct(i) {
      const p = products[i];
      const company = getCompany();
      if (!p || !company || !(p.production_ids || {})[company]) return;
      showConfirm('Deactivate product', 'Set this product to inactive on production? (Keeps locally)', async () => {
        const src = p._source || source;
        const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
        try {
          const company = getCompany();
          const r = await fetch(apiUrl('api/deactivate-from-production'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...(viewAllSuppliers ? { items: [{ source: src, index: idx }] } : { source, indices: [idx] }), company_slug: company })
          });
          const d = await r.json();
          if (d.ok) { syncNotes[i] = 'Deactivated'; render(); setTimeout(() => { delete syncNotes[i]; render(); }, 2000); }
        } catch (e) {}
      });
    }

    async function reactivateProduct(i) {
      const p = products[i];
      const company = getCompany();
      if (!p || !company || !(p.production_ids || {})[company]) return;
      showConfirm('Reactivate product', 'Set this product to active on production?', async () => {
        const src = p._source || source;
        const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
        try {
          const company = getCompany();
          const r = await fetch(apiUrl('api/reactivate-from-production'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ...(viewAllSuppliers ? { items: [{ source: src, index: idx }] } : { source, indices: [idx] }), company_slug: company })
          });
          const d = await r.json();
          if (d.ok) { syncNotes[i] = 'Reactivated'; render(); setTimeout(() => { delete syncNotes[i]; render(); }, 2000); }
        } catch (e) {}
      });
    }

    function deleteProduct(i) {
      if (!products[i]) return;
      showConfirm('Delete product', 'Delete this product?', async () => {
        selectedIndices.clear();
        selectedIndices.add(i);
        await deleteFromProductionThenLocal(async () => {
          const affectedSources = viewAllSuppliers ? new Set([products[i]._source].filter(Boolean)) : null;
          products.splice(i, 1);
          expandedIndex = null;
          selectedIndices.clear();
          refreshNotes = {};
          syncNotes = {};
          render();
          await saveAfterDelete(affectedSources);
        });
      });
    }

    async function deactivateSelected() {
      if (selectedIndices.size === 0) return;
      const n = selectedIndices.size;
      showConfirm('Deactivate products', 'Set ' + n + ' selected product(s) to inactive on production? (Keeps locally)', async () => {
        if (viewAllSuppliers) {
          const items = [];
          getFiltered().forEach(({ p, i }) => {
            if (selectedIndices.has(i)) items.push({ source: p._source, index: p._sourceIndex });
          });
          if (items.length > 0) {
            try {
              const company = getCompany();
              const r = await fetch(apiUrl('api/deactivate-from-production'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items, company_slug: company })
              });
              const d = await r.json();
              if (d.ok) selectedIndices.clear();
              render();
            } catch (e) {}
          }
        } else {
          const indices = Array.from(selectedIndices);
          try {
            const company = getCompany();
            const r = await fetch(apiUrl('api/deactivate-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source, indices, company_slug: company })
            });
            const d = await r.json();
            if (d.ok) selectedIndices.clear();
            render();
          } catch (e) {}
        }
      });
    }

    async function syncSelected() {
      if (selectedIndices.size === 0) return;
      const company = getCompany();
      if (!company) {
        showSyncError('Choose a company in the Company dropdown at the top of this page, then try again.');
        return;
      }
      const indices = Array.from(selectedIndices);
      const toSync = [];
      const skipped = [];
      for (const i of indices) {
        const p = products[i];
        if (!p) continue;
        if (!p.category_id) {
          skipped.push({ i, reason: 'no category' });
          continue;
        }
        if (!hasPackagingDimensions(p) && !p.bundle_items) {
          skipped.push({ i, reason: 'missing dimensions' });
          continue;
        }
        if (!hasWeight(p) && !p.bundle_items) {
          skipped.push({ i, reason: 'missing weight' });
          continue;
        }
        if (p._source === 'gumtree') {
          const pickup = ['pickup_street', 'pickup_suburb', 'pickup_city', 'pickup_province', 'pickup_postal_code', 'pickup_country'];
          const missing = pickup.filter(f => !(p[f] || '').trim());
          if (missing.length) {
            skipped.push({ i, reason: 'Gumtree: missing pickup address' });
            continue;
          }
        }
        toSync.push(i);
      }
      if (toSync.length === 0) {
        showSyncError(skipped.length ? 'Selected products missing dimensions, weight, or category. Add them first.' : 'No valid products to sync.');
        return;
      }
      const selectedCount = indices.length;
      const skipSummary = (function () {
        if (!skipped.length) return '';
        const by = {};
        skipped.forEach(function (s) { const r = s.reason || 'other'; by[r] = (by[r] || 0) + 1; });
        return Object.keys(by).map(function (k) { return by[k] + ' ' + k; }).join(', ');
      })();
      const imgChoice = await showImageSyncChoice(
        'Sync images?',
        'Sync images: re-upload local photos and replace CRM gallery. Skip images: update fields only; images upload only if CRM has none.'
      );
      if (imgChoice === null) return;

      const msgEl = document.getElementById('msg');
      syncSelectedRunning = true;
      render();
      if (msgEl) {
        var pre = 'Syncing ' + toSync.length + ' of ' + selectedCount + ' selected';
        if (skipped.length) pre += ' (' + skipped.length + ' skipped: ' + skipSummary + ')';
        msgEl.textContent = pre + '…';
        msgEl.className = 'msg';
      }

      const targets = toSync.map(function (i) {
        const p = products[i];
        return {
          source: p._source || source,
          index: p._sourceIndex !== undefined ? p._sourceIndex : i,
          category_id: p.category_id,
          name: p.name || ''
        };
      });
      const productsBySource = buildProductsBySourceForSync(toSync);
      openSyncModal();
      try {
        const r = await fetch(apiUrl('api/sync-start'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            company_slug: company,
            sync_images: imgChoice,
            targets: targets,
            products_by_source: productsBySource
          })
        });
        const data = await r.json();
        if (!data.ok) {
          closeSyncModal();
          syncSelectedRunning = false;
          render();
          showSyncError(data.error || 'Could not start sync');
          return;
        }
        pollSyncStatus();
      } catch (e) {
        closeSyncModal();
        syncSelectedRunning = false;
        render();
        showSyncError('Error: ' + e.message);
      }
    }

    async function reactivateSelected() {
      if (selectedIndices.size === 0) return;
      const n = selectedIndices.size;
      showConfirm('Reactivate products', 'Set ' + n + ' selected product(s) to active on production?', async () => {
        if (viewAllSuppliers) {
          const items = [];
          getFiltered().forEach(({ p, i }) => {
            if (selectedIndices.has(i)) items.push({ source: p._source, index: p._sourceIndex });
          });
          if (items.length > 0) {
            try {
              const company = getCompany();
              const r = await fetch(apiUrl('api/reactivate-from-production'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items, company_slug: company })
              });
              const d = await r.json();
              if (d.ok) selectedIndices.clear();
              render();
            } catch (e) {}
          }
        } else {
          const indices = Array.from(selectedIndices);
          try {
            const company = getCompany();
            const r = await fetch(apiUrl('api/reactivate-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source, indices, company_slug: company })
            });
            const d = await r.json();
            if (d.ok) selectedIndices.clear();
            render();
          } catch (e) {}
        }
      });
    }

    async function createBundle() {
      if (selectedIndices.size < 2) return;
      const indices = Array.from(selectedIndices).sort((a, b) => a - b);
      for (const idx of indices) {
        if (products[idx] && products[idx].bundle_items) {
          const msgEl = document.getElementById('msg');
          if (msgEl) msgEl.textContent = 'Cannot bundle a product that is already a bundle.';
          return;
        }
      }
      const msgEl = document.getElementById('msg');
      if (msgEl) msgEl.textContent = '';
      try {
        const company = getCompany();
        const body = viewAllSuppliers
          ? { items: indices.map(i => ({ source: products[i]._source, index: products[i]._sourceIndex })), company_slug: company }
          : { source: source, indices: indices, company_slug: company };
        const r = await fetch(apiUrl('api/create-bundle'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const data = await r.json();
        if (data.ok) {
          selectedIndices.clear();
          if (msgEl) msgEl.textContent = 'Bundle created.';
          if (viewAllSuppliers) {
            await viewAll();
          } else {
            products = [...products, data.product];
            expandedIndex = products.length - 1;
            render();
          }
        } else {
          if (msgEl) msgEl.textContent = data.error || 'Failed to create bundle';
        }
      } catch (e) {
        if (msgEl) msgEl.textContent = 'Error: ' + e.message;
      }
    }

    function deleteSelected() {
      if (selectedIndices.size === 0) return;
      const n = selectedIndices.size;
      showConfirm('Delete products', 'Delete ' + n + ' selected product(s)?', async () => {
        const indices = Array.from(selectedIndices).sort((a, b) => b - a);
        const affectedSources = viewAllSuppliers ? new Set(indices.map(i => products[i]._source).filter(Boolean)) : null;
        await deleteFromProductionThenLocal(async () => {
          indices.forEach(idx => products.splice(idx, 1));
          selectedIndices.clear();
          expandedIndex = null;
          refreshNotes = {};
          syncNotes = {};
          render();
          await saveAfterDelete(affectedSources);
        });
      });
    }

    document.getElementById('saveBtn').onclick = doSave;

    async function resetToUnsynced() {
      const company = getCompany();
      if (!company) {
        const msg = document.getElementById('msg');
        msg.textContent = 'Choose a company in the Company dropdown at the top of this page.';
        msg.className = 'msg err';
        return;
      }
      showConfirm('Reset to unsynced', 'Clear sync status for all products in this supplier? They will show as unsynced. (Does not delete on production.)', async () => {
      const btn = document.getElementById('resetBtn');
      btn.disabled = true;
      try {
        let body;
        if (viewAllSuppliers) {
          const bySource = {};
          products.forEach((p, i) => {
            const src = p._source;
            if (!src || !(p.production_ids || {})[company]) return;
            if (!bySource[src]) bySource[src] = [];
            bySource[src].push({ source: src, index: p._sourceIndex });
          });
          const items = Object.values(bySource).flat();
          if (items.length === 0) {
            document.getElementById('msg').textContent = 'No synced products to reset.';
            return;
          }
          body = { items, company_slug: company };
        } else {
          body = { source, company_slug: company };
        }
        const r = await fetch(apiUrl('api/reset-production-ids'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const d = await r.json();
        const msg = document.getElementById('msg');
        if (d.ok) {
          msg.textContent = 'Reset ' + (d.reset || 0) + ' product(s) to unsynced.';
          msg.className = 'msg ok';
          loadSource(source);
        } else {
          msg.textContent = d.error || 'Reset failed';
          msg.className = 'msg err';
        }
      } finally {
        btn.disabled = false;
      }
      });
    }
    document.getElementById('resetBtn').onclick = resetToUnsynced;

    (async function () {
      await loadCompanySelector();
      await initTabs();
    })();
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    print(f"Edit products: http://127.0.0.1:{args.port}")
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
