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

## 6. Integrations (each optional; the app works without them and says what's missing)

Every one is a secret in Secret Manager plus a redeploy. `deploy/deploy.sh` wires each secret that exists
and prints what it found. Create a secret with:

```bash
printf '%s' 'VALUE' | gcloud secrets create NAME --data-file=- --replication-policy=user-managed --locations=northamerica-northeast2
```

To replace a value later: `printf '%s' 'NEW' | gcloud secrets versions add NAME --data-file=-` then redeploy.

| What | Secret name(s) | Where the value comes from |
| --- | --- | --- |
| Service-area geocoding | `crm-google-maps-api-key` | Maps Platform key restricted to the Geocoding API |
| Texting (alerts, reminders, reply codes, texts to leads) | `crm-twilio-account-sid`, `crm-twilio-auth-token`, `crm-twilio-agency-from` | Twilio console; the third is the agency's own number in E.164 (`+14165550100`) |
| Email (alerts, digests, emails to leads) | `crm-postmark-token` **or** `crm-sendgrid-api-key` | Postmark server token, or a SendGrid key with Mail Send |
| CallRail webhook signatures | `crm-callrail-signing-key` | shown beside the webhook in CallRail; optional, the per-client key in the URL is always required |
| Meta lead ads + Conversions API | `crm-meta-app-secret`, `crm-meta-verify-token`, `crm-meta-access-token` | see Meta below |
| Google Ads conversions + spend | `crm-google-ads-yaml` | the Campaign Manager's `google-ads.yaml`, uploaded as a file: `gcloud secrets create crm-google-ads-yaml --data-file=/path/google-ads.yaml ...` |

### Twilio

1. Buy one Canadian number for the agency (alerts and reminders to clients come from it) and one per
   client that will text leads. Canadian long codes need a registered campaign for A2P; Twilio walks you
   through it under Messaging → Regulatory compliance.
2. On **every** number, Messaging → "A message comes in": Webhook, `https://<service>/v1/twilio/sms`, HTTP POST.
   Delivery receipts are requested per message automatically.
3. Put the agency number in `crm-twilio-agency-from`; put each client's number on their settings page
   ("Their texting number").
4. Set up the three secrets, redeploy, then text a reply code from your own mobile (the one on your Account
   page) to the agency number: `?` gets the code list back.

### Email

Pick one provider. Postmark: create a server, use its Server API token; add and verify the sending domain
(`web-marketing-toronto.com`) under Sender Signatures with the DKIM and Return-Path records. SendGrid: an
API key with Mail Send, plus domain authentication. The From address is `EMAIL_FROM` (default
`leads@web-marketing-toronto.com`; override with `EMAIL_FROM=... bash deploy/deploy.sh`). Emails to leads carry the
client's reply-to address so replies go to them.

### Meta (lead ads webhook and Conversions API)

1. developers.facebook.com → the WMT app (type Business; create it if there is none) → add the **Webhooks**
   product → Page → callback `https://<service>/v1/meta/webhook`, verify token = a long random string,
   subscribe to `leadgen`. The verify step only succeeds after step 2 is deployed, so do 2 first.
2. Secrets: `crm-meta-app-secret` (App settings → Basic → App secret), `crm-meta-verify-token` (the string
   you chose), `crm-meta-access-token` (Business Settings → System users → generate a token with
   `leads_retrieval`, `pages_manage_ads`, `pages_read_engagement`, `pages_show_list`, `ads_management`; add
   each client Page and ad account to the system user's assets). Redeploy, then complete step 1.
3. Per client: subscribe their Page to the app (`POST /{page-id}/subscribed_apps?subscribed_fields=leadgen`
   with the Page token, or via Leads Access Manager), then on their settings page enter the Page id, the
   dataset (pixel) id and the ad account id. Test with Meta's Lead Ads Testing Tool; test the Conversions
   API with a test event code from Events Manager → Test events (clear it when live).
4. Events Manager → the dataset → map the custom events `Qualified` and `Converted` to funnel stages so
   campaigns can optimize for Conversion Leads once the integration shows healthy for a few weeks.

Until the app has the permissions approved, Zapier's Facebook Lead Ads trigger into the intake URL still
works (set `channel` to `meta_lead_ad`).

### Google Ads (conversion uploads and spend)

1. The Campaign Manager's `google-ads.yaml` (developer token, OAuth client id/secret, refresh token,
   `login_customer_id` = the MCC) is all the CRM needs; store it as `crm-google-ads-yaml`. The service
   account the token belongs to must have access to each client account through the MCC.
