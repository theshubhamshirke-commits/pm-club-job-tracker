# PM Club Job Tracker

The static page is `index.html`. GitHub Actions runs `scripts/refresh_jobs.py`
daily at 13:07 UTC and can also be started from the Actions tab.

## Setup and checks

Use Python 3.11 or later:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Tests use the real Anthropic SDK with simulated streaming responses. They make
no external requests and require no API key. Direct dependencies are pinned;
test any dependency update before publishing it.

For a live refresh, configure the `ANTHROPIC_API_KEY` Actions secret, or set it
locally, then run `python scripts/refresh_jobs.py`. Live runs use Anthropic API
credits. Never commit a key to the repository.

## Daily behavior and coverage

Each run rechecks every published opening and previously unverified candidate,
then searches three independent passes: technology internships, internships in
other industries, and 2027 graduate full-time/rotational PM roles. Each pass has
eight required queries in `data/search-plan.json`, up to 30 search and 25 fetch
uses, and at most three paused-turn continuations. This increases API usage over
the former single search pass; it is bounded and does not guarantee exhaustive
market coverage or a particular number of jobs.

Full-time roles still require explicit 2027 graduate/cohort/start evidence in
the matching description or title. Immediate experienced-hire roles do not qualify.
The target is May-August 2027; an explicit start outside that window is excluded.
A 2027 graduate cohort with no stated month is labeled a strong 2027 signal, not
an exact confirmed summer start. Internships require Summer 2027 timing. Both
tracks are United States only.

Source checks require an exact matching PM title plus an enabled Apply control,
or a matching public Workday payload with both `posted` and `canApply` true.
Closed or expired postings are removed. Unreachable or inconclusive sources are
hidden and saved for another check; they are not labeled closed. Description
checks exclude related-job/footer text and use job location metadata when present.
Optional facts are retained only when grounded in source text, or when previously
reviewed facts have an unchanged source fingerprint. Industry and priority are
editorial labels. These checks are conservative and may omit genuine openings.

`data/tracked-jobs.json` persists verification state. `data/refresh-report.json`
records each check, exclusion reason, discovery queries and pass status. Historical
URLs in `data/discovery-seeds.json` are candidates only; each must pass fresh checks.
A source check is a snapshot, not a guarantee that the opening stays available.

Streaming supports the 48,000-token output budget. Truncated, refused or malformed
research output fails that discovery pass. Existing openings are still rechecked;
only freshly verified rows publish. A partially completed search is shown on the
page and in the report. If no source can be verified, the page displays an honest
empty result, while unknown candidates remain queued for retry. A completely
failed workflow leaves its previous page timestamp; the page warns when checks
are over 36 hours old.

Offline regression tests run before research. Overlapping refreshes are serialized
and each workflow stops after 45 minutes. Passing tests does not establish API
credit, source reachability, complete discovery or successful live deployment.

The daily workflow explicitly requests a Pages rebuild after pushing because
commits made with `GITHUB_TOKEN` do not automatically build branch-based Pages.
This uses the built-in token with `pages: write`; no additional secret is needed.
Keep Pages configured for `main` / repository root. Check the separate Pages
deployment run as well as the refresh job before treating an update as live.

The manually reviewed five-listing snapshot and corrections are recorded in
`data/manual-review-2026-10-27.json`. That report describes that snapshot only;
subsequent daily refreshes can change the list.

The current `claude-sonnet-4-5` model is retained by this repair. The installed
SDK reports its retirement on November 30, 2026; validate a replacement before
then. The UTC schedule changes local Eastern time with daylight saving time.
