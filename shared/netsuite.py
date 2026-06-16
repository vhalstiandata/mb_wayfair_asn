"""NetSuite OAuth, Restlet, SuiteQL, and REST record helpers."""

import time
import uuid
import hmac
import hashlib
import base64
import urllib.parse
import io
from datetime import datetime 

import requests
import pandas as pd

from shared import config as cfg
from shared.http_helpers import urllib_post


# ==============================================================================
# OAUTH HEADERS
# ==============================================================================
def _oauth_header(method: str, url: str, query_params: dict = None) -> str:
    """OAuth 1.0 HMAC-SHA256 header for both Restlet (with query params)
    and REST API (without). Pass query_params for Restlets that need them."""
    nonce = uuid.uuid4().hex
    ts    = str(int(time.time()))

    oauth = {
        "oauth_consumer_key":     cfg.NETSUITE_CONSUMER_KEY,
        "oauth_token":            cfg.NETSUITE_TOKEN,
        "oauth_nonce":            nonce,
        "oauth_timestamp":        ts,
        "oauth_signature_method": "HMAC-SHA256",
        "oauth_version":          "1.0",
    }

    all_params = {**oauth, **(query_params or {})}
    pstr = "&".join(
        f"{urllib.parse.quote_plus(k)}={urllib.parse.quote_plus(str(v))}"
        for k, v in sorted(all_params.items())
    )
    base = "&".join([
        method.upper(),
        urllib.parse.quote_plus(url),
        urllib.parse.quote_plus(pstr),
    ])
    key = "&".join([
        urllib.parse.quote_plus(cfg.NETSUITE_CONSUMER_SECRET),
        urllib.parse.quote_plus(cfg.NETSUITE_TOKEN_SECRET),
    ])
    sig = base64.b64encode(
        hmac.new(key.encode(), base.encode(), hashlib.sha256).digest()
    ).decode()
    return ",".join([
        f'OAuth realm="{cfg.NETSUITE_REALM}"',
        *[f'{k}="{v}"' for k, v in oauth.items()],
        f'oauth_signature="{urllib.parse.quote(sig)}"',
    ])


def ns_oauth_header_restlet(method: str, url: str, query_params: dict) -> str:
    return _oauth_header(method, url, query_params)


def ns_oauth_header_rest_api(method: str, url: str) -> str:
    return _oauth_header(method, url, None)


# ==============================================================================
# SUITEQL
# ==============================================================================
def ns_suiteql(query: str):
    url = f"{cfg.NETSUITE_SUITETALK_BASE}/query/v1/suiteql"
    headers = {
        "Authorization": ns_oauth_header_rest_api("POST", url),
        "Content-Type":  "application/json",
        "Prefer":        "transient",
    }
    status, data = urllib_post(url, {"q": query}, headers)
    if status != 200:
        print(f"  ⚠ SuiteQL error {status}: {str(data)[:300]}")
        return None
    return data.get("items", [])


def get_item_internal_id(oracle_sku: str):
    rows = ns_suiteql(f"SELECT id FROM item WHERE itemid = '{oracle_sku}'")
    return str(rows[0]["id"]) if rows else None


def _fetch_all_available_serials(item_id: str):
    """
    Returns all available serials across all non-excluded locations,
    sorted by inventorynumber ASC.
    """
    excluded = ", ".join(str(x) for x in cfg.EXCLUDED_LOCATIONS)
    return ns_suiteql(f"""
        SELECT
            invNum.id              AS serial_id,
            invNum.inventorynumber AS serial_number,
            invLoc.location,
            invLoc.quantityavailable AS qty_available
        FROM inventorynumber invNum
        JOIN inventorynumberlocation invLoc
            ON invNum.id = invLoc.inventorynumber
        WHERE invNum.item = {item_id}
          AND invLoc.quantityavailable > 0
          AND invLoc.location NOT IN ({excluded})
        ORDER BY invNum.inventorynumber ASC
    """) or []


def allocate_serials_multi_location(item_id: str, qty_needed: int):
    """
    Return a list of location-grouped serial allocations, sorted by location size DESC.
    Each entry: { "location": "<loc_id>", "serials": [ {serial_id, serial_number, ...}, ... ] }
    Returns None if total available across all locations < qty_needed.

    Greedy: location with most stock first, take min(qty_needed_remaining, loc_qty).
    """
    all_serials = _fetch_all_available_serials(item_id)
    if not all_serials:
        return None

    # Group by location
    by_loc = {}
    for s in all_serials:
        loc = str(s.get("location"))
        by_loc.setdefault(loc, []).append(s)

    total = sum(len(v) for v in by_loc.values())
    if total < qty_needed:
        return None  # global shortage

    # Locations sorted by stock count DESC (largest first)
    locations_sorted = sorted(by_loc.keys(), key=lambda l: len(by_loc[l]), reverse=True)

    allocations = []
    remaining = qty_needed
    for loc in locations_sorted:
        if remaining <= 0:
            break
        loc_serials = by_loc[loc]
        take = min(remaining, len(loc_serials))
        allocations.append({
            "location": loc,
            "serials":  loc_serials[:take],
        })
        remaining -= take

    return allocations


