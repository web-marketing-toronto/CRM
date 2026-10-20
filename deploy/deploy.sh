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
# A custom domain set once (BASE_URL=https://… bash deploy/deploy.sh) is remembered from the running service,
# so later deploys don't fall back to the run.app address.
if [[ -z "$BASE_URL" ]]; then
  BASE_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format=json 2>/dev/null | python3 -c '
import json, sys
try:
    envs = json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env", [])
    print(next((e.get("value", "") for e in envs if e.get("name") == "BASE_URL" and "run.app" not in e.get("value", "")), ""))
except Exception:
    print("")' 2>/dev/null || true)"
fi
URL="${BASE_URL:-${EXISTING_URL:-https://placeholder.invalid}}"

# Optional secrets: each one that exists in Secret Manager is wired to its environment variable.
# Create them with:  printf '%s' 'VALUE' | gcloud secrets create NAME --data-file=- --replication-policy=user-managed --locations=$REGION
#   crm-google-maps-api-key     GOOGLE_MAPS_API_KEY         geocoding for the service-area check
#   crm-twilio-account-sid      TWILIO_ACCOUNT_SID          texting (with the next two)
#   crm-twilio-auth-token       TWILIO_AUTH_TOKEN
#   crm-twilio-agency-from      TWILIO_AGENCY_FROM          the agency's number, E.164, e.g. +14165550100
#   crm-smtp-password           SMTP_PASSWORD               email via a Google Workspace mailbox (sets EMAIL_PROVIDER=smtp;
#                                                           SMTP_USER defaults to EMAIL_FROM; override with SMTP_USER=... on the command line)
#   crm-postmark-token          POSTMARK_TOKEN              email via Postmark (sets EMAIL_PROVIDER=postmark)
#   crm-sendgrid-api-key        SENDGRID_API_KEY            or email via SendGrid (sets EMAIL_PROVIDER=sendgrid)
#   crm-callrail-signing-key    CALLRAIL_SIGNING_KEY        verify CallRail webhooks (optional)
#   crm-meta-app-secret         META_APP_SECRET             Meta lead ads webhook + CAPI
#   crm-meta-verify-token       META_VERIFY_TOKEN
#   crm-meta-access-token       META_ACCESS_TOKEN
#   crm-google-ads-yaml         (mounted as a file)         the Campaign Manager's google-ads.yaml, for conversion uploads + spend
#   crm-google-ads-sa-key       (mounted as a file)         its service_account.json, when the yaml uses json_key_file_path
OPTIONAL_SECRETS=(
  "crm-google-maps-api-key:GOOGLE_MAPS_API_KEY"
  "crm-twilio-account-sid:TWILIO_ACCOUNT_SID"
  "crm-twilio-auth-token:TWILIO_AUTH_TOKEN"
  "crm-twilio-agency-from:TWILIO_AGENCY_FROM"
  "crm-smtp-password:SMTP_PASSWORD"
  "crm-postmark-token:POSTMARK_TOKEN"
  "crm-sendgrid-api-key:SENDGRID_API_KEY"
  "crm-callrail-signing-key:CALLRAIL_SIGNING_KEY"
  "crm-meta-app-secret:META_APP_SECRET"
  "crm-meta-verify-token:META_VERIFY_TOKEN"
  "crm-meta-access-token:META_ACCESS_TOKEN"
)
SECRET_FLAGS=""
EXTRA_ENV=""
for pair in "${OPTIONAL_SECRETS[@]}"; do
  name="${pair%%:*}"; var="${pair##*:}"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    SECRET_FLAGS="${SECRET_FLAGS},${var}=${name}:latest"
    gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
    case "$var" in
      TWILIO_AUTH_TOKEN) EXTRA_ENV="${EXTRA_ENV},SMS_PROVIDER=twilio" ;;
      SMTP_PASSWORD) EXTRA_ENV="${EXTRA_ENV},EMAIL_PROVIDER=smtp,SMTP_HOST=${SMTP_HOST:-smtp.gmail.com},SMTP_PORT=${SMTP_PORT:-587}"; [[ -n "${SMTP_USER:-}" ]] && EXTRA_ENV="${EXTRA_ENV},SMTP_USER=${SMTP_USER}"
                     # Leads by email: read the same mailbox over IMAP (EMAIL_INTAKE=off to disable).
                     [[ "${EMAIL_INTAKE:-on}" == "off" ]] || EXTRA_ENV="${EXTRA_ENV},IMAP_HOST=${IMAP_HOST:-imap.gmail.com}" ;;
      POSTMARK_TOKEN) [[ "$EXTRA_ENV" == *EMAIL_PROVIDER* ]] || EXTRA_ENV="${EXTRA_ENV},EMAIL_PROVIDER=postmark" ;;
      SENDGRID_API_KEY) [[ "$EXTRA_ENV" == *EMAIL_PROVIDER* ]] || EXTRA_ENV="${EXTRA_ENV},EMAIL_PROVIDER=sendgrid" ;;
    esac
  fi
