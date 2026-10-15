#!/usr/bin/env bash
# One-time setup of the WMT CRM in Google Cloud. Safe to re-run: every step checks before creating.
#
#   bash deploy/setup_gcp.sh
#
# Needs: gcloud (logged in as an owner of the project), openssl. Takes ~10 minutes, most of it Cloud SQL.
set -euo pipefail

PROJECT="${PROJECT:-wmt-crm}"
REGION="${REGION:-northamerica-northeast2}"      # Toronto
BACKUP_REGION="${BACKUP_REGION:-northamerica-northeast1}"  # Montreal, for backup copies
INSTANCE="${INSTANCE:-crm-pg}"
DB="crm"
SERVICE="crm"
TIER="${TIER:-db-g1-small}"                       # ~$35/mo; move to db-custom-1-3840 if it ever struggles

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT" >/dev/null

say "Enabling APIs"
gcloud services enable run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com iam.googleapis.com

say "Cloud SQL instance $INSTANCE in $REGION"
if ! gcloud sql instances describe "$INSTANCE" >/dev/null 2>&1; then
  gcloud sql instances create "$INSTANCE" \
    --database-version=POSTGRES_16 --tier="$TIER" --region="$REGION" \
    --storage-type=SSD --storage-size=10GB --storage-auto-increase \
    --backup-start-time=07:00 --backup-location="$BACKUP_REGION" --retained-backups-count=30 \
    --enable-point-in-time-recovery --retained-transaction-log-days=7 \
    --availability-type=zonal --deletion-protection
else
  echo "exists"
fi
CONNECTION_NAME="$(gcloud sql instances describe "$INSTANCE" --format='value(connectionName)')"

say "Database and the migration user"
gcloud sql databases describe "$DB" --instance="$INSTANCE" >/dev/null 2>&1 || gcloud sql databases create "$DB" --instance="$INSTANCE"

secret_exists() { gcloud secrets describe "$1" >/dev/null 2>&1; }
ensure_secret() {  # name, value-generator
  if secret_exists "$1"; then echo "secret $1 exists"; else printf '%s' "$2" | gcloud secrets create "$1" --data-file=- --replication-policy=user-managed --locations="$REGION" >/dev/null; echo "secret $1 created"; fi
}

MIGRATE_PW="$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)"
APP_PW="$(openssl rand -base64 30 | tr -d '/+=' | cut -c1-32)"
if ! secret_exists crm-migrate-database-url; then
  gcloud sql users create crm_migrate --instance="$INSTANCE" --password="$MIGRATE_PW" 2>/dev/null || gcloud sql users set-password crm_migrate --instance="$INSTANCE" --password="$MIGRATE_PW"
  ensure_secret crm-migrate-database-url "postgresql://crm_migrate:${MIGRATE_PW}@/${DB}?host=/cloudsql/${CONNECTION_NAME}"
fi
if ! secret_exists crm-app-password; then
  ensure_secret crm-app-password "$APP_PW"
  ensure_secret crm-database-url "postgresql://crm_app:${APP_PW}@/${DB}?host=/cloudsql/${CONNECTION_NAME}"
fi
ensure_secret crm-secret-key "$(openssl rand -hex 32)"
ensure_secret crm-cron-secret "$(openssl rand -hex 24)"
secret_exists crm-google-maps-api-key || echo "note: create secret crm-google-maps-api-key later with your Maps key (service-area checks are skipped until then)"

say "Service account for Cloud Run"
SA="crm-run@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 || gcloud iam service-accounts create crm-run --display-name="CRM Cloud Run"
gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role=roles/cloudsql.client --condition=None >/dev/null
for s in crm-database-url crm-migrate-database-url crm-app-password crm-secret-key crm-cron-secret; do
  gcloud secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

say "Done. Next: bash deploy/deploy.sh"
echo "Cloud SQL connection name: $CONNECTION_NAME"
