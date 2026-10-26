#!/usr/bin/env python3
"""Validate and optionally publish a normalized quality bundle to Mackerel.

Default operation is offline. The calling CI must serialize its baseline-branch
publisher and obtain expected revision/run/attempt from the current generation,
not by copying them out of this bundle. Mackerel metadata has no compare-and-swap.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


API_BASE = "https://api.mackerelio.com"
MAX_BYTES = 4 * 1024 * 1024
MAX_INTEGER = 2 ** 53 - 1
PROFILE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
REVISION_RE = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
COMPARISON_KEY_RE = re.compile(r"[a-f0-9]{64}\Z")
TEST_KINDS = {"junit", "test_cases"}
TEST_SUFFIXES = {
    "cases.selected", "cases.passed", "cases.failed", "cases.error", "cases.skipped",
    "ratios.strict_pass", "run.last_completed_timestamp", "run.duration_seconds",
    "run.report_complete", "run.complete", "run.success",
}
CCCC_SUFFIXES = {
    "analysis.files", "analysis.functions", "analysis.parse_errors", "analysis.parse_error_files",
    "analysis.complete", "analysis.last_success_timestamp",
} | {metric + "." + suffix for metric in ("cognitive", "cyclomatic")
     for suffix in ("max", "p95", "over_limit_count", "over_limit_ratio")}
PARTIAL_SUFFIXES = {"run.report_complete", "run.complete", "run.success", "run.duration_seconds", "analysis.complete"}


class PublicationError(ValueError):
    """Safe diagnostic: never includes response bodies, headers or credentials."""


class APIError(PublicationError):
    def __init__(self, status=None):
        self.status = status
        super().__init__(f"Mackerel request failed (HTTP {status})" if status else "Mackerel request failed")


def require(condition, message):
    if not condition:
        raise PublicationError(message)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON contains duplicate keys")
        result[key] = value
    return result


def invalid_constant(_value):
    raise PublicationError("JSON contains a non-finite number")


def parse_json(data):
    try:
        return json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise PublicationError("Invalid JSON evidence or API response") from error


def read_file(path):
    require(path.is_file() and not path.is_symlink(), "Bundle file is missing or not a regular file")
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, "Bundle file is too large")
    return data


def number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def epoch(value):
    if type(value) is int and value >= 0:
        return value
    require(isinstance(value, str), "Invalid metadata measurement time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "Metadata measurement time needs a timezone")
        return int(parsed.timestamp())
    except (ValueError, OverflowError) as error:
        raise PublicationError("Invalid metadata measurement time") from error


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        raise APIError(code)


class Client:
    def __init__(self, key, sleep=time.sleep):
        require(isinstance(key, str) and key and "\n" not in key and "\r" not in key, "MACKEREL_APIKEY is required")
        self.key = key
        self.sleep = sleep
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        require(path.startswith("/api/v0/"), "Invalid API path")
        data = json_bytes(body) if body is not None else None
        # Only reads retry automatically. An uncertain mutation remains explicit
        # in publication-result.json and can be retried with the same snapshot.
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                request = Request(API_BASE + path, data=data, method=method, headers={
                    "X-Api-Key": self.key, "Content-Type": "application/json",
                    "Accept": "application/json", "User-Agent": "mackerel-dev-improve/1",
                })
                with self.opener.open(request, timeout=10) as response:
                    require(response.status == 200, "Unexpected API response status")
                    payload = response.read(MAX_BYTES + 1)
                    require(len(payload) <= MAX_BYTES, "API response is too large")
                return parse_json(payload)
            except HTTPError as error:
                status = error.code
                error.close()
                if status not in (429, 500, 502, 503, 504) or attempt + 1 == attempts:
                    raise APIError(status) from None
            except (URLError, TimeoutError, OSError):
                if attempt + 1 == attempts:
                    raise APIError() from None
            self.sleep(attempt + 1)


def load_bundle(bundle):
    manifest = parse_json(read_file(bundle / "manifest.json"))
    payload = read_file(bundle / "metrics.json")
    metrics = parse_json(payload)
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1, "Unsupported manifest")
    publication = manifest.get("publication", {})
    require(isinstance(publication, dict) and publication.get("payload_sha256") == hashlib.sha256(payload).hexdigest(),
            "Metrics checksum differs from manifest")
    service = manifest.get("mackerel_service")
    require(isinstance(service, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,62}", service), "Invalid Mackerel service")
    require(publication.get("service") == service, "Publication service differs from manifest")
    stamp = publication.get("measurement_timestamp")
    require(type(stamp) is int and stamp >= 0, "Invalid measurement timestamp")
    require(epoch(manifest.get("finished_at")) == stamp, "Measurement time differs from run finish")
    profiles = manifest.get("profiles")
    collection = manifest.get("collection")
    require(isinstance(profiles, dict) and 0 < len(profiles) <= 16 and isinstance(collection, dict), "Invalid profile manifest")
    allowed = {}
    expected_states = {}
    for name, profile in profiles.items():
        require(PROFILE_RE.fullmatch(name) is not None and isinstance(profile, dict), "Invalid fixed profile")
        require(isinstance(profile.get("comparison_key"), str)
                and COMPARISON_KEY_RE.fullmatch(profile["comparison_key"]), "Invalid profile comparison key")
        require(isinstance(profile.get("scope"), dict), "Invalid profile scope")
        kind = profile["scope"].get("kind")
        require(kind in TEST_KINDS or kind == "cccc", "Unknown metric profile kind")
        family = "tests" if kind in TEST_KINDS else "cccc"
        suffixes = TEST_SUFFIXES if kind in TEST_KINDS else CCCC_SUFFIXES
        state = collection.get(name)
        require(isinstance(state, dict) and type(state.get("complete")) is bool
                and state.get("status") in ("complete", "partial", "missing", "error"), "Missing collection completeness")
        require((state["status"] == "complete") == state["complete"], "Collection status contradicts completeness")
        require(type(state.get("report_complete")) is bool, "Missing report completeness")
        require(not state["complete"] or state["report_complete"], "Complete collection requires a complete report")
        require(state["status"] not in ("missing", "error") or not state["report_complete"],
                "Missing or invalid reports cannot be complete")
        prefix = family + "." + name + "."
        if kind in TEST_KINDS:
            expected_states[prefix + "run.complete"] = int(state["complete"])
            expected_states[prefix + "run.report_complete"] = int(state["report_complete"])
            expected_states[prefix + "run.success"] = state.get("success")
        else:
            expected_states[prefix + "analysis.complete"] = int(state["complete"])
        for suffix in suffixes:
            if state["complete"] or suffix in PARTIAL_SUFFIXES:
                allowed[prefix + suffix] = suffix
    require(isinstance(metrics, list) and 0 < len(metrics) <= 256, "Invalid metrics payload")
    names = []
    for item in metrics:
        require(isinstance(item, dict) and set(item) == {"name", "time", "value"}, "Invalid metric record")
        name, value = item.get("name"), item.get("value")
        require(isinstance(name, str) and name in allowed, "Metric is outside the fixed profile contract")
        require(type(item["time"]) is int and item["time"] == stamp, "Metric snapshot time differs from manifest")
        require(number(value) and value >= 0, "Invalid metric value")
        suffix = allowed[name]
        if suffix.startswith("ratios.") or suffix.endswith("_ratio"):
            require(value <= 1, "Metric ratio is outside 0..1")
        elif suffix != "run.duration_seconds":
            require(type(value) is int and value <= MAX_INTEGER,
                    "Counts, complexity and timestamps must be integers within 0..2^53-1")
        if suffix in {"run.complete", "run.report_complete", "run.success", "analysis.complete"}:
            require(value in (0, 1), "Invalid completeness metric")
            require(type(expected_states[name]) is int and value == expected_states[name],
                    "State metric contradicts the collection evidence")
        names.append(name)
    require(len(names) == len(set(names)), "Duplicate metric series")
    require(publication.get("series") == names, "Series list differs from manifest")
    return manifest, metrics


def send_guards(manifest, expected, now, use_key_organization=False):
    require(manifest.get("working_tree_dirty") is False, "Publishing requires a clean measured source tree")
    require(isinstance(manifest.get("revision"), str) and REVISION_RE.fullmatch(manifest["revision"]), "Invalid source revision")
    for field in ("revision", "run_id", "attempt_id"):
        require(isinstance(expected.get(field), str) and expected[field] and manifest.get(field) == expected[field],
                "Current generation does not match bundle " + field)
    if not use_key_organization:
        require(isinstance(expected.get("org"), str) and expected["org"], "Expected organization is required")
    age = now - manifest["publication"]["measurement_timestamp"]
    require(0 <= age < 24 * 60 * 60, "Snapshot is in the future or at least 24 hours old")
    uri = manifest.get("artifact_uri")
    require(isinstance(uri, str), "Artifact URL is required for publication")
    parsed = urlsplit(uri)
    require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password and not parsed.query,
            "Artifact URL must be HTTPS without credentials or query secrets")


def key_organization(client):
    """Bind this invocation to the key's organization without disclosing its name."""
    response = client.request("GET", "/api/v0/org")
    name = response.get("name") if isinstance(response, dict) else None
    require(isinstance(name, str) and 0 < len(name) <= 255 and name == name.strip()
            and all(ord(character) >= 32 and ord(character) != 127 for character in name),
            "Invalid organization response")
    return name


