"""Wayfair GraphQL API: token, get orders, accept, register, labels, send ASN."""

import os
import time
import re
from datetime import datetime, timedelta

from shared import config as cfg
from shared.http_helpers import urllib_post, urllib_get_binary


# ==============================================================================
# AUTH
# ==============================================================================
def get_wf_token() -> str:
    status, data = urllib_post(
        cfg.WAYFAIR_TOKEN_URL,
        {
            "grant_type":    "client_credentials",
            "client_id":     cfg.WAYFAIR_CLIENT_ID,
            "client_secret": cfg.WAYFAIR_CLIENT_SECRET,
            "audience":      cfg.WAYFAIR_GQL_URL,
        },
        {"Content-Type": "application/json"},
        timeout=20,
    )
    if status != 200:
        raise RuntimeError(f"Wayfair token request failed: {status} {data}")
    return data["access_token"]


# ==============================================================================
# DROPSHIP PURCHASE ORDERS
# ==============================================================================
def get_open_orders(wf_token: str, from_date_iso: str, only_new: bool):
    if only_new:
        query = """
        query GetOpenPOs($fromDate: IsoDateTime, $limit: Int32) {
          getDropshipPurchaseOrders(
            limit: $limit, hasResponse: false, fromDate: $fromDate, sortOrder: DESC
          ) {
            poNumber poDate customerName
            shipTo { name address1 address2 city state country postalCode phoneNumber }
            products { partNumber quantity price }
          }
        }
        """
    else:
        query = """
        query GetOpenPOs($fromDate: IsoDateTime, $limit: Int32) {
          getDropshipPurchaseOrders(
            limit: $limit, fromDate: $fromDate, sortOrder: DESC
          ) {
            poNumber poDate customerName
            shipTo { name address1 address2 city state country postalCode phoneNumber }
            products { partNumber quantity price }
          }
        }
        """
    status, data = urllib_post(
        cfg.WAYFAIR_GQL_URL,
        {"query": query, "variables": {"fromDate": from_date_iso, "limit": 500}},
        {"Authorization": f"Bearer {wf_token}", "Content-Type": "application/json"},
        timeout=60,
    )
    if status != 200 or "errors" in data:
        raise RuntimeError(f"PO fetch failed: {status} {data}")
    return data.get("data", {}).get("getDropshipPurchaseOrders", []) or []


