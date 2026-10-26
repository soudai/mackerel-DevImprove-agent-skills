"""Exercise the shipped portable example through both public CLI entry points."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SKILL = Path(__file__).resolve().parents[1] / "skills/mackerel-dev-improve"


class PortableCLITest(unittest.TestCase):
    def test_example_from_another_directory_preserves_failure_and_publishes_offline(self):
        with tempfile.TemporaryDirectory(prefix="quality cli ") as directory:
            root = Path(directory)
            bundle = root / "normalized output"
            normalized = subprocess.run([
                sys.executable, str(SKILL / "scripts/normalize.py"),
                "--config", str(SKILL / "assets/project.portable.example.json"),
                "--run", str(SKILL / "assets/run.portable.example.json"),
                "--output", str(bundle),
            ], cwd=root, capture_output=True, text=True)
            self.assertEqual(0, normalized.returncode, normalized.stderr)
            manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
            metrics = {item["name"]: item["value"] for item in json.loads(
                (bundle / "metrics.json").read_text(encoding="utf-8"))}
            self.assertTrue(manifest["collection"]["unit"]["complete"])
            self.assertEqual(1, manifest["collection"]["unit"]["exit_code"])
            self.assertEqual(0, metrics["tests.unit.run.success"])
            self.assertEqual(3, metrics["tests.unit.cases.selected"])
            self.assertEqual(0.5, metrics["tests.unit.ratios.strict_pass"])
            self.assertEqual(64, len(manifest["profiles"]["unit"]["comparison_key"]))
            published = subprocess.run([
                sys.executable, str(SKILL / "scripts/publish.py"), "--bundle", str(bundle),
            ], cwd=root, capture_output=True, text=True)
            self.assertEqual(0, published.returncode, published.stderr)
            result = json.loads((bundle / "publication-result.json").read_text(encoding="utf-8"))
            self.assertEqual("dry_run", result["status"])
            self.assertEqual(manifest["publication"]["payload_sha256"], result["payload_sha256"])


if __name__ == "__main__":
    unittest.main()