# Backwards compatibility: keep old single-location helper available.
# Returns serials list (single location) OR None — preserves prior behaviour.
def get_serials(item_id: str, qty: int):
    allocs = allocate_serials_multi_location(item_id, qty)
    if not allocs:
        return None
    if len(allocs) == 1:
        return allocs[0]["serials"]
    # Multi-location → caller should use allocate_serials_multi_location directly.
    # Return all flat serials so callers that don't yet know about multi-loc still work.
    return [s for a in allocs for s in a["serials"]]


def get_item_retail_price(item_id: str):
    rows = ns_suiteql(f"""
        SELECT price FROM itemprice
        WHERE item = {item_id} AND pricelevel = {cfg.RETAIL_PRICELEVEL_ID}
    """)
    if not rows:
        return None
    try:
        return float(rows[0]["price"])
    except (KeyError, ValueError, TypeError):
        return None


def get_fulfillments_for_so(so_internal_id: str):
    return ns_suiteql(f"""
        SELECT
            t.id                              AS if_id,
            t.tranid                          AS if_number,
            t.trandate                        AS if_date,
            BUILTIN.DF(t.status)              AS status,
            BUILTIN.DF(t.trackingnumberlist)  AS tracking_numbers,
            BUILTIN.DF(ts.shippingmethod)     AS shipmethod
        FROM transaction t
        INNER JOIN transactionline tl
            ON tl.transaction = t.id AND tl.mainline = 'T'
        LEFT JOIN transactionshipment ts
            ON ts.doc = t.id
        WHERE t.type = 'ItemShip'
          AND tl.createdfrom = {so_internal_id}
        ORDER BY t.id ASC
    """) or []


def get_so_items(so_internal_id: str):
    """Return list of {oracle_sku, ordered_qty} for an SO (for email body)."""
    rows = ns_suiteql(f"""
        SELECT
            BUILTIN.DF(tl.item)  AS oracle_sku,
            tl.quantity          AS qty
        FROM transactionline tl
        WHERE tl.transaction = {so_internal_id}
          AND tl.mainline = 'F'
          AND tl.itemtype IN ('InvtPart', 'NonInvtPart', 'Kit', 'Assembly')
          AND tl.quantity > 0
        ORDER BY tl.linesequencenumber ASC
    """) or []
    return [
        {
            "wayfair_sku": "",
            "oracle_sku":  r.get("oracle_sku") or "?",
            "ordered_qty": int(float(r.get("qty") or 0)),
        }
        for r in rows
    ]


# ==============================================================================
# INVENTORY (via Restlet)
# ==============================================================================
def fetch_inventory() -> pd.DataFrame:
    params = cfg.NETSUITE_INVENTORY_RESTLET
    headers = {
        "Authorization": ns_oauth_header_restlet("GET", cfg.NETSUITE_RESTLET_URL, params),
        "Accept": "*/*",
    }
    r = requests.get(cfg.NETSUITE_RESTLET_URL, headers=headers, params=params, timeout=60)
    r.raise_for_status()
    if "text/csv" in r.headers.get("Content-Type", ""):
        return pd.read_csv(io.StringIO(r.text))
    return pd.json_normalize(r.json())


def build_wf_inventory_table(df: pd.DataFrame) -> pd.DataFrame:
    sku_col = cfg.NS_COLS["sku"]

    def choose_qty(row):
        sku = str(row[sku_col])
        return row[cfg.NS_COLS["avail"]] if sku.startswith(("SET-", "VANK-")) else row[cfg.NS_COLS["loc_av"]]

    df["qty"] = df.apply(choose_qty, axis=1).fillna(0).astype(int)
    grp  = df.groupby([sku_col, cfg.NS_COLS["loc"]], as_index=False)["qty"].sum()
    wide = grp.pivot_table(
        index=sku_col, columns=cfg.NS_COLS["loc"], values="qty",
        aggfunc="sum", fill_value=0
    ).astype(int).reset_index()
    wf_df = wide[[sku_col] + [c for c in cfg.WF_LOC if c in wide.columns]].copy()
    for col in cfg.WF_LOC:
        wf_df[col] = wf_df.get(col, 0)
    wf_df["Total"] = wf_df[cfg.WF_LOC].sum(axis=1)
    wf_df.rename(columns={sku_col: "oracle_sku"}, inplace=True)
    return wf_df[["oracle_sku", "Total"] + cfg.WF_LOC]


