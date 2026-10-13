#!/usr/bin/env python3
"""PreToolUse hook for `gh pr create` / `gh pr edit`: a second model writes the body.

Gemini 3.8 Flash (medium), run through the `agy` CLI, writes the PR body. The
model doing the work does not. What the caller passes as --body / --body-file
is read as author notes (issue numbers, what was measured, intent). The hook:

  1. Collects the commits and the diff against the base branch (--base, the
     open PR's base for `gh pr edit`, else PR_BODY_GATE_BASE, else the
     repository's default branch).
  2. Asks agy for a body in the format of _pr_rules.py and validates it. A
     rejected draft is sent back with the reason, up to MAX_ATTEMPTS.
  3. Writes the body to GENERATED_DIR/<sha>.md and rewrites the command to
     `--body-file <that path>` via updatedInput.

agy keeps every print call as a conversation and has no command that deletes
one. A conversation removed by hand from ~/.gemini/antigravity-cli comes back
in the list on the next agy start, restored from jetbox_summaries_proto.pb.
So the gate runs agy on a data directory of its own, AGY_HOME/AGY_STORE
(the hidden --app_data_dir flag), and deletes each call's conversation files
there when agy returns. The user's conversation list never sees a gate call.
settings.json in that directory is a link to the user's, which keeps the
auth method agy uses everywhere else. Without it agy ignores an API key set
there, falls back to the keyring login and makes extra network calls on
every start.

A --body-file inside GENERATED_DIR whose content hash matches its name is one
the hook wrote: it passes unchanged, with no agy call. An edited one is read
as notes again. If the command cannot be rewritten in place, the hook denies
and names the file to pass as --body-file.

On top of the rules in _pr_rules.py, the prompt asks for two things the
linter cannot check:

  - Measured impact ordered by MEASURE_CLASSES: unit economics first, then
    performance, then developer experience, then everything else.
  - a mermaid diagram when the change has a shape a reader would otherwise
    reconstruct from the diff (a request path, a state machine, a user
    flow). Not wanted for a one-file fix.

The notes must carry at least one `Measured:` line (see NOTES_RULES); the
hook refuses before calling agy otherwise, so the Measured impact section can
only ever be built from figures the caller took. Set PR_BODY_GATE_MODEL to
run another agy model (an `agy models` id). eval/compare_models.py measures
how many unsupported claims a model leaves per body and how long it takes.

Refuses --fill / -f outright.

pr-body-gate.py is the other version: it lints the body the caller wrote and
calls no model. Install one of the two, not both.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pr_rules import (  # noqa: E402
    FILL, MAX_IMPACT_WORDS, MAX_WORDS, MIN_WORDS, PR_EDIT, RULES, deny,
    extract_body, flag_value, gate_title, read_command, rewrite_command, validate,
)

MEASURE_CLASSES = (
    "unit economics (cost per user, per request, per task or per run; tokens,"
    " model calls, API calls, machine minutes, money)",
    "performance (latency, throughput, memory)",
    "developer experience (test wall time, fewer steps to a result, less code"
    " to touch, fewer flakes)",
    "everything else (test counts, coverage, correctness checks)",
)
MAX_ATTEMPTS = 3
MODEL = os.environ.get("PR_BODY_GATE_MODEL", "gemini-3.8-flash-medium")
AGY_TIMEOUT_S = 150  # medium calls have run 72 s; high up to 80 s
AGY_HOME = os.path.expanduser("~/.gemini")
AGY_STORE = "pr-body-gate"  # --app_data_dir: a directory name under AGY_HOME, never a path
AGY_USER_SETTINGS = os.path.join(AGY_HOME, "antigravity-cli", "settings.json")
CONVERSATION_ID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")
DIFF_BUDGET = 60_000  # chars of diff handed to the model
MAX_LISTED_FILES = 200  # above this, the stat is per directory and omitted files are a count
GENERATED_DIR = os.path.join(tempfile.gettempdir(), "pr-body-gate")
FALLBACK_BASE = "main"

NOTES_RULES = (
    "The gate needs at least one `Measured:` line in the notes you pass as\n"
    "--body before it asks agy for a body. Each line is a measurement you took\n"
    "on this branch, with a number and how you got it, or\n"
    "`Measured: none, <why nothing could be measured>`. Measure what ranks\n"
    "highest first and stop when a class has nothing: unit economics (cost per\n"
    "user, request, task or run; tokens, model calls, machine minutes), then\n"
    "performance, then developer experience, then everything else. A test\n"
    "count is the floor, not the goal. Everything else in the notes is\n"
    "optional. Example:\n"
    "  Closes #123\n"
    "  Measured: 1 model call per task instead of 3 (log lines from pytest tests/x -k y)\n"
    "  Measured: extraction 101 s -> 29 s (pytest tests/x -k y, 3 runs)\n"
    "  Measured: tests/api/test_x.py 14 passed, 3 new\n"
    "  Measured: none, docs only\n"
    "  Why: <what the diff does not say; what was rejected and why>"
)

MEASURED_LINE = re.compile(r"^\s*measured\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

PROMPT = """You write the body of a GitHub pull request. Return ONLY the body as plain
markdown. No code fence around it, no preamble, no sign-off, no title line.
Do not run any tools; everything you need is below.

