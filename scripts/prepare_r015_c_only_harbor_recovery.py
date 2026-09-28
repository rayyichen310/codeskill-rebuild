#!/usr/bin/env python3
"""Prepare an explicit no-rerun recovery manifest for one Harbor trial.

The command validates the original driver input, terminal failed driver
process, importer failure, official Harbor result artifacts, and sidecar
attempt hashes.  It writes one fresh recovery namespace plus a manifest.  It
does not start Harbor, OpenClaw, a model, or a manager call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from codeskill_rebuild.harbor_recovery import (  # noqa: E402
    HarborRecoveryError,
    create_harbor_recovery_manifest,
)
from codeskill_rebuild.types import sha256_file  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="original immutable C-only driver-input.json")
    parser.add_argument("--original-driver-process", type=Path, required=True)
    parser.add_argument("--import-failure", type=Path, required=True)
    parser.add_argument("--official-harbor-process", type=Path, required=True)
    parser.add_argument("--trial-dir", type=Path, required=True, help="exact Harbor trial directory containing result.json")
    parser.add_argument("--sidecar-dir", type=Path, required=True, help="exact sidecar evidence directory containing upstream_requests/")
    parser.add_argument("--recovery-root", type=Path, required=True, help="fresh child namespace for recovery artifacts")
    parser.add_argument("--manager-root", type=Path, required=True, help="trusted coordinator execution manager root")
    parser.add_argument("--output", type=Path, required=True, help="new immutable recovery manifest")
    return parser


def main() -> None:
    args = _parser().parse_args()
    manifest = create_harbor_recovery_manifest(
        input_path=args.input,
        original_driver_process_path=args.original_driver_process,
        import_failure_path=args.import_failure,
        official_harbor_process_path=args.official_harbor_process,
        trial_dir=args.trial_dir,
        sidecar_dir=args.sidecar_dir,
        recovery_root=args.recovery_root,
        manager_root=args.manager_root,
        output_path=args.output,
    )
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "manifest": str(args.output.resolve()),
                "manifest_sha256": sha256_file(args.output),
                "harbor_rerun": False,
                "sidecar_rerun": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except (HarborRecoveryError, OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise SystemExit(f"{type(error).__name__}: {error}") from error
