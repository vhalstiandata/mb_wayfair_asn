#!/usr/bin/env bash
#
# One-time setup script for Wayfair ↔ NetSuite Cloud Run deployment.
#
# What it does:
#   1. Enables required GCP APIs
#   2. Creates Artifact Registry repo for Docker images
#   3. Creates 2 service accounts:
#        - runtime SA (used by Cloud Run services + Cloud Scheduler)
#        - deployer SA (used by GitHub Actions via WIF, NO KEY)
#   4. Grants IAM roles
#   5. Configures Workload Identity Federation for GitHub Actions
#   6. Creates Secret Manager entries (empty — you fill them in afterwards)
#   7. Prints WIF_PROVIDER + WIF_SERVICE_ACCOUNT values to put in GitHub secrets
#
# Usage:
#   Edit the variables at the top, then:
#     bash scripts/setup.sh
#
# Re-running is idempotent — safe to run multiple times.
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ============================================================================
# EDIT THESE
# ============================================================================
PROJECT_ID="maestrobath"
REGION="us-central1"
GITHUB_OWNER="vhalstiandata"
GITHUB_REPO="mb_wayfair_asn"               # repo name (no owner prefix)

# Names (you usually don't need to change)
AR_REPO="wayfair-netsuite"                   # Artifact Registry repo
RUNTIME_SA_NAME="wayfair-runtime"            # Cloud Run + Scheduler identity
DEPLOYER_SA_NAME="wayfair-deployer"          # GitHub Actions identity
WIF_POOL="github-pool"
WIF_PROVIDER="github-provider"
# ============================================================================

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
DEPLOYER_SA="${DEPLOYER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "▶ Project: $PROJECT_ID ($PROJECT_NUMBER)"
echo "▶ Region:  $REGION"
echo "▶ GitHub:  $GITHUB_OWNER/$GITHUB_REPO"
echo

# ─────────────────────────────────────────────────────────────────────────────
# 1. Enable APIs
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Enabling APIs..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com \
  cloudscheduler.googleapis.com \
  bigquery.googleapis.com \
  --project="$PROJECT_ID"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Artifact Registry
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Creating Artifact Registry repo..."
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --project="$PROJECT_ID"

# ─────────────────────────────────────────────────────────────────────────────
# 3. Service Accounts
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Creating service accounts..."
gcloud iam service-accounts describe "$RUNTIME_SA" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$RUNTIME_SA_NAME" \
    --display-name="Wayfair runtime (Cloud Run + Scheduler)" \
    --project="$PROJECT_ID"

gcloud iam service-accounts describe "$DEPLOYER_SA" --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$DEPLOYER_SA_NAME" \
    --display-name="Wayfair deployer (GitHub Actions WIF)" \
    --project="$PROJECT_ID"

# ─────────────────────────────────────────────────────────────────────────────
# 4. IAM Roles
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Granting IAM roles to RUNTIME SA..."
for role in \
    roles/bigquery.dataEditor \
    roles/bigquery.jobUser \
    roles/secretmanager.secretAccessor \
    roles/run.invoker ; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$RUNTIME_SA" \
    --role="$role" \
    --condition=None --quiet >/dev/null
done

echo "▶ Granting IAM roles to DEPLOYER SA..."
for role in \
    roles/run.admin \
    roles/artifactregistry.writer \
    roles/iam.serviceAccountUser ; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$DEPLOYER_SA" \
    --role="$role" \
    --condition=None --quiet >/dev/null
done

# Deployer needs to act-as runtime SA when deploying Cloud Run
gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA" \
  --member="serviceAccount:$DEPLOYER_SA" \
  --role="roles/iam.serviceAccountUser" \
  --project="$PROJECT_ID" --quiet >/dev/null

# ─────────────────────────────────────────────────────────────────────────────
# 5. Workload Identity Federation for GitHub Actions
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Configuring Workload Identity Federation..."

gcloud iam workload-identity-pools describe "$WIF_POOL" \
  --location=global --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools create "$WIF_POOL" \
    --location=global \
    --display-name="GitHub Actions pool" \
    --project="$PROJECT_ID"

gcloud iam workload-identity-pools providers describe "$WIF_PROVIDER" \
  --workload-identity-pool="$WIF_POOL" --location=global --project="$PROJECT_ID" >/dev/null 2>&1 || \
  gcloud iam workload-identity-pools providers create-oidc "$WIF_PROVIDER" \
    --workload-identity-pool="$WIF_POOL" \
    --location=global \
    --display-name="GitHub provider" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner=='${GITHUB_OWNER}'" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --project="$PROJECT_ID"

# Bind deployer SA so GitHub Actions in our repo can impersonate it
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/attribute.repository/${GITHUB_OWNER}/${GITHUB_REPO}" \
  --role="roles/iam.workloadIdentityUser" \
  --project="$PROJECT_ID" --quiet >/dev/null

# ─────────────────────────────────────────────────────────────────────────────
# 6. Secret Manager skeleton (empty secrets — you fill them after)
# ─────────────────────────────────────────────────────────────────────────────
echo "▶ Creating Secret Manager entries (empty)..."
for secret in \
    netsuite-realm \
    netsuite-consumer-key \
    netsuite-consumer-secret \
    netsuite-token \
    netsuite-token-secret \
    wayfair-client-id \
    wayfair-client-secret ; do
  gcloud secrets describe "$secret" --project="$PROJECT_ID" >/dev/null 2>&1 || \
    gcloud secrets create "$secret" --replication-policy=automatic --project="$PROJECT_ID"
done

# ─────────────────────────────────────────────────────────────────────────────
# DONE
# ─────────────────────────────────────────────────────────────────────────────
WIF_PROVIDER_FULL="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${WIF_POOL}/providers/${WIF_PROVIDER}"

cat <<EOF

═══════════════════════════════════════════════════════════════════════════
✅ Setup complete.

NEXT STEPS:

1. Populate secrets (run each command, pasting the value when prompted):

   echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-realm           --data-file=- --project=$PROJECT_ID
   echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-consumer-key    --data-file=- --project=$PROJECT_ID
   echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-consumer-secret --data-file=- --project=$PROJECT_ID
   echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-token           --data-file=- --project=$PROJECT_ID
   echo -n 'YOUR_VALUE' | gcloud secrets versions add netsuite-token-secret    --data-file=- --project=$PROJECT_ID
   echo -n 'YOUR_VALUE' | gcloud secrets versions add wayfair-client-id        --data-file=- --project=$PROJECT_ID
   echo -n 'YOUR_VALUE' | gcloud secrets versions add wayfair-client-secret    --data-file=- --project=$PROJECT_ID

2. In GitHub → Settings → Secrets and variables → Actions, add:

   Secrets:
     WIF_PROVIDER         = $WIF_PROVIDER_FULL
     WIF_SERVICE_ACCOUNT  = $DEPLOYER_SA

   Variables:
     GCP_PROJECT_ID            = $PROJECT_ID
     GCP_REGION                = $REGION
     RUNTIME_SERVICE_ACCOUNT   = $RUNTIME_SA
     BQ_PROJECT_ID             = $PROJECT_ID
     BQ_DATASET                = wayfair_inventory
     WF_ENVIRONMENT            = sandbox        # change to 'production' when ready

3. Push to main → GitHub Actions will deploy both services.

4. After first deploy, set up Cloud Scheduler:
     bash scripts/setup-scheduler.sh

═══════════════════════════════════════════════════════════════════════════
EOF
