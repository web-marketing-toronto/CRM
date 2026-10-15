"""The numbers: per client and across clients, for a date range.

Everything reads from `lead`, `lead_source`, `activity` and `ad_spend`, so it is as honest as what
was logged. Attribution is first touch: the platform on the lead's first lead_source row. Leads with
no click or campaign data are reported as what they are (organic / direct / unknown), never hidden,
so cost per lead is never quietly flattered.
"""

from __future__ import annotations

import calendar
import csv
import io
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import psycopg

from app.models import CHANNEL_LABELS, PLATFORM_LABELS, STATUS_LABELS, Channel, Platform
from app.services import followup

PAID = (Platform.GOOGLE_ADS.value, Platform.META_ADS.value)


@dataclass
class Period:
    key: str
    label: str
    start: datetime  # inclusive, UTC
    end: datetime    # exclusive, UTC

    @property
    def days(self) -> int:
        return max(int((self.end - self.start).total_seconds() // 86400), 1)


def period(key: str, tz_name: str = "America/Toronto", start: str | None = None, end: str | None = None, now: datetime | None = None) -> Period:
    tz = ZoneInfo(tz_name)
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    first = today.replace(day=1)
    if key == "last_month":
        last_end = first
        last_start = (first - timedelta(days=1)).replace(day=1)
        s, e, label = last_start, last_end, last_start.strftime("%B %Y")
    elif key == "last_30":
        s, e, label = today - timedelta(days=29), today + timedelta(days=1), "Last 30 days"
    elif key == "last_90":
        s, e, label = today - timedelta(days=89), today + timedelta(days=1), "Last 90 days"
    elif key == "year":
        s, e, label = today.replace(month=1, day=1), today + timedelta(days=1), f"{today.year} to date"
    elif key == "custom" and start and end:
        try:
            s, e = date.fromisoformat(start), date.fromisoformat(end) + timedelta(days=1)
        except ValueError:
            s, e = first, today + timedelta(days=1)
        label = f"{s.strftime('%b %-d, %Y')} to {(e - timedelta(days=1)).strftime('%b %-d, %Y')}"
    elif key.startswith("month:"):
        y, m = (int(x) for x in key[6:].split("-"))
        s = date(y, m, 1)
        e = date(y + (m == 12), (m % 12) + 1, 1)
        label = s.strftime("%B %Y")
        key = "custom"
    else:
        key, s, e, label = "this_month", first, today + timedelta(days=1), today.strftime("%B %Y") + " so far"
    return Period(key, label, datetime.combine(s, datetime.min.time(), tz).astimezone(UTC), datetime.combine(e, datetime.min.time(), tz).astimezone(UTC))


def _pct(n: int | float, d: int | float) -> float | None:
    return round(100 * n / d, 1) if d else None


def _div(n: Decimal | float | int | None, d: int | float | None) -> float | None:
    if n is None or not d:
        return None
    return round(float(n) / float(d), 2)


@dataclass
class SourceRow:
    key: str
    label: str
    leads: int = 0
    contacted: int = 0
    won: int = 0
    lost: int = 0
    bad: int = 0
    revenue: float = 0.0
    spend: float | None = None
    clicks: int | None = None

    @property
    def qualified_rate(self) -> float | None:
        return _pct(self.contacted, self.leads)

    @property
    def close_rate(self) -> float | None:
        return _pct(self.won, self.won + self.lost)

    @property
    def cpl(self) -> float | None:
        return _div(self.spend, self.leads) if self.spend is not None else None

    @property
    def cost_per_won(self) -> float | None:
        return _div(self.spend, self.won) if self.spend is not None else None

    @property
    def roas(self) -> float | None:
        return _div(self.revenue, self.spend) if self.spend else None

    @property
    def bad_share(self) -> float | None:
        return _pct(self.bad, self.leads)


@dataclass
class Summary:
    period: Period
    by_platform: list[SourceRow] = field(default_factory=list)
    by_channel: list[SourceRow] = field(default_factory=list)
    by_campaign: list[SourceRow] = field(default_factory=list)
    total: SourceRow = field(default_factory=lambda: SourceRow("total", "All sources"))
    speed_median_min: float | None = None
    speed_within_hour_pct: float | None = None
    speed_n: int = 0
    compliance_pct: float | None = None
    compliance_n: int = 0
    overdue: int = 0
    open_leads: int = 0
    lost_reasons: list[tuple[str, int]] = field(default_factory=list)
    status_counts: dict[str, int] = field(default_factory=dict)


LEAD_FILTER = "l.tenant_id = %(tid)s AND l.created_at >= %(start)s AND l.created_at < %(end)s AND NOT (l.flags ? 'test')"

AGG = """
    count(*) AS leads,
    count(*) FILTER (WHERE l.first_contact_at IS NOT NULL OR l.status IN ('contacted', 'quoted', 'won')) AS contacted,
    count(*) FILTER (WHERE l.status = 'won') AS won,
    count(*) FILTER (WHERE l.status = 'lost') AS lost,
    count(*) FILTER (WHERE l.status = 'lost' AND l.lost_reason IN ('spam', 'out_of_area')) AS bad,
    coalesce(sum(l.deal_value) FILTER (WHERE l.status = 'won'), 0) AS revenue
"""


def _add(row: SourceRow, r: dict) -> SourceRow:
    row.leads += int(r["leads"])
    row.contacted += int(r["contacted"])
    row.won += int(r["won"])
    row.lost += int(r["lost"])
    row.bad += int(r["bad"])
    row.revenue += float(r["revenue"])
    return row


def _fill(row: SourceRow, r: dict) -> SourceRow:
    row.leads, row.contacted, row.won, row.lost, row.bad, row.revenue = int(r["leads"]), int(r["contacted"]), int(r["won"]), int(r["lost"]), int(r["bad"]), float(r["revenue"])
    return row


def tenant_summary(conn: psycopg.Connection, tenant: dict, p: Period, *, now: datetime | None = None) -> Summary:
    now = now or datetime.now(UTC)
    params = {"tid": tenant["id"], "start": p.start, "end": p.end}
    s = Summary(period=p)

    spend = {r["platform"]: r for r in conn.execute(
        "SELECT platform, sum(spend) AS spend, sum(clicks) AS clicks FROM ad_spend WHERE tenant_id = %(tid)s AND date >= %(start)s::date AND date < %(end)s::date GROUP BY platform", params
    ).fetchall()}

    rows = {r["platform"]: r for r in conn.execute(f"SELECT l.platform, {AGG} FROM lead l WHERE {LEAD_FILTER} GROUP BY l.platform", params).fetchall()}
    for plat in Platform:
        r = rows.get(plat.value)
        sp = spend.get(plat.value)
        if r is None and sp is None:
            continue
        row = SourceRow(plat.value, PLATFORM_LABELS[plat])
        if r:
            _fill(row, r)
        if plat.value in PAID:
            row.spend = float(sp["spend"]) if sp else 0.0
            row.clicks = int(sp["clicks"]) if sp else 0
        s.by_platform.append(row)
    s.by_platform.sort(key=lambda x: (-(x.spend or 0), -x.leads))

    tot = conn.execute(f"SELECT {AGG} FROM lead l WHERE {LEAD_FILTER}", params).fetchone()
    _fill(s.total, tot)
    paid_spend = sum((r.spend or 0) for r in s.by_platform)
    s.total.spend = paid_spend if any(r.spend is not None for r in s.by_platform) else None
    s.total.clicks = sum((r.clicks or 0) for r in s.by_platform) or None

    for r in conn.execute(f"SELECT l.channel, {AGG} FROM lead l WHERE {LEAD_FILTER} GROUP BY l.channel ORDER BY leads DESC", params).fetchall():
        s.by_channel.append(_fill(SourceRow(r["channel"], CHANNEL_LABELS.get(Channel(r["channel"]), r["channel"])), r))

    # Campaigns: leads by first-touch campaign, joined to spend by campaign id (Google and Meta ids are what the
    # platforms report spend under), falling back to the name for leads that only carried a UTM.
    camp_rows = conn.execute(
        f"""
        WITH first_src AS (
            SELECT DISTINCT ON (lead_id) lead_id, campaign_id, utm_campaign
            FROM lead_source ORDER BY lead_id, created_at
        )
        SELECT l.platform, coalesce(nullif(f.campaign_id, ''), '') AS campaign_id,
               coalesce(nullif(l.campaign_name, ''), nullif(f.utm_campaign, ''), '') AS campaign_name,
               {AGG}
        FROM lead l LEFT JOIN first_src f ON f.lead_id = l.id
        WHERE {LEAD_FILTER} AND l.platform IN ('google_ads', 'meta_ads')
        GROUP BY 1, 2, 3
        """,
        params,
    ).fetchall()
    camp_spend = conn.execute(
        "SELECT platform, campaign_id, max(campaign_name) AS campaign_name, sum(spend) AS spend, sum(clicks) AS clicks FROM ad_spend WHERE tenant_id = %(tid)s AND date >= %(start)s::date AND date < %(end)s::date GROUP BY 1, 2",
        params,
    ).fetchall()
    by_key: dict[tuple[str, str], SourceRow] = {}
    name_index: dict[tuple[str, str], tuple[str, str]] = {}
    for sp in camp_spend:
        key = (sp["platform"], sp["campaign_id"])
        row = SourceRow(f"{sp['platform']}:{sp['campaign_id']}", sp["campaign_name"] or sp["campaign_id"], spend=float(sp["spend"]), clicks=int(sp["clicks"]))
        by_key[key] = row
        if sp["campaign_name"]:
            name_index[(sp["platform"], sp["campaign_name"].strip().lower())] = key
    for r in camp_rows:
        key = (r["platform"], r["campaign_id"])
        if r["campaign_id"] == "" and r["campaign_name"]:
            key = name_index.get((r["platform"], r["campaign_name"].strip().lower()), (r["platform"], f"name:{r['campaign_name']}"))
        row = by_key.get(key)
        if row is None:
            row = SourceRow(f"{r['platform']}:{key[1]}", r["campaign_name"] or r["campaign_id"] or f"{PLATFORM_LABELS[Platform(r['platform'])]} (no campaign)")
            by_key[key] = row
        elif r["campaign_name"] and (not row.label or row.label == key[1]):
            row.label = r["campaign_name"]
        _add(row, r)
    for (plat, _), row in by_key.items():
        row.label = f"{PLATFORM_LABELS[Platform(plat)]}: {row.label}"
        if row.spend is None:
            row.spend = 0.0
    s.by_campaign = sorted(by_key.values(), key=lambda x: (-(x.spend or 0), -x.leads))

    # Speed to lead: minutes from arrival to the first logged touch; imports and calls that were answered on the
    # spot are excluded (an answered call is instant by definition and would flatter the number).
    sp = conn.execute(
        f"""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM (l.first_contact_at - l.created_at)) / 60) AS med,
               count(*) FILTER (WHERE l.first_contact_at - l.created_at <= interval '1 hour') AS within_hour, count(*) AS n
        FROM lead l WHERE {LEAD_FILTER} AND l.first_contact_at IS NOT NULL AND l.channel NOT IN ('import')
          AND NOT (l.channel = 'call' AND l.first_contact_at - l.created_at < interval '2 minutes')
        """,
        params,
    ).fetchone()
    s.speed_median_min = round(float(sp["med"]), 1) if sp["med"] is not None else None
    s.speed_within_hour_pct = _pct(int(sp["within_hour"]), int(sp["n"]))
    s.speed_n = int(sp["n"])

    # Follow-up compliance: closed before the cadence ended, or touched at least as often as the cadence asks by now.
    cadence = sorted(float(d) for d in followup.settings_for(tenant)["cadence_days"])
    leads = conn.execute(
        f"""
        SELECT l.created_at, l.status, l.won_at, l.lost_at,
               (SELECT count(*) FROM activity a WHERE a.lead_id = l.id AND a.type IN ('call_attempt', 'text_sent', 'email_sent')) AS touches
        FROM lead l WHERE {LEAD_FILTER} AND l.channel <> 'import' AND NOT (l.flags ? 'needs_review')
        """,
        params,
    ).fetchall()
    ok = 0
    for ld in leads:
        age_days = (now - ld["created_at"]).total_seconds() / 86400
        if ld["status"] in ("won", "lost"):
            ok += 1
            continue
        expected = 1 + sum(1 for d in cadence if age_days >= d + 0.5)
        if int(ld["touches"]) >= expected:
            ok += 1
    s.compliance_n = len(leads)
    s.compliance_pct = _pct(ok, len(leads))

    s.lost_reasons = [(r["lost_reason"], int(r["n"])) for r in conn.execute(f"SELECT l.lost_reason, count(*) AS n FROM lead l WHERE {LEAD_FILTER} AND l.status = 'lost' GROUP BY 1 ORDER BY n DESC", params).fetchall()]
    s.status_counts = {r["status"]: int(r["n"]) for r in conn.execute(f"SELECT l.status, count(*) AS n FROM lead l WHERE {LEAD_FILTER} GROUP BY 1", params).fetchall()}
    live = conn.execute(
        """
        SELECT count(*) FILTER (WHERE l.status NOT IN ('won', 'lost')) AS open_leads,
               count(*) FILTER (WHERE l.status NOT IN ('won', 'lost') AND NOT (l.flags ? 'needs_review') AND (
                   (l.status = 'new' AND l.created_at < now() - interval '1 hour')
                   OR (l.status IN ('contacted', 'no_response') AND coalesce(l.last_touch_at, l.created_at) < now() - interval '3 days')
                   OR (l.status = 'quoted' AND coalesce(l.quoted_at, l.created_at) < now() - interval '10 days'))) AS overdue
        FROM lead l WHERE l.tenant_id = %(tid)s
        """,
        params,
    ).fetchone()
    s.open_leads, s.overdue = int(live["open_leads"]), int(live["overdue"])
    return s


@dataclass
class ClientRow:
    tenant: dict
    summary: Summary


def agency_summary(conn: psycopg.Connection, p: Period, *, now: datetime | None = None) -> tuple[list[ClientRow], SourceRow, dict[str, Any]]:
    rows: list[ClientRow] = []
    total = SourceRow("total", "All clients")
    speeds: list[tuple[float, int]] = []
    compliance: list[tuple[float, int]] = []
    for tenant in conn.execute("SELECT * FROM tenant WHERE is_active ORDER BY name").fetchall():
        sm = tenant_summary(conn, tenant, p, now=now)
        rows.append(ClientRow(tenant, sm))
        t = sm.total
        _add(total, {"leads": t.leads, "contacted": t.contacted, "won": t.won, "lost": t.lost, "bad": t.bad, "revenue": t.revenue})
        if t.spend is not None:
            total.spend = (total.spend or 0.0) + t.spend
        if sm.speed_median_min is not None:
            speeds.append((sm.speed_median_min, sm.speed_n))
        if sm.compliance_pct is not None:
            compliance.append((sm.compliance_pct, sm.compliance_n))
    extras = {
        "speed_median_min": round(sum(m * n for m, n in speeds) / sum(n for _, n in speeds), 1) if speeds and sum(n for _, n in speeds) else None,
        "compliance_pct": round(sum(c * n for c, n in compliance) / sum(n for _, n in compliance), 1) if compliance and sum(n for _, n in compliance) else None,
    }
    rows.sort(key=lambda r: (-r.summary.total.leads, r.tenant["name"]))
    return rows, total, extras


# --- CSV -----------------------------------------------------------------------------------------

CSV_COLUMNS = ["client", "ref", "received", "name", "company", "phone", "email", "job_address", "job_type", "channel", "platform", "campaign", "status", "deal_value", "lost_reason", "assigned_to", "first_contact_minutes", "last_touch", "touches", "landing_page", "gclid", "fbclid", "message"]


def leads_csv(conn: psycopg.Connection, p: Period, *, tenant_id: UUID | None = None, tz_name: str = "America/Toronto") -> str:
    tz = ZoneInfo(tz_name)
    where = "l.created_at >= %(start)s AND l.created_at < %(end)s AND NOT (l.flags ? 'test')"
    params: dict[str, Any] = {"start": p.start, "end": p.end}
    if tenant_id is not None:
        where += " AND l.tenant_id = %(tid)s"
        params["tid"] = tenant_id
    rows = conn.execute(
        f"""
        WITH first_src AS (SELECT DISTINCT ON (lead_id) * FROM lead_source ORDER BY lead_id, created_at)
        SELECT t.name AS client, l.*, u.name AS assignee_name, f.landing_page, f.gclid, f.fbclid,
               (SELECT count(*) FROM activity a WHERE a.lead_id = l.id AND a.type IN ('call_attempt', 'text_sent', 'email_sent')) AS touches
        FROM lead l JOIN tenant t ON t.id = l.tenant_id LEFT JOIN app_user u ON u.id = l.assigned_to LEFT JOIN first_src f ON f.lead_id = l.id
        WHERE {where} ORDER BY t.name, l.created_at
        """,
        params,
    ).fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for r in rows:
        fc = round((r["first_contact_at"] - r["created_at"]).total_seconds() / 60, 1) if r["first_contact_at"] else ""
        w.writerow([
            r["client"], r["ref_code"], r["created_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M"), r["contact_name"] or "", r["company"] or "", r["phone_e164"] or "", r["email"] or "",
            r["job_address"] or "", r["job_type"] or "", CHANNEL_LABELS.get(Channel(r["channel"]), r["channel"]), PLATFORM_LABELS.get(Platform(r["platform"]), r["platform"]), r["campaign_name"] or "",
            STATUS_LABELS.get(r["status"], r["status"]), r["deal_value"] if r["deal_value"] is not None else "", r["lost_reason"] or "", r["assignee_name"] or "", fc,
            r["last_touch_at"].astimezone(tz).strftime("%Y-%m-%d %H:%M") if r["last_touch_at"] else "", r["touches"], r["landing_page"] or "", r["gclid"] or "", r["fbclid"] or "", (r["message"] or "").replace("\n", " ")[:500],
        ])
    return out.getvalue()


# --- Monthly PDF ----------------------------------------------------------------------------------

def month_period(year: int, month: int, tz_name: str) -> Period:
    return period(f"month:{year:04d}-{month:02d}", tz_name)


def monthly_pdf(tenant: dict, s: Summary, *, show_spend: bool, agency_name: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ink, ink2, line, paper, hivis = colors.HexColor("#16202b"), colors.HexColor("#4f5a66"), colors.HexColor("#d6d9d3"), colors.HexColor("#eef0ec"), colors.HexColor("#ffd400")
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=20, alignment=0, textColor=ink, spaceAfter=2)
    sub = ParagraphStyle("sub", parent=styles["Normal"], fontName="Helvetica", fontSize=10.5, textColor=ink2, spaceAfter=14)
    h2 = ParagraphStyle("h2", parent=styles["Heading2"], fontName="Helvetica-Bold", fontSize=13, textColor=ink, spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("body", parent=styles["Normal"], fontName="Helvetica", fontSize=10, leading=14, textColor=ink)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, textColor=ink2, leading=11)

    def money(v: float | None) -> str:
        return "—" if v is None else f"${v:,.0f}"

    def pct(v: float | None) -> str:
        return "—" if v is None else f"{v:.0f}%"

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.8 * inch, rightMargin=0.8 * inch, topMargin=0.7 * inch, bottomMargin=0.7 * inch, title=f"{tenant['name']} — {s.period.label}", author=agency_name)
    story: list[Any] = [Paragraph(f"{tenant['name']}: lead report", h1), Paragraph(f"{s.period.label} · prepared by {agency_name}", sub)]

    t = s.total
    tiles = [("Leads", str(t.leads)), ("Contacted", pct(t.qualified_rate)), ("Won", f"{t.won} · {money(t.revenue)}"), ("Close rate", pct(t.close_rate))]
    if show_spend and t.spend is not None:
        tiles += [("Ad spend", money(t.spend)), ("Cost per lead", money(t.cpl)), ("Cost per won job", money(t.cost_per_won)), ("Return on ad spend", f"{t.roas:.1f}x" if t.roas is not None else "—")]
    tiles += [("Speed to lead (median)", f"{s.speed_median_min:.0f} min" if s.speed_median_min is not None else "—"), ("Contacted within 1 h", pct(s.speed_within_hour_pct)), ("Follow-up compliance", pct(s.compliance_pct)), ("Spam / out of area", pct(t.bad_share))]
    grid = [tiles[i:i + 4] for i in range(0, len(tiles), 4)]
    data = []
    for row in grid:
        data.append([Paragraph(f"<font size=15><b>{v}</b></font><br/><font size=8.5 color='#4f5a66'>{k}</font>", body) for k, v in row] + [""] * (4 - len(row)))
    tbl = Table(data, colWidths=[1.72 * inch] * 4, rowHeights=[0.62 * inch] * len(data))
    tbl.hAlign = "LEFT"
    tbl.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), paper), ("BOX", (0, 0), (-1, -1), 0.5, line), ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.white), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 9)]))
    story += [tbl]

    def table(title: str, header: list[str], rows: list[list[str]], widths: list[float]) -> None:
        story.append(Paragraph(title, h2))
        if not rows:
            story.append(Paragraph("Nothing in this period.", small))
            return
        tb = Table([header] + rows, colWidths=widths, repeatRows=1)
        tb.hAlign = "LEFT"
        st = [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 9), ("FONT", (0, 1), (-1, -1), "Helvetica", 9), ("TEXTCOLOR", (0, 0), (-1, 0), ink2),
              ("LINEBELOW", (0, 0), (-1, 0), 0.8, ink), ("LINEBELOW", (0, 1), (-1, -1), 0.3, line), ("ALIGN", (1, 0), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
              ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        tb.setStyle(TableStyle(st))
        story.append(tb)

    head = ["Source", "Leads", "Contacted", "Won", "Revenue"] + (["Spend", "Cost / lead", "Cost / won"] if show_spend else []) + ["Spam / OOA"]
    rows = []
    for r in s.by_platform:
        rows.append([r.label, str(r.leads), pct(r.qualified_rate), str(r.won), money(r.revenue)] + ([money(r.spend) if r.spend is not None else "—", money(r.cpl), money(r.cost_per_won)] if show_spend else []) + [pct(r.bad_share)])
    widths = [1.9 * inch, 0.6 * inch, 0.85 * inch, 0.55 * inch, 0.85 * inch] + ([0.75 * inch, 0.8 * inch, 0.8 * inch] if show_spend else []) + [0.8 * inch]
    table("Leads by source", head, rows, widths)

    if s.by_campaign:
        head = ["Campaign", "Leads", "Won", "Revenue"] + (["Spend", "Cost / lead", "Cost / won"] if show_spend else []) + ["Spam / OOA"]
        rows = [[r.label[:48], str(r.leads), str(r.won), money(r.revenue)] + ([money(r.spend), money(r.cpl), money(r.cost_per_won)] if show_spend else []) + [pct(r.bad_share)] for r in s.by_campaign[:25]]
        widths = [2.6 * inch, 0.6 * inch, 0.55 * inch, 0.85 * inch] + ([0.75 * inch, 0.8 * inch, 0.8 * inch] if show_spend else []) + [0.8 * inch]
        table("By campaign", head, rows, widths)

    rows = [[r.label, str(r.leads), pct(r.qualified_rate), str(r.won), money(r.revenue)] for r in s.by_channel]
    table("How leads arrived", ["Channel", "Leads", "Contacted", "Won", "Revenue"], rows, [2.6 * inch, 0.7 * inch, 0.9 * inch, 0.6 * inch, 1.0 * inch])

    if s.lost_reasons:
        from app.models import LOST_REASON_LABELS, LostReason

        rows = [[LOST_REASON_LABELS.get(LostReason(k), k) if k else "No reason", str(n)] for k, n in s.lost_reasons]
        table("Why leads were lost", ["Reason", "Leads"], rows, [3.0 * inch, 0.8 * inch])

    story.append(Spacer(1, 10))
    story.append(Paragraph(
        "Leads are counted by the day they arrived. A lead belongs to the platform that first brought it (click id, UTM or call source); leads with no campaign data are "
        "shown as organic, direct or unknown rather than hidden. Speed to lead is the median time from arrival to the first logged call, text or email. Follow-up compliance is the share of leads "
        "either closed or touched at least as often as the follow-up schedule asks. Spend comes from the ad platforms nightly." if show_spend else
        "Leads are counted by the day they arrived. A lead belongs to the platform that first brought it (click id, UTM or call source). Speed to lead is the median time from arrival to the first "
        "logged call, text or email. Follow-up compliance is the share of leads either closed or touched at least as often as the follow-up schedule asks.", small))

    def footer(canvas, doc_):
        canvas.saveState()
        canvas.setFillColor(hivis)
        canvas.rect(0.8 * inch, letter[1] - 0.45 * inch, 0.25 * inch, 0.1 * inch, stroke=0, fill=1)
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(ink2)
        canvas.drawString(0.8 * inch, 0.45 * inch, f"{agency_name} · {tenant['name']} · {s.period.label}")
        canvas.drawRightString(letter[0] - 0.8 * inch, 0.45 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def months_available(conn: psycopg.Connection, tenant_id: UUID, tz_name: str) -> list[tuple[int, int, str]]:
    """(year, month, label) for every month with a lead, newest first, plus the current one."""
    tz = ZoneInfo(tz_name)
    first = conn.execute("SELECT min(created_at) AS m FROM lead WHERE tenant_id = %s", (tenant_id,)).fetchone()["m"]
    today = datetime.now(UTC).astimezone(tz).date()
    start = (first.astimezone(tz).date() if first else today).replace(day=1)
    out = []
    y, m = today.year, today.month
    while (y, m) >= (start.year, start.month) and len(out) < 24:
        out.append((y, m, f"{calendar.month_name[m]} {y}"))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out
