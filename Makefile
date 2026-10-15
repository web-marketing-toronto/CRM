PGDIR ?= /home/pgdata
PGBIN ?= /usr/lib/postgresql/16/bin

.PHONY: dev db db-stop migrate test lint seed admin

dev:            ## run the app with reload on :8000
	uv run uvicorn app.main:app --reload --port 8000

db:             ## start a local Postgres on /tmp:5433 (first run initialises it)
	@test -d $(PGDIR)/data || (mkdir -p $(PGDIR) && chown postgres:postgres $(PGDIR) && su postgres -c "$(PGBIN)/initdb -D $(PGDIR)/data -U crm --auth=trust >/dev/null")
	@su postgres -c "$(PGBIN)/pg_ctl -D $(PGDIR)/data -o '-p 5433 -k /tmp' -l $(PGDIR)/pg.log start" || true
	@psql -h /tmp -p 5433 -U crm -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='crm'" | grep -q 1 || psql -h /tmp -p 5433 -U crm -d postgres -c "CREATE DATABASE crm"
	@psql -h /tmp -p 5433 -U crm -d postgres -tc "SELECT 1 FROM pg_database WHERE datname='crm_test'" | grep -q 1 || psql -h /tmp -p 5433 -U crm -d postgres -c "CREATE DATABASE crm_test"

db-stop:
	@su postgres -c "$(PGBIN)/pg_ctl -D $(PGDIR)/data stop"

migrate:        ## apply migrations to the dev database, then make sure crm_app can log in
	uv run python -m app.migrate
	@psql -h /tmp -p 5433 -U crm -d crm -c "ALTER ROLE crm_app LOGIN PASSWORD 'crm_app'" >/dev/null

test:
	uv run pytest -q -p no:warnings

lint:
	uv run ruff check app tests

seed:           ## demo client + sample leads (dev only)
	uv run python -m app.cli seed-demo

admin:          ## create the first agency admin: make admin EMAIL=you@x.com NAME="Jeff"
	uv run python -m app.cli create-admin --email $(EMAIL) --name "$(NAME)"
