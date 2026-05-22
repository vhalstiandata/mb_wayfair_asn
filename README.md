# Wayfair ↔ NetSuite Integration

Two Cloud Run services that automate dropship order fulfillment between Wayfair and NetSuite.

## Architecture

```
                      ┌─────────────────────────────┐
                      │   WAYFAIR (sandbox/prod)    │
                      └──────────┬──────────────────┘
                                 │ PO arrives
                                 ▼
              ┌──────────────────────────────────────┐
              │  Cloud Scheduler triggers every 10m  │
              └──────────────┬───────────────────────┘
                             ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  func1 — wayfair-func1-po-to-so                              │
   │                                                              │
   │  1. Pull POs from Wayfair (last 3 days)                      │
   │  2. For each PO not yet in wayfair_so_log:                   │
   │     a. SKU map check (BQ external Sheets)                    │
   │     b. Serials lookup (NS SuiteQL)                           │
   │     c. Accept in Wayfair                                     │
   │     d. Create SO in NetSuite (with discount line)            │
   │     e. Register shipment in Wayfair                          │
   │        → returns tracking + carrier (Wayfair-assigned)       │
   │     f. Download shipping label PDF                           │
   │     g. Download packing slip PDF                             │
   │     h. Email warehouse with PDFs                             │
   │  3. Log to BQ                                                │
   └──────┬───────────────┬───────────────────────────────┬───────┘
          │ SO created    │ Registration done       Email sent
          ▼               ▼                                ▼
   ┌─────────┐    ┌──────────────┐              ┌──────────────────┐
   │  BQ     │    │   BQ         │              │ sale@maestrobath │
   │ so_log  │    │  reg_log     │              │ + 4 cc           │
   └────┬────┘    └──────────────┘              └──────────────────┘
        │
        │ (polled every 15 min)
        ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  Warehouse staff:                                            │
   │    1. Opens email, prints PDFs                               │
   │    2. Packages product                                       │
   │    3. Sticks Wayfair label on box                            │
   │    4. Hands to FedEx (or whatever carrier Wayfair chose)     │
   │    5. Creates Item Fulfillment in NetSuite                   │
   └──────────────┬───────────────────────────────────────────────┘
                  ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  func2 — wayfair-func2-if-to-asn (every 15 min)              │
   │                                                              │
   │  1. Read SOs from wayfair_so_log (last 3 days, SUCCESS)      │
   │  2. For each SO: SuiteQL query → find IF                     │
   │  3. If IF exists:                                            │
   │     a. Read tracking+carrier from wayfair_reg_log            │
   │        (NS tracking is fallback only)                        │
   │     b. Get PO destination from Wayfair                       │
   │     c. Send ASN (shipment mutation) to Wayfair               │
   │  4. Log to BQ                                                │
   └────────────┬─────────────────────────────────────────────────┘
                ▼
   ┌──────────────────┐
   │  BQ  asn_log     │
   └──────────────────┘
```

## Service summary

| Service | Schedule | What it does |
|---|---|---|
| **func1** `wayfair-func1-po-to-so` | every 10 min | Pulls new dropship POs from Wayfair → accepts line items → creates Sales Orders in NetSuite (with serial numbers + line-level discount) → registers shipment in Wayfair → downloads shipping label + packing slip PDFs → emails warehouse |
| **func2** `wayfair-func2-if-to-asn` | every 15 min | For each SO created by func1, checks if Item Fulfillment exists in NetSuite → forwards tracking number to Wayfair as ASN |

## Project structure

