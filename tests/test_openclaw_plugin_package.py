from __future__ import annotations

import json
import unittest
from pathlib import Path


class OpenClawPluginPackageTest(unittest.TestCase):
    def test_plugin_declares_the_exact_session_permit_contract(self) -> None:
        root = Path(__file__).resolve().parents[1] / "openclaw_plugin"
        manifest = json.loads((root / "openclaw.plugin.json").read_text(encoding="utf-8"))
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        source = (root / "index.js").read_text(encoding="utf-8")
        self.assertEqual(manifest["id"], "codeskill-r012-sidecar")
        self.assertEqual(manifest["providers"], ["codeskill-r012"])
        self.assertEqual(
            set(manifest["configSchema"]["required"]),
            {"permitDirectory", "trialId", "sessionId"},
        )
        self.assertIn("openclaw", package["peerDependencies"])
        self.assertIn('api.on("before_compaction"', source)
        self.assertIn("writeNativeSummaryPermit", source)
        self.assertIn("writeNormalCallBoundary", source)
        self.assertIn('"codeskill_normal_call_boundary", "normal-call"', source)
        self.assertIn('"codeskill_native_summary_permit", "native-summary"', source)
        self.assertNotIn("native-summary-${sessionId}", source)
        self.assertIn("wrapStreamFn", source)
        self.assertIn("StreamOptions.sessionId", source)
        self.assertIn("armedNativeSummary", source)
        self.assertIn("native_summary_stream_without_session_accepted", source)
        self.assertNotIn("prompt.includes", source)
        self.assertNotIn("messages.length ===", source)


if __name__ == "__main__":
    unittest.main()
