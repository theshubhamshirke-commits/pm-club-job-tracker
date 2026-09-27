"""
Daily PM Club Job Tracker refresh script.

Calls the Anthropic API with the native web_search tool to research current
Product Manager jobs/internships in the USA, then rewrites the `jobs` array
and "last updated" line inside index.html in place.

Requires: pip install anthropic
Env var:  ANTHROPIC_API_KEY
"""

import os
import re
import json
import sys
from datetime import datetime, timezone

import anthropic

SCHEMA_KEYS = [
    "role", "company", "location", "type", "visa", "visaCategory",
    "salary", "industry", "startDate", "endDate", "notes", "source", "dateFound",
]

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

PROMPT = f"""You are researching CURRENTLY OPEN Product Manager jobs and internships in the USA
for a university Product Management club's public job tracker. Use your web_search tool to find
real, currently open listings — both internships and full-time roles — from a mix of sources:

- Major aggregators: LinkedIn, Indeed, Glassdoor, Handshake, WayUp, Built In
- Company career pages directly (tech, fintech, healthcare, industrial, retail, insurance, consumer goods — vary the industries, don't just do big tech)
- PM-specific boards: APMList.org, Product School's job board, intern-list.com, ProductHQ

Run enough distinct searches to gather roughly 20-30 real listings, mixing internship and full-time roles
and covering multiple industries.

STRICT NO-HALLUCINATION RULE: for every listing, only report a detail (visa sponsorship, salary,
start date, end date) if the source you found explicitly states it. If a detail is not explicitly
stated, use the literal string "Not specified" for that field — never guess, infer, or estimate.
Do not invent a source URL; only include a listing if you have a real link to it from your search results.

For each listing, produce an object with EXACTLY these keys:
- role (string)
- company (string)
- location (string)
- type ("Internship" or "Full-time")
- visa (string — quote/paraphrase what the source said about visa sponsorship, or "Not specified")
- visaCategory (one of exactly: "Not offered", "Unclear / case-by-case", "Not specified")
- salary (string, or "Not specified")
- industry (string, e.g. "Fintech", "Healthcare", "Technology", "Insurance", "Industrial", etc.)
- startDate (string, or "Not specified")
- endDate (string, or "Not specified" or "N/A" for full-time roles with no fixed end)
- notes (string, can be empty "")
- source (a real URL you found)
- dateFound ("{TODAY}")

Respond with ONLY a raw JSON array of these objects — no markdown code fences, no commentary
before or after, no explanation. Just the JSON array, starting with [ and ending with ].
"""


def get_jobs():
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 25}],
        messages=[{"role": "user", "content": PROMPT}],
    )

    text_parts = [block.text for block in resp.content if getattr(block, "type", None) == "text"]
    full_text = "\n".join(text_parts).strip()

    match = re.search(r"\[\s*\{.*\}\s*\]", full_text, re.DOTALL)
    if not match:
        raise ValueError(f"Could not find a JSON array in model output:\n{full_text[:2000]}")

    data = json.loads(match.group(0))

    cleaned = []
    for job in data:
        row = {}
        for key in SCHEMA_KEYS:
            row[key] = job.get(key) or "Not specified"
        if row["dateFound"] in ("Not specified", ""):
            row["dateFound"] = TODAY
        if not row["source"] or row["source"] == "Not specified":
            continue  # skip anything without a real source link
        cleaned.append(row)
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
    print(f"Found {len(jobs)} listings with real sources.")
    if len(jobs) < 5:
        print("Too few valid listings found — aborting without publishing to avoid a broken/empty page.", file=sys.stderr)
        sys.exit(1)
    update_html(jobs)
    print(f"index.html updated with {len(jobs)} jobs.")
