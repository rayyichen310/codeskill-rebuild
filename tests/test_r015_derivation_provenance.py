from __future__ import annotations

import unittest

from codeskill_rebuild.bank import SkillBank
from scripts import run_r015_c_only_harbor_driver as driver


class R015DerivationProvenanceTest(unittest.TestCase):
    def _trace(self) -> dict[str, object]:
        return {
            "source": {
                "instance_id": "task",
                "canonical_instance_id": "task",
                "task_name": "terminal-bench/task",
            },
            "instruction": "Repair the service.",
            "text_manager_eligible": True,
            "steps": [
                {"source_entry_id": "action", "role": "assistant", "content": []},
                {"source_entry_id": "result", "role": "toolResult", "content": []},
            ],
            "outcome": {"reward": 1},
        }

    def test_publication_rejects_unknown_source_as_exclude_before_fig9(self) -> None:
        with self.assertRaisesRegex(driver.COnlyHarborDriverError, "no request-bound derivation provenance"):
            driver._extraction_publication(
                context={
                    "trial_id": "r1:C:task",
                    "task_id": "task",
                    "driver_config": {"historical_thinking_policy": "exclude"},
                },
                executor=object(),
                frozen_bank=SkillBank.empty("terminal-bench"),
                extraction={"candidates": [{"candidate_id": "legacy", "skill": {}}]},
                supplied_attempts=[],
                trace=self._trace(),
            )


if __name__ == "__main__":
    unittest.main()
