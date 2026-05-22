#!/usr/bin/env bash
# Rollback a Cloud Run service to its previous revision.
# Usage: bash scripts/rollback.sh [func1|func2]
set -euo pipefail

PROJECT_ID="maestrobath"
REGION="us-central1"

case "${1:-func2}" in
  func1) SERVICE="wayfair-func1-po-to-so" ;;
  func2) SERVICE="wayfair-func2-if-to-asn" ;;
  *)     echo "Usage: $0 [func1|func2]"; exit 1 ;;
esac

echo "Current revisions for $SERVICE:"
gcloud run revisions list --service="$SERVICE" --region="$REGION" --project="$PROJECT_ID" --limit=5

echo ""
read -p "Enter revision name to roll back to (or 'latest-1' for previous): " REV

if [[ "$REV" == "latest-1" ]]; then
  REV=$(gcloud run revisions list --service="$SERVICE" --region="$REGION" --project="$PROJECT_ID" \
    --format='value(REVISION)' --limit=2 | tail -1)
  echo "Rolling back to: $REV"
fi

gcloud run services update-traffic "$SERVICE" \
  --to-revisions="$REV=100" \
  --region="$REGION" \
  --project="$PROJECT_ID"

echo "Done. All traffic now routed to $REV"
echo "Verify: gcloud run services describe $SERVICE --region=$REGION --project=$PROJECT_ID --format='value(status.traffic)'"
