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

**Importing a client's existing list**: client page → *Import leads from CSV*. Upload the spreadsheet export,
check the column mapping and the status-word mapping on the next screen, read the dry run (created / merged /
skipped and why), then import. Imported leads carry an "Imported, review" tag and sit in the client's
"To review" tab; they never trigger reminders or texts, and the tag clears the first time the client touches
them or sets a status.

**Attribution snippet**: `https://<service>/static/wmt.js` on every page of the client's site (GTM Custom HTML
tag on All Pages). It keeps gclid / fbclid / UTMs for 90 days and fills them into every form; WordPress forms
need hidden fields with those names (the client page lists them). Wix and Squarespace forms mostly can't carry
them; those leads show as "Website".

**CallRail**: per client, a Post-call webhook to the CallRail URL shown on the client page. Optional: put the
webhook signing key in Secret Manager as `crm-callrail-signing-key` and redeploy; then every call payload is
signature-checked. Answered calls of 30 s+ become leads already marked Contacted; missed, voicemail and short
calls become leads flagged "Missed call" with an immediate alert; calls tagged spam / vendor / solicitor /
existing customer / wrong number are dropped.

**Google Ads lead forms**: on the form asset, Lead delivery → Webhook: the Google URL and key from the client
page. Google's "send test data" shows up as a TEST lead with no messages sent.

**Meta Lead Ads** (one app for all clients):
1. developers.facebook.com → the WMT app (or create one, type Business) → add the Webhooks product →
   Page → callback `https://<service>/v1/meta/webhook`, verify token = any long random string, subscribe `leadgen`.
2. Secret Manager: `crm-meta-app-secret` (App settings → Basic), `crm-meta-verify-token` (the string from step 1),
   `crm-meta-access-token` (a system-user token from Business Settings → System users, with
   `leads_retrieval`, `pages_manage_ads`, `pages_read_engagement`, `pages_show_list`; add each client Page to the
   system user's assets). Redeploy.
3. Per client: subscribe their Page to the app (`POST /{page-id}/subscribed_apps` with `subscribed_fields=leadgen`,
   or via Leads Access Manager), then enter the Page id on the client's settings page. Test with Meta's
   Lead Ads Testing Tool.
Until the app is approved, Zapier's Facebook Lead Ads trigger into the intake URL still works (set
`channel` to `meta_lead_ad`).

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
