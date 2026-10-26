#!/usr/bin/env python3
"""Generate a Mackerel dashboard API payload locally from a validated bundle.

This command does not deploy a dashboard or claim that its series exist remotely.
Review the JSON, apply it to the intended organization, and verify the readback.
API schema: https://mackerel.io/api-docs/entry/dashboards
Expressions: https://mackerel.io/docs/entry/advanced/advanced-graph
"""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
from urllib.parse import quote, urlsplit

import publish
import observe


PATH_RE = re.compile(r"[A-Za-z0-9_][-A-Za-z0-9_]*(?:/[A-Za-z0-9_][-A-Za-z0-9_]*)*\Z")


def link_url(value):
    """Accept explicit HTTPS links, escape Markdown delimiters, never fetch them."""
    publish.require(isinstance(value, str) and 0 < len(value) <= 2048
                    and not any(ord(char) < 33 or ord(char) == 127 for char in value),
                    "Dashboard link must be a valid HTTPS URL")
    try:
        parsed = urlsplit(value)
        publish.require(parsed.scheme == "https" and parsed.hostname
                        and not parsed.username and not parsed.password
                        and parsed.port in (None, 443), "Dashboard link must be HTTPS without credentials")
    except ValueError as error:
        raise publish.PublicationError("Dashboard link must be a valid HTTPS URL") from error
    return quote(value, safe=":/?&=%#+,;@!$*~-._")


def text_value(value):
    publish.require(isinstance(value, str) and 0 < len(value) <= 200
                    and not any(ord(char) < 32 or ord(char) == 127 for char in value),
                    "Dashboard title must be nonempty plain text")
    return value


