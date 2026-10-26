"""Optional Contrib 0.163.2 checks; never contacts a DB or Mackerel."""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "skills/mackerel-dev-improve/assets"
COLLECTOR = os.environ.get("OTELCOL_CONTRIB")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(COLLECTOR, "Set OTELCOL_CONTRIB for the real Collector integration tests")
class InfrastructureConfigTests(unittest.TestCase):
    def setUp(self):
        self.assertIn("0.163.2", subprocess.check_output(
            [COLLECTOR, "--version"], text=True, encoding="utf-8"))
        work = ROOT / ".quality/otel-tests"
        work.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="infrastructure-", dir=work))
        self.source = self.work / "access.jsonl"
        self.source.write_text("", encoding="utf-8")
        self.environment = dict(os.environ, MACKEREL_APIKEY="validation-only",
            OTELCOL_INSTANCE_ID="fixture-collector", INFRA_SERVICE_NAMESPACE="fixture-namespace",
            INFRA_HOST_ID="fixture-host", INFRA_ENVIRONMENT="development",
            POSTGRESQL_MONITOR_ENDPOINT="127.0.0.1:5432", POSTGRESQL_MONITOR_USERNAME="fixture-monitor",
            POSTGRESQL_MONITOR_PASSWORD="validation-only", POSTGRESQL_MONITOR_DATABASE="fixture-db",
            POSTGRESQL_MONITOR_INSTANCE_ID="fixture-postgresql", WEB_ACCESS_LOG_PATH=str(self.source),
            WEB_SERVICE_NAME="fixture-web", WEB_INSTANCE_ID="fixture-web-instance")

    def command(self, *overlays):
        result = [COLLECTOR, "--config", str(ASSETS / "otel-collector.example.yaml")]
        for overlay in overlays:
            result.extend(["--config", str(ASSETS / overlay)])
        return result

    def test_optional_overlays_validate_without_database_connection(self):
        overlays = ["otel-host.example.yaml", "otel-postgresql.example.yaml", "otel-web-logs.example.yaml"]
        for selection in [(name,) for name in overlays] + [tuple(overlays)]:
            with self.subTest(overlays=selection):
                result = subprocess.run(self.command(*selection) + ["validate"], env=self.environment,
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_web_json_preserves_body_and_only_uses_valid_supplied_trace_context(self):
        grpc_port, http_port, metrics_port = free_port(), free_port(), free_port()
        output = self.work / "web-output.json"
        overrides = {
            "receivers": {
                "otlp": {"protocols": {
                    "grpc": {"endpoint": f"127.0.0.1:{grpc_port}"},
                    "http": {"endpoint": f"127.0.0.1:{http_port}"}}},
                "prometheus/collector": {"config": {"scrape_configs": [{
                    "job_name": "dev-otel-collector", "scrape_interval": "30s",
                    "static_configs": [{"targets": [f"127.0.0.1:{metrics_port}"]}]}]}}},
            "exporters": {"file/test_web": {"path": str(output), "flush_interval": "100ms"}, "nop": {}},
            "service": {
                "telemetry": {"metrics": {"readers": [{"pull": {"exporter": {"prometheus": {
                    "host": "127.0.0.1", "port": metrics_port}}}}]}},
                "pipelines": {**{name: {"exporters": ["nop"]} for name in (
                    "traces", "logs", "metrics", "metrics/collector")},
                    "logs/development_web": {"exporters": ["file/test_web"]}}},
        }
        override_path = self.work / "local-exporters.json"
        override_path.write_text(json.dumps(overrides), encoding="utf-8")
        command = self.command("otel-web-logs.example.yaml") + ["--config", str(override_path)]
        log = (self.work / "collector.log").open("wb")
        process = subprocess.Popen(command, env=self.environment, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        opener = build_opener(ProxyHandler({}))
        try:
            deadline = time.monotonic() + 20
            while True:
                self.assertIsNone(process.poll(), "Collector stopped; see " + str(self.work))
                try:
                    with opener.open(f"http://127.0.0.1:{metrics_port}/metrics", timeout=1):
                        break
                except (URLError, OSError):
                    if time.monotonic() >= deadline:
                        self.fail("Collector did not start; see " + str(self.work))
                    time.sleep(0.1)
            # Allow the tailer to discover the empty file before appending.
            time.sleep(0.5)
            common = {"timestamp": "2026-10-26T01:02:03.123Z", "level": "INFO",
                      "method": "GET", "route": "/fixture", "status": 200, "request_time": 0.125}
            records = [
                dict(common, fixture="valid", trace_id="a" * 32, span_id="b" * 16),
                dict(common, fixture="absent"),
                dict(common, fixture="invalid", trace_id="invalid", span_id="b" * 16),
                dict(common, fixture="zero", trace_id="0" * 32, span_id="0" * 16),
                dict(common, fixture="failure", level="ERROR", status=500, request_time=1.75),
            ]
            lines = [json.dumps(record) for record in records] + ["malformed-json-fixture"]
            with self.source.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
            deadline = time.monotonic() + 15
            exported = []
            resources = []
            while len(exported) < len(lines):
                self.assertIsNone(process.poll(), "Collector stopped; see " + str(self.work))
                if output.exists():
                    try:
                        batches = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line]
                        resources = [r for batch in batches for r in batch.get("resourceLogs", [])]
                        exported = [r for resource in resources for scope in resource.get("scopeLogs", [])
                                    for r in scope.get("logRecords", [])]
                    except json.JSONDecodeError:
                        pass  # A file exporter may be in the middle of writing a batch.
                if time.monotonic() >= deadline:
                    self.fail("Access logs not exported; see " + str(self.work))
                time.sleep(0.1)
            self.assertEqual(len(exported), len(lines))
            for resource in resources:
                attrs = {a["key"]: a["value"]["stringValue"] for a in resource["resource"]["attributes"]}
                self.assertEqual(attrs["service.name"], "fixture-web")
                self.assertEqual(attrs["service.namespace"], "fixture-namespace")
                self.assertEqual(attrs["deployment.environment.name"], "development")
                self.assertNotIn("service.version", attrs)
            parsed = {json.loads(log_record["body"]["stringValue"])["fixture"]: log_record
                      for log_record in exported if log_record["body"]["stringValue"] != lines[-1]}
            self.assertEqual(parsed["valid"]["traceId"], "a" * 32)
            self.assertEqual(parsed["valid"]["spanId"], "b" * 16)
            self.assertEqual(int(parsed["valid"]["timeUnixNano"]), 1792976523123000000)
            self.assertEqual(parsed["valid"]["severityNumber"], 9)
            self.assertEqual(parsed["failure"]["severityNumber"], 17)
            for fixture in ("absent", "invalid", "zero", "failure"):
                self.assertFalse(parsed[fixture].get("traceId"))
                self.assertFalse(parsed[fixture].get("spanId"))
            self.assertEqual(json.loads(parsed["failure"]["body"]["stringValue"])["request_time"], 1.75)
            self.assertTrue(any(item["body"]["stringValue"] == lines[-1] for item in exported))
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.close()


if __name__ == "__main__":
    unittest.main()
