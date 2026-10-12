#!/usr/bin/env python3
"""Compare agy models for pr-body-gate-agy.py on merged commits of a repository.

For each (case, model, run) it calls the gate's generate() in-process, then
asks a judge model to list every claim in the body that neither the notes nor
the diff supports. Writes every body and verdict to <outdir>/results.json and
prints a table of mean unsupported claims, lint attempts and seconds.

    python3 eval/compare_models.py <repo> <cases.json> <outdir> [runs] [models,comma,separated]

cases.json maps a case name to the commit under test, the PR title and the
notes its author would have passed (see cases.example.json). The body is
written for the diff between the commit's first parent and the commit, so a
squash-merge or merge commit stands in for the branch. The script adds one
detached worktree per case under <outdir>; `git worktree remove` them after.

PR_BODY_GATE_JUDGE names the judge model (default gemini-3.1-pro-high).
JUDGE_ONLY=1 judges the bodies already in <outdir>/results.json again.
"""
import concurrent.futures as cf
import importlib.util
import json
import os
import subprocess
import sys
import time

REPO = sys.argv[1]
CASES = json.load(open(sys.argv[2]))
OUT = sys.argv[3]
RUNS = int(sys.argv[4]) if len(sys.argv) > 4 else 2
MODELS = (sys.argv[5].split(",") if len(sys.argv) > 5
          else ["gemini-3.8-flash-medium", "gemini-3.8-flash-high"])
JUDGE = os.environ.get("PR_BODY_GATE_JUDGE", "gemini-3.1-pro-high")
os.makedirs(OUT, exist_ok=True)

HOOKS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks")
sys.path.insert(0, HOOKS)
spec = importlib.util.spec_from_file_location("gate", os.path.join(HOOKS, "pr-body-gate-agy.py"))
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

JUDGE_PROMPT = """You audit a pull request body for claims its sources do not support.
Sources: the author notes, the commits and the diff below. A claim is
unsupported if the sources do not state it or directly imply it. Count:
invented reasons or motives, invented history, invented test activity,
numbers not present in the sources, outcomes stated as certain that the
sources only suggest, and mechanisms described differently from the diff.
Do not count style, word choice, omissions, or paraphrase of something the
sources do say. Do not count the two heading lines. Be strict but literal.

Return JSON only, matching the schema: {{"unsupported": [{{"claim": "<quote from the body>", "why": "<one sentence>"}}], "count": <int>}}

PR BODY UNDER AUDIT:
<<<
{body}
>>>

AUTHOR NOTES:
{notes}

COMMITS:
{commits}

DIFF:
{diff}
"""

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "unsupported": {"type": "array", "items": {"type": "object", "properties": {
            "claim": {"type": "string"}, "why": {"type": "string"}}, "required": ["claim", "why"]}},
        "count": {"type": "integer"},
    },
    "required": ["unsupported", "count"],
})


def worktree(name, sha):
    path = os.path.join(OUT, "wt-" + name)
    if not os.path.isdir(path):
        subprocess.run(["git", "-C", REPO, "worktree", "add", "--detach", "-q", path, sha], check=True)
    return path


def make_cmd(case):
    return f'gh pr create --base {case["sha"]}^1 --title "{case["title"]}" --body "notes"'


def one_generation(name, case, model, run):
    cwd = worktree(name, case["sha"])
    gate.MODEL = model
    t0 = time.time()
    denied = {}

    def _deny(reason):
        denied["reason"] = reason
        raise SystemExit(0)
    gate.deny = _deny
    try:
        body, attempts = gate.generate(make_cmd(case), case["notes"], cwd)
        status = "ok"
    except SystemExit:
        body, attempts, status = denied.get("reason", ""), gate.MAX_ATTEMPTS, "denied"
    return dict(case=name, model=model, run=run, status=status, body=body,
                attempts=attempts, seconds=round(time.time() - t0, 1))


def judge(rec, ctx):
    prompt = JUDGE_PROMPT.format(body=rec["body"], notes=CASES[rec["case"]]["notes"],
                                 commits=ctx["commits"], diff=ctx["diff"])
    out = subprocess.run([gate.agy_path(), "--model", JUDGE, "--output-format", "json",
                          "--disable-slash-commands", "--sandbox", "--print-timeout", "170s",
                          "--json-schema", SCHEMA, "-p", prompt],
                         capture_output=True, text=True, timeout=200, stdin=subprocess.DEVNULL)
    try:
        verdict = json.loads(out.stdout)["structured_output"]
        verdict["count"] = len(verdict["unsupported"])
    except (json.JSONDecodeError, KeyError, TypeError):
        verdict = {"unsupported": [], "count": -1, "raw": (out.stdout + out.stderr)[:500]}
    rec["judge"] = verdict
    return rec


def row(label, name, rs):
    ok = [r for r in rs if r["status"] == "ok" and r["judge"]["count"] >= 0]
    uns = (sum(r["judge"]["count"] for r in ok) / len(ok)) if ok else float("nan")
    mean = lambda k: sum(r[k] for r in rs) / len(rs)  # noqa: E731
    note = "" if len(ok) == len(rs) else f"   ({len(rs) - len(ok)} denied/unjudged)"
    print(f"{label:26} {name:28} {len(rs):>4} {uns:>11.1f} {mean('attempts'):>8.1f} {mean('seconds'):>8.1f}{note}")


def main():
    contexts = {name: gate.gather_context(make_cmd(c), worktree(name, c["sha"])) for name, c in CASES.items()}
    jobs = [(n, c, m, r) for n, c in CASES.items() for m in MODELS for r in range(RUNS)]
    results = []
    if os.environ.get("JUDGE_ONLY"):
        results = json.load(open(os.path.join(OUT, "results.json")))
    else:
        # gate.MODEL is module state, so generations for one model run together.
        for model in MODELS:
            batch = [j for j in jobs if j[2] == model]
            with cf.ThreadPoolExecutor(max_workers=3) as ex:
                results += list(ex.map(lambda j: one_generation(*j), batch))
            print(f"generated {model}: {len(batch)} bodies", file=sys.stderr)
        json.dump(results, open(os.path.join(OUT, "results.json"), "w"), indent=2)
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        results = list(ex.map(lambda r: judge(r, contexts[r["case"]]), results))
    json.dump(results, open(os.path.join(OUT, "results.json"), "w"), indent=2)

    print(f"{'model':26} {'case':28} {'runs':>4} {'unsupported':>11} {'attempts':>8} {'seconds':>8}")
    for model in MODELS:
        for name in CASES:
            row(model, name, [r for r in results if r["model"] == model and r["case"] == name])
        row(model, "ALL", [r for r in results if r["model"] == model])


if __name__ == "__main__":
    main()
