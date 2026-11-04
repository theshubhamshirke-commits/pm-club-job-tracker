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
        self.assertIn('::error::Discovery passes failed or missed required searches', result.stdout)

    def test_one_failed_track_fails_despite_successful_other_tracks_and_publication(self):
        for other_status in ('complete', 'partial'):
            for error in ('Your credit balance is too low', 'Malformed research JSON'):
                with self.subTest(other_status=other_status, error=error):
                    result = self.check_report(json.dumps({
                        'publishedCount': 15, 'existingRechecked': 84,
                        'batches': [
                            {'name': 'internships-technology', 'status': other_status, 'candidateCount': 6},
                            {'name': 'internships-other-industries', 'status': other_status, 'candidateCount': 13},
                            {'name': 'graduate-full-time', 'status': 'failed', 'queries': [], 'error': error},
                        ],
                    }))
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn('graduate-full-time', result.stdout)
                    self.assertIn('Existing-job verification and publication are separate', result.stdout)

    def test_missing_required_searches_fail_even_without_a_failed_status(self):
        for status in ('complete', 'partial'):
            with self.subTest(status=status):
                result = self.check_report(json.dumps({
                    'publishedCount': 15,
                    'batches': [{'name': 'graduate-full-time', 'status': status,
                                 'candidateCount': 20,
                                 'missingQueries': ['product manager 2027 graduates']}],
                }))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('missed required searches: graduate-full-time', result.stdout)

    def test_partial_coverage_and_legitimate_zero_results_do_not_fail(self):
        for statuses in [('complete',) * 3, ('partial',) * 3,
                         ('partial', 'complete', 'complete')]:
            with self.subTest(statuses=statuses):
                result = self.check_report(json.dumps({
                    'publishedCount': 0,
                    'batches': [{'status': status, 'candidateCount': 0, 'missingQueries': [],
                                 'toolErrors': ['web_fetch_tool_result_error'] if status == 'partial' else []}
                                for status in statuses],
                }))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_unreadable_reports_cannot_pass(self):
        for content in ('not JSON', 'null', '{}', '{"batches":[]}',
                        '{"batches":[{}]}', '{"batches":[{"status":"unknown"}]}',
                        '{"batches":[{"status":"partial","missingQueries":"invalid"}]}'):
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
