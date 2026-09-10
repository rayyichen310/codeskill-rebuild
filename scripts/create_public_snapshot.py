"""Export one committed, sanitized public CODESKILL source snapshot.

The exporter deliberately reads Git blobs from a requested commit rather than
the working tree.  Consequently private history, untracked evidence, ignored
runtime configuration, and concurrent local edits cannot enter the output.
It creates a reviewable tree only; creating a remote and pushing remain
separate actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class PublicSnapshotError(RuntimeError):
    """The source cannot safely be exported as a public snapshot."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    blob: str
    path: str


# These patterns intentionally describe deployment-shaped values instead of
# embedding a particular private host, account, or workstation path.  That
# keeps the exported copy of this tool usable and free of those identifiers.
SANITIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/home/t2-\d+-\d+ti/ray/codeskill-rebuild(?:-\d+)?"), "<PROJECT_ROOT>"),
    (re.compile(r"/home/t2-\d+-\d+ti/ray/openclaw"), "<OPENCLAW_ROOT>"),
    (re.compile(r"/home/m\d{8}"), "<REMOTE_USER_HOME>"),
    (re.compile(r"(?<![A-Za-z0-9_])t2-\d+-\d+ti(?![A-Za-z0-9_])"), "<T2_HOST>"),
    (re.compile(r"(?<![A-Za-z0-9_])gpu\d+(?![A-Za-z0-9_])", re.IGNORECASE), "<MODEL_SERVICE_HOST>"),
    (re.compile(r"(?<![A-Za-z0-9_])140(?:\.\d{1,3}){3}(?![A-Za-z0-9_])"), "<MODEL_SERVICE_HOST>"),
    (re.compile(r"(?<![A-Za-z0-9_])m\d{8}(?![A-Za-z0-9_])"), "<REMOTE_USER>"),
    (re.compile(r"(?<![A-Za-z0-9_])ray\d{2}(?![A-Za-z0-9_])"), "<LOCAL_USER>"),
    (re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/](?:[^\s`\"'()<>\[\]{}]+)"), "<LOCAL_PATH>"),
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
)

RAW_ARTIFACT_SUFFIXES = {".sqlite", ".sqlite3", ".db", ".jsonl", ".har", ".pcap", ".pcapng"}
MAX_TRACKED_FILE_BYTES = 2 * 1024 * 1024
MANIFEST_NAME = "PUBLIC_SNAPSHOT_MANIFEST.json"


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise PublicSnapshotError(f"git {' '.join(args[:2])} failed: {message}")
    return completed.stdout


def _source_root(value: Path) -> Path:
    root = value.resolve()
    top_level = Path(_git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
    if root != top_level:
        raise PublicSnapshotError("--source-root must be the Git repository root")
    return root


def _commit(root: Path, ref: str) -> str:
    return _git(root, "rev-parse", "--verify", f"{ref}^{{commit}}").decode("ascii").strip()


def _entries(root: Path, commit: str) -> list[TreeEntry]:
    records = _git(root, "ls-tree", "-r", "-z", commit).split(b"\0")
    entries: list[TreeEntry] = []
    for record in records:
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", maxsplit=1)
        mode, object_type, blob = metadata.decode("ascii").split()
        if object_type != "blob":
            raise PublicSnapshotError("public snapshot accepts regular Git blobs only")
        path = encoded_path.decode("utf-8")
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
            raise PublicSnapshotError("tracked path is not safe for snapshot output")
        entries.append(TreeEntry(mode=mode, blob=blob, path=path))
    return entries


def _sanitize_text(text: str) -> tuple[str, int]:
    count = 0
    for pattern, replacement in SANITIZERS:
        text, replaced = pattern.subn(replacement, text)
        count += replaced
    return text, count


def _scan_text(path: str, text: str) -> None:
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise PublicSnapshotError(f"secret-like content detected in tracked file: {path}")


def _copy_entry(root: Path, entry: TreeEntry, output: Path) -> tuple[int, bool]:
    raw = _git(root, "cat-file", "blob", entry.blob)
    if len(raw) > MAX_TRACKED_FILE_BYTES:
        raise PublicSnapshotError(f"tracked file exceeds public snapshot size limit: {entry.path}")
    if Path(entry.path).suffix.lower() in RAW_ARTIFACT_SUFFIXES:
        raise PublicSnapshotError(f"raw runtime artifact cannot enter public snapshot: {entry.path}")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PublicSnapshotError(f"non-UTF-8 tracked file requires manual public review: {entry.path}") from error
    _scan_text(entry.path, source)
    sanitized, replacements = _sanitize_text(source)
    destination = output / entry.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(sanitized, encoding="utf-8", newline="\n")
    if entry.mode == "100755":
        os.chmod(destination, 0o755)
    return replacements, bool(Path(entry.path).suffix.lower() in RAW_ARTIFACT_SUFFIXES)


def _safe_output(root: Path, value: Path) -> Path:
    output = value.resolve()
    if output == root or root in output.parents:
        raise PublicSnapshotError("--output must be outside the private repository")
    if output.exists():
        raise PublicSnapshotError("--output must not already exist")
    return output


def export_snapshot(*, source_root: Path, commit_ref: str, output: Path) -> dict[str, object]:
    root = _source_root(source_root)
    commit = _commit(root, commit_ref)
    target = _safe_output(root, output)
    entries = _entries(root, commit)
    replacement_files: list[dict[str, object]] = []
    try:
        target.mkdir(parents=True)
        for entry in entries:
            replacements, _ = _copy_entry(root, entry, target)
            if replacements:
                replacement_files.append({"path": entry.path, "replacement_count": replacements})
        manifest: dict[str, object] = {
            "schema_version": 1,
            "kind": "codeskill_rebuild_public_snapshot",
            "source_commit": commit,
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "included_tracked_files": [entry.path for entry in entries],
            "excluded_categories": [
                "private_git_history",
                "untracked_files",
                "ignored_runtime_artifacts",
                "credentials",
            ],
            "sanitization": {
                "replacement_count": sum(int(item["replacement_count"]) for item in replacement_files),
                "files": replacement_files,
            },
            "safety_scan": {
                "tracked_file_size_limit_bytes": MAX_TRACKED_FILE_BYTES,
                "raw_artifact_suffixes_checked": sorted(RAW_ARTIFACT_SUFFIXES),
                "secret_pattern_matches": 0,
            },
        }
        (target / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        return manifest
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--commit", default="HEAD", help="Committed source ref to export; default: HEAD")
    parser.add_argument("--output", type=Path, required=True, help="New directory outside the private repository")
    args = parser.parse_args()
    manifest = export_snapshot(source_root=args.source_root, commit_ref=args.commit, output=args.output)
    print(json.dumps({"source_commit": manifest["source_commit"], "included_file_count": len(manifest["included_tracked_files"]), "replacement_count": manifest["sanitization"]["replacement_count"]}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except PublicSnapshotError as error:
        print(f"public snapshot blocked: {error}", file=sys.stderr)
        raise SystemExit(2) from error
