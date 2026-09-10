from __future__ import annotations

import unittest

from codeskill_rebuild.retrieval import description_index_record, rank, skill_index_text, token_budgeted_text


class CharacterTokenizer:
    def num_special_tokens_to_add(self, pair: bool = False) -> int:
        return 2

    def encode(self, value: str, add_special_tokens: bool = True, truncation: bool = False) -> list[int]:
        tokens = [ord(char) for char in value]
        return ([-1] + tokens + [-2]) if add_special_tokens else tokens

    def decode(self, values: list[int], skip_special_tokens: bool = True, clean_up_tokenization_spaces: bool = False) -> str:
        return "".join(chr(value) for value in values if value not in {-1, -2})


class RetrievalTest(unittest.TestCase):
    def test_rank_respects_threshold_and_title_condition_rules(self) -> None:
        skills = [
            {"skill_id": "b", "title": "Build diagnostic", "when_to_apply": "Build command fails", "rules": ["Read the error"]},
            {"skill_id": "a", "title": "Network", "when_to_apply": "Network fails", "rules": ["Retry"]},
        ]
        ranked = rank([1.0, 0.0], skills, [[1.0, 0.0], [0.0, 1.0]], threshold=0.5, limit=2)
        self.assertEqual([item["skill"]["skill_id"] for item in ranked], ["b"])
        text = skill_index_text(skills[0])
        self.assertIn("Title:", text)
        self.assertIn("When to apply:", text)
        self.assertIn("Rules:", text)

    def test_actual_token_allocation_keeps_late_rules_field(self) -> None:
        tokenizer = CharacterTokenizer()
        record = token_budgeted_text(
            tokenizer,
            [("Title:", "T" * 100, 0.15), ("When:", "W" * 100, 0.35), ("Rules:", "R" * 100, 0.50)],
            max_seq_length=80,
        )
        self.assertLessEqual(record["token_count"], 80)
        self.assertTrue(all(field["present"] for field in record["fields"]))
        self.assertGreater(record["fields"][2]["retained_token_count"], 0)

    def test_short_title_releases_unused_budget_to_later_fields(self) -> None:
        tokenizer = CharacterTokenizer()
        record = token_budgeted_text(
            tokenizer,
            [("Title:", "T", 0.15), ("When:", "W" * 100, 0.35), ("Rules:", "R" * 100, 0.50)],
            max_seq_length=80,
        )
        retained = [field["retained_token_count"] for field in record["fields"]]
        self.assertEqual(retained[0], 1)
        self.assertGreater(retained[2], retained[1])
        self.assertLessEqual(record["token_count"], 80)

    def test_description_index_retains_all_d01_fields(self) -> None:
        record = description_index_record(
            CharacterTokenizer(),
            {
                "task_family": "T" * 100,
                "observed_obstacle": "O" * 100,
                "attempted_procedure": "P" * 100,
                "observed_outcome": "R" * 100,
            },
            max_seq_length=100,
        )
        self.assertLessEqual(record["token_count"], 100)
        self.assertTrue(all(field["present"] for field in record["fields"]))
