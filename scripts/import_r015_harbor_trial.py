#!/usr/bin/env python3
"""Package one completed official Harbor/OpenClaw trial for lifecycle finish.

This command never launches Harbor, OpenClaw, a verifier, or a manager.  It
reads an already completed isolated trial plus its same-trial sidecar evidence
and writes an immutable finish packet.  Use its emitted paths with the
existing ``run_m3_r012_lifecycle.py finish`` command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from codeskill_rebuild.r015_harbor_evidence import import_harbor_openclaw_trial


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--harbor-trial-dir", type=Path, required=True)
    parser.add_argument("--sidecar-evidence-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--session-source-path",
        type=Path,
        help="Optional immutable OpenClaw SQLite source when Harbor did not copy session JSONL into the trial logs.",
    )
    args = parser.parse_args()
    manifest = import_harbor_openclaw_trial(
        trial_id=args.trial_id,
        instance_id=args.instance_id,
        harbor_trial_dir=args.harbor_trial_dir,
        sidecar_evidence_dir=args.sidecar_evidence_dir,
        output_dir=args.output_dir,
        session_source_path=args.session_source_path,
    )
    print(
        json.dumps(
            {
                "status": "imported",
                "trial_id": manifest["trial_id"],
                "official_reward": manifest["official_reward"],
                "output_dir": str(args.output_dir),
            }
        )
    )


if __name__ == "__main__":
    main()