2. Per client, in their Google Ads account: Goals → Conversions → New → Import → CRM, files or other data
   sources → Track conversions from clicks. Create **CRM Qualified Lead** and **CRM Closed Won**, both
   **secondary** actions, click-through window 90 days, "Enhanced conversions for leads" on. Copy each
   action's id (the number in the URL, or via the API) onto the client's settings page with their 10-digit
   customer id and, optionally, a value to send with Qualified lead.
3. GTM, once per site: the Google tag with "Include user-provided data" on (the user-provided data variable
   reading the form's email and phone fields), and the Meta pixel. That is what lets uploads match leads that
   arrived without a click id (Wix, Squarespace, phone calls).

Policy: both actions stay secondary until a client has about three months of stage data; when campaigns
move to value-based bidding, record the date on the client's settings page so reports can show before/after.

### Spend

Nightly after 04:00 Toronto the tick pulls yesterday's and the day before's campaign spend for every client
with a Google customer id or Meta ad account id on their settings page. Nothing to schedule; it rides on
`crm-tick`.

## 6b. Checking a deploy

```bash
curl -s https://<service>/health
gcloud run services logs read crm --region=northamerica-northeast2 --limit=100 | grep -i "tick\|intake\|sms\|email"
```

The tick logs one line a minute with counts of reminders sent, digests, conversion uploads and the spend
job. `SMS to ...` / `EMAIL to ...` lines appear when a provider is not configured (the `log` provider), which
is how you can see what would have gone out before Twilio or Postmark is wired.

## 7. Optional: custom domain

Cloud Run → Domain mappings → map `crm.web-marketing-toronto.com` (or similar) to the `crm` service,
add the DNS records it shows, then redeploy with `BASE_URL=https://crm.web-marketing-toronto.com bash deploy/deploy.sh`
so intake URLs and emails use the real domain.

## Day-to-day

- **Release**: `git pull && bash deploy/deploy.sh`
- **Logs**: `gcloud run services logs read crm --region=northamerica-northeast2 --limit=200`
- **Database shell**: `gcloud sql connect crm-pg --user=crm_migrate --database=crm`
- **Forgot password**: the login screen has *Forgot your password?*; it emails a one-hour link. Until an email
  provider is wired, the link is printed in the Cloud Run logs instead:
  `gcloud run services logs read crm --region=northamerica-northeast2 --limit=50 | grep -o 'https://[^ ]*/login/reset/[^ ]*'`
- **Reset any login from the command line** (including your own, and a lockout): run the `crm-admin` job from
  step 3 with `--args="-m,app.cli,set-password,--email,who@example.com,--password,NEW-PASSWORD"`; add
  `,--clear-mfa` to also remove their authenticator.
- **A client user lost their phone**: client page → *Clear authenticator*. They enrol a new one at next login.
- **Rotate a client's intake key** (if a URL leaked): client page → *Generate a new key*, then update their forms
  and the CallRail / Google lead-form webhooks.
- **A conversion didn't reach Google or Meta**: the lead page (agency view) shows every upload, its status and
  the platform's reason; *Retry now* re-queues it. Settings → *Recent uploads* lists the last ten per client.
- **Someone texts STOP**: recorded on the lead; the CRM never texts that number for that client again
  unless they text START.
- **Change a client's reminder timings or message wording**: all on their settings page; takes effect for the
  next lead. The follow-up schedule applies to leads already in the cadence at their next touch.

## Costs at this scale

Cloud SQL `db-g1-small` with 10 GB is roughly $35–45 CAD/month; Cloud Run with one warm instance roughly
$15–25; Secret Manager and Scheduler are cents. Twilio: about $1.50/month per number plus roughly a cent per
text; a client doing 60 leads a month with the full cadence is a few dollars. Postmark's smallest plan
covers 10,000 emails a month.
