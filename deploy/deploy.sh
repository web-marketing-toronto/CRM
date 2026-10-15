#!/usr/bin/env bash
# Build from source with Cloud Build and deploy to Cloud Run. Re-run on every release.
#
#   bash deploy/deploy.sh
#
set -euo pipefail
PROJECT="${PROJECT:-wmt-crm}"
REGION="${REGION:-northamerica-northeast2}"
INSTANCE="${INSTANCE:-crm-pg}"
SERVICE="crm"
BASE_URL="${BASE_URL:-}"   # set to https://crm.yourdomain.com once a domain is mapped; otherwise the run.app URL is used

gcloud config set project "$PROJECT" >/dev/null
CONNECTION_NAME="$(gcloud sql instances describe "$INSTANCE" --format='value(connectionName)')"
SA="crm-run@${PROJECT}.iam.gserviceaccount.com"

# First deploy has no URL yet; deploy once, read the URL, then redeploy with BASE_URL set.
EXISTING_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)' 2>/dev/null || true)"
URL="${BASE_URL:-${EXISTING_URL:-https://placeholder.invalid}}"

MAPS_FLAG=""
if gcloud secrets describe crm-google-maps-api-key >/dev/null 2>&1; then
  MAPS_FLAG=",GOOGLE_MAPS_API_KEY=crm-google-maps-api-key:latest"
  gcloud secrets add-iam-policy-binding crm-google-maps-api-key --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
fi

gcloud run deploy "$SERVICE" --source . --region="$REGION" --platform=managed \
  --service-account="$SA" \
  --add-cloudsql-instances="$CONNECTION_NAME" \
  --set-env-vars="ENV=prod,BASE_URL=${URL},COOKIE_SECURE=true,DB_POOL_MAX=6" \
  --set-secrets="DATABASE_URL=crm-database-url:latest,MIGRATE_DATABASE_URL=crm-migrate-database-url:latest,CRM_APP_PASSWORD=crm-app-password:latest,SECRET_KEY=crm-secret-key:latest,CRON_SECRET=crm-cron-secret:latest${MAPS_FLAG}" \
  --allow-unauthenticated --min-instances=1 --max-instances=4 --cpu=1 --memory=512Mi --concurrency=40 --port=8080 \
  --execution-environment=gen2 --timeout=60

NEW_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
if [[ -z "$BASE_URL" && "$NEW_URL" != "$EXISTING_URL" ]]; then
  echo "First deploy: setting BASE_URL=$NEW_URL and redeploying once"
  gcloud run services update "$SERVICE" --region="$REGION" --update-env-vars="BASE_URL=${NEW_URL}" >/dev/null
fi

# Minute tick for reminders (phase 2). Idempotent.
CRON_SECRET="$(gcloud secrets versions access latest --secret=crm-cron-secret)"
if gcloud scheduler jobs describe crm-tick --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http crm-tick --location="$REGION" --uri="${NEW_URL}/internal/cron/tick" --update-headers="X-Cron-Secret=${CRON_SECRET}" >/dev/null
else
  gcloud scheduler jobs create http crm-tick --location="$REGION" --schedule="* * * * *" --time-zone="America/Toronto" \
    --uri="${NEW_URL}/internal/cron/tick" --http-method=POST --headers="X-Cron-Secret=${CRON_SECRET}" --attempt-deadline=50s >/dev/null
fi

echo
echo "Deployed: $NEW_URL"
echo "Health:   $NEW_URL/healthz"
