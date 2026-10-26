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
or unfinished responses fail without replacing the existing listings. The
existing minimum of five valid listings is also retained. Inspect the failed
Actions step and the page's last-updated timestamp when a refresh fails.

The refresh workflow runs offline tests before research, serializes overlapping
runs, and stops after 45 minutes. Passing offline tests does not establish that
the API account has credit, that all source sites are reachable, or that a live
refresh has completed. The HTTP link check still treats inaccessible sites as
unknown, and the model's extracted listing details still require source review.

The current `claude-sonnet-4-5` model is retained by this repair. The installed
SDK reports its retirement on November 30, 2026; validate a replacement before
then. The UTC schedule changes local Eastern time with daylight saving time.
