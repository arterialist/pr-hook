#!/usr/bin/env python3
"""PreToolUse hook for `gh pr create` / `gh pr edit`: lints the title and body.

The caller writes the body and passes it as --body or --body-file. The hook
checks it against the rules in _pr_rules.py and denies the command with the
first rule it breaks. A body that passes goes through untouched. No model
call, no dependency outside the standard library.

`gh pr edit` with no body (a title, a label, a base) is checked for its title
only. Refuses --fill / -f outright, and `--body-file -`, because neither
gives the hook a body to read.

pr-body-gate-agy.py is the other version: a second model writes the body.
Install one of the two, not both.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _pr_rules import (  # noqa: E402
    FILL, PR_EDIT, RULES, deny, extract_body, gate_title, read_command, validate,
)


def main() -> None:
    _, _, cmd = read_command()
    if FILL.search(cmd):
        deny("`gh pr create --fill` takes the body from the commits and skips "
             "these rules. Write the body and pass it with --body or --body-file. "
             + RULES)
    gate_title(cmd)

    body, body_file = extract_body(cmd)
    if body_file == "-":
        deny("`--body-file -` (stdin) cannot be checked. Pass the body inline "
             "with --body or in a file.")
    if body is None:
        if PR_EDIT.search(cmd):
            sys.exit(0)
        deny("`gh pr create` needs a body. " + RULES)
    if body == cmd:
        deny("The hook could not read the body out of this command. Write the "
             "body to a file and pass it with --body-file <path>.")

    problem = validate(body)
    if problem:
        deny(problem + "\n\n" + RULES)


if __name__ == "__main__":
    main()
