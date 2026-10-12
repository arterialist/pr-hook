#!/usr/bin/env python3
"""Rules and command parsing shared by the two PR gates.

pr-body-gate.py checks the body the caller wrote. pr-body-gate-agy.py has a
second model write the body and checks that. Both hold the title and the body
to the rules here.

Rules the body must satisfy (validate()):

  - exactly two headings, last, in this order and no others:
        ## Measured impact
        ## Business impact
  - prose before Measured impact is mandatory: at least MIN_WORDS words that
    are not `Closes #N` lines, and at most MAX_WORDS words; fenced blocks (a
    mermaid diagram) count for neither. The two impact sections at most
    MAX_IMPACT_WORDS words together.
  - Measured impact stated as a gain ("71% faster"), never as a decrease.
  - the shared prose rules in _prose_rules.py.

Rules the title must satisfy (validate_title()): `type(scope): summary` with
a type from TITLE_TYPES, at most MAX_TITLE_CHARS characters, no trailing
period, and the same prose rules.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _prose_rules import check as _prose_check  # noqa: E402

REQUIRED = ("## Measured impact", "## Business impact")
MIN_WORDS = 20
MAX_WORDS = 100
MAX_IMPACT_WORDS = 100

RULES = (
    "PR body rules:\n"
    f"  Prose before the impact sections is mandatory: {MIN_WORDS} to {MAX_WORDS}\n"
    f"  words (a fenced mermaid diagram is free), and the 2 impact sections\n"
    f"  at most {MAX_IMPACT_WORDS} words together. Short paragraphs (what changed\n"
    "  and why), a mermaid diagram where the change has a shape (a request\n"
    "  path, a state machine, a user flow), then\n"
    "  exactly two headings, in this order and no others:\n"
    "    ## Measured impact   - two or three sentences: the win, backed by\n"
    "                           what was measured, stated as a GAIN:\n"
    "                           '71% faster', '17x smaller', '3 clicks fewer'.\n"
    "                           Never 'fell/dropped/went down from X to Y'.\n"
    "                           Unit economics first, then performance, then\n"
    "                           developer experience, then everything else.\n"
    "    ## Business impact   - two or three sentences: users, cost, risk.\n"
    "  Nothing about the session, tools, machine or detours."
)

TITLE_TYPES = ("feat", "fix", "perf", "refactor", "test", "docs", "style",
               "chore", "ci", "build", "revert")
TITLE_FORM = re.compile(r"^(" + "|".join(TITLE_TYPES) + r")(\([\w./-]+\))?: (\S.*)$")
MAX_TITLE_CHARS = 80

TITLE_RULES = (
    f"PR title rules: `type(scope): summary`, at most {MAX_TITLE_CHARS} characters, no\n"
    "trailing period, the issue id in brackets at the end when there is one. The\n"
    "summary says what the change does in plain words (the same prose rules as\n"
    "the body):\n"
    "  fix(alerts): SLA pages fire on outages only (ABC-645)\n"
    "  perf(api): the daily board waits on one fewer database round trip (ABC-648)"
)

PR_COMMAND = re.compile(r"\bgh\s+pr\s+(create|edit)\b")
PR_CREATE = re.compile(r"\bgh\s+pr\s+create\b")
PR_EDIT = re.compile(r"\bgh\s+pr\s+edit\b")
FILL = re.compile(r"\s(--fill|--fill-first|--fill-verbose|-f)\b")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# --- validation -------------------------------------------------------------

def validate(body: str) -> str | None:
    """Return the first rule the body breaks, or None."""
    missing = [h for h in REQUIRED if h not in body]
    if missing:
        return "PR body is missing: " + ", ".join(missing) + "."

    headings = re.findall(r"^\s*#{1,6}\s+(.+?)\s*$", body, re.MULTILINE)
    extra = [h for h in headings if "## " + h not in REQUIRED]
    if extra:
        return ("PR body has headings besides the two required ones: "
                + ", ".join(repr(h) for h in extra) + ".")
    if headings != [h[3:] for h in REQUIRED]:
        return "The two required headings must be in order, Measured then Business."

    measured = re.split(r"^\s*##\s+Business impact\s*$", body, flags=re.MULTILINE)[0]
    measured = measured.split("## Measured impact", 1)[-1]
    decrease = re.search(
        r"\b(fell|dropped|drops|decreased|decreases|reduced|reduces|went down|down from|cut from|lowered)\b"
        r"|\bfrom\s+[\d.,]+\s*[a-zA-Z%]*\s+to\s+[\d.,]+",
        measured,
        re.IGNORECASE,
    )
    if decrease:
        return ("Measured impact states a decrease ('" + decrease.group(0) + "'). "
                "Reframe it as the gain: '71% faster', '17x smaller', 'a fifth of the wait'. "
                "The before/after numbers can follow in brackets.")

    problem = _prose_check(body)
    if problem:
        return problem

    main, _, impact = body.partition("## Measured impact")
    prose = re.sub(r"```.*?```", " ", main, flags=re.S)
    prose = re.sub(r"^\s*(closes|fixes|resolves)\s+#\d+\s*$", " ", prose,
                   flags=re.IGNORECASE | re.MULTILINE)
    words = len(prose.split())
    if words < MIN_WORDS:
        return (f"PR body has {words} words of prose before the impact sections; "
                f"at least {MIN_WORDS} are required. Say what changed and why.")
    if words > MAX_WORDS:
        return f"PR body is {words} words before the impact sections; the limit is {MAX_WORDS}."
    impact_words = len(impact.replace("## Business impact", " ").split())
    if impact_words > MAX_IMPACT_WORDS:
        return f"The 2 impact sections total {impact_words} words; the limit is {MAX_IMPACT_WORDS} together."
    return None


def validate_title(title: str) -> str | None:
    """Return the first rule the PR title breaks, or None."""
    m = TITLE_FORM.match(title.strip())
    if not m:
        return (f"PR title {title!r} must read type(scope): summary, with type one of "
                + ", ".join(TITLE_TYPES) + ".")
    if len(title) > MAX_TITLE_CHARS:
        return f"PR title is {len(title)} characters; the limit is {MAX_TITLE_CHARS}."
    summary = m.group(3)
    if summary.rstrip().endswith("."):
        return "PR title ends with a period. Drop it."
    if re.search(r"\bnot (just|only)\b", summary, re.IGNORECASE):
        return "PR title uses 'not just X but Y'. State the point directly."
    problem = _prose_check(summary)
    return f"PR title: {problem}" if problem else None


# --- command parsing --------------------------------------------------------

HEREDOC = re.compile(
    r"""(?:--body|-b)(?:=|\s+)"\$\(\s*cat\s+<<-?\s*(['"]?)(\w+)\1\s*\n(.*?)\n\s*\2\s*\n?\s*\)\"""",
    re.DOTALL,
)
QUOTED = re.compile(r"""(?:--body|-b)(?:=|\s+)(?:"((?:[^"\\]|\\.)*)"|'([^']*)')""")
BODY_FILE = re.compile(r"""(?:--body-file|-F)(?:=|\s+)(["']?)([^\s"']+)\1""")


