"""BigQuery: client factory, log tables, dedup queries, log writes."""

import google.auth
from google.cloud import bigquery

from shared import config as cfg


# ==============================================================================
# CLIENT
# ==============================================================================
# Scopes required for BQ + external tables that read from Google Drive/Sheets.
# Cloud Run's default token only has cloud-platform scope — without drive scope,
# BQ refuses to query external Sheets tables.
_BQ_SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
]


def get_bq_client() -> bigquery.Client:
    """
    Uses Application Default Credentials (ADC) with explicit Drive scope so
    BigQuery external tables backed by Google Sheets work.

    On Cloud Run → the runtime service account.
    Locally → run `gcloud auth application-default login` once.
    """
    credentials, project = google.auth.default(scopes=_BQ_SCOPES)
    return bigquery.Client(project=cfg.BQ_PROJECT_ID, credentials=credentials)