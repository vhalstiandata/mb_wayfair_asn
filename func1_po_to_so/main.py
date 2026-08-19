"""
Cloud Run service: Wayfair PO → NetSuite SO → Register → Labels → Email.

Per PO flow:
  1. Pull dropship POs from Wayfair (last LOOKBACK_DAYS)
  2. For each PO not yet in BQ log:
       - Resolve SKUs + serials in NetSuite
       - For SKUs with no stock BUT in fixed_500 list → allow BACKORDER SO
       - Accept line items in Wayfair
       - Create Sales Order in NetSuite
       - Register shipment in Wayfair (gets tracking + label)
       - Download shipping label + packing slip PDFs
       - Email warehouse with PDFs attached
  3. Log result to BQ (wayfair_so_log + wayfair_reg_log)

ASN (shipment confirmation) is handled by func2 once Item Fulfillment exists.
"""

import json
import os
import sys
import time as time_mod
import traceback
from datetime import datetime, timedelta, timezone

import flask
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import config as cfg
from shared import netsuite as ns
from shared import wayfair  as wf
from shared import bigquery_log as bqlog
from shared import email_notify as email
from shared import sheets_lists   # ← NEW


app = flask.Flask(__name__)


# ==============================================================================
# REGISTER + LABELS + EMAIL (post-SO)
# ==============================================================================
def post_so_actions(wayfair_po, so_number, accepted_items, wf_token, bq):
    """
    After SO is created, register the shipment with Wayfair,
    download the label + packing slip, and email the warehouse.

    Idempotent via wayfair_reg_log — if PO already registered, returns early.
    All errors are non-fatal: SO is already created, this is enrichment.
    """
    existing = bqlog.registration_already_done(bq, wayfair_po)
    if existing:
        print(f"  Register: already done (event={existing.get('register_event_id')})")
        return existing

    # ----- Register -----
    print(f"  Register: calling mutation...")
    try:
        reg = wf.register_shipment(wf_token, wayfair_po)
        print(f"    Registered: event={reg.get('id')} "
              f"tracking={reg.get('trackingNumber')} carrier={reg.get('carrierCode')}")
    except Exception as e:
        print(f"    Register FAILED (non-fatal): {e}")
        bqlog.write_reg_log(bq, {
            "logged_at": datetime.utcnow().isoformat(),
            "so_number": so_number, "wayfair_po": wayfair_po,
            "register_event_id": None, "pickup_date": None,
            "tracking_number": None, "carrier_code": None, "label_path": None,
            "status": "FAILED", "error_message": str(e)[:1000],
            "environment": cfg.ENVIRONMENT,
        })
        return None

    # ----- Download labels (non-fatal) -----
    label_path = None
    packing_path = None
    bol_path = None
    try:
        label_path = wf.download_shipping_label(wf_token, wayfair_po)
    except Exception as e:
        print(f"    Label download failed (non-fatal): {e}")

    try:
        packing_path = wf.download_packing_slip(wf_token, wayfair_po)
    except Exception as e:
        print(f"    Packing slip download failed (non-fatal): {e}")

    try:
        bol_path = wf.download_bol(wf_token, wayfair_po)
        if bol_path:
            print(f"    BOL downloaded: {bol_path}")
    except Exception as e:
        print(f"    BOL download failed (non-fatal): {e}")

    reg_info = {
        "register_event_id": str(reg.get("id")) if reg.get("id") else None,
        "tracking_number":   reg.get("trackingNumber"),
        "carrier_code":      reg.get("carrierCode"),
        "label_path":        label_path,
        "packing_path":      packing_path,
        "bol_path":          bol_path,
    }

    bqlog.write_reg_log(bq, {
        "logged_at": datetime.utcnow().isoformat(),
        "so_number": so_number, "wayfair_po": wayfair_po,
        "register_event_id": reg_info["register_event_id"],
        "pickup_date": reg.get("pickupDate"),
        "tracking_number": reg_info["tracking_number"],
        "carrier_code": reg_info["carrier_code"],
        "label_path": label_path,
        "status": "SUCCESS", "error_message": None,
        "environment": cfg.ENVIRONMENT,
    })

    # ----- Email (non-fatal) -----
    try:
        email.send_so_email(
            wayfair_po, so_number, accepted_items, reg_info,
            label_path=label_path, packing_path=packing_path, bol_path=bol_path,
        )
    except Exception as e:
        print(f"    Email failed (non-fatal): {e}")

    return reg_info