def get_po_data(wf_token: str, po_number: str) -> dict:
    query = """
    query GetPOs($poNumbers: [String!]!) {
      getDropshipPurchaseOrders(poNumbers: $poNumbers) {
        poNumber
        shipTo { name address1 address2 city state country postalCode phoneNumber }
        products { partNumber quantity }
      }
    }
    """
    status, data = urllib_post(
        cfg.WAYFAIR_GQL_URL,
        {"query": query, "variables": {"poNumbers": [po_number]}},
        {"Authorization": f"Bearer {wf_token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if status != 200 or "errors" in data:
        raise RuntimeError(f"PO fetch failed: {status} {data}")
    orders = data.get("data", {}).get("getDropshipPurchaseOrders", [])
    if not orders:
        raise RuntimeError(f"No PO data returned for {po_number}")
    return orders[0]


# ==============================================================================
# ACCEPT
# ==============================================================================
def acknowledge_po(wf_token: str, po_number: str, accepted_items: list):
    line_items = [
        {
            "partNumber":        i["wayfair_sku"],
            "quantity":          int(i["ordered_qty"]),
            "unitPrice":         float(i.get("wayfair_price") or 0),
            "estimatedShipDate": (datetime.utcnow() + timedelta(days=cfg.PICKUP_OFFSET_DAYS)).strftime("%Y-%m-%dT00:00:00Z"),
        }
        for i in accepted_items
    ]
    query = """
    mutation AcceptPO($poNumber: String!, $lineItems: [AcceptedLineItemInput!]!) {
        purchaseOrders {
            accept(poNumber: $poNumber, lineItems: $lineItems, shipSpeed: GROUND) { id }
        }
    }
    """
    status, data = urllib_post(
        cfg.WAYFAIR_GQL_URL,
        {"query": query, "variables": {"poNumber": po_number, "lineItems": line_items}},
        {"Authorization": f"Bearer {wf_token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if status != 200 or "errors" in data:
        raise RuntimeError(f"Accept failed for {po_number}: {data.get('errors', data)}")
    return data.get("data", {}).get("purchaseOrders", {}).get("accept", {}).get("id")


# ==============================================================================
# REGISTER SHIPMENT
# ==============================================================================
def register_shipment(wf_token, po_number, warehouse_id=None, pickup_date=None):
    """
    Register shipment with Wayfair (required before labels/ASN).
    pickup_date defaults to UTC midnight + PICKUP_OFFSET_DAYS (env-controlled, 2..5).
    """
    if cfg.DRY_RUN:
        return {"id": "DRY_RUN", "poNumber": po_number}

    warehouse_id = warehouse_id or cfg.WAYFAIR_WAREHOUSE_ID
    if not pickup_date:
        pickup_dt = datetime.utcnow() + timedelta(days=cfg.PICKUP_OFFSET_DAYS)
        # 17:00 UTC = 09:00 LA (PST) / 10:00 LA (PDT) — realistic morning pickup window
        pickup_date = pickup_dt.replace(hour=17, minute=0, second=0, microsecond=0).strftime(
            "%Y-%m-%d %H:%M:%S.000000 +00:00"
        )

    query = """
    mutation register($registrationInput: RegistrationInput!) {
        purchaseOrders {
            register(registrationInput: $registrationInput) {
                id poNumber pickupDate eventDate
                consolidatedShippingLabel { url }
                billOfLading { url }
                purchaseOrder { packingSlipUrl }
                generatedShippingLabels {
                    poNumber carrier carrierCode trackingNumber
                }
                customsDocument { required url }
            }
        }
    }
    """
    params = {
        "poNumber": po_number,
        "warehouseId": warehouse_id,
        "requestForPickupDate": pickup_date,
    }
    status, data = urllib_post(
        cfg.WAYFAIR_GQL_URL,
        {"query": query, "variables": {"registrationInput": params}},
        {"Authorization": f"Bearer {wf_token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if status != 200 or "errors" in data:
        raise RuntimeError(
            f"Register failed for {po_number}: HTTP {status} - {data.get('errors', data)}"
        )

    reg = data.get("data", {}).get("purchaseOrders", {}).get("register", {})

    result = {
        "id":         reg.get("id"),
        "poNumber":   reg.get("poNumber"),
        "pickupDate": reg.get("pickupDate"),
        "eventDate":  reg.get("eventDate"),
    }

    labels = reg.get("generatedShippingLabels") or []
    if labels:
        first = labels[0]
        result["trackingNumber"] = first.get("trackingNumber")
        result["carrierCode"]    = first.get("carrierCode")
        result["carrier"]        = first.get("carrier")
    else:
        result["trackingNumber"] = None
        result["carrierCode"]    = None

    csl = reg.get("consolidatedShippingLabel") or {}
    result["shippingLabelUrl"] = csl.get("url")
    bol = reg.get("billOfLading") or {}
    result["bolUrl"] = bol.get("url")
    po_info = reg.get("purchaseOrder") or {}
    result["packingSlipUrl"] = po_info.get("packingSlipUrl")

    return result


# ==============================================================================
# DOWNLOAD SHIPPING DOCUMENTS (REST) — with content-type validation
# ==============================================================================
def _save_binary(po_number, body, content_type, save_dir, suffix, allowed_exts):
    ct = (content_type or "").lower()
    if not any(x in ct for x in allowed_exts):
        print(f"    {suffix} skipped: unexpected content-type '{content_type}'")
        return None
    if len(body) < 500:
        print(f"    {suffix} skipped: payload too small ({len(body)} bytes)")
        return None
    os.makedirs(save_dir, exist_ok=True)
    ext = "zpl" if "zpl" in ct else "pdf"
    path = os.path.join(save_dir, f"{po_number}_{suffix}.{ext}")
    with open(path, "wb") as f:
        f.write(body)
    print(f"    {suffix} saved: {path} ({len(body):,} bytes)")
    return path


def download_shipping_label(wf_token, po_number, save_dir=None):
    save_dir = save_dir or cfg.LABEL_DOWNLOAD_DIR
    url = f"{cfg.WAYFAIR_REST_BASE}/shipping_label/{po_number}"
    status, content_type, body = urllib_get_binary(
        url,
        {"Authorization": f"Bearer {wf_token}", "Accept": "application/pdf"},
        timeout=30,
    )
    if status != 200:
        print(f"    Label download failed: HTTP {status}")
        return None
    return _save_binary(po_number, body, content_type, save_dir, "label",
                         ("pdf", "zpl", "octet-stream"))


def download_packing_slip(wf_token, po_number, save_dir=None):
    save_dir = save_dir or cfg.LABEL_DOWNLOAD_DIR
    url = f"{cfg.WAYFAIR_REST_BASE}/packing_slip/{po_number}"
    status, content_type, body = urllib_get_binary(
        url,
        {"Authorization": f"Bearer {wf_token}", "Accept": "application/pdf"},
        timeout=30,
    )
    if status != 200:
        return None
    return _save_binary(po_number, body, content_type, save_dir, "packing_slip",
                         ("pdf", "octet-stream"))


# ==============================================================================
# SHIP NOTICE (ASN)
# ==============================================================================
def detect_carrier_from_tracking(t):
    if not t:
        return None
    t = re.sub(r"\s+", "", str(t)).upper()
    if t.startswith("1Z") and len(t) >= 12:
        return "UPS"
    if re.match(r"^(9400|9205|9407|9303|9270|9202|9261)\d+$", t) and 20 <= len(t) <= 30:
        return "USPS"
    if re.match(r"^(92|94)\d{18,28}$", t):
        return "USPS"
    if t.isdigit() and len(t) in (12, 15, 20, 22):
        return "FEDEX"
    if t.isdigit() and len(t) == 10:
        return "DHL"
    return None


def send_asn(wf_token, po_number, tracking, carrier, products, ship_to):
    if cfg.DRY_RUN:
        return {"id": "DRY_RUN", "status": "DRY_RUN"}

    ship_date = time.strftime("%Y-%m-%d %H:%M:%S.000000 +00:00", time.gmtime())
    items = [{"partNumber": p["partNumber"], "quantity": int(p["quantity"])} for p in products]
    dest = {
        "name":           ship_to.get("name", "Customer"),
        "streetAddress1": ship_to.get("address1", ""),
        "streetAddress2": ship_to.get("address2", "") or "",
        "city":           ship_to.get("city", ""),
        "state":          ship_to.get("state", ""),
        "postalCode":     ship_to.get("postalCode", ""),
        "country":        ship_to.get("country", "US"),
    }
    notice = {
        "poNumber":       po_number,
        "supplierId":     cfg.WAYFAIR_SUPPLIER_ID,
        "packageCount":   1,
        "weight":         10.0,
        "volume":         1.0,
        "carrierCode":    carrier,
        "trackingNumber": tracking,
        "shipSpeed":      "GROUND",
        "shipDate":       ship_date,
        "sourceAddress":  cfg.SOURCE_ADDRESS,
        "destinationAddress": dest,
        "smallParcelShipments": [{
            "package": {"code": {"type": "TRACKING_NUMBER", "value": tracking}, "weight": 10.0},
            "items":   items,
        }],
    }
    query = """
    mutation shipment($notice: ShipNoticeInput!) {
        purchaseOrders { shipment(notice: $notice) { id status } }
    }
    """
    status, data = urllib_post(
        cfg.WAYFAIR_GQL_URL,
        {"query": query, "variables": {"notice": notice}},
        {"Authorization": f"Bearer {wf_token}", "Content-Type": "application/json"},
        timeout=30,
    )
    if status != 200 or "errors" in data:
        raise RuntimeError(f"Shipment failed: HTTP {status} - {data.get('errors', data)}")
    return data.get("data", {}).get("purchaseOrders", {}).get("shipment", {})
