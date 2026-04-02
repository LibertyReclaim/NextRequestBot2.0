"""Configuration and environment loading for NextRequest bot."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class RequesterProfile:
    """Contact profile used to fill request forms."""

    full_name: str
    email: str
    phone: str
    address: str
    city: str
    state: str
    zip_code: str
    company: str
    organization: str


REQUEST_TEMPLATE = """Subject: California Public Records Act Request – Unclaimed Property Records

To Whom It May Concern,

Pursuant to the California Public Records Act (Government Code §6250 et seq.), I am requesting access to records related to any unclaimed property, uncashed checks, stale warrants, or other funds currently held by the {municipality}

Specifically, I am requesting:

1. A copy of the current unclaimed property list maintained by the City, including but not limited to uncashed checks, vendor payments, refunds, deposits, or other outstanding obligations.
2. If available, I request the records in a downloadable electronic format such as CSV, Excel, or other machine-readable file.
3. Any documentation describing the process required for an individual or business to claim these funds.
4. Any policies, rules, or requirements that apply to third parties assisting claimants in recovering funds, including whether registration, licensing, contracts, notarization, or other authorization is required.
5. Any forms, instructions, or guidelines used by the City for submitting claims.

If any portion of these records is available online, please provide the direct link.
"""


DEPARTMENT_PRIORITY = ["controller", "tax", "finance", "clerk"]
REQUIRED_COLUMNS = ["municipality", "state", "portal_url"]
TRACKING_COLUMNS = [
    "status",
    "submitted_at",
    "request_number",
    "failed",
    "records_received",
    "notes",
    "screenshot_path",
]

INPUT_XLSX = Path("municipalities_input.xlsx")
OUTPUT_XLSX = Path("municipalities_tracking.xlsx")
SCREENSHOT_DIR = Path("screenshots")

ACCOUNT_PASSWORD = "April1518@"


def _env(name: str, fallback: str = "") -> str:
    value = os.getenv(name, fallback).strip()
    return value


def get_requester_profile() -> RequesterProfile:
    """Load requester profile from .env with hardcoded fallbacks."""
    return RequesterProfile(
        full_name=_env("REQUESTER_FULL_NAME", "Nathan Garcia"),
        email=_env("REQUESTER_EMAIL", "n.garcia@libertyreclaim.org"),
        phone=_env("REQUESTER_PHONE", "951-526-7367"),
        address=_env("REQUESTER_ADDRESS", "29288 Stirling"),
        city=_env("REQUESTER_CITY", "Lake Elsinore"),
        state=_env("REQUESTER_STATE", "CA"),
        zip_code=_env("REQUESTER_ZIP", "92530"),
        company=_env("REQUESTER_COMPANY", "Liberty Reclaim"),
        organization=_env("REQUESTER_ORGANIZATION", "Liberty Reclaim"),
    )


def get_openai_api_key() -> str:
    """Return OpenAI API key (required for field classification)."""
    return _env("OPENAI_API_KEY")
