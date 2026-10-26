"""Compare actual normalizer output without accessing Mackerel or credentials."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/mackerel-dev-improve/scripts"


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


compare = module("compare")
normalizer = module("normalize")


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quality comparison ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "schema_version": 1, "service": "example", "environment": "development",
            "mackerel_service": "example-dev",
            "profiles": {
                "unit": {"kind": "test_cases", "contract": "test-cases/v1", "producer": "runner", "version": "1",
                         "converter": {"name": "converter", "version": "1"}, "selection": "all unit tests", "completed_exit_codes": [0, 1]},
                "static": {"kind": "cccc", "producer": "moznion/cccc", "version": "1.8.3", "language": "php",
                           "selection": "all source files", "cognitive_limit": 15, "cyclomatic_limit": 10}}}
        self.run = {"revision": "a" * 40, "base_revision": None, "run_id": "1", "attempt_id": "1",
                    "started_at": "2026-10-26T01:00:00Z", "finished_at": "2026-10-26T01:01:00Z",
                    "working_tree_dirty": False, "artifact_uri": "https://example.com/artifacts/1",
                    "profiles": {
                        "unit": {"report": "tests.json", "version": "1", "exit_code": 0, "run_status": "completed",
                                 "expected_cases": 2, "test_plan_complete": True},
                        "static": {"report": "cccc.json", "version": "1.8.3", "exit_code": 0, "expected_files": ["src/example.php"]}}}

    def bundle(self, name, *, states=("passed", "passed"), complexity=(20, 1), config=None, run=None, ids=None):
        directory = self.root / name
        directory.mkdir()
        config = copy.deepcopy(config or self.config)
        run = copy.deepcopy(run or self.run)
        ids = ids or ["case-" + str(index) for index in range(len(states))]
        test_report = {"schema_version": 1, "producer": {"name": "runner", "version": "1"},
                       "converter": {"name": "converter", "version": "1"},
                       "cases": [{"id": key, "status": status} for key, status in zip(ids, states)]}
        run["profiles"]["unit"]["expected_cases"] = len(states)
        if "failed" in states or "error" in states:
            run["profiles"]["unit"]["exit_code"] = 1
        functions = [{"name": "function" + str(index), "kind": "function", "line": index + 1,
                      "cognitive": value, "cyclomatic": value + 1} for index, value in enumerate(complexity)]
        summary = {"file_count": 1, "function_count": len(functions), "parse_error_count": 0, "parse_error_file_count": 0}
        for metric in ("cognitive", "cyclomatic"):
            values = [function[metric] for function in functions]
            summary[metric] = {"sum": sum(values), "max": max(values, default=0), "median": normalizer.percentile(values, .5),
                               "p90": normalizer.percentile(values, .9), "p95": normalizer.percentile(values, .95)}
        static_report = {"files": [{"path": "src/example.php", "cognitive": sum(complexity),
                                    "cyclomatic": sum(complexity) + len(complexity), "functions": functions}], "summary": summary}
        (directory / "tests.json").write_bytes(normalizer.json_bytes(test_report))
        (directory / "cccc.json").write_bytes(normalizer.json_bytes(static_report))
        if "unit" not in config["profiles"]:
            run["profiles"].pop("unit")
        if "static" not in config["profiles"]:
            run["profiles"].pop("static")
        manifest, details, metrics = normalizer.normalize(config, run, directory)
        self.write(directory, manifest, details, metrics)
        return directory

    def write(self, directory, manifest, details, metrics):
        payload = normalizer.json_bytes(metrics)
        data = normalizer.json_bytes(details)
        manifest["publication"].update(payload_sha256=hashlib.sha256(payload).hexdigest(),
                                       series=[metric["name"] for metric in metrics])
        manifest["artifacts"]["details.json"] = {"uri": "details.json", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        (directory / "details.json").write_bytes(data)
        (directory / "metrics.json").write_bytes(payload)
        (directory / "manifest.json").write_bytes(normalizer.json_bytes(manifest))

    def rewrite(self, directory, change):
        files = [json.loads((directory / file).read_text(encoding="utf-8")) for file in ("manifest.json", "details.json", "metrics.json")]
        change(*files)
        self.write(directory, *files)

    def test_lower_complexity_with_passing_same_tests_is_measured_improvement(self):
        base = self.bundle("base")
        head = self.bundle("head", complexity=(10, 1))
        result = compare.compare(base, head)
        self.assertEqual("improve", result["verdict"])
        self.assertEqual("unchanged", result["profiles"]["unit"]["verdict"])
        metric = next(row for row in result["profiles"]["static"]["metrics"] if row["name"].endswith("cognitive.max"))
        self.assertEqual(-10, metric["delta"])

    def test_one_function_regression_is_not_hidden_by_lower_p95(self):
        result = compare.compare(self.bundle("base"), self.bundle("head", complexity=(10, 2)))
        self.assertEqual("regression", result["verdict"])

    def test_new_test_failure_overrides_complexity_improvement(self):
        result = compare.compare(self.bundle("base"), self.bundle("head", complexity=(10, 1), states=("passed", "failed")))
        self.assertEqual("regression", result["verdict"])
        self.assertEqual(0, result["profiles"]["unit"]["head_success"])

    def test_existing_failures_cannot_become_overall_improvement(self):
        result = compare.compare(self.bundle("base", states=("failed", "failed")),
                                 self.bundle("head", complexity=(10, 1), states=("passed", "failed")))
        self.assertEqual("insufficient_evidence", result["verdict"])
        self.assertIn("head_run_still_failed", result["profiles"]["unit"]["reasons"])

    def test_repaired_same_tests_can_show_improvement(self):
        result = compare.compare(self.bundle("base", states=("passed", "failed")), self.bundle("head"))
        self.assertEqual("improve", result["verdict"])

    def test_failure_replaced_with_skip_is_not_improvement(self):
        result = compare.compare(self.bundle("base", states=("passed", "failed")),
                                 self.bundle("head", states=("passed", "skipped")))
        self.assertEqual("insufficient_evidence", result["verdict"])

    def test_all_skipped_is_not_100_percent_success(self):
        result = compare.compare(self.bundle("base", states=("skipped", "skipped")),
                                 self.bundle("head", states=("skipped", "skipped")))
        self.assertEqual("insufficient_evidence", result["verdict"])
        self.assertEqual({"base": 0, "head": 0}, result["profiles"]["unit"]["denominators"])

    def test_case_replacement_same_count_is_incomparable(self):
        result = compare.compare(self.bundle("base"), self.bundle("head", ids=["case-0", "replacement"]))
        self.assertEqual("incomparable", result["verdict"])
        self.assertEqual(1, result["profiles"]["unit"]["population"]["added"])

    def test_definition_change_is_incomparable(self):
        config = copy.deepcopy(self.config)
        config["profiles"]["static"]["cognitive_limit"] = 50
        result = compare.compare(self.bundle("base"), self.bundle("head", config=config))
        self.assertEqual("incomparable", result["verdict"])

    def test_missing_profile_does_not_disappear_from_comparison(self):
        config = copy.deepcopy(self.config)
        del config["profiles"]["static"]
        result = compare.compare(self.bundle("base"), self.bundle("head", config=config))
        self.assertEqual("incomparable", result["verdict"])
        self.assertIn("static", result["profiles"])

    def test_incomplete_run_has_null_missing_values_and_no_improvement(self):
        run = copy.deepcopy(self.run)
        run["profiles"]["unit"]["test_plan_complete"] = False
        result = compare.compare(self.bundle("base"), self.bundle("head", run=run, complexity=(10, 1)))
        self.assertEqual("insufficient_evidence", result["verdict"])
        selected = next(row for row in result["profiles"]["unit"]["metrics"] if row["name"].endswith("cases.selected"))
        self.assertIsNone(selected["head"])
        self.assertIsNone(selected["delta"])

    def test_gate_failure_is_not_overridden_by_passed_cases(self):
        run = copy.deepcopy(self.run)
        run["profiles"]["unit"]["gate_exit_code"] = 1
        result = compare.compare(self.bundle("base"), self.bundle("head", run=run, complexity=(10, 1)))
        self.assertEqual("regression", result["verdict"])

    def test_analysis_gate_failure_is_preserved_even_when_complexity_decreases(self):
        run = copy.deepcopy(self.run)
        run["profiles"]["static"]["gate_exit_code"] = 1
        result = compare.compare(self.bundle("base"), self.bundle("head", run=run, complexity=(10, 1)))
        self.assertEqual("insufficient_evidence", result["verdict"])
        self.assertIn("head_analysis_gate_failed", result["profiles"]["static"]["reasons"])

    def test_service_environment_and_revision_lineage_are_checked(self):
        base = self.bundle("base")
        for index, field in enumerate(("service", "environment", "mackerel_service")):
            config = copy.deepcopy(self.config)
            config[field] = "different"
            self.assertEqual("incomparable", compare.compare(base, self.bundle(str(index), config=config))["verdict"])
        run = copy.deepcopy(self.run)
        run["base_revision"] = "b" * 40
        self.assertEqual("incomparable", compare.compare(base, self.bundle("lineage", run=run))["verdict"])

    def test_dirty_tree_and_complexity_without_tests_cannot_claim_verified_improvement(self):
        base = self.bundle("base")
        run = copy.deepcopy(self.run)
        run["working_tree_dirty"] = True
        self.assertEqual("insufficient_evidence", compare.compare(base, self.bundle("dirty", run=run, complexity=(10, 1)))["verdict"])
        config = copy.deepcopy(self.config)
        del config["profiles"]["unit"]
        result = compare.compare(self.bundle("static-base", config=config), self.bundle("static-head", config=config, complexity=(10, 1)))
        self.assertEqual("insufficient_evidence", result["verdict"])

    def test_tampered_details_and_missing_required_series_are_rejected(self):
        base, head = self.bundle("base"), self.bundle("head")
        (head / "details.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(compare.publisher.PublicationError):
            compare.compare(base, head)
        missing = self.bundle("missing")
        self.rewrite(missing, lambda manifest, details, metrics: metrics.pop(next(index for index, item in enumerate(metrics)
                                                                                  if item["name"] == "tests.unit.cases.failed")))
        with self.assertRaises(compare.publisher.PublicationError):
            compare.compare(base, missing)

    def test_cli_from_another_directory_and_existing_output_preserved(self):
        base, head = self.bundle("base"), self.bundle("head", complexity=(10, 1))
        output = self.root / "comparison result.json"
        command = [sys.executable, str(SCRIPTS / "compare.py"), "--base", str(base), "--head", str(head), "--out", str(output)]
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        original = output.read_bytes()
        self.assertEqual("improve", json.loads(original)["verdict"])
        repeated = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(3, repeated.returncode)
        self.assertEqual(original, output.read_bytes())


if __name__ == "__main__":
    unittest.main()
