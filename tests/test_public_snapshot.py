from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def snapshot_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "create_public_snapshot.py"
    spec = importlib.util.spec_from_file_location("public_snapshot_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class PublicSnapshotTest(unittest.TestCase):
    def make_repository(self, root: Path, files: dict[str, str]) -> None:
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
            check=True,
            capture_output=True,
        )

    def test_exports_only_committed_files_and_sanitizes_environment_fields(self) -> None:
        module = snapshot_module()
        endpoint = ".".join(("140", "118", "202", "100"))
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source, output = work / "private", work / "public"
            self.make_repository(
                source,
                {
                    "README.md": "source\n",
                    "docs/connection.md": f"host <T2_HOST> <MODEL_SERVICE_HOST> {endpoint} <REMOTE_USER> <LOCAL_USER> <PROJECT_ROOT> <LOCAL_PATH>",
                },
            )
            (source / "untracked-runtime.json").write_text('{"token":"not tracked"}\n', encoding="utf-8")
            manifest = module.export_snapshot(source_root=source, commit_ref="HEAD", output=output)
            exported = (output / "docs" / "connection.md").read_text(encoding="utf-8")
            self.assertIn("<PROJECT_ROOT>", exported)
            self.assertIn("<MODEL_SERVICE_HOST>", exported)
            self.assertIn("<LOCAL_PATH>", exported)
            self.assertNotIn(endpoint, exported)
            self.assertFalse((output / "untracked-runtime.json").exists())
            saved_manifest = json.loads((output / module.MANIFEST_NAME).read_text(encoding="utf-8"))
            self.assertEqual(saved_manifest["source_commit"], manifest["source_commit"])
            self.assertIn("untracked_files", saved_manifest["excluded_categories"])
            self.assertGreater(saved_manifest["sanitization"]["replacement_count"], 0)

    def test_blocks_a_tracked_secret_like_value(self) -> None:
        module = snapshot_module()
        fake_token = "gh" + "p_" + "abcdefghijklmnopqrstuvwx"
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            source, output = work / "private", work / "public"
            self.make_repository(source, {"leak.txt": fake_token + "\n"})
            with self.assertRaisesRegex(module.PublicSnapshotError, "secret-like content"):
                module.export_snapshot(source_root=source, commit_ref="HEAD", output=output)
            self.assertFalse(output.exists())
