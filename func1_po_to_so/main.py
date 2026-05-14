"""
Cloud Run service: Wayfair PO → NetSuite SO.

Endpoint:
    POST /  →  runs the pipeline once

Returns JSON summary: { "status": "ok", "summary": {...}, "duration_s": N }
HTTP 200 even on partial failures (Cloud Scheduler will not retry indefinitely);
detailed status per-PO lives in BigQuery (wayfair_so_log).
"""

import json
import sys
import os
import time as time_mod
import traceback
from datetime import datetime, timedelta, timezone

import flask
import pandas as pd

# Make shared/ importable when running from inside this folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import config as cfg
from shared import netsuite as ns
from shared import wayfair  as wf
from shared import bigquery_log as bqlog


app = flask.Flask(__name__)


# ==============================================================================
# CORE PIPELINE
# ==============================================================================
def process_po(po, wf_token, sku_map, bq):
    po_number = po["poNumber"]
    po_date   = po.get("poDate")

    print(f"\n=== PO {po_number} ===")

    if bqlog.so_already_processed(bq, po_number):
        print(f"  ⏭ Already in log as SUCCESS — SKIP")
        return "SKIPPED_ALREADY_DONE", None

    accepted_items = []
    unmapped, short_stock = [], []

    for p in po.get("products", []):
        wf_sku = p["partNumber"]
        qty    = int(p["quantity"])
        price  = float(p.get("price") or 0)

        match = sku_map[sku_map["wayfair_sku"] == wf_sku]
        if match.empty:
            unmapped.append(wf_sku)
            print(f"  × Unmapped: {wf_sku}")
            continue
        oracle_sku = match.iloc[0]["oracle_sku"]

        item_id = ns.get_item_internal_id(oracle_sku)
        if not item_id:
            unmapped.append(f"{wf_sku} → {oracle_sku}")
            print(f"  × Oracle SKU not in NS: {oracle_sku}")
            continue

        serials = ns.get_serials(item_id, qty)
        if not serials:
            short_stock.append(oracle_sku)
            print(f"  × No serials for {oracle_sku} (need {qty})")
            continue

        accepted_items.append({
            "wayfair_sku":   wf_sku,
            "oracle_sku":    oracle_sku,
            "ns_item_id":    item_id,
            "ordered_qty":   qty,
            "wayfair_price": price,
            "retail_price":  ns.get_item_retail_price(item_id),
            "serials":       serials,
        })

    if not accepted_items:
        print(f"  ⚠ No fulfillable items — SKIP")
        return "SKIPPED_NO_STOCK", {"unmapped": unmapped, "short_stock": short_stock}

    if short_stock or unmapped:
        print(f"  ⚠ Partial — unmapped={unmapped}, short={short_stock} — SKIP")
        return "SKIPPED_PARTIAL", {"unmapped": unmapped, "short_stock": short_stock}

    print(f"  → Accepting {len(accepted_items)} line(s) in Wayfair...")
    wf_accept_id = wf.acknowledge_po(wf_token, po_number, accepted_items)
    print(f"  ✓ Wayfair Accept ID: {wf_accept_id}")

    print(f"  → Creating SO in NetSuite...")
    so_number, so_internal_id = ns.create_sales_order(po_number, accepted_items, po_date)
    if not so_number:
        raise RuntimeError("SO creation returned no tranid")
    print(f"  ✓ SO Created: {so_number} (internal id {so_internal_id})")

    return "SUCCESS", {
        "wf_accept_id":   wf_accept_id,
        "so_number":      so_number,
        "so_internal_id": so_internal_id,
        "item_count":     len(accepted_items),
    }


def run_pipeline():
    start = time_mod.time()
    print(f"=== FUNC1: WAYFAIR PO → NS SO   env={cfg.ENVIRONMENT}   dry={cfg.DRY_RUN} ===")

    bq = bqlog.get_bq_client()
    bqlog.ensure_so_log_table(bq)

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

    # Client-side date filter (Wayfair sandbox sometimes ignores fromDate)
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
            status, detail = process_po(po, wf_token, sku_map, bq)
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
            print(f"  × ERROR processing {po_number}: {e}")
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
    print(f"\n=== SUMMARY ({duration}s) ===  {summary}")
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
        print(f"× FATAL: {e}")
        traceback.print_exc()
        return flask.jsonify({"status": "error", "error": str(e)[:500]}), 500


@app.route("/health", methods=["GET"])
def health():
    return flask.jsonify({"status": "healthy", "service": "func1-po-to-so"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
