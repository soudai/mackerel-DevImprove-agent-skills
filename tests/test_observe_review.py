"""Independent cases for filtering after cursor pagination."""

import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "skills/mackerel-dev-improve/scripts"
SPEC = importlib.util.spec_from_file_location("observe_review", SCRIPTS / "observe.py")
observe = importlib.util.module_from_spec(SPEC)
with patch.object(sys, "path", [str(SCRIPTS)] + sys.path):
    SPEC.loader.exec_module(observe)


class PageReader:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        return self.pages[len(self.calls) - 1]


class ObservationScopeReviewTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"service_name": "app", "service_namespace": "", "environment": "development", "version": "rev-a"}
        self.request = {"body": {"from": 1792972800, "to": 1792973700, "serviceName": "app",
                                 "serviceNamespace": "", "severities": ["ERROR"], "perPage": 100}}

    def log(self, cursor, **attributes):
        attrs = {"deployment.environment.name": "development", "service.version": "rev-a"}
        attrs.update(attributes)
        return {"cursor": cursor, "timestamp": "2026-10-26T00:00:01Z", "severity": "ERROR",
                "body": "message", "traceId": None, "spanId": None, "serviceName": "app", "serviceNamespace": None,
                "resourceAttributes": [{"key": key, "value": {"valueType": "string", "stringValue": value}}
                                       for key, value in attrs.items() if value is not None]}

    def test_other_environment_page_does_not_prevent_reading_matching_next_page(self):
        reader = PageReader([
            {"results": [self.log("p1", **{"deployment.environment.name": "production"})],
             "pagination": {"hasNextPage": True, "endCursor": "p1"}},
            {"results": [self.log("p2")], "pagination": {"hasNextPage": False}},
        ])
        result = observe.collect_logs(reader, self.request, self.identity, 3, False)
        self.assertEqual("complete", result["status"])
        self.assertEqual((2, 1, 1), (result["scanned"], result["excluded_other_scope"], result["matched"]))
        self.assertEqual({"direction": "NEXT", "cursor": "p1"}, reader.calls[1][2]["pageCursor"])
        self.assertIsNone(result["records"][0]["trace_id"])

    def test_all_unscoped_records_remain_partial_even_when_traversal_finishes(self):
        reader = PageReader([{"results": [self.log("p1", **{"service.version": None})],
                              "pagination": {"hasNextPage": False}}])
        result = observe.collect_logs(reader, self.request, self.identity, 3, False)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["excluded_missing_scope"])
        self.assertEqual(0, result["matched"])
        self.assertEqual({}, result["severity_counts"])

    def test_legacy_environment_attribute_matches_but_conflicting_alias_does_not(self):
        reader = PageReader([{"results": [
            self.log("legacy", **{"deployment.environment.name": None, "deployment.environment": "development"}),
            self.log("conflict", **{"deployment.environment": "production"}),
        ], "pagination": {"hasNextPage": False}}])
        result = observe.collect_logs(reader, self.request, self.identity, 3, False)
        self.assertEqual(1, result["matched"])
        self.assertEqual(1, result["excluded_other_scope"])
        self.assertEqual({"ERROR": 1}, result["severity_counts"])


if __name__ == "__main__":
    unittest.main()
