# Wayfair ↔ NetSuite Integration

Two Cloud Run services that automate dropship order fulfillment between Wayfair and NetSuite:

| Service | Schedule | What it does |
|---|---|---|
| **func1** `wayfair-func1-po-to-so` | every 10 min | Pulls new dropship POs from Wayfair → accepts line items → creates Sales Orders in NetSuite (with serial numbers + line-level discount) |
| **func2** `wayfair-func2-if-to-asn` | every 15 min | For each SO created by func1, checks if an Item Fulfillment exists in NetSuite → forwards tracking number to Wayfair as an ASN |

Both functions log every action to BigQuery (`wayfair_so_log`, `wayfair_asn_log`) and use those logs for deduplication.

## Architecture

```
                ┌─────────────────┐
                │ Cloud Scheduler │  (cron)
                └────────┬────────┘
                         │ OIDC-authenticated POST
       ┌─────────────────┴─────────────────┐
       ▼                                   ▼
┌──────────────┐                    ┌──────────────┐
│  Cloud Run   │                    │  Cloud Run   │
│   func1      │                    │   func2      │
│ PO → SO      │                    │ IF → ASN     │
└──────┬───────┘                    └──────┬───────┘
       │                                   │
       │  ┌─────────────┐  ┌─────────────┐ │
       └─→│  NetSuite   │  │   Wayfair   │←┘
          │ REST/Restlet│  │   GraphQL   │
          └─────────────┘  └─────────────┘
       │                                   │
       └──────────┬──────────────┬─────────┘
                  ▼              ▼
            ┌─────────────────────────┐
            │       BigQuery          │
            │  wayfair_so_log         │
            │  wayfair_asn_log        │
            │  wayfair_sku_mapper     │
            └─────────────────────────┘
```

## Repo layout

```
.
├── shared/                    # OAuth, NetSuite, Wayfair, BQ — used by both funcs
│   ├── config.py              # env-based config (Secret Manager in prod)
│   ├── netsuite.py            # OAuth, SuiteQL, inventory, SO creation
│   ├── wayfair.py             # token, get_po, accept, send_asn
│   ├── bigquery_log.py        # ensure_tables, dedup, writes
│   └── http_helpers.py
├── func1_po_to_so/
│   ├── main.py                # Flask app, runs the pipeline
│   ├── requirements.txt
│   └── Dockerfile
├── func2_if_to_asn/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
├── .github/workflows/
│   ├── deploy-func1.yml       # GitHub Actions → Cloud Run
│   └── deploy-func2.yml
├── scripts/
│   ├── setup.sh               # one-time GCP infra provisioning
│   └── setup-scheduler.sh     # Cloud Scheduler jobs (after first deploy)
└── .env.example               # local development template
```

## Deploy from scratch

### 1. One-time GCP setup

Edit the top of `scripts/setup.sh` (`PROJECT_ID`, `GITHUB_OWNER`, `GITHUB_REPO`), then:

```bash
bash scripts/setup.sh
```

This enables APIs, creates the Artifact Registry repo, two service accounts (runtime + deployer), grants IAM, configures Workload Identity Federation, and creates empty Secret Manager entries.

### 2. Populate secrets

```bash
echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-realm           --data-file=- --project=maestrobath
echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-consumer-key    --data-file=- --project=maestrobath
echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-consumer-secret --data-file=- --project=maestrobath
echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-token           --data-file=- --project=maestrobath
echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-token-secret    --data-file=- --project=maestrobath
echo -n 'YOUR_VALUE' | gcloud secrets versions add wayfair-client-id        --data-file=- --project=maestrobath
echo -n 'YOUR_VALUE' | gcloud secrets versions add wayfair-client-secret    --data-file=- --project=maestrobath
```

### 3. Configure GitHub

In `Settings → Secrets and variables → Actions`:

**Secrets:**
- `WIF_PROVIDER` — full WIF provider resource (printed by `setup.sh`)
- `WIF_SERVICE_ACCOUNT` — `wayfair-deployer@maestrobath.iam.gserviceaccount.com`

**Variables:**
- `GCP_PROJECT_ID` = `maestrobath`
- `GCP_REGION` = `us-central1`
- `RUNTIME_SERVICE_ACCOUNT` = `wayfair-runtime@maestrobath.iam.gserviceaccount.com`
- `BQ_PROJECT_ID` = `maestrobath`
- `BQ_DATASET` = `wayfair_inventory`
- `WF_ENVIRONMENT` = `sandbox` (switch to `production` later)

### 4. First deploy

```bash
git push origin main
```

GitHub Actions runs `deploy-func1.yml` + `deploy-func2.yml`. Watch the run, get the service URLs, then:

### 5. Schedule

```bash
bash scripts/setup-scheduler.sh
```

Done. Cron-driven from this point on.

## Local development

```bash
cp .env.example .env       # fill in values
gcloud auth application-default login   # for BigQuery

python -m venv .venv && source .venv/bin/activate
pip install -r func1_po_to_so/requirements.txt

# Run func1 locally
PYTHONPATH=. python func1_po_to_so/main.py
# In another terminal:
curl -X POST http://localhost:8080/
```

## Switching sandbox → production

1. Update `WF_ENVIRONMENT` GitHub variable to `production`
2. Update the secrets `wayfair-client-id` / `wayfair-client-secret` to the production app's creds
3. Update `SOURCE_ADDR_*` env vars in the workflows (real warehouse address)
4. Push to main → both services redeploy
5. Verify with one manual trigger via `gcloud scheduler jobs run …` before letting cron take over

## Operational notes

- **Idempotency:** BQ logs are the source of truth for dedup. If you need to re-process a PO, mark its row in `wayfair_so_log` as anything other than `SUCCESS` (or delete it).
- **Wayfair sandbox quirks:** sandbox often ignores `fromDate` and returns un-acceptable test POs. `CLIENT_SIDE_DATE_FILTER=true` patches this. `ONLY_NEW_POS=false` is safer in this case (rely on BQ dedup, not Wayfair's `hasResponse`).
- **Cloud Run timeout:** services configured for 60-minute timeout. Schedules attempt-deadline is 30 minutes. Lookback of 3 days × max ~50 POs comfortably fits.
- **Failure handling:** any per-PO/IF failure is logged with `status='FAILED'` and the run continues. The HTTP response is still `200` so Cloud Scheduler doesn't retry blindly.
- **Logs:** Cloud Run logs go to Cloud Logging. Filter `resource.labels.service_name="wayfair-func1-po-to-so"`.
