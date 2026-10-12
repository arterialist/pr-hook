# pr-hook

Claude Code `PreToolUse` hooks that check the text an agent writes for other people. They check pull request titles and bodies filed with `gh`. They also check the issues, comments, status updates and documents that an agent saves through a Linear MCP server.

The hooks use the Python 3.10+ standard library. There is nothing to install.

## The hooks

| File | Fires on | What it does |
|---|---|---|
| `hooks/pr-body-gate.py` | `gh pr create`, `gh pr edit` | Lints the title and the body the agent wrote. Denies the command and names the first broken rule. |
| `hooks/pr-body-gate-agy.py` | `gh pr create`, `gh pr edit` | Lints the title. A second model, run through the `agy` CLI, writes the body from the diff, the commits and the notes of the agent. |
| `hooks/linear-prose-gate.py` | Linear MCP `save_*` tools | Lints the text fields. Denies the call and names the first broken rule. |

The 2 PR gates are 2 versions of one hook. Install one of them.

`hooks/_pr_rules.py` holds the title rules and the body rules for the 2 PR gates. `hooks/_prose_rules.py` holds the prose rules for all 3 hooks.

## Install

1. Copy the `hooks/` directory to `.claude/hooks/` in your repository.
2. Add the hooks to `.claude/settings.local.json` for one machine, or to `.claude/settings.json` for everyone on the repository.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/pr-body-gate.py\"",
            "timeout": 10,
            "statusMessage": "Checking the PR title and body"
          }
        ]
      },
      {
        "matcher": "mcp__linear-server__save_.*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$CLAUDE_PROJECT_DIR/.claude/hooks/linear-prose-gate.py\"",
            "timeout": 10,
            "statusMessage": "Checking Linear prose"
          }
        ]
      }
    ]
  }
}
```

For the agy version, name `pr-body-gate-agy.py` in the first command and set `"timeout": 540`. The hook makes at most 3 model calls of 150 s each.

In the second matcher, replace `linear-server` with the name of your Linear MCP server.

## Title rules

The 2 PR gates check every `--title` with code. They deny `gh pr create` without `--title`.

- The form is `type(scope): summary`. The scope is optional.
- The type is one of `feat`, `fix`, `perf`, `refactor`, `test`, `docs`, `style`, `chore`, `ci`, `build`, `revert`.
- The title has at most 80 characters and no period at the end.
- The summary passes the prose rules below. The gates also refuse "not just X but Y".

Example: `perf(api): the daily board waits on one fewer database round trip (ABC-648)`

## Body rules

- The body ends with exactly 2 headings, `## Measured impact` then `## Business impact`. The gates refuse any other heading.
- The prose before the headings has 20 to 100 words. The count skips `Closes #N` lines and fenced blocks, such as a mermaid diagram.
- The 2 impact sections have at most 100 words together.
- Measured impact states a gain, such as "71% faster". The gates refuse the words fell, dropped, reduced and lowered there, and also "from 101 s to 29 s".
- The body passes the prose rules below.

## Prose rules

All 3 hooks refuse the items in this list.

- A number written as a word, such as "twenty-six". Numbers are digits.
- The words in `BANNED` in `_prose_rules.py`, such as `crucial`, `robust`, `seamless` and `leverage`.
- An announcement at the start of a line, such as "in summary" or "going forward".
- A chatbot phrase, such as "happy to" or "let me know".
- Em dashes, en dashes, curly quotes and emoji.
- A colon that joins 2 clauses in one sentence.

The word rules and the colon rule skip text in backticks, fenced blocks and URLs.

## The lint-only version

`pr-body-gate.py` reads the body from `--body` or `--body-file` and checks it. A body that passes goes to `gh` unchanged. The hook denies a body that fails, and gives the broken rule and the full format. The agent then writes the body again.

The hook denies these commands because it cannot read a body from them:

