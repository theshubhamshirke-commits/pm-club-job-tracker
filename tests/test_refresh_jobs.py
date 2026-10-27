"""Exercise the real SDK with offline SSE responses; no API key or paid calls."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import anthropic
import httpx2 as httpx
import requests

from scripts import refresh_jobs as tracker


def response_events(text, stop_reason="end_turn", paused_tool=False):
    events = [{
        "type": "message_start",
        "message": {
            "id": "msg_offline", "type": "message", "role": "assistant",
            "model": "claude-sonnet-4-5", "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0},
        },
    }]
    if paused_tool:
        events.extend([
            {"type": "content_block_start", "index": 0, "content_block": {
                "type": "server_tool_use", "id": "srvtoolu_offline",
                "name": "web_search", "input": {},
            }},
            {"type": "content_block_delta", "index": 0, "delta": {
                "type": "input_json_delta", "partial_json": '{"query":"PM internships 2027"}',
            }},
            {"type": "content_block_stop", "index": 0},
        ])
    else:
        events.append({"type": "content_block_start", "index": 0,
                       "content_block": {"type": "text", "text": ""}})
        halfway = len(text) // 2
        for part in (text[:halfway], text[halfway:]):
            events.append({"type": "content_block_delta", "index": 0,
                           "delta": {"type": "text_delta", "text": part}})
        events.append({"type": "content_block_stop", "index": 0})
    events.extend([
        {"type": "message_delta", "delta": {
            "stop_reason": stop_reason, "stop_sequence": None,
        }, "usage": {"output_tokens": 20}},
        {"type": "message_stop"},
    ])
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


def fake_client(responses, requests):
    responses = iter(responses)

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=next(responses))

    return anthropic.Anthropic(
        api_key="offline-test-key", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    )


def sample_jobs():
    return [dict.fromkeys(tracker.SCHEMA_KEYS, "Not specified") | {
        "role": "Product Management Intern", "company": f"Test Company {i}",
        "type": "Internship", "startDateFit": "Confirmed Summer 2027",
        "priority": "Good fit", "source": f"https://example.com/careers/job/{i}",
        "dateFound": tracker.TODAY,
    } for i in range(5)]


class ResearchTests(unittest.TestCase):
    def test_large_request_streams_and_accumulates_json(self):
        calls = []
        with fake_client([response_events('[{"role":"PM intern"}]')], calls) as client:
            result = tracker._research_jobs(client)
        self.assertEqual(json.loads(result.content[0].text), [{"role": "PM intern"}])
        self.assertTrue(calls[0]["stream"])
        self.assertEqual(calls[0]["max_tokens"], 48000)

    def test_paused_tool_turn_resumes_with_its_state(self):
        calls = []
        responses = [response_events("", "pause_turn", paused_tool=True), response_events("[]")]
        with fake_client(responses, calls) as client:
            result = tracker._research_jobs(client)
        self.assertEqual(result.stop_reason, "end_turn")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["tools"], calls[0]["tools"])
        previous = calls[1]["messages"][-1]
        self.assertEqual(previous["role"], "assistant")
        self.assertEqual(previous["content"][0]["id"], "srvtoolu_offline")
        self.assertEqual(previous["content"][0]["input"], {"query": "PM internships 2027"})

    def test_repeated_pauses_have_a_finite_limit(self):
        calls = []
        responses = [response_events("", "pause_turn", paused_tool=True)] * 4
        with fake_client(responses, calls) as client:
            with self.assertRaisesRegex(RuntimeError, "continuation limit"):
                tracker._research_jobs(client)
        self.assertEqual(len(calls), 4)

    def test_incomplete_or_refused_turn_cannot_publish_valid_looking_json(self):
        for reason in ("max_tokens", "refusal", "tool_use"):
            with self.subTest(reason=reason), fake_client(
                [response_events(json.dumps(sample_jobs()), reason)], []
            ) as client:
                with self.assertRaisesRegex(RuntimeError, "did not finish"):
                    tracker._research_jobs(client)

    def test_stream_disconnect_is_not_interpreted_as_an_empty_search(self):
        calls = []
        incomplete = response_events("[]").split("event: message_delta")[0]
        with fake_client([incomplete], calls) as client:
            with self.assertRaises((RuntimeError, AssertionError)):
                tracker._research_jobs(client)

    def test_missing_key_has_a_clear_error(self):
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY is missing"):
                tracker.get_jobs()


class PublishingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.page = Path(self.temp.name) / "index.html"
        self.original = (Path(__file__).resolve().parents[1] / "index.html").read_text()
        self.page.write_text(self.original)

    def run_with_response(self, text, reason="end_turn"):
        client = fake_client([response_events(text, reason)], [])
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "offline-test-key"}), \
             patch.object(tracker.anthropic, "Anthropic", return_value=client), \
             patch.object(tracker, "_link_is_live", return_value=True):
            return tracker.main(path=self.page)

    def test_complete_stream_passes_filtering_and_updates_real_template(self):
        self.assertEqual(self.run_with_response(json.dumps(sample_jobs())), 0)
        updated = self.page.read_text()
        data = json.JSONDecoder().raw_decode(updated.split("const jobs = ", 1)[1])[0]
        self.assertEqual(data, sample_jobs())
        self.assertIn("function badge", updated)
        self.assertIn("Last updated:", updated)

    def test_truncated_response_keeps_existing_page_byte_for_byte(self):
        with self.assertRaisesRegex(RuntimeError, "did not finish"):
            self.run_with_response(json.dumps(sample_jobs()), "max_tokens")
        self.assertEqual(self.page.read_text(), self.original)

    def test_small_verified_result_is_published_without_padding(self):
        self.assertEqual(self.run_with_response(json.dumps(sample_jobs()[:1])), 0)
        data = json.JSONDecoder().raw_decode(self.page.read_text().split('const jobs = ', 1)[1])[0]
        self.assertEqual(len(data), 1)

    def test_empty_or_malformed_results_keep_existing_page(self):
        for text in ("[]", "Response was cut off ["):
            with self.subTest(text=text):
                self.assertEqual(self.run_with_response(text), 1)
                self.assertEqual(self.page.read_text(), self.original)

    def test_template_mismatch_does_not_write_half_an_update(self):
        original = self.original.replace("document.getElementById('updated-line').textContent =", "const differentTemplate =")
        self.page.write_text(original)
        with self.assertRaisesRegex(ValueError, "updated-line"):
            tracker.update_html(sample_jobs(), self.page)
        self.assertEqual(self.page.read_text(), original)


class PostingVerificationTests(unittest.TestCase):
    def setUp(self):
        self.job = sample_jobs()[0] | {'role': 'Product Management Intern (Summer 2027)'}
        self.html = ('<h1>Product Management Intern (Summer 2027)</h1>'
                     '<p>Test Company 0. Build product features for our customers.</p>'
                     '<button>Apply now</button>')

    def test_matching_open_posting_passes(self):
        self.assertTrue(tracker._posting_matches(self.html, self.job))

    def test_wrong_role_generic_page_closed_page_and_missing_apply_fail(self):
        pages = [
            self.html.replace('Product Management', 'Product Design'),
            self.html.replace('<h1>Product Management Intern (Summer 2027)</h1>', '<h1>All jobs</h1>'),
            self.html.replace('<button>Apply now</button>', ''),
            self.html.replace('<button>', '<button disabled>'),
            self.html + '<p>This position is no longer available</p>',
            '<script>' + 'x' * 22000 + '</script>' + self.html + '<p>Applications are closed</p>',
            '<script>' + self.html + '</script>',
        ]
        for page in pages:
            with self.subTest(page=page[:80]):
                self.assertFalse(tracker._posting_matches(page, self.job))

    def test_non_pm_and_wrong_track_fail_even_with_matching_title(self):
        for role, track in [('Product Designer (2027)', 'Full-time'),
                            ('Product Management Intern (Summer 2027)', 'Full-time'),
                            ('Product Manager (2026)', 'Full-time')]:
            job = self.job | {'role': role, 'type': track}
            page = self.html.replace(self.job['role'], role)
            self.assertFalse(tracker._posting_matches(page, job))

    def test_network_failures_and_http_errors_are_unknown_not_open(self):
        for status in (403, 404, 410, 429, 500):
            response = SimpleNamespace(status_code=status, url=self.job['source'], text=self.html)
            with patch.object(tracker.requests, 'get', return_value=response):
                self.assertFalse(tracker._link_is_live(self.job))
        with patch.object(tracker.requests, 'get', side_effect=requests.Timeout()):
            self.assertFalse(tracker._link_is_live(self.job))

    def test_unsupported_dates_work_mode_and_salary_are_withheld(self):
        self.job.update(startDate='May 25, 2027', workMode='On-site', salary='$62/hr')
        tracker._ground_optional_details(self.job, self.html)
        for field in ('startDate', 'workMode', 'salary'):
            self.assertEqual(self.job[field], 'Not specified')

    def test_expired_structured_deadline_overrides_apply_control(self):
        page = self.html + '<script type="application/ld+json">' + json.dumps({
            '@type': 'JobPosting', 'validThrough': '2020-01-01',
        }) + '</script>'
        self.assertFalse(tracker._posting_matches(page, self.job))

    def test_deadline_without_year_is_checked_in_current_year(self):
        from datetime import datetime, timezone
        with patch.object(tracker, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 31, tzinfo=timezone.utc)
            self.assertTrue(tracker._deadline_already_passed('Applications close Friday, October 30th at 5 PM PST'))


if __name__ == "__main__":
    unittest.main()
