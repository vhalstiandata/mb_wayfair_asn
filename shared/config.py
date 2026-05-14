"""
Centralized configuration.

Reads from environment variables (populated by Secret Manager in Cloud Run,
or by a .env file when running locally).

Non-secret config has sensible defaults. Secrets must be set explicitly.
"""

import os


def _env(name: str, default: str = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# ==============================================================================
# RUNTIME FLAGS
# ==============================================================================
ENVIRONMENT       = _env("ENVIRONMENT", "sandbox")         # sandbox | production
DRY_RUN           = _env("DRY_RUN", "false").lower() == "true"
LOOKBACK_DAYS     = int(_env("LOOKBACK_DAYS", "3"))

# Function 1 specifics
ONLY_NEW_POS              = _env("ONLY_NEW_POS", "false").lower() == "true"
CLIENT_SIDE_DATE_FILTER   = _env("CLIENT_SIDE_DATE_FILTER", "true").lower() == "true"

# Function 2 specifics
FORCE_CARRIER     = _env("FORCE_CARRIER", "FEDEX")          # set to "" for auto-detect


# ==============================================================================
# NETSUITE
# ==============================================================================
NETSUITE_REALM           = _env("NETSUITE_REALM", required=True)
NETSUITE_CONSUMER_KEY    = _env("NETSUITE_CONSUMER_KEY", required=True)
NETSUITE_CONSUMER_SECRET = _env("NETSUITE_CONSUMER_SECRET", required=True)
NETSUITE_TOKEN           = _env("NETSUITE_TOKEN", required=True)
NETSUITE_TOKEN_SECRET    = _env("NETSUITE_TOKEN_SECRET", required=True)

NETSUITE_RESTLET_URL    = f"https://{NETSUITE_REALM}.restlets.api.netsuite.com/app/site/hosting/restlet.nl"
NETSUITE_SUITETALK_BASE = f"https://{NETSUITE_REALM}.suitetalk.api.netsuite.com/services/rest"

NETSUITE_INVENTORY_RESTLET = {
    "script": _env("NETSUITE_INVENTORY_SCRIPT", "1386"),
    "deploy": _env("NETSUITE_INVENTORY_DEPLOY", "2"),
}

NETSUITE_WAYFAIR_CUSTOMER_ID = _env("NETSUITE_WAYFAIR_CUSTOMER_ID", "18")
NETSUITE_SUBSIDIARY_ID       = _env("NETSUITE_SUBSIDIARY_ID", "2")
NETSUITE_DEFAULT_LOCATION_ID = _env("NETSUITE_DEFAULT_LOCATION_ID", "8")

EXCLUDED_LOCATIONS = tuple(int(x) for x in _env("EXCLUDED_LOCATIONS", "15,20").split(","))

# Discount
WAYFAIR_NET_FACTOR   = float(_env("WAYFAIR_NET_FACTOR", "0.83"))
DISCOUNT_ITEM_ID     = _env("DISCOUNT_ITEM_ID", "10463")
RETAIL_PRICELEVEL_ID = int(_env("RETAIL_PRICELEVEL_ID", "1"))


# ==============================================================================
# WAYFAIR
# ==============================================================================
WAYFAIR_CLIENT_ID     = _env("WAYFAIR_CLIENT_ID", required=True)
WAYFAIR_CLIENT_SECRET = _env("WAYFAIR_CLIENT_SECRET", required=True)
WAYFAIR_SUPPLIER_ID   = int(_env("WAYFAIR_SUPPLIER_ID", "267342"))

_WAYFAIR_BASE = "https://api.wayfair.com" if ENVIRONMENT == "production" else "https://sandbox.api.wayfair.com"
WAYFAIR_GQL_URL   = f"{_WAYFAIR_BASE}/v1/graphql"
WAYFAIR_TOKEN_URL = "https://sso.auth.wayfair.com/oauth/token"

# Source address — UPDATE TO REAL WAREHOUSE ADDRESS BEFORE PRODUCTION
SOURCE_ADDRESS = {
    "name":           _env("SOURCE_ADDR_NAME",     "Maestro Bath Warehouse"),
    "streetAddress1": _env("SOURCE_ADDR_STREET1",  "123 Warehouse Street"),
    "streetAddress2": _env("SOURCE_ADDR_STREET2",  ""),
    "city":           _env("SOURCE_ADDR_CITY",     "Los Angeles"),
    "state":          _env("SOURCE_ADDR_STATE",    "CA"),
    "postalCode":     _env("SOURCE_ADDR_POSTAL",   "90001"),
    "country":        _env("SOURCE_ADDR_COUNTRY",  "US"),
}


# ==============================================================================
# BIGQUERY
# ==============================================================================
BQ_PROJECT_ID   = _env("BQ_PROJECT_ID", "maestrobath")
BQ_DATASET      = _env("BQ_DATASET",    "wayfair_inventory")

BQ_SO_LOG_TABLE  = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_so_log"
BQ_ASN_LOG_TABLE = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_asn_log"
BQ_SKU_MAP_TABLE = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_sku_mapper"


# ==============================================================================
# INVENTORY MAPPER (Restlet column names + Wayfair-eligible NS locations)
# ==============================================================================
NS_COLS = {
    "sku":     "Name",
    "on_hand": "On Hand Quantity",
    "avail":   "Available Quantity",
    "loc_av":  "Location Available",
    "loc":     "Inventory Location",
}
WF_LOC = ["- None -", "21Rancho-R", "23501-AA", "23561-A", "In-Transit", "SanMiguel-Mailing"]