# ==============================================================================
# CREATE SALES ORDER (multi-location aware)
# ==============================================================================
def create_sales_order(po_number: str, accepted_items: list, po_date: str = None, deadline: str = None):
    """
    Create a Sales Order. Each item may span multiple locations:
    accepted_items[i] should contain "allocations" — list of
    {"location": "<loc_id>", "serials": [...]}. If absent, falls back to old
    "serials" key (single location). For each allocation we create:
      - 1 NS line for the item (qty=len(serials), location=loc, serials assigned)
      - 1 discount line (qty=1, rate = -(disc_per_unit * qty)) — if applicable
    """
    if cfg.DRY_RUN:
        print("  DRY_RUN — skip SO creation")
        return None, None

    ns_items = []
    for item in accepted_items:
        item_id       = item["ns_item_id"]
        oracle_sku    = item.get("oracle_sku", "?")
        wayfair_price = float(item.get("wayfair_price") or 0)
        retail        = item.get("retail_price")

        # New schema: allocations. Old schema: flat serials (single loc).
        allocations = item.get("allocations")
        if not allocations:
            serials = item.get("serials") or []
            if not serials:
                print(f"  × {oracle_sku}: no allocations/serials — skipped")
                continue
            allocations = [{
                "location": str(serials[0].get("location", cfg.NETSUITE_DEFAULT_LOCATION_ID)),
                "serials":  serials,
            }]

        for alloc in allocations:
            loc_id  = str(alloc["location"])
            serials = alloc["serials"]
            qty     = len(serials)
            if qty <= 0:
                continue

            print(f"  • {oracle_sku}: serials "
                  f"{[s.get('serial_number') for s in serials]} @ loc {loc_id} (qty={qty})")

            ns_items.append({
                "item":     {"id": item_id},
                "quantity": qty,
                "location": {"id": loc_id},
                "inventoryDetail": {
                    "inventoryAssignment": {
                        "items": [
                            {"issueInventoryNumber": {"id": str(s["serial_id"])}, "quantity": 1}
                            for s in serials
                        ]
                    }
                }
            })

            # ---- Discount line for THIS location's quantity ----
            if retail is None:
                print(f"    ⚠ No Base Price for {oracle_sku} — discount skipped for this line")
                continue
            if wayfair_price <= 0:
                print(f"    ⚠ Wayfair price 0 for {oracle_sku} — discount skipped for this line")
                continue
            d_unit  = round(retail - cfg.WAYFAIR_NET_FACTOR * wayfair_price, 2)
            if d_unit <= 0:
                print(f"    ⚠ retail ≤ 0.83×wayfair — discount skipped for this line")
                continue
            d_total = round(d_unit * qty, 2)
            ns_items.append({
                "item":     {"id": cfg.DISCOUNT_ITEM_ID},
                "quantity": 1,
                "rate":     -d_total,
            })
            print(f"    + discount: retail={retail}, wayfair={wayfair_price}, "
                  f"unit=-{d_unit}, line_total=-{d_total}")

    if not ns_items:
        return None, None

    
    deadline_today = deadline or datetime.utcnow().strftime("%Y-%m-%d")

    payload = {
        "entity":      {"id": cfg.NETSUITE_WAYFAIR_CUSTOMER_ID},
        "otherrefnum": po_number,
        "subsidiary":  {"id": cfg.NETSUITE_SUBSIDIARY_ID},
        "location":    {"id": cfg.NETSUITE_DEFAULT_LOCATION_ID},
        "item":        {"items": ns_items},
        "custbody_mb_ready_to_ship": True,
        "custbody_mb_so_deadline":   deadline_today,   
        "istaxable":   False,
    }
    if po_date:
        payload["trandate"] = po_date

    url = f"{cfg.NETSUITE_SUITETALK_BASE}/record/v1/salesorder"
    headers = {
        "Authorization": ns_oauth_header_rest_api("POST", url),
        "Content-Type":  "application/json",
    }
    status, data = urllib_post(url, payload, headers)
    if status not in (201, 204):
        raise RuntimeError(f"SO creation failed: HTTP {status}, body={str(data)[:1500]}")

    time.sleep(2)
    rows = ns_suiteql(
        f"SELECT id, tranid FROM salesorder "
        f"WHERE otherrefnum = '{po_number}' ORDER BY id DESC"
    )
    if not rows:
        return None, None
    return rows[0].get("tranid"), str(rows[0].get("id"))


# ==============================================================================
# DEDUP: find existing SO by Wayfair PO (otherrefnum)
# ==============================================================================
def find_so_by_otherrefnum(po_number: str):
    """
    Find existing Sales Order in NetSuite by Wayfair PO number (otherrefnum).
    Returns dict {id, tranid, trandate} or None.

    Used for deduplication — covers both programmatically-created SOs AND
    SOs that warehouse staff created manually. Prevents duplicate SOs from
    being generated when func1 runs.

    Safe — read-only SuiteQL query.
    """
    if not po_number:
        return None
    # Escape single quotes in PO (paranoid; PO numbers shouldn't contain them)
    safe_po = str(po_number).replace("'", "''")
    rows = ns_suiteql(
        f"SELECT id, tranid, trandate FROM salesorder "
        f"WHERE otherrefnum = '{safe_po}' "
        f"ORDER BY id DESC FETCH FIRST 1 ROWS ONLY"
    )
    if not rows:
        return None
    return {
        "id":       str(rows[0].get("id")),
        "tranid":   rows[0].get("tranid"),
        "trandate": rows[0].get("trandate"),
    }
