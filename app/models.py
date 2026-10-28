"""Enumerations and small value types shared across the app. Mirrors the Postgres enums."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    AGENCY_ADMIN = "agency_admin"
    OWNER = "owner"
    MEMBER = "member"


class LeadStatus(StrEnum):
    NEW = "new"
    CONTACTED = "contacted"
    NO_RESPONSE = "no_response"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]

    @property
    def is_closed(self) -> bool:
        return self in (LeadStatus.WON, LeadStatus.LOST)


STATUS_LABELS = {
    LeadStatus.NEW: "New",
    LeadStatus.CONTACTED: "Contacted",
    LeadStatus.NO_RESPONSE: "No response",
    LeadStatus.QUOTED: "Quoted",
    LeadStatus.WON: "Won",
    LeadStatus.LOST: "Lost",
}


class Channel(StrEnum):
    WEBSITE_FORM = "website_form"
    CALL = "call"
    META_LEAD_AD = "meta_lead_ad"
    GOOGLE_LEAD_FORM = "google_lead_form"
    INBOUND_TEXT = "inbound_text"
    MANUAL = "manual"
    IMPORT = "import"


CHANNEL_LABELS = {
    Channel.WEBSITE_FORM: "Website form",
    Channel.CALL: "Phone call",
    Channel.META_LEAD_AD: "Meta lead form",
    Channel.GOOGLE_LEAD_FORM: "Google lead form",
    Channel.INBOUND_TEXT: "Text message",
    Channel.MANUAL: "Added manually",
    Channel.IMPORT: "Imported",
}


class Platform(StrEnum):
    GOOGLE_ADS = "google_ads"
    META_ADS = "meta_ads"
    ORGANIC = "organic"
    REFERRAL = "referral"
    DIRECT = "direct"
    UNKNOWN = "unknown"


PLATFORM_LABELS = {
    Platform.GOOGLE_ADS: "Google Ads",
    Platform.META_ADS: "Meta Ads",
    Platform.ORGANIC: "Organic",
    Platform.REFERRAL: "Referral",
    Platform.DIRECT: "Direct",
    Platform.UNKNOWN: "Unknown",
}


class AreaStatus(StrEnum):
    IN = "in"
    NEAR = "near"
    OUT = "out"
    UNKNOWN = "unknown"


class ActivityType(StrEnum):
    CREATED = "created"
    CALL_ATTEMPT = "call_attempt"
    TEXT_SENT = "text_sent"
    EMAIL_SENT = "email_sent"
    LEAD_REPLIED = "lead_replied"
    NOTE = "note"
    STATUS_CHANGE = "status_change"
    ASSIGNMENT = "assignment"
    AUTO_MESSAGE = "auto_message"
    REMINDER_SENT = "reminder_sent"
    DUPLICATE_SUBMISSION = "duplicate_submission"
    CONVERSION_SENT = "conversion_sent"
    IMPORT = "import"
    APPOINTMENT = "appointment"
    ESCALATION = "escalation"
    OUTCOME = "outcome"


# Logging any of these on a New lead moves it to Contacted automatically.
TOUCH_TYPES = (ActivityType.CALL_ATTEMPT, ActivityType.TEXT_SENT, ActivityType.EMAIL_SENT)

ACTIVITY_LABELS = {
    ActivityType.CREATED: "Lead received",
    ActivityType.CALL_ATTEMPT: "Called",
    ActivityType.TEXT_SENT: "Texted",
    ActivityType.EMAIL_SENT: "Emailed",
    ActivityType.LEAD_REPLIED: "Lead replied",
    ActivityType.NOTE: "Note",
    ActivityType.STATUS_CHANGE: "Status changed",
    ActivityType.ASSIGNMENT: "Assigned",
    ActivityType.AUTO_MESSAGE: "Automatic message",
    ActivityType.REMINDER_SENT: "Reminder sent",
    ActivityType.DUPLICATE_SUBMISSION: "Submitted again",
    ActivityType.CONVERSION_SENT: "Sent to ad platform",
    ActivityType.IMPORT: "Imported",
    ActivityType.APPOINTMENT: "Appointment",
    ActivityType.ESCALATION: "Escalated to agency",
    ActivityType.OUTCOME: "Outcome",
}


class LostReason(StrEnum):
    PRICE = "price"
    TIMING = "timing"
    OUT_OF_AREA = "out_of_area"
    WENT_ELSEWHERE = "went_elsewhere"
    NO_RESPONSE = "no_response"
    SPAM = "spam"
    OTHER = "other"


LOST_REASON_LABELS = {
    LostReason.PRICE: "Price",
    LostReason.TIMING: "Timing",
    LostReason.OUT_OF_AREA: "Out of service area",
    LostReason.WENT_ELSEWHERE: "Went with someone else",
    LostReason.NO_RESPONSE: "Never responded",
    LostReason.SPAM: "Spam / not a real lead",
    LostReason.OTHER: "Other",
}
