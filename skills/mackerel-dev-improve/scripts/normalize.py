#!/usr/bin/env python3
"""Normalize test-cases/v1, PHPUnit 12.5 JUnit and CCCC 1.8.3 (stdlib only).

Paths in the run manifest are relative to that manifest, not the working directory.
Test profiles support one non-retrying invocation. The portable JSON contract
lets validated runner-specific converters use the same measurement pipeline.
It does not infer retry, execution, coverage or defect measurements.
"""

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import sys
import xml.etree.ElementTree as ET


ADAPTER_VERSION = "2"
MAX_REPORT_BYTES = 64 * 1024 * 1024
PROFILE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
REVISION_RE = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")


class InvalidInput(ValueError):
    """An input cannot safely contribute canonical measurements."""


def require(condition, message):
    if not condition:
        raise InvalidInput(message)


def integer(value):
    return type(value) is int and 0 <= value <= 2 ** 53 - 1


def process_exit_code(value):
    # POSIX subprocess APIs use negative signal numbers; Windows can expose
    # unsigned 32-bit termination codes. Neither is a completed test plan.
    return type(value) is int and -(2 ** 31) <= value <= 2 ** 32 - 1


def finite_number(value):
    if type(value) is int:
        return integer(value)
    return type(value) is float and math.isfinite(value)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                       allow_nan=False) + "\n").encode("utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def reject_constant(_value):
    raise InvalidInput("JSON contains a non-finite number")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON contains duplicate keys")
        result[key] = value
    return result


def parse_json(data):
    try:
        return json.loads(data, parse_constant=reject_constant,
                          object_pairs_hook=unique_object)
    except InvalidInput:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise InvalidInput("invalid JSON") from error


def read_bounded(path):
    with path.open("rb") as source:
        data = source.read(MAX_REPORT_BYTES + 1)
    require(len(data) <= MAX_REPORT_BYTES, "report exceeds the 64 MiB limit")
    return data


def timestamp(value):
    require(isinstance(value, str), "timestamp must be a UTC ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise InvalidInput("timestamp must be a UTC ISO 8601 string") from error
    require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
            "timestamp must use UTC")
    require(parsed.timestamp() >= 0, "timestamp must be after the Unix epoch")
    return parsed.timestamp()


def relative_source_path(value):
    require(isinstance(value, str) and value != "", "source path is missing")
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    path = PurePosixPath(value)
    require(not path.is_absolute() and ".." not in path.parts
            and not re.match(r"^[A-Za-z]:", value),
            "source paths must be repository-relative")
    require(str(path) != ".", "source path is empty")
    return str(path)


def validate_config(config):
    require(isinstance(config, dict) and type(config.get("schema_version")) is int
            and config["schema_version"] == 1,
            "config schema_version must be 1")
    for field in ("service", "environment", "mackerel_service"):
        require(isinstance(config.get(field), str) and config[field].strip(),
                "config is missing " + field)
    profiles = config.get("profiles")
    require(isinstance(profiles, dict) and 0 < len(profiles) <= 16,
            "config must define between 1 and 16 fixed profiles")
    for name, profile in profiles.items():
        require(PROFILE_RE.fullmatch(name) is not None, "invalid profile name")
        require(isinstance(profile, dict), "invalid profile configuration")
        kind = profile.get("kind")
        require(kind in ("junit", "test_cases", "cccc"), "unsupported profile kind")
        require(isinstance(profile.get("selection"), str) and profile["selection"].strip(),
                "profile must name its fixed selection")
        if kind == "junit":
            require(profile.get("producer") == "phpunit"
                    and re.fullmatch(r"12\.5\.[0-9]+", str(profile.get("version"))),
                    "JUnit adapter supports phpunit 12.5.x only")
        elif kind == "test_cases":
            require(profile.get("contract") == "test-cases/v1", "unsupported test cases contract")
            for field in ("producer", "version"):
                require(isinstance(profile.get(field), str) and profile[field].strip(),
                        "test cases profile must pin producer and version")
            validate_tool_identity(profile.get("converter"), "converter")
            codes = profile.get("completed_exit_codes")
            require(isinstance(codes, list) and codes and len(codes) <= 16
                    and all(process_exit_code(code) and code >= 0 for code in codes)
                    and len(set(codes)) == len(codes) and 0 in codes,
                    "completed_exit_codes must be unique nonnegative codes including zero")
        else:
            require(profile.get("producer") == "moznion/cccc"
                    and profile.get("version") == "1.8.3",
                    "CCCC adapter supports moznion/cccc 1.8.3 only")
            require(isinstance(profile.get("language"), str) and profile["language"],
                    "CCCC profile must specify its language")
            for metric in ("cognitive", "cyclomatic"):
                require(integer(profile.get(metric + "_limit")),
                        "CCCC profile must provide nonnegative integer limits")


def validate_run(run, config):
    require(isinstance(run, dict), "run must be an object")
    require(isinstance(run.get("revision"), str)
            and REVISION_RE.fullmatch(run["revision"]), "full revision is required")
    require(run.get("base_revision") is None
            or (isinstance(run["base_revision"], str)
                and REVISION_RE.fullmatch(run["base_revision"])),
            "base_revision must be a full revision or null")
    for field in ("run_id", "attempt_id"):
        require(isinstance(run.get(field), str) and run[field], field + " is required")
    require(timestamp(run.get("finished_at")) >= timestamp(run.get("started_at")),
            "finished_at precedes started_at")
    require(isinstance(run.get("profiles"), dict), "run profiles must be a map")
    require(set(run["profiles"]) <= set(config["profiles"]),
            "run contains a profile absent from the fixed config allowlist")
    require(run.get("test_wrapper_exit_code") is None or process_exit_code(run["test_wrapper_exit_code"]),
            "invalid test wrapper exit code")


def add_metric(metrics, name, value, measured_at):
    if value is None:
        return
    require(finite_number(value), "invalid metric")
    metrics.append({"name": name, "time": int(measured_at), "value": value})


def junit_report(data):
    # Decode before checking so UTF-16 cannot bypass the declaration rejection.
    # The supported PHPUnit dialect emits UTF-8.
    try:
        xml = data.decode("utf-8-sig")
    except UnicodeError as error:
        raise InvalidInput("JUnit must be UTF-8") from error
    require("\x00" not in xml and not re.search(r"<!\s*(DOCTYPE|ENTITY)\b", xml, re.I),
            "DTD and entity declarations are not accepted")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise InvalidInput("invalid JUnit XML") from error
    require(root.tag in ("testsuites", "testsuite"), "unsupported JUnit root")
    allowed = {"testsuites", "testsuite", "testcase", "failure", "error", "skipped",
               "system-out", "system-err", "properties", "property"}
    require(all(node.tag in allowed for node in root.iter()),
            "unsupported JUnit extension (retry/xfail dialects need a separate adapter)")
    cases = []
    seen = set()
    statuses = {}
    for case in root.iter("testcase"):
        require(case.get("name") and (case.get("classname") or case.get("class")),
                "JUnit case identity is incomplete")
        identity = ["phpunit", case.get("file", ""),
                    case.get("classname", case.get("class")), case.get("name")]
        case_id = digest(json_bytes(identity))
        require(case_id not in seen, "duplicate JUnit case identity")
        seen.add(case_id)
        outcomes = [node.tag for node in case if node.tag in ("failure", "error", "skipped")]
        require(len(outcomes) <= 1, "ambiguous JUnit case result")
        status = {"failure": "failed", "error": "error", "skipped": "skipped"}.get(
            outcomes[0] if outcomes else "", "passed")
        statuses[id(case)] = status
        # Names, parameters, messages and stdout can contain secrets. The hash
        # links cases within a profile without duplicating those strings.
        cases.append({"entity_id": case_id, "entity_kind": "test_case", "status": status})
    require(cases, "JUnit reported zero cases")
    suites = list(root.iter("testsuite"))
    require(suites, "JUnit must contain a testsuite")
    for suite in suites:
        counts = Counter(statuses[id(case)] for case in suite.iter("testcase"))
        expected = {"tests": sum(counts.values()), "failures": counts["failed"],
                    "errors": counts["error"], "skipped": counts["skipped"]}
        for field, value in expected.items():
            raw = suite.get(field)
            require(raw is not None and re.fullmatch(r"[0-9]{1,16}", raw)
                    and int(raw) == value, "JUnit suite summary disagrees with cases")
    # Some generators also include aggregate counters on testsuites.
    if root.tag == "testsuites":
        counts = Counter(case["status"] for case in cases)
        for field, value in {"tests": len(cases), "failures": counts["failed"],
                             "errors": counts["error"], "skipped": counts["skipped"]}.items():
            if root.get(field) is not None:
                require(re.fullmatch(r"[0-9]{1,16}", root.get(field)) and int(root.get(field)) == value,
                        "JUnit root summary disagrees with cases")
    return cases


def validate_tool_identity(value, label):
    require(isinstance(value, dict) and set(value) == {"name", "version"}
            and all(isinstance(value[field], str) and value[field].strip()
                    for field in ("name", "version")),
            "invalid " + label + " identity")


def test_cases_report(data, profile, expected_cases):
    """Read a conservative interchange contract, never arbitrary JUnit dialects."""
    report = parse_json(data)
    require(isinstance(report, dict)
            and set(report) == {"schema_version", "producer", "converter", "cases"},
            "test cases report has missing or unsupported fields")
    require(type(report["schema_version"]) is int and report["schema_version"] == 1,
            "test cases schema_version must be 1")
    validate_tool_identity(report["producer"], "producer")
    validate_tool_identity(report["converter"], "converter")
    require(report["producer"] == {"name": profile["producer"], "version": profile["version"]}
            and report["converter"] == profile["converter"],
            "test cases producer or converter differs from the pinned profile")
    require(integer(expected_cases) and expected_cases > 0,
            "test cases require a positive independently collected expected_cases count")
    require(isinstance(report["cases"], list) and len(report["cases"]) == expected_cases,
            "test cases report disagrees with the selected case count")
    records = []
    seen = set()
    for case in report["cases"]:
        require(isinstance(case, dict) and set(case) == {"id", "status"},
                "test case has missing or unsupported fields")
        require(isinstance(case["id"], str) and case["id"].strip(), "test case identity is missing")
        require(case["id"] not in seen, "duplicate test case identity")
        seen.add(case["id"])
        require(isinstance(case["status"], str)
                and case["status"] in ("passed", "failed", "error", "skipped"),
                "unsupported test case status")
        records.append({"entity_id": digest(json_bytes(["test-cases/v1", case["id"]])),
                        "entity_kind": "test_case", "status": case["status"]})
    return records


def percentile(values, fraction):
    return sorted(values)[math.ceil(len(values) * fraction) - 1] if values else 0


def cccc_report(data, expected_files):
    report = parse_json(data)
    require(isinstance(report, dict) and "top" not in report
            and isinstance(report.get("files"), list)
            and isinstance(report.get("summary"), dict), "CCCC needs full files/summary JSON")
    require(isinstance(expected_files, list) and expected_files,
            "CCCC requires a nonempty expected_files manifest")
    expected = [relative_source_path(path) for path in expected_files]
    require(len(set(expected)) == len(expected), "duplicate expected source path")
    summary = report["summary"]
    for field in ("file_count", "function_count", "parse_error_count", "parse_error_file_count"):
        require(integer(summary.get(field)), "CCCC is missing a summary count")
    functions = []
    actual = []
    parse_error_files = []
    parse_errors = 0

    def collect(items, path, parents=()):
        require(isinstance(items, list), "invalid CCCC function list")
        for index, function in enumerate(items):
            require(isinstance(function, dict), "invalid CCCC function")
            require(isinstance(function.get("name"), str) and function["name"],
                    "invalid CCCC function name")
            require(integer(function.get("line")) and function["line"] > 0,
                    "invalid CCCC function line")
            require(isinstance(function.get("kind"), str) and function["kind"],
                    "invalid CCCC function kind")
            for metric in ("cognitive", "cyclomatic"):
                require(integer(function.get(metric)), "invalid CCCC function value")
            hierarchy = (*parents, (function["name"], function["kind"], function["line"], index))
            functions.append({"entity_id": digest(json_bytes([path, hierarchy])),
                              "entity_kind": "function", "file": path,
                              "name": function["name"], "kind": function["kind"],
                              "line": function["line"], "cognitive": function["cognitive"],
                              "cyclomatic": function["cyclomatic"]})
            collect(function.get("children", []), path, hierarchy)

    for file in report["files"]:
        require(isinstance(file, dict), "invalid CCCC file")
        path = relative_source_path(file.get("path"))
        require(path not in actual, "duplicate CCCC file")
        actual.append(path)
        for metric in ("cognitive", "cyclomatic"):
            require(integer(file.get(metric)), "invalid CCCC file total")
        errors = file.get("parse_errors", [])
        require(isinstance(errors, list) and all(isinstance(e, str) for e in errors),
                "invalid CCCC parse_errors")
        parse_errors += len(errors)
        if errors:
            parse_error_files.append(path)
        collect(file.get("functions"), path)
    require(summary["file_count"] == len(actual)
            and summary["function_count"] == len(functions),
            "CCCC summary counts disagree with details (filtered output is unsupported)")
    require(summary["parse_error_count"] == parse_errors
            and summary["parse_error_file_count"] == len(parse_error_files),
            "CCCC parse error counts disagree with details")
    reported_error_paths = summary.get("parse_error_files", [])
    require(isinstance(reported_error_paths, list)
            and sorted(relative_source_path(p) for p in reported_error_paths) == sorted(parse_error_files),
            "CCCC parse error paths disagree with details")
    for metric in ("cognitive", "cyclomatic"):
        values = [function[metric] for function in functions]
        expected_summary = {"sum": sum(values), "max": max(values, default=0),
                            "median": percentile(values, .5), "p90": percentile(values, .9),
                            "p95": percentile(values, .95)}
        measured = summary.get(metric)
        require(isinstance(measured, dict), "CCCC metric summary is missing")
        for field, value in expected_summary.items():
            require(finite_number(measured.get(field)) and measured[field] == value,
                    "CCCC metric summary disagrees with all function details")
    diagnostics = []
    if sorted(expected) != sorted(actual):
        diagnostics.append("source_manifest_mismatch")
    if parse_errors:
        diagnostics.append("parse_errors")
    return functions, summary, diagnostics


def normalize_profile(name, profile, entry, run_dir, measured_at, wrapper_exit_code=None,
                      run_started_at=None):
    family = "tests" if profile["kind"] in ("junit", "test_cases") else "cccc"
    prefix = family + "." + name + "."
    metrics = []
    details = {"status": "missing", "records": [], "unsupported_metrics": []}
    collection = {"status": "missing", "diagnostics": [], "exit_code": None,
                  "gate_exit_code": None, "report_complete": False, "complete": False}
    artifact = None
    data = None
    entry = entry if isinstance(entry, dict) else {}
    exit_code = entry.get("exit_code")
    gate_exit = entry.get("gate_exit_code")
    try:
        require(exit_code is None or process_exit_code(exit_code), "invalid process exit code")
        require(gate_exit is None or process_exit_code(gate_exit), "invalid gate exit code")
        collection.update(exit_code=exit_code, gate_exit_code=gate_exit)
        path_value = entry.get("report")
        require(isinstance(path_value, str) and path_value, "report path is missing")
        path = run_dir / path_value
        data = read_bounded(path)
        artifact = {"uri": path_value, "relative_to": "run_manifest_parent",
                    "sha256": digest(data), "bytes": len(data)}
        require(data.strip(), "report is empty")
        require(entry.get("version") == profile["version"], "producer version is missing or changed")
        ruleset_hash = entry.get("ruleset_hash")
        require(ruleset_hash is None or (isinstance(ruleset_hash, str)
                and re.fullmatch(r"[a-f0-9]{64}", ruleset_hash)), "invalid ruleset hash")
        sidecars = entry.get("raw_artifacts", {})
        require(isinstance(sidecars, dict), "raw_artifacts must be a map")
        for sidecar in sidecars.values():
            require(isinstance(sidecar, dict) and isinstance(sidecar.get("path"), str)
                    and isinstance(sidecar.get("sha256"), str), "invalid sidecar artifact")
            require(digest(read_bounded(run_dir / sidecar["path"])) == sidecar["sha256"],
                    "sidecar checksum mismatch")
        collection["status"] = "partial"
        if family == "tests":
            require(("started_at" in entry) == ("finished_at" in entry),
                    "profile timestamps must include both started_at and finished_at")
            duration = None
            if "started_at" in entry:
                started = timestamp(entry["started_at"])
                finished = timestamp(entry["finished_at"])
                require(started <= finished <= measured_at
                        and (run_started_at is None or started >= run_started_at),
                        "profile timestamps must lie within the enclosing run")
                duration = finished - started
            if profile["kind"] == "test_cases":
                cases = test_cases_report(data, profile, entry.get("expected_cases"))
                collection["expected_cases"] = entry["expected_cases"]
                completed_codes = profile["completed_exit_codes"]
                require("run_status" in entry, "test cases require an explicit run_status")
            else:
                cases = junit_report(data)
                completed_codes = (0, 1, 2)
            details["records"] = cases
            details["unsupported_metrics"] = ["cases.executed", "cases.flaky", "cases.xfail",
                                              "cases.xpass", "attempts.executed", "attempts.retries"]
            collection["report_complete"] = True
            run_status = entry.get("run_status", "completed" if exit_code is not None else "unknown")
            require(run_status in ("completed", "aborted", "running", "unknown"), "invalid run_status")
            collection["run_status"] = run_status
            collection["test_plan_complete"] = entry.get("test_plan_complete") is True
            collection["complete"] = (collection["test_plan_complete"]
                                      and run_status == "completed" and exit_code in completed_codes)
            if not collection["complete"]:
                collection["diagnostics"].append("test_plan_not_confirmed_complete")
            counts = Counter(case["status"] for case in cases)
            if exit_code == 0 and (counts["failed"] or counts["error"]):
                collection["complete"] = False
                collection["diagnostics"].append("runner_success_disagrees_with_cases")
            if collection["complete"]:
                add_metric(metrics, prefix + "cases.selected", len(cases), measured_at)
                for field in ("passed", "failed", "error", "skipped"):
                    add_metric(metrics, prefix + "cases." + field, counts[field], measured_at)
                denominator = counts["passed"] + counts["failed"] + counts["error"]
                details["strict_pass"] = {"numerator": counts["passed"], "denominator": denominator,
                                          "value": counts["passed"] / denominator if denominator else None}
                if denominator:
                    add_metric(metrics, prefix + "ratios.strict_pass", counts["passed"] / denominator, measured_at)
                add_metric(metrics, prefix + "run.last_completed_timestamp", int(measured_at), measured_at)
            if duration is not None:
                add_metric(metrics, prefix + "run.duration_seconds", duration, measured_at)
        else:
            functions, summary, diagnostics = cccc_report(data, entry.get("expected_files"))
            details["records"] = functions
            details["summary"] = summary
            collection["diagnostics"].extend(diagnostics)
            collection["report_complete"] = not diagnostics
            collection["complete"] = not diagnostics and exit_code in (0, 1)
            if exit_code not in (0, 1):
                collection["diagnostics"].append("analyzer_did_not_finish")
            if collection["complete"]:
                for suffix, field in (("files", "file_count"), ("functions", "function_count"),
                                      ("parse_errors", "parse_error_count"),
                                      ("parse_error_files", "parse_error_file_count")):
                    add_metric(metrics, prefix + "analysis." + suffix, summary[field], measured_at)
                add_metric(metrics, prefix + "analysis.last_success_timestamp", int(measured_at), measured_at)
                for metric in ("cognitive", "cyclomatic"):
                    values = [function[metric] for function in functions]
                    if not values:
                        details["unsupported_metrics"].append(metric + ".distribution_no_functions")
                        continue
                    over = sum(value > profile[metric + "_limit"] for value in values)
                    for suffix, value in (("max", max(values)), ("p95", percentile(values, .95)),
                                          ("over_limit_count", over), ("over_limit_ratio", over / len(values))):
                        add_metric(metrics, prefix + metric + "." + suffix, value, measured_at)
            else:
                # Keep the known failure state, never partial counts/distributions.
                details["population"] = "partial_observation_only"
        if collection["complete"]:
            collection["status"] = "complete"
    except FileNotFoundError:
        collection["status"] = "missing"
        collection["diagnostics"].append("report_missing")
    except (InvalidInput, json.JSONDecodeError, UnicodeError, RecursionError, OSError) as error:
        collection["status"] = "error" if data is not None else "missing"
        collection["diagnostics"].append(str(error) if isinstance(error, InvalidInput) else "report_unreadable_or_invalid")
        collection["report_complete"] = False
        collection["complete"] = False
        metrics = []
    if family == "tests":
        collection["wrapper_exit_code"] = wrapper_exit_code
        add_metric(metrics, prefix + "run.report_complete", int(collection["report_complete"]), measured_at)
        add_metric(metrics, prefix + "run.complete", int(collection["complete"]), measured_at)
        success = None
        if (process_exit_code(exit_code) and exit_code != 0) or (process_exit_code(gate_exit) and gate_exit != 0) or (process_exit_code(wrapper_exit_code) and wrapper_exit_code != 0) or entry.get("run_status") == "aborted":
            success = 0
        elif collection["complete"] and exit_code == 0 and gate_exit in (None, 0):
            success = 1
        add_metric(metrics, prefix + "run.success", success, measured_at)
        collection["success"] = success
    else:
        add_metric(metrics, prefix + "analysis.complete", int(collection["complete"]), measured_at)
    details["status"] = collection["status"]
    return collection, details, metrics, artifact


def normalize(config, run, run_dir):
    validate_config(config)
    validate_run(run, config)
    measured_at = timestamp(run["finished_at"])
    manifest = {field: run.get(field) for field in ("revision", "base_revision", "run_id", "attempt_id",
                                                   "started_at", "finished_at", "working_tree_dirty", "artifact_uri",
                                                   "test_wrapper_exit_code")}
    manifest.update({field: config[field] for field in ("schema_version", "service", "environment", "mackerel_service")})
    rulesets = {name: entry.get("ruleset_hash") for name, entry in run["profiles"].items()
                if isinstance(entry, dict)}
    comparison = {"adapter_version": ADAPTER_VERSION, "service": config["service"],
                  "environment": config["environment"], "profiles": config["profiles"], "ruleset_hashes": rulesets}
    manifest.update(comparison_key=digest(json_bytes(comparison)), profiles={}, collection={}, artifacts={})
    details = {"schema_version": 1, "profiles": {}}
    metrics = []
    for name, profile in config["profiles"].items():
        collection, records, series, artifact = normalize_profile(
            name, profile, run["profiles"].get(name), Path(run_dir), measured_at,
            run.get("test_wrapper_exit_code"), timestamp(run["started_at"]))
        manifest["collection"][name] = collection
        manifest["profiles"][name] = {
            "comparison_key": digest(json_bytes({**comparison, "profiles": {name: profile},
                                                 "ruleset_hashes": {name: rulesets.get(name)}})),
            "tool": {"producer": profile["producer"], "version": profile["version"],
                     "observed_version": run["profiles"].get(name, {}).get("version")
                     if isinstance(run["profiles"].get(name), dict) else None,
                     "adapter_version": ADAPTER_VERSION}, "scope": profile}
        if artifact:
            manifest["artifacts"][name + "_raw"] = artifact
        entry = run["profiles"].get(name, {})
        if isinstance(entry, dict):
            manifest["profiles"][name]["ruleset_hash"] = entry.get("ruleset_hash")
            manifest["profiles"][name]["raw_artifacts"] = entry.get("raw_artifacts", {})
        details["profiles"][name] = records
        metrics.extend(series)
    metrics.sort(key=lambda item: item["name"])
    manifest["publication"] = {"service": config["mackerel_service"], "status": "not_published",
                               "measurement_timestamp": int(measured_at),
                               "payload_sha256": digest(json_bytes(metrics)),
                               "series": [metric["name"] for metric in metrics]}
    return manifest, details, metrics


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        require(not args.output.exists(), "output directory already exists; use a fresh run directory")
        config = parse_json(read_bounded(args.config))
        run = parse_json(read_bounded(args.run))
        manifest, details, metrics = normalize(config, run, args.run.resolve().parent)
        args.output.mkdir(parents=True, exist_ok=False)
        for name, value in (("details.json", details), ("metrics.json", metrics)):
            data = json_bytes(value)
            (args.output / name).write_bytes(data)
            manifest["artifacts"][name] = {"uri": name, "sha256": digest(data), "bytes": len(data)}
        manifest["artifacts"]["config"] = {"sha256": digest(read_bounded(args.config))}
        manifest["artifacts"]["run"] = {"sha256": digest(read_bounded(args.run))}
        (args.output / "manifest.json").write_bytes(json_bytes(manifest))
        complete = all(item["complete"] for item in manifest["collection"].values())
        print("Reports normalized: " + ("complete" if complete else "incomplete; inspect manifest.json"))
        return 0 if complete else 1
    except (InvalidInput, json.JSONDecodeError, UnicodeError, OSError) as error:
        message = str(error) if isinstance(error, InvalidInput) else "configuration or output could not be read/written"
        print("Normalization failed: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
