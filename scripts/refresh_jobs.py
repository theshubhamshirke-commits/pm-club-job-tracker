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
from html.parser import HTMLParser
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
Use the exact source heading for role. The automated verifier requires a 2027 role heading
and a readable Apply control. For location, salary, workMode, startDate, endDate,
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

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'noscript'):
            self.ignored += 1
        if self.ignored:
            return
        if tag == 'h1':
            self.heading = []
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


def _posting_matches(html_text, job):
    """Require positive, matching evidence. A 200 response alone is not enough.

    JavaScript-only pages with no rendered Apply control fail closed. This may
    omit real openings; it must never turn an inaccessible page into evidence.
    """
    page = _PostingText()
    page.feed(html_text)
    visible = ' '.join(page.parts)
    if CLOSED_SIGNAL_RE.search(visible) or _structured_data_says_expired(html_text):
        return False
    if _deadline_already_passed(visible):
        return False
    role = _normalized(job['role'])
    if role not in [_normalized(h) for h in page.headings]:
        return False
    if _normalized(job['company']) not in _normalized(visible):
        return False
    if not re.search(r'product (?:manager|management|intern)', role):
        return False
    if '2027' not in role:
        return False
    if ('intern' in role) != (job['type'] == 'Internship'):
        return False
    return any(re.fullmatch(r'apply(?: now| for (?:this )?(?:job|position))?',
                            _normalized(c)) for c in page.controls)


def _ground_optional_details(job, html_text):
    """Withhold unsupported model details instead of publishing plausible guesses.

    Literal excerpts are intentionally conservative: paraphrases can be correct,
    but this check cannot prove that they are. Editorial category/priority labels
    are separate from the employer's facts.
    """
    page = _PostingText()
    page.feed(html_text)
    source = _normalized(' '.join(page.parts))
    for field in ('location', 'salary', 'workMode', 'startDate', 'endDate',
                  'workAuthDetail', 'notes'):
        value = job[field]
        if value in ('Not specified', 'N/A'):
            continue
        if _normalized(value) not in source:
            job[field] = 'Not specified'
            print(f"  withheld unsupported {field}: {job['company']} / {job['role']}", file=sys.stderr)
    if job['workAuth'] != 'Not specified':
        # Do not derive immigration eligibility from silence or export controls.
        if job['workAuthDetail'] == 'Not specified':
            job['workAuth'] = 'Not specified'
        elif job['workAuth'] == 'No sponsorship':
            if not re.search(r'(?:not|cannot|unable to) sponsor|no sponsorship',
                             job['workAuthDetail'], re.I):
                job['workAuth'] = 'Not specified'
        elif _normalized(job['workAuth']) not in source:
            job['workAuth'] = 'Not specified'


def _link_is_live(job):
    """Verify the exact role and Apply control, then withhold unsupported details."""
    url = job['source']
    try:
        resp = requests.get(
            url, headers=LIVE_CHECK_HEADERS, timeout=LIVE_CHECK_TIMEOUT, allow_redirects=True
        )
    except requests.RequestException as e:
        print(f"  could not verify {url} ({e}) — excluded as unknown", file=sys.stderr)
        return False

    if resp.status_code != 200:
        return False

    if not _is_valid_source(resp.url):
        return False  # bounced from the specific job page to a generic listing page

    if not _posting_matches(resp.text, job):
        return False
    _ground_optional_details(job, resp.text)
    return True


def _research_jobs(client):
    """Stream a complete research turn, resuming bounded server-tool pauses."""
    # web_fetch lets the model actually retrieve each candidate's real current
    # page content (not just a cached search snippet) before including it —
    # this is what catches postings that look fine in search results but have
    # since closed, especially on JS-rendered ATS sites (Workday, Google
    # careers) where the "no longer available" message only shows up after
    # the page actually loads. Requires the web-fetch beta header + the
    # client.beta.messages namespace rather than the plain client.messages one.
    messages = [{"role": "user", "content": PROMPT}]
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
            print(f"  tool result ({btype}): {'ERROR' if is_error else 'ok'} — {str(content)[:300]}", file=sys.stderr)


def get_jobs():
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is missing. Set it in the repository's Actions secrets.")
    with anthropic.Anthropic(api_key=api_key, max_retries=2) as client:
        resp = _research_jobs(client)

    text_parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    full_text = "\n".join(text_parts).strip()
    print(f"Final text output from model (first 500 chars): {full_text[:500]!r}", file=sys.stderr)

    data = _extract_json_array(full_text)
    print(f"Model returned {len(data)} raw candidate(s) before any filtering.", file=sys.stderr)

    # Pass 1: basic schema cleanup + reject invalid/generic/missing sources.
    # Every rejection path below prints *why*, so a run that drops to zero
    # candidates is diagnosable straight from the log instead of a bare count.
    drop_counts = {
        "not_dict": 0,
        "bad_source": 0,
        "notes_say_closed": 0,
        "deadline_passed": 0,
        "link_dead": 0,
    }
    candidates = []
    for job in data:
        if not isinstance(job, dict):
            drop_counts["not_dict"] += 1
            continue
        row = {}
        for key in SCHEMA_KEYS:
            val = job.get(key)
            row[key] = val.strip() if isinstance(val, str) and val.strip() else "Not specified"

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
            drop_counts["bad_source"] += 1
            print(f"  dropping (invalid/generic/missing link): {row['source']!r} ({row.get('role')!r} @ {row.get('company')!r})", file=sys.stderr)
            continue
        if CLOSED_SIGNAL_RE.search(row.get("notes", "")):
            drop_counts["notes_say_closed"] += 1
            print(f"  dropping (notes admit closed): {row['source']} — notes: {row.get('notes')!r}", file=sys.stderr)
            continue
        if _deadline_already_passed(row.get("notes", "")) or _deadline_already_passed(row.get("startDate", "")):
            drop_counts["deadline_passed"] += 1
            print(f"  dropping (stated deadline already passed): {row['source']}", file=sys.stderr)
            continue
        if not _link_is_live(row):
            drop_counts["link_dead"] += 1
            print(f"  dropping (posting could not be positively verified): {row['source']}", file=sys.stderr)
            continue

        candidates.append(row)

    print(f"After per-listing filters: {len(candidates)} remain. Drop reasons: {drop_counts}", file=sys.stderr)

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

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def main(path="index.html"):
    jobs = get_jobs()
    print(f"Found {len(jobs)} listings with verified unique sources.")
    if not jobs:
        print("No verified listings found — preserving the last successful page and timestamp.", file=sys.stderr)
        return 1
    update_html(jobs, path=path)
    print(f"index.html updated with {len(jobs)} jobs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
