"""Report a total discovery outage after verification and publication finish.

This command reads the existing report only; it never runs research or checks jobs.
Partial discovery remains a warning in refresh_jobs.py, not a failed workflow.
"""
import json
from pathlib import Path
import sys


def main(report_path):
    try:
        report = json.loads(Path(report_path).read_text())
        batches = report.get('batches') if isinstance(report, dict) else None
        if not isinstance(batches, list) or not batches or any(
            not isinstance(batch, dict)
            or batch.get('status') not in ('complete', 'partial', 'failed')
            for batch in batches
        ):
            raise ValueError('Missing or invalid discovery batches')
    except (OSError, ValueError) as error:
        print(f'::error::Cannot assess discovery health: {error}')
        return 1

    if all(batch['status'] == 'failed' for batch in batches):
        print('::error::Every new-job discovery pass failed. Existing-job verification '
              'and publication are separate. Inspect data/refresh-report.json for '
              'the API or discovery errors; a published page does not mean searches ran.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else 'data/refresh-report.json'))
