# WMT CRM

A lead CRM for Web Marketing Toronto's contractor clients. It does three things:

1. **Captures every lead with its source**: website forms, CallRail calls, Meta and Google lead forms,
   Zapier, and manual entry all post to one intake endpoint, which normalizes, dedupes, geocodes and
   records the click IDs needed later for ad-platform feedback.
2. **Makes the client follow up**: a phone-first app with a lead list, five status buttons, tap-to-call
   logging, and (phase 2) SMS and email reminders that don't stop until the lead is won or lost.
3. **Feeds outcomes back to Google Ads and Meta** (phase 3) so campaigns optimize toward revenue, and
   reports cost per lead and cost per won deal by channel (phase 4).

The full specification lives in the Claude doc "Contractor CRM — Build Spec".

## Stack

Python 3.13, FastAPI, server-rendered Jinja templates with HTMX, Postgres 16 (row-level security per
tenant), Cloud Run in Toronto, Cloud SQL, Cloud Scheduler. Authentication is in-app: Argon2 passwords,
TOTP two-factor with backup codes, server-side sessions. No PostGIS; the service-area check is a
point-in-polygon in Python.

## Layout

```
app/
  main.py           app factory, error handling, security headers
  config.py         settings from environment
  db.py             connection pool + the RLS context (db(tenant_id, role))
  migrate.py        SQL migration runner (python -m app.migrate)
  cli.py            create-admin, seed-demo
  auth/             passwords, TOTP, sessions, request principals
  services/         normalize, attribution, geo, leads, worker
  routers/          intake (API), auth, leads, admin, account, cron, health
  templates/        Jinja pages; leads/_lead.html is the ticket re-rendered after each action
  static/           app.css, htmx, PWA manifest + service worker, icons
migrations/         0001_initial.sql ...
tests/              pytest against a real Postgres
deploy/             setup_gcp.sh (one-time), deploy.sh (every release)
docs/SETUP.md       the steps Jeff runs
```

## Local development

```bash
make db        # start Postgres on /tmp:5433 (creates crm and crm_test databases)
cp .env.example .env
make migrate   # apply migrations, give crm_app a login
make seed      # demo client: dan@northridge.example / demo-password-1
make dev       # http://localhost:8000
make test
```

## Deploying

See `docs/SETUP.md`. In short: `bash deploy/setup_gcp.sh` once, then `bash deploy/deploy.sh` per release.
