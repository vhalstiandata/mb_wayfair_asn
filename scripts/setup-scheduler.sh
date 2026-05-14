#!/usr/bin/env bash
#
# Sets up Cloud Scheduler jobs that invoke the two Cloud Run services on a cron.
# Run AFTER the services have been deployed at least once (so URLs exist).
#
# Usage:
#   bash scripts/setup-scheduler.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ============================================================================
PROJECT_ID="maestrobath"
REGION="us-central1"
RUNTIME_SA="wayfair-runtime@${PROJECT_ID}.iam.gserviceaccount.com"

FUNC1_SERVICE="wayfair-func1-po-to-so"
FUNC2_SERVICE="wayfair-func2-if-to-asn"

FUNC1_SCHEDULE="*/10 * * * *"   # every 10 minutes
FUNC2_SCHEDULE="*/15 * * * *"   # every 15 minutes
TIMEZONE="America/Los_Angeles"
# ============================================================================

create_or_update_job() {
  local job_name="$1"
  local service="$2"
  local schedule="$3"

  echo "▶ Resolving URL for $service..."
  local url
  url=$(gcloud run services describe "$service" --region="$REGION" --project="$PROJECT_ID" --format='value(status.url)')
  if [[ -z "$url" ]]; then
    echo "  × Service $service not deployed yet — deploy it first, then re-run this script."
    exit 1
  fi
  echo "  URL: $url"

  if gcloud scheduler jobs describe "$job_name" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
    echo "▶ Updating scheduler job $job_name..."
    gcloud scheduler jobs update http "$job_name" \
      --location="$REGION" --project="$PROJECT_ID" \
      --schedule="$schedule" \
      --time-zone="$TIMEZONE" \
      --uri="$url/" \
      --http-method=POST \
      --oidc-service-account-email="$RUNTIME_SA" \
      --oidc-token-audience="$url" \
      --attempt-deadline=1800s
  else
    echo "▶ Creating scheduler job $job_name..."
    gcloud scheduler jobs create http "$job_name" \
      --location="$REGION" --project="$PROJECT_ID" \
      --schedule="$schedule" \
      --time-zone="$TIMEZONE" \
      --uri="$url/" \
      --http-method=POST \
      --oidc-service-account-email="$RUNTIME_SA" \
      --oidc-token-audience="$url" \
      --attempt-deadline=1800s
  fi
}

create_or_update_job "$FUNC1_SERVICE" "$FUNC1_SERVICE" "$FUNC1_SCHEDULE"
create_or_update_job "$FUNC2_SERVICE" "$FUNC2_SERVICE" "$FUNC2_SCHEDULE"

cat <<EOF

═══════════════════════════════════════════════════════════════════════════
✅ Schedulers configured.

  ${FUNC1_SERVICE}: ${FUNC1_SCHEDULE}  (${TIMEZONE})
  ${FUNC2_SERVICE}: ${FUNC2_SCHEDULE}  (${TIMEZONE})

To trigger manually right now:
  gcloud scheduler jobs run ${FUNC1_SERVICE} --location=${REGION}
  gcloud scheduler jobs run ${FUNC2_SERVICE} --location=${REGION}

To pause/resume:
  gcloud scheduler jobs pause  ${FUNC1_SERVICE} --location=${REGION}
  gcloud scheduler jobs resume ${FUNC1_SERVICE} --location=${REGION}
═══════════════════════════════════════════════════════════════════════════
EOF
