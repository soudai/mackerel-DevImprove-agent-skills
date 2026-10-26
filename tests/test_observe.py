import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/mackerel-dev-improve/scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("observe", SCRIPTS / "observe.py")
observe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observe)
sys.path.pop(0)


class FakeClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
        self.org = "example-org"
        self.failure = None
        self.metric_points = [{"time": cfg["window"]["from"], "value": 1}]
        self.http = [{"method": "GET", "route": "/secret?token=hidden", "totalMillis": 200, "averageMillis": 20, "approxP95Millis": 30, "errorRatePercentage": 5, "requestCount": 10}]
        self.database = [{"query": "SELECT secret FROM private", "totalMillis": 100, "averageMillis": 10, "approxP95Millis": 15, "executionCount": 10}]
        self.log_pages = [{"results": [self.log()], "pagination": {"hasNextPage": False}}]
        self.apm_has_next = False

    def log(self, cursor="one", environment="development", version=None):
        version = self.cfg["identity"]["version"] if version is None else version
        attrs = [{"key": "service.version", "value": {"valueType": "string", "stringValue": version}}]
        if environment is not None:
            attrs.append({"key": "deployment.environment.name", "value": {"valueType": "string", "stringValue": environment}})
        return {"cursor": cursor, "timestamp": "2026-10-26T00:00:00.123456789Z", "severity": "ERROR", "body": "SECRET log text", "traceId": "a" * 32, "spanId": "b" * 16, "serviceName": "example", "serviceNamespace": "example-team", "resourceAttributes": attrs}

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        if path == "/api/v0/org":
            return {"name": self.org}
        if self.failure and self.failure in path:
            raise observe.publish.APIError(403)
        if "/metrics?" in path:
            return {"metrics": self.metric_points}
        if "/apm/" in path:
            rows = self.http if "http-server" in path else self.database
            return {"results": rows, "hasNextPage": self.apm_has_next}
        if path == "/api/v0/logs":
            self_log_calls = sum(call[1] == path for call in self.calls)
            return self.log_pages[self_log_calls - 1]
        raise AssertionError(path)


class ObserveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = json.loads((ROOT / "skills/mackerel-dev-improve/assets/observation.example.json").read_text())
        self.client = FakeClient(self.cfg)

    def collect(self, **kwargs):
        return observe.observe(self.cfg, self.client, "example-org", sleep=lambda _: None, **kwargs)

    def test_all_signals_use_read_operations_and_explicit_scope(self):
        result = self.collect()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["signals"]["http"]["rows"][0]["error_ratio"], .05)
        self.assertEqual(result["signals"]["logs"]["matched"], 1)
        for method, path, body in self.client.calls:
            self.assertTrue(method == "GET" or (method == "POST" and path == "/api/v0/logs"))
            if "/apm/" in path:
                query = parse_qs(urlsplit(path).query)
                self.assertEqual(query["version"], [self.cfg["identity"]["version"]])
                self.assertEqual(query["environment"], ["development"])
        serialized = json.dumps(result)
        self.assertNotIn("SECRET", serialized)
        self.assertNotIn("SELECT secret", serialized)
        self.assertNotIn("token=hidden", serialized)
        self.assertEqual(result["signals"]["logs"]["records"][0]["trace_id"], "a" * 32)

    def test_raw_text_requires_explicit_option(self):
        result = self.collect(include_text=True)
        self.assertIn("SECRET", result["signals"]["logs"]["records"][0]["body"])
        self.assertIn("SELECT", result["signals"]["database"]["rows"][0]["query"])

    def test_wrong_organization_stops_before_reading_any_signal(self):
        self.client.org = "different"
        with self.assertRaises(observe.publish.PublicationError):
            self.collect()
        self.assertEqual(len(self.client.calls), 1)

    def test_missing_metric_does_not_become_zero(self):
        self.client.metric_points = []
        result = self.collect()
        metric = result["signals"]["metric:" + self.cfg["metrics"][0]]
        self.assertEqual(metric["status"], "missing")
        self.assertIsNone(metric["latest"])
        self.assertEqual(result["status"], "partial")

    def test_future_duplicate_and_nonfinite_points_are_rejected(self):
        for points in ([{"time": self.cfg["window"]["to"] + 1, "value": 1}], [{"time": self.cfg["window"]["from"], "value": float("nan")}], self.client.metric_points * 2):
            with self.subTest(points=points):
                self.client.metric_points = points
                self.client.calls.clear()
                result = self.collect()
                self.assertEqual(result["signals"]["metric:" + self.cfg["metrics"][0]]["status"], "error")

    def test_one_signal_error_preserves_other_evidence(self):
        self.client.failure = "http-server"
        result = self.collect()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["signals"]["http"]["status"], "error")
        self.assertEqual(result["signals"]["database"]["status"], "complete")

    def test_apm_page_limit_marks_truncation(self):
        self.client.apm_has_next = True
        result = self.collect(max_pages=1)
        self.assertEqual(result["signals"]["http"]["status"], "truncated")
        self.assertEqual(result["status"], "partial")

    def test_apm_repeated_group_does_not_double_count(self):
        self.client.apm_has_next = True
        result = self.collect(max_pages=2)
        self.assertEqual(result["signals"]["http"]["status"], "error")

    def test_logs_filter_environment_and_revision_locally(self):
        self.client.log_pages[0]["results"] += [self.client.log("prod", "production"), self.client.log("old", version="old"), self.client.log("missing", None)]
        result = self.collect()
        logs = result["signals"]["logs"]
        self.assertEqual((logs["matched"], logs["excluded_other_scope"], logs["excluded_missing_scope"]), (1, 2, 1))
        self.assertEqual(logs["status"], "partial")
        self.assertEqual(logs["severity_counts"], {"ERROR": 1})

    def test_logs_cursor_paging_and_half_open_window(self):
        self.client.log_pages = [{"results": [self.client.log("one")], "pagination": {"hasNextPage": True, "endCursor": "one"}}, {"results": [self.client.log("two")], "pagination": {"hasNextPage": False}}]
        result = self.collect()
        self.assertEqual(result["signals"]["logs"]["matched"], 2)
        last_body = self.client.calls[-1][2]
        self.assertEqual(last_body["pageCursor"], {"direction": "NEXT", "cursor": "one"})
        self.client.calls.clear()
        self.client.log_pages[1]["results"][0]["timestamp"] = "2026-10-26T00:15:00Z"
        self.assertEqual(self.collect()["signals"]["logs"]["status"], "error")

    def test_logs_limit_and_cursor_loop_are_not_complete(self):
        self.client.log_pages[0]["pagination"] = {"hasNextPage": True, "endCursor": "one"}
        self.assertEqual(self.collect(max_pages=1)["signals"]["logs"]["status"], "truncated")
        self.client.calls.clear()
        self.client.log_pages.append(copy.deepcopy(self.client.log_pages[0]))
        self.assertEqual(self.collect(max_pages=2)["signals"]["logs"]["status"], "error")

    def test_trace_filter_is_propagated_and_verified(self):
        self.cfg["logs"]["trace_id"] = "c" * 32
        result = self.collect()
        self.assertEqual(self.client.calls[-1][2]["traceId"], "c" * 32)
        self.assertEqual(result["signals"]["logs"]["status"], "error")

    def test_config_rejects_unknown_fields_unbounded_windows_and_bad_ids(self):
        for change in (lambda c: c.update(extra=True), lambda c: c["window"].update(to=c["window"]["from"] + 1801), lambda c: c["logs"].update(trace_id="bad"), lambda c: c["identity"].update(version=""), lambda c: c.update(metrics=["a", "a"])):
            cfg = copy.deepcopy(self.cfg)
            change(cfg)
            path = self.root / "config.json"
            path.write_text(json.dumps(cfg))
            with self.assertRaises(observe.publish.PublicationError):
                observe.config_file(path)

    def test_offline_cli_uses_bundled_example_without_key_or_network(self):
        out = self.root / "folder with spaces" / "plan.json"
        with patch.object(observe.publish, "Client", side_effect=AssertionError("network")), contextlib.redirect_stdout(io.StringIO()):
            code = observe.main(["--config", str(ROOT / "skills/mackerel-dev-improve/assets/observation.example.json"), "--out", str(out)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.read_text())["status"], "planned")

    def test_pacing_and_mutation_block(self):
        waits = []
        reader = observe.PacedReader(self.client, sleep=waits.append, monotonic=lambda: 0)
        reader.request("GET", "/api/v0/org")
        reader.request("GET", "/api/v0/org")
        self.assertEqual(waits, [1.1])
        with self.assertRaises(observe.publish.PublicationError):
            reader.request("POST", "/api/v0/services", {})

    def test_host_metrics_use_explicit_host_ids_and_preserve_source(self):
        self.cfg["host_metrics"] = [{"name": "db_load", "host_id": "dbHost1", "metric": "loadavg5"}]
        result = self.collect()
        signal = result["signals"]["host_metric:db_load"]
        self.assertEqual(("complete", "dbHost1", "loadavg5"), (signal["status"], signal["host_id"], signal["metric"]))
        paths = [path for method, path, body in self.client.calls]
        host_path = next(path for path in paths if path.startswith("/api/v0/hosts/"))
        self.assertEqual("/api/v0/hosts/dbHost1/metrics", urlsplit(host_path).path)
        self.assertEqual(["loadavg5"], parse_qs(urlsplit(host_path).query)["name"])
        self.assertIn("no revision/environment ownership", signal["interpretation"])

    def test_missing_host_metric_keeps_other_signals_and_remains_partial(self):
        self.cfg["host_metrics"] = [{"name": "db_load", "host_id": "dbHost1", "metric": "loadavg5"}]
        self.client.failure = "/hosts/"
        result = self.collect()
        self.assertEqual("partial", result["status"])
        self.assertEqual("error", result["signals"]["host_metric:db_load"]["status"])
        self.assertEqual("complete", result["signals"]["http"]["status"])
        self.assertNotIn("points", result["signals"]["host_metric:db_load"])

    def test_host_only_config_and_version_null_are_explicitly_supported(self):
        self.cfg["identity"]["version"] = None
        self.cfg["metrics"] = []
        self.cfg["apm"].update(http=False, database=False)
        self.cfg["logs"]["enabled"] = False
        self.cfg["host_metrics"] = [{"name": "web_cpu", "host_id": "webHost1", "metric": "cpu.user.percentage"}]
        path = self.root / "host-config.json"
        path.write_text(json.dumps(self.cfg))
        cfg = observe.config_file(path)
        result = observe.observe(cfg, self.client, "example-org", sleep=lambda _: None)
        self.assertEqual("complete", result["status"])
        self.assertEqual({"host_metric:web_cpu"}, set(result["signals"]))

    def test_null_version_omits_apm_filter_and_marks_mixed_logs_unpinned(self):
        original_version = self.cfg["identity"]["version"]
        self.cfg["identity"]["version"] = None
        unversioned = self.client.log("missing", version="discard")
        unversioned["resourceAttributes"] = [a for a in unversioned["resourceAttributes"] if a["key"] != "service.version"]
        self.client.log_pages[0]["results"] += [self.client.log("old", version="old"), unversioned]
        result = self.collect()
        self.assertFalse(result["runtime_version_filter_applied"])
        self.assertFalse(result["signals"]["http"]["version_filter_applied"])
        self.assertFalse(result["signals"]["database"]["version_filter_applied"])
        logs = result["signals"]["logs"]
        self.assertEqual("complete", logs["status"])
        self.assertEqual(3, logs["matched"])
        self.assertFalse(logs["version_filter_applied"])
        self.assertEqual(1, logs["matched_without_version"])
        self.assertEqual({None, "old", original_version}, {row["service_version"] for row in logs["records"]})
        for method, path, body in self.client.calls:
            if "/apm/" in path:
                self.assertNotIn("version", parse_qs(urlsplit(path).query, keep_blank_values=True))

    def test_null_version_never_disables_environment_filtering(self):
        self.cfg["identity"]["version"] = None
        self.client.log_pages[0]["results"] = [self.client.log("prod", "production", version="old"),
                                                self.client.log("missing", None, version="old")]
        logs = self.collect()["signals"]["logs"]
        self.assertEqual("partial", logs["status"])
        self.assertEqual((0, 1, 1), (logs["matched"], logs["excluded_other_scope"], logs["excluded_missing_scope"]))

    def test_host_config_rejects_duplicates_unbounded_selectors_and_path_injection(self):
        entry = {"name": "db_load", "host_id": "dbHost1", "metric": "loadavg5"}
        invalid_hosts = [[entry, dict(entry, metric="memory.used")],
                         [entry, dict(entry, name="second_name")],
                         [dict(entry, host_id="../services/another")],
                         [dict(entry, metric="loadavg5&from=0")],
                         [dict(entry, name="name-" + str(i)) for i in range(33)]]
        for entries in invalid_hosts:
            with self.subTest(entries=entries):
                cfg = copy.deepcopy(self.cfg)
                cfg["host_metrics"] = entries
                path = self.root / "invalid-hosts.json"
                path.write_text(json.dumps(cfg))
                with self.assertRaises(observe.publish.PublicationError):
                    observe.config_file(path)
        self.cfg["identity"].pop("version")
        path = self.root / "missing-version.json"
        path.write_text(json.dumps(self.cfg))
        with self.assertRaises(observe.publish.PublicationError):
            observe.config_file(path)

    def test_infrastructure_example_plans_host_metrics_and_includes_info_logs(self):
        config = ROOT / "skills/mackerel-dev-improve/assets/infrastructure-observation.example.json"
        cfg = observe.config_file(config)
        self.assertIsNone(cfg["identity"]["version"])
        self.assertIn("INFO", cfg["logs"]["severities"])
        out = self.root / "infra-plan.json"
        with patch.object(observe.publish, "Client", side_effect=AssertionError("network")), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, observe.main(["--config", str(config), "--out", str(out)]))
        result = json.loads(out.read_text())
        self.assertEqual(3, len(result["requests"]["host_metrics"]))
        self.assertFalse(result["runtime_version_filter_applied"])
        self.assertNotIn("http", result["requests"])


if __name__ == "__main__":
    unittest.main()
