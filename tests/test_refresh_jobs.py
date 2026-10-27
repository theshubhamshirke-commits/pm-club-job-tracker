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
        "priority": "Good fit", "source": f"https://testcompany{i}.com/careers/job/{i}",
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
        self.page = Path(self.temp.name) / 'index.html'
        self.page.write_text((Path(__file__).resolve().parents[1] / 'index.html').read_text())
        self.original_jobs = sample_jobs()[:2]
        tracker.update_html(self.original_jobs, self.page)
        self.original = self.page.read_text()
        self.data = self.page.parent / 'data'

    def check(self, job, previous=None):
        return {'job': job | {'lastChecked': '2026-10-27T00:00:00Z'}, 'status': 'open',
                'checkedAt': '2026-10-27T00:00:00Z', 'reason': 'offline fixture'}

    def run_refresh(self, new=None, check=None, failed=False):
        def discovery(_):
            if failed:
                raise RuntimeError('search disconnected')
            return new or [], [{'name': 'test', 'status': 'complete'}]
        with patch.object(tracker, '_verify_job', side_effect=check or self.check):
            return tracker.refresh(self.page, discovery=discovery)

    def test_existing_jobs_survive_search_omission_and_new_jobs_are_added(self):
        report = self.run_refresh(sample_jobs()[2:3])
        self.assertEqual(report['existingRechecked'], 2)
        self.assertEqual(report['publishedCount'], 3)
        self.assertEqual(report['addedCount'], 1)
        self.assertEqual(len(tracker._load_page_jobs(self.page)), 3)

    def test_discovery_failure_still_rechecks_existing_jobs(self):
        report = self.run_refresh(failed=True)
        self.assertEqual(report['existingRechecked'], 2)
        self.assertEqual(report['publishedCount'], 2)
        self.assertFalse(report['discoveryComplete'])
        self.assertIn('"discoveryComplete": false', self.page.read_text())

    def test_closed_removed_unknown_hidden_then_retried(self):
        def unavailable(job, previous):
            result = self.check(job)
            result['status'] = 'closed' if job['company'].endswith('0') else 'unverified'
            return result
        report = self.run_refresh(check=unavailable)
        self.assertEqual(report['publishedCount'], 0)
        self.assertEqual(tracker._load_page_jobs(self.page), [])
        state = json.loads((self.data / 'tracked-jobs.json').read_text())
        self.assertEqual({r['status'] for r in state['jobs']}, {'closed', 'unverified'})
        report = self.run_refresh()
        self.assertEqual(report['existingRechecked'], 1)
        self.assertEqual(report['publishedCount'], 1)

    def test_duplicate_discoveries_reuse_existing_record(self):
        duplicate = self.original_jobs[0] | {'source': self.original_jobs[0]['source'] + '?utm_source=search'}
        report = self.run_refresh([duplicate, duplicate])
        self.assertEqual(report['newCandidatesChecked'], 0)
        self.assertEqual(report['publishedCount'], 2)

    def test_small_verified_result_is_not_padded(self):
        def one(job, previous):
            result = self.check(job)
            result['status'] = 'open' if job['company'].endswith('0') else 'closed'
            return result
        self.assertEqual(self.run_refresh(check=one)['publishedCount'], 1)

    def test_template_mismatch_does_not_write_half_an_update(self):
        original = self.original.replace("document.getElementById('updated-line').textContent =", 'const differentTemplate =')
        self.page.write_text(original)
        with self.assertRaisesRegex(ValueError, 'updated-line'):
            tracker.update_html(sample_jobs(), self.page)
        self.assertEqual(self.page.read_text(), original)

    def test_embedded_script_content_is_escaped(self):
        row = sample_jobs()[0] | {'notes': '</script><script>alert(1)</script>'}
        tracker.update_html([row], self.page)
        self.assertNotIn(row['notes'], self.page.read_text())
        self.assertEqual(tracker._load_page_jobs(self.page)[0]['notes'], row['notes'])

    def test_malformed_discovery_data_is_not_successful_empty_search(self):
        with self.assertRaises(ValueError):
            tracker._extract_json_array('Response was cut off [')
        self.assertEqual(tracker._extract_json_array('[]'), [])


