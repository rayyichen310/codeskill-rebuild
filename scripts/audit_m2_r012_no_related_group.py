"""Write the R012 raw-description packet for one no-related-group decision.

It is deliberately read-only with respect to descriptions, pairing rules, and
skill banks.  The resulting packet supports human review only.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from codeskill_rebuild.pairing_audit import no_related_group_audit, write_no_related_group_audit
from codeskill_rebuild.types import read_json, sha256_file


def _reference(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": sha256_file(path)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--anchor-trace", type=Path, required=True)
    parser.add_argument("--anchor-description", type=Path, required=True)
    parser.add_argument("--pairing-result", type=Path, required=True)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = read_json(args.candidate_manifest)
    candidates: list[dict[str, Any]] = []
    values = manifest.get("candidates") if isinstance(manifest, dict) else None
    if not isinstance(values, list):
        raise ValueError("candidate manifest needs a candidates list")
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("trace_path"), str) or not isinstance(item.get("description_path"), str):
            raise ValueError("candidate manifest entries need trace_path and description_path")
        trace_path = Path(item["trace_path"])
        description_path = Path(item["description_path"])
        candidates.append(
            {
                "trace": read_json(trace_path),
                "description": read_json(description_path),
                "trace_ref": _reference(trace_path),
                "description_ref": _reference(description_path),
            }
        )
    audit = no_related_group_audit(
        anchor_trace=read_json(args.anchor_trace),
        anchor_description=read_json(args.anchor_description),
        candidates=candidates,
        pairing_result=read_json(args.pairing_result),
        anchor_trace_ref=_reference(args.anchor_trace),
        anchor_description_ref=_reference(args.anchor_description),
    )
    write_no_related_group_audit(args.output, audit)


if __name__ == "__main__":
    main()