Reader: someone who was not there and opens this PR in a month to understand
why the code looks the way it does.

Content rules:
- Say what changed and why, one short paragraph each. Leave out anything the
  reviewer can read in the diff.
- Every statement must be traceable to the diff, the commits or the author
  notes below. If neither says it, leave it out. Do not guess at motives,
  history, or what was tested.
- If the author notes contain lines like `Closes #123` (or Fixes/Resolves),
  copy each one verbatim onto its own line near the top. One keyword per
  issue. Do not invent issue numbers.
- Nothing about a session, tools, logins, machines, detours, who asked, or
  that an AI wrote it. No run ids or timestamps.
- Never state the number of lines of code.
- Prose before the first heading is mandatory: {min_words} to {max_words}
  words, not counting Closes lines or fenced blocks. The two impact
  sections at most {max_impact_words} words together.
- Add one mermaid diagram (a fenced ```mermaid block, before the headings)
  when the change has a shape the reader would otherwise rebuild from the
  diff, such as a request path through services, a state machine, a
  scheduling or approval flow, or a user flow across screens. Keep it to
  the nodes the diff touches, 12 at most. A one-file fix, a rename or a
  config change gets no diagram. The diagram does not count toward the
  word limit.
- End with exactly these two headings, in this order, and no other heading
  anywhere in the body:

## Measured impact
## Business impact

- Measured impact: 2 or 3 sentences built ONLY from the Measurements
  section below. Every number in it must appear there; the diff is not a
  source for this section. State each as a gain ('71% faster',
  '17x smaller', '3 clicks fewer'), never as a decrease. The words fell,
  dropped, decreased, reduced, went down, down from, cut from, lowered, and
  the pattern 'from X to Y' with numbers, are all forbidden there. Raw
  before/after numbers may follow the gain in square brackets. A
  measurement of `none` means one sentence saying nothing was measured,
  with the reason given there and no other.
- Order the Measured impact sentences by class, highest present first, and
  lead with the highest: {measure_classes}. A test count belongs last. Do
  not invent a higher class the measurements do not contain.
- Business impact: 1 to 3 sentences on what the change does for the people
  the sources name (a user, a customer, an operator). Do not introduce a
  role the sources do not name; no 'users', 'teams', 'customers' or
  'operators' unless the notes or diff say so, and no claims about support
  load, time saved or workflows. An outcome that waits on a deploy or an
  unrun test is stated as what the change makes possible, not as done. No
  absolutes such as 'eliminates', 'guarantees', 'ensures'. One supported
  sentence beats three padded ones.

Style rules (a linter rejects the body otherwise):
- Numbers are digits (26, not twenty-six). Use few of them.
- No em dashes, en dashes, curly quotes, or emoji. Straight quotes only.
- No colon in the middle of a sentence as a connector. Colons only before a
  list.
- Plain words. Never: delve, crucial, pivotal, tapestry, testament,
  underscore, robust, seamless, leverage, utilize, foster, harness, resonate,
  elevate, embrace, transformative, multifaceted, intricate, vibrant, myriad,
  realm, beacon, paving the way, "it's important to note", "in today's".
- No announcements ("next up", "going forward", "in summary") and no chatbot
  phrasing ("happy to", "let me know", "feel free").
- Active voice. One idea per sentence. Write like a colleague, not a brochure.

PR title: {title}
Branch: {branch} (base: {base})

Commits on the branch:
{commits}

Measurements (the only source for the Measured impact section):
{measured}

Author notes (rough; use the facts in them, not the wording):
{notes}

Diff summary:
{stat}

Diff:
{diff}
{omitted}
"""

RETRY = """

Your previous draft was rejected by the linter:
  {problem}

Previous draft:
{previous}

Fix that and return the complete corrected body again, following every rule.
"""


def allow(tool_input: dict, command: str, reason: str) -> None:
    # updatedInput replaces the whole tool input, so every original field
    # (description, timeout, run_in_background) is echoed back.
    updated = dict(tool_input)
    updated["command"] = command
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
            "updatedInput": updated,
        }
    }))
    sys.exit(0)


# --- context ----------------------------------------------------------------

def _git(cwd: str, *args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def default_base(cwd: str) -> str:
    """PR_BODY_GATE_BASE, else the branch origin/HEAD points at, else FALLBACK_BASE."""
    head = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip()
    return (os.environ.get("PR_BODY_GATE_BASE")
            or head.removeprefix("origin/") or FALLBACK_BASE)


def resolve_base(cmd: str, cwd: str) -> str:
    base = flag_value(cmd, "--base", "-B")
    if not base and PR_EDIT.search(cmd):
        try:
            out = subprocess.run(["gh", "pr", "view", "--json", "baseRefName",
                                  "-q", ".baseRefName"], cwd=cwd,
                                 capture_output=True, text=True, timeout=20)
            base = out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            base = ""
    base = base or default_base(cwd)
    if _git(cwd, "rev-parse", "--verify", "--quiet", f"origin/{base}"):
        return f"origin/{base}"
    return base


def gather_context(cmd: str, cwd: str) -> dict:
    base = resolve_base(cmd, cwd)
    merge_base = _git(cwd, "merge-base", base, "HEAD").strip() or base
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    commits = _git(cwd, "log", "--format=- %s%n%b", f"{merge_base}..HEAD").strip()
    files = _git(cwd, "diff", "--name-only", f"{merge_base}..HEAD").split()
    # A per-file stat and omitted list for thousands of files overflow agy's argv (E2BIG).
    wide = len(files) > MAX_LISTED_FILES
    stat = _git(cwd, "diff", "--shortstat" if wide else "--stat", f"{merge_base}..HEAD").strip()
    if wide:
        stat += "\n" + _git(cwd, "diff", "--dirstat=files,2", f"{merge_base}..HEAD").strip()
    diff_parts: list[str] = []
    omitted: list[str] = []
    used = 0
    for f in files:
        if used >= DIFF_BUDGET:
            omitted.append(f)
            continue
        part = _git(cwd, "diff", f"{merge_base}..HEAD", "--", f)
        if used + len(part) > DIFF_BUDGET:
            omitted.append(f)
            continue
        diff_parts.append(part)
        used += len(part)
    if wide and omitted:
        omitted = [f"{len(omitted)} files"]
    return {
        "base": base,
        "branch": branch,
        "commits": commits or "(none)",
        "stat": stat or "(no diff against the base)",
        "diff": "".join(diff_parts) or "(empty)",
        "omitted": ("Files whose diff is not shown (see the summary above): "
                    + ", ".join(omitted)) if omitted else "",
    }


# --- generation -------------------------------------------------------------

def agy_path() -> str:
    found = shutil.which("agy")
    if found:
        return found
    fallback = os.path.expanduser("~/.local/bin/agy")
    if os.access(fallback, os.X_OK):
        return fallback
    deny("`agy` is not on PATH and not at ~/.local/bin/agy; the PR body gate "
         "cannot write the body. Install agy or put it on PATH.")
    return ""  # unreachable


def agy_store() -> str:
    store = os.path.join(AGY_HOME, AGY_STORE)
    os.makedirs(store, exist_ok=True)
    try:
        os.symlink(AGY_USER_SETTINGS, os.path.join(store, "settings.json"))
    except FileExistsError:
        pass
    return store


def delete_conversation(store: str, conversation_id: str) -> None:
    """Remove conversations/<id>.db, brain/<id>, annotations/<id>.pbtxt and presence/<id>.lock."""
    for path in glob.glob(os.path.join(store, "*", conversation_id + "*")):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def run_agy(prompt: str, cwd: str) -> str:
    store = agy_store()
    cmd = [agy_path(), "--app_data_dir", AGY_STORE, "--model", MODEL,
           "--output-format", "json", "--disable-slash-commands", "--sandbox",
           "--print-timeout", f"{AGY_TIMEOUT_S}s", "-p", prompt]
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=AGY_TIMEOUT_S + 20, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        deny(f"agy ({MODEL}) did not answer within {AGY_TIMEOUT_S}s. Retry the command.")
    except OSError as exc:
        deny(f"agy could not be started: {exc}.")
    if out.returncode != 0:
        err = (out.stderr or out.stdout).strip()
        deny(f"agy ({MODEL}) failed (exit {out.returncode}): {err}")
    try:
        reply = json.loads(out.stdout)
        conversation_id, response = reply["conversation_id"], reply["response"]
    except (ValueError, KeyError, TypeError):
        conversation_id = response = None
    if not (isinstance(response, str) and CONVERSATION_ID.fullmatch(str(conversation_id))):
        deny(f"agy ({MODEL}) did not print the JSON the gate reads, an object with "
             f"conversation_id and response. It printed: {out.stdout.strip()}")
    delete_conversation(store, conversation_id)
    return response


def clean(text: str) -> str:
    text = text.strip()
    m = re.fullmatch(r"```[a-zA-Z]*\n(.*?)\n```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    return text + "\n"


def measurements(notes: str) -> str | None:
    """The `Measured:` lines as one block, or None when the notes lack a usable one."""
    lines = [m.group(1) for m in MEASURED_LINE.finditer(notes)]
    usable = [ln for ln in lines
              if re.match(r"none\b", ln, re.IGNORECASE) or re.search(r"\d", ln)]
    if not usable:
        return None
    return "\n".join("- " + ln for ln in usable)


def generate(cmd: str, notes: str, cwd: str) -> tuple[str, int]:
    measured = measurements(notes)
    if measured is None:
        deny("No usable `Measured:` line in the notes. " + NOTES_RULES)
    ctx = gather_context(cmd, cwd)
    prompt = PROMPT.format(min_words=MIN_WORDS, max_words=MAX_WORDS,
                           max_impact_words=MAX_IMPACT_WORDS,
                           measure_classes="; then ".join(MEASURE_CLASSES),
                           title=flag_value(cmd, "--title", "-t") or "(none given)",
                           measured=measured, notes=notes.strip() or "(none)", **ctx)
    problem = None
    body = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        ask = prompt if problem is None else prompt + RETRY.format(problem=problem, previous=body)
        body = clean(run_agy(ask, cwd))
        problem = validate(body)
        if problem is None:
            return body, attempt
    deny(f"agy ({MODEL}) produced no acceptable PR body in {MAX_ATTEMPTS} attempts. "
         f"Last rejection: {problem}\n\nLast draft:\n{body}\n" + RULES)
    return "", 0  # unreachable


def store(body: str) -> str:
    os.makedirs(GENERATED_DIR, exist_ok=True)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(GENERATED_DIR, f"{digest}.md")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return path


def is_generated(path: str, body: str) -> bool:
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(GENERATED_DIR):
        return False
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return os.path.basename(real) == f"{digest}.md"


# --- entry ------------------------------------------------------------------

def main() -> None:
    payload, tool_input, cmd = read_command()
    if FILL.search(cmd):
        deny("`gh pr create --fill` gives the gate nothing to work from. " + NOTES_RULES)
    gate_title(cmd)

    cwd = payload.get("cwd") or os.getcwd()
    notes, body_file = extract_body(cmd)
    if body_file == "-":
        deny("`--body-file -` (stdin) is not supported by the gate. Pass the notes "
             "inline with --body or in a file.")
    if notes is None and PR_EDIT.search(cmd):
        sys.exit(0)  # title/labels/base only; no body to write
    if body_file and notes is not None and is_generated(body_file, notes):
        sys.exit(0)  # a body this gate wrote, unchanged

    body, attempts = generate(cmd, notes or "", cwd)
    path = store(body)
    new_cmd = rewrite_command(cmd, path)
    if new_cmd is None:
        deny(f"The gate wrote the PR body to {path} (agy {MODEL}, {attempts} attempt(s)) "
             "but could not replace the --body argument in this command. Re-run "
             f"the same command with `--body-file {path}` instead of --body.")
    allow(tool_input, new_cmd,
          f"PR body written by agy {MODEL} in {attempts} attempt(s) at {path}; "
          "the command now passes it as --body-file.")


if __name__ == "__main__":
    main()
