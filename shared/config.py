"""
Centralized configuration.

Reads from environment variables (populated by Secret Manager in Cloud Run,
or by a .env file when running locally).
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
ENVIRONMENT       = _env("ENVIRONMENT", "sandbox")
DRY_RUN           = _env("DRY_RUN", "false").lower() == "true"
LOOKBACK_DAYS     = int(_env("LOOKBACK_DAYS", "3"))

# Function 1 specifics
ONLY_NEW_POS              = _env("ONLY_NEW_POS", "false").lower() == "true"
CLIENT_SIDE_DATE_FILTER   = _env("CLIENT_SIDE_DATE_FILTER", "true").lower() == "true"

# Function 2 specifics
FORCE_CARRIER     = _env("FORCE_CARRIER", "FEDEX")

# Wayfair shipment pickup offset (how many days after register we expect carrier pickup)
# Clamped to 2..5; configurable per environment without redeploy.
def _pickup_days():
    try:
        v = int(_env("PICKUP_OFFSET_DAYS", "3"))
    except (TypeError, ValueError):
        v = 3
    return max(2, min(5, v))

PICKUP_OFFSET_DAYS = _pickup_days()


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

# ==============================================================================
# LOCATION PRIORITY (for SO fulfillment allocation)
# ==============================================================================
# When an item has stock in multiple locations, allocate_serials_multi_location
# takes serials from these locations IN ORDER — even if other locations have
# more stock. Locations not listed fall to the end (sorted by stock size DESC).
#
# Order requested by warehouse team (2026-06-27):
#   Unit R → Unit B → Unit A → CastleGate → In-Transit
#
# NS location IDs (verified via SuiteQL 2026-06-27):
#   id=8    21Rancho-R     → Unit R
#   id=9    23322-B        → Unit B
#   id=11   23561-A        → Unit A
#   id=15   Castlegate     → CastleGate
#   id=13   In-Transit     → In transit
#
# Overridable via LOCATION_PRIORITY env var (comma-separated int IDs).
LOCATION_PRIORITY = tuple(
    int(x) for x in _env("LOCATION_PRIORITY", "8,9,11,15,13").split(",") if x.strip()
)

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
WAYFAIR_WAREHOUSE_ID  = _env("WAYFAIR_WAREHOUSE_ID", str(WAYFAIR_SUPPLIER_ID))

_WAYFAIR_BASE = "https://api.wayfair.com" if ENVIRONMENT == "production" else "https://sandbox.api.wayfair.com"
WAYFAIR_GQL_URL   = f"{_WAYFAIR_BASE}/v1/graphql"
WAYFAIR_REST_BASE = f"{_WAYFAIR_BASE}/v1"
WAYFAIR_TOKEN_URL = "https://sso.auth.wayfair.com/oauth/token"

# Where to store downloaded labels (ephemeral in Cloud Run)
LABEL_DOWNLOAD_DIR = _env("LABEL_DOWNLOAD_DIR", "/tmp/wayfair_labels")

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
# EMAIL NOTIFICATIONS (func1 — fires when SO is created with shipping label)
# ==============================================================================
EMAIL_ENABLED      = _env("EMAIL_ENABLED", "true").lower() == "true"
SMTP_HOST          = _env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT          = int(_env("SMTP_PORT", "587"))
EMAIL_ADDR         = _env("EMAIL_ADDR", "sale@maestrobath.com")
EMAIL_APP_PASSWORD = _env("EMAIL_APP_PASSWORD", "")
EMAIL_TO           = _env("EMAIL_TO", "sale@maestrobath.com")
EMAIL_CC           = [e.strip() for e in _env(
    "EMAIL_CC",
    "johnny@maestrobath.com,fernando@maestrobath.com,mehdi@maestrobath.com"
).split(",") if e.strip()]


# ==============================================================================
# BIGQUERY
# ==============================================================================
BQ_PROJECT_ID    = _env("BQ_PROJECT_ID", "maestrobath")
BQ_DATASET       = _env("BQ_DATASET",    "wayfair_inventory")

BQ_SO_LOG_TABLE   = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_so_log"
BQ_ASN_LOG_TABLE  = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_asn_log"
BQ_REG_LOG_TABLE  = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_reg_log"
BQ_SKU_MAP_TABLE  = f"{BQ_PROJECT_ID}.{BQ_DATASET}.wayfair_sku_mapper"


# ==============================================================================
# INVENTORY MAPPER
# ==============================================================================
NS_COLS = {
    "sku":     "Name",
    "on_hand": "On Hand Quantity",
    "avail":   "Available Quantity",
    "loc_av":  "Location Available",
    "loc":     "Inventory Location",
}
WF_LOC = ["- None -", "21Rancho-R", "23501-AA", "23561-A", "In-Transit", "SanMiguel-Mailing"]