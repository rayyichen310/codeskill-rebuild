from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TERMINAL_BENCH_NAMESPACE = "terminal-bench/"


def canonical_instance_id(value: str) -> str:
    """Map the one approved Terminal-Bench namespace to its frozen split ID.

    The raw official task name remains provenance metadata.  Unknown namespaces
    and nested paths are intentionally left untouched rather than guessed.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("instance ID must be a nonempty string")
    if value.startswith(TERMINAL_BENCH_NAMESPACE):
        suffix = value[len(TERMINAL_BENCH_NAMESPACE) :]
        if suffix and "/" not in suffix:
            return suffix
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def contract_from_files(spec_path: Path, decisions_path: Path) -> dict[str, str]:
    """Build a run contract from the exact spec text rather than a hard-coded label."""
    spec_text = spec_path.read_text(encoding="utf-8")
    match = re.search(r"文件版本\s*[：:]\s*(v\d+\.\d+)", spec_text)
    if match is None:
        raise ValueError(f"cannot determine document version from {spec_path}")
    return {
        "version": match.group(1),
        "reproduction_spec_sha256": sha256_file(spec_path),
        "research_decisions_sha256": sha256_file(decisions_path),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    # A replace makes snapshot, ledger, and evidence files either the old
    # complete JSON or the new complete JSON after a crash, never a partial one.
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def write_contract_snapshot(run_dir: Path, spec_path: Path, decisions_path: Path, contract: dict[str, str]) -> dict[str, dict[str, str]]:
    """Copy the exact contract documents consumed by a new run into its evidence tree."""
    directory = run_dir / "contract"
    directory.mkdir(parents=True, exist_ok=True)
    copies: dict[str, dict[str, str]] = {}
    for label, source in (("reproduction_spec", spec_path), ("research_decisions", decisions_path)):
        destination = directory / source.name
        source_bytes = source.read_bytes()
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        copies[label] = {"path": str(destination), "sha256": sha256_file(destination)}
    if copies["reproduction_spec"]["sha256"] != contract["reproduction_spec_sha256"]:
        raise RuntimeError("spec changed while its contract snapshot was being written")
    if copies["research_decisions"]["sha256"] != contract["research_decisions_sha256"]:
        raise RuntimeError("research decisions changed while their contract snapshot was being written")
    write_json(directory / "contract.json", {"contract": contract, "documents": copies})
    return copies


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