class PostingVerificationTests(unittest.TestCase):
    def setUp(self):
        self.job = sample_jobs()[0] | {'role': 'Product Management Intern (Summer 2027)'}
        self.html = ('<h1>Product Management Intern (Summer 2027)</h1>'
                     '<p>Test Company 0. San Mateo, CA, United States. Build product features for our customers.</p>'
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


    def test_description_timing_qualifies_without_2027_in_title(self):
        job = self.job | {'role': 'Product Management Intern'}
        page = self.html.replace(self.job['role'], job['role']) + '<p>Summer 2027 internship</p>'
        self.assertTrue(tracker._posting_matches(page, job))

    def test_related_job_or_footer_cannot_supply_missing_2027(self):
        job = self.job | {'role': 'Product Management Intern'}
        page = self.html.replace(self.job['role'], job['role'])
        for unrelated in ('Related jobs: Summer 2027 Product Manager Intern', 'Privacy Policy Copyright Summer 2027'):
            self.assertFalse(tracker._posting_matches(page + '<p>' + unrelated + '</p>', job))

    def test_fulltime_requires_2027_signal_and_rejects_explicit_fall_start(self):
        job = self.job | {'role': 'Associate Product Manager', 'type': 'Full-time'}
        for text, expected in [('United States. Hiring immediately.', False),
                               ('United States. Class of 2027.', True),
                               ('United States. Starts September 2027.', False)]:
            self.assertEqual(not tracker._scope_reason(job, text), expected)

    def test_year_labeled_early_career_and_college_grad_cohorts_qualify(self):
        for role in ('[2027] Associate Product Manager, Early Career', 'Associate Product Manager (College Grad 2027)'):
            job = self.job | {'role': role, 'type': 'Full-time'}
            self.assertEqual(tracker._scope_reason(job, 'San Mateo, CA. Product responsibilities.'), '')
        job = self.job | {'role': 'Senior Product Manager 2027', 'type': 'Full-time'}
        self.assertTrue(tracker._scope_reason(job, 'San Mateo, CA. Hiring immediately.'))

    def test_explicit_location_can_appear_later_in_the_description(self):
        description = 'Product responsibilities. ' * 100 + 'Most roles will be located at company headquarters in Seattle, WA or Arlington, VA.'
        self.assertEqual(tracker._scope_reason(self.job, description), '')

    def test_us_footer_does_not_establish_job_location(self):
        job = self.job
        self.assertTrue(tracker._scope_reason(job, 'Toronto. ' + 'Job requirements. ' * 100 + 'US benefits policy.'))

    def test_non_us_metadata_overrides_us_company_footer(self):
        job = self.job
        page = self.html + '<script type="application/ld+json">' + json.dumps({
            '@type': 'JobPosting', 'title': job['role'],
            'jobLocation': {'address': {'addressCountry': 'Canada'}},
        }) + '</script>'
        response = SimpleNamespace(status_code=200, url=job['source'], text=page)
        with patch.object(tracker.requests, 'get', return_value=response):
            self.assertEqual(tracker._verify_job(job)['status'], 'out_of_scope')

    def test_workday_requires_positive_application_flags_and_matching_title(self):
        job = self.job | {'company': 'Testco', 'source': 'https://testco.wd5.myworkdayjobs.com/External/job/City/PM_R123-1'}
        shell = SimpleNamespace(status_code=200, url=job['source'], text='tenant: "testco", siteId: "External",')
        info = {'title': job['role'], 'jobDescription': '<p>Summer 2027 internship</p>',
                'country': {'descriptor': 'United States of America'}, 'location': 'San Mateo, CA',
                'canApply': True, 'posted': True, 'startDate': '2026-10-01'}
        for changes, expected in [({}, 'open'), ({'canApply': False}, 'closed'),
                                  ({'posted': False}, 'closed'), ({'canApply': None}, 'unverified'),
                                  ({'title': 'Unrelated job'}, 'unverified'),
                                  ({'country': {'descriptor': 'Canada'}}, 'out_of_scope')]:
            payload = SimpleNamespace(status_code=200, json=lambda: {'jobPostingInfo': info | changes})
            with self.subTest(changes=changes), patch.object(tracker.requests, 'get', side_effect=[shell, payload]):
                result = tracker._verify_job(job)
                self.assertEqual(result['status'], expected)
                if expected == 'open':
                    self.assertEqual(result['job']['startDate'], 'Not specified')

    def test_changed_source_regrounds_optional_facts(self):
        job = self.job | {'salary': '$999/hr'}
        response = SimpleNamespace(status_code=200, url=job['source'], text=self.html)
        with patch.object(tracker.requests, 'get', return_value=response):
            result = tracker._verify_job(job, {'sourceFingerprint': 'old'})
        self.assertEqual(result['status'], 'open')
        self.assertEqual(result['job']['salary'], 'Not specified')

    def test_workday_locales_and_apply_urls_have_same_identity(self):
        job = self.job | {'source': 'https://company.wd5.myworkdayjobs.com/en-US/Jobs/job/City/PM_JR123-1'}
        alternate = job | {'source': 'https://company.wd5.myworkdayjobs.com/Jobs/job/City/PM_JR123-2/apply?source=search'}
        self.assertEqual(tracker._job_key(job), tracker._job_key(alternate))


    def test_ashby_requires_listed_exact_job_and_application_link(self):
        job = self.job | {'company': 'Solace', 'role': 'Associate Product Manager (College Grad 2027)',
                         'type': 'Full-time', 'source': 'https://jobs.ashbyhq.com/solace/id123'}
        shell = SimpleNamespace(status_code=200, url=job['source'], text='<div id="app"></div>')
        info = {'id': 'id123', 'title': job['role'], 'isListed': True,
                'jobUrl': job['source'], 'applyUrl': job['source'] + '/application',
                'descriptionHtml': '<p>Start date: July 2027.</p>', 'location': 'Redwood City, CA',
                'workplaceType': 'Hybrid', 'address': {'postalAddress': {'addressCountry': 'USA'}}}
        for change, expected in [({}, 'open'), ({'isListed': False}, 'unverified'),
                                  ({'id': 'other'}, 'unverified'), ({'applyUrl': 'https://example.com'}, 'unverified'),
                                  ({'title': 'Product Designer'}, 'unverified'),
                                  ({'address': {'postalAddress': {'addressCountry': 'Canada'}}}, 'out_of_scope')]:
            response = SimpleNamespace(status_code=200, json=lambda: {'jobs': [info | change]})
            with self.subTest(change=change), patch.object(tracker.requests, 'get', side_effect=[shell, response]):
                result = tracker._verify_job(job)
                self.assertEqual(result['status'], expected)
                if expected == 'open':
                    self.assertEqual(result['job']['workMode'], 'Hybrid')


class RecoveryTests(unittest.TestCase):
    def test_malformed_pass_can_recover_candidates_without_claiming_complete_search(self):
        with tempfile.TemporaryDirectory() as directory:
            plan = Path(directory) / 'plan.json'
            plan.write_text(json.dumps({'batches': [{'name': 'test', 'focus': 'PM', 'queries': ['PM 2027']}], 'employers': ['Exampleco']}))
            def research(client, prompt, telemetry):
                telemetry['fetchedUrls'] = ['https://exampleco.com/jobs/123']
                return SimpleNamespace(content=[SimpleNamespace(type='text', text='Malformed response [')])
            response = SimpleNamespace(status_code=200, url='https://exampleco.com/jobs/123',
                text='<h1>Product Management Intern - Summer 2027</h1>')
            with patch.dict('os.environ', {'ANTHROPIC_API_KEY': 'offline'}), \
                 patch.object(tracker.anthropic, 'Anthropic'), \
                 patch.object(tracker, '_research_jobs', side_effect=research), \
                 patch.object(tracker.requests, 'get', return_value=response):
                rows, batches = tracker.discover_jobs(plan)
            self.assertEqual(len(rows), 1)
            self.assertEqual(batches[0]['status'], 'failed')
            self.assertEqual(batches[0]['recoveredFromSources'], 1)

    def test_recovers_only_observed_titles_on_configured_employer_sources(self):
        url = 'https://careers.exampleco.com/jobs/123'
        response = SimpleNamespace(status_code=200, url=url,
            text='<h1>Product Management Intern - Summer 2027</h1><p>United States</p>')
        with patch.object(tracker.requests, 'get', return_value=response):
            rows = tracker.recover_candidates([url, url, 'https://aggregator.com/jobs/123'], ['Exampleco'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['role'], 'Product Management Intern - Summer 2027')
        self.assertEqual(rows[0]['salary'], 'Not specified')
        # Recovery alone does not qualify a posting: an Apply control is absent.
        with patch.object(tracker.requests, 'get', return_value=response):
            self.assertEqual(tracker._verify_job(rows[0])['status'], 'unverified')

    def test_unreadable_or_non_pm_source_cannot_supply_a_title(self):
        url = 'https://careers.exampleco.com/jobs/123'
        for html in ('<h1>All jobs</h1>', '<h1>Product Designer 2027</h1>', '<div>loading</div>'):
            response = SimpleNamespace(status_code=200, url=url, text=html)
            with patch.object(tracker.requests, 'get', return_value=response):
                self.assertEqual(tracker.recover_candidates([url], ['Exampleco']), [])



if __name__ == "__main__":
    unittest.main()
