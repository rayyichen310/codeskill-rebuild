#!/usr/bin/env python3
"""Run the bounded R012 fake-CLI probe against one official npm release.

The command deliberately downloads a fresh OpenClaw npm tarball into a new
work directory and installs that exact tarball into a per-run npm prefix only.
It does not patch or mount a shared OpenClaw checkout. The nested probe runs
the resulting package read-only in Docker against only its local fake transport.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_tarball(tarball: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    with tarfile.open(tarball, "r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination_root and destination_root not in target.parents:
                raise ValueError(f"official package archive has an unsafe member path: {member.name}")
        archive.extractall(destination, members=members)


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="exact published npm version, for example 2026.9.3")
    parser.add_argument("--work-dir", type=Path, required=True, help="a new isolated work directory")
    parser.add_argument("--timeout-seconds", type=_positive_int, default=900)
    parser.add_argument(
        "--node-image",
        default="node:24-slim",
        help="isolated Docker Node image for the official package lifecycle and CLI probe",
    )
    args = parser.parse_args()
    if not isinstance(args.node_image, str) or not args.node_image or any(char.isspace() for char in args.node_image):
        parser.error("--node-image must be a nonempty Docker image reference without whitespace")
    if args.timeout_seconds > 900:
        parser.error("--timeout-seconds may not exceed the R012 controlled-probe limit of 900")
    deadline = time.monotonic() + args.timeout_seconds

    def remaining_timeout(cap: int) -> int:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("official-release compatibility budget elapsed before the next isolated step")
        return min(cap, remaining)

    repository = Path(__file__).resolve().parents[1]
    work_dir = args.work_dir.resolve()
    if work_dir.exists():
        raise SystemExit(f"--work-dir must not already exist: {work_dir}")
    work_dir.mkdir(parents=True)
    pack_dir = work_dir / "npm-pack"
    pack_dir.mkdir()
    package_spec = f"openclaw@{args.version}"
    npm_pack = ["npm", "pack", package_spec, "--pack-destination", str(pack_dir)]
    npm_result = subprocess.run(npm_pack, cwd=work_dir, text=True, capture_output=True, timeout=remaining_timeout(120))
    (work_dir / "npm-pack.stdout").write_text(npm_result.stdout, encoding="utf-8")
    (work_dir / "npm-pack.stderr").write_text(npm_result.stderr, encoding="utf-8")
    if npm_result.returncode != 0:
        raise SystemExit(f"npm pack failed for {package_spec}; see {work_dir / 'npm-pack.stderr'}")
    tarballs = sorted(pack_dir.glob("*.tgz"))
    if len(tarballs) != 1:
        raise SystemExit(f"npm pack did not produce exactly one tarball in {pack_dir}")
    tarball = tarballs[0]
    extracted = work_dir / "extracted"
    _extract_tarball(tarball, extracted)
    package_root = extracted / "package"
    package_json_path = package_root / "package.json"
    openclaw_entry = package_root / "openclaw.mjs"
    if not package_json_path.is_file() or not openclaw_entry.is_file():
        raise SystemExit("npm tarball is not a runnable OpenClaw package")
    package_json: dict[str, Any] = json.loads(package_json_path.read_text(encoding="utf-8"))
    if package_json.get("name") != "openclaw" or package_json.get("version") != args.version:
        raise SystemExit("npm tarball package identity does not match the requested official release")
    # The official tarball intentionally starts with a lifecycle-pending marker.
    # Install the exact packed tarball as a dependency of a fresh per-run npm
    # prefix. This is the supported package-manager shape and never operates
    # on a shared checkout, global prefix, or CODESKILL source directory.
    install_prefix = work_dir / "isolated-npm-prefix"
    npm_install = [
        "docker",
        "run",
        "--rm",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        "HOME=/work/npm-home",
        "-e",
        "npm_config_cache=/work/npm-cache",
        "-v",
        f"{work_dir}:/work:rw",
        "-w",
        "/work",
        args.node_image,
        "npm",
        "install",
        "--prefix",
        "/work/isolated-npm-prefix",
        f"/work/npm-pack/{tarball.name}",
        "--omit=dev",
        "--no-audit",
        "--no-fund",
    ]
    install_result = subprocess.run(
        npm_install,
        cwd=work_dir,
        text=True,
        capture_output=True,
        timeout=remaining_timeout(300),
    )
    (work_dir / "npm-install.stdout").write_text(install_result.stdout, encoding="utf-8")
    (work_dir / "npm-install.stderr").write_text(install_result.stderr, encoding="utf-8")
    if install_result.returncode != 0:
        raise SystemExit(f"isolated npm install failed for {package_spec}; see {work_dir / 'npm-install.stderr'}")
    installed_package_root = install_prefix / "node_modules" / "openclaw"
    installed_package_json_path = installed_package_root / "package.json"
    if not installed_package_json_path.is_file() or not (installed_package_root / "openclaw.mjs").is_file():
        raise SystemExit("isolated npm prefix has no runnable OpenClaw dependency")
    installed_package_json: dict[str, Any] = json.loads(installed_package_json_path.read_text(encoding="utf-8"))
    if installed_package_json.get("name") != "openclaw" or installed_package_json.get("version") != args.version:
        raise SystemExit("isolated npm dependency identity does not match the requested official release")
    lifecycle_pending_after_install = (installed_package_root / ".openclaw-lifecycle-pending").exists()
    if lifecycle_pending_after_install:
        raise SystemExit("official package lifecycle still reports pending after isolated npm install")

    probe_root = work_dir / "probe"
    probe = repository / "scripts" / "run_openclaw_r012_fake_cli_probe.py"
    command = [
        sys.executable,
        str(probe),
        str(probe_root),
        "--openclaw-root",
        str(installed_package_root),
        "--node-image",
        args.node_image,
        "--openclaw-mount-root",
        str(install_prefix),
    ]
    env = dict(os.environ)
    source_path = str(repository / "src")
    env["PYTHONPATH"] = source_path + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    probe_result = subprocess.run(
        command,
        cwd=repository,
        text=True,
        capture_output=True,
        timeout=remaining_timeout(900),
        env=env,
    )
    (work_dir / "probe.stdout").write_text(probe_result.stdout, encoding="utf-8")
    (work_dir / "probe.stderr").write_text(probe_result.stderr, encoding="utf-8")
    status_path = probe_root / "status.json"
    probe_status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else None
    result = {
        "schema_version": 1,
        "kind": "r012_official_openclaw_release_compatibility",
        "published_package_spec": package_spec,
        "published_tarball": {
            "filename": tarball.name,
            "sha256": _sha256(tarball),
            "npm_pack_command": npm_pack,
            "shared_source_mutation": "not_performed",
        },
        "isolated_package_install": {
            "command": npm_install,
            "exit_code": install_result.returncode,
            "lifecycle_pending_after_install": lifecycle_pending_after_install,
            "install_prefix": str(install_prefix),
            "node_image": args.node_image,
            "shared_install_change": "not_performed",
        },
        "extracted_package": {
            "name": package_json["name"],
            "version": package_json["version"],
            "entrypoint": str(openclaw_entry),
        },
        "installed_package": {
            "name": installed_package_json["name"],
            "version": installed_package_json["version"],
            "entrypoint": str(installed_package_root / "openclaw.mjs"),
        },
        "probe_command": command,
        "probe_exit_code": probe_result.returncode,
        "probe_native_compaction_evidence_verified": probe_status.get("native_compaction_evidence_verified") if isinstance(probe_status, dict) else None,
        "probe_status_path": str(status_path),
        "scope": "official npm release in an isolated directory plus fake-only transport; not a real model, GPU, solver, verifier, or formal trial",
    }
    (work_dir / "official-release-compatibility.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    if probe_result.returncode != 0 or result["probe_native_compaction_evidence_verified"] is not True:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
