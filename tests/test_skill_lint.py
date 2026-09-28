from __future__ import annotations

import unittest

from codeskill_rebuild.skill_lint import lint_skill, repo_terms_for


def skill(when: str, *rules: str) -> dict[str, object]:
    return {"title": "t", "when_to_apply": when, "rules": list(rules)}


class SkillLintTest(unittest.TestCase):
    def test_flags_task_specific_identifiers(self) -> None:
        findings = lint_skill(
            skill(
                "When editing django/forms/models.py in Django",
                "Call `self._find_signature()` before `visit_Tuple`",
                'Restore commit 3f2a9c1b and set the value to "a very specific literal string"',
            ),
            repo_terms_for(["django__django-11790"]),
        )
        kinds = {(f["kind"], f["text"]) for f in findings}
        self.assertIn(("path", "django/forms/models.py"), kinds)
        self.assertIn(("repo_name", "django"), kinds)
        self.assertIn(("code_symbol", "self._find_signature()"), kinds)
        self.assertIn(("code_symbol", "visit_Tuple"), kinds)
        self.assertIn(("commit_hash", "3f2a9c1b"), kinds)
        self.assertIn(("long_literal", "a very specific literal string"), kinds)

    def test_flags_bare_code_names_outside_backticks(self) -> None:
        findings = lint_skill(skill(
            "When str(form['name']) lacks an attribute set after super().__init__()",
            "Update widget_attrs() whenever max_length changes",
        ))
        flagged = {f["text"] for f in findings if f["kind"] == "code_symbol"}
        self.assertEqual(flagged, {"__init__", "widget_attrs(", "max_length"})

    def test_standard_commands_tools_and_system_interfaces_pass(self) -> None:
        clean = skill(
            "When `xxd` is not found or the command exits with code 127",
            "Use `od -c file | head` instead of `xxd`",
            "Read /proc/net/tcp when `ss` is missing and send noise to /dev/null",
            "Set `LD_LIBRARY_PATH` and drop `-lX11`",
            'Run `git config user.email "you@example.com" && git config user.name "You"` first',
            "Compare repr() with str() and check the attribute(s) that PATH_MAX allows",
        )
        self.assertEqual(lint_skill(clean), [])


if __name__ == "__main__":
    unittest.main()
