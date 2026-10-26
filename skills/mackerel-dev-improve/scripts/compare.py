#!/usr/bin/env python3
"""Compare two normalized quality bundles offline, preserving missing evidence.

This is a comparison of observed test outcomes and static complexity, not proof
of behavioral equivalence or a performance benchmark. No network access occurs.
"""

import argparse
from collections import Counter
import hashlib
import importlib.util
import math
from pathlib import Path
import sys


_SPEC = importlib.util.spec_from_file_location("_quality_publisher", Path(__file__).with_name("publish.py"))
publisher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publisher)
require = publisher.require
MAX_DETAILS_BYTES = 64 * 1024 * 1024
STATUSES = {"passed", "failed", "error", "skipped"}
EXIT_CODES = {"improve": 0, "unchanged": 0, "regression": 1,
              "incomparable": 2, "insufficient_evidence": 2}


def checked_metric(values, name, expected):
    require(name in values and values[name] == expected,
            "Canonical metric is missing or contradicts details: " + name)


def load_snapshot(path):
    manifest, records = publisher.load_bundle(path)
    for field in ("service", "environment", "run_id", "attempt_id"):
        require(isinstance(manifest.get(field), str) and manifest[field].strip(), "Missing snapshot identity: " + field)
    require(isinstance(manifest.get("revision"), str)
            and publisher.REVISION_RE.fullmatch(manifest["revision"]), "Invalid snapshot revision")
    if manifest.get("base_revision") is not None:
        require(isinstance(manifest["base_revision"], str)
                and publisher.REVISION_RE.fullmatch(manifest["base_revision"]), "Invalid base revision")
    details_path = path / "details.json"
    require(details_path.is_file() and not details_path.is_symlink(), "Missing regular details.json")
    with details_path.open("rb") as stream:
        data = stream.read(MAX_DETAILS_BYTES + 1)
    require(len(data) <= MAX_DETAILS_BYTES, "Details exceed 64 MiB")
    artifact = manifest.get("artifacts", {}).get("details.json", {})
    require(isinstance(artifact, dict) and artifact.get("uri") == "details.json"
            and artifact.get("sha256") == hashlib.sha256(data).hexdigest()
            and type(artifact.get("bytes")) is int and artifact["bytes"] == len(data), "Details checksum or size differs from manifest")
    details = publisher.parse_json(data)
    require(isinstance(details, dict) and details.get("schema_version") == 1
            and isinstance(details.get("profiles"), dict)
            and set(details["profiles"]) == set(manifest["profiles"]), "Invalid details profile contract")
    metrics = {item["name"]: item["value"] for item in records}
    for name, profile in manifest["profiles"].items():
        detail = details["profiles"][name]
        state = manifest["collection"][name]
        require(isinstance(detail, dict) and detail.get("status") == state["status"]
                and isinstance(detail.get("records"), list), "Details status disagrees with collection")
        if not state["complete"]:
            continue
        kind = profile["scope"]["kind"]
        entities = detail["records"]
        ids = set()
        for entity in entities:
            require(isinstance(entity, dict) and isinstance(entity.get("entity_id"), str)
                    and publisher.COMPARISON_KEY_RE.fullmatch(entity["entity_id"])
                    and entity["entity_id"] not in ids, "Invalid or duplicate entity ID")
            ids.add(entity["entity_id"])
        if kind in publisher.TEST_KINDS:
            prefix = "tests." + name + "."
            require(entities and all(entity.get("entity_kind") == "test_case"
                    and entity.get("status") in STATUSES for entity in entities), "Invalid complete test details")
            counts = Counter(entity["status"] for entity in entities)
            checked_metric(metrics, prefix + "cases.selected", len(entities))
            for status in STATUSES:
                checked_metric(metrics, prefix + "cases." + status, counts[status])
            denominator = len(entities) - counts["skipped"]
            if denominator:
                checked_metric(metrics, prefix + "ratios.strict_pass", counts["passed"] / denominator)
            else:
                require(prefix + "ratios.strict_pass" not in metrics, "All-skipped tests cannot have a pass ratio")
            checked_metric(metrics, prefix + "run.complete", 1)
            checked_metric(metrics, prefix + "run.report_complete", 1)
            require(type(state.get("success")) is int and state["success"] in (0, 1), "Complete test success is unknown")
            checked_metric(metrics, prefix + "run.success", state["success"])
            require(not state["success"] or not (counts["failed"] + counts["error"]), "Successful run contains failed tests")
            for field in ("exit_code", "gate_exit_code", "wrapper_exit_code"):
                code = state.get(field)
                require(code is None or type(code) is int, "Invalid process exit code")
                require(not state["success"] or code in (None, 0), "Successful run contradicts process failure")
        else:
            prefix = "cccc." + name + "."
            summary = detail.get("summary")
            require(isinstance(summary, dict) and type(summary.get("file_count")) is int
                    and summary["file_count"] > 0 and summary.get("function_count") == len(entities)
                    and summary.get("parse_error_count") == 0 and summary.get("parse_error_file_count") == 0,
                    "Invalid complete CCCC summary")
            checked_metric(metrics, prefix + "analysis.complete", 1)
            for suffix, field in (("files", "file_count"), ("functions", "function_count"),
                                  ("parse_errors", "parse_error_count"), ("parse_error_files", "parse_error_file_count")):
                checked_metric(metrics, prefix + "analysis." + suffix, summary[field])
            require(all(entity.get("entity_kind") == "function" for entity in entities), "Invalid CCCC entities")
            for metric in ("cognitive", "cyclomatic"):
                values = [entity.get(metric) for entity in entities]
                limit = profile["scope"].get(metric + "_limit")
                require(type(limit) is int and limit >= 0 and all(type(value) is int and 0 <= value <= publisher.MAX_INTEGER for value in values),
                        "Invalid complexity values or limit")
                if not values:
                    require(not any(key.startswith(prefix + metric + ".") for key in metrics), "Empty population has complexity distribution")
                    continue
                over = sum(value > limit for value in values)
                for suffix, expected in (("max", max(values)), ("p95", sorted(values)[math.ceil(len(values) * .95) - 1]),
                                         ("over_limit_count", over), ("over_limit_ratio", over / len(values))):
                    checked_metric(metrics, prefix + metric + "." + suffix, expected)
    return {"manifest": manifest, "details": details["profiles"], "metrics": metrics}