```
mb_wayfair_asn/
├── shared/                       # Shared modules used by both services
│   ├── config.py                 # Env-based configuration (secrets, flags)
│   ├── http_helpers.py           # urllib wrappers (POST JSON, GET binary)
│   ├── netsuite.py               # OAuth, SuiteQL, serials, SO creation
│   ├── wayfair.py                # GraphQL: token, get POs, accept, register, labels, ASN
│   ├── bigquery_log.py           # Logging tables (so/asn/reg) + dedup + SKU map
│   └── email_notify.py           # Gmail SMTP with PDF attachments
├── func1_po_to_so/
│   ├── main.py                   # Flask app — full PO→SO→Register→Labels→Email flow
│   ├── Dockerfile
│   └── requirements.txt
├── func2_if_to_asn/
│   ├── main.py                   # Flask app — pure IF→ASN
│   ├── Dockerfile
│   └── requirements.txt
├── .github/workflows/
│   ├── deploy-func1.yml          # GitHub Actions: WIF auth → Docker build → Cloud Run
│   └── deploy-func2.yml
├── scripts/
│   ├── setup.sh                  # One-time GCP setup (APIs, SAs, WIF, Secret Manager)
│   ├── setup-scheduler.sh        # Creates Cloud Scheduler jobs
│   └── rollback.sh               # Rolls Cloud Run service to previous revision
├── DEPLOY_GUIDE.md               # Step-by-step upgrade/deploy guide
└── README.md                     # This file
```

## Configuration

All config is read from environment variables (Cloud Run pulls these from Secret Manager + variables defined in workflows).

### Runtime variables (non-secret)

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `sandbox` | `sandbox` or `production` — switches Wayfair API base URL |
| `DRY_RUN` | `false` | If `true`, skips actual Wayfair/NetSuite mutations |
| `LOOKBACK_DAYS` | `3` | How far back to look for POs / SOs |
| `PICKUP_OFFSET_DAYS` | `3` | Days after register to schedule carrier pickup. Clamped 2..5. |
| `FORCE_CARRIER` | `FEDEX` | Used by func2 when Wayfair didn't return one |
| `WAYFAIR_WAREHOUSE_ID` | `267342` | Wayfair-assigned warehouse identifier |
| `WAYFAIR_NET_FACTOR` | `0.83` | Net-of-discount factor for retail-price calculation |
| `RETAIL_PRICELEVEL_ID` | `1` | NetSuite price level used for retail price (Base Price) |
| `DISCOUNT_ITEM_ID` | `10463` | NS internal id of the Discount item used in SO |
| `EXCLUDED_LOCATIONS` | `15,20` | NS location ids to exclude when picking serials |
| `EMAIL_ENABLED` | `true` | Toggle off to silence emails |
| `EMAIL_TO` | `sale@maestrobath.com` | Primary recipient |
| `EMAIL_CC` | (johnny, fernando, mehdi, data) @ maestrobath.com | Comma-separated |

### Secrets (Google Secret Manager)

| Secret | Used by |
|---|---|
| `netsuite-realm` | both |
| `netsuite-consumer-key` | both |
| `netsuite-consumer-secret` | both |
| `netsuite-token` | both |
| `netsuite-token-secret` | both |
| `wayfair-client-id` | both |
| `wayfair-client-secret` | both |
| `email-app-password` | func1 only |

## Deployment

GitHub Actions deploys both services automatically on push to `main`. See `DEPLOY_GUIDE.md` for the full step-by-step (Secret Manager setup, GitHub variables, scheduler).

### Manual smoke test

```bash
# func1 — full PO → SO → Register → Labels → Email
SVC=$(gcloud run services describe wayfair-func1-po-to-so \
        --region=us-central1 --project=maestrobath --format='value(status.url)')
curl -X POST "$SVC/" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json"

# func2 — IF → ASN
SVC2=$(gcloud run services describe wayfair-func2-if-to-asn \
         --region=us-central1 --project=maestrobath --format='value(status.url)')
curl -X POST "$SVC2/" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H "Content-Type: application/json"
```

### Manual scheduler trigger

```bash
gcloud scheduler jobs run wayfair-func1-po-to-so --location=us-central1
gcloud scheduler jobs run wayfair-func2-if-to-asn --location=us-central1
```

### Tweak env var without redeploy

```bash
gcloud run services update wayfair-func1-po-to-so \
  --region=us-central1 \
  --update-env-vars="PICKUP_OFFSET_DAYS=4"
```

## Logging

Three layers of observability.

### 1. BigQuery (durable, for analytics)

