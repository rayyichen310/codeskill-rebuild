from __future__ import annotations

from pathlib import Path
import os
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class OfficialReleaseCompatibilityRunnerTest(unittest.TestCase):
    def test_help_declares_a_bounded_fresh_npm_release_probe(self) -> None:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "run_openclaw_r012_official_release_compat.py"), "--help"],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("official npm release", result.stdout)
        self.assertIn("--version", result.stdout)
        self.assertIn("--timeout-seconds", result.stdout)


if __name__ == "__main__":
    unittest.main()
