"""Offline CLI regression checks for discovery outages."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check_discovery_health.py'


class DiscoveryHealthTests(unittest.TestCase):
    def check_report(self, content):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report.json'
            report.write_text(content)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(report)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(report.read_text(), content)
            return result

    def test_total_api_outage_fails_even_when_verified_jobs_were_published(self):
        report = {
            'publishedCount': 11, 'existingRechecked': 44, 'discoveryComplete': False,
            'batches': [
                {'name': name, 'status': 'failed', 'queries': [],
                 'error': 'Your credit balance is too low to access the Anthropic API.'}
                for name in ('internships-technology', 'internships-other-industries',
                             'graduate-full-time')
            ],
        }
        result = self.check_report(json.dumps(report))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('::error::Every new-job discovery pass failed', result.stdout)

    def test_partial_coverage_and_legitimate_zero_results_do_not_fail(self):
        for statuses in [('complete',) * 3, ('partial',) * 3,
                         ('partial', 'failed', 'complete')]:
            with self.subTest(statuses=statuses):
                result = self.check_report(json.dumps({
                    'publishedCount': 0,
                    'batches': [{'status': status, 'candidateCount': 0}
                                for status in statuses],
                }))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_unreadable_reports_cannot_pass(self):
        for content in ('not JSON', 'null', '{}', '{"batches":[]}',
                        '{"batches":[{}]}', '{"batches":[{"status":"unknown"}]}'):
            with self.subTest(content=content):
                result = self.check_report(content)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('::error::Cannot assess discovery health', result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), str(Path(directory) / 'missing.json')],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn('::error::Cannot assess discovery health', result.stdout)


if __name__ == '__main__':
    unittest.main()
