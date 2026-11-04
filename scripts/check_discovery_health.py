"""Report failed discovery passes or missing searches after publication is requested.

This command reads the existing report only; it never runs research or checks jobs.
Partial coverage from source-fetch errors alone remains a warning.
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
            or not isinstance(batch.get('missingQueries', []), list)
            for batch in batches
        ):
            raise ValueError('Missing or invalid discovery batches')
    except (OSError, ValueError) as error:
        print(f'::error::Cannot assess discovery health: {error}')
        return 1

    unhealthy = [str(batch.get('name', 'unnamed pass')) for batch in batches
                 if batch['status'] == 'failed' or batch.get('missingQueries')]
    if unhealthy:
        print('::error::Discovery passes failed or missed required searches: '
              + ', '.join(unhealthy) + '. Existing-job verification '
              'and publication are separate. Inspect data/refresh-report.json for '
              'the API or discovery errors; a published page does not mean searches ran.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else 'data/refresh-report.json'))
