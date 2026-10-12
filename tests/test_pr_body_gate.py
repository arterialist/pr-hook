#!/usr/bin/env python3
"""Tests for the title and body rules, and for pr-body-gate.py, the lint-only gate.

    python3 tests/test_pr_body_gate.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
HOOK = os.path.join(HOOKS, "pr-body-gate.py")
sys.path.insert(0, HOOKS)

import _pr_rules as rules  # noqa: E402

GOOD = """Closes #1234

The gate reads the body out of the command and checks it against the rules.

A body that breaks a rule is refused with the reason, and a clean one passes untouched.

## Measured impact
Nothing was measured on this branch; the change is a hook, not a hot path.

## Business impact
PR bodies read the same regardless of who wrote them. Reviewers get the same shape every time.
"""

BAD_SPELLED = GOOD.replace("Closes #1234", "Closes #1234\n\nThere are two paths.")
TITLE = '--title "feat: second line"'


class RulesTest(unittest.TestCase):
    def test_title_rules(self):
        ok = [
            "fix(alerts): SLA pages fire on outages, not on retried checks (ABC-645)",
            "perf: the daily board waits on one fewer database round trip",
            "chore(release): latency fixes go to production",
        ]
        for title in ok:
            self.assertIsNone(rules.validate_title(title), title)
        bad = {
            "x": "type(scope): summary",
            "Fix the thing": "type(scope): summary",
            "fix(api): make the board faster.": "period",
            "fix(api): " + "word " * 20: "80",
            "fix(api): a crucial speedup for the board": "AI vocabulary",
            "fix(api): two reads instead of four": "Spelled-out number",
            "fix(api): board reads — one call": "em dash",
            "fix(api): not just faster but cheaper": "not just",
        }
        for title, reason in bad.items():
            problem = rules.validate_title(title)
            self.assertIsNotNone(problem, title)
            self.assertIn(reason, problem, title)

    def test_body_rules(self):
        self.assertIsNone(rules.validate(GOOD))
        self.assertIn("Spelled-out", rules.validate(BAD_SPELLED))
        self.assertIn("decrease", rules.validate(GOOD.replace(
            "Nothing was measured on this branch; the change is a hook, not a hot path.",
            "Latency fell from 101 s to 29 s.")))
        self.assertIn("headings besides", rules.validate("## Notes\n" + GOOD))
        self.assertIn("missing", rules.validate("just text"))

    def test_body_without_prose_before_the_headings_is_rejected(self):
        tail = GOOD.split("## Measured impact", 1)[1]
        self.assertIn("prose", rules.validate("## Measured impact" + tail))
        self.assertIn("prose", rules.validate("Closes #1234\n\n## Measured impact" + tail))
        self.assertIn("prose", rules.validate("Closes #1234\n\nOne line.\n\n## Measured impact" + tail))

    def test_first_section_is_capped_at_100_words_and_a_diagram_is_free(self):
        head, tail = GOOD.split("## Measured impact", 1)
        padded = head + ("word " * 80) + "\n\n## Measured impact" + tail
        self.assertIn("100", rules.validate(padded))
        diagram = "```mermaid\nflowchart LR\n  " + " --> ".join(f"n{i}" for i in range(80)) + "\n```\n\n"
        self.assertIsNone(rules.validate(head + diagram + "## Measured impact" + tail))


class LintGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate-test-")

    def run_hook(self, command, tool_name="Bash"):
        payload = {"tool_name": tool_name, "tool_input": {"command": command}, "cwd": self.tmp}
        out = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["hookSpecificOutput"] if out.stdout.strip() else None

    def body_file(self, body):
        path = os.path.join(self.tmp, "body.md")
        with open(path, "w") as fh:
            fh.write(body)
        return path

    def assertDenied(self, out, reason):
        self.assertIsNotNone(out)
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(reason, out["permissionDecisionReason"])

    def test_ignores_other_commands(self):
        self.assertIsNone(self.run_hook("git status"))
        self.assertIsNone(self.run_hook("gh pr create --body x", tool_name="Read"))
        self.assertIsNone(self.run_hook("gh pr create --help"))

    def test_a_clean_body_passes_in_every_shape_the_hook_reads(self):
        self.assertIsNone(self.run_hook(f"gh pr create {TITLE} --body-file {self.body_file(GOOD)}"))
        self.assertIsNone(self.run_hook(f"gh pr create {TITLE} --body \"$(cat <<'EOF'\n{GOOD}EOF\n)\""))
        self.assertIsNone(self.run_hook(f"gh pr create {TITLE} --body '{GOOD}' --draft"))
        self.assertIsNone(self.run_hook(f"gh pr edit 12 --body-file {self.body_file(GOOD)}"))

    def test_a_body_that_breaks_a_rule_is_denied_with_the_rule_and_the_format(self):
        out = self.run_hook(f"gh pr create {TITLE} --body-file {self.body_file(BAD_SPELLED)}")
        self.assertDenied(out, "Spelled-out number: 'two'")
        self.assertIn("## Measured impact", out["permissionDecisionReason"])
        self.assertDenied(self.run_hook(f'gh pr edit 12 --body "just text"'), "missing")

    def test_create_needs_a_title_and_a_body(self):
        self.assertDenied(self.run_hook(f"gh pr create --body-file {self.body_file(GOOD)}"), "--title")
        self.assertDenied(self.run_hook(f"gh pr create {TITLE} --base main"), "needs a body")

    def test_a_sloppy_title_is_denied_on_create_and_edit(self):
        body = self.body_file(GOOD)
        self.assertDenied(self.run_hook(f'gh pr create --title "Crucial fix" --body-file {body}'),
                          "type(scope): summary")
        self.assertDenied(self.run_hook('gh pr edit 12 --title "x"'), "type(scope): summary")

    def test_edit_without_a_body_passes(self):
        self.assertIsNone(self.run_hook('gh pr edit 12 --title "fix(portal): slots read in one call (ABC-629)"'))
        self.assertIsNone(self.run_hook("gh pr edit 12 --add-label perf"))

    def test_commands_that_hide_the_body_are_denied(self):
        self.assertDenied(self.run_hook("gh pr create --fill"), "--fill")
        self.assertDenied(self.run_hook(f"gh pr create {TITLE} --body-file -"), "stdin")
        self.assertDenied(self.run_hook(f"gh pr create {TITLE} --body $BODY"), "--body-file <path>")
        self.assertDenied(self.run_hook(f"gh pr create {TITLE} --body-file {self.tmp}/absent.md"),
                          "Cannot read")


if __name__ == "__main__":
    unittest.main(verbosity=2)
