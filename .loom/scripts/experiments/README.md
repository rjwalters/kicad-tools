# `defaults/scripts/experiments/`

Off-by-default, isolated experiment artifacts. Nothing here is wired into the
production Loom path (sweep / judge / doctor / merge). These files exist to be
picked up deliberately by a human or a future top-level session — never
auto-loaded, never triggered by a normal sweep.

## Contents

| File | What it is |
|------|------------|
| `judge-fanout-workflow.js` | A Claude Code Dynamic Workflow that reviews ONE PR via multi-dimension reviewer fan-out + a typed adversarial verify pass. Shipped as a **design sketch** (#3739) and **hardened into a load-bearing artifact** (#3748): real dimension-scoped reviewer prompts (one per `correctness` / `security-credential-surface` / `test-coverage` / `perf-simplification`), a fuller adversarial-verify prompt, and a No-Fable-Judge guard (#3702). NOT wired into `sweep.md`/`judge.md`; NOT in a discovered `workflows/` directory, so the CLI does not auto-load it. |
| `judge-fanout-experiment.sh` | Off-by-default gate/runner. No-op unless `LOOM_JUDGE_FANOUT_EXPERIMENT=1`. When enabled it syntax-checks the workflow **and the corpus-runner scaffold** and prints top-level-session invocation guidance — it never dispatches a live judge run, applies no labels, merges nothing, and creates no issues. |
| `judge-fanout-corpus-runner.sh` | **Measurement-harness scaffold** (#3748). Builds per-PR `args` (`{ pr, diff }`) from `gh pr diff` for a 3–5 PR corpus and emits an empty results table. It is READ-ONLY on GitHub and has a clearly-marked **unfilled** hook point where the operator invokes `Workflow({...})` — a builder cannot fill it in (the Workflow tool is top-level-only; #3289). |
| `judge-fanout-results-template.md` | **Empty** results-table template (#3748). Columns for dimension findings, verified findings, precision, recall, latency, token cost, and the single-pass-Judge baseline. Shipped with no numbers — the operator fills in REAL measured data. |

The **operator runbook** for executing the deferred measurement lives at
[`docs/research/judge-fanout-measurement-runbook.md`](../../../docs/research/judge-fanout-measurement-runbook.md).

Full context, the capability→need mapping, the substrate boundary, and the
keep/defer/reject verdicts live in
[`docs/research/dynamic-workflows-evaluation.md`](../../../docs/research/dynamic-workflows-evaluation.md).

## Flag contract

Follows the `LOOM_MODEL_EXPERIMENT` / `sweep.modelExperiment` precedent
(off by default, loud banner when on):

```bash
# Disabled (default) — no-op, exits 0:
./defaults/scripts/experiments/judge-fanout-experiment.sh

# Enabled — banner + syntax check + guidance (still NO live judge run):
LOOM_JUDGE_FANOUT_EXPERIMENT=1 ./defaults/scripts/experiments/judge-fanout-experiment.sh
```

## Guardrails (why this is safe to have in the tree)

- **One level deep (#3289):** the workflow makes direct `agent()`/`parallel()`
  calls and never calls `workflow()` — it adds no second nesting level.
- **Single-token / in-session:** all agents share the session's one OAuth token.
  This makes NO claim to multi-account rotation — that is a `loom-daemon` +
  `spawn-claude.sh` concern on the other side of the substrate boundary.
- **Read-only:** the workflow returns a verdict object; it merges nothing, applies
  no `loom:pr` / `loom:changes-requested` transitions, and creates no GitHub issues.
- **No-Fable-Judge (#3702):** the fan-out reviewers play the Judge role, so their
  resolved model must never be `fable`. The workflow's `assertNotFable()` guard
  rejects an explicit `args.model: "fable"`; run it from an opus/sonnet session so
  the inherited session default is never fable either.
- **Deferred measurement:** #3748 hardened the workflow and shipped the
  measurement scaffold (`judge-fanout-corpus-runner.sh`), the empty results
  template, and the operator runbook — but **the live measurement itself is still
  a deferred operator action**. It requires the Workflow tool (top-level-session
  only, #3289) and produces real precision/recall/latency/cost numbers that are
  NOT fabricated here. See the runbook and the evaluation doc → "What is deferred".
