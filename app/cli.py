"""Operator commands.

    python -m app.cli create-admin --email you@agency.com --name "Jeff"
    python -m app.cli set-password --email you@agency.com [--password ...] [--clear-mfa]
    python -m app.cli seed-demo            # local only: a demo client with sample leads
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from datetime import UTC, datetime, timedelta

from app.auth import service as auth_service
from app.auth.service import AuthError
from app.config import get_settings
from app.db import close_pool, db, init_pool
from app.models import Channel, Platform, Role
from app.services.attribution import IntakeFields
from app.services.leads import create_from_intake


def create_admin(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Password (min 10 chars): ")
    init_pool()
    try:
        with db(None, "system") as conn:
            user = auth_service.create_user(
                conn, email=args.email, name=args.name, role=Role.AGENCY_ADMIN, tenant_id=None, password=password,
                must_set_password=False,
            )
            auth_service.audit(conn, action="admin_created_cli", user_id=user["id"], tenant_id=None)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        close_pool()
    print(f"created agency admin {args.email}. They will set up an authenticator app at first login.")
    return 0


def set_password(args: argparse.Namespace) -> int:
    """Operator reset for any login: new password, lockout cleared, every session ended, optionally the authenticator too."""
    password = args.password or getpass.getpass("New password (min 10 chars): ")
    init_pool()
    try:
        with db(None, "system") as conn:
            user = conn.execute("SELECT * FROM app_user WHERE email_lower = lower(%s)", (args.email,)).fetchone()
            if user is None:
                print(f"error: no login with the email {args.email}", file=sys.stderr)
                return 1
            auth_service.set_password(conn, user["id"], password)
            conn.execute("UPDATE app_user SET failed_logins = 0, locked_until = NULL, is_active = true WHERE id = %s", (user["id"],))
            conn.execute("DELETE FROM session WHERE user_id = %s", (user["id"],))
            if args.clear_mfa:
                conn.execute("UPDATE app_user SET totp_enabled = false, totp_secret_enc = NULL, totp_last_counter = NULL, backup_codes = '[]'::jsonb WHERE id = %s", (user["id"],))
            auth_service.audit(conn, action="password_reset_cli", user_id=user["id"], tenant_id=user["tenant_id"], detail={"mfa_cleared": bool(args.clear_mfa)})
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        close_pool()
    print(f"password set for {user['email']}; lockout cleared, sessions ended" + ("; authenticator cleared, they enrol again at next login" if args.clear_mfa else ""))
    return 0


DEMO_LEADS = [
    ("Mike Reynolds", "+1 705 555 0142", "mike.r@example.com", "12 Birch Lane, Bracebridge, ON P1L 1A1", "Roof replacement", Channel.WEBSITE_FORM, {"gclid": "demo-gclid-1", "utm_campaign": "Roofing - Muskoka"}, 0.5),
    ("Dana Whitfield", "705-555-0177", None, "48 Lakeshore Rd, Gravenhurst, ON", "Repair", Channel.CALL, {"callrail_call_id": "CR-9931", "utm_source": "google", "utm_medium": "cpc"}, 3),
    ("Priya Sandhu", "(705) 555-0199", "priya@example.com", "Huntsville", "Roof replacement", Channel.META_LEAD_AD, {"meta_lead_id": "1122334455", "campaign_id": "238811"}, 26),
    ("Tom Bellamy", "705 555 0123", "tom.b@example.com", "9 Pine St, Toronto, ON M4E 1A1", "Eavestrough", Channel.WEBSITE_FORM, {"referrer": "https://www.google.com/"}, 72),
    ("Carla Ng", "+17055550111", None, "Port Carling", "Skylight", Channel.WEBSITE_FORM, {"fbclid": "demo-fbclid", "utm_source": "facebook", "utm_medium": "paid"}, 240),
]


def seed_demo(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.is_prod:
        print("refusing to seed demo data in prod", file=sys.stderr)
        return 1
    init_pool()
    try:
        with db(None, "system") as conn:
            tenant = conn.execute("SELECT * FROM tenant WHERE slug = 'demo-roofing'").fetchone()
            if tenant is None:
                tenant = conn.execute(
                    """
                    INSERT INTO tenant (slug, name, job_types, service_area)
                    VALUES ('demo-roofing', 'North Ridge Roofing (demo)', ARRAY['Roof replacement','Repair','Eavestrough','Skylight'],
                            '{"center": {"lat": 45.04, "lng": -79.31}, "radius_km": 60, "buffer_km": 25}'::jsonb)
                    RETURNING *
                    """
                ).fetchone()
                owner = auth_service.create_user(
                    conn, email="dan@northridge.example", name="Dan Mercer", role=Role.OWNER, tenant_id=tenant["id"],
                    password="demo-password-1", must_set_password=False,
                )
                conn.execute("UPDATE tenant SET default_assignee_id = %s WHERE id = %s", (owner["id"], tenant["id"]))
                tenant = conn.execute("SELECT * FROM tenant WHERE id = %s", (tenant["id"],)).fetchone()
            now = datetime.now(UTC)
            for name, phone, email, address, job, channel, attr, hours_ago in DEMO_LEADS:
                if conn.execute("SELECT 1 FROM lead WHERE tenant_id = %s AND contact_name = %s", (tenant["id"], name)).fetchone():
                    continue
                fields = IntakeFields(contact_name=name, phone_raw=phone, email=email, job_address=address, job_type=job, channel=channel, attribution=attr, message="Please call me about a quote.")
                from app.services.attribution import infer_platform

                fields.platform = infer_platform(channel, attr)
                create_from_intake(conn, tenant, fields, raw_payload={"demo": True}, ip=None, user_agent=None, maps_api_key=None, now=now - timedelta(hours=hours_ago))
    finally:
        close_pool()
    print("demo client 'demo-roofing' ready: login dan@northridge.example / demo-password-1")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crm")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin")
    p.add_argument("--email", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--password", help="omit to be prompted")
    p.set_defaults(func=create_admin)
    r = sub.add_parser("set-password")
    r.add_argument("--email", required=True)
    r.add_argument("--password", help="omit to be prompted")
    r.add_argument("--clear-mfa", action="store_true", help="also remove the authenticator so they enrol a new one")
    r.set_defaults(func=set_password)
    s = sub.add_parser("seed-demo")
    s.set_defaults(func=seed_demo)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


def random_password() -> str:
    return secrets.token_urlsafe(12)


__all__ = ["main", "Platform"]
