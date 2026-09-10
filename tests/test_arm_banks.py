from __future__ import annotations

import unittest

from codeskill_rebuild.arm_banks import direct_candidate_bank, group_exact_candidates, same_granularity_top5
from codeskill_rebuild.bank import SkillBank


def skill(title: str = "Inspect diagnostics", rules: list[str] | None = None) -> dict:
    return {
        "title": title,
        "granularity": "event",
        "when_to_apply": "When an observed command result needs diagnosis",
        "rules": rules or ["Inspect the observed result before changing configuration."],
        "benchmark": "terminal-bench",
    }


class FakeEncoder:
    def index_skill(self, value: dict) -> tuple[list[float], dict]:
        vectors = {
            "Candidate": [1.0, 0.0],
            "Same-source": [0.9, 0.1],
            "Other-source": [0.1, 0.9],
        }
        return vectors.get(value["title"], [0.0, 1.0]), {"title": value["title"]}


class ArmBankTest(unittest.TestCase):
    def test_direct_bank_exact_dedup_unions_canonical_and_raw_provenance(self) -> None:
        records = [
            {
                "skill": skill(),
                "source_instance_ids": ["terminal-bench/source-a"],
                "source_instance_ids_raw": ["terminal-bench/source-a"],
                "candidate_record": {"path": "one.json"},
            },
            {
                "skill": skill(),
                "source_instance_ids": ["source-b"],
                "source_instance_ids_raw": ["source-b"],
                "candidate_record": {"path": "two.json"},
            },
            {
                "skill": skill(rules=["Use a different observed procedure."]),
                "source_instance_ids": ["source-c"],
                "source_instance_ids_raw": ["source-c"],
                "candidate_record": {"path": "three.json"},
            },
        ]
        grouped = group_exact_candidates(records)
        bank = direct_candidate_bank(benchmark="terminal-bench", grouped_candidates=grouped)
        self.assertEqual(len(grouped), 2)
        self.assertEqual(len(bank.eligible(instance_id="unseen", granularity="event")), 2)
        merged = next(item for item in bank.skills if item["title"] == "Inspect diagnostics")
        self.assertEqual(merged["provenance"]["source_instance_ids"], ["source-a", "source-b"])
        self.assertEqual(merged["provenance"]["source_instance_ids_raw"], ["source-a", "source-b", "terminal-bench/source-a"])
        self.assertEqual(len(bank.operations[0]["evidence"]["candidate_records"]), 2)

    def test_maintenance_retrieval_keeps_same_source_candidates(self) -> None:
        bank = SkillBank.empty("terminal-bench")
        bank.apply(
            operation_id="same",
            decision="add",
            candidate=skill("Same-source"),
            source_instance_ids=["source-a"],
            evidence={},
        )
        bank.apply(
            operation_id="other",
            decision="add",
            candidate=skill("Other-source"),
            source_instance_ids=["source-b"],
            evidence={},
        )
        retrieved, record = same_granularity_top5(bank, skill("Candidate"), FakeEncoder())
        self.assertEqual([item["title"] for item in retrieved], ["Same-source", "Other-source"])
        self.assertEqual(record["source_provenance_filter"], "none")


if __name__ == "__main__":
    unittest.main()
