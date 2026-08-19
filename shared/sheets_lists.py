"""
Load SKU classification lists from Google Sheets.

Currently only fixed_500_sku is used by func1_po_to_so:
  - SKUs listed here have no NS inventory (usually), but we commit to Wayfair
    as `quantityOnHand=500` because procurement can source them on demand.
  - When a Wayfair PO comes for one of these SKUs, we create the SO in NS
    as a BACKORDER (without inventoryDetail / serial assignment) instead of
    skipping the PO.

Reads directly from the same Google Sheet used by wayfair_inventory pipeline
so both stay in sync.
"""

import os
import google.auth
from googleapiclient.discovery import build


# Same Sheet ID / tab as wayfair_inventory pipeline
SHEET_ID          = os.getenv("INVENTORY_SHEET_ID", "1iMNP7g1-iTz7qYxtCHQ0c7k0zIEp7wN4Jz38zuuD_jE")
FIXED_500_TAB     = os.getenv("FIXED_500_SHEET_NAME", "fixed_500_sku")

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_service():
    creds, _ = google.auth.default(scopes=_SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def load_fixed_500_skus() -> set:
    """
    Return a set of Oracle SKUs (MB SKUs) that should be allowed as backorder.
    Reads column A of the fixed_500_sku tab, first row is header.
    Empty rows and header row are skipped. All SKUs uppercased and trimmed.
    """
    try:
        svc = _get_service()
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID,
            range=f"{FIXED_500_TAB}!A:A",
        ).execute()
        rows = result.get("values", [])
        skus = set()
        for i, row in enumerate(rows):
            if i == 0:
                continue  # header
            if not row:
                continue
            sku = str(row[0]).strip().upper()
            if sku:
                skus.add(sku)
        print(f"  Loaded {len(skus)} fixed_500 SKUs from sheet")
        return skus
    except Exception as e:
        print(f"  ⚠ Failed to load fixed_500 SKUs from sheet: {e}")
        return set()