def _unescape_double(s: str) -> str:
    return re.sub(r"\\([\\\"$`\n])", r"\1", s)


def extract_body(cmd: str) -> tuple[str | None, str | None]:
    """Return (body text or None, --body-file path or None).

    A --body argument in a shape the regexes miss comes back as the whole
    command: the text is still in there.
    """
    m = BODY_FILE.search(cmd)
    if m:
        path = os.path.expanduser(m.group(2))
        if path == "-":
            return None, "-"
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read(), path
        except OSError as exc:
            deny(f"Cannot read --body-file {path}: {exc}. " + RULES)
    m = HEREDOC.search(cmd)
    if m:
        return m.group(3), None
    m = QUOTED.search(cmd)
    if m:
        if m.group(1) is not None:
            return _unescape_double(m.group(1)), None
        return m.group(2), None
    if re.search(r"(?:--body|-b)(?:=|\s)", cmd):
        return cmd, None
    return None, None


def rewrite_command(cmd: str, path: str) -> str | None:
    """Swap whatever body argument the command carries for --body-file <path>."""
    repl = f"--body-file {path}"
    for pat in (BODY_FILE, HEREDOC, QUOTED):
        new, n = pat.subn(repl, cmd, count=1)
        if n:
            return new
    if re.search(r"(?:--body|-b)(?:=|\s)", cmd):
        return None
    return cmd.rstrip() + " " + repl


def flag_value(cmd: str, *flags: str) -> str:
    for flag in flags:
        m = re.search(re.escape(flag) + r"""(?:=|\s+)(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+))""", cmd)
        if m:
            return next(g for g in m.groups() if g is not None)
    return ""


# --- entry helpers ----------------------------------------------------------

def read_command() -> tuple[dict, dict, str]:
    """Return (payload, tool input, command) for a `gh pr create|edit` call, or exit."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    tool_input = payload.get("tool_input", {}) or {}
    cmd = tool_input.get("command", "") or ""
    if not PR_COMMAND.search(cmd):
        sys.exit(0)
    if re.search(r"\s(--help|-h)\b", cmd):
        sys.exit(0)
    return payload, tool_input, cmd


def gate_title(cmd: str) -> None:
    """Deny a `gh pr create` with no --title, and any --title that breaks the rules."""
    title = flag_value(cmd, "--title", "-t")
    if not title and PR_CREATE.search(cmd):
        deny("Pass the PR title with --title. " + TITLE_RULES)
    if title:
        problem = validate_title(title)
        if problem:
            deny(problem + " " + TITLE_RULES)