def identity(snapshot):
    manifest = snapshot["manifest"]
    result = {key: manifest.get(key) for key in ("service", "environment", "mackerel_service", "revision",
                                               "base_revision", "run_id", "attempt_id", "finished_at", "artifact_uri")}
    result["metrics_sha256"] = manifest["publication"]["payload_sha256"]
    result["details_sha256"] = manifest["artifacts"]["details.json"]["sha256"]
    return result


def deltas(base, head, prefix):
    result = []
    for name in sorted(set(base) | set(head)):
        if not name.startswith(prefix):
            continue
        left, right = base.get(name), head.get(name)
        result.append({"name": name, "base": left, "head": right,
                       "delta": right - left if left is not None and right is not None else None})
    return result


def compare_profile(name, base, head):
    bm, hm = base["manifest"], head["manifest"]
    bp, hp = bm["profiles"].get(name), hm["profiles"].get(name)
    result = {"verdict": "incomparable", "reasons": [], "metrics": []}
    if bp is None or hp is None:
        result["reasons"].append("profile_missing_on_one_side")
        return result
    kind = hp["scope"]["kind"]
    prefix = ("tests." if kind in publisher.TEST_KINDS else "cccc.") + name + "."
    result["metrics"] = deltas(base["metrics"], head["metrics"], prefix)
    result["head_success"] = hm["collection"][name].get("success")
    if bp["comparison_key"] != hp["comparison_key"] or bp["scope"] != hp["scope"]:
        result["reasons"].append("measurement_definition_changed")
        return result
    bs, hs = bm["collection"][name], hm["collection"][name]
    if not bs["complete"] or not hs["complete"]:
        result.update(verdict="insufficient_evidence", reasons=["collection_incomplete"])
        return result
    br = {item["entity_id"]: item for item in base["details"][name]["records"]}
    hr = {item["entity_id"]: item for item in head["details"][name]["records"]}
    result["population"] = {"base": len(br), "head": len(hr),
                            "added": len(hr.keys() - br.keys()), "removed": len(br.keys() - hr.keys())}
    if br.keys() != hr.keys():
        result.update(verdict="incomparable", reasons=["entity_population_changed"])
        return result
    if kind in publisher.TEST_KINDS:
        denominator_base = sum(item["status"] != "skipped" for item in br.values())
        denominator_head = sum(item["status"] != "skipped" for item in hr.values())
        result["denominators"] = {"base": denominator_base, "head": denominator_head}
        if hs["success"] == 0:
            result.update(verdict="regression" if bs["success"] == 1 else "insufficient_evidence",
                          reasons=["head_run_failed" if bs["success"] == 1 else "head_run_still_failed"])
        elif any(hr[key]["status"] == "skipped" and br[key]["status"] != "skipped" for key in br):
            result.update(verdict="insufficient_evidence", reasons=["previously_executed_cases_now_skipped"])
        elif denominator_base == 0 or denominator_head == 0:
            result.update(verdict="insufficient_evidence", reasons=["no_non_skipped_test_population"])
        elif bs["success"] == 0:
            result.update(verdict="improve", reasons=["same_test_population_now_passes"])
        else:
            result.update(verdict="unchanged", reasons=["successful_test_outcomes_preserved"])
        result["notes"] = ["duration_delta_is_descriptive_not_a_controlled_benchmark"]
    else:
        if hs.get("gate_exit_code") not in (None, 0):
            result.update(verdict="regression" if bs.get("gate_exit_code") == 0 else "insufficient_evidence",
                          reasons=["head_analysis_gate_failed"])
            return result
        if base["metrics"][prefix + "analysis.files"] != head["metrics"][prefix + "analysis.files"]:
            result.update(verdict="incomparable", reasons=["source_file_count_changed"])
            return result
        if not br:
            result.update(verdict="insufficient_evidence", reasons=["no_functions"])
            return result
        changes = [hr[key][metric] - br[key][metric] for key in br for metric in ("cognitive", "cyclomatic")]
        # Entity-level increases cannot be hidden by a falling project p95/max.
        result.update(verdict="regression" if any(value > 0 for value in changes) else
                      "improve" if any(value < 0 for value in changes) else "unchanged",
                      reasons=["function_complexity_increased"] if any(value > 0 for value in changes) else
                      ["function_complexity_decreased"] if any(value < 0 for value in changes) else ["complexity_unchanged"])
        result["notes"] = ["function_ids_include_source_positions; moves_or_splits_need_manual_review"]
    return result


