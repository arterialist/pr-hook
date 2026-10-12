#!/usr/bin/env python3
"""Tests for pr-body-gate-agy.py, run with a fake `agy` on PATH.

    python3 tests/test_pr_body_gate_agy.py

The fake agy serves canned bodies from FAKE_AGY_BODIES (a file, bodies split
by a line of ===) in call order, counts calls in FAKE_AGY_COUNT, and saves
the last prompt to FAKE_AGY_PROMPT.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
HOOK = os.path.join(HOOKS, "pr-body-gate-agy.py")

FAKE_AGY = r'''#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
prompt = args[args.index("-p") + 1]
prompt = "MODEL=" + args[args.index("--model") + 1] + "\n" + prompt
with open(os.environ["FAKE_AGY_PROMPT"], "w") as fh:
    fh.write(prompt)
n = 0
if os.path.exists(os.environ["FAKE_AGY_COUNT"]):
    n = int(open(os.environ["FAKE_AGY_COUNT"]).read() or 0)
with open(os.environ["FAKE_AGY_COUNT"], "w") as fh:
    fh.write(str(n + 1))
bodies = open(os.environ["FAKE_AGY_BODIES"]).read().split("\n===\n")
sys.stdout.write(bodies[min(n, len(bodies) - 1)])
'''

GOOD = """Closes #1234

The gate now hands the diff to a separate model and validates what comes back.

The caller passes rough notes and gets a body in the house format.

## Measured impact
Nothing was measured on this branch; the change is a hook, not a hot path.

