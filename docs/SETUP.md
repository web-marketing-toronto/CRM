# Setup: from an empty GCP project to a running CRM

Everything here runs from your own machine with `gcloud` logged in as an owner of project
`wmt-crm`. Each block is copy-paste. Total time about 20 minutes, most of it waiting for Cloud SQL.

## 0. One-time tools

- Install the Google Cloud CLI: https://cloud.google.com/sdk/docs/install
- `gcloud auth login` and `gcloud config set project wmt-crm`
- Clone this repo: `git clone https://github.com/web-marketing-toronto/crm.git && cd crm`

## 1. Create the infrastructure (Cloud SQL, secrets, service account)

```bash
bash deploy/setup_gcp.sh
```

Creates, in Toronto (`northamerica-northeast2`): a Cloud SQL Postgres 16 instance `crm-pg` with daily
backups copied to Montreal and 7 days of point-in-time recovery; the `crm` database and a `crm_migrate`
user; Secret Manager entries for the database URLs, the app's signing key and the cron secret; and
the `crm-run` service account that Cloud Run uses. Re-running it is safe; it skips what already exists.

## 2. Deploy

```bash
bash deploy/deploy.sh
```

Cloud Build builds the container from the `Dockerfile`, Cloud Run starts it (migrations run on boot),
and a Cloud Scheduler job starts calling `/internal/cron/tick` every minute. The script prints the
service URL at the end; `/health` on that URL should return `{"ok": true, "db": true}`.

## 3. Create your agency login

Run the admin command inside the deployed container once:

```bash
gcloud run jobs create crm-admin --region=northamerica-northeast2 \
  --image="$(gcloud run services describe crm --region=northamerica-northeast2 --format='value(spec.template.spec.containers[0].image)')" \
  --service-account=crm-run@wmt-crm.iam.gserviceaccount.com \
  --set-cloudsql-instances="$(gcloud sql instances describe crm-pg --format='value(connectionName)')" \
  --set-secrets="DATABASE_URL=crm-database-url:latest,SECRET_KEY=crm-secret-key:latest" \
  --set-env-vars="ENV=prod" \
  --command=python --args="-m,app.cli,create-admin,--email,jfidler@web-marketing-toronto.com,--name,Jeff,--password,CHANGE-ME-NOW-12"
gcloud run jobs execute crm-admin --region=northamerica-northeast2 --wait
gcloud run jobs delete crm-admin --region=northamerica-northeast2 --quiet
```

Then open the service URL, sign in, and the app walks you through setting up an authenticator app
(required for agency and owner logins). Change that password from **Account** right away.

## 4. Add the first client

In **Clients → Add client**: name, short name (goes in their intake URL) and job types. On the client's
page you get the **intake URL** and can create their logins. The owner you create becomes the default
assignee for new leads.

## 5. Point lead sources at the intake URL

- **WordPress forms** (Elementor Pro, WPForms, Gravity Forms): add a Webhook action on the form with
  the intake URL. Field names are matched automatically; odd ones go in the client's field map.
- **Anything via Zapier / Shopify Flow**: POST JSON to the intake URL (sample on the client page).
- **Test**: the `curl` command on the client page creates a lead you can then delete by marking it Lost → Spam.

CallRail, Meta Lead Ads, Google lead forms and the attribution snippet come with the next phase.

## 6. Optional: Google Maps key for service-area checks

Create a Maps Platform key restricted to the Geocoding API, then:

```bash
printf '%s' 'YOUR_KEY' | gcloud secrets create crm-google-maps-api-key --data-file=- --replication-policy=user-managed --locations=northamerica-northeast2
bash deploy/deploy.sh
```

## 7. Optional: custom domain

Cloud Run → Domain mappings → map `crm.web-marketing-toronto.com` (or similar) to the `crm` service,
add the DNS records it shows, then redeploy with `BASE_URL=https://crm.web-marketing-toronto.com bash deploy/deploy.sh`
so intake URLs and emails use the real domain.

## Day-to-day

- **Release**: `git pull && bash deploy/deploy.sh`
- **Logs**: `gcloud run services logs read crm --region=northamerica-northeast2 --limit=200`
- **Database shell**: `gcloud sql connect crm-pg --user=crm_migrate --database=crm`
- **A client user lost their phone**: client page → *Clear authenticator*. They enrol a new one at next login.
- **Rotate a client's intake key** (if a URL leaked): client page → *Generate a new key*, then update their forms.

## Costs at this scale

Cloud SQL `db-g1-small` with 10 GB is roughly $35–45 CAD/month; Cloud Run with one warm instance roughly
$15–25; Secret Manager and Scheduler are cents. Twilio and email are usage-based and come with phase 2.