def compare(base_path, head_path):
    base, head = load_snapshot(base_path), load_snapshot(head_path)
    bm, hm = base["manifest"], head["manifest"]
    result = {"schema_version": 1, "kind": "quality-comparison/v1", "base": identity(base), "head": identity(head),
              "verdict": "incomparable", "reasons": [], "profiles": {},
              "limitations": ["Repository identity is not recorded by normalized bundle v1; the caller must select the same repository.",
                              "This compares observed metrics, not behavioral equivalence or causality."]}
    for field in ("service", "environment", "mackerel_service"):
        if bm[field] != hm[field]:
            result["reasons"].append(field + "_changed")
    if hm.get("base_revision") is not None and hm["base_revision"] != bm["revision"]:
        result["reasons"].append("head_base_revision_does_not_match_baseline")
    if hm["publication"]["measurement_timestamp"] < bm["publication"]["measurement_timestamp"]:
        result["reasons"].append("head_measurement_precedes_baseline")
    if result["reasons"]:
        return result
    result["profiles"] = {name: compare_profile(name, base, head)
                          for name in sorted(set(bm["profiles"]) | set(hm["profiles"]))}
    verdicts = {profile["verdict"] for profile in result["profiles"].values()}
    result["verdict"] = next(value for value in ("regression", "incomparable", "insufficient_evidence", "improve", "unchanged") if value in verdicts)
    if bm.get("working_tree_dirty") is not False or hm.get("working_tree_dirty") is not False:
        result["reasons"].append("clean_source_revision_not_confirmed")
        if result["verdict"] in ("improve", "unchanged"):
            result["verdict"] = "insufficient_evidence"
    if not any(profile["scope"]["kind"] in publisher.TEST_KINDS for profile in hm["profiles"].values()):
        result["reasons"].append("no_behavioral_test_profile")
        if result["verdict"] == "improve":
            result["verdict"] = "insufficient_evidence"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", type=Path, required=True, help="Baseline normalized bundle directory")
    parser.add_argument("--head", type=Path, required=True, help="Changed normalized bundle directory")
    parser.add_argument("--out", type=Path, required=True, help="New JSON result file; never overwrites evidence")
    args = parser.parse_args(argv)
    try:
        result = compare(args.base, args.head)
        with args.out.open("xb") as stream:
            stream.write(publisher.json_bytes(result))
        print("Quality comparison: " + result["verdict"])
        return EXIT_CODES[result["verdict"]]
    except (publisher.PublicationError, OSError, TypeError, KeyError, AttributeError) as error:
        message = str(error) if isinstance(error, publisher.PublicationError) else "evidence is invalid or output cannot be created"
        print("Comparison failed: " + message, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
