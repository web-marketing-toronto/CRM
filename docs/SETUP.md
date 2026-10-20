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

That is the only login that ever needs the command line. Everyone else on the agency side is added from
**Team** in the app: name, email, optional mobile. They get a one-time temporary password, choose their own
at first sign-in, then set up an authenticator (mandatory for agency logins). Team members have the same
access as you: every client, every setting, every lead. Someone who should only see one client's leads is
added as that client's login instead (Clients → the client → Logins).

Signing in as agency staff lands on the **Dashboard**: what needs attention across every client (new leads
nobody has called, leads waiting on a reply, overdue follow-ups), volume, the latest leads, whether the
pipes are alive (scheduler, texting, email, ad-platform uploads), any text or email that failed in the last
day, and a client table with each client's missing setup steps.

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

- **Sites that can only email** (RealtyNinja, Wix, Squarespace, Houzz…): every client has a **Leads by email**
  address on their page, `crm+<client>.<token>@web-marketing-toronto.com`. The site sends or BCCs its lead
  notification there; the CRM reads the crm@ mailbox over IMAP every minute, pulls name / email / phone /
  message / address out of the email (labelled lines or a two-column table, with the client's field map for
  odd labels) and makes the lead through the normal path. RealtyNinja: Settings → Leads → *Lead BCC Email*.
  Wix: Automations → form submitted → send email. Squarespace: form Storage → Email. Needs IMAP enabled on
  the crm@ mailbox (Gmail → Settings → Forwarding and POP/IMAP → Enable IMAP); the deploy turns the reader on
  whenever SMTP is wired (`EMAIL_INTAKE=off` to stop it). The Dashboard's *Pipes* shows when the mailbox was
  last read and how many emails became leads or couldn't be read.

**Attribution snippet**: the client page's snippet URL (`…/static/wmt.js?c=<client>.<public key>`) on every
page of the site (GTM Custom HTML tag on All Pages; RealtyNinja: Settings → Custom Code → Custom Header Code).
It keeps gclid / fbclid / UTMs for 90 days and fills them into every form; WordPress forms need hidden fields
with those names (the client page lists them). Platform forms that post to their own servers (RealtyNinja, Wix,
Squarespace) can't carry them, so the snippet also sends a **beacon** to the CRM when such a form is
submitted: the email/phone typed in plus the ad details. When the lead arrives within 48 hours by any route
(email, Zapier, a call) the CRM joins the two by email or phone, so the lead still shows which ad brought it.
The `c=` key is public and can't create leads.

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

1. Buy one Canadian local number for the agency (alerts and reminders to clients come from it) and one per
   client that will text leads, each with SMS capability. Canada-to-Canada texting needs no 10DLC campaign
   registration (that is a US requirement; if a client ever texts US numbers, register a 10DLC brand then).
   Upgrade the Twilio account off the free trial first; trial accounts can only text verified numbers.
2. On **every** number, Messaging → "A message comes in": Webhook, `https://<service>/v1/twilio/sms`, HTTP POST.
   On each **client** number also set Voice → "A call comes in": Webhook, `https://<service>/v1/twilio/voice`,
   HTTP POST, so a lead who calls the number back rings the client's phone (the forwarding number on the client's
   settings page, else the owner's mobile) with the lead's own number as caller id. Delivery receipts are
   requested per message automatically.
   How replies work: a lead's text is relayed from the client's number to the assignee's phone as
   "[A14] Mike: …"; whatever the assignee texts back to that number goes to the lead, logged as a touch. A text
   starting with a ref ("A15 Hi Sara…") goes to that lead instead; the codes 1-5, W, L, K, N still work.
   "Relay lead texts" on the client's settings page turns this off (alerts only, answer from the app).
   Keeping personal mobiles out of it: on a client with a texting number, the lead page's Call button rings the
   user's own phone first and then dials the lead showing the business number (two Twilio legs, a few cents a
   minute), the Text button opens Messages addressed to the business number with the lead's ref pre-filled, and
   alerts link to the lead page instead of printing the lead's number. A lead therefore only ever sees, saves and
   calls back the business number.
3. Put the agency number in `crm-twilio-agency-from`; put each client's number on their settings page
   ("Their texting number").
