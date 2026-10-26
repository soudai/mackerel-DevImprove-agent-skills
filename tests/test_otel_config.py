"""Optional real-Collector test: set OTELCOL_CONTRIB to Contrib 0.163.2.

All active exporters are replaced with local files. No Mackerel credentials or
network destinations are used by this test. The ordinary stdlib suite skips it
when the version-pinned Collector executable is unavailable.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "skills/mackerel-dev-improve/assets/otel-collector.example.yaml"
COLLECTOR = os.environ.get("OTELCOL_CONTRIB")


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(COLLECTOR, "Set OTELCOL_CONTRIB for the real Collector integration test")
class CollectorIntegrationTests(unittest.TestCase):
    def test_identity_correlation_redaction_and_internal_metrics(self):
        self.assertIn("0.163.2", subprocess.check_output(
            [COLLECTOR, "--version"], text=True, encoding="utf-8"))
        work = ROOT / ".quality/otel-tests"
        work.mkdir(parents=True, exist_ok=True)
        # This automatically generated directory is kept for diagnostics; no
        # configured target path is ever recursively deleted by the test.
        temp = Path(tempfile.mkdtemp(prefix="collector-", dir=work))
        grpc_port, http_port, metrics_port = free_port(), free_port(), free_port()
        outputs = {name: temp / (name + ".json") for name in ("traces", "logs", "metrics", "collector")}
        overrides = {
            "receivers": {
                "otlp": {"protocols": {
                    "grpc": {"endpoint": f"127.0.0.1:{grpc_port}"},
                    "http": {"endpoint": f"127.0.0.1:{http_port}"}}},
                "prometheus/collector": {"config": {"scrape_configs": [{
                    "job_name": "dev-otel-collector", "scrape_interval": "1s",
                    "static_configs": [{"targets": [f"127.0.0.1:{metrics_port}"]}]}]}}},
            "exporters": {"file/" + name: {"path": str(path), "flush_interval": "100ms"}
                          for name, path in outputs.items()},
            "service": {
                "telemetry": {"metrics": {"readers": [{"pull": {"exporter": {"prometheus": {
                    "host": "127.0.0.1", "port": metrics_port}}}}]}},
                "pipelines": {name if name != "collector" else "metrics/collector": {
                    "exporters": ["file/" + name]} for name in outputs}},
        }
        override_path = temp / "local-exporters.json"
        override_path.write_text(json.dumps(overrides), encoding="utf-8")
        environment = dict(os.environ, MACKEREL_APIKEY="validation-only-not-a-real-key",
                           OTELCOL_INSTANCE_ID="integration-test-collector")
        command = [COLLECTOR, "--config", str(CONFIG), "--config", str(override_path)]
        validated = subprocess.run(command + ["validate"], env=environment,
                                   capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(validated.returncode, 0, validated.stderr)
        log = (temp / "collector.log").open("wb")
        process = subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        opener = build_opener(ProxyHandler({}))
        try:
            deadline = time.monotonic() + 20
            while True:
                self.assertIsNone(process.poll(), "Collector stopped; see " + str(temp))
                try:
                    with opener.open(f"http://127.0.0.1:{metrics_port}/metrics", timeout=1):
                        break
                except (URLError, OSError):
                    if time.monotonic() >= deadline:
                        self.fail("Collector did not start; see " + str(temp))
                    time.sleep(0.1)

            attributes = [
                {"key": "service.name", "value": {"stringValue": "fixture-app"}},
                {"key": "service.namespace", "value": {"stringValue": "fixture-namespace"}},
                {"key": "service.version", "value": {"stringValue": "fixture-version"}},
                {"key": "deployment.environment.name", "value": {"stringValue": "development"}},
            ]
            record_attributes = [
                {"key": "http.request.header.authorization", "value": {"stringValue": "fixture-secret"}},
                {"key": "fixture.safe", "value": {"stringValue": "retained"}},
            ]
            stamp = time.time_ns()
            trace_id, span_id = "a" * 32, "b" * 16
            payloads = {
                "traces": {"resourceSpans": [{"resource": {"attributes": attributes}, "scopeSpans": [{"spans": [{
                    "traceId": trace_id, "spanId": span_id, "name": "fixture-operation", "kind": 2,
                    "startTimeUnixNano": str(stamp), "endTimeUnixNano": str(stamp + 1000000),
                    "attributes": record_attributes}]}]}]},
                "logs": {"resourceLogs": [{"resource": {"attributes": attributes}, "scopeLogs": [{"logRecords": [{
                    "timeUnixNano": str(stamp), "traceId": trace_id, "spanId": span_id,
                    "severityNumber": 17, "severityText": "ERROR", "body": {"stringValue": "fixture log"},
                    "attributes": record_attributes}]}]}]},
                "metrics": {"resourceMetrics": [{"resource": {"attributes": attributes}, "scopeMetrics": [{"metrics": [{
                    "name": "fixture.metric", "gauge": {"dataPoints": [{"timeUnixNano": str(stamp), "asInt": "1"}]}}]}]}]},
            }
            for signal, payload in payloads.items():
                request = Request(f"http://127.0.0.1:{http_port}/v1/{signal}",
                                  data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with opener.open(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)

            deadline = time.monotonic() + 15
            while not all(path.exists() and path.stat().st_size for path in outputs.values()):
                self.assertIsNone(process.poll(), "Collector stopped; see " + str(temp))
                if time.monotonic() >= deadline:
                    self.fail("Exporter output missing; see " + str(temp))
                time.sleep(0.1)
            for signal in ("traces", "logs", "metrics"):
                result = outputs[signal].read_text(encoding="utf-8")
                self.assertIn("fixture-app", result)
                self.assertIn("fixture-namespace", result)
                self.assertIn("fixture-version", result)
                self.assertNotIn("dev-otel-collector", result)
                if signal != "metrics":
                    self.assertNotIn("fixture-secret", result)
                    self.assertNotIn("http.request.header.authorization", result)
                    self.assertIn(trace_id, result)
                    self.assertIn(span_id, result)
                    self.assertIn("retained", result)
            internal = outputs["collector"].read_text(encoding="utf-8")
            self.assertIn("dev-otel-collector", internal)
            self.assertIn("integration-test-collector", internal)
            self.assertIn("otelcol_", internal)
            self.assertNotIn("validation-only-not-a-real-key", internal)
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
