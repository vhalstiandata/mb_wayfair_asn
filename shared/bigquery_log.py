"""BigQuery: client factory, log tables, dedup queries, log writes."""

import google.auth
from google.cloud import bigquery

from shared import config as cfg


# ==============================================================================
# CLIENT
# ==============================================================================
_BQ_SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_bq_client():
    credentials, project = google.auth.default(scopes=_BQ_SCOPES)
    return bigquery.Client(project=cfg.BQ_PROJECT_ID, credentials=credentials)


# ==============================================================================
# SCHEMAS
# ==============================================================================
SO_LOG_SCHEMA = [
    bigquery.SchemaField("logged_at",       "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("wayfair_po",      "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("po_date",         "TIMESTAMP", mode="NULLABLE"),
    bigquery.SchemaField("so_number",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("so_internal_id",  "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("wf_accept_id",    "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("item_count",      "INTEGER",   mode="NULLABLE"),
    bigquery.SchemaField("status",          "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("error_message",   "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("environment",     "STRING",    mode="NULLABLE"),
]

ASN_LOG_SCHEMA = [
    bigquery.SchemaField("logged_at",           "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("so_number",           "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("if_number",           "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("wayfair_po",          "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("tracking_number",     "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("carrier",             "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("wayfair_shipment_id", "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("status",              "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("error_message",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("environment",         "STRING",    mode="NULLABLE"),
]

REG_LOG_SCHEMA = [
    bigquery.SchemaField("logged_at",           "TIMESTAMP", mode="REQUIRED"),
    bigquery.SchemaField("so_number",           "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("wayfair_po",          "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("register_event_id",   "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("pickup_date",         "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("tracking_number",     "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("carrier_code",        "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("label_path",          "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("status",              "STRING",    mode="REQUIRED"),
    bigquery.SchemaField("error_message",       "STRING",    mode="NULLABLE"),
    bigquery.SchemaField("environment",         "STRING",    mode="NULLABLE"),
]


# ==============================================================================
# ENSURE TABLES
# ==============================================================================
def ensure_table(bq, table_id, schema):
    try:
        bq.get_table(table_id)
    except Exception:
        bq.create_table(bigquery.Table(table_id, schema=schema))
        print(f"Created log table: {table_id}")


def ensure_so_log_table(bq):   ensure_table(bq, cfg.BQ_SO_LOG_TABLE,  SO_LOG_SCHEMA)
def ensure_asn_log_table(bq):  ensure_table(bq, cfg.BQ_ASN_LOG_TABLE, ASN_LOG_SCHEMA)
def ensure_reg_log_table(bq):  ensure_table(bq, cfg.BQ_REG_LOG_TABLE, REG_LOG_SCHEMA)


# ==============================================================================
# SO LOG: dedup + load pending
# ==============================================================================
def so_already_processed(bq, po_number):
    q = f"""
        SELECT COUNT(*) AS c
        FROM `{cfg.BQ_SO_LOG_TABLE}`
        WHERE wayfair_po = @po AND status = 'SUCCESS'
    """
    job = bq.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("po", "STRING", po_number)]
    ))
    return list(job.result())[0].c > 0


def get_recent_successful_sos(bq, lookback_days):
    q = f"""
        SELECT wayfair_po, so_number, so_internal_id, po_date
        FROM `{cfg.BQ_SO_LOG_TABLE}`
        WHERE status = 'SUCCESS'
          AND logged_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
        ORDER BY logged_at DESC
    """
    job = bq.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("days", "INT64", lookback_days)]
    ))
    return [dict(r) for r in job.result()]


# ==============================================================================
# ASN LOG: dedup
# ==============================================================================
def asn_already_sent(bq, so_number, tracking):
    q = f"""
        SELECT COUNT(*) AS c
        FROM `{cfg.BQ_ASN_LOG_TABLE}`
        WHERE so_number = @so
          AND status = 'SUCCESS'
          AND (tracking_number = @tr OR (@tr IS NULL AND tracking_number IS NULL))
    """
    job = bq.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("so", "STRING", so_number),
            bigquery.ScalarQueryParameter("tr", "STRING", tracking),
        ]
    ))
    return list(job.result())[0].c > 0


# ==============================================================================
# REG LOG: dedup
# ==============================================================================
def registration_already_done(bq, wayfair_po):
    """Check if PO was already registered. Returns row dict or None."""
    q = f"""
        SELECT register_event_id, tracking_number, carrier_code, label_path
        FROM `{cfg.BQ_REG_LOG_TABLE}`
        WHERE wayfair_po = @po AND status = 'SUCCESS'
        ORDER BY logged_at DESC
        LIMIT 1
    """
    job = bq.query(q, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("po", "STRING", wayfair_po)]
    ))
    rows = list(job.result())
    if rows:
        return dict(rows[0])
    return None


# ==============================================================================
# WRITE
# ==============================================================================
def write_row(bq, table_id, row):
    errors = bq.insert_rows_json(table_id, [row])
    if errors:
        print(f"  BQ insert errors for {table_id}: {errors}")


def write_so_log(bq, row):   write_row(bq, cfg.BQ_SO_LOG_TABLE,  row)
def write_asn_log(bq, row):  write_row(bq, cfg.BQ_ASN_LOG_TABLE, row)
def write_reg_log(bq, row):  write_row(bq, cfg.BQ_REG_LOG_TABLE, row)


# ==============================================================================
# SKU MAP
# ==============================================================================
def get_sku_map(bq):
    return bq.query(
        f"SELECT TRIM(sku) AS oracle_sku, TRIM(wayfair_sku) AS wayfair_sku "
        f"FROM `{cfg.BQ_SKU_MAP_TABLE}` WHERE wayfair_sku IS NOT NULL"
    ).to_dataframe()
