import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "run_crashfence.py"
FIXTURES = ROOT / "fixtures"


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        cp = subprocess.run(
            [sys.executable, "-I", str(LAUNCHER), *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return json.loads(cp.stdout)

    def test_simulate_outputs_utf8_json(self):
        report = self.run_cli(
            "simulate", str(FIXTURES / "scenario_revocation_priority.json"), "--compact-state"
        )
        self.assertEqual(report["status"], "safe")
        self.assertGreaterEqual(report["step_count"], 10)

    def test_check_outputs_trace_status(self):
        report = self.run_cli("check", str(FIXTURES / "trace_ambiguous.json"))
        self.assertEqual(report["status"], "ambiguous_authorization")
        self.assertIsNotNone(report["minimal_authorization_evidence"])


if __name__ == "__main__":
    unittest.main()
