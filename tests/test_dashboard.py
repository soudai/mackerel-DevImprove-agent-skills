import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/mackerel-dev-improve/scripts"
SPEC = importlib.util.spec_from_file_location("dashboard", SCRIPTS / "dashboard.py")
dashboard = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(SCRIPTS)] + sys.path):
    SPEC.loader.exec_module(dashboard)
STAMP = 1792972800


def graph_series(widget):
    return re.findall(r"service\('example-dev', '([a-z0-9._]+)'\)", widget["graph"]["expression"])


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dashboard test ")
        self.addCleanup(self.temp.cleanup)
        self.bundle = Path(self.temp.name) / "bundle"
        self.bundle.mkdir()
        self.metrics = [
            {"name": "tests.unit." + suffix, "time": STAMP, "value": value}
            for suffix, value in {"cases.selected": 3, "cases.passed": 2, "cases.failed": 1,
                                  "cases.error": 0, "cases.skipped": 0, "ratios.strict_pass": 2 / 3,
                                  "run.report_complete": 1, "run.complete": 1, "run.success": 0,
                                  "run.duration_seconds": 12.5, "run.last_completed_timestamp": STAMP}.items()
        ] + [{"name": "cccc.source." + suffix, "time": STAMP, "value": value}
             for suffix, value in {"analysis.complete": 1, "analysis.files": 2, "analysis.functions": 8,
                                   "analysis.parse_errors": 0, "analysis.parse_error_files": 0,
                                   "analysis.last_success_timestamp": STAMP, "cognitive.p95": 12,
                                   "cognitive.max": 15, "cognitive.over_limit_count": 2,
                                   "cognitive.over_limit_ratio": 0.25, "cyclomatic.p95": 8,
                                   "cyclomatic.max": 10, "cyclomatic.over_limit_count": 0,
                                   "cyclomatic.over_limit_ratio": 0}.items()]
        self.manifest = {
            "schema_version": 1, "mackerel_service": "example-dev",
            "finished_at": datetime.fromtimestamp(STAMP, timezone.utc).isoformat(),
            "artifact_uri": "https://example.com/evidence/run42",
            "profiles": {"unit": {"scope": {"kind": "test_cases"}, "comparison_key": "a" * 64},
                         "source": {"scope": {"kind": "cccc"}, "comparison_key": "b" * 64}},
            "collection": {"unit": {"complete": True, "report_complete": True, "status": "complete", "success": 0},
                           "source": {"complete": True, "report_complete": True, "status": "complete"}},
            "publication": {"service": "example-dev", "measurement_timestamp": STAMP},
        }
        self.write_bundle()

    def write_bundle(self):
        payload = dashboard.publish.json_bytes(self.metrics)
        self.manifest["publication"].update(payload_sha256=hashlib.sha256(payload).hexdigest(),
                                            series=[item["name"] for item in self.metrics])
        (self.bundle / "metrics.json").write_bytes(payload)
        (self.bundle / "manifest.json").write_bytes(dashboard.publish.json_bytes(self.manifest))

    def observation(self, *, service="example-dev"):
        return {
            "schema_version": 1, "mackerel_service": service,
            "identity": {"service_name": "example", "service_namespace": "team", "environment": "development", "version": None},
            "window": {"from": STAMP, "to": STAMP + 300},
            "metrics": ["runtime.web.requests_per_second", "runtime.db.connection_ratio"],
            "host_metrics": [{"name": "db_memory", "host_id": "Ab123cd", "metric": "memory.used"},
                             {"name": "web_cpu", "host_id": "Xy456z", "metric": "cpu.user.percentage"}],
            "apm": {"http": False, "database": False, "sampling_context": "not used"},
            "logs": {"enabled": False, "severities": ["INFO"]},
        }

    def write_observation(self, config):
        path = Path(self.temp.name) / "observation config.json"
        path.write_bytes(dashboard.publish.json_bytes(config))
        return path

    def test_payload_uses_documented_widget_shapes_and_nonoverlapping_layout(self):
        result = dashboard.generate(self.bundle)
        self.assertEqual({"title", "memo", "urlPath", "widgets"}, set(result))
        self.assertEqual("dev-improve/example-dev", result["urlPath"])
        occupied = set()
        for widget in result["widgets"]:
            layout = widget["layout"]
            self.assertLessEqual(layout["x"] + layout["width"], 24)
            self.assertGreaterEqual(layout["width"], 6 if widget["type"] == "graph" else 4)
            self.assertGreaterEqual(layout["height"], 6 if widget["type"] == "graph" else 2)
            for x in range(layout["x"], layout["x"] + layout["width"]):
                for y in range(layout["y"], layout["y"] + layout["height"]):
                    self.assertNotIn((x, y), occupied)
                    occupied.add((x, y))
            if widget["type"] == "graph":
                self.assertEqual({"type", "expression"}, set(widget["graph"]))
                self.assertEqual("expression", widget["graph"]["type"])
                self.assertNotIn("legendList", widget)
                self.assertNotIn("*", widget["graph"]["expression"])
                self.assertTrue(graph_series(widget))
                self.assertTrue(set(graph_series(widget)).issubset({m["name"] for m in self.metrics}))
            elif widget["type"] == "value":
                self.assertIn("metric", widget)
                self.assertNotIn("graph", widget)

    def test_seconds_timestamps_states_ratios_and_complexity_never_share_axes(self):
        graphs = [w for w in dashboard.generate(self.bundle)["widgets"] if w["type"] == "graph"]
        for widget in graphs:
            series = graph_series(widget)
            self.assertFalse(any("timestamp" in name for name in series))
            if any(name.endswith("duration_seconds") for name in series):
                self.assertEqual(["tests.unit.run.duration_seconds"], series)
                self.assertNotIn("max", widget["valueRange"])
            if any(name.endswith(("ratio", "strict_pass")) for name in series):
                self.assertEqual(1, widget["valueRange"]["max"])
                self.assertEqual(1, len(series))
            if any(name.endswith("over_limit_count") for name in series):
                self.assertEqual(1, len(series))
        self.assertTrue(any(graph_series(w) == ["tests.unit.run.report_complete", "tests.unit.run.complete",
                                               "tests.unit.run.success"] for w in graphs))

    def test_partial_collection_does_not_create_absent_canonical_series(self):
        self.metrics = [{"name": "tests.unit.run.complete", "time": STAMP, "value": 0}]
        self.manifest["profiles"].pop("source")
        self.manifest["collection"] = {"unit": {"complete": False, "report_complete": False, "status": "missing"}}
        self.write_bundle()
        result = dashboard.generate(self.bundle)
        self.assertEqual(2, len(result["widgets"]))
        self.assertEqual(["tests.unit.run.complete"], graph_series(result["widgets"][1]))

    def test_links_are_explicit_escaped_and_do_not_claim_latest_snapshot(self):
        self.manifest["artifact_uri"] = "https://example.com/(source)[42]"
        self.write_bundle()
        result = dashboard.generate(self.bundle, apm_url="https://mackerel.io/apm?service=example",
                                    logs_url="https://mackerel.io/logs")
        notes = result["widgets"][0]["markdown"]
        self.assertIn("%28source%29%5B42%5D", notes)
        self.assertIn("fixed snapshot, not the latest run", notes)
        self.assertIn("[APM investigation](https://mackerel.io/apm?service=example)", notes)
        self.assertIn("[Log investigation](https://mackerel.io/logs)", notes)

    def test_invalid_links_and_paths_are_rejected_without_network(self):
        for url in ("javascript:alert(1)", "http://example.com/", "https://key@example.com/",
                    "https://example.com/\n[bad]", "https://example.com:abc/", "https://example.com:8443/"):
            with self.subTest(url=url), self.assertRaises(dashboard.publish.PublicationError):
                dashboard.generate(self.bundle, logs_url=url)
        for path in ("", "/absolute", "path/../bad", "double//slash", "space path"):
            with self.subTest(path=path), self.assertRaises(dashboard.publish.PublicationError):
                dashboard.generate(self.bundle, url_path=path)

    def test_tampered_bundle_is_rejected_before_output(self):
        (self.bundle / "metrics.json").write_text("[]", encoding="utf-8")
        target = Path(self.temp.name) / "output.json"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, dashboard.main(["--bundle", str(self.bundle), "--out", str(target)]))
        self.assertFalse(target.exists())

    def test_cli_runs_from_another_directory_and_never_reads_credentials(self):
        target = Path(self.temp.name) / "generated output" / "dashboard.json"
        with patch.object(dashboard.publish, "Client", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, dashboard.main(["--bundle", str(self.bundle), "--out", str(target)]))
        result = subprocess.run([sys.executable, str(SCRIPTS / "dashboard.py"), "--bundle", str(self.bundle),
                                 "--out", str(target)], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(dashboard.generate(self.bundle), json.loads(target.read_text(encoding="utf-8")))

    def test_output_cannot_replace_input_evidence(self):
        path = self.bundle / "manifest.json"
        original = path.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, dashboard.main(["--bundle", str(self.bundle), "--out", str(path)]))
        self.assertEqual(original, path.read_bytes())

    def test_runtime_graphs_keep_each_selector_and_native_unit_separate(self):
        config = self.observation()
        original = dashboard.generate(self.bundle)
        result = dashboard.generate(self.bundle, observation_config=self.write_observation(config))
        additions = result["widgets"][len(original["widgets"]):]
        self.assertEqual(4, len(additions))
        self.assertEqual([
            "service('example-dev', 'runtime.web.requests_per_second')",
            "service('example-dev', 'runtime.db.connection_ratio')",
            "host('Ab123cd', 'memory.used')",
            "host('Xy456z', 'cpu.user.percentage')",
        ], [widget["graph"]["expression"] for widget in additions])
        self.assertTrue(all("valueRange" not in widget for widget in additions))
        self.assertTrue(all("group(" not in widget["graph"]["expression"] for widget in additions))
        notes = result["widgets"][0]["markdown"]
        self.assertIn("do not prove environment or version ownership", notes)
        self.assertIn("time window is not applied", notes)
        self.assertNotIn("APM investigation", notes)
        self.assertNotIn("Log investigation", notes)

    def test_wrong_service_invalid_schema_and_expression_injection_are_rejected(self):
        configs = [self.observation(service="different-dev")]
        malformed = self.observation()
        malformed["metrics"] = ["latency'), host('other', 'secret')"]
        configs.append(malformed)
        for field, value in (("host_id", "Ab123'), host('other"), ("metric", "memory.*"), ("name", "bad/name")):
            malformed = self.observation()
            malformed["host_metrics"][0][field] = value
            configs.append(malformed)
        malformed = self.observation()
        malformed["host_metrics"][0]["unknown"] = "field"
        configs.append(malformed)
        for config in configs:
            with self.subTest(config=config), self.assertRaises(dashboard.publish.PublicationError):
                dashboard.generate(self.bundle, observation_config=self.write_observation(config))

    def test_runtime_config_cli_is_offline_from_another_directory(self):
        config = self.write_observation(self.observation())
        target = Path(self.temp.name) / "runtime dashboard.json"
        with patch.object(dashboard.publish, "Client", side_effect=AssertionError("network")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, dashboard.main(["--bundle", str(self.bundle), "--out", str(target),
                                                "--observation-config", str(config)]))
        expected = target.read_bytes()
        completed = subprocess.run([sys.executable, str(SCRIPTS / "dashboard.py"), "--bundle", str(self.bundle),
                                    "--out", str(target), "--observation-config", str(config)],
                                   cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(expected, target.read_bytes())

    def test_runtime_config_cannot_be_overwritten_by_dashboard_output(self):
        config = self.write_observation(self.observation())
        original = config.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, dashboard.main(["--bundle", str(self.bundle), "--out", str(config),
                                                "--observation-config", str(config)]))
        self.assertEqual(original, config.read_bytes())


if __name__ == "__main__":
    unittest.main()
