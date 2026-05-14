"""NetSuite OAuth, Restlet, SuiteQL, and REST record helpers."""

import time
import uuid
import hmac
import hashlib
import base64
import urllib.parse
import io

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
    """Execute a SuiteQL query. Returns list of dicts (rows) or None on error."""
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


def get_serials(item_id: str, qty: int):
    """Reserve `qty` serials from a single location (smallest by serial number)."""
    excluded = ", ".join(str(x) for x in cfg.EXCLUDED_LOCATIONS)
    rows = ns_suiteql(f"""
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
    """)
    if not rows:
        return None
    smallest_location = rows[0].get("location")
    same_loc = [s for s in rows if s.get("location") == smallest_location]
    if len(same_loc) < qty:
        return None
    return same_loc[:qty]


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
    """Returns list of IFs (one row per IF) linked to the given SO."""
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


# ==============================================================================
# INVENTORY (via Restlet)
# ==============================================================================
def fetch_inventory() -> pd.DataFrame:
    """Pull current inventory via NetSuite Restlet (CSV / JSON)."""
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
    """Aggregate raw inventory across Wayfair-eligible locations."""
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
# CREATE SALES ORDER
# ==============================================================================
def create_sales_order(po_number: str, accepted_items: list, po_date: str = None):
    """
    Create a Sales Order with serial assignments and per-line discount lines.
    Returns (tranid, internal_id) on success, (None, None) on no-op,
    raises RuntimeError on API failure.
    """
    if cfg.DRY_RUN:
        print("  DRY_RUN — skip SO creation")
        return None, None

    ns_items = []
    for item in accepted_items:
        item_id = item["ns_item_id"]
        serials = item["serials"]
        qty     = int(item["ordered_qty"])

        item_location_id = str(serials[0].get("location", cfg.NETSUITE_DEFAULT_LOCATION_ID))
        print(f"  • {item['oracle_sku']}: serials "
              f"{[s.get('serial_number') for s in serials]} @ loc {item_location_id}")

        ns_items.append({
            "item":     {"id": item_id},
            "quantity": qty,
            "location": {"id": item_location_id},
            "inventoryDetail": {
                "inventoryAssignment": {
                    "items": [
                        {"issueInventoryNumber": {"id": str(s["serial_id"])}, "quantity": 1}
                        for s in serials
                    ]
                }
            }
        })

        # Discount line
        wayfair_price = float(item.get("wayfair_price") or 0)
        retail        = item.get("retail_price")
        if retail is None:
            print(f"    ⚠ No Base Price for {item['oracle_sku']} — discount skipped")
        elif wayfair_price <= 0:
            print(f"    ⚠ Wayfair price 0 for {item['oracle_sku']} — discount skipped")
        else:
            d_unit  = round(retail - cfg.WAYFAIR_NET_FACTOR * wayfair_price, 2)
            d_total = round(d_unit * qty, 2)
            if d_unit <= 0:
                print(f"    ⚠ retail ≤ 0.83×wayfair — discount skipped")
            else:
                ns_items.append({
                    "item":     {"id": cfg.DISCOUNT_ITEM_ID},
                    "quantity": 1,
                    "rate":     -d_total
                })
                print(f"    + discount: retail={retail}, wayfair={wayfair_price}, total=-{d_total}")

    if not ns_items:
        return None, None

    payload = {
        "entity":      {"id": cfg.NETSUITE_WAYFAIR_CUSTOMER_ID},
        "otherrefnum": po_number,
        "subsidiary":  {"id": cfg.NETSUITE_SUBSIDIARY_ID},
        "location":    {"id": cfg.NETSUITE_DEFAULT_LOCATION_ID},
        "item":        {"items": ns_items},
        "custbody_mb_ready_to_ship": True,
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

    # SO creation returns 204 No Content; query SuiteQL to fetch the id/tranid
    time.sleep(2)
    rows = ns_suiteql(
        f"SELECT id, tranid FROM salesorder "
        f"WHERE otherrefnum = '{po_number}' ORDER BY id DESC"
    )
    if not rows:
        return None, None
    return rows[0].get("tranid"), str(rows[0].get("id"))
