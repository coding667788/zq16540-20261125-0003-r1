import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crashfence.durable import PHASE_NAMES, recover_root, run_phases

ROOT = _ROOT
WORKER = ROOT / "crashfence" / "worker.py"
TMP_BASE = ROOT / ".runtime-tmp" / "unittest-subprocess"
TMP_BASE.mkdir(parents=True, exist_ok=True)


class RealSubprocessCrashRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="kill-recover-", dir=TMP_BASE))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _wait_ready(self, proc, ready_file: Path):
        deadline = time.time() + 30
        while time.time() < deadline:
            if ready_file.exists():
                return json.loads(ready_file.read_text(encoding="utf-8"))
            code = proc.poll()
            if code is not None:
                out, err = proc.communicate(timeout=5)
                self.fail(f"worker exited early code={code}\nstdout={out}\nstderr={err}")
            time.sleep(0.03)
        proc.kill()
        out, err = proc.communicate(timeout=5)
        self.fail(f"timed out waiting for {ready_file}\nstdout={out}\nstderr={err}")

    def _recover_subprocess(self, ledger: Path):
        cp = subprocess.run(
            [sys.executable, "-I", str(WORKER), "recover", "--root", str(ledger)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return json.loads(cp.stdout)

    def test_kill_and_recover_at_all_six_durable_phases(self):
        expectations = {
            1: {"classification": "uncommitted_or_incomplete", "committed": False},
            2: {"classification": "last_frame_incomplete", "committed": False,
                "incomplete": ["n1"]},
            3: {"classification": "single_node_durable_waiting_for_quorum", "committed": False},
            4: {"classification": "quorum_durable_waiting_for_commit_marker", "committed": False},
            5: {"classification": "committed_waiting_for_read_token", "committed": True},
            6: {"classification": "readable", "committed": True, "visible": True},
        }

        for phase in range(1, 7):
            with self.subTest(phase=phase, phase_name=PHASE_NAMES[phase]):
                ledger = self.tmpdir / f"ledger-{phase}"
                ready_file = self.tmpdir / f"ready-{phase}.json"
                proc = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(WORKER),
                        "run",
                        "--root",
                        str(ledger),
                        "--crash-at",
                        str(phase),
                        "--ready-file",
                        str(ready_file),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                )
                try:
                    descriptor = self._wait_ready(proc, ready_file)
                    self.assertEqual(descriptor["phase"], phase)
                    # It is parked immediately before the parent kills it.
                    self.assertIsNone(proc.poll())
                finally:
                    if proc.poll() is None:
                        proc.kill()
                    try:
                        proc.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.communicate(timeout=10)

                self.assertIsNotNone(proc.returncode)
                report = self._recover_subprocess(ledger)
                expected = expectations[phase]
                self.assertEqual(report["classification"], expected["classification"], report)
                self.assertEqual(report["committed"], expected["committed"])
                if "visible" in expected:
                    self.assertEqual(
                        report["visible_to_token_holder"], expected["visible"], report
                    )
                if "incomplete" in expected:
                    self.assertEqual(report["incomplete_nodes"], expected["incomplete"])

    def test_committed_frame_corruption_is_not_classified_as_torn_tail(self):
        ledger = self.tmpdir / "corrupt-committed"
        run_phases(ledger)
        target = ledger / "nodes" / "n1" / "slot-0.log"
        with open(target, "ab") as handle:
            handle.write(b"\nCORRUPT-COMMITTED-BYTES")
            handle.flush()
        report = self._recover_subprocess(ledger)
        self.assertEqual(report["classification"], "committed_log_corruption")
        self.assertIn("n1", report["corruption_nodes"])
        self.assertFalse(report["visible_to_token_holder"])

        direct = recover_root(ledger)
        self.assertEqual(direct["classification"], "committed_log_corruption")


if __name__ == "__main__":
    unittest.main()
