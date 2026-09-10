"""Derive R012 evolution candidates from durable proxy evidence only.

This offline runner is the bridge from an isolated M3 trial's request records
to a later manager evolution/maintenance step.  It never invokes a model or
changes a bank.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from codeskill_rebuild.r012_runtime import R012Runtime
from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.trial_schedule import InstanceBankFreeze
from codeskill_rebuild.types import read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--attempt-record", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = [read_json(path) for path in args.attempt_record]
    # The no-op single-arm coordinator gives this evidence audit the same
    # runtime entrypoint as the full lifecycle without inventing an evaluation
    # profile or applying an update.
    runtime = R012Runtime(InstanceBankFreeze({"audit": SkillBank.empty("audit")}, repeat_ids=("evidence",)))
    supplied = runtime.supplied_evolution_candidates(trial_id=args.trial_id, proxy_attempt_records=records)
    write_json(
        args.output,
        {
            "kind": "r012_supplied_only_evolution_candidates",
            "trial_id": args.trial_id,
            "attempt_records": [str(path) for path in args.attempt_record],
            "candidates": supplied,
            "maintenance_required_before_adoption": True,
        },
    )


if __name__ == "__main__":
    main()
