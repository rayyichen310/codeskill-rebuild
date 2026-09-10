"""Create the pre-instance R012 arm/repeat bank-freeze manifest.

This planning entrypoint does not launch OpenClaw or apply maintenance.  It
serializes exactly which independent bank snapshot every trial must receive.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from codeskill_rebuild.bank import SkillBank
from codeskill_rebuild.r012_runtime import R012Runtime
from codeskill_rebuild.trial_schedule import InstanceBankFreeze
from codeskill_rebuild.types import write_json


def _arm_bank(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--arm-bank needs ARM=PATH")
    arm, raw_path = value.split("=", 1)
    if not arm or not raw_path:
        raise argparse.ArgumentTypeError("--arm-bank needs a nonempty ARM and PATH")
    return arm, Path(raw_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repeat", action="append", required=True)
    parser.add_argument("--arm-bank", type=_arm_bank, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    banks: dict[str, SkillBank] = {}
    for arm, path in args.arm_bank:
        if arm in banks:
            raise ValueError(f"duplicate arm {arm}")
        banks[arm] = SkillBank.load(path)
    runtime = R012Runtime(InstanceBankFreeze(banks, repeat_ids=tuple(args.repeat)))
    assignments = runtime.freeze_instance(args.instance_id)
    write_json(
        args.output,
        {
            "kind": "r012_pre_instance_bank_freeze",
            "instance_id": args.instance_id,
            "assignments": assignments,
            "update_gate": "every assignment must finish before an explicit ordered release callback may mutate any arm bank",
        },
    )


if __name__ == "__main__":
    main()