# ==============================================================================
# CORE PIPELINE
# ==============================================================================
def process_po(po, wf_token, sku_map, bq, fixed_500_set):
    po_number = po["poNumber"]
    po_date   = po.get("poDate")

    print(f"\n=== PO {po_number} ===")

    # Dedup #1: BQ wayfair_so_log (our own log)
    if bqlog.so_already_processed(bq, po_number):
        print(f"  Already in log as SUCCESS — SKIP")
        return "SKIPPED_ALREADY_DONE", None

    # Dedup #2: NetSuite otherrefnum (catches SOs that warehouse created manually)
    existing_so = ns.find_so_by_otherrefnum(po_number)
    if existing_so:
        print(f"  SO already exists in NetSuite: {existing_so['tranid']} "
              f"(id {existing_so['id']}) — SKIP")
        # Sync to BQ so future runs short-circuit on dedup #1
        bqlog.write_so_log(bq, {
            "logged_at":      datetime.utcnow().isoformat(),
            "wayfair_po":     po_number,
            "po_date":        po_date,
            "so_number":      existing_so["tranid"],
            "so_internal_id": existing_so["id"],
            "wf_accept_id":   None,
            "item_count":     None,
            "status":         "SUCCESS",
            "error_message":  "Pre-existing SO in NetSuite (manual or external)",
            "environment":    cfg.ENVIRONMENT,
        })
        return "SKIPPED_ALREADY_DONE", {
            "reason":         "exists_in_ns",
            "so_number":      existing_so["tranid"],
            "so_internal_id": existing_so["id"],
        }

    accepted_items = []
    unmapped, short_stock = [], []
    backorder_lines = []   # for logging/summary

    for p in po.get("products", []):
        wf_sku = p["partNumber"]
        qty    = int(p["quantity"])
        price  = float(p.get("price") or 0)

        match = sku_map[sku_map["wayfair_sku"].str.upper() == wf_sku.upper()]
        if match.empty:
            unmapped.append(wf_sku)
            print(f"  Unmapped: {wf_sku}")
            continue
        oracle_sku = match.iloc[0]["oracle_sku"]

        item_id = ns.get_item_internal_id(oracle_sku)
        if not item_id:
            unmapped.append(f"{wf_sku} -> {oracle_sku}")
            print(f"  Oracle SKU not in NS: {oracle_sku}")
            continue

        serials_allocations = ns.allocate_serials_multi_location(item_id, qty)

        if serials_allocations:
            # Normal path — we have inventory
            if len(serials_allocations) > 1:
                split_summary = ", ".join(f"loc {a['location']}: {len(a['serials'])}"
                                          for a in serials_allocations)
                print(f"  {oracle_sku}: split across locations ({split_summary})")

            accepted_items.append({
                "wayfair_sku":   wf_sku,
                "oracle_sku":    oracle_sku,
                "ns_item_id":    item_id,
                "ordered_qty":   qty,
                "wayfair_price": price,
                "retail_price":  ns.get_item_retail_price(item_id),
                "allocations":   serials_allocations,
                "is_backorder":  False,
            })
        elif oracle_sku.upper() in fixed_500_set:
            # No inventory BUT SKU is in fixed_500 (procurement will source) → backorder
            print(f"  {oracle_sku}: no stock but in fixed_500 → BACKORDER "
                  f"(qty={qty} @ default loc {cfg.NETSUITE_DEFAULT_LOCATION_ID})")
            backorder_lines.append(oracle_sku)
            accepted_items.append({
                "wayfair_sku":   wf_sku,
                "oracle_sku":    oracle_sku,
                "ns_item_id":    item_id,
                "ordered_qty":   qty,
                "wayfair_price": price,
                "retail_price":  ns.get_item_retail_price(item_id),
                "allocations":   None,          # NO serials
                "is_backorder":  True,
            })
        else:
            short_stock.append(oracle_sku)
            print(f"  No serials for {oracle_sku} (need {qty}) — not in fixed_500 either")
            continue

    if not accepted_items:
        print(f"  No fulfillable items — SKIP")
        return "SKIPPED_NO_STOCK", {"unmapped": unmapped, "short_stock": short_stock}

    if short_stock or unmapped:
        print(f"  Partial — unmapped={unmapped}, short={short_stock} — SKIP")
        return "SKIPPED_PARTIAL", {"unmapped": unmapped, "short_stock": short_stock}

    # Accept in Wayfair
    print(f"  Accepting {len(accepted_items)} line(s) in Wayfair "
          f"({len(backorder_lines)} as backorder)...")
    wf_accept_id = wf.acknowledge_po(wf_token, po_number, accepted_items)
    print(f"  Wayfair Accept ID: {wf_accept_id}")

    # Extract Wayfair's "Must Ship By" date for NS deadline
    # estimatedShipDate format: "2026-06-15 00:00:00.000000 +00:00" → take YYYY-MM-DD
    wf_deadline = po.get("estimatedShipDate")
    if wf_deadline:
        wf_deadline = wf_deadline[:10]  # "2026-06-15"

    # Create SO in NetSuite
    print(f"  Creating SO in NetSuite (deadline={wf_deadline or 'today'})...")
    so_number, so_internal_id = ns.create_sales_order(
        po_number, accepted_items, po_date, deadline=wf_deadline
    )
    if not so_number:
        raise RuntimeError("SO creation returned no tranid")
    print(f"  SO Created: {so_number} (internal id {so_internal_id})")

    # Register + labels + email (as before)
    reg_info = post_so_actions(po_number, so_number, accepted_items, wf_token, bq)

    return "SUCCESS", {
        "wf_accept_id":     wf_accept_id,
        "so_number":        so_number,
        "so_internal_id":   so_internal_id,
        "item_count":       len(accepted_items),
        "backorder_lines":  backorder_lines,
        "registered":       bool(reg_info),
    }


