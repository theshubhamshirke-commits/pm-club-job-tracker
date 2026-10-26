"""
Daily PM Club Job Tracker refresh script.

Calls the Anthropic API with the native web_search tool to research current
Product Manager internships (Summer 2027) and full-time PM roles (May-Aug 2027
start), then rewrites the `jobs` array and "last updated" line inside
index.html in place.

Requires: pip install anthropic requests python-dateutil
Env var:  ANTHROPIC_API_KEY
"""

import os
import re
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import anthropic
import requests
from dateutil import parser as date_parser

SCHEMA_KEYS = [
    "role", "company", "location", "type", "workMode",
    "workAuth", "workAuthDetail", "salary", "industry",
    "startDate", "endDate", "startDateFit", "priority",
    "notes", "source", "dateFound",
]

TYPE_VALUES = {"Internship", "Full-time", "Rotational/Development Program", "Unclear"}
WORK_AUTH_VALUES = {"Sponsorship available", "OPT/international possible", "Not specified", "No sponsorship"}
START_FIT_VALUES = {"Confirmed Summer 2027", "Strong 2027 signal", "Flexible/Potential", "Not specified"}
PRIORITY_VALUES = {"High priority", "Good fit", "Watch"}

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# URL patterns that are generic search/listing/aggregator pages rather than a
# specific job posting. A source matching any of these is rejected outright,
# even if the model includes it — this is the exact bug pattern that caused
# "link redirects to something else": several unrelated jobs all citing the
# same generic search-results page as their "source".
BAD_LINK_PATTERNS = [
    r"indeed\.com/q-",
    r"indeed\.com/jobs\?",
    r"glassdoor\.com/Job/[\w-]+-jobs-SRCH",
    r"linkedin\.com/jobs/search",
    r"linkedin\.com/jobs/collections",
    r"github\.com/[\w-]+/.*Product-Management-Internship",
    r"intern-list\.com/pm-intern-list/?$",
    r"wellfound\.com/jobs/?$",
    r"ziprecruiter\.com/candidate/search",
    r"builtin\.com/jobs/?$",
]
BAD_LINK_RE = re.compile("|".join(BAD_LINK_PATTERNS), re.IGNORECASE)

# Phrases that signal a posting is dead — checked both against the model's own
# "notes" field and against the live-fetched text of the posting's own page.
CLOSED_SIGNAL_RE = re.compile(
    r"no longer accepting applications|position has been filled|"
    r"this (?:job|position|posting|requisition)s? (?:has|have) (?:expired|been filled|been closed|closed)|"
    r"(?:job|position|posting|requisition) (?:is|was) (?:no longer available|closed|expired|filled)|"
    r"applications? (?:are|is) (?:now )?closed|this requisition has been closed|"
    r"job has been closed|this position is no longer available|closed to new applicants|"
    r"page not found|job not found|we(?:'| a)re sorry.{0,60}no longer (?:active|available)|"
    r"likely closed|already closed|appears closed|no longer accepting|"
    r"application (?:window|deadline) (?:has )?passed|posting (?:may be|is) expired",
    re.IGNORECASE,
)

LIVE_CHECK_TIMEOUT = 10
LIVE_CHECK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 PMJobTrackerBot"
}

# Most ATS platforms (including Google's own careers site and Workday) embed
# schema.org JobPosting structured data as a <script type="application/ld+json">
# block for SEO / "Google for Jobs" indexing purposes. Crucially, this block is
# written server-side into the raw HTML even on sites whose visible "this
# posting has closed" banner only renders client-side via JavaScript after the
# page loads — so it's visible to a plain HTTP fetch when the human-facing
# banner is not. If present, its `validThrough` field is a much more reliable
# deadline signal than scraping visible text.
LD_JSON_BLOCK_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def _iter_jobposting_nodes(html_text):
    """Yield dict nodes that look like schema.org JobPosting objects found in
    any embedded JSON-LD block on the page. Tolerant of a bare object, an
    array of objects, or an `@graph`-wrapped collection; never raises on
    malformed/partial JSON (ATS pages often emit slightly-off JSON-LD)."""
    for block in LD_JSON_BLOCK_RE.findall(html_text):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue

        if isinstance(data, list):
            nodes = data
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            nodes = data["@graph"]
        elif isinstance(data, dict):
            nodes = [data]
        else:
            nodes = []

        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if any(isinstance(t, str) and t.lower() == "jobposting" for t in types):
                yield node


def _structured_data_says_expired(html_text):
    """Return True if an embedded JobPosting's `validThrough` date has already
    passed. Defensive by design: any parsing failure is swallowed and treated
    as no signal (kept, not dropped) rather than risking a false drop."""
    today = datetime.now(timezone.utc).date()
    try:
        for node in
