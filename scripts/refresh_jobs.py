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
    r"job has been closed|this position is no longer available|closed to