done
if gcloud secrets describe crm-google-ads-yaml >/dev/null 2>&1; then
  SECRET_FLAGS="${SECRET_FLAGS},/secrets/google-ads/google-ads.yaml=crm-google-ads-yaml:latest"
  EXTRA_ENV="${EXTRA_ENV},GOOGLE_ADS_YAML=/secrets/google-ads/google-ads.yaml"
  # Google retires each Ads API version about a year after release; override the app's default here when
  # the connection check says the version is retired: GOOGLE_ADS_API_VERSION=v26 bash deploy/deploy.sh
  [[ -n "${GOOGLE_ADS_API_VERSION:-}" ]] && EXTRA_ENV="${EXTRA_ENV},GOOGLE_ADS_API_VERSION=${GOOGLE_ADS_API_VERSION}"
  gcloud secrets add-iam-policy-binding crm-google-ads-yaml --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
  # A yaml that authenticates as a service account (json_key_file_path) needs the key file too.
  if gcloud secrets describe crm-google-ads-sa-key >/dev/null 2>&1; then
    SECRET_FLAGS="${SECRET_FLAGS},/secrets/google-ads-key/service_account.json=crm-google-ads-sa-key:latest"
    EXTRA_ENV="${EXTRA_ENV},GOOGLE_ADS_SA_KEY=/secrets/google-ads-key/service_account.json"
    gcloud secrets add-iam-policy-binding crm-google-ads-sa-key --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
  fi
fi
EMAIL_FROM="${EMAIL_FROM:-leads@web-marketing-toronto.com}"
EMAIL_FROM_NAME="${EMAIL_FROM_NAME:-WMT CRM}"

gcloud run deploy "$SERVICE" --source . --region="$REGION" --platform=managed \
  --service-account="$SA" \
  --add-cloudsql-instances="$CONNECTION_NAME" \
  --set-env-vars="ENV=prod,BASE_URL=${URL},COOKIE_SECURE=true,DB_POOL_MAX=6,EMAIL_FROM=${EMAIL_FROM},EMAIL_FROM_NAME=${EMAIL_FROM_NAME}${EXTRA_ENV}" \
  --set-secrets="DATABASE_URL=crm-database-url:latest,MIGRATE_DATABASE_URL=crm-migrate-database-url:latest,CRM_APP_PASSWORD=crm-app-password:latest,SECRET_KEY=crm-secret-key:latest,CRON_SECRET=crm-cron-secret:latest${SECRET_FLAGS}" \
  --allow-unauthenticated --min-instances=1 --max-instances=4 --cpu=1 --memory=512Mi --concurrency=40 --port=8080 \
  --execution-environment=gen2 --timeout=60

NEW_URL="$(gcloud run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
if [[ -z "$BASE_URL" && "$NEW_URL" != "$EXISTING_URL" ]]; then
  echo "First deploy: setting BASE_URL=$NEW_URL and redeploying once"
  gcloud run services update "$SERVICE" --region="$REGION" --update-env-vars="BASE_URL=${NEW_URL}" >/dev/null
fi

# Minute tick: reminders, digests, conversion uploads, nightly spend. Idempotent.
CRON_SECRET="$(gcloud secrets versions access latest --secret=crm-cron-secret)"
if gcloud scheduler jobs describe crm-tick --location="$REGION" >/dev/null 2>&1; then
  gcloud scheduler jobs update http crm-tick --location="$REGION" --uri="${NEW_URL}/internal/cron/tick" --update-headers="X-Cron-Secret=${CRON_SECRET}" >/dev/null
else
  gcloud scheduler jobs create http crm-tick --location="$REGION" --schedule="* * * * *" --time-zone="America/Toronto" \
    --uri="${NEW_URL}/internal/cron/tick" --http-method=POST --headers="X-Cron-Secret=${CRON_SECRET}" --attempt-deadline=50s >/dev/null
fi

echo
echo "Deployed: $NEW_URL"
echo "Health:   $NEW_URL/health"
echo "Wired:    ${SECRET_FLAGS#,}"
