"""
Daily PM Club Job Tracker refresh script.

Calls the Anthropic API with the native web_search tool to research current
Product Manager internships (Summer 2027) and full-time PM roles (May-Aug 2027
start), then rewrites the `jobs` array and "last updated" line inside
index.html in place.

Requires: pip install anthropic
Env var:  ANTHROPIC_API_KEY
"""

import os
import re
import json
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

import anthropic

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
