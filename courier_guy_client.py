"""
Courier Guy API client for products app.
Uses env-based credentials: COURIER_GUY_API_BASE_URL, COURIER_GUY_API_KEY.
Can later switch to Django CRM global config.
"""
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

LOG = logging.getLogger("products.courier_guy")
PRODUCTS_ROOT = Path(__file__).resolve().parent

DEFAULT_BASE = "https://api.portal.thecourierguy.co.za"


def _get_credentials() -> tuple[str, str]:
    """Load Courier Guy credentials from env. Returns (base_url, api_key)."""
    base = (os.environ.get("COURIER_GUY_API_BASE_URL") or "").strip() or DEFAULT_BASE
    key = (os.environ.get("COURIER_GUY_API_KEY") or "").strip()
    return base, key


def _norm_address(addr: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize address for Courier Guy API."""
    PROVINCE_MAP = {
        "GP": "Gauteng", "WC": "Western Cape", "KZN": "KwaZulu-Natal",
        "EC": "Eastern Cape", "FS": "Free State", "LP": "Limpopo",
        "MP": "Mpumalanga", "NW": "North West", "NC": "Northern Cape",
    }
    zone = addr.get("zone") or addr.get("province", "")
    zone = PROVINCE_MAP.get(zone, zone) if zone and len(str(zone)) <= 3 else zone
    country = addr.get("country", "South Africa")
    if country and ("south" in str(country).lower() or len(str(country)) > 2):
        country = "ZA"
    return {
        "type": addr.get("type", "residential"),
        "company": addr.get("company", "") or "",
        "street_address": addr.get("street_address") or addr.get("address", ""),
        "local_area": addr.get("local_area") or addr.get("suburb") or addr.get("city", ""),
        "city": addr.get("city", ""),
        "zone": zone or "",
        "country": country or "ZA",
        "code": addr.get("code") or addr.get("postalCode") or addr.get("postal_code", ""),
    }


def get_quote(
    collection_address: Dict[str, Any],
    delivery_address: Dict[str, Any],
    parcels: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Get Courier Guy rate quote.
    Returns {ok: bool, rates: [...], error: str}.
    """
    base, key = _get_credentials()
    if not key:
        return {"ok": False, "rates": [], "error": "COURIER_GUY_API_KEY not set in .env"}

    coll = _norm_address(collection_address)
    deliv = _norm_address(delivery_address)
    if delivery_address.get("lat") is not None and delivery_address.get("lng") is not None:
        try:
            deliv["lat"] = float(delivery_address["lat"])
            deliv["lng"] = float(delivery_address["lng"])
        except (TypeError, ValueError):
            pass

    api_parcels = []
    for p in (parcels or [{}]):
        api_parcels.append({
            "submitted_length_cm": float(p.get("submitted_length_cm", 30)),
            "submitted_width_cm": float(p.get("submitted_width_cm", 20)),
            "submitted_height_cm": float(p.get("submitted_height_cm", 15)),
            "submitted_weight_kg": float(p.get("submitted_weight_kg", 0.5)),
        })
    if not api_parcels:
        api_parcels = [{"submitted_length_cm": 30, "submitted_width_cm": 20, "submitted_height_cm": 15, "submitted_weight_kg": 0.5}]

    from datetime import datetime, timedelta
    request_data = {
        "collection_address": coll,
        "delivery_address": deliv,
        "parcels": api_parcels,
        "opt_in_rates": [],
        "opt_in_time_based_rates": [],
        "collection_min_date": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d"),
        "delivery_min_date": (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"),
    }

    try:
        resp = requests.post(
            f"{base}/rates",
            json=request_data,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code != 200:
            err = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
            LOG.error("Courier Guy rates error: %s", err)
            return {"ok": False, "rates": [], "error": err}
        data = resp.json()
        rates = data.get("rates", [])
        result = []
        for r in rates:
            sl = r.get("service_level", {})
            result.append({
                "service_code": sl.get("code", ""),
                "service_name": sl.get("name", ""),
                "rate": float(r.get("rate", 0)),
            })
        return {"ok": True, "rates": result}
    except requests.exceptions.RequestException as e:
        LOG.exception("Courier Guy API error: %s", e)
        return {"ok": False, "rates": [], "error": str(e)}


def create_shipment(
    collection_address: Dict[str, Any],
    delivery_address: Dict[str, Any],
    delivery_contact: Dict[str, str],
    collection_contact: Dict[str, str],
    customer_reference: str,
    declared_value: float = 0,
    service_level_code: str = "ECO",
) -> Dict[str, Any]:
    """
    Create Courier Guy shipment.
    Returns {ok: bool, waybill_number: str, shipment_id: str, error: str}.
    """
    base, key = _get_credentials()
    if not key:
        return {"ok": False, "error": "COURIER_GUY_API_KEY not set in .env"}

    from datetime import datetime, timedelta
    coll = _norm_address(collection_address)
    deliv = _norm_address(delivery_address)
    if delivery_address.get("lat") is not None and delivery_address.get("lng") is not None:
        try:
            deliv["lat"] = float(delivery_address["lat"])
            deliv["lng"] = float(delivery_address["lng"])
        except (TypeError, ValueError):
            pass

    collection_date = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT09:00:00.000Z")
    delivery_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%dT17:00:00.000Z")

    shipment_data = {
        "collection_min_date": collection_date,
        "delivery_min_date": delivery_date,
        "collection_address": coll,
        "collection_contact": collection_contact,
        "delivery_address": deliv,
        "delivery_contact": delivery_contact,
        "parcels": [{
            "parcel_description": customer_reference,
            "submitted_length_cm": 30,
            "submitted_width_cm": 20,
            "submitted_height_cm": 15,
            "submitted_weight_kg": 0.5,
        }],
        "declared_value": declared_value,
        "customer_reference": customer_reference,
        "service_level_code": service_level_code,
        "mute_notifications": False,
    }

    try:
        resp = requests.post(
            f"{base}/shipments",
            json=shipment_data,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code not in (200, 201):
            err = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
            LOG.error("Courier Guy shipment error: %s", err)
            return {"ok": False, "error": err}
        data = resp.json()
        shipment = data.get("shipment", data)
        waybill = shipment.get("tracking_reference") or shipment.get("waybill_number", "")
        return {"ok": True, "waybill_number": waybill, "shipment_id": shipment.get("id", "")}
    except requests.exceptions.RequestException as e:
        LOG.exception("Courier Guy API error: %s", e)
        return {"ok": False, "error": str(e)}