- `gh pr create --fill`
- `--body-file -`
- a `--body` that is not a quoted string or a `"$(cat <<'EOF' ... EOF)"` heredoc

For `gh pr edit` without a body, the hook checks the title only.

## The agy version

`pr-body-gate-agy.py` needs the `agy` CLI on `PATH` or at `~/.local/bin/agy`, with a login that can run the model.

The agent does not write the body. It passes notes as `--body` or `--body-file`:

```
Closes #123
Measured: extraction 101 s -> 29 s (pytest tests/x -k y, 3 runs)
Measured: tests/api/test_x.py 14 passed, 3 new
Why: the old path read request.url, which behind the proxy is 0.0.0.0:3000
Rejected: the host from the account settings, the user is already on the right host
```

The hook does these steps:

1. It denies the command if the notes have no `Measured:` line. A line needs a number, or the form `Measured: none, <reason>`.
2. It collects the commits and the diff between the base branch and `HEAD`. The hook stops at 60,000 characters of diff and names the files it left out.
3. It gives the model the title, the commits, the diff and the notes, and asks for a body.
4. It checks the body with the body rules. It sends a rejected draft back with the reason, for at most 3 attempts.
5. It writes the body to `$TMPDIR/pr-body-gate/<hash>.md` and replaces the body argument with `--body-file <that file>`.

The model builds the Measured impact section only from the `Measured:` lines. The prompt asks the model to put unit economics first, then performance, then developer experience, then test counts. The prompt also asks for one mermaid diagram when the change has a shape, such as a request path or a state machine.

A `--body-file` that the hook wrote passes with no model call if you did not edit it. If you edit the file, the hook reads it as notes again.

The base branch is `--base`, then the base of the open PR for `gh pr edit`, then `PR_BODY_GATE_BASE`, then the branch that `origin/HEAD` names, then `main`.

The hook diffs the directory where Claude Code runs. A `cd` inside the command does not change that directory.

### Model

The default model is `gemini-3.8-flash-medium`. Set `PR_BODY_GATE_MODEL` to another id from `agy models`.

On 3 merged branches with 2 runs each, `gemini-3.8-flash-medium` left 0.2 unsupported claims per body and took 34 s. `gemini-3.8-flash-high` left 0.0 and took 65 s. The judge was `gemini-3.1-pro-high`.

`eval/compare_models.py` does this measurement on your repository. Write a cases file from `eval/cases.example.json`, then run:

```
python3 eval/compare_models.py <repo> <cases.json> <outdir> [runs] [models]
```

## Linear rules

`linear-prose-gate.py` reads the `title`, `description`, `body`, `text` and `content` fields of a Linear MCP write.

| Tool | Rules |
|---|---|
| `save_document`, `save_project` | The prose rules. The hook permits markdown. |
| `save_status_update` | The prose rules, no headings, no bold, at most 4 numbers, at most 90 words and 5 sentences. |
| Every other `save_*` tool | The prose rules, no headings, no bold, at most 4 numbers. |

A short text gets fewer numbers. The hook refuses 3 numbers if they are more than 12% of the word count.

Documents also pass through the linter of the [asd-ste100 skill](https://github.com/danyuchn/asd-ste100-skill) if the skill is at `~/.claude/skills/asd-ste100/`. If the skill is absent, the hook does not do that check.

## Change the rules

Each limit, word list and prompt is a constant at the top of its file. Edit the copy in `.claude/hooks/`.

| To change | Edit |
|---|---|
| Word limits, required headings, title types, title length | `_pr_rules.py` |
| Banned words, announcements, chatbot phrases | `_prose_rules.py` |
| The prompt, the model, the diff limit, the number of attempts | `pr-body-gate-agy.py` |
| Which Linear tools permit markdown | `linear-prose-gate.py` |

## Tests

```
python3 -m unittest discover -s tests
```

The tests for the agy version put a fake `agy` on `PATH`. They make no model call and need no network.
