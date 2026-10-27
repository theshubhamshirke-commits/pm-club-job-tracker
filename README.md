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

## Failure behavior

Research uses streaming to support its 48,000-token output budget. Paused
server-tool turns can resume up to three times; truncated, refused, disconnected,
or unfinished responses fail without replacing the existing listings. Any
nonempty verified result can publish; there is no quota to pad with weak listings.
Zero results preserve the previous page and its timestamp. Inspect the failed
Actions step and the page's last-updated timestamp when a refresh fails.

The refresh workflow runs offline tests before research, serializes overlapping
runs, and stops after 45 minutes. Passing offline tests does not establish that
the API account has credit, that all source sites are reachable, or that a live
refresh has completed.

The source check now fails closed on HTTP errors, timeouts, generic redirects,
mismatched role titles, missing Apply controls, expired deadlines, and closed
postings. It requires a matching PM heading containing 2027 on a readable page;
this conservative rule omits some genuine openings, including JavaScript-only
pages. Unknown is not evidence that a job is open. Optional factual details are
withheld unless their text appears on the fetched page. These checks reduce
unsupported output; they do not guarantee future availability or interpret every
employer's eligibility terms. Industry and priority are editorial tracker labels.

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
