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

# Phrases in a listing's own notes that admit the posting is probably already
# closed/expired — reject these rather than publish a known-stale listing.
STALE_NOTES_RE = re.compile(
    r"likely closed|already closed|appears closed|no longer accepting|"
    r"application (?:window|deadline) (?:has )?passed|posting (?:may be|is) expired",
    re.IGNORECASE,
)

PROMPT = f"""You are a highly selective Product Management career-opportunity research agent
building a daily public job tracker for a university Product Management club. Today's date is
{TODAY}. You run TWO separate, independently-searched tracks:

TRACK 1 — SUMMER 2027 PRODUCT MANAGEMENT INTERNSHIPS
Real, currently-open PM internships intended for Summer 2027 (or clearly Summer-2027-equivalent
timing: May/June/July/August 2027 start). Do not include Summer 2026 or earlier internships.

TRACK 2 — FULL-TIME PRODUCT MANAGEMENT ROLES WITH A MAY 1 - AUGUST 31, 2027 START
Do NOT simply list currently-open PM jobs — most experienced-hire PM postings want someone
immediately, and those must be excluded from this track. Only include full-time roles where the
posting gives a real signal of 2027 graduate/MBA timing: explicit 2027 start month, "Class of 2027",
"2027 graduates", MBA/graduate leadership-development or rotational programs recruiting for 2027,
university/campus recruiting for 2027, or an explicit flexible-start policy tied to 2027 graduation.
If a full-time posting reads like an ordinary immediate-hire role with no 2027-cycle signal, exclude it.

The goal is relevance and verification quality over sheer quantity. A handful of well-verified,
correctly-linked listings is far better than a long list with weak or duplicate links.

SEARCH BREADTH: use your web_search tool across official company career pages, university/early-career
recruiting pages, ATS platforms (Greenhouse, Lever, Workday, Ashby, SmartRecruiters), and job boards
(LinkedIn, Indeed, Glassdoor, Handshake, Built In, Wellfound, WayUp). Run enough distinct, varied
searches (company-specific and keyword-based) to responsibly cover both tracks across multiple
industries — do not stop after one or two generic queries.

HARD LINK-QUALITY RULE (this is the most important rule — read twice):
Every listing's `source` must be the specific, direct, unique application page for THAT exact job —
never a search-results page, a category/browse page, an aggregator's generic listing page, or any
link that two different listings could plausibly share. If you cannot find a specific direct link for
a listing, DO NOT include that listing at all. Never reuse the same source URL for more than one
listing in your output — if you only have a generic page for a role, omit the role instead.

ACTIVE-JOB VERIFICATION: do not include a listing if your search results give you reason to believe
the application window has already closed or the posting is stale/expired. When in doubt about
whether a specific posting is still open, prefer excluding it over including a possibly-dead listing.

NO-HALLUCINATION RULE: only report a detail (work authorization, salary, start date, work mode) if
the source explicitly states it — use the literal string "Not specified" otherwise. Never guess,
infer, or estimate. This does NOT mean you should drop a listing for having several "Not specified"
fields — partial information is normal and expected. The only hard requirements to include a listing
are (1) it's a real, currently-open, correctly-tracked opportunity per the track definitions above,
and (2) you have a real, unique, direct source URL for it. Missing detail fields are never a reason
to exclude a listing or to refuse to respond.

For each listing, produce an object with EXACTLY these keys:
- role (string)
- company (string)
- location (string)
- type (one of exactly: "Internship", "Full-time", "Rotational/Development Program", "Unclear")
- workMode (one of exactly: "Remote", "Hybrid", "On-site", "Not specified")
- workAuth (one of exactly: "Sponsorship available", "OPT/international possible", "Not specified", "No sponsorship")
- workAuthDetail (string — quote/paraphrase what the source said, or "Not specified")
- salary (string, or "Not specified")
- industry (string, e.g. "Fintech", "Healthcare", "Technology", "Insurance", "Industrial")
- startDate (string, or "Not specified")
- endDate (string, "N/A" for full-time with no fixed end, or "Not specified")
- startDateFit (one of exactly: "Confirmed Summer 2027", "Strong 2027 signal", "Flexible/Potential", "Not specified")
- priority (one of exactly: "High priority", "Good fit", "Watch" — your own judgment of how strong a fit this is for the 2027 tracks defined above)
- notes (string, can be empty "")
- source (a real, specific, direct, unique URL you found)
- dateFound ("{TODAY}")

Respond with ONLY a raw JSON array of these objects — no markdown code fences, no commentary before
or after, no explanation, no apology. Just the JSON array, starting with [ and ending with ]. If your
searches turned up fewer usable listings than you'd like, output as many as you genuinely verified
(even if that's only 6-10) — never pad with anything invented, and never refuse or return prose
instead of data. If you truly found zero usable listings, respond with exactly [] and nothing else.
"""


