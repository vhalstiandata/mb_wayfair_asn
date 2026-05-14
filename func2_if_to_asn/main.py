"""
Cloud Run service: Item Fulfillment → Wayfair ASN.

Driven by wayfair_so_log: for each SUCCESS SO in the lookback window,
check NetSuite for fulfillments and forward tracking to Wayfair.

Endpoint:
    POST /  →  runs the pipeline once

Returns JSON summary: { "status": "ok", "summary": {...}, "duration_s": N }
"""

import os
import sys
import time as time_mod
import traceback
from datetime import datetime

import flask

# Make shared/ importable when running from inside this folder
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared import config as cfg
from shared import netsuite as ns
from shared import wayfair  as wf
from shared import bigquery_log as bqlog


app = flask.Flask(__name__)


# ==============================================================================
# PER-SO PROCESSING
# ==============================================================================
def process_so(so_row, wf_token, bq):
    so_number      = so_row["so_number"]
    so_internal_id = so_row["so_internal_id"]
    wayfair_po     = so_row["wayfair_po"]

    print(f"\n=== SO {so_number} (id {so_internal_id}) → PO {wayfair_po} ===")

    ifs = ns.get_fulfillments_for_so(so_internal_id)
    if not ifs:
        print(f"  No Item Fulfillment yet — will retry next run")
        return {"asn_sent": 0, "no_if": True}

    print(f"  Found {len(ifs)} IF(s) for this SO")

    sent_count = 0
    skipped_no_tracking = 0

    for if_row in ifs:
        if_number     = if_row.get("if_number")
        tracking_raw  = if_row.get("tracking_numbers")

        if not tracking_raw or str(tracking_raw).strip() in ("-", ""):
            print(f"  • {if_number}: no tracking yet — skip")
            skipped_no_tracking += 1
            continue

        # Take first tracking if multiple
        tracking = str(tracking_raw).replace("\n", ",").split(",")[0].strip()

        if bqlog.asn_already_sent(bq, so_number, tracking):
            print(f"  • {if_number}: already sent (tracking={tracking}) — skip")
            continue

        # Carrier
        if cfg.FORCE_CARRIER:
            carrier_code = cfg.FORCE_CARRIER
        else:
            carrier_code = wf.detect_carrier_from_tracking(tracking) or "FEDEX"
        print(f"  • {if_number}: tracking={tracking} carrier={carrier_code}")

        # Fetch PO from Wayfair (shipTo + products)
        try:
            po_data = wf.get_po_data(wf_token, wayfair_po)
        except Exception as e:
            print(f"    × PO fetch failed: {e}")
            bqlog.write_asn_log(bq, {
                "logged_at": datetime.utcnow().isoformat(),
                "so_number": so_number, "if_number": if_number,
                "wayfair_po": wayfair_po,
                "tracking_number": tracking, "carrier": carrier_code,
                "wayfair_shipment_id": None,
                "status": "FAILED", "error_message": f"PO fetch: {e}"[:1000],
                "environment": cfg.ENVIRONMENT,
            })
            continue

        # Send ASN
        try:
            result = wf.send_asn(
                wf_token, wayfair_po, tracking, carrier_code,
                po_data["products"], po_data["shipTo"]
            )
            print(f"    ✓ Shipment ID: {result.get('id')} status={result.get('status')}")
            bqlog.write_asn_log(bq, {
                "logged_at": datetime.utcnow().isoformat(),
                "so_number": so_number, "if_number": if_number,
                "wayfair_po": wayfair_po,
                "tracking_number": tracking, "carrier": carrier_code,
                "wayfair_shipment_id": str(result.get("id")) if result else None,
                "status": "SUCCESS", "error_message": None,
                "environment": cfg.ENVIRONMENT,
            })
            sent_count += 1
        except Exception as e:
            print(f"    × ASN failed: {e}")
            bqlog.write_asn_log(bq, {
                "logged_at": datetime.utcnow().isoformat(),
                "so_number": so_number, "if_number": if_number,
                "wayfair_po": wayfair_po,
                "tracking_number": tracking, "carrier": carrier_code,
                "wayfair_shipment_id": None,
                "status": "FAILED", "error_message": str(e)[:1000],
                "environment": cfg.ENVIRONMENT,
            })

    return {"asn_sent": sent_count, "no_if": False,
            "skipped_no_tracking": skipped_no_tracking}


def run_pipeline():
    start = time_mod.time()
    print(f"=== FUNC2: IF → WAYFAIR ASN   env={cfg.ENVIRONMENT}   dry={cfg.DRY_RUN}   "
          f"carrier={cfg.FORCE_CARRIER or 'auto'} ===")

    bq = bqlog.get_bq_client()
    bqlog.ensure_asn_log_table(bq)

    sos = bqlog.get_recent_successful_sos(bq, cfg.LOOKBACK_DAYS)
    print(f"Loaded {len(sos)} SO(s) from {cfg.BQ_SO_LOG_TABLE}")

    if not sos:
        return {"status": "ok", "summary": {"sos": 0, "asn_sent": 0},
                "duration_s": round(time_mod.time() - start, 2)}

    wf_token = wf.get_wf_token()

    summary = {"sos_processed": 0, "asn_sent": 0, "sos_no_if": 0, "errors": 0}
    so_results = []

    for so in sos:
        summary["sos_processed"] += 1
        try:
            res = process_so(so, wf_token, bq)
            summary["asn_sent"] += res["asn_sent"]
            if res["no_if"]:
                summary["sos_no_if"] += 1
            so_results.append({"so": so.get("so_number"), **res})
        except Exception as e:
            summary["errors"] += 1
            print(f"  × ERROR processing SO {so.get('so_number')}: {e}")
            traceback.print_exc()
            so_results.append({"so": so.get("so_number"), "error": str(e)[:200]})

    duration = round(time_mod.time() - start, 2)
    print(f"\n=== SUMMARY ({duration}s) ===  {summary}")
    return {"status": "ok", "summary": summary,
            "duration_s": duration, "results": so_results}


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
    return flask.jsonify({"status": "healthy", "service": "func2-if-to-asn"}), 200


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
