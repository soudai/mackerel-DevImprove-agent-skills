#!/usr/bin/env python3
"""Read bounded Mackerel evidence. Offline request plan unless --fetch is set."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import quote, urlencode

import publish


SEVERITIES = {"UNSPECIFIED", "TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}


def require(condition, message):
    publish.require(condition, message)


def text(value, limit=256):
    return isinstance(value, str) and 0 < len(value) <= limit and all(ord(c) >= 32 for c in value)


def integer(value, minimum=0, maximum=publish.MAX_INTEGER):
    return type(value) is int and minimum <= value <= maximum


def config_file(path):
    cfg = publish.parse_json(publish.read_file(path))
    required = {"schema_version", "mackerel_service", "identity", "window", "metrics", "apm", "logs"}
    require(isinstance(cfg, dict) and required.issubset(cfg) and set(cfg).issubset(required | {"host_metrics"}), "Invalid observation config fields")
    require(type(cfg["schema_version"]) is int and cfg["schema_version"] == 1, "Unsupported observation schema")
    require(isinstance(cfg["mackerel_service"], str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,62}", cfg["mackerel_service"]), "Invalid Mackerel service")
    identity = cfg["identity"]
    require(isinstance(identity, dict) and set(identity) == {"service_name", "service_namespace", "environment", "version"}, "Invalid observation identity")
    require(all(text(identity[k]) for k in ("service_name", "environment")), "Service and environment are required")
    require(identity["version"] is None or text(identity["version"]), "Version must be an exact nonempty string or explicit null for all versions")
    require(identity["service_namespace"] == "" or text(identity["service_namespace"]), "Invalid service namespace")
    window = cfg["window"]
    require(isinstance(window, dict) and set(window) == {"from", "to"} and all(integer(v) for v in window.values()), "Invalid observation window")
    require(0 < window["to"] - window["from"] <= 20 * 3600, "Observation window must be positive and at most 20 hours")
    metrics = cfg["metrics"]
    require(isinstance(metrics, list) and len(metrics) <= 32 and all(isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9._-]{1,255}", v) for v in metrics), "Invalid fixed metric list")
    require(len(set(metrics)) == len(metrics), "Duplicate metric selector")
    hosts = cfg.get("host_metrics", [])
    require(isinstance(hosts, list) and len(metrics) + len(hosts) <= 32, "At most 32 service and host metric selectors are allowed")
    names, selectors = set(), set()
    for item in hosts:
        require(isinstance(item, dict) and set(item) == {"name", "host_id", "metric"}, "Invalid host metric selector fields")
        require(isinstance(item["name"], str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item["name"]), "Invalid fixed host metric name")
        require(isinstance(item["host_id"], str) and re.fullmatch(r"[A-Za-z0-9]{1,64}", item["host_id"]), "Invalid explicit host ID")
        require(isinstance(item["metric"], str) and re.fullmatch(r"[a-zA-Z0-9._-]{1,255}", item["metric"]), "Invalid host metric name")
        selector = (item["host_id"], item["metric"])
        require(item["name"] not in names and selector not in selectors, "Duplicate host metric selector")
        names.add(item["name"])
        selectors.add(selector)
    apm = cfg["apm"]
    require(isinstance(apm, dict) and set(apm) == {"http", "database", "sampling_context"} and all(type(apm[k]) is bool for k in ("http", "database")), "Invalid APM selectors")
    require(text(apm["sampling_context"], 1024), "Record the APM sampling context, including unknown when unverified")
    require(not (apm["http"] or apm["database"]) or window["to"] - window["from"] <= 1800, "APM queries are limited to 30 minutes; do not average window percentiles")
    logs = cfg["logs"]
    require(isinstance(logs, dict) and set(logs).issubset({"enabled", "severities", "trace_id"}) and {"enabled", "severities"}.issubset(logs), "Invalid log selectors")
    require(type(logs["enabled"]) is bool and isinstance(logs["severities"], list) and 0 < len(logs["severities"]) <= len(SEVERITIES) and all(isinstance(s, str) and s in SEVERITIES for s in logs["severities"]), "Invalid log severity filter")
    require(len(set(logs["severities"])) == len(logs["severities"]), "Duplicate severity filter")
    require("trace_id" not in logs or (isinstance(logs["trace_id"], str) and re.fullmatch(r"[a-fA-F0-9]{32}", logs["trace_id"])), "Invalid trace ID")
    require(metrics or hosts or apm["http"] or apm["database"] or logs["enabled"], "Select at least one signal")
    return cfg


def plan(cfg):
    identity, window = cfg["identity"], cfg["window"]
    result = {"service_metrics": [{"method": "GET", "path": "/api/v0/services/" + quote(cfg["mackerel_service"], safe="") + "/metrics?" + urlencode(dict(window, name=name))} for name in cfg["metrics"]]}
    result["host_metrics"] = [{"method": "GET", "path": "/api/v0/hosts/" + quote(item["host_id"], safe="") + "/metrics?" + urlencode(dict(window, name=item["metric"]))} for item in cfg.get("host_metrics", [])]
    for kind, endpoint in (("http", "http-server-stats"), ("database", "db-query-stats")):
        if cfg["apm"][kind]:
            params = dict(window, serviceName=identity["service_name"], serviceNamespace=identity["service_namespace"], environment=identity["environment"], orderColumn="SUM", orderDirection="DESC", page=1, perPage=100)
            if identity["version"] is not None:
                params["version"] = identity["version"]
            result[kind] = {"method": "GET", "path": "/api/v0/apm/" + endpoint, "params": params}
    if cfg["logs"]["enabled"]:
        body = dict(window, serviceName=identity["service_name"], serviceNamespace=identity["service_namespace"], severities=cfg["logs"]["severities"], order={"column": "TIMESTAMP", "direction": "DESC"}, perPage=100)
        if "trace_id" in cfg["logs"]:
            body["traceId"] = cfg["logs"]["trace_id"].lower()
        result["logs"] = {"method": "POST", "path": "/api/v0/logs", "body": body}
    return result


class PacedReader:
    """POST /logs is a read; restrict all calls to the known read endpoints."""
    def __init__(self, client, sleep=time.sleep, monotonic=time.monotonic):
        self.client, self.sleep, self.monotonic = client, sleep, monotonic
        self.last = None

    def request(self, method, path, body=None):
        require(method == "GET" or (method == "POST" and path == "/api/v0/logs"), "Observation only supports reads")
        if self.last is not None:
            self.sleep(max(0, 1.1 - (self.monotonic() - self.last)))
        self.last = self.monotonic()
        return self.client.request(method, path, body)


def valid_number(value):
    return publish.number(value) and abs(value) <= publish.MAX_INTEGER


def collect_metric(reader, request, window):
    response = reader.request("GET", request["path"])
    require(isinstance(response, dict) and isinstance(response.get("metrics"), list), "Invalid service metric response")
    points, seen = [], set()
    for point in response["metrics"]:
        require(isinstance(point, dict) and integer(point.get("time")) and valid_number(point.get("value")), "Invalid metric point")
        stamp = point["time"]
        require(window["from"] <= stamp <= window["to"] and stamp not in seen, "Metric timestamp is outside window or duplicated")
        seen.add(stamp)
        points.append({"time": stamp, "value": point["value"]})
    points.sort(key=lambda p: p["time"])
    return {"status": "complete" if points else "missing", "points": points, "point_count": len(points), "latest": points[-1] if points else None,
            "age_at_window_end_seconds": window["to"] - points[-1]["time"] if points else None,
            "interpretation": "Stored metric series; no revision/environment ownership or continuous coverage is inferred."}


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def apm_row(row, kind, include_text):
    require(isinstance(row, dict), "Invalid APM row")
    count_key = "requestCount" if kind == "http" else "executionCount"
    require(integer(row.get(count_key)), "Invalid APM count")
    for key in ("totalMillis", "averageMillis", "approxP95Millis"):
        require(valid_number(row.get(key)) and row[key] >= 0, "Invalid APM latency")
    result = {key: row[key] for key in (count_key, "totalMillis", "averageMillis", "approxP95Millis")}
    identity_keys = ("method", "route") if kind == "http" else ("query",)
    require(all(isinstance(row.get(key), str) and len(row[key]) <= 65536 for key in identity_keys), "Invalid APM group identity")
    result["group_sha256"] = digest(json.dumps([row[k] for k in identity_keys], ensure_ascii=False))
    if include_text:
        result.update({k: row[k] for k in identity_keys})
    if kind == "http":
        require(valid_number(row.get("errorRatePercentage")) and 0 <= row["errorRatePercentage"] <= 100, "Invalid HTTP error rate")
        result["error_ratio"] = row["errorRatePercentage"] / 100
    return result


def collect_apm(reader, request, kind, max_pages, include_text):
    rows, seen, pages = [], set(), 0
    has_next = True
    while has_next and pages < max_pages:
        params = dict(request["params"], page=pages + 1)
        response = reader.request("GET", request["path"] + "?" + urlencode(params))
        require(isinstance(response, dict) and isinstance(response.get("results"), list) and type(response.get("hasNextPage")) is bool, "Invalid APM pagination")
        require(len(response["results"]) <= 100, "Oversized APM page")
        for raw in response["results"]:
            row = apm_row(raw, kind, include_text)
            require(row["group_sha256"] not in seen, "APM groups repeated across pages; remeasure after ingestion settles")
            seen.add(row["group_sha256"])
            rows.append(row)
        has_next = response["hasNextPage"]
        require(not has_next or response["results"], "Empty APM page has a next page")
        pages += 1
    return {"status": "truncated" if has_next else ("complete" if rows else "missing"), "pages": pages, "rows": rows,
            "version_filter_applied": "version" in request["params"],
            "population": "Received matching spans, not all application requests; percentiles are not additive."}


def resource_strings(raw):
    require(isinstance(raw, list), "Invalid log resource attributes")
    attrs = {}
    for entry in raw:
        require(isinstance(entry, dict) and isinstance(entry.get("key"), str) and isinstance(entry.get("value"), dict), "Invalid log resource attribute")
        key, value = entry["key"], entry["value"]
        require(key not in attrs and value.get("valueType") == "string" and isinstance(value.get("stringValue"), str), "Invalid or duplicate log resource attribute")
        attrs[key] = value["stringValue"]
    return attrs


def log_epoch(raw):
    require(isinstance(raw, str), "Invalid log timestamp")
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        require(stamp.tzinfo is not None, "Log timestamp needs timezone")
        return stamp.timestamp()
    except (ValueError, OverflowError):
        raise publish.PublicationError("Invalid log timestamp") from None


def collect_logs(reader, request, identity, max_pages, include_text):
    records, cursors, pages, scanned, excluded, unscoped = [], set(), 0, 0, 0, 0
    body = dict(request["body"])
    version_filter_applied = identity["version"] is not None
    has_next = True
    while has_next and pages < max_pages:
        response = reader.request("POST", "/api/v0/logs", body)
        require(isinstance(response, dict) and isinstance(response.get("results"), list) and isinstance(response.get("pagination"), dict), "Invalid log response")
        pagination = response["pagination"]
        require(type(pagination.get("hasNextPage")) is bool and len(response["results"]) <= 100, "Invalid log pagination")
        page_cursors = []
        for row in response["results"]:
            require(isinstance(row, dict) and text(row.get("cursor"), 4096) and row["cursor"] not in cursors, "Duplicate or invalid log cursor")
            cursors.add(row["cursor"])
            page_cursors.append(row["cursor"])
            stamp = log_epoch(row.get("timestamp"))
            require(body["from"] <= stamp < body["to"], "Log timestamp is outside search window")
            namespace = row.get("serviceNamespace")
            require(namespace is None or isinstance(namespace, str), "Invalid log service namespace")
            require(row.get("serviceName") == identity["service_name"] and (namespace or "") == identity["service_namespace"], "Log service differs from requested scope")
            severity = row.get("severity")
            require(isinstance(severity, str) and severity in body["severities"], "Log severity differs from requested filter")
            trace, span = row.get("traceId"), row.get("spanId")
            for value, width in ((trace, 32), (span, 16)):
                require(value is None or (isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{" + str(width) + "}", value)), "Invalid log correlation ID")
            if "traceId" in body:
                require(isinstance(trace, str) and trace.lower() == body["traceId"], "Log trace differs from requested filter")
            attrs = resource_strings(row.get("resourceAttributes"))
            environments = {attrs[k] for k in ("deployment.environment.name", "deployment.environment") if k in attrs}
            scanned += 1
            version = attrs.get("service.version") or None
            if not environments or (version_filter_applied and version is None):
                unscoped += 1
                continue
            if environments != {identity["environment"]} or (version_filter_applied and version != identity["version"]):
                excluded += 1
                continue
            require(isinstance(row.get("body"), str), "Invalid log body")
            record = {"timestamp": row["timestamp"], "severity": severity, "trace_id": trace, "span_id": span,
                      "service_version": version, "body_sha256": digest(row["body"])}
            if include_text:
                record["body"] = row["body"]
            records.append(record)
        has_next = pagination["hasNextPage"]
        if has_next:
            cursor = pagination.get("endCursor")
            require(page_cursors and cursor == page_cursors[-1], "Invalid log next-page cursor")
            body = dict(body, pageCursor={"direction": "NEXT", "cursor": cursor})
        pages += 1
    return {"status": "truncated" if has_next else ("partial" if unscoped else "complete"), "pages": pages, "scanned": scanned,
            "excluded_other_scope": excluded, "excluded_missing_scope": unscoped, "matched": len(records), "records": records,
            "version_filter_applied": version_filter_applied,
            "matched_without_version": sum(r["service_version"] is None for r in records),
            "severity_counts": {s: sum(r["severity"] == s for r in records) for s in sorted(set(r["severity"] for r in records))},
            "interpretation": "Counts of retrieved logs, not bug counts or request error rates. Environment is filtered locally; explicit version=null includes all versions and cannot establish revision ownership."}


def observe(cfg, client, expected_org, max_pages=3, include_text=False, sleep=time.sleep):
    require(text(expected_org), "Expected organization is required")
    require(integer(max_pages, 1, 20), "Max pages must be 1..20")
    reader = PacedReader(client, sleep=sleep)
    require(publish.key_organization(reader) == expected_org, "API key organization differs from expected organization")
    queries = plan(cfg)
    signals = {}
    for name, request in zip(cfg["metrics"], queries["service_metrics"]):
        try:
            signals["metric:" + name] = collect_metric(reader, request, cfg["window"])
        except publish.PublicationError as error:
            signals["metric:" + name] = {"status": "error", "diagnostic": str(error)}
    for item, request in zip(cfg.get("host_metrics", []), queries["host_metrics"]):
        try:
            signals["host_metric:" + item["name"]] = dict(collect_metric(reader, request, cfg["window"]),
                                                        host_id=item["host_id"], metric=item["metric"])
        except publish.PublicationError as error:
            signals["host_metric:" + item["name"]] = {"status": "error", "diagnostic": str(error),
                                                    "host_id": item["host_id"], "metric": item["metric"]}
    for kind in ("http", "database", "logs"):
        if kind not in queries:
            continue
        try:
            if kind == "logs":
                signals[kind] = collect_logs(reader, queries[kind], cfg["identity"], max_pages, include_text)
            else:
                signals[kind] = collect_apm(reader, queries[kind], kind, max_pages, include_text)
        except publish.PublicationError as error:
            signals[kind] = {"status": "error", "diagnostic": str(error)}
    return {"schema_version": 1, "source": "mackerel-observation/v1", "status": "complete" if all(s["status"] == "complete" for s in signals.values()) else "partial",
            "config": cfg, "collected_at": datetime.now(timezone.utc).isoformat(), "max_pages_per_signal": max_pages,
            "runtime_version_filter_applied": cfg["identity"]["version"] is not None,
            "text_included": include_text, "signals": signals,
            "decision": "Evidence collection only. Completeness of API traversal does not prove instrumentation coverage or improvement."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fetch", action="store_true", help="Read Mackerel using MACKEREL_APIKEY; default writes an offline request plan")
    parser.add_argument("--expected-org")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--include-text", action="store_true", help="Include raw log bodies and APM route/SQL text in the local evidence artifact")
    args = parser.parse_args(argv)
    try:
        require(args.config.resolve() != args.out.resolve(), "Output must differ from config")
        require(not args.out.exists(), "Output already exists; use a new evidence path")
        require(integer(args.max_pages, 1, 20), "Max pages must be 1..20")
        cfg = config_file(args.config)
        if args.fetch:
            require(text(args.expected_org), "--expected-org is required with --fetch")
            result = observe(cfg, publish.Client(os.environ.get("MACKEREL_APIKEY")), args.expected_org, args.max_pages, args.include_text)
        else:
            result = {"schema_version": 1, "status": "planned", "config": cfg, "requests": plan(cfg), "max_pages_per_signal": args.max_pages, "text_included": args.include_text,
                      "runtime_version_filter_applied": cfg["identity"]["version"] is not None}
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(publish.json_bytes(result))
        print("Observation " + result["status"])
        return 2 if result["status"] == "partial" else 0
    except publish.PublicationError as error:
        print("Observation failed: " + str(error), file=sys.stderr)
        return 1
    except OSError:
        print("Observation failed: input or output is inaccessible.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
