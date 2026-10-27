"""
Daily PM Club Job Tracker refresh script.

Calls the Anthropic API with the native web_search tool to research current
Product Manager internships (Summer 2027) and full-time PM roles (May-Aug 2027
start), then rewrites the `jobs` array and "last updated" line inside
index.html in place.

Requires: pip install -r requirements.txt
Env var:  ANTHROPIC_API_KEY
"""

import os
import re
import json
import sys
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from datetime import datetime, timezone
from urllib.parse import urlparse, urlsplit, urlunsplit, parse_qsl, urlencode

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
MAX_PAUSE_CONTINUATIONS = 3

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

LIVE_CHECK_TIMEOUT = 30
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
        for node in _iter_jobposting_nodes(html_text):
            valid_through = node.get("validThrough")
            if not valid_through or not isinstance(valid_through, str):
                continue
            try:
                dt = date_parser.parse(valid_through)
            except (ValueError, OverflowError, TypeError):
                continue
            if dt.date() < today:
                return True
    except Exception as e:  # pragma: no cover - never let this crash the run
        print(f"  structured-data check error (ignored): {e}", file=sys.stderr)
    return False

# Catches phrasing like "close Oct 6, 2026", "closes on October 6, 2026",
# "deadline: 10/6/2026", "apply by Oct. 6, 2026" inside a listing's own notes
# or startDate field, so we can compare the stated deadline against today's
# date directly — this catches JS-rendered sites (e.g. Google careers) whose
# "this posting has closed" banner a plain HTTP fetch can't see, because it
# relies on the date the model already extracted rather than re-fetching.
DEADLINE_PHRASE_RE = re.compile(
    r"(?:clos(?:e|es|ed|ing)(?:\s+(?:on|date))?|deadline(?:\s*:)?|apply\s+by|applications?\s+(?:are\s+)?due)"
    r"[^.\n]{0,40}?"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?"
    r"|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)


def _deadline_already_passed(text):
    """Return True if `text` mentions an explicit close/deadline date and that
    date is strictly before today (UTC). Never raises — unparseable dates are
    treated as no signal (kept, not dropped)."""
    if not text or not isinstance(text, str):
        return False
    today = datetime.now(timezone.utc).date()
    for match in DEADLINE_PHRASE_RE.finditer(text):
        try:
            dt = date_parser.parse(match.group(1), fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            continue
        if dt.date() < today:
            return True
    return False

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

Only include roles located in the United States. Use the employer's own job description or
its official ATS posting as the source; job boards are for discovery only.
Use the exact source heading for role. The 2027 timing may appear in the title OR the
job description. A matching employer posting must still accept applications.
For location, salary, workMode, startDate, endDate,
workAuthDetail and notes, use short verbatim source excerpts or "Not specified".
Do not infer remote/on-site work from an office location, calculate precise dates from a
program duration, or borrow dates from a different internship at the same company.
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

MANDATORY VERIFICATION FETCH (do this for every single candidate before it goes in your output):
Your web_search results can be stale — a page can be indexed one day and closed, filled, or taken
down the next, and many ATS sites (Workday, and Google's own careers site) render their "this posting
has closed" or "page not found" message dynamically, so a search snippet alone can look fine even when
the live page no longer is. Therefore: for every candidate listing, use your web_fetch tool to actually
retrieve that exact `source` URL and read its real current content before finalizing the listing.
  - If the fetched page clearly shows the specific role is open and accepting applications, keep it.
  - If the fetched page says the posting is closed, filled, expired, no longer accepting applications,
    or says the page/job cannot be found, DROP that listing entirely — do not include it.
  - If the fetched content doesn't actually describe this specific role (e.g. it redirected to a
    generic "all open jobs" page, a generic company homepage, or an unrelated listing), DROP it.
  - If the fetch fails outright (network error, blocked, timeout) treat that as inconclusive, not as
    proof it's open — prefer dropping a listing you couldn't actually verify over guessing it's fine.
This fetch step is not optional and is more important than finding a large number of listings — a
shorter list where every single entry has been freshly fetched and confirmed open beats a longer list
based only on search snippets.

BE TERSE WHILE WORKING: do not narrate your process in prose ("Now let me search for...", "I can see
that..."). Your output budget is limited and must be spent on actual searching, fetching, and the final
JSON — not on describing what you're about to do. Call your tools silently with minimal or no
commentary between calls. The only prose you should ever produce is the final JSON array itself.

ACTIVE-JOB VERIFICATION: do not include a listing if your search results OR your verification fetch
give you reason to believe the application window has already closed or the posting is stale/expired.
When in doubt about whether a specific posting is still open, prefer excluding it over including a
possibly-dead listing. If a posting states an explicit application open/close date (e.g. "Applications
open Sep 22 – close Oct 6, 2026"), always include that exact date in the `notes` field in a recognizable
form — this lets a downstream automated check cross-reference the stated deadline against today's date
as a second, independent layer of verification on top of your own fetch.

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

    Malformed output is a failed discovery pass, never an empty successful search.
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

    raise ValueError("Research returned malformed data instead of a complete JSON array")


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
    if parsed.username or parsed.password or parsed.path.rstrip('/') in ('', '/careers', '/jobs'):
        return False
    return True


class _PostingText(HTMLParser):
    """Read server-rendered text and controls, excluding scripts and styles."""

    def __init__(self):
        super().__init__()
        self.parts, self.headings, self.controls = [], [], []
        self.ignored = 0
        self.heading = None
        self.control = None
        self.heading_positions = []
        self.heading_start = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.ignored += 1
        if self.ignored:
            return
        if tag == 'h1':
            self.heading = []
            self.heading_start = len(self.parts)
        if tag in ('a', 'button'):
            attributes = dict(attrs)
            if 'disabled' not in attributes and attributes.get('aria-disabled') != 'true':
                self.control = []

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'noscript'):
            self.ignored = max(0, self.ignored - 1)
            return
        if self.ignored:
            return
        if tag == 'h1' and self.heading is not None:
            self.headings.append(' '.join(self.heading))
            self.heading_positions.append((self.headings[-1], self.heading_start))
            self.heading = None
        if tag in ('a', 'button') and self.control is not None:
            self.controls.append(' '.join(self.control))
            self.control = None

    def handle_data(self, data):
        if self.ignored:
            return
        self.parts.append(data)
        if self.heading is not None:
            self.heading.append(data)
        if self.control is not None:
            self.control.append(data)


def _normalized(text):
    return ' '.join(re.findall(r'\w+', text.casefold()))


# Positive evidence must belong to the job, not a footer or related opening.
RELATED_RE = re.compile(r'\b(?:related jobs|similar jobs|related content|recommended jobs|privacy policy|equal opportunity employer)\b', re.I)
PM_ROLE_RE = re.compile(r'\bproduct\s+(?:manager|management|intern)\b', re.I)
INTERN_RE = re.compile(r'\bintern(?:ship|s)?\b', re.I)
SUMMER_RE = re.compile(r'\bsummer[\s\-]+2027\b|\b2027\s+summer\b|\b(?:May|June|July|August)[^.!?\n]{0,30}\b2027\b', re.I)
GRAD_RE = re.compile(
    r'\b(?:class\s+of|cohort\s+of)\s+2027\b|'
    r'\b2027\s+(?:APM|RPM|cohort|graduates?|graduate|new[ -]grads?|start)\b|'
    r'\b(?:graduat(?:e|es|ing|ion)|cohort|start(?:ing|s|\s+date)?|begin(?:s|ning)?)\b[^.!?\n]{0,90}\b2027\b|'
    r'\b2027\b[^.!?\n]{0,35}\b(?:graduates?|graduation|cohort|start)\b', re.I)
US_RE = re.compile(r'\b(?:United States(?: of America)?|USA|U\.S\.|US|California|Washington|Virginia|Illinois|New York|Texas|Massachusetts|North Carolina|Colorado|Michigan|Minnesota|Wisconsin|Pennsylvania|Ohio|Florida|New Jersey|Oregon|Utah|Arizona|Maryland|Tennessee|Indiana|Connecticut|Missouri|Nebraska|Nevada|Kansas|Kentucky|Georgia)\b|,\s*(?:CA|WA|VA|IL|NY|TX|MA|NC|CO|MI|MN|WI|PA|OH|FL|NJ|OR|UT|AZ|MD|TN|IN|CT|MO|NE|NV|KS|KY|GA)\b')


def _plain_text(html_text):
    page = _PostingText()
    page.feed(html_text)
    return ' '.join(page.parts)


def _primary_text(html_text, role):
    page = _PostingText()
    page.feed(html_text)
    for heading, start in page.heading_positions:
        if _normalized(heading) == _normalized(role):
            return RELATED_RE.split(' '.join(page.parts[start:]), maxsplit=1)[0], page
    return '', page


def _scope_reason(job, description, country=''):
    role = job['role']
    if not PM_ROLE_RE.search(role):
        return 'Role is not product management'
    intern = bool(INTERN_RE.search(role))
    if intern != (job['type'] == 'Internship'):
        return 'Role title does not match the selected track'
    if job['type'] not in TYPE_VALUES - {'Unclear'}:
        return 'Employment track is not established'
    # Do not mistake a 2026 internship for the student's 2027 graduation date.
    years_in_title = set(re.findall(r'\b20\d{2}\b', role))
    if years_in_title and '2027' not in years_in_title:
        return 'Role title names a different recruiting year'
    evidence = role + '\n' + description
    if intern and not SUMMER_RE.search(evidence):
        return 'No Summer 2027 internship timing in the posting'
    if not intern and not GRAD_RE.search(evidence):
        return 'No 2027 graduate/cohort/start signal in the posting'
    if not intern and re.search(r'\b(?:start(?:s|ing|\s+date)?|begin(?:s|ning)?)\b[^.!?\n]{0,35}\b(?:January|February|March|April|September|October|November|December|Fall|Autumn|Winter)\b[^.!?\n]{0,15}2027', description, re.I):
        return 'Explicit start falls outside May-August 2027'
    if country and _normalized(country) not in ('us', 'usa', 'united states', 'united states of america'):
        return 'Posting is outside the United States'
    if not country and not US_RE.search(description):
        return 'US location could not be established from the source'
    return ''


def _source_is_employer(job, url):
    """Reject discovery boards; accept matching employer domains or ATS tenants."""
    parsed = urlparse(url)
    company = re.sub(r'[^a-z0-9]', '', job['company'].casefold())
    if not company or company == 'notspecified':
        return False
    host = (parsed.hostname or '').lower()
    is_ats = any(host == d or host.endswith('.' + d) for d in
                 ('myworkdayjobs.com', 'greenhouse.io', 'lever.co', 'ashbyhq.com', 'smartrecruiters.com', 'icims.com'))
    identity = host + (parsed.path if is_ats else '')
    return company in re.sub(r'[^a-z0-9]', '', identity)


def _posting_country(html_text, role):
    """Use location metadata belonging to this exact role, when supplied."""
    countries = []
    for node in _iter_jobposting_nodes(html_text):
        if _normalized(str(node.get('title', ''))) != _normalized(role):
            continue
        locations = node.get('jobLocation', [])
        for location in locations if isinstance(locations, list) else [locations]:
            if not isinstance(location, dict):
                continue
            address = location.get('address', {})
            if not isinstance(address, dict):
                continue
            country = address.get('addressCountry', '')
            if isinstance(country, dict):
                country = country.get('name', '')
            if isinstance(country, str) and country:
                countries.append(country)
    for country in countries:
        if _normalized(country) in ('us', 'usa', 'united states', 'united states of america'):
            return country
    return countries[0] if countries else ''


def _posting_matches(html_text, job):
    """Readable matching employer heading, an Apply control, and track evidence."""
    primary, page = _primary_text(html_text, job['role'])
    if not primary:
        return False
    if CLOSED_SIGNAL_RE.search(primary) or _structured_data_says_expired(html_text):
        return False
    if _deadline_already_passed(primary) or _scope_reason(job, primary):
        return False
    if _normalized(job['company']) not in _normalized(' '.join(page.parts)):
        return False
    return any(re.fullmatch(r'apply(?: now| for (?:this )?(?:job|position))?',
                            _normalized(c)) for c in page.controls)


def _ground_optional_details(job, source_text):
    """Keep literal source facts; withhold summaries this check cannot prove."""
    source = _normalized(source_text)
    for field in ('location', 'salary', 'workMode', 'startDate', 'endDate',
                  'workAuthDetail', 'notes'):
        value = job[field]
        if value not in ('Not specified', 'N/A') and _normalized(value) not in source:
            job[field] = 'Not specified'
    if job['workAuth'] != 'Not specified':
        if job['workAuthDetail'] == 'Not specified':
            job['workAuth'] = 'Not specified'
        elif job['workAuth'] == 'No sponsorship':
            if not re.search(r'(?:not|cannot|unable to) sponsor|no sponsorship', job['workAuthDetail'], re.I):
                job['workAuth'] = 'Not specified'
        elif _normalized(job['workAuth']) not in source:
            job['workAuth'] = 'Not specified'
    job['startDateFit'] = 'Confirmed Summer 2027' if job['type'] == 'Internship' else 'Strong 2027 signal'


def _workday_posting(url, html_text):
    """Read Workday's public job payload, including posted/canApply flags.

    The tenant/site are taken from the employer page's app configuration. No
    authentication or application submission is involved. Missing flags fail closed.
    """
    parsed = urlparse(url)
    if not (parsed.hostname or '').endswith('.myworkdayjobs.com'):
        return None
    tenant = re.search(r'\btenant:\s*"([\w-]+)"', html_text)
    site = re.search(r'\bsiteId:\s*"([\w-]+)"', html_text)
    if not tenant or not site or '/job/' not in parsed.path:
        return None
    job_path = '/job/' + parsed.path.split('/job/', 1)[1].split('/apply', 1)[0]
    endpoint = f'{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant[1]}/{site[1]}{job_path}'
    response = requests.get(endpoint, headers=LIVE_CHECK_HEADERS, timeout=LIVE_CHECK_TIMEOUT)
    if response.status_code in (404, 410):
        return {'status': 'closed', 'reason': f'Workday job endpoint returned {response.status_code}'}
    if response.status_code != 200:
        return {'status': 'unverified', 'reason': f'Workday job endpoint returned {response.status_code}'}
    try:
        info = response.json().get('jobPostingInfo', {})
    except (ValueError, AttributeError):
        return {'status': 'unverified', 'reason': 'Workday returned no readable job payload'}
    if not info or not info.get('title'):
        return {'status': 'unverified', 'reason': 'Workday returned no matching job'}
    if info.get('canApply') is False or info.get('posted') is False:
        return {'status': 'closed', 'reason': 'Workday reports applications unavailable'}
    if info.get('canApply') is not True or info.get('posted') is not True:
        return {'status': 'unverified', 'reason': 'Workday application status is unknown'}
    return {'status': 'open', 'title': info['title'],
            'description': _plain_text(info.get('jobDescription', '')),
            'country': info.get('country', {}).get('descriptor', ''),
            'location': '; '.join([info.get('location', '')] + info.get('additionalLocations', [])),
            'reason': 'Matching Workday posting is posted and accepts applications'}


def _verify_job(job, previous=None):
    """Return an auditable status; unknown is never relabeled as closed or open."""
    checked = datetime.now(timezone.utc).isoformat()
    result = {'job': dict(job), 'status': 'unverified', 'reason': '', 'checkedAt': checked}
    def finish(status, reason):
        result.update(status=status, reason=reason)
        return result
    url = job['source']
    if not _is_valid_source(url) or not _source_is_employer(job, url):
        return finish('unverified', 'Source is not a specific recognized employer/ATS posting')
    try:
        response = requests.get(url, headers=LIVE_CHECK_HEADERS, timeout=LIVE_CHECK_TIMEOUT, allow_redirects=True)
        if response.status_code in (404, 410):
            return finish('closed', f'Employer page returned {response.status_code}')
        if response.status_code != 200:
            return finish('unverified', f'Employer page returned HTTP {response.status_code}')
        if not _is_valid_source(response.url) or not _source_is_employer(job, response.url):
            return finish('unverified', 'Source redirected away from a matching employer posting')
        primary, page = _primary_text(response.text, job['role'])
        visible = primary or ' '.join(page.parts)
        if CLOSED_SIGNAL_RE.search(visible) or _structured_data_says_expired(response.text):
            return finish('closed', 'Employer page reports the posting closed or expired')
        if _deadline_already_passed(visible):
            return finish('closed', 'The stated application deadline has passed')
        workday = _workday_posting(response.url, response.text)
        country = _posting_country(response.text, job['role'])
        if workday:
            if workday['status'] != 'open':
                return finish(workday['status'], workday['reason'])
            if _normalized(workday['title']) != _normalized(job['role']):
                return finish('unverified', 'Workday title does not match this role')
            primary = workday['description']
            country = workday['country']
            reason = workday['reason']
        elif not primary:
            return finish('unverified', 'Matching job heading is not readable')
        elif not any(re.fullmatch(r'apply(?: now| for (?:this )?(?:job|position))?', _normalized(c)) for c in page.controls):
            return finish('unverified', 'No readable enabled Apply control')
        else:
            reason = 'Matching employer heading and Apply control'
        if CLOSED_SIGNAL_RE.search(primary) or _deadline_already_passed(primary):
            return finish('closed', 'Job description reports closure or an elapsed deadline')
        scope = _scope_reason(job, primary, country)
        if scope:
            return finish('out_of_scope', scope)
        fingerprint = hashlib.sha256(_normalized(primary).encode()).hexdigest()
        result['sourceFingerprint'] = fingerprint
        # A retained row whose source changed must be grounded again. Unchanged
        # reviewed facts need not be replaced with a new model interpretation.
        if not previous or previous.get('sourceFingerprint') != fingerprint:
            _ground_optional_details(result['job'], primary)
        if workday:
            result['job']['location'] = workday['location'] or 'Not specified'
        result['job']['lastChecked'] = checked
        result['finalUrl'] = response.url
        return finish('open', reason)
    except requests.RequestException as error:
        return finish('unverified', f'Employer page could not be reached ({type(error).__name__})')


def _link_is_live(job):
    # Compatibility helper for a single posting; the refresh uses full records.
    result = _verify_job(job)
    if result['status'] == 'open':
        job.update(result['job'])
        return True
    return False


def _research_jobs(client, prompt=None, telemetry=None):
    """Stream a complete research turn, resuming bounded server-tool pauses."""
    # web_fetch lets the model actually retrieve each candidate's real current
    # page content (not just a cached search snippet) before including it —
    # this is what catches postings that look fine in search results but have
    # since closed, especially on JS-rendered ATS sites (Workday, Google
    # careers) where the "no longer available" message only shows up after
    # the page actually loads. Requires the web-fetch beta header + the
    # client.beta.messages namespace rather than the plain client.messages one.
    messages = [{"role": "user", "content": prompt or PROMPT}]
    tools = [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 30},
        {
            "type": "web_fetch_20250910",
            "name": "web_fetch",
            "max_uses": 25,
            "max_content_tokens": 8000,
        },
    ]
    for attempt in range(MAX_PAUSE_CONTINUATIONS + 1):
        # A 48k output budget requires streaming in the SDK. The helper still
        # returns a complete message, so filtering never sees partial events.
        with client.beta.messages.stream(
            model="claude-sonnet-4-5",
            max_tokens=48000,
            tools=tools,
            betas=["web-fetch-2025-09-10"],
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()

        _log_research_response(resp)
        if telemetry is not None:
            for block in resp.content:
                if getattr(block, 'type', '') == 'server_tool_use':
                    key = 'queries' if block.name == 'web_search' else 'fetchedUrls'
                    value = getattr(block, 'input', {}).get('query' if key == 'queries' else 'url')
                    if value and value not in telemetry.setdefault(key, []):
                        telemetry[key].append(value)
                elif getattr(block, 'type', '') in ('web_search_tool_result', 'web_fetch_tool_result'):
                    content = getattr(block, 'content', None)
                    kind = content.get('type', '') if isinstance(content, dict) else getattr(content, 'type', '')
                    if kind.endswith('_error'):
                        telemetry.setdefault('toolErrors', []).append(kind)
        if resp.stop_reason == "end_turn":
            return resp
        if resp.stop_reason != "pause_turn":
            raise RuntimeError(
                f"Research did not finish (stop_reason={resp.stop_reason!r}); "
                "keeping the previously published listings."
            )
        if attempt == MAX_PAUSE_CONTINUATIONS:
            raise RuntimeError(
                "Research exceeded the server-tool continuation limit; "
                "keeping the previously published listings."
            )
        # Resume the same assistant turn with every tool block intact. Do not
        # replace this with a new user prompt or parse its unfinished text.
        messages.append({"role": "assistant", "content": resp.content})
        print(f"Resuming paused research ({attempt + 1}/{MAX_PAUSE_CONTINUATIONS}).", file=sys.stderr)


def _log_research_response(resp):
    # Diagnostic: confirm whether web_fetch (and web_search) actually ran, and
    # whether any fetch calls errored out server-side. Without this, a run
    # that returns zero candidates is ambiguous — it could mean "genuinely
    # found nothing that survived verification" or "the fetch tool itself is
    # broken/inaccessible and every candidate got dropped as unverifiable."
    from collections import Counter
    block_type_counts = Counter(getattr(b, "type", None) for b in resp.content)
    print(f"stop_reason={resp.stop_reason!r}; content block types: {dict(block_type_counts)}", file=sys.stderr)
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype in ("server_tool_use",) and getattr(block, "name", None) in ("web_fetch", "web_search"):
            print(f"  tool call: {block.name} input={getattr(block, 'input', None)}", file=sys.stderr)
        elif btype in ("web_fetch_tool_result", "web_search_tool_result"):
            content = getattr(block, "content", None)
            result_type = content.get("type") if isinstance(content, dict) else getattr(content, "type", None)
            is_error = result_type in ("web_fetch_tool_result_error", "web_search_tool_result_error")
            print(f"  tool result ({btype}): {'ERROR' if is_error else 'ok'}", file=sys.stderr)


def _clean_job(job):
    if not isinstance(job, dict):
        return None
    row = {key: value.strip() if isinstance(value := job.get(key), str) and value.strip()
           else 'Not specified' for key in SCHEMA_KEYS}
    if row['role'] == 'Not specified' or row['company'] == 'Not specified' or not _is_valid_source(row['source']):
        return None
    if row['type'] not in TYPE_VALUES:
        row['type'] = 'Unclear'
    if row['workAuth'] not in WORK_AUTH_VALUES:
        row['workAuth'] = 'Not specified'
    if row['startDateFit'] not in START_FIT_VALUES:
        row['startDateFit'] = 'Not specified'
    if row['priority'] not in PRIORITY_VALUES:
        row['priority'] = 'Watch'
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', row['dateFound']):
        row['dateFound'] = TODAY
    return row


def _result_jobs(resp):
    full_text = '\n'.join(block.text for block in resp.content if getattr(block, 'type', '') == 'text')
    rows = _extract_json_array(full_text)
    return [clean for row in rows if (clean := _clean_job(row)) is not None]


def get_jobs():
    """Single-pass discovery helper; publication always verifies these candidates."""
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY is missing. Set it in the repository Actions secrets.')
    with anthropic.Anthropic(api_key=api_key, max_retries=2) as client:
        return _result_jobs(_research_jobs(client))


def discover_jobs(plan_path):
    """Run independent searches; one track cannot consume the other's budget."""
    plan = json.loads(Path(plan_path).read_text())
    candidates, batches = [], []
    api_key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not api_key:
        return [], [{'name': 'discovery', 'status': 'failed', 'error': 'ANTHROPIC_API_KEY is missing'}]
    with anthropic.Anthropic(api_key=api_key, max_retries=2) as client:
        for batch in plan['batches']:
            stats = {'name': batch['name'], 'queries': [], 'fetchedUrls': [], 'toolErrors': []}
            prompt = PROMPT + '\nTHIS PASS ONLY: ' + batch['focus'] + '\n'
            prompt += ('Execute each of the following exact search queries before finalizing. '
                       'Then follow promising official postings and search further as needed. '
                       'Return every verified candidate you can find within the tool budget; '
                       'there is no five-job or twenty-job limit. Do not return unrelated roles '\
                       'to reach a target count.\n' + '\n'.join(batch['queries']))
            try:
                response = _research_jobs(client, prompt=prompt, telemetry=stats)
                found = _result_jobs(response)
                candidates.extend(found)
                actual = {_normalized(q) for q in stats['queries']}
                stats['missingQueries'] = [q for q in batch['queries'] if _normalized(q) not in actual]
                stats['status'] = 'complete' if not stats['missingQueries'] and not stats['toolErrors'] else 'partial'
                stats['candidateCount'] = len(found)
            except Exception as error:
                # A broken discovery pass must not prevent existing-job checks.
                stats.update(status='failed', error=f'{type(error).__name__}: {str(error)[:300]}')
            batches.append(stats)
            print(f"Discovery pass {batch['name']}: {stats['status']} ({stats.get('candidateCount', 0)} candidates)")
    return candidates, batches


def _job_key(job):
    """Collapse common tracking/ATS aliases without merging distinct requisitions."""
    parts = urlsplit(job['source'])
    host = (parts.hostname or '').lower().removeprefix('www.')
    company = _normalized(job['company'])
    if host.endswith('myworkdayjobs.com'):
        match = re.search(r'_(R[A-Z0-9]+|JR\d+|REF\w+)(?:-\d+)?(?:/|$)', parts.path, re.I)
        if match:
            return company + ':workday:' + match[1].lower()
    if host.endswith('databricks.com'):
        match = re.search(r'-(\d{8,})/?$', parts.path)
        if match:
            return company + ':databricks:' + match[1]
    if host.endswith('amazon.jobs'):
        match = re.search(r'/jobs/(\d+)', parts.path)
        if match:
            return company + ':amazon:' + match[1]
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith('utm_') and k.lower() not in ('source', 'src', 'ref', 'referrer')]
    return urlunsplit(('https', host, parts.path.rstrip('/'), urlencode(sorted(query)), ''))


def _load_page_jobs(path):
    text = Path(path).read_text()
    match = re.search(r'const jobs\s*=\s*', text)
    if not match:
        raise ValueError('Existing page has no readable jobs array')
    data, _ = json.JSONDecoder().raw_decode(text[match.end():])
    if not isinstance(data, list):
        raise ValueError('Existing jobs data is not an array')
    return [clean for row in data if (clean := _clean_job(row)) is not None]


def _check_many(items):
    def check(item):
        job, previous = item
        try:
            return _verify_job(job, previous)
        except Exception as error:
            return {'job': job, 'status': 'unverified', 'reason': f'Verification error ({type(error).__name__})',
                    'checkedAt': datetime.now(timezone.utc).isoformat()}
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(check, items))


def refresh(path='index.html', data_dir=None, discovery=discover_jobs):
    """Recheck the entire published list first, then add verified discoveries."""
    data_dir = Path(data_dir) if data_dir else Path(path).resolve().parent / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / 'tracked-jobs.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'jobs': []}
    records = {_job_key(record['job']): record for record in state['jobs']}
    published = _load_page_jobs(path)
    published_keys = {_job_key(row) for row in published}
    existing = {key: record['job'] for key, record in records.items() if record['status'] in ('open', 'unverified')}
    existing.update({_job_key(row): row for row in published})
    # Recheck published AND temporarily unverified jobs even when discovery fails.
    checked = _check_many([(row, records.get(key)) for key, row in existing.items()])
    refreshed = {_job_key(record['job']): record for record in checked}
    checked_existing_count = len(checked)
    try:
        discovered, batches = discovery(data_dir / 'search-plan.json')
    except Exception as error:
        discovered, batches = [], [{'name': 'discovery', 'status': 'failed', 'error': type(error).__name__}]
    # Seeds recover old known URLs once, without declaring them open or trusting
    # their historical fields. Closed seeds are not re-requested every day.
    seeds_path = data_dir / 'discovery-seeds.json'
    seeds = json.loads(seeds_path.read_text()) if seeds_path.exists() else []
    new = {}
    for raw, seed in [(row, False) for row in discovered] + [(row, True) for row in seeds]:
        row = _clean_job(raw)
        if not row:
            continue
        key = _job_key(row)
        if key in refreshed or (seed and key in records):
            continue
        if key in records:
            row['dateFound'] = records[key]['job']['dateFound']
        new.setdefault(key, row)
    new_checks = _check_many([(row, records.get(key)) for key, row in new.items()])
    refreshed.update({_job_key(record['job']): record for record in new_checks})
    records.update(refreshed)
    open_records = [r for r in refreshed.values() if r['status'] == 'open']
    jobs = [r['job'] for r in open_records]
    jobs.sort(key=lambda j: (j['type'] != 'Internship', j['company'].casefold(), j['role'].casefold()))
    complete = bool(batches) and all(b['status'] == 'complete' for b in batches)
    now = datetime.now(timezone.utc).isoformat()
    from collections import Counter
    report = {'checkedAt': now, 'discoveryComplete': complete, 'batches': batches,
              'publishedCount': len(jobs), 'previousPublishedCount': len(published),
              'existingRechecked': checked_existing_count, 'discoveredCandidates': len(discovered),
              'newCandidatesChecked': len(new_checks),
              'addedCount': sum(_job_key(j) not in published_keys for j in jobs),
              'statusCounts': dict(Counter(r['status'] for r in refreshed.values())),
              'checks': [{k: v for k, v in r.items() if k != 'sourceFingerprint'} for r in refreshed.values()]}
    # Empty is an honest result of this check, not proof that no jobs exist.
    # Unknown entries stay in state for retries, never in the open-jobs table.
    update_html(jobs, path=path, report=report)
    for target, value in ((state_path, {'version': 1, 'updatedAt': now, 'jobs': list(records.values())}),
                          (data_dir / 'refresh-report.json', report)):
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(value, indent=2) + '\n')
        temp.replace(target)
    if not complete:
        print('::warning::New-job discovery was incomplete; existing postings were still rechecked. See data/refresh-report.json.')
    print(f"Published {len(jobs)} verified openings; rechecked {checked_existing_count} known postings; added {report['addedCount']}.")
    return report


def update_html(jobs, path="index.html", report=None):
    with open(path, "r", encoding="utf-8") as f:
        html = f.read()

    jobs_json = json.dumps(jobs, indent=2).replace('<', '\\u003c')
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
    if report is not None:
        summary = {key: report[key] for key in ('checkedAt', 'discoveryComplete', 'statusCounts')}
        status = 'const refreshStatus = ' + json.dumps(summary).replace('<', '\\u003c') + ';'
        pattern = re.compile(r'const refreshStatus = .*?;(?=\s*const jobs)', re.DOTALL)
        if pattern.search(html):
            html = pattern.sub(lambda _: status, html, count=1)
        else:
            html = html.replace('const jobs = ', status + '\nconst jobs = ', 1)
    temp = Path(path).with_suffix('.tmp')
    temp.write_text(html, encoding='utf-8')
    temp.replace(path)


def main(path="index.html"):
    refresh(path=path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