## Business impact
PR bodies read the same regardless of which model did the work. Reviewers get the same shape every time.
"""

BAD_SPELLED = GOOD.replace("Closes #1234", "Closes #1234\n\nThere are two paths.")


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gate-test-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        agy = os.path.join(self.bin, "agy")
        with open(agy, "w") as fh:
            fh.write(FAKE_AGY)
        os.chmod(agy, 0o755)
        self.bodies = os.path.join(self.tmp, "bodies.txt")
        self.count = os.path.join(self.tmp, "count.txt")
        self.prompt = os.path.join(self.tmp, "prompt.txt")
        self.env = dict(os.environ,
                        PATH=self.bin + os.pathsep + os.environ.get("PATH", ""),
                        FAKE_AGY_BODIES=self.bodies,
                        FAKE_AGY_COUNT=self.count,
                        FAKE_AGY_PROMPT=self.prompt,
                        TMPDIR=self.tmp)
        self.env.pop("PR_BODY_GATE_MODEL", None)
        self.env.pop("PR_BODY_GATE_BASE", None)
        self.repo = os.path.join(self.tmp, "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        g = ["git", "-C", self.repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "-c", "commit.gpgsign=false"]
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("one\n")
        subprocess.run(g + ["add", "a.txt"], check=True)
        subprocess.run(g + ["commit", "-q", "-m", "base"], check=True)
        subprocess.run(g + ["checkout", "-q", "-b", "feature"], check=True)
        with open(os.path.join(self.repo, "a.txt"), "w") as fh:
            fh.write("one\ntwo\n")
        subprocess.run(g + ["commit", "-q", "-am", "feat: add the second line"], check=True)

    def serve(self, *bodies):
        with open(self.bodies, "w") as fh:
            fh.write("\n===\n".join(bodies))

    def calls(self):
        if not os.path.exists(self.count):
            return 0
        with open(self.count) as fh:
            return int(fh.read())

    def last_prompt(self):
        with open(self.prompt) as fh:
            return fh.read()

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def run_hook(self, command, tool_name="Bash"):
        tool_input = {"command": command, "description": "file the PR", "timeout": 120000}
        payload = {"tool_name": tool_name, "tool_input": tool_input, "cwd": self.repo}
        out = subprocess.run([sys.executable, HOOK], input=json.dumps(payload),
                             capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["hookSpecificOutput"] if out.stdout.strip() else None

    def test_ignores_other_commands(self):
        self.assertIsNone(self.run_hook("git status"))
        self.assertIsNone(self.run_hook("gh pr create --body x", tool_name="Read"))
        self.assertEqual(self.calls(), 0)

    def test_fill_is_denied_without_calling_agy(self):
        out = self.run_hook("gh pr create --fill")
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("--fill", out["permissionDecisionReason"])
        self.assertEqual(self.calls(), 0)

    def test_edit_with_a_clean_title_and_no_body_passes_through(self):
        self.assertIsNone(self.run_hook('gh pr edit 12 --title "fix(portal): slots read in one call (ABC-629)"'))
        self.assertIsNone(self.run_hook("gh pr edit 12 --add-label perf"))
        self.assertEqual(self.calls(), 0)

    def test_edit_with_a_sloppy_title_is_denied_without_calling_agy(self):
        out = self.run_hook('gh pr edit 12 --title "x"')
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("type(scope): summary", out["permissionDecisionReason"])
        self.assertEqual(self.calls(), 0)

    def test_create_without_a_title_is_denied(self):
        out = self.run_hook('gh pr create --base main --body "Measured: 3 tests pass"')
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("--title", out["permissionDecisionReason"])
        self.assertEqual(self.calls(), 0)

    def test_create_with_a_sloppy_title_is_denied_before_agy(self):
        self.serve(GOOD)
        out = self.run_hook('gh pr create --title "Crucial fix" --body "Measured: 3 tests pass"')
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("title", out["permissionDecisionReason"].lower())
        self.assertEqual(self.calls(), 0)

    def test_heredoc_body_becomes_generated_body_file(self):
        self.serve(GOOD)
        cmd = ('gh pr create --title "feat: second line" --body "$(cat <<\'EOF\'\n'
               'Closes #1234\nMeasured: none, hook only\nEOF\n)"')
        out = self.run_hook(cmd)
        self.assertEqual(out["permissionDecision"], "allow", out)
        new_cmd = out["updatedInput"]["command"]
        self.assertNotIn("cat <<", new_cmd)
        self.assertIn("--title \"feat: second line\"", new_cmd)
        self.assertIn("--body-file ", new_cmd)
        path = new_cmd.split("--body-file ", 1)[1].split()[0]
        self.assertEqual(self.read(path), GOOD)
        self.assertEqual(os.path.basename(path),
                         hashlib.sha256(GOOD.encode()).hexdigest()[:16] + ".md")
        # the untouched fields come back with the command
        self.assertEqual(out["updatedInput"]["description"], "file the PR")
        self.assertEqual(out["updatedInput"]["timeout"], 120000)
        self.assertIn("gemini-3.8-flash-medium", out["permissionDecisionReason"])
        prompt = self.last_prompt()
        self.assertIn("Closes #1234\nMeasured: none, hook only", prompt)
        self.assertIn("- none, hook only", prompt)  # the Measurements block
        self.assertIn("feat: add the second line", prompt)  # commit on the branch
        self.assertIn("+two", prompt)  # the diff against the fallback base, main
        self.assertIn("feat: second line", prompt)  # the title
        self.assertEqual(self.calls(), 1)

    def test_base_comes_from_the_flag_then_the_environment(self):
        self.serve(GOOD)
        g = ["git", "-C", self.repo]
        subprocess.run(g + ["branch", "-q", "release", "feature"], check=True)
        cmd = 'gh pr create --title "feat: second line" --body "Measured: none, x"'
        self.run_hook(cmd + " --base release")
        self.assertIn("(base: release)", self.last_prompt())
        self.assertNotIn("+two", self.last_prompt())
        self.env["PR_BODY_GATE_BASE"] = "release"
        self.run_hook(cmd)
        self.assertIn("(base: release)", self.last_prompt())
        self.run_hook(cmd + " --base main")
        self.assertIn("(base: main)", self.last_prompt())
        self.assertIn("+two", self.last_prompt())

    def test_rejected_draft_is_retried_with_the_reason(self):
        self.serve(BAD_SPELLED, GOOD)
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: 3 tests pass"')
        self.assertEqual(out["permissionDecision"], "allow", out)
        self.assertEqual(self.calls(), 2)
        self.assertIn("2 attempt(s)", out["permissionDecisionReason"])
        prompt = self.last_prompt()
        self.assertIn("rejected by the linter", prompt)
        self.assertIn("Spelled-out number: 'two'", prompt)
        self.assertIn("There are two paths.", prompt)  # previous draft shown

    def test_gives_up_after_three_bad_drafts(self):
        self.serve(BAD_SPELLED)
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: 3 tests pass"')
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertEqual(self.calls(), 3)
        self.assertIn("3 attempts", out["permissionDecisionReason"])
        self.assertIn("Spelled-out number", out["permissionDecisionReason"])

    def test_generated_body_file_passes_without_a_call(self):
        self.serve(GOOD)
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: 3 tests pass"')
        new_cmd = out["updatedInput"]["command"]
        self.assertIsNone(self.run_hook(new_cmd))
        self.assertEqual(self.calls(), 1)

    def test_edited_generated_body_file_is_refused_as_notes(self):
        self.serve(GOOD)
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: 3 tests pass"')
        path = out["updatedInput"]["command"].split("--body-file ", 1)[1].split()[0]
        with open(path, "a") as fh:
            fh.write("\nhand edit\n")
        out = self.run_hook(f"gh pr create --title 'feat: second line' --body-file {path}")
        # an edited file is notes again, and a generated body has no Measured line
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("`Measured:` line", out["permissionDecisionReason"])
        self.assertEqual(self.calls(), 1)

    def test_create_without_body_is_denied_for_missing_measurement(self):
        self.serve(GOOD)
        out = self.run_hook("gh pr create --title 'feat: second line' --base main")
        self.assertEqual(out["permissionDecision"], "deny", out)
        self.assertIn("`Measured:` line", out["permissionDecisionReason"])
        self.assertIn("Measured: none, <why", out["permissionDecisionReason"])
        self.assertEqual(self.calls(), 0)

    def test_measured_line_without_a_number_is_not_enough(self):
        self.serve(GOOD)
        out = self.run_hook('gh pr create --title "feat: second line" --body "Closes #1\nMeasured: faster"')
        self.assertEqual(out["permissionDecision"], "deny", out)
        self.assertEqual(self.calls(), 0)

    def test_measured_lines_reach_the_prompt_as_their_own_block(self):
        self.serve(GOOD)
        notes = "Closes #1\nmeasured: extraction 101 s -> 29 s (3 runs)\nMeasured: none, portal build not run here\nWhy: x"
        out = self.run_hook(f'gh pr create --title "feat: second line" --body "{notes}"')
        self.assertEqual(out["permissionDecision"], "allow", out)
        prompt = self.last_prompt()
        block = prompt.split("Measurements (the only source", 1)[1].split("Author notes", 1)[0]
        self.assertIn("- extraction 101 s -> 29 s (3 runs)", block)
        self.assertIn("- none, portal build not run here", block)
        self.assertNotIn("Why: x", block)

    def test_model_comes_from_the_environment(self):
        self.serve(GOOD)
        self.env["PR_BODY_GATE_MODEL"] = "gemini-3.8-flash-high"
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: none, x"')
        self.assertIn("gemini-3.8-flash-high", out["permissionDecisionReason"])
        self.assertIn("MODEL=gemini-3.8-flash-high", self.last_prompt())

    def test_fenced_output_is_unwrapped(self):
        self.serve("```markdown\n" + GOOD.rstrip("\n") + "\n```")
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: 3 tests pass"')
        path = out["updatedInput"]["command"].split("--body-file ", 1)[1].split()[0]
        self.assertEqual(self.read(path), GOOD)

    def test_single_quoted_and_equals_forms(self):
        self.serve(GOOD)
        out = self.run_hook("gh pr create --title 'feat: second line' --body='some notes\nMeasured: none, docs' --draft")
        self.assertEqual(out["permissionDecision"], "allow", out)
        self.assertIn("some notes", self.last_prompt())
        self.assertRegex(out["updatedInput"]["command"], r"--body-file \S+ --draft$")

    def test_prompt_ranks_measurements_by_class(self):
        self.serve(GOOD)
        out = self.run_hook('gh pr create --title "feat: second line" --body "Measured: 3 tests pass"')
        self.assertEqual(out["permissionDecision"], "allow", out)
        prompt = self.last_prompt().lower()
        order = [prompt.index(k) for k in ("unit economics", "performance", "developer experience", "everything else")]
        self.assertEqual(order, sorted(order), order)
        self.assertIn("mermaid", prompt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
