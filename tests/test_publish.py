import contextlib
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


SCRIPT = Path(__file__).resolve().parents[1] / "skills/mackerel-dev-improve/scripts/publish.py"
SPEC = importlib.util.spec_from_file_location("publish", SCRIPT)
publisher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
STAMP = 1792972800
REVISION = "a" * 40
COMPARISON_KEY = "c" * 64


class FakeClient:
    def __init__(self, metrics, metadata=None):
        self.metrics = metrics
        self.metadata = copy.deepcopy(metadata)
        self.calls = []
        self.org = "example-org"
        self.service_exists = True
        self.readback = "match"
        self.fail_post = False
        self.fail_metadata = False

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/api/v0/org":
            return {"name": self.org}
        if path == "/api/v0/services":
            if method == "POST":
                self.service_exists = True
                return {"name": "example-dev"}
            return {"services": [{"name": "example-dev"}] if self.service_exists else []}
        if path.endswith("/metadata/dev-quality"):
            if method == "PUT":
                if self.fail_metadata:
                    raise publisher.APIError(403)
                self.metadata = copy.deepcopy(body)
                return {"success": True}
            if self.metadata is None:
                raise publisher.APIError(404)
            return copy.deepcopy(self.metadata)
        if path.endswith("/tsdb"):
            if self.fail_post:
                raise publisher.APIError()
            return {"success": True}
        if "/metrics?" in path:
            name = parse_qs(urlsplit(path).query)["name"][0]
            metric = next(item for item in self.metrics if item["name"] == name)
            point = {"time": metric["time"] - 30, "value": metric["value"]}
            if self.readback == "missing":
                return {"metrics": []}
            if self.readback == "wrong":
                point["value"] += 1
            if self.readback == "ambiguous":
                return {"metrics": [point, dict(point, time=metric["time"] + 30)]}
            return {"metrics": [point]}
        raise AssertionError("Unexpected fake API request")


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name)
        self.metrics = [
            {"name": "tests.unit.cases.passed", "time": STAMP, "value": 3},
            {"name": "tests.unit.run.complete", "time": STAMP, "value": 1},
        ]
        self.manifest = {
            "schema_version": 1, "service": "example", "environment": "development",
            "mackerel_service": "example-dev", "revision": REVISION, "run_id": "42", "attempt_id": "1",
            "finished_at": datetime.fromtimestamp(STAMP, timezone.utc).isoformat(),
            "working_tree_dirty": False, "artifact_uri": "https://github.com/example/repo/actions/runs/42",
            "profiles": {"unit": {"scope": {"kind": "junit"}, "comparison_key": COMPARISON_KEY}},
            "collection": {"unit": {"complete": True, "status": "complete", "report_complete": True, "success": 1}},
            "publication": {"service": "example-dev", "measurement_timestamp": STAMP},
        }
        self.expected = {"org": "example-org", "revision": REVISION, "run_id": "42", "attempt_id": "1"}
        self.write_bundle()
        self.client = FakeClient(self.metrics)

    def write_bundle(self):
        payload = publisher.json_bytes(self.metrics)
        self.manifest["publication"].update(payload_sha256=hashlib.sha256(payload).hexdigest(), series=[item["name"] for item in self.metrics])
        (self.bundle / "metrics.json").write_bytes(payload)
        (self.bundle / "manifest.json").write_bytes(publisher.json_bytes(self.manifest))

    def publish(self, **kwargs):
        return publisher.publish(self.bundle, send=True, expected=self.expected, client=self.client,
                                 now=STAMP + 300, sleep=lambda _: None, **kwargs)

    def result(self):
        return json.loads((self.bundle / "publication-result.json").read_text())

    def test_offline_default_does_not_read_key_or_create_client(self):
        with patch.object(publisher, "Client", side_effect=AssertionError("network")):
            self.assertEqual(publisher.main(["--bundle", str(self.bundle)]), 0)
        self.assertEqual(self.result()["status"], "dry_run")
        self.assertFalse((self.bundle / ".publication.lock").exists())

    def test_verified_publication_preserves_unrelated_profile_metadata(self):
        unrelated = {"revision": "b" * 40, "measured_at": STAMP - 1000, "artifact_uri": "https://example.com/old"}
        self.client.metadata = {
            "schema_version": 1, "service": "example", "environment": "development",
            "profiles": {"other": unrelated}, "note": "keep",
        }
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.result()["status"], "verified")
        self.assertEqual(self.client.metadata["profiles"]["other"], unrelated)
        self.assertEqual(self.client.metadata["note"], "keep")
        self.assertEqual(self.client.metadata["profiles"]["unit"]["run_id"], "42")
        self.assertEqual(self.client.metadata["profiles"]["unit"]["measured_at"], STAMP)
        self.assertEqual(self.client.metadata["profiles"]["unit"]["comparison_key"], COMPARISON_KEY)

    def test_language_independent_test_cases_use_the_same_metric_contract(self):
        self.manifest["profiles"]["unit"]["scope"] = {"kind": "test_cases", "producer": "example-runner"}
        self.write_bundle()
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.result()["status"], "verified")
        self.manifest["collection"]["unit"]["success"] = None
        self.metrics.append({"name": "tests.unit.run.success", "time": STAMP, "value": 0})
        self.write_bundle()
        self.assertEqual(self.publish(), 1)

    def test_missing_or_invalid_profile_comparison_key_is_rejected_offline(self):
        for invalid in (None, "", "not-a-hash", "c" * 63):
            with self.subTest(key=invalid):
                self.manifest["profiles"]["unit"]["comparison_key"] = invalid
                self.write_bundle()
                self.assertEqual(self.publish(), 1)
                self.assertEqual(self.client.calls, [])

    def test_same_profile_cannot_silently_change_measurement_definition(self):
        self.client.metadata = {
            "schema_version": 1, "service": "example", "environment": "development",
            "profiles": {"unit": {"revision": "b" * 40, "run_id": "41", "attempt_id": "1",
                                  "measured_at": STAMP - 300, "comparison_key": "d" * 64}},
        }
        self.assertEqual(self.publish(), 1)
        self.assertFalse(any(method != "GET" for method, _, _ in self.client.calls))
        self.assertIn("Profile definition changed", self.result()["diagnostic"])

    def test_legacy_profile_metadata_is_upgraded_without_inventing_its_definition(self):
        self.client.metadata = {
            "schema_version": 1, "service": "example", "environment": "development",
            "profiles": {"unit": {"revision": "b" * 40, "run_id": "41", "attempt_id": "1",
                                  "measured_at": STAMP - 300}},
        }
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.client.metadata["profiles"]["unit"]["comparison_key"], COMPARISON_KEY)

    def test_identical_run_is_retryable_but_changed_evidence_is_not(self):
        self.assertEqual(self.publish(), 0)
        self.client.calls.clear()
        self.assertEqual(self.publish(), 0)
        self.metrics[0]["value"] = 4
        self.write_bundle()
        self.client.calls.clear()
        self.assertEqual(self.publish(), 1)
        self.assertFalse(any(method != "GET" for method, _, _ in self.client.calls))

    def test_org_and_generation_mismatch_never_post(self):
        self.client.org = "another-org"
        self.assertEqual(self.publish(), 1)
        self.assertFalse(any(method != "GET" for method, _, _ in self.client.calls))
        self.client.calls.clear()
        self.expected["attempt_id"] = "2"
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.client.calls, [])

    def test_old_and_dirty_snapshots_are_rejected_before_network(self):
        self.assertEqual(publisher.publish(self.bundle, send=True, expected=self.expected, client=self.client, now=STAMP + 86400), 1)
        self.manifest["working_tree_dirty"] = True
        self.write_bundle()
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.client.calls, [])

    def test_duration_over_one_second_is_not_a_ratio(self):
        self.metrics.append({"name": "tests.unit.run.duration_seconds", "time": STAMP, "value": 138.5})
        self.write_bundle()
        self.assertEqual(self.publish(), 0)
        self.assertEqual(self.result()["status"], "verified")

    def test_payload_tampering_and_nan_are_rejected(self):
        (self.bundle / "metrics.json").write_text("[]")
        self.assertEqual(self.publish(), 1)
        payload = b'[{"name":"tests.unit.cases.passed","time":1792972800,"value":NaN}]'
        (self.bundle / "metrics.json").write_bytes(payload)
        self.manifest["publication"]["payload_sha256"] = hashlib.sha256(payload).hexdigest()
        (self.bundle / "manifest.json").write_bytes(publisher.json_bytes(self.manifest))
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.client.calls, [])

    def test_huge_integer_and_fractional_counts_are_rejected_without_crashing(self):
        for invalid in (10 ** 1000, 2 ** 53, 2 ** 53 + 1, 3.5):
            with self.subTest(value_kind=type(invalid).__name__):
                self.metrics[0]["value"] = invalid
                self.write_bundle()
                self.assertEqual(self.publish(), 1)
                self.assertEqual(self.result()["status"], "rejected")
                self.assertEqual(self.client.calls, [])

    def test_collection_status_and_health_values_must_agree(self):
        self.metrics = [{"name": "tests.unit.run.complete", "time": STAMP, "value": 1}]
        for state in (
            {"complete": False, "report_complete": True, "status": "complete"},
            {"complete": False, "report_complete": True, "status": "partial"},
            {"complete": True, "report_complete": False, "status": "complete"},
        ):
            with self.subTest(state=state):
                self.manifest["collection"]["unit"] = state
                self.write_bundle()
                self.assertEqual(self.publish(), 1)
                self.assertEqual(self.client.calls, [])

    def test_unknown_success_cannot_be_published_as_zero_or_one(self):
        self.metrics = [{"name": "tests.unit.run.success", "time": STAMP, "value": 0}]
        self.manifest["collection"]["unit"] = {"complete": False, "report_complete": False, "status": "missing", "success": None}
        self.write_bundle()
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.client.calls, [])

    def test_unknown_series_and_partial_canonical_values_are_rejected(self):
        self.metrics[0]["name"] = "tests.commit_deadbeef.cases.passed"
        self.write_bundle()
        self.assertEqual(self.publish(), 1)
        self.metrics[0]["name"] = "tests.unit.cases.passed"
        self.manifest["collection"]["unit"] = {"complete": False, "status": "partial", "report_complete": True}
        self.write_bundle()
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.client.calls, [])

    def test_existing_newer_or_nearby_run_blocks_post(self):
        for offset in (10, -60):
            with self.subTest(offset=offset):
                self.client.calls.clear()
                self.client.metadata = {
                    "schema_version": 1, "service": "example", "environment": "development",
                    "profiles": {"unit": {"revision": "b" * 40, "run_id": "41", "attempt_id": "1", "measured_at": STAMP + offset}},
                }
                self.assertEqual(self.publish(), 1)
                self.assertFalse(any(method == "POST" for method, _, _ in self.client.calls))

    def test_http_success_without_unique_matching_readback_is_unverified(self):
        for readback in ("missing", "wrong", "ambiguous"):
            with self.subTest(readback=readback):
                self.client.calls.clear()
                self.client.readback = readback
                self.assertEqual(self.publish(), 1)
                self.assertEqual(self.result()["status"], "published_unverified")
                self.assertFalse(any(method == "PUT" for method, _, _ in self.client.calls))

    def test_readback_does_not_accept_an_adjacent_minute_with_the_same_value(self):
        metric = {"name": "tests.unit.cases.passed", "time": STAMP, "value": 3}
        for offset in (-60, 60):
            with self.subTest(offset=offset):
                response = {"metrics": [{"time": STAMP + offset, "value": 3}]}
                self.assertFalse(publisher.metric_matches(response, metric))

    def test_uncertain_post_and_metadata_failure_keep_distinct_status(self):
        self.client.fail_post = True
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.result()["status"], "publication_unknown")
        self.client.fail_post = False
        self.client.fail_metadata = True
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.result()["status"], "metrics_verified_metadata_failed")

    def test_service_creation_is_explicit(self):
        self.client.service_exists = False
        self.assertEqual(self.publish(), 1)
        self.assertFalse(any(method == "POST" for method, _, _ in self.client.calls))
        self.assertEqual(self.publish(create_service=True), 0)
        self.assertTrue(any(method == "POST" and path == "/api/v0/services" for method, path, _ in self.client.calls))

    def test_readonly_connection_check_accepts_missing_service_without_creating(self):
        self.client.service_exists = False
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(publisher.check_connection("example-org", "example-dev", self.client), 0)
        self.assertIn("service is missing", output.getvalue())
        self.assertEqual([call[0] for call in self.client.calls], ["GET", "GET"])

    def test_key_organization_dry_run_stays_offline(self):
        with patch.object(publisher, "Client", side_effect=AssertionError("network")):
            self.assertEqual(publisher.main(["--bundle", str(self.bundle), "--use-key-organization"]), 0)
        self.assertEqual(self.result()["status"], "dry_run")

    def test_key_organization_selection_is_exclusive(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            publisher.main(["--check-connection", "--use-key-organization", "--expected-org", "example-org", "--service", "example-dev"])
        self.assertEqual(raised.exception.code, 2)

    def test_key_organization_preflight_uses_only_get_and_never_discloses_name(self):
        self.client.org = "private-organization-fixture"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(publisher.check_connection(None, "example-dev", self.client, use_key_organization=True), 0)
        self.assertEqual([(method, path) for method, path, _ in self.client.calls], [
            ("GET", "/api/v0/org"), ("GET", "/api/v0/org"), ("GET", "/api/v0/services"),
        ])
        self.assertNotIn(self.client.org, output.getvalue())

    def test_key_organization_send_binds_internally_without_metadata_or_result_disclosure(self):
        self.client.org = "private-organization-fixture"
        self.expected.pop("org")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.publish(use_key_organization=True), 0)
        self.assertEqual([(method, path) for method, path, _ in self.client.calls[:2]], [
            ("GET", "/api/v0/org"), ("GET", "/api/v0/org"),
        ])
        self.assertNotIn(self.client.org, output.getvalue())
        self.assertNotIn(self.client.org, json.dumps(self.result()))
        self.assertNotIn(self.client.org, json.dumps(self.client.metadata))
        self.assertNotIn(self.client.org, json.dumps(self.client.calls))

    def test_invalid_key_organization_response_is_rejected_without_mutation(self):
        self.expected.pop("org")
        for invalid in (None, 123, [], "", "x" * 256, "private\norganization"):
            with self.subTest(response_type=type(invalid).__name__):
                self.client.calls.clear()
                self.client.org = invalid
                self.assertEqual(self.publish(use_key_organization=True), 1)
                self.assertEqual(self.result()["diagnostic"], "Invalid organization response")
                self.assertEqual([(method, path) for method, path, _ in self.client.calls], [("GET", "/api/v0/org")])

    def test_key_organization_is_rechecked_with_the_same_client(self):
        self.expected.pop("org")
        original = self.client.request
        organizations = iter(["first-private-fixture", "second-private-fixture"])

        def changed_response(method, path, body=None):
            response = original(method, path, body)
            return {"name": next(organizations)} if path == "/api/v0/org" else response

        with patch.object(self.client, "request", side_effect=changed_response):
            self.assertEqual(self.publish(use_key_organization=True), 1)
        self.assertEqual(self.result()["diagnostic"], "Mackerel organization mismatch")
        self.assertTrue(all(method == "GET" for method, _, _ in self.client.calls))
        self.assertNotIn("private-fixture", json.dumps(self.result()))

    def test_connection_and_publication_options_cannot_be_mixed(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            publisher.main(["--check-connection", "--send", "--expected-org", "example-org", "--service", "example-dev"])
        self.assertEqual(raised.exception.code, 2)


class ClientTest(unittest.TestCase):
    def test_http_error_redacts_body_url_and_key(self):
        key = "sensitive-fixture-key"
        client = publisher.Client(key, sleep=lambda _: None)
        error = HTTPError("https://example.com/" + key, 403, key, {"X-Secret": key}, io.BytesIO(key.encode()))
        with patch.object(client.opener, "open", side_effect=error), self.assertRaises(publisher.APIError) as raised:
            client.request("GET", "/api/v0/org")
        self.assertNotIn(key, str(raised.exception))

    def test_reads_retry_three_times_but_uncertain_posts_do_not_retry(self):
        client = publisher.Client("fixture-key", sleep=lambda _: None)
        with patch.object(client.opener, "open", side_effect=URLError("fixture-key")) as request:
            with self.assertRaises(publisher.APIError):
                client.request("GET", "/api/v0/org")
            self.assertEqual(request.call_count, 3)
            request.reset_mock()
            with self.assertRaises(publisher.APIError):
                client.request("POST", "/api/v0/services/test/tsdb", [])
            self.assertEqual(request.call_count, 1)

    def test_redirect_does_not_forward_api_key(self):
        handler = publisher.NoRedirect()
        with self.assertRaises(publisher.APIError):
            handler.redirect_request(None, None, 302, "", {}, "https://example.com/")


if __name__ == "__main__":
    unittest.main()