def metadata_guard(metadata, manifest):
    require(isinstance(metadata, dict), "Existing quality metadata is not an object")
    if metadata:
        require(metadata.get("schema_version") == 1 and metadata.get("service") == manifest.get("service")
                and metadata.get("environment") == manifest.get("environment"), "Existing metadata belongs to a different measurement contract")
    existing = metadata.get("profiles", {})
    require(isinstance(existing, dict), "Existing metadata profiles are invalid")
    stamp = manifest["publication"]["measurement_timestamp"]
    checksum = manifest["publication"]["payload_sha256"]
    for name in manifest["profiles"]:
        if name not in existing:
            continue
        previous = existing[name]
        require(isinstance(previous, dict), "Existing profile metadata is invalid")
        # Metadata published before comparison keys were stored remains readable.
        # Its old definition cannot be inferred; the next successful publication
        # records the current key. Once present, a key binds this fixed series.
        if "comparison_key" in previous:
            require(previous["comparison_key"] == manifest["profiles"][name]["comparison_key"],
                    "Profile definition changed; use a new fixed profile for the new baseline")
        previous_stamp = epoch(previous.get("measured_at"))
        same_run = all(previous.get(field) == manifest.get(field) for field in ("revision", "run_id", "attempt_id"))
        require(previous_stamp <= stamp, "A newer measurement is already published")
        if same_run:
            require(previous_stamp == stamp and previous.get("payload_sha256") == checksum,
                    "The same run already has different measurement evidence")
        else:
            require(stamp - previous_stamp > 120, "Another run overlaps the measurement readback window")


