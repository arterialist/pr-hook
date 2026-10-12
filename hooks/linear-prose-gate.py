#!/usr/bin/env python3
"""PreToolUse gate for Linear MCP writes.

Every status update, comment, issue and document written through a Linear
MCP server passes through here. It refuses text that reads as machine output
and names the first rule the text breaks.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _prose_rules import HINT, check  # noqa: E402

PROSE_FIELDS = ("body", "description", "text", "content", "title")

# Documentation (Linear documents and project descriptions) may use proper
# markdown: headings, bold, lists, tables. It is still prose-gated, and where
# the asd-ste100 skill is installed it must pass that skill's ASD-STE100
# structural rules (hard findings only). Everything else (status updates,
# comments, issue and milestone text) stays plain sentences.
DOC_TOOLS = ("save_document", "save_project")
STE_LINT = os.path.expanduser("~/.claude/skills/asd-ste100/scripts/ste-lint.py")


def deny(reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason + "\n\n" + HINT,
        }
    }))
    sys.exit(0)


def ste_hard_findings(raw: str) -> list[str]:
    """Run the asd-ste100 linter; return its hard findings as one line each."""
    if not os.path.exists(STE_LINT):
        return []
    try:
        out = subprocess.run(
            [sys.executable, STE_LINT, "--json"],
            input=raw, capture_output=True, text=True, timeout=20,
        ).stdout
        data = json.loads(out)
    except Exception:
        return []
    return [
        f"line {v.get('line')}: {v.get('rule')} '{v.get('match')}'. {v.get('message')}"
        for v in data.get("violations", [])
        if v.get("level") == "advisory-free"
    ]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    tool = payload.get("tool_name", "")
    if not (tool.startswith("mcp__") and "linear" in tool.lower()):
        sys.exit(0)

    tool_input = payload.get("tool_input", {}) or {}
    parts = [str(tool_input[f]) for f in PROSE_FIELDS if tool_input.get(f)]
    if not parts:
        sys.exit(0)
    raw = "\n".join(parts)
    is_doc = any(tool.endswith(t) for t in DOC_TOOLS)

    problem = check(raw, digit_spam=not is_doc)
    if problem:
        deny(problem)

    if is_doc:
        hard = ste_hard_findings(raw)
        if hard:
            deny("Documentation must pass the ASD-STE100 structural rules "
                 f"({len(hard)} hard findings):\n" + "\n".join(hard[:8]))
    elif re.search(r"(^|\n)#{1,6}\s", raw) or "**" in raw:
        deny("Headings or bold in a Linear update. Write plain sentences. "
             "(Documents and project descriptions may use markdown.)")

    body = str(tool_input.get("body", ""))
    if body and tool.endswith("save_status_update"):
        wc = len(body.split())
        sentences = len([s for s in re.split(r"[.!?]+\s", body) if s.strip()])
        if wc > 90 or sentences > 5:
            deny(f"Status update is {wc} words in {sentences} sentences. "
                 "Two sentences is the target. Say what changed and why it "
                 "matters, then stop.")
    sys.exit(0)


if __name__ == "__main__":
    main()