def generate(bundle, *, title=None, url_path=None, apm_url=None, logs_url=None, observation_config=None):
    publish.require(bundle.is_dir() and not bundle.is_symlink(), "Bundle directory is missing or is a symlink")
    manifest, metrics = publish.load_bundle(bundle)
    service = manifest["mackerel_service"]
    observation = observe.config_file(observation_config) if observation_config is not None else None
    if observation is not None:
        publish.require(observation["mackerel_service"] == service,
                        "Observation service differs from the quality bundle")
    title = text_value(title if title is not None else service + " development quality")
    url_path = url_path if url_path is not None else "dev-improve/" + service
    publish.require(isinstance(url_path, str) and len(url_path) <= 200 and PATH_RE.fullmatch(url_path),
                    "Invalid dashboard URL path")
    measured = datetime.fromtimestamp(manifest["publication"]["measurement_timestamp"], timezone.utc).isoformat()
    notes = [
        "Graphs read stored metrics; this payload does not publish measurements.",
        "Missing points are unknown, not zero. Check completeness and measurement timestamps before comparing.",
        "Timestamp values use Unix seconds; they are measurement times, not a continuously updated age.",
        "The source bundle was measured at " + measured + ". Its link below is a fixed snapshot, not the latest run.",
        "Resolve the latest run and profile definitions through service metadata `dev-quality`.",
    ]
    if observation is not None:
        notes.extend([
            "Runtime service and host metrics use native stored units, one selector per graph. Confirm units, counter resets and host membership before comparing.",
            "Runtime series do not prove environment or version ownership. Match the target hosts, service and time range with the observation evidence.",
            "The observation config time window is not applied to this dashboard. Select the intended display time range in Mackerel.",
        ])
    artifact = manifest.get("artifact_uri")
    if artifact:
        notes.append("[Source bundle evidence](" + link_url(artifact) + ")")
    for label, url in (("APM investigation", apm_url), ("Log investigation", logs_url)):
        if url is not None:
            notes.append("[" + label + "](" + link_url(url) + ")")
    widgets = [{"type": "markdown", "title": "Measurement context", "markdown": "\n\n".join(notes),
                "layout": {"x": 0, "y": 0, "width": 24, "height": 8}}]
    present = {item["name"] for item in metrics}

    def append(widget):
        index = len(widgets) - 1
        widget["layout"] = {"x": (index % 2) * 12, "y": 8 + (index // 2) * 6, "width": 12, "height": 6}
        widgets.append(widget)

    def graph(prefix, group, suffixes, label, maximum=None):
        names = [prefix + group + "." + suffix for suffix in suffixes if prefix + group + "." + suffix in present]
        if not names:
            return
        # Select exact metrics rather than guessing the remote service graph
        # definition name. Both service and metric names passed here have been
        # restricted to safe identifier characters by load_bundle.
        expressions = ["service('" + service + "', '" + name + "')" for name in names]
        expression = expressions[0] if len(expressions) == 1 else "group(" + ", ".join(expressions) + ")"
        widget = {"type": "graph", "title": label,
                  "graph": {"type": "expression", "expression": expression}, "valueRange": {"min": 0}}
        if maximum is not None:
            widget["valueRange"]["max"] = maximum
        append(widget)

    def stamp(prefix, name, label):
        if prefix + name in present:
            append({"type": "value", "title": label,
                    "metric": {"type": "service", "serviceName": service, "name": prefix + name},
                    "fractionSize": 0, "suffix": " Unix seconds"})

    # Stable profile order and fixed series names. Never put revisions, case IDs,
    # source paths or function names in metric names or graph selectors.
    for profile, config in sorted(manifest["profiles"].items()):
        if config["scope"]["kind"] in publish.TEST_KINDS:
            prefix = "tests." + profile + "."
            graph(prefix, "run", ["report_complete", "complete", "success"], profile + " report / run / success (0..1)", 1)
            stamp(prefix, "run.last_completed_timestamp", profile + " last complete measurement")
            graph(prefix, "cases", ["selected", "passed", "failed", "error", "skipped"], profile + " cases (selected is the total)")
            graph(prefix, "ratios", ["strict_pass"], profile + " strict pass ratio (0..1)", 1)
            graph(prefix, "run", ["duration_seconds"], profile + " elapsed run time (seconds)")
        else:
            prefix = "cccc." + profile + "."
            graph(prefix, "analysis", ["complete"], profile + " analysis completeness (0..1)", 1)
            stamp(prefix, "analysis.last_success_timestamp", profile + " last complete analysis")
            graph(prefix, "analysis", ["files", "functions"], profile + " analysis population (counts)")
            graph(prefix, "analysis", ["parse_errors", "parse_error_files"], profile + " analysis errors (counts)")
            for dimension in ("cognitive", "cyclomatic"):
                graph(prefix, dimension, ["p95", "max"], profile + " " + dimension + " complexity")
                graph(prefix, dimension, ["over_limit_count"], profile + " " + dimension + " over limit (count)")
                graph(prefix, dimension, ["over_limit_ratio"], profile + " " + dimension + " over limit (0..1)", 1)
    if observation is not None:
        # config_file restricts identifiers to safe characters, excluding quotes,
        # wildcards and expression operators. Native units are not inferred from
        # a metric name, and independent selectors never share an axis.
        for name in observation["metrics"]:
            append({"type": "graph", "title": "Service metric: " + name,
                    "graph": {"type": "expression", "expression": "service('" + service + "', '" + name + "')"}})
        for selector in observation.get("host_metrics", []):
            append({"type": "graph", "title": "Host " + selector["name"] + ": " + selector["metric"],
                    "graph": {"type": "expression", "expression": "host('" + selector["host_id"] + "', '" + selector["metric"] + "')"}})
    return {"title": title, "memo": "Generated by mackerel-dev-improve. Review and verify dashboard API readback before use.",
            "urlPath": url_path, "widgets": widgets}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--title")
    parser.add_argument("--url-path")
    parser.add_argument("--apm-url", help="Explicit HTTPS link to the intended APM view")
    parser.add_argument("--logs-url", help="Explicit HTTPS link to the intended logs view")
    parser.add_argument("--observation-config", type=Path,
                        help="Validated observe.py config supplying additional service and host metric selectors")
    args = parser.parse_args(argv)
    try:
        result = generate(args.bundle, title=args.title, url_path=args.url_path,
                          apm_url=args.apm_url, logs_url=args.logs_url, observation_config=args.observation_config)
        protected = {(args.bundle / name).resolve() for name in ("manifest.json", "metrics.json", "details.json")}
        if args.observation_config is not None:
            protected.add(args.observation_config.resolve())
        publish.require(not args.out.is_symlink() and args.out.resolve() not in protected,
                        "Dashboard output must not replace bundle evidence")
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(publish.json_bytes(result))
        print("Dashboard payload saved locally; no Mackerel request was made")
        return 0
    except (publish.PublicationError, OSError, ValueError, KeyError, TypeError, OverflowError):
        print("Dashboard generation failed: invalid bundle, options, or output path", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