def run_pipeline():
    start = time_mod.time()
    print(f"=== FUNC1: WAYFAIR PO -> NS SO -> REGISTER -> LABELS -> EMAIL   "
          f"env={cfg.ENVIRONMENT}  dry={cfg.DRY_RUN}  "
          f"pickup_offset_days={cfg.PICKUP_OFFSET_DAYS} ===")

    bq = bqlog.get_bq_client()
    bqlog.ensure_so_log_table(bq)
    bqlog.ensure_reg_log_table(bq)

    # Load fixed_500 SKU set from Google Sheets (shared with wayfair_inventory pipeline)
    fixed_500_set = sheets_lists.load_fixed_500_skus()

    raw_inv  = ns.fetch_inventory()
    wf_table = ns.build_wf_inventory_table(raw_inv)
    sku_map  = bqlog.get_sku_map(bq).merge(
        wf_table[["oracle_sku"]], on="oracle_sku", how="inner"
    )
    print(f"SKU map: {len(sku_map)} mappings")

    wf_token = wf.get_wf_token()
    from_iso = (datetime.now(timezone.utc) - timedelta(days=cfg.LOOKBACK_DAYS))\
                   .strftime("%Y-%m-%dT%H:%M:%SZ")
    open_pos = wf.get_open_orders(wf_token, from_iso, only_new=cfg.ONLY_NEW_POS)
    print(f"Fetched {len(open_pos)} PO(s) from Wayfair (only_new={cfg.ONLY_NEW_POS})")

    if cfg.CLIENT_SIDE_DATE_FILTER and open_pos:
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.LOOKBACK_DAYS)
        kept = []
        for o in open_pos:
            pd_raw = o.get("poDate")
            if not pd_raw:
                kept.append(o); continue
            po_dt = pd.to_datetime(pd_raw, utc=True, errors="coerce")
            if pd.isna(po_dt) or po_dt >= pd.Timestamp(cutoff):
                kept.append(o)
        print(f"Client-side date filter: {len(kept)}/{len(open_pos)}")
        open_pos = kept

    summary = {"SUCCESS": 0, "SKIPPED_ALREADY_DONE": 0, "SKIPPED_NO_STOCK": 0,
               "SKIPPED_PARTIAL": 0, "FAILED": 0}
    pos_processed = []

    for po in open_pos:
        po_number = po["poNumber"]
        po_date   = po.get("poDate")
        items_n   = len(po.get("products", []))

        try:
            status, detail = process_po(po, wf_token, sku_map, bq, fixed_500_set)
            summary[status] = summary.get(status, 0) + 1
            pos_processed.append({"po": po_number, "status": status})

            if status != "SKIPPED_ALREADY_DONE":
                bqlog.write_so_log(bq, {
                    "logged_at":      datetime.utcnow().isoformat(),
                    "wayfair_po":     po_number,
                    "po_date":        po_date,
                    "so_number":      (detail or {}).get("so_number"),
                    "so_internal_id": (detail or {}).get("so_internal_id"),
                    "wf_accept_id":   str((detail or {}).get("wf_accept_id")) if (detail or {}).get("wf_accept_id") else None,
                    "item_count":     (detail or {}).get("item_count", items_n),
                    "status":         status,
                    "error_message":  json.dumps(detail) if status.startswith("SKIPPED") and detail else None,
                    "environment":    cfg.ENVIRONMENT,
                })
        except Exception as e:
            summary["FAILED"] += 1
            pos_processed.append({"po": po_number, "status": "FAILED", "error": str(e)[:200]})
            print(f"  ERROR: {po_number}: {e}")
            traceback.print_exc()
            bqlog.write_so_log(bq, {
                "logged_at":      datetime.utcnow().isoformat(),
                "wayfair_po":     po_number,
                "po_date":        po_date,
                "so_number":      None, "so_internal_id": None, "wf_accept_id": None,
                "item_count":     items_n,
                "status":         "FAILED",
                "error_message":  str(e)[:1000],
                "environment":    cfg.ENVIRONMENT,
            })

    duration = round(time_mod.time() - start, 2)
    print(f"\n=== SUMMARY ({duration}s) === {summary}")
    return {"status": "ok", "summary": summary, "duration_s": duration,
            "processed": pos_processed}


# ==============================================================================
# HTTP ENDPOINT
# ==============================================================================
@app.route("/", methods=["POST", "GET"])
def handler():
    try:
        result = run_pipeline()
        return flask.jsonify(result), 200
    except Exception as e:
        print(f"FATAL: {e}")
        traceback.print_exc()
        return flask.jsonify({"status": "error", "error": str(e)[:500]}), 500


@app.route("/health", methods=["GET"])
def health():
    return flask.jsonify({"status": "healthy", "service": "func1-po-to-so"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