| Table | Written when | Key columns |
|---|---|---|
| `wayfair_so_log` | Each PO processed | `wayfair_po`, `so_number`, `so_internal_id`, `wf_accept_id`, `item_count`, `status`, `error_message`, `po_date`, `logged_at` |
| `wayfair_reg_log` | Each Wayfair registration | `wayfair_po`, `so_number`, `register_event_id`, `pickup_date`, `tracking_number`, `carrier_code`, `label_path`, `status`, `error_message`, `logged_at` |
| `wayfair_asn_log` | Each ASN sent | `so_number`, `if_number`, `wayfair_po`, `tracking_number`, `carrier`, `wayfair_shipment_id`, `status`, `error_message`, `logged_at` |

Example: find recent failures
```sql
SELECT logged_at, wayfair_po, status, error_message
FROM `maestrobath.wayfair_inventory.wayfair_so_log`
WHERE status = 'FAILED'
  AND logged_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
ORDER BY logged_at DESC
```

### 2. Cloud Logging (stdout from Cloud Run)

Every `print()` in the code goes here. Last hour for func1:

```bash
gcloud logging read 'resource.labels.service_name="wayfair-func1-po-to-so"' \
  --project=maestrobath --limit=100 --freshness=1h --format='value(textPayload)'
```

Or in Cloud Console: filter `resource.labels.service_name="wayfair-func1-po-to-so"`.

### 3. HTTP response (per-run summary)

Each `POST /` returns a JSON summary you can pipe through `jq`:

```json
{
  "status": "ok",
  "duration_s": 45.95,
  "summary": {"SUCCESS": 2, "SKIPPED_ALREADY_DONE": 2, "FAILED": 0, ...},
  "processed": [
    {"po": "CS655146656", "status": "SUCCESS"},
    {"po": "CS654954003", "status": "SUCCESS"}
  ]
}
```

## Rollback

Three layers of rollback, easiest to hardest.

### 1. Cloud Run revision (no git)

Every deploy keeps the previous revision. Switch traffic in seconds:

```bash
bash scripts/rollback.sh func1
bash scripts/rollback.sh func2
```

The script lists recent revisions and asks which one to route 100% traffic to.

### 2. Re-deploy a previous Docker image (no git)

```bash
gcloud run deploy wayfair-func1-po-to-so \
  --image=us-central1-docker.pkg.dev/maestrobath/wayfair-netsuite/wayfair-func1:OLDER_SHA \
  --region=us-central1 ...
```

Tags by commit SHA are preserved in Artifact Registry indefinitely.

### 3. Git revert

```bash
git revert <bad-commit-sha>
git push
```

GitHub Actions will re-deploy automatically.

## Wayfair production migration

Before flipping `ENVIRONMENT=production`:

1. Set production source address via Cloud Run env vars (or extend `shared/config.py` for per-location lookup):
   ```bash
   gcloud run services update wayfair-func1-po-to-so \
     --update-env-vars="SOURCE_ADDR_STREET1=21 Rancho Cir,SOURCE_ADDR_CITY=Lake Forest,SOURCE_ADDR_STATE=CA,SOURCE_ADDR_POSTAL=92630"
   ```
2. Create a Production Application on `partners.wayfair.com` and update secrets:
   ```bash
   echo -n 'PROD_CLIENT_ID'     | gcloud secrets versions add wayfair-client-id     --data-file=-
   echo -n 'PROD_CLIENT_SECRET' | gcloud secrets versions add wayfair-client-secret --data-file=-
   ```
3. Switch GitHub Actions variable `WF_ENVIRONMENT` to `production`.
4. Push any commit → both services redeploy with new config.
5. Rotate the Gmail App Password (`email-app-password`) — the sandbox-era value should not be reused.

## Why the flow split (func1 = full PO+register, func2 = ASN only)

In an earlier iteration, register + labels + email all lived in func2 and waited for an Item Fulfillment to exist in NS. That created a chicken-and-egg problem: the warehouse couldn't print the Wayfair label until they manually created the IF — but they typically created the IF *after* shipping.

Now func1 does register/labels/email immediately after creating the SO. The warehouse always has the label first, ships physically, then records IF in NS. func2 just confirms the shipment with Wayfair (ASN) once IF appears with the tracking number.

## Owner

`vhalstiandata` / `diroxik@gmail.com`. NetSuite realm 8104048, Wayfair supplier id 267342.