def _extract_json_array(full_text):
    """Pull a JSON array out of the model's response, as leniently as possible.

    Never raises — if nothing parseable is found, prints a warning and returns
    an empty list so the caller's "too few listings" safety check can abort
    the run cleanly instead of crashing with an ugly traceback.
    """
    try:
        parsed = json.loads(full_text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    start = full_text.find("[")
    end = full_text.rfind("]")
    if start != -1 and end != -1 and end > start:
        candidate = full_text[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    print(
        "WARNING: model did not return a parseable JSON array. "
        f"First 1000 chars of its response:\n{full_text[:1000]}",
        file=sys.stderr,
    )
    return []


def _is_valid_source(url):
    if not url or not isinstance(url, str):
        return False
    if url.strip().lower() in ("not specified", ""):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if BAD_LINK_RE.search(url):
        return False
    return True


def get_jobs():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=12000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 30}],
        messages=[{"role": "user", "content": PROMPT}],
    )

    text_parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    full_text = "\n".join(text_parts).strip()

    data = _extract_json_array(full_text)

    # Pass 1: basic schema cleanup + reject invalid/generic/missing sources.
    candidates = []
    for job in data:
        if not isinstance(job, dict):
            continue
        row = {}
        for key in SCHEMA_KEYS:
            val = job.get(key)
            row[key] = val if val not in (None, "") else "Not specified"

        if row["dateFound"] in ("Not specified", ""):
            row["dateFound"] = TODAY

        if row["type"] not in TYPE_VALUES:
            row["type"] = "Unclear"
        if row["workAuth"] not in WORK_AUTH_VALUES:
            row["workAuth"] = "Not specified"
        if row["startDateFit"] not in START_FIT_VALUES:
            row["startDateFit"] = "Not specified"
        if row["priority"] not in PRIORITY_VALUES:
            row["priority"] = "Watch"

        if not _is_valid_source(row["source"]):
            continue  # no usable direct link — drop per the hard link-quality rule
        if STALE_NOTES_RE.search(row.get("notes", "")):
            continue  # listing admits it's probably already closed

        candidates.append(row)

    # Pass 2: drop any source URL reused across more than one listing — this is
    # the exact signature of the "generic page masquerading as a direct link"
    # bug (several unrelated jobs all citing the same aggregator page).
    url_counts = {}
    for row in candidates:
        url_counts[row["source"]] = url_counts.get(row["source"], 0) + 1

    cleaned = [row for row in candidates if url_counts[row["source"]] == 1]
    dropped_dupes = len(candidates) - len(cleaned)
    if dropped_dupes:
        print(f"Dropped {dropped_dupes} listing(s) sharing a reused source URL.", file=sys.stderr)

    return cleaned


def update_html(jobs, path="index.html"):
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    jobs_json = json.dumps(jobs, indent=2)
    new_jobs_block = "const jobs = " + jobs_json + ";"

    jobs_pattern = re.compile(r"const jobs = \[.*?\];(?=\s*function badge)", re.DOTALL)
    if not jobs_pattern.search(html):
        raise ValueError("Could not find `const jobs = [...]` block in index.html — template may have changed.")
    html = jobs_pattern.sub(lambda _m: new_jobs_block, html, count=1)

    now_str = datetime.now(timezone.utc).strftime("%B %d, %Y, %H:%M UTC")
    updated_line = (
        "document.getElementById('updated-line').textContent = "
        "'Last updated: " + now_str + " · Refreshes daily via GitHub Actions · ' + jobs.length + ' listings tracked';"
    )
    updated_pattern = re.compile(r"document\.getElementById\('updated-line'\)\.textContent = .*?;", re.DOTALL)
    if not updated_pattern.search(html):
        raise ValueError("Could not find the updated-line text assignment in index.html.")
    html = updated_pattern.sub(lambda _m: updated_line, html, count=1)

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    jobs = get_jobs()
    print(f"Found {len(jobs)} listings with verified unique sources.")
    if len(jobs) < 5:
        print("Too few valid listings found — aborting without publishing to avoid a broken/empty page.", file=sys.stderr)
        sys.exit(1)
    update_html(jobs)
    print(f"index.html updated with {len(jobs)} jobs.")
