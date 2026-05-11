# GitHub CI/CD Setup Guide

## Overview

This guide sets up automatic deployment to Cloud Run whenever you push to `main` branch.

**Flow:**
```
Push to GitHub (main branch)
    ↓
GitHub Actions triggers
    ↓
Authenticate with GCP
    ↓
Deploy both functions
    ↓
Run health checks
    ↓
Done! ✅
```

---

## Step 1: Create GitHub Repository

```bash
# In your local project folder
cd wayfair_cloud_run

# Initialize git
git init
git add .
git commit -m "Initial commit: Wayfair NetSuite integration"

# Create repo on GitHub.com, then:
git remote add origin https://github.com/YOUR_USERNAME/wayfair-integration.git
git branch -M main
git push -u origin main
```

---

## Step 2: Create Service Account

```bash
# Set project
gcloud config set project YOUR_PROJECT_ID

# Create service account
gcloud iam service-accounts create github-actions \
    --display-name="GitHub Actions Deployment"

# Get service account email
SA_EMAIL="github-actions@YOUR_PROJECT_ID.iam.gserviceaccount.com"

# Grant permissions
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/run.admin"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/storage.admin"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/iam.serviceAccountUser"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/bigquery.dataViewer"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/bigquery.jobUser"

# Create key
gcloud iam service-accounts keys create github-actions-key.json \
    --iam-account=$SA_EMAIL

# ⚠️ IMPORTANT: Keep this file secure!
# You'll upload it to GitHub Secrets
```

---

## Step 3: Configure GitHub Secrets

1. **Go to your GitHub repo**
2. **Settings → Secrets and variables → Actions**
3. **Click "New repository secret"**

### Add these secrets:

| Secret Name | Value | Example |
|-------------|-------|---------|
| `GCP_PROJECT_ID` | Your GCP Project ID | `maestrobath` |
| `GCP_SA_KEY` | Content of `github-actions-key.json` | `{"type": "service_account"...}` |
| `NS_CONSUMER_KEY` | NetSuite OAuth | `f4fc22f7ac706...` |
| `NS_CONSUMER_SECRET` | NetSuite OAuth | `dc45a0a3ed5d5...` |
| `NS_TOKEN` | NetSuite OAuth | `6588e39d0fcfc...` |
| `NS_TOKEN_SECRET` | NetSuite OAuth | `a8b87e5c8948d...` |
| `NS_REALM` | NetSuite Account ID | `8104048` |
| `NS_CUSTOMER_ID` | Wayfair Customer ID | `18` |
| `NS_SUBSIDIARY_ID` | Subsidiary ID | `2` |
| `NS_DEFAULT_LOCATION` | Default Location | `8` |
| `WF_CLIENT_ID` | Wayfair API Client ID | `1VJt4yBibV-y...` |
| `WF_CLIENT_SECRET` | Wayfair API Secret | `D4WMqImVZOfZ...` |
| `WF_SUPPLIER_ID` | Wayfair Supplier ID | `267342` |
| `WF_GQL_URL` | Wayfair GraphQL URL | `https://sandbox.api.wayfair.com/v1/graphql` |
| `BQ_PROJECT_ID` | BigQuery Project | `maestrobath` |
| `BQ_MAP_TABLE` | BigQuery Table | `maestrobath.wayfair_inventory.wayfair_sku_mapper` |

**For Production, change `WF_GQL_URL` to:**
```
https://api.wayfair.com/v1/graphql
```

---

## Step 4: Test GitHub Actions

### Manual Trigger
1. Go to **Actions** tab in GitHub
2. Click **Deploy to Cloud Run**
3. Click **Run workflow** → **Run workflow**
4. Watch it deploy! 🚀

### Automatic Trigger
```bash
# Make a change
echo "# Test" >> README.md
git add README.md
git commit -m "Test CI/CD"
git push

# GitHub Actions will automatically deploy!
```

---

## Step 5: Verify Deployment

### Check GitHub Actions
1. Go to **Actions** tab
2. Click on latest workflow run
3. Check all steps are ✅ green

### Check Cloud Run
```bash
gcloud run services list --region=us-central1
```

### Check Logs
```bash
gcloud run logs read wayfair-accept-so --limit=20
gcloud run logs read wayfair-ship-asn --limit=20
```

---

## Step 6: Create Cloud Scheduler (One-time)

**⚠️ Schedulers are NOT created by GitHub Actions!**

You need to create them once manually:

