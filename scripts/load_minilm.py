#!/usr/bin/env python3
"""Load the required MiniLM encoder and save a reproducible capability record."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from codeskill_rebuild.retrieval import MiniLMEncoder
from codeskill_rebuild.types import write_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--contract-sha256", required=True)
    parser.add_argument("--research-decisions-sha256", required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    encoder = MiniLMEncoder()
    metadata = encoder.load()
    skill = {
        "skill_id": "probe-skill",
        "title": "Inspect the first failing diagnostic",
        "when_to_apply": "When a build command reports a concrete failure",
        "rules": ["Read the first failure before changing configuration.", "Re-run the relevant check after the change."],
    }
    vector, index = encoder.index_skill(skill)
    actual_skill_ids = encoder._model.tokenizer(index["text"], add_special_tokens=True, truncation=False)["input_ids"]
    query_vector, query = encoder.encode_query(
        "event",
        {
            "observation_errors_tests": "The build command failed with a compiler diagnostic.",
            "recent_action": "Ran the project test command.",
            "public_reasoning": "Inspect the diagnostic before changing configuration.",
            "task_context": "Repair a coding task using terminal tools.",
        },
    )
    actual_query_ids = encoder._model.tokenizer(query["text"], add_special_tokens=True, truncation=False)["input_ids"]
    if actual_skill_ids != index["token_ids"] or actual_query_ids != query["token_ids"]:
        raise RuntimeError("Recorded token IDs differ from the actual SentenceTransformer tokenizer input")
    write_json(
        args.out / "minilm-evidence.json",
        {
            "kind": "live_minilm_load_and_encode",
            "historical": False,
            "fixture": False,
            "contract": {
                "version": "v0.4",
                "reproduction_spec_sha256": args.contract_sha256,
                "research_decisions_sha256": args.research_decisions_sha256,
            },
            "model": metadata,
            "skill_index": index,
            "event_query": query,
            "tokenizer_input_verification": {"skill_ids_match": True, "query_ids_match": True},
            "skill_vector": {"dimension": len(vector), "sha256": hashlib.sha256(repr(vector).encode()).hexdigest()},
            "query_vector": {"dimension": len(query_vector), "sha256": hashlib.sha256(repr(query_vector).encode()).hexdigest()},
        },
    )


if __name__ == "__main__":
    main()
