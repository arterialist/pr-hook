#!/usr/bin/env python3
"""Tests for linear-prose-gate.py.

    python3 tests/test_linear_prose_gate.py

HOME points at an empty directory, so the optional asd-ste100 linter is absent.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
HOOK = os.path.join(HOOKS, "linear-prose-gate.py")

CLEAN = "The export job now retries a failed page once. A flaky upstream no longer fails the whole export."


class LinearGateTest(unittest.TestCase):
    def setUp(self):
        self.env = dict(os.environ, HOME=tempfile.mkdtemp(prefix="gate-test-"))

    def run_hook(self, tool, **tool_input):
        payload = {"tool_name": tool, "tool_input": tool_input}
        out = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["hookSpecificOutput"] if out.stdout.strip() else None

    def assertDenied(self, out, reason):
        self.assertIsNotNone(out)
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(reason, out["permissionDecisionReason"])

    def test_clean_prose_passes_under_any_linear_server_name(self):
        self.assertIsNone(self.run_hook("mcp__linear-server__save_comment", body=CLEAN))
        self.assertIsNone(self.run_hook("mcp__linear__save_issue", title="Export retries a failed page", description=CLEAN))
        self.assertIsNone(self.run_hook("mcp__claude_ai_Linear__save_status_update", body=CLEAN))

    def test_other_tools_and_calls_without_prose_are_ignored(self):
        self.assertIsNone(self.run_hook("mcp__github__save_comment", body="A crucial — update"))
        self.assertIsNone(self.run_hook("Bash", command="echo crucial"))
        self.assertIsNone(self.run_hook("mcp__linear-server__save_issue", id="ABC-1", state="Done"))

    def test_prose_rules_apply_to_every_text_field(self):
        tool = "mcp__linear-server__save_issue"
        self.assertDenied(self.run_hook(tool, title="A crucial fix", description=CLEAN), "AI vocabulary")
        self.assertDenied(self.run_hook(tool, description="The job retries — once."), "em dash")
        self.assertDenied(self.run_hook(tool, description="There are two retries."), "Spelled-out number")
        self.assertDenied(self.run_hook(tool, description="Happy to report the job retries."), "Chatbot")
        self.assertDenied(self.run_hook(tool, description="The fix is simple: retry the page."), "Colon")
        self.assertDenied(self.run_hook(
            tool, description="Ran 3 jobs over 40 pages in 12 s with 2 retries and 1 failure."), "Digit spam")

    def test_markdown_is_for_documents_only(self):
        text = "## Retries\n\n" + CLEAN
        self.assertDenied(self.run_hook("mcp__linear-server__save_comment", body=text), "Headings or bold")
        self.assertDenied(self.run_hook("mcp__linear-server__save_comment", body="**Done.** " + CLEAN),
                          "Headings or bold")
        self.assertIsNone(self.run_hook("mcp__linear-server__save_document", title="Retries", content=text))
        self.assertIsNone(self.run_hook("mcp__linear-server__save_project", description=text))

    def test_documents_skip_the_digit_limit(self):
        text = "Ran 3 jobs over 40 pages in 12 s with 2 retries and 1 failure."
        self.assertIsNone(self.run_hook("mcp__linear-server__save_document", content=text))

    def test_a_long_status_update_is_denied(self):
        long = " ".join(["The export job retries a failed page."] * 6)
        self.assertDenied(self.run_hook("mcp__linear-server__save_status_update", body=long),
                          "Two sentences is the target")
        self.assertIsNone(self.run_hook("mcp__linear-server__save_comment", body=long))


if __name__ == "__main__":
    unittest.main(verbosity=2)