```bash
# Get service URLs
FUNCTION1_URL=$(gcloud run services describe wayfair-accept-so --region=us-central1 --format='value(status.url)')
FUNCTION2_URL=$(gcloud run services describe wayfair-ship-asn --region=us-central1 --format='value(status.url)')

# Create schedulers
gcloud scheduler jobs create http wayfair-accept-schedule \
  --schedule='*/15 * * * *' \
  --uri="$FUNCTION1_URL" \
  --http-method=POST \
  --location=us-central1

gcloud scheduler jobs create http wayfair-ship-schedule \
  --schedule='*/15 * * * *' \
  --uri="$FUNCTION2_URL" \
  --http-method=POST \
  --location=us-central1
```

---

## Workflow Explained

### `.github/workflows/deploy.yml`

**Triggers:**
- Push to `main` branch
- Manual trigger (workflow_dispatch)

**Steps:**
1. ✅ Checkout code
2. ✅ Authenticate with GCP
3. ✅ Deploy Function 1
4. ✅ Deploy Function 2
5. ✅ Get service URLs
6. ✅ Test both functions
7. ✅ Print deployment summary

**Environment Variables:**
- From GitHub Secrets
- Passed to Cloud Run services
- Encrypted and secure

---

## Development Workflow

### Feature Branch
```bash
# Create feature branch
git checkout -b feature/new-feature

# Make changes
nano function1_accept_so/main.py

# Commit
git add .
git commit -m "Add new feature"

# Push (won't deploy yet)
git push origin feature/new-feature

# Create Pull Request on GitHub
# Review → Merge to main → Auto-deploy! 🚀
```

### Hotfix
```bash
# Make urgent fix
nano function2_ship_asn/main.py

# Commit and push to main
git add .
git commit -m "Fix: urgent bug"
git push origin main

# Automatically deploys in ~3 minutes! ⚡
```

---

## Production Deployment

### Switch to Production

1. **Update secrets in GitHub:**
   - Change `WF_GQL_URL` to `https://api.wayfair.com/v1/graphql`
   - Update Wayfair credentials to Production

2. **Push to main:**
```bash
git commit -m "Deploy to Production"
git push origin main
```

3. **Monitor deployment:**
   - GitHub Actions tab
   - Cloud Run console

---

## Monitoring

### GitHub Actions
- **History:** Actions tab → All workflow runs
- **Logs:** Click on any run → See detailed logs
- **Notifications:** Settings → Notifications

### Cloud Run
```bash
# Service status
gcloud run services list

# Logs
gcloud run logs read SERVICE_NAME --limit=50

# Metrics
# https://console.cloud.google.com/run
```

---

## Troubleshooting

### Deployment fails: "Permission denied"
**Solution:** Check service account has correct roles (Step 2)

### Deployment fails: "Invalid credentials"
**Solution:** Verify all GitHub Secrets are set correctly

### Function fails health check
**Solution:** Check logs:
```bash
gcloud run logs read SERVICE_NAME --limit=100
```

### Workflow doesn't trigger
**Solution:** 
1. Check `.github/workflows/deploy.yml` exists in main branch
2. Verify push is to `main` branch
3. Check GitHub Actions is enabled in repo settings

---

## Cost Optimization

**GitHub Actions:**
- **Free tier:** 2,000 minutes/month
- Our workflow: ~5 minutes per deployment
- **Cost:** FREE for most cases

**Cloud Run:**
- Same as before: $10-15/month

**Total:** Still ~$10-15/month! 🎉

---

## Security Best Practices

✅ **Use GitHub Secrets** - Never commit credentials  
✅ **Use Service Account** - Don't use personal credentials  
✅ **Enable branch protection** - Require PR reviews  
✅ **Rotate keys** - Update service account keys periodically  
✅ **Audit logs** - Monitor GitHub Actions history  

---

## Alternative: Workload Identity Federation

**More secure** (no service account key file):

See: https://github.com/google-github-actions/auth#workload-identity-federation

**Benefits:**
- ✅ No JSON key file to manage
- ✅ Automatic key rotation
- ✅ Better security

**Setup is more complex** - use service account key for now, migrate later.

---

## Summary

✅ **Automatic deployment** on push to main  
✅ **Secure** credentials via GitHub Secrets  
✅ **Fast** - deploys in ~3 minutes  
✅ **Safe** - test in feature branches first  
✅ **Free** - GitHub Actions free tier  

**Now every push to main = automatic Cloud Run deployment!** 🚀

---

## Next Steps

1. ✅ Set up GitHub repo
2. ✅ Configure secrets
3. ✅ Push code
4. ✅ Watch it deploy
5. ✅ Create schedulers (one-time)
6. 🎉 Enjoy CI/CD!
