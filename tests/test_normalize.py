"""Behavioral fixtures for the explicitly supported report dialects."""

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/mackerel-dev-improve/scripts/normalize.py"
SPEC = importlib.util.spec_from_file_location("normalize", SCRIPT)
normalize = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(normalize)


def junit(cases, *, failures=0, errors=0, skipped=0, count=None):
    number = len(cases) if count is None else count
    summary = f'tests="{number}" failures="{failures}" errors="{errors}" skipped="{skipped}"'
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<testsuites><testsuite name="full" ' + summary + '>'
            '<testsuite name="ExampleTest" ' + summary + '>'
            + ''.join(f'<testcase name="test_{index}" class="ExampleTest" file="tests/ExampleTest.php">{body}</testcase>'
                      for index, body in enumerate(cases))
            + '</testsuite></testsuite></testsuites>').encode()


def cccc_fixture(*, empty_functions=False):
    child = {"name": "<closure>", "kind": "closure", "line": 3,
             "cognitive": 16, "cyclomatic": 11}
    parent = {"name": "work", "kind": "function", "line": 1,
              "cognitive": 0, "cyclomatic": 1, "children": [child]}
    values = {"cognitive": [0, 16], "cyclomatic": [1, 11]}
    summary = {"file_count": 1, "function_count": 0 if empty_functions else 2,
               "parse_error_count": 0, "parse_error_file_count": 0}
    for metric, measurements in values.items():
        measurements = [] if empty_functions else measurements
        summary[metric] = {"sum": sum(measurements), "max": max(measurements, default=0),
                           "median": normalize.percentile(measurements, .5),
                           "p90": normalize.percentile(measurements, .9),
                           "p95": normalize.percentile(measurements, .95)}
    return {"files": [{"path": "src/a.php", "cognitive": 16, "cyclomatic": 12,
                       "functions": [] if empty_functions else [parent]}], "summary": summary}


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {
            "schema_version": 1, "service": "sample", "environment": "test",
            "mackerel_service": "sample-ci", "profiles": {
                "unit": {"kind": "junit", "producer": "phpunit", "version": "12.5.40",
                         "selection": "full_suite"},
                "php_static": {"kind": "cccc", "producer": "moznion/cccc", "version": "1.8.3",
                               "language": "php", "selection": "all src PHP files",
                               "cognitive_limit": 15, "cyclomatic_limit": 10}}}
        self.run = {"revision": "a" * 40, "base_revision": None, "run_id": "123", "attempt_id": "1",
                    "started_at": "2026-10-26T01:00:00Z", "finished_at": "2026-10-26T01:01:00Z",
                    "profiles": {
                        "unit": {"report": "junit.xml", "version": "12.5.40", "exit_code": 0,
                                 "test_plan_complete": True},
                        "php_static": {"report": "cccc.json", "version": "1.8.3", "exit_code": 1,
                                       "expected_files": ["src/a.php"]}}}
        self.write_junit(junit(["", ""]))
        self.write_cccc(cccc_fixture())

    def write_junit(self, data):
        (self.root / "junit.xml").write_bytes(data)

    def write_cccc(self, data):
        (self.root / "cccc.json").write_text(json.dumps(data), encoding="utf-8")

    def collect(self):
        manifest, details, metrics = normalize.normalize(self.config, self.run, self.root)
        return manifest, details, {metric["name"]: metric["value"] for metric in metrics}

    def assert_no_cases(self, metrics):
        self.assertFalse(any(name.startswith("tests.unit.cases.") for name in metrics))

    def assert_no_distributions(self, metrics):
        self.assertFalse(any(name.startswith("cccc.php_static.cognitive.") for name in metrics))

    def test_full_reports_recurse_without_double_counting_and_keep_zero(self):
        manifest, details, metrics = self.collect()
        self.assertTrue(all(result["complete"] for result in manifest["collection"].values()))
        self.assertEqual(2, metrics["tests.unit.cases.selected"])
        self.assertEqual(2, metrics["cccc.php_static.analysis.functions"])
        self.assertEqual(.5, metrics["cccc.php_static.cognitive.over_limit_ratio"])
        self.assertEqual(16, metrics["cccc.php_static.cognitive.p95"])
        self.assertEqual(0, details["profiles"]["php_static"]["records"][0]["cognitive"])
        self.assertNotIn("tests.unit.attempts.retries", metrics)
        self.assertNotIn("tests.unit.cases.executed", metrics)

    def test_failed_full_suite_is_complete_and_not_successful(self):
        self.write_junit(junit(["", "<failure>secret failure body</failure>", "<error>secret error</error>",
                                "<skipped/>"], failures=1, errors=1, skipped=1))
        self.run["profiles"]["unit"]["exit_code"] = 2
        manifest, details, metrics = self.collect()
        self.assertTrue(manifest["collection"]["unit"]["complete"])
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.assertEqual(1 / 3, metrics["tests.unit.ratios.strict_pass"])
        self.assertNotIn("secret", json.dumps(details))
        self.assertEqual(3, details["profiles"]["unit"]["strict_pass"]["denominator"])

    def test_all_skipped_has_no_pass_ratio_or_execution_estimate(self):
        self.write_junit(junit(["<skipped/>", "<skipped/>"], skipped=2))
        _, _, metrics = self.collect()
        self.assertNotIn("tests.unit.ratios.strict_pass", metrics)
        self.assertNotIn("tests.unit.cases.executed", metrics)
        self.assertEqual(2, metrics["tests.unit.cases.skipped"])

    def test_duplicate_case_is_rejected_not_merged(self):
        self.write_junit(junit(["", ""]).replace(b'test_1', b'test_0'))
        manifest, _, metrics = self.collect()
        self.assertEqual("error", manifest["collection"]["unit"]["status"])
        self.assert_no_cases(metrics)

    def test_parameter_names_produce_distinct_hashed_ids(self):
        self.write_junit(junit(["", ""]).replace(b'test_0', b'test_param with data set #0')
                         .replace(b'test_1', b'test_param with data set #1'))
        _, details, metrics = self.collect()
        self.assertEqual(2, metrics["tests.unit.cases.selected"])
        records = details["profiles"]["unit"]["records"]
        self.assertNotEqual(records[0]["entity_id"], records[1]["entity_id"])
        self.assertNotIn("test_param", json.dumps(details))

    def test_summary_mismatch_prevents_case_publication(self):
        self.write_junit(junit([""], count=2))
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["tests.unit.run.report_complete"])
        self.assert_no_cases(metrics)

    def test_missing_report_and_unknown_runner_does_not_fabricate_failure(self):
        (self.root / "junit.xml").unlink()
        self.run["profiles"]["unit"]["exit_code"] = None
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["tests.unit.run.complete"])
        self.assertNotIn("tests.unit.run.success", metrics)

    def test_missing_report_with_confirmed_runner_failure_records_failure(self):
        (self.root / "junit.xml").unlink()
        self.run["profiles"]["unit"]["exit_code"] = 1
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.assert_no_cases(metrics)

    def test_unconfirmed_plan_is_partial_even_with_valid_xml(self):
        self.run["profiles"]["unit"]["test_plan_complete"] = False
        _, _, metrics = self.collect()
        self.assertEqual(1, metrics["tests.unit.run.report_complete"])
        self.assertEqual(0, metrics["tests.unit.run.complete"])
        self.assertNotIn("tests.unit.run.success", metrics)
        self.assert_no_cases(metrics)

    def test_aborted_run_preserves_observed_cases_only_in_details(self):
        self.run["profiles"]["unit"]["run_status"] = "aborted"
        _, details, metrics = self.collect()
        self.assertEqual(2, len(details["profiles"]["unit"]["records"]))
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.assert_no_cases(metrics)

    def test_zero_cases_cannot_be_a_successful_plan(self):
        self.write_junit(junit([]))
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["tests.unit.run.complete"])
        self.assertNotIn("tests.unit.run.success", metrics)

    def test_unsupported_retry_and_xfail_dialects_are_rejected(self):
        for extension in ("rerunFailure", "flakyFailure", "xfail", "xpass"):
            with self.subTest(extension=extension):
                self.write_junit(junit([f"<{extension}/>"]))
                _, _, metrics = self.collect()
                self.assert_no_cases(metrics)

    def test_dtd_entities_and_utf16_are_rejected(self):
        for data in (b'<!DOCTYPE testsuite [<!ENTITY x "bad">]>' + junit([""]),
                     junit([""]).decode().encode("utf-16")):
            with self.subTest(data=data[:15]):
                self.write_junit(data)
                _, _, metrics = self.collect()
                self.assert_no_cases(metrics)

    def test_gates_and_measurement_completeness_are_independent(self):
        self.run["profiles"]["unit"]["gate_exit_code"] = 1
        _, _, metrics = self.collect()
        self.assertEqual(1, metrics["tests.unit.run.complete"])
        self.assertEqual(0, metrics["tests.unit.run.success"])

    def test_wrapper_cleanup_failure_preserves_runner_success_and_failed_gate(self):
        self.run["test_wrapper_exit_code"] = 1
        manifest, _, metrics = self.collect()
        self.assertEqual(1, manifest["test_wrapper_exit_code"])
        self.assertEqual(0, manifest["collection"]["unit"]["exit_code"])
        self.assertEqual(1, manifest["collection"]["unit"]["wrapper_exit_code"])
        self.assertEqual(1, metrics["tests.unit.run.complete"])
        self.assertEqual(0, metrics["tests.unit.run.success"])

    def test_wrapper_precondition_failure_before_runner_is_still_known_failure(self):
        self.run["test_wrapper_exit_code"] = 1
        self.run["profiles"].pop("unit")
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.assert_no_cases(metrics)

    def test_cccc_parse_errors_are_partial_with_no_canonical_distribution(self):
        report = cccc_fixture()
        report["files"][0]["parse_errors"] = ["unexpected token"]
        report["summary"].update(parse_error_count=1, parse_error_file_count=1,
                                  parse_error_files=["src/a.php"])
        self.write_cccc(report)
        manifest, details, metrics = self.collect()
        self.assertEqual("partial", manifest["collection"]["php_static"]["status"])
        self.assertEqual(2, len(details["profiles"]["php_static"]["records"]))
        self.assertEqual(0, metrics["cccc.php_static.analysis.complete"])
        self.assert_no_distributions(metrics)

    def test_cccc_input_manifest_detects_missing_files(self):
        self.run["profiles"]["php_static"]["expected_files"].append("src/missing.php")
        _, _, metrics = self.collect()
        self.assert_no_distributions(metrics)
        self.assertEqual(0, metrics["cccc.php_static.analysis.complete"])

    def test_cccc_filtered_top_and_inconsistent_summary_are_rejected(self):
        for mutate in (
            lambda report: report.update(top=[]),
            lambda report: report["files"][0]["functions"][0].pop("children"),
            lambda report: report["summary"]["cognitive"].update(p95=1),
            lambda report: report["summary"].pop("parse_error_count"),
        ):
            with self.subTest(mutation=mutate):
                report = cccc_fixture()
                mutate(report)
                self.write_cccc(report)
                _, _, metrics = self.collect()
                self.assert_no_distributions(metrics)

    def test_cccc_zero_functions_has_counts_but_no_distribution(self):
        self.write_cccc(cccc_fixture(empty_functions=True))
        _, _, metrics = self.collect()
        self.assertEqual(1, metrics["cccc.php_static.analysis.complete"])
        self.assertEqual(0, metrics["cccc.php_static.analysis.functions"])
        self.assert_no_distributions(metrics)

    def test_cccc_threshold_boundary_is_strict(self):
        self.config["profiles"]["php_static"]["cognitive_limit"] = 16
        self.config["profiles"]["php_static"]["cyclomatic_limit"] = 11
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["cccc.php_static.cognitive.over_limit_count"])
        self.assertEqual(0, metrics["cccc.php_static.cyclomatic.over_limit_count"])

    def test_version_change_needs_explicit_adapter_validation(self):
        self.run["profiles"]["php_static"]["version"] = "1.9.0"
        _, _, metrics = self.collect()
        self.assert_no_distributions(metrics)

    def test_non_finite_and_duplicate_json_fields_are_rejected(self):
        for data in ('{"a": NaN}', '{"a": 1, "a": 2}'):
            with self.assertRaises(normalize.InvalidInput):
                normalize.parse_json(data)

    def test_oversized_numeric_fields_fail_without_crashing(self):
        report = cccc_fixture()
        report["summary"]["cognitive"]["max"] = 10 ** 1000
        self.write_cccc(report)
        _, _, metrics = self.collect()
        self.assert_no_distributions(metrics)
        self.write_junit(junit([""]).replace(b'tests="1"', b'tests="' + b'9' * 5000 + b'"'))
        _, _, metrics = self.collect()
        self.assert_no_cases(metrics)
        with self.assertRaises(normalize.InvalidInput):
            normalize.parse_json('{"number":' + '9' * 5000 + '}')

    def test_unknown_run_profile_cannot_create_arbitrary_metric_series(self):
        self.run["profiles"]["pr_999"] = self.run["profiles"]["unit"]
        with self.assertRaises(normalize.InvalidInput):
            self.collect()

    def test_comparison_ignores_revision_but_includes_ruleset(self):
        first, _, _ = self.collect()
        self.run["revision"] = "b" * 40
        second, _, _ = self.collect()
        self.assertEqual(first["comparison_key"], second["comparison_key"])
        self.run["profiles"]["php_static"]["ruleset_hash"] = "c" * 64
        third, _, _ = self.collect()
        self.assertNotEqual(second["comparison_key"], third["comparison_key"])
        self.assertEqual(second["profiles"]["unit"]["comparison_key"], third["profiles"]["unit"]["comparison_key"])

    def test_duration_uses_profile_wall_time_not_sum_or_overall_run(self):
        self.run["profiles"]["unit"].update(started_at="2026-10-26T01:00:15Z", finished_at="2026-10-26T01:00:20Z")
        _, _, metrics = self.collect()
        self.assertEqual(5, metrics["tests.unit.run.duration_seconds"])

    def test_negative_signal_is_known_failure_and_not_a_complete_plan(self):
        self.run["profiles"]["unit"]["exit_code"] = -9
        manifest, _, metrics = self.collect()
        self.assertEqual(-9, manifest["collection"]["unit"]["exit_code"])
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.assertEqual(0, metrics["tests.unit.run.complete"])
        self.assert_no_cases(metrics)

    def test_negative_wrapper_or_gate_exit_preserves_failure(self):
        self.run["test_wrapper_exit_code"] = -15
        manifest, _, metrics = self.collect()
        self.assertEqual(-15, manifest["collection"]["unit"]["wrapper_exit_code"])
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.run["test_wrapper_exit_code"] = None
        self.run["profiles"]["unit"]["gate_exit_code"] = -9
        manifest, _, metrics = self.collect()
        self.assertEqual(-9, manifest["collection"]["unit"]["gate_exit_code"])
        self.assertEqual(0, metrics["tests.unit.run.success"])

    def test_cccc_selection_must_be_pinned(self):
        for selection in (None, "", "   "):
            with self.subTest(selection=selection):
                self.config["profiles"]["php_static"]["selection"] = selection
                with self.assertRaises(normalize.InvalidInput):
                    self.collect()

    def test_profile_timestamp_pair_and_run_bounds_are_checked(self):
        for times in (
            {"started_at": "2026-10-26T01:00:00Z"},
            {"finished_at": "2026-10-26T01:01:00Z"},
            {"started_at": "2026-10-26T00:59:00Z", "finished_at": "2026-10-26T01:00:30Z"},
            {"started_at": "2026-10-26T01:00:00Z", "finished_at": "2026-10-26T01:02:00Z"},
        ):
            with self.subTest(times=times):
                entry = self.run["profiles"]["unit"]
                entry.pop("started_at", None)
                entry.pop("finished_at", None)
                entry.update(times)
                manifest, _, metrics = self.collect()
                self.assertEqual("error", manifest["collection"]["unit"]["status"])
                self.assert_no_cases(metrics)
                self.assertNotIn("tests.unit.run.duration_seconds", metrics)

    def test_changed_sidecar_invalidates_measurement(self):
        sidecar = self.root / "version.txt"
        sidecar.write_bytes(b"cccc 1.8.3\n")
        self.run["profiles"]["php_static"]["raw_artifacts"] = {
            "version.txt": {"path": "version.txt", "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest()}}
        _, _, metrics = self.collect()
        self.assertEqual(1, metrics["cccc.php_static.analysis.complete"])
        sidecar.write_bytes(b"cccc 1.9.0\n")
        _, _, metrics = self.collect()
        self.assertEqual(0, metrics["cccc.php_static.analysis.complete"])
        self.assert_no_distributions(metrics)

    def test_cli_artifact_checksums_and_fresh_output(self):
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.root / "run.json").write_text(json.dumps(self.run), encoding="utf-8")
        command = [sys.executable, str(SCRIPT), "--config", str(self.root / "config.json"),
                   "--run", str(self.root / "run.json"), "--output", str(self.root / "normalized")]
        result = subprocess.run(command, capture_output=True, text=True, cwd=self.root.parent)
        self.assertEqual(0, result.returncode, result.stderr)
        manifest = json.loads((self.root / "normalized/manifest.json").read_text(encoding="utf-8"))
        payload = (self.root / "normalized/metrics.json").read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), manifest["publication"]["payload_sha256"])
        self.assertEqual(hashlib.sha256((self.root / "junit.xml").read_bytes()).hexdigest(),
                         manifest["artifacts"]["unit_raw"]["sha256"])
        again = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(2, again.returncode)

    def test_incomplete_cli_still_writes_evidence_and_returns_one(self):
        self.run["profiles"]["unit"]["test_plan_complete"] = False
        for name, data in (("config.json", self.config), ("run.json", self.run)):
            (self.root / name).write_text(json.dumps(data), encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.root / "config.json"),
                                 "--run", str(self.root / "run.json"), "--output", str(self.root / "out")],
                                capture_output=True, text=True)
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertTrue((self.root / "out/manifest.json").is_file())


class PortableTestCasesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.profile = {
            "kind": "test_cases", "contract": "test-cases/v1",
            "producer": "example-runner", "version": "4.2.0", "selection": "all unit tests",
            "converter": {"name": "example-converter", "version": "1.0.0"},
            "completed_exit_codes": [0, 1],
        }
        self.config = {"schema_version": 1, "service": "sample", "environment": "test",
                       "mackerel_service": "sample-ci", "profiles": {"unit": self.profile}}
        self.entry = {"report": "cases.json", "version": "4.2.0", "exit_code": 0,
                      "run_status": "completed", "test_plan_complete": True, "expected_cases": 2}
        self.run = {"revision": "a" * 40, "run_id": "1", "attempt_id": "1",
                    "started_at": "2026-10-26T01:00:00Z", "finished_at": "2026-10-26T01:01:00Z",
                    "profiles": {"unit": self.entry}}
        self.report = {"schema_version": 1,
                       "producer": {"name": "example-runner", "version": "4.2.0"},
                       "converter": {"name": "example-converter", "version": "1.0.0"},
                       "cases": [{"id": "suite::case[secret-parameter]", "status": "passed"},
                                 {"id": "suite::case[other]", "status": "skipped"}]}

    def collect(self):
        (self.root / "cases.json").write_text(json.dumps(self.report), encoding="utf-8")
        manifest, details, metrics = normalize.normalize(self.config, self.run, self.root)
        return manifest, details, {metric["name"]: metric["value"] for metric in metrics}

    def assert_incomplete(self):
        manifest, details, metrics = self.collect()
        self.assertEqual(0, metrics["tests.unit.run.complete"])
        self.assertFalse(any(name.startswith("tests.unit.cases.") for name in metrics))
        self.assertNotEqual(1, metrics.get("tests.unit.run.success"))
        return manifest, details, metrics

    def test_portable_complete_run_hashes_ids_and_preserves_known_counts(self):
        manifest, details, metrics = self.collect()
        self.assertEqual(1, metrics["tests.unit.run.success"])
        self.assertEqual(2, metrics["tests.unit.cases.selected"])
        self.assertEqual(1, metrics["tests.unit.cases.passed"])
        self.assertEqual(1, metrics["tests.unit.cases.skipped"])
        self.assertEqual(1, metrics["tests.unit.ratios.strict_pass"])
        self.assertEqual(2, manifest["collection"]["unit"]["expected_cases"])
        self.assertNotIn("secret-parameter", json.dumps(details))
        self.assertNotIn("tests.unit.cases.executed", metrics)
        self.assertNotIn("tests.unit.attempts.retries", metrics)

    def test_portable_failed_plan_is_complete_with_a_failed_run(self):
        self.report["cases"][1]["status"] = "error"
        self.entry["exit_code"] = 1
        manifest, _, metrics = self.collect()
        self.assertTrue(manifest["collection"]["unit"]["complete"])
        self.assertEqual(0, metrics["tests.unit.run.success"])
        self.assertEqual(.5, metrics["tests.unit.ratios.strict_pass"])

    def test_case_count_is_not_inferred_from_arrived_records(self):
        for expected in (None, 0, 1, 3, True):
            with self.subTest(expected=expected):
                self.entry["expected_cases"] = expected
                self.assert_incomplete()

    def test_selected_count_alone_does_not_prove_plan_completion(self):
        for status, plan in (("running", True), ("aborted", True), ("unknown", True), ("completed", False)):
            with self.subTest(status=status, plan=plan):
                self.entry.update(run_status=status, test_plan_complete=plan)
                _, details, _ = self.assert_incomplete()
                self.assertEqual(2, len(details["profiles"]["unit"]["records"]))

    def test_run_status_and_nonterminal_exits_require_explicit_evidence(self):
        self.entry.pop("run_status")
        self.assert_incomplete()
        self.entry["run_status"] = "completed"
        for code in (None, 2, -9, 4294967295):
            with self.subTest(code=code):
                self.entry["exit_code"] = code
                manifest, _, metrics = self.assert_incomplete()
                self.assertEqual(code, manifest["collection"]["unit"]["exit_code"])
                self.assertEqual(None if code is None else 0, metrics.get("tests.unit.run.success"))

    def test_unsupported_statuses_and_retry_details_are_rejected(self):
        for status in ("unknown", "not_run", "interrupted", "xfail", "xpass", "flaky", None):
            with self.subTest(status=status):
                self.report["cases"][0]["status"] = status
                self.assert_incomplete()
        self.report["cases"][0]["status"] = "passed"
        self.report["cases"][0]["attempts"] = [{"status": "failed"}, {"status": "passed"}]
        self.assert_incomplete()

    def test_duplicate_ids_cannot_hide_retries_or_shard_overlap(self):
        self.report["cases"][1]["id"] = self.report["cases"][0]["id"]
        self.assert_incomplete()

    def test_exit_zero_cannot_override_failed_records(self):
        self.report["cases"][0]["status"] = "failed"
        manifest, _, metrics = self.assert_incomplete()
        self.assertIn("runner_success_disagrees_with_cases", manifest["collection"]["unit"]["diagnostics"])

    def test_report_identity_schema_and_unknown_fields_are_checked(self):
        import copy
        original = copy.deepcopy(self.report)
        for mutate in (
            lambda report: report.update(schema_version=2),
            lambda report: report.update(schema_version=True),
            lambda report: report.update(retries=1),
            lambda report: report["producer"].update(version="4.3.0"),
            lambda report: report["converter"].update(version="2.0.0"),
            lambda report: report["producer"].update(extra="unrecognized"),
        ):
            self.report = copy.deepcopy(original)
            mutate(self.report)
            self.assert_incomplete()

    def test_profile_requires_pinned_converter_contract_and_exit_policy(self):
        import copy
        original = copy.deepcopy(self.profile)
        for mutate in (
            lambda profile: profile.pop("converter"),
            lambda profile: profile.update(contract="test-cases/v2"),
            lambda profile: profile.update(completed_exit_codes=[1]),
            lambda profile: profile.update(completed_exit_codes=[0, 0]),
            lambda profile: profile.update(completed_exit_codes=[0, -9]),
            lambda profile: profile.update(completed_exit_codes=[0, True]),
        ):
            self.config["profiles"]["unit"] = copy.deepcopy(original)
            mutate(self.config["profiles"]["unit"])
            with self.assertRaises(normalize.InvalidInput):
                self.collect()

    def test_converter_and_exit_policy_changes_start_a_distinct_baseline(self):
        first, _, _ = self.collect()
        self.profile["converter"]["version"] = "1.0.1"
        self.report["converter"]["version"] = "1.0.1"
        second, _, _ = self.collect()
        self.assertNotEqual(first["comparison_key"], second["comparison_key"])
        self.assertNotEqual(first["profiles"]["unit"]["comparison_key"], second["profiles"]["unit"]["comparison_key"])
        self.profile["completed_exit_codes"].append(2)
        third, _, _ = self.collect()
        self.assertNotEqual(second["comparison_key"], third["comparison_key"])


if __name__ == "__main__":
    unittest.main()
