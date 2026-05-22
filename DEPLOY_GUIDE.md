# v3 Upgrade Guide — Register/Labels/Email moved to func1

## What changed vs v2

**Previously (v2):** func2 did everything — wait for IF → register → labels → ASN → email.
**Problem:** If warehouse hasn't created IF in NS yet, the label never gets emailed, so warehouse doesn't know what to do.

**Now (v3):** Split responsibilities:

- **func1** (every 10 min): PO → Accept → Create SO → **Register → Labels → Email warehouse**
- **func2** (every 15 min): wait for IF in NS → **ASN only**

## Flow

```
Wayfair PO arrives
   │
   ▼
[func1] Accept → Create SO → Register → Download labels → Email warehouse with PDFs
   │                                                       │
   │                                                       ▼
   │                                       Warehouse prints label, ships, creates IF in NS
   │                                                       │
   ▼                                                       ▼
[func2 sees SO in BQ, polls for IF] ─────────────► ASN sent to Wayfair when IF appears
```

## Configurable pickup window

`PICKUP_OFFSET_DAYS` env var on func1 (default 3, clamped to 2..5).
Change without redeploy:

```bash
gcloud run services update wayfair-func1-po-to-so \
  --region=us-central1 \
  --update-env-vars="PICKUP_OFFSET_DAYS=4"
```

Or set GitHub variable `PICKUP_OFFSET_DAYS` to bake in the default.

## Pre-deploy checklist

### 1. Tag current state (for rollback)

```bash
cd ~/mb_wayfair_asn
git pull
git tag v2-before-flow-split
git push origin v2-before-flow-split
```

### 2. Create email secret (one-time)

```bash
echo -n 'ztdn ocvm fnxi tymd' | gcloud secrets create email-app-password \
  --data-file=- --replication-policy=automatic --project=maestrobath

gcloud secrets add-iam-policy-binding email-app-password \
  --member="serviceAccount:wayfair-runtime@maestrobath.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor" \
  --project=maestrobath
```

### 3. Add GitHub Variables

In repo Settings → Secrets and variables → Actions → Variables:

| Variable | Value |
|---|---|
| `WAYFAIR_WAREHOUSE_ID` | `267342` |
| `PICKUP_OFFSET_DAYS` | `3` |

### 4. Copy v3 files into your repo

```
shared/config.py             (NEW — adds PICKUP_OFFSET_DAYS + EMAIL_*)
shared/wayfair.py            (NEW — register/labels with content-type check)
shared/email_notify.py       (NEW)
shared/http_helpers.py       (NEW — urllib_get_binary)
shared/bigquery_log.py       (NEW — adds REG_LOG)
func1_po_to_so/main.py       (REPLACE — adds register+labels+email)
func2_if_to_asn/main.py      (REPLACE — pure IF→ASN)
.github/workflows/deploy-func1.yml  (REPLACE)
.github/workflows/deploy-func2.yml  (REPLACE)
scripts/rollback.sh          (NEW)
```

### 5. Patch shared/netsuite.py

Add this function to the end of `shared/netsuite.py` (it's used for the email body):

```python
def get_so_items(so_internal_id: str):
    """Return list of {oracle_sku, ordered_qty} for an SO (for email body)."""
    rows = ns_suiteql(f"""
        SELECT
            BUILTIN.DF(tl.item)  AS oracle_sku,
            tl.quantity          AS qty
        FROM transactionline tl
        WHERE tl.transaction = {so_internal_id}
          AND tl.mainline = 'F'
          AND tl.itemtype IN ('InvtPart', 'NonInvtPart', 'Kit', 'Assembly')
          AND tl.quantity > 0
        ORDER BY tl.linesequencenumber ASC
    """) or []
    return [
        {
            "wayfair_sku": "",
            "oracle_sku":  r.get("oracle_sku") or "?",
            "ordered_qty": int(float(r.get("qty") or 0)),
        }
        for r in rows
    ]
```

Note: func1 actually passes `accepted_items` (already in memory) to `send_so_email`, so `get_so_items` is currently only used as a fallback / for future scripts. You can skip the patch if you don't want it.

### 6. Commit and push

```bash
git add -A
git commit -m "feat: split register/labels/email into func1 (post-SO); func2 = ASN only"
git push origin main
```

Both workflows will fire because `shared/` changed. Watch them at
https://github.com/vhalstiandata/mb_wayfair_asn/actions

### 7. Verify

```bash
# func1 smoke test
SVC=$(gcloud run services describe wayfair-func1-po-to-so --region=us-central1 --format='value(status.url)')
curl -X POST "$SVC/" -H "Authorization: Bearer $(gcloud auth print-identity-token)"

# Check reg log
bq query --project_id=maestrobath --use_legacy_sql=false \
  'SELECT * FROM wayfair_inventory.wayfair_reg_log ORDER BY logged_at DESC LIMIT 5'
```

You should see registrations happening for new POs, label PDFs in /tmp/wayfair_labels
inside the container (ephemeral), and emails arriving at sale@maestrobath.com.

## Rollback

```bash
# Quick: previous Cloud Run revision
bash scripts/rollback.sh func1
bash scripts/rollback.sh func2

# Full: revert to tag
git checkout v2-before-flow-split -- func1_po_to_so/ func2_if_to_asn/ shared/ .github/
git commit -m "revert to v2"
git push
```
