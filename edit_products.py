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
from pathlib import Path

from dotenv import load_dotenv
from flask import Blueprint, Flask, jsonify, make_response, render_template_string, request, send_from_directory

load_dotenv(Path(__file__).parent / ".env")

logging.getLogger("werkzeug").setLevel(logging.ERROR)

PRODUCTS_ROOT = Path(__file__).parent

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
        if s not in sources:
            return _no_cache(jsonify({"products": [], "updated": None}))
        products, updated = load_products_with_meta(s, sources)
        return _no_cache(jsonify({"products": products, "updated": updated}))

    def _sync_source_to_company(sources_dict, source: str, company_slug: str, category_id: str) -> tuple[int, list[str]]:
        """Sync all products from one source to company. Returns (synced_count, errors). category_id required."""
        from shared.upload import find_product_by_source_url, get_auth_token, update_product, upload_product

        if not (category_id or "").strip():
            return 0, ["Select a category"]
        base_url = (os.environ.get("API_BASE_URL") or "").strip()
        username = (os.environ.get("API_USERNAME") or "").strip()
        password = (os.environ.get("API_PASSWORD") or "").strip()
        use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
        if not all([base_url, username, password]):
            return 0, ["Set API_BASE_URL, API_USERNAME, API_PASSWORD in .env"]
        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
        if not token:
            return 0, ["Login failed"]

        synced = 0
        errors = []
        output_dir = sources_dict.get(source)
        if not output_dir:
            return 0, ["Invalid source"]
        products = load_products(source, sources_dict)
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
                products_by_source = {src: load_products(src, sources_dict) for src in ref_sources}
            production_ids = prod.get("production_ids") or {}
            existing_id = production_ids.get(company_slug)
            if existing_id:
                cat = (prod.get("category_id") or "").strip() or category_id
                ok = update_product(
                    prod, base_url, token, company_slug, existing_id,
                    source=source, products=products, products_by_source=products_by_source,
                    output_dir=output_dir, sources_dict=sources_dict,
                    category_id=cat,
                )
                if ok:
                    synced += 1
                else:
                    # Product may have been deleted on prod; clear stale id and retry as create
                    del production_ids[company_slug]
                    prod["production_ids"] = production_ids
                    changed = True
                    existing_id = None
            if not existing_id:
                # Fallback: try to find existing product by source_url on API (avoids duplicates when production_ids was lost)
                found_id = find_product_by_source_url(base_url, token, company_slug, prod.get("url"))
                if found_id:
                    cat = (prod.get("category_id") or "").strip() or category_id
                    ok = update_product(
                        prod, base_url, token, company_slug, found_id,
                        source=source, products=products, products_by_source=products_by_source,
                        output_dir=output_dir, sources_dict=sources_dict,
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
                    prod, output_dir, base_url, token, company_slug, cat,
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
            save_products(source, products, sources_dict)
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
            company_slug = (data.get("company_slug") or "").strip()
            category_id = (data.get("category_id") or "").strip()
            updated = save_products(s, prods, sources)
            result = {"ok": True, "updated": updated}
            if company_slug and category_id:
                def _ready_for_sync(p):
                    if p.get("bundle_items"):
                        return True
                    for k in ("dimension_length", "dimension_width", "dimension_height"):
                        try:
                            if p.get(k) is None or float(p.get(k)) <= 0:
                                return False
                        except (TypeError, ValueError):
                            return False
                    try:
                        w = p.get("weight")
                        if w is None or int(w) <= 0:
                            return False
                    except (TypeError, ValueError):
                        return False
                    return True
                missing = [p for p in prods if not _ready_for_sync(p)]
                if missing:
                    result["sync_skipped"] = True
                    result["sync_message"] = f"{len(missing)} product(s) missing packaging dimensions or weight. Add dimensions and weight before syncing."
                else:
                    synced, errors = _sync_source_to_company(sources, s, company_slug, category_id)
                    result["synced"] = synced
                    if errors:
                        result["sync_errors"] = errors
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
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            if not company_slug:
                return jsonify({"ok": False, "error": "Select a company"})
            if not category_id:
                return jsonify({"ok": False, "error": "Select a category"})

            if prods:
                save_products(s, prods, sources)

            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            username = (os.environ.get("API_USERNAME") or "").strip()
            password = (os.environ.get("API_PASSWORD") or "").strip()
            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            if not all([base_url, username, password]):
                return jsonify({"ok": False, "error": "Set API_BASE_URL, API_USERNAME, API_PASSWORD in .env"})

            from shared.upload import get_auth_token, update_product, upload_product

            products = load_products(s, sources)
            if index < 0 or index >= len(products):
                return jsonify({"ok": False, "error": "Invalid index"})
            prod = products[index]
            output_dir = sources[s]
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
                    products_by_source = {src: load_products(src, sources) for src in ref_sources}
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

            token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
            if not token:
                return jsonify({"ok": False, "error": "Login failed"})

            production_ids = prod.get("production_ids") or {}
            existing_id = production_ids.get(company_slug)
            if existing_id:
                cat = (prod.get("category_id") or "").strip() or category_id
                if update_product(
                    prod, base_url, token, company_slug, existing_id,
                    source=s, products=products, products_by_source=products_by_source,
                    output_dir=output_dir, sources_dict=sources,
                    category_id=cat,
                ):
                    return jsonify({"ok": True, "product_id": existing_id})
                # Product may have been deleted on prod; clear stale id and retry as create
                del production_ids[company_slug]
                prod["production_ids"] = production_ids
                save_products(s, products, sources)
                existing_id = None
            if not existing_id:
                pid = upload_product(
                    prod, output_dir, base_url, token, company_slug, category_id,
                    products=products, products_by_source=products_by_source,
                    sources_dict=sources if products_by_source else None,
                    bundle_source=s,
                )
                if pid:
                    production_ids[company_slug] = pid
                    prod["production_ids"] = production_ids
                    save_products(s, products, sources)
                    return jsonify({"ok": True, "product_id": pid})
                return jsonify({"ok": False, "error": "Create failed"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/delete-from-production", methods=["POST"])
    def api_delete_from_production():
        """Archive (soft-delete) products on production API, then remove locally. Uses PATCH status=archived. Supports items: [{source, index}, ...] for view-all."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            username = (os.environ.get("API_USERNAME") or "").strip()
            password = (os.environ.get("API_PASSWORD") or "").strip()
            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            if not all([base_url, username, password]):
                return jsonify({"ok": False, "error": "Set API_BASE_URL, API_USERNAME, API_PASSWORD in .env"})

            from shared.upload import get_auth_token, deactivate_product_from_api

            items = data.get("items")
            if items:
                deactivated = 0
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    products = load_products(s, sources)
                    if idx < 0 or idx >= len(products):
                        continue
                    prod = products[idx]
                    production_ids = prod.get("production_ids") or {}
                    for company_slug, pid in production_ids.items():
                        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
                        if token and deactivate_product_from_api(base_url, token, company_slug, pid):
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

            products = load_products(s, sources)
            deactivated = 0
            for idx in indices:
                if idx < 0 or idx >= len(products):
                    continue
                prod = products[idx]
                production_ids = prod.get("production_ids") or {}
                for company_slug, pid in production_ids.items():
                    token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
                    if token and deactivate_product_from_api(base_url, token, company_slug, pid):
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
            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            username = (os.environ.get("API_USERNAME") or "").strip()
            password = (os.environ.get("API_PASSWORD") or "").strip()
            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            if not all([base_url, username, password]):
                return jsonify({"ok": False, "error": "Set API_BASE_URL, API_USERNAME, API_PASSWORD in .env"})

            from shared.upload import get_auth_token, deactivate_product_from_api

            items = data.get("items")
            if items:
                deactivated = 0
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    products = load_products(s, sources)
                    if idx < 0 or idx >= len(products):
                        continue
                    prod = products[idx]
                    production_ids = prod.get("production_ids") or {}
                    for company_slug, pid in production_ids.items():
                        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
                        if token and deactivate_product_from_api(base_url, token, company_slug, pid):
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

            products = load_products(s, sources)
            deactivated = 0
            for idx in indices:
                if idx < 0 or idx >= len(products):
                    continue
                prod = products[idx]
                production_ids = prod.get("production_ids") or {}
                for company_slug, pid in production_ids.items():
                    token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
                    if token and deactivate_product_from_api(base_url, token, company_slug, pid):
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
            base_url = (os.environ.get("API_BASE_URL") or "").strip()
            username = (os.environ.get("API_USERNAME") or "").strip()
            password = (os.environ.get("API_PASSWORD") or "").strip()
            use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
            if not all([base_url, username, password]):
                return jsonify({"ok": False, "error": "Set API_BASE_URL, API_USERNAME, API_PASSWORD in .env"})

            from shared.upload import get_auth_token, reactivate_product_from_api

            items = data.get("items")
            if items:
                reactivated = 0
                for item in items:
                    s = item.get("source", "temu")
                    idx = item.get("index", 0)
                    if s not in sources:
                        continue
                    products = load_products(s, sources)
                    if idx < 0 or idx >= len(products):
                        continue
                    prod = products[idx]
                    production_ids = prod.get("production_ids") or {}
                    for company_slug, pid in production_ids.items():
                        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
                        if token and reactivate_product_from_api(base_url, token, company_slug, pid):
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

            products = load_products(s, sources)
            reactivated = 0
            for idx in indices:
                if idx < 0 or idx >= len(products):
                    continue
                prod = products[idx]
                production_ids = prod.get("production_ids") or {}
                for company_slug, pid in production_ids.items():
                    token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
                    if token and reactivate_product_from_api(base_url, token, company_slug, pid):
                        reactivated += 1
            return jsonify({"ok": True, "reactivated": reactivated})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.route("/api/refresh-product", methods=["POST"])
    def api_refresh_product():
        try:
            sources = _get_sources()
            data = request.get_json() or {}
            s = data.get("source", "temu")
            index = data.get("index", 0)
            if s not in sources:
                return jsonify({"ok": False, "error": "Invalid source"})
            from shared.refresh import refresh_product
            result = refresh_product(s, index)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

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
        username = (os.environ.get("API_USERNAME") or "").strip()
        password = (os.environ.get("API_PASSWORD") or "").strip()
        use_email = str(os.environ.get("API_USE_EMAIL", "")).lower() in ("1", "true", "yes")
        if not all([base_url, username, password]):
            return jsonify({"categories": [], "error": "Set API_BASE_URL, API_USERNAME, API_PASSWORD in .env"})

        from shared.upload import get_auth_token
        import requests

        token = get_auth_token(base_url, username, password, company_slug=company_slug, use_email=use_email)
        if not token:
            return jsonify({"categories": [], "error": "Login failed"})
        try:
            r = requests.get(
                f"{base_url.rstrip('/')}/v1/categories/",
                headers={"Authorization": f"Bearer {token}", "X-Company-Slug": company_slug},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("results") if isinstance(data.get("results"), list) else (data if isinstance(data, list) else data.get("data", []))
            categories = [{"id": str(c.get("id")), "name": c.get("name", ""), "slug": c.get("slug", "")} for c in items if c.get("id")]
            return jsonify({"categories": categories})
        except Exception as e:
            return jsonify({"categories": [], "error": str(e)})

    @bp.route("/images/<source>/<path:filename>")
    def serve_image(source, filename):
        sources = _get_sources()
        if source in sources:
            path = sources[source] / filename
            if path.exists():
                return send_from_directory(str(path.parent), path.name)
        return "", 404

    @bp.route("/api/create-bundle", methods=["POST"])
    def api_create_bundle():
        """Create a bundle product. Accepts either (source, indices) for single-source or items=[{source,index},...] for cross-supplier."""
        try:
            sources = _get_sources()
            data = request.get_json() or {}
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
                    prods = load_products(src, sources)
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
                products = load_products(s, sources)
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
                for img in p.get("images") or []:
                    raw = img.split("?")[0] if isinstance(img, str) else str(img)
                    key = f"{src}/{raw}" if cross_supplier else raw
                    if key not in images_seen:
                        images_seen.add(key)
                        combined_images.append(f"{src}/{raw}" if cross_supplier else img)
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
            products_to_save = load_products(s, sources)
            products_to_save.append(bundle_product)
            save_products(s, products_to_save, sources)
            return jsonify({"ok": True, "product": bundle_product, "index": len(products_to_save) - 1, "source": s})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    return bp


app = Flask(__name__)
app.register_blueprint(create_edit_blueprint())


def load_products(source: str, sources: dict | None = None) -> list:
    products, _ = load_products_with_meta(source, sources)
    return products


def load_products_with_meta(source: str, sources: dict | None = None) -> tuple[list, str | None]:
    """Return (products list, updated timestamp or None)."""
    sources = sources or _get_sources()
    base = sources.get(source)
    if not base:
        return [], None
    path = base / "products.json"
    if not path.exists():
        return [], None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("products", []), data.get("updated")
    except Exception:
        return [], None


def save_products(source: str, products: list, sources: dict | None = None) -> str:
    sources = sources or _get_sources()
    base = sources.get(source)
    if not base:
        raise ValueError(f"Invalid source: {source}")
    path = base / "products.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
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
    body { font-family: system-ui, sans-serif; margin: 1rem; background: #1a1a1a; color: #e0e0e0; }
    h1 { font-size: 1.25rem; margin-bottom: 1rem; }
    .tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
    .tabs button { padding: 0.5rem 1rem; background: #333; border: 1px solid #555; border-radius: 4px; color: #e0e0e0; cursor: pointer; }
    .tabs button.active { background: #2a7; border-color: #2a7; }
    .tabs button:hover { background: #444; }
    .tab-placeholder { color: #888; font-size: 0.9rem; }
    .filter-bar { display: flex; gap: 0.75rem; margin-bottom: 1rem; flex-wrap: wrap; }
    .filter-bar input { padding: 0.4rem 0.6rem; background: #252525; border: 1px solid #444; border-radius: 4px; color: #e0e0e0; min-width: 180px; }
    .last-updated { font-size: 0.85rem; color: #888; align-self: center; }
    .product-count { font-size: 0.85rem; color: #aaa; align-self: center; }
    .source-badge { font-size: 0.7rem; font-weight: 600; color: #2a7; }
    .row { display: flex; align-items: center; gap: 1rem; padding: 0.5rem 0.75rem; background: #252525; border-radius: 6px; margin-bottom: 0.25rem; cursor: pointer; }
    .row:hover { background: #2a2a2a; }
    .row.expanded { flex-wrap: wrap; }
    .row .col-name { flex: 1; min-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
    .sync-btn { background: #2a5; color: white; font-size: 0.8rem; padding: 0.3rem 0.6rem; }
    .sync-btn:hover { background: #3b6; }
    .sync-btn:disabled { opacity: 0.5; cursor: not-allowed; }
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
    .select-actions { display: flex; gap: 0.5rem; align-items: center; }
    .select-actions button { padding: 0.35rem 0.7rem; font-size: 0.85rem; border: none; border-radius: 4px; cursor: pointer; }
    .select-actions .delete-selected { background: #c44; color: white; }
    .select-actions .delete-selected:hover { background: #e55; }
    .select-actions .delete-selected:disabled { opacity: 0.5; cursor: not-allowed; }
    .select-actions .create-bundle-btn { background: #2a7; color: white; }
    .select-actions .create-bundle-btn:hover { background: #3b8; }
    .select-actions .create-bundle-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .view-all-btn { padding: 0.4rem 0.75rem; font-size: 0.9rem; background: #444; color: #e0e0e0; border: 1px solid #555; border-radius: 4px; cursor: pointer; -webkit-tap-highlight-color: transparent; }
    .view-all-btn:hover { background: #555; }
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
    .modal-extra { margin: 0.75rem 0 0 0; }
    .modal-link { color: #6af; text-decoration: none; }
    .packaging-field .packaging-row { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
    .packaging-field .packaging-select { flex: 1; min-width: 180px; }
    .packaging-field .dimension-preview { display: flex; align-items: center; gap: 0.5rem; }
    .packaging-field .dimension-preview svg { flex-shrink: 0; }
    .packaging-field .dimension-text { font-size: 0.8rem; color: #888; }
    .packaging-field .dimension-text.empty { color: #555; font-style: italic; }
    .modal-link:hover { text-decoration: underline; }
    @media (max-width: 640px) {
      body { margin: 0.5rem; padding-bottom: max(2rem, env(safe-area-inset-bottom, 0px)); -webkit-text-size-adjust: 100%; }
      h1 { font-size: 1.1rem; }
      .tabs { flex-wrap: nowrap; overflow-x: auto; gap: 0.4rem; padding-bottom: 0.25rem; -webkit-overflow-scrolling: touch; }
      .tabs button { padding: 0.5rem 0.75rem; font-size: 0.9rem; min-height: 44px; -webkit-tap-highlight-color: transparent; }
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
  <div class="tabs" id="tabs">
    <span class="tab-placeholder">Loading suppliers...</span>
  </div>
  <div class="filter-bar">
    <input type="text" id="searchName" placeholder="Search name..." oninput="viewAllSuppliers=false; render()">
    <button type="button" class="view-all-btn" id="viewAllBtn">View all</button>
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
      <button type="button" class="delete-selected" id="deleteSelectedBtn" onclick="deleteSelected()" disabled>Delete</button>
    </div>
  </div>
  <div class="update-section">
    <label>Sync to company (required for Sync/Save)</label>
    <select id="companySelect"><option value="">Loading...</option></select>
  </div>
  <div id="products"></div>
  <div id="modalOverlay" class="modal-overlay hidden">
    <div class="modal">
      <h3 id="modalTitle">Confirm</h3>
      <p id="modalMessage"></p>
      <div id="modalExtra" class="modal-extra" style="display:none;"></div>
      <div class="modal-actions">
        <button type="button" class="btn-cancel" onclick="closeModal()">Cancel</button>
        <button type="button" class="btn-confirm" id="modalConfirmBtn">Confirm</button>
      </div>
    </div>
  </div>
  <button class="save-btn" id="saveBtn">Save</button>
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
    let sources = [];
    let categories = [];

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

    async function initTabs() {
      const r = await fetch(cacheBust(apiUrl('api/sources')));
      sources = await r.json();
      const tabsEl = document.getElementById('tabs');
      if (!sources.length) {
        tabsEl.innerHTML = '<span class="tab-placeholder">No suppliers</span>';
        return;
      }
      const urlSource = new URLSearchParams(window.location.search).get('source');
      source = (urlSource && sources.some(s => s.slug === urlSource)) ? urlSource : sources[0].slug;
      tabsEl.innerHTML = sources.map((s) =>
        '<button class="tab' + (s.slug === source ? ' active' : '') + '" data-source="' + s.slug + '">' + (s.display_name || s.slug) + '</button>'
      ).join('');
      tabsEl.querySelectorAll('.tab').forEach(b => {
        b.onclick = () => loadSource(b.dataset.source);
      });
      loadSource(source);
      const viewAllBtn = document.getElementById('viewAllBtn');
      if (viewAllBtn) viewAllBtn.addEventListener('click', viewAll);
    }

    function apiUrl(path) {
      const base = (document.location.pathname || '/edit').replace(/\/$/, '') || '/edit';
      return base + (path.startsWith('/') ? path : '/' + path);
    }
    function cacheBust(url) { return url + (url.includes('?') ? '&' : '?') + '_=' + Date.now(); }

    async function loadSource(s) {
      source = s;
      viewAllSuppliers = false;
      selectedIndices.clear();
      refreshNotes = {};
      syncNotes = {};
      document.querySelectorAll('.tab').forEach(b => { b.classList.toggle('active', b.dataset.source === source); });
      const r = await fetch(cacheBust(apiUrl('api/products?source=' + source)));
      const data = await r.json();
      products = data.products || [];
      lastUpdated = data.updated || null;
      render();
      loadCompanies();
    }

    async function loadCompanies() {
      try {
        const r = await fetch(apiUrl('api/companies'));
        const d = await r.json();
        const sel = document.getElementById('companySelect');
        const companies = d.companies || [];
        sel.innerHTML = companies.length ? '<option value="">Select company</option>' + companies.map(c => '<option value="' + c + '">' + c + '</option>').join('') : '<option value="">Set COMPANY_SLUG in .env</option>';
        if (companies.length === 1) sel.value = companies[0];
        sel.onchange = loadCategories;
        loadCategories();
      } catch (e) {}
    }

    async function loadCategories() {
      const company = (document.getElementById('companySelect') || {}).value;
      if (!company) {
        categories = [];
        render();
        return;
      }
      try {
        const r = await fetch(apiUrl('api/categories?company_slug=' + encodeURIComponent(company)));
        const d = await r.json();
        const cats = d.categories || [];
        if (d.error || !cats.length) {
          categories = [];
        } else {
          categories = cats;
        }
      } catch (e) {
        categories = [];
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
      const btn = document.getElementById('viewAllBtn');
      if (btn) { btn.disabled = true; btn.textContent = 'Loading…'; }
      viewAllSuppliers = true;
      const search = document.getElementById('searchName');
      if (search) search.value = '';
      const combined = [];
      for (const s of sources) {
        try {
          const r = await fetch(cacheBust(apiUrl('api/products?source=' + encodeURIComponent(s.slug))));
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
      const createBundleBtn = document.getElementById('createBundleBtn');
      if (createBundleBtn) createBundleBtn.disabled = selectedIndices.size < 2;
      const filtered = getFiltered();
      const pc = document.getElementById('productCount');
      if (pc) pc.textContent = filtered.length + ' of ' + products.length + ' products';
      const updateSection = document.querySelector('.update-section');
      if (updateSection) updateSection.style.display = '';
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
        const company = (document.getElementById('companySelect') || {}).value || '';
        const isSynced = company && (p.production_ids || {})[company];
        const noteClass = note && !note.valid ? 'invalid' : (note && note.price_change_note && note.price_change_note !== 'No change' ? 'up' : '');
        const checked = selectedIndices.has(i) ? ' checked' : '';
        const pSource = p._source || source;
        const supplierName = escapeHtml((sources.find(s => s.slug === pSource) || {}).display_name || pSource);
        const prodCat = p.category_id || '';
        const catOpts = categories.length ? '<option value=""' + (!prodCat ? ' selected' : '') + '>—</option>' + categories.map(c => '<option value="' + escapeAttr(c.id) + '"' + (prodCat === c.id ? ' selected' : '') + '>' + escapeHtml((c.name || c.slug || c.id).slice(0, 20)) + ((c.name || c.slug || '').length > 20 ? '…' : '') + '</option>').join('') : '<option value="">Select company</option>';
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
              ${(p.images || []).length ? '<div class="field"><label>Images</label><div class="image-thumbs' + (p.bundle_items ? ' bundle-images' : '') + '">' + (p.images || []).map((img, imgIdx) => { const hasSourcePrefix = sources.some(s => (img || '').startsWith(s.slug + '/')); const imgSrc = hasSourcePrefix ? 'images/' + img : 'images/' + pSource + '/' + img; return '<div class="thumb-wrap"><img src="' + escapeAttr(imgSrc) + '" alt="" class="thumb" onerror="this.style.display=\\'none\\'"><button type="button" class="thumb-remove" onclick="event.stopPropagation(); removeImage(' + i + ',' + imgIdx + ')">×</button></div>'; }).join('') + '</div></div>' : ''}
              <div class="field"><label>Name</label><input type="text" value="${name}" oninput="updateField(${i}, 'name', this.value)"></div>
              <div class="field"><label>Short description</label><input type="text" value="${shortDesc}" oninput="updateField(${i}, 'short_description', this.value)"></div>
              <div class="field"><label>Description</label><textarea rows="4" oninput="updateField(${i}, 'description', this.value)">${desc}</textarea></div>
              <div class="field"><label>Price</label><input type="number" step="0.01" value="${p.price ?? ''}" oninput="updateField(${i}, 'price', parseFloat(this.value) || 0)"></div>
              <div class="field"><label>Cost</label><input type="number" step="0.01" value="${p.cost ?? ''}" oninput="updateField(${i}, 'cost', parseFloat(this.value) || 0)"></div>
              <div class="field"><label>Units (stock)</label><input type="number" min="0" step="1" value="${p.stock_quantity ?? ''}" placeholder="0" oninput="const v = parseInt(this.value, 10); updateField(${i}, 'stock_quantity', isNaN(v) ? 0 : v); updateField(${i}, 'in_stock', !isNaN(v) && v > 0)"></div>
              <div class="field"><label>Delivery time</label><input type="text" value="${escapeAttr(p.delivery_time || '')}" placeholder="e.g. 7-13 days (from supplier)" oninput="updateField(${i}, 'delivery_time', this.value.trim() || null)"></div>
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
        const r = await fetch(apiUrl('api/refresh-product'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ source: src, index: idx })
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

    async function syncProduct(i) {
      const p = products[i];
      if (!p) return;
      const src = p._source || source;
      const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
      const company = (document.getElementById('companySelect') || {}).value;
      const category = p.category_id;
      if (!company) {
        showSyncError('Select a company first to sync.');
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
          body: JSON.stringify({ source: src, index: idx, company_slug: company, category_id: category, products: prodsForSource })
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
      }
      syncLoading[i] = false;
      render();
      if (syncNotes[i] === 'Synced') {
        setTimeout(() => { delete syncNotes[i]; render(); }, 2000);
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
      getFiltered().forEach(({ i }) => selectedIndices.add(i));
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
      const company = (document.getElementById('companySelect') || {}).value || '';
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
          let totalSynced = 0;
          const allErrors = [];
          const syncSkipped = [];
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
            try { data = await r.json(); } catch (e) { msg.textContent = 'Save failed: invalid response'; msg.className = 'msg err'; ok = false; break; }
            if (!data.ok) { msg.textContent = data.error || 'Save failed'; msg.className = 'msg err'; ok = false; break; }
            if (data.synced) totalSynced += data.synced;
            if (data.sync_errors) allErrors.push(...data.sync_errors);
            if (data.sync_skipped && data.sync_message) syncSkipped.push(data.sync_message);
          }
          if (ok) {
            msg.textContent = 'Saved.';
            if (company && totalSynced > 0) msg.textContent += ' Synced ' + totalSynced + ' to production.';
            if (syncSkipped.length) msg.textContent += ' ' + syncSkipped[0];
            if (allErrors.length) msg.textContent += ' ' + allErrors.slice(0, 3).join('; ');
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
          try { data = await r.json(); } catch (e) { msg.textContent = 'Save failed: invalid response'; msg.className = 'msg err'; return false; }
          if (data.ok) {
            msg.textContent = 'Saved.';
            if (company && data.synced) msg.textContent += ' Synced ' + data.synced + ' to production.';
            if (data.sync_skipped && data.sync_message) msg.textContent += ' ' + data.sync_message;
            if (data.sync_errors && data.sync_errors.length) msg.textContent += ' ' + data.sync_errors.slice(0, 3).join('; ');
            msg.className = 'msg ok';
            if (data.updated) lastUpdated = data.updated;
            loadSource(source);
            return true;
          } else {
            msg.textContent = data.error || 'Save failed';
            msg.className = 'msg err';
            return false;
          }
        }
      } catch (e) {
        msg.textContent = 'Error: ' + e.message;
        msg.className = 'msg err';
        return false;
      } finally {
        btn.disabled = false;
      }
    }

    async function saveAfterDelete(affectedSources) {
      const company = (document.getElementById('companySelect') || {}).value || '';
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
      const company = (document.getElementById('companySelect') || {}).value;
      if (viewAllSuppliers) {
        const items = [];
        getFiltered().forEach(({ p, i }) => {
          if (selectedIndices.has(i)) items.push({ source: p._source, index: p._sourceIndex });
        });
        if (items.length > 0) {
          try {
            await fetch(apiUrl('api/delete-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ items })
            });
          } catch (e) {}
        }
      } else {
        const indices = Array.from(selectedIndices);
        if (indices.length > 0) {
          try {
            await fetch(apiUrl('api/delete-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source, indices })
            });
          } catch (e) {}
        }
      }
      await doDelete();
    }

    async function deactivateProduct(i) {
      const p = products[i];
      const company = (document.getElementById('companySelect') || {}).value;
      if (!p || !company || !(p.production_ids || {})[company]) return;
      showConfirm('Deactivate product', 'Set this product to inactive on production? (Keeps locally)', async () => {
        const src = p._source || source;
        const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
        try {
          const r = await fetch(apiUrl('api/deactivate-from-production'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(viewAllSuppliers ? { items: [{ source: src, index: idx }] } : { source, indices: [idx] })
          });
          const d = await r.json();
          if (d.ok) { syncNotes[i] = 'Deactivated'; render(); setTimeout(() => { delete syncNotes[i]; render(); }, 2000); }
        } catch (e) {}
      });
    }

    async function reactivateProduct(i) {
      const p = products[i];
      const company = (document.getElementById('companySelect') || {}).value;
      if (!p || !company || !(p.production_ids || {})[company]) return;
      showConfirm('Reactivate product', 'Set this product to active on production?', async () => {
        const src = p._source || source;
        const idx = p._sourceIndex !== undefined ? p._sourceIndex : i;
        try {
          const r = await fetch(apiUrl('api/reactivate-from-production'), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(viewAllSuppliers ? { items: [{ source: src, index: idx }] } : { source, indices: [idx] })
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
              const r = await fetch(apiUrl('api/deactivate-from-production'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items })
              });
              const d = await r.json();
              if (d.ok) selectedIndices.clear();
              render();
            } catch (e) {}
          }
        } else {
          const indices = Array.from(selectedIndices);
          try {
            const r = await fetch(apiUrl('api/deactivate-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source, indices })
            });
            const d = await r.json();
            if (d.ok) selectedIndices.clear();
            render();
          } catch (e) {}
        }
      });
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
              const r = await fetch(apiUrl('api/reactivate-from-production'), {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ items })
              });
              const d = await r.json();
              if (d.ok) selectedIndices.clear();
              render();
            } catch (e) {}
          }
        } else {
          const indices = Array.from(selectedIndices);
          try {
            const r = await fetch(apiUrl('api/reactivate-from-production'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ source, indices })
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
        const body = viewAllSuppliers
          ? { items: indices.map(i => ({ source: products[i]._source, index: products[i]._sourceIndex })) }
          : { source: source, indices: indices };
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

    initTabs();
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