def metric_matches(response, metric):
    if not isinstance(response, dict) or not isinstance(response.get("metrics"), list):
        return False
    candidates = []
    for item in response["metrics"]:
        if not isinstance(item, dict) or not number(item.get("time")) or not number(item.get("value")):
            return False
        # A full minute away is an adjacent point, not timestamp rounding.
        # Sub-minute tolerance still requires caller-side snapshot isolation.
        if abs(item["time"] - metric["time"]) < 60:
            candidates.append(item)
    return len(candidates) == 1 and math.isclose(candidates[0]["value"], metric["value"], rel_tol=1e-9, abs_tol=1e-12)


def result_writer(bundle, result, status, **fields):
    result.update(status=status, **fields)
    result.setdefault("stages", []).append({"status": status, "at": datetime.now(timezone.utc).isoformat()})
    path = bundle / "publication-result.json"
    require(not path.is_symlink(), "Publication result must not be a symlink")
    path.write_bytes(json_bytes(result))


def publish(bundle, *, send=False, expected=None, create_service=False, client=None,
            now=None, readback_attempts=3, sleep=time.sleep, use_key_organization=False):
    result = {"schema_version": 1, "status": "validating"}
    posted = False
    verified = False
    stage = "validation"
    try:
        expected = dict(expected or {})
        require(not use_key_organization or expected.get("org") is None,
                "Choose either an expected organization or the key organization")
        manifest, metrics = load_bundle(bundle)
        result.update(revision=manifest.get("revision"), run_id=manifest.get("run_id"),
                      attempt_id=manifest.get("attempt_id"), service=manifest["mackerel_service"],
                      payload_sha256=manifest["publication"]["payload_sha256"],
                      metric_count=len(metrics), measurement_timestamp=manifest["publication"]["measurement_timestamp"])
        if not send:
            result_writer(bundle, result, "dry_run")
            return 0
        send_guards(manifest, expected, time.time() if now is None else now, use_key_organization)
        if client is None:
            client = Client(os.environ.get("MACKEREL_APIKEY"))
        stage = "preflight"
        if use_key_organization:
            expected["org"] = key_organization(client)
        org = client.request("GET", "/api/v0/org")
        require(isinstance(org, dict) and org.get("name") == expected["org"], "Mackerel organization mismatch")
        services = client.request("GET", "/api/v0/services")
        require(isinstance(services, dict) and isinstance(services.get("services"), list), "Invalid service listing")
        service = manifest["mackerel_service"]
        exists = any(isinstance(item, dict) and item.get("name") == service for item in services["services"])
        if not exists:
            require(create_service, "Mackerel service is absent; use --create-service only for an intended new service")
            stage = "creating_service"
            result_writer(bundle, result, "creating_service")
            created = client.request("POST", "/api/v0/services", {"name": service, "memo": "Development quality measurements"})
            require(isinstance(created, dict) and created.get("name") == service, "Service creation was not confirmed")
            result_writer(bundle, result, "service_created")
        service_path = "/api/v0/services/" + quote(service, safe="")
        metadata_path = service_path + "/metadata/dev-quality"
        try:
            metadata = client.request("GET", metadata_path)
        except APIError as error:
            if error.status != 404:
                raise
            metadata = {}
        metadata_guard(metadata, manifest)
        result_writer(bundle, result, "preflight_complete")
        stage = "posting"
        result_writer(bundle, result, "posting")
        response = client.request("POST", service_path + "/tsdb", metrics)
        require(isinstance(response, dict) and response.get("success") is True, "Metric post was not acknowledged")
        posted = True
        result_writer(bundle, result, "published_unverified")
        stage = "readback"
        pending = list(metrics)
        for attempt in range(readback_attempts):
            remaining = []
            for metric in pending:
                path = service_path + "/metrics?" + urlencode({
                    "name": metric["name"], "from": metric["time"] - 120, "to": metric["time"] + 120,
                })
                try:
                    response = client.request("GET", path)
                except APIError as error:
                    if error.status != 404:
                        raise
                    response = {"metrics": []}
                if not metric_matches(response, metric):
                    remaining.append(metric)
            pending = remaining
            if not pending:
                break
            if attempt + 1 < readback_attempts:
                sleep(2)
        require(not pending, "Posted values are absent, ambiguous, or do not match the snapshot")
        verified = True
        result_writer(bundle, result, "metrics_verified")
        stage = "metadata"
        # Detect intervening metadata changes. Caller serialization is still
        # required because the remote API cannot provide an atomic CAS update.
        try:
            latest = client.request("GET", metadata_path)
        except APIError as error:
            if error.status != 404:
                raise
            latest = {}
        require(latest == metadata, "Metadata changed during publication; refusing to overwrite another writer")
        updated = dict(metadata)
        updated.update(schema_version=1, service=manifest["service"], environment=manifest["environment"])
        profiles = dict(updated.get("profiles", {}))
        for name in manifest["profiles"]:
            profiles[name] = {
                "revision": manifest["revision"], "run_id": manifest["run_id"], "attempt_id": manifest["attempt_id"],
                "comparison_key": manifest["profiles"][name]["comparison_key"],
                "measured_at": manifest["publication"]["measurement_timestamp"],
                "artifact_uri": manifest["artifact_uri"],
                "report_completeness": manifest["collection"][name]["status"],
                "published_status": "verified", "payload_sha256": manifest["publication"]["payload_sha256"],
            }
        updated["profiles"] = profiles
        require(len(json_bytes(updated)) <= 100000, "Updated metadata exceeds the service limit")
        response = client.request("PUT", metadata_path, updated)
        require(isinstance(response, dict) and response.get("success") is True, "Metadata update was not acknowledged")
        require(client.request("GET", metadata_path) == updated, "Metadata readback did not match")
        result_writer(bundle, result, "verified")
        return 0
    except (PublicationError, OSError, ValueError, KeyError, TypeError) as error:
        status = ("metrics_verified_metadata_failed" if verified else "published_unverified" if posted
                  else "service_creation_unknown" if stage == "creating_service"
                  else "publication_unknown" if stage == "posting" else "rejected")
        diagnostic = str(error) if isinstance(error, PublicationError) else "Invalid bundle or local result could not be saved"
        result_writer(bundle, result, status, failed_stage=stage, diagnostic=diagnostic)
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--bundle", type=Path)
    mode.add_argument("--check-connection", action="store_true")
    parser.add_argument("--service")
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--create-service", action="store_true")
    organization = parser.add_mutually_exclusive_group()
    organization.add_argument("--expected-org")
    organization.add_argument("--use-key-organization", action="store_true",
                              help="Explicitly select this API key's organization without displaying its name")
    parser.add_argument("--expected-revision")
    parser.add_argument("--expected-run-id")
    parser.add_argument("--expected-attempt-id")
    args = parser.parse_args(argv)
    if args.check_connection:
        if args.send or args.create_service or any((args.expected_revision, args.expected_run_id, args.expected_attempt_id)):
            parser.error("--check-connection cannot be combined with publication options")
        if not (args.expected_org or args.use_key_organization) or not args.service:
            parser.error("--check-connection requires an organization selection and --service")
        return check_connection(args.expected_org, args.service, use_key_organization=args.use_key_organization)
    if args.service:
        parser.error("--service is only valid with --check-connection")
    if not args.bundle.is_dir() or args.bundle.is_symlink():
        print("Publication failed: bundle directory is missing or is a symlink", file=sys.stderr)
        return 2
    lock = args.bundle / ".publication.lock"
    try:
        with lock.open("x"):
            pass
        try:
            status = publish(args.bundle, send=args.send, create_service=args.create_service, expected={
                "org": args.expected_org, "revision": args.expected_revision,
                "run_id": args.expected_run_id, "attempt_id": args.expected_attempt_id,
            }, use_key_organization=args.use_key_organization)
        finally:
            lock.unlink()
        print("Publication result saved in publication-result.json")
        return status
    except (OSError, PublicationError):
        print("Publication failed: bundle is locked or result cannot be written", file=sys.stderr)
        return 2


def check_connection(expected_org, service, client=None, use_key_organization=False):
    try:
        require(not use_key_organization or expected_org is None,
                "Choose either an expected organization or the key organization")
        require(use_key_organization or (isinstance(expected_org, str) and expected_org),
                "Expected organization is required")
        require(isinstance(service, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,62}", service), "Invalid service name")
        if client is None:
            client = Client(os.environ.get("MACKEREL_APIKEY"))
        if use_key_organization:
            expected_org = key_organization(client)
        org = client.request("GET", "/api/v0/org")
        require(isinstance(org, dict) and org.get("name") == expected_org, "Mackerel organization mismatch")
        services = client.request("GET", "/api/v0/services")
        require(isinstance(services, dict) and isinstance(services.get("services"), list), "Invalid service listing")
        exists = any(isinstance(item, dict) and item.get("name") == service for item in services["services"])
        print("Mackerel authentication and organization verified; service " + ("exists" if exists else "is missing"))
        return 0
    except PublicationError as error:
        print("Mackerel connection check failed: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