4. Set up the three secrets, redeploy, then text a reply code from your own mobile (the one on your Account
   page) to the agency number: `?` gets the code list back.

### Email

Three ways; pick one. The Google Workspace one is the least work and is what the Campaign Manager already does.

**Google Workspace mailbox (recommended to start).** Use a dedicated mailbox such as `leads@web-marketing-toronto.com`
(create it in the Admin console, or use `campaigns@`). On that account: turn on 2-Step Verification, then
Google Account → Security → 2-Step Verification → App passwords → create one named "WMT CRM" (16 characters).

```bash
printf '%s' 'xxxx xxxx xxxx xxxx' | gcloud secrets create crm-smtp-password --data-file=- --replication-policy=user-managed --locations=northamerica-northeast2
EMAIL_FROM=leads@web-marketing-toronto.com bash deploy/deploy.sh
```

The From address must be the mailbox itself (or a "Send mail as" alias on it), or Google rewrites it. Every
message the CRM sends appears in that mailbox's Sent folder, and emails to leads carry the client's address as
Reply-To, so replies go to the client. Workspace allows 2,000 messages a day per mailbox, far above what a dozen
clients generate. If the mailbox is not the From address, add `SMTP_USER=mailbox@... ` before `bash deploy/deploy.sh`.

**Postmark.** Create a server, use its Server API token (`crm-postmark-token`), and verify the sending domain under
Sender Signatures with the DKIM and Return-Path records. New accounts can only email your own domain until
Postmark approves them, usually the same day.

**SendGrid.** An API key with Mail Send (`crm-sendgrid-api-key`) plus domain authentication.

With any of them the From address is `EMAIL_FROM` (default `leads@web-marketing-toronto.com`; override with
`EMAIL_FROM=... bash deploy/deploy.sh`).

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

1. The Campaign Manager's `google-ads.yaml` is all the CRM needs, stored as `crm-google-ads-yaml`. It comes
   in two flavours and both work: OAuth (`client_id`, `client_secret`, `refresh_token`) or service account
   (`json_key_file_path` pointing at the key JSON). For the service-account kind, store the key file too,
   as `crm-google-ads-sa-key`; the deploy mounts it and the CRM ignores the VM path written in the yaml.
   Either way `login_customer_id` is the MCC, and the identity (the OAuth user, or the service account's
   email) must be a user on that MCC. From the Campaign Manager VM:

   ```bash
   gcloud secrets create crm-google-ads-yaml --data-file=/home/campaigns/wmt-ads-manager/google-ads.yaml --replication-policy=user-managed --locations=northamerica-northeast2
   gcloud secrets create crm-google-ads-sa-key --data-file=/home/campaigns/wmt-ads-manager/service_account.json --replication-policy=user-managed --locations=northamerica-northeast2
   ```
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

#### Checking Google Ads is working

Four layers, each provable on its own. Stop at the first one that fails.

1. **Credentials reach the server.** Dashboard → *Pipes* → *Ad platforms* says "Google: credentials loaded
   (google-ads.yaml, service account)". If it says no credentials, the secrets above don't exist or the deploy
   ran before they did (the deploy's `Wired:` line lists what was mounted); for the service-account kind
   both secrets are needed.
2. **The MCC can see the client's account.** Client settings → *Conversion uploads* → type the customer id →
   **Check Google Ads connection**. It signs in with the refresh token, reads the account name, currency and
   last-7-day spend, and lists every import-from-clicks conversion action with its id and status. Errors come
   back in plain words (developer token not approved, MCC doesn't manage that account, wrong id…). Nothing is
   saved or sent.
3. **The conversion actions are right.** In that same result, use *Use for Qualified / Use for Won* to fill
   the ids, then Save. The check flags an id that points at a removed action, a primary action (keep them
   secondary for the first three months), or a manager account id.
4. **A real upload is accepted.** Add a lead by hand with an email address (not a test lead; those never
   upload), move it to Contacted. Within a minute the lead's *Ad platform sync* table (agency view) shows
   *Qualified → Google Ads · sent*. Google answers synchronously, so "sent" means Google accepted it;
   a rejection shows the reason on the same row and *Retry now* re-queues it. Then delete the lead.

   What "accepted" does not mean: that it will count. A lead without a click id is matched later on hashed
   email/phone against the Google tag's user-provided data from the site, which needs step 3 under
   "Google Ads" above. Google Ads → Goals → Conversions → the action → *Diagnostics* shows uploads and
   match rate after a few hours; the Conversions column in reports fills in over 1–3 days, and only for
   clicks that actually came from an ad.

**"Google Ads API vNN has been retired"** from the check: Google keeps each API version about a year
(v25 until roughly August 2027). Redeploy with the next one and nothing else changes:
`GOOGLE_ADS_API_VERSION=v26 EMAIL_FROM=… bash deploy/deploy.sh` (or bump
`DEFAULT_GOOGLE_ADS_API_VERSION` in `app/services/adplatforms.py`).

**Attribution (the other direction)** is checked on the site: open the client's page as
`https://their-site.com/?gclid=TEST123`, submit the form, and the lead shows a Google Ads chip with
*Google click: yes* under its source. If it shows Unknown, `wmt.js` isn't on the page or the form posts
without the hidden fields (see step 5).

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

## 7. Custom domain (a clean URL for clients)

Cloud Run's own domain mapping is not offered in the Toronto region, so the domain goes through
**Firebase Hosting**, which sits in front of the service for free with a Google-managed certificate.
`deploy/hosting/` holds the config (every path is rewritten to the `crm` service).

1. Add Firebase to the project once: https://console.firebase.google.com → *Add project* → pick the
   existing `wmt-crm` project (no Analytics needed).
2. On the VM: `npm install -g firebase-tools && firebase login --no-localhost`, then
   `cd ~/crm/deploy/hosting && firebase deploy --only hosting`. The site is now at
   `https://wmt-crm.web.app`, already serving the CRM.
3. Firebase console → Hosting → *Add custom domain* → e.g. `app.yourproduct.com` or
   `crm.web-marketing-toronto.com` → add the TXT record it shows (ownership), then the two A records, at
   your DNS host. The certificate is issued within an hour or so of the records going live.
4. Tell the CRM its address, once: `cd ~/crm && BASE_URL=https://app.yourproduct.com EMAIL_FROM=… bash deploy/deploy.sh`.
   Later deploys remember it. Every link the CRM sends (alerts, one-tap actions, password resets),
   the intake URLs, the snippet URL and the Twilio/CallRail webhook URLs now use the domain, so update
   the two Twilio webhooks and CallRail to the new address (the old run.app URLs keep working too).

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
  Same for a team member, from **Team**.
- **Someone leaves the agency**: Team → *Disable*. They are signed out everywhere at once and the audit trail
  keeps their name. You can't disable your own login or the last active one.
- **"Are my alerts actually reaching me?"**: Account → *Send me a test text* / *Send me a test email*. The text
  comes from the agency number; replying `?` to it proves the inbound side too.
- **Is the scheduler running?**: the Dashboard's *Pipes* panel shows when the minute tick last ran and turns
  red after 5 minutes without one (then check Cloud Scheduler and the Cloud Run logs).
- **Rotate a client's intake key** (if a URL leaked): client page → *Generate a new key*, then update their forms
  and the CallRail / Google lead-form webhooks.
- **A conversion didn't reach Google or Meta**: the lead page (agency view) shows every upload, its status and
  the platform's reason; *Retry now* re-queues it. Settings → *Recent uploads* lists the last ten per client.
- **Someone texts STOP**: recorded on the lead; the CRM never texts that number for that client again
  unless they text START.
- **Change a client's reminder timings or message wording**: all on their settings page; takes effect for the
  next lead. The follow-up schedule applies to leads already in the cadence at their next touch. The same
  section holds the **First text**: what the Text button on a lead pre-fills the first time anyone texts
  that lead (signed with the texter's own name; blank after the first text). It is never sent on its own.
  Clients edit this one themselves: an owner's Account page links to **Text wording** (`/messages`), with a
  live preview and the fill-ins explained; the agency page shows whatever they saved.

## Costs at this scale

Cloud SQL `db-g1-small` with 10 GB is roughly $35–45 CAD/month; Cloud Run with one warm instance roughly
$15–25; Secret Manager and Scheduler are cents. Twilio: about $1.50/month per number plus roughly a cent per
text; a client doing 60 leads a month with the full cadence is a few dollars. Postmark's smallest plan
covers 10,000 emails a month.
