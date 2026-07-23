<!--
  judge-fanout-results-template.md — EMPTY results template (issue #3748).

  This template is shipped INTENTIONALLY EMPTY. No precision / recall / latency /
  token-cost numbers have been measured, and none are fabricated (the #3739
  evaluation and #3748 both forbid inventing data). An operator running the
  deferred measurement from a TOP-LEVEL Claude Code session fills the rows in
  with REAL numbers — see docs/research/judge-fanout-measurement-runbook.md.

  Provenance of the numbers, once measured:
    - Dimension findings   : count of raw findings across all four reviewers
                             (workflow result: sum before adversarial verify).
    - Verified findings    : findings the adversarial pass kept
                             (workflow result: `findings.length`).
    - Precision            : 1 - (unverified nits / total findings)
                             = verified / (verified + droppedUnverified).
    - Recall               : dimensions that produced a *verified* finding,
                             judged against the PR's known real defects.
    - Fan-out latency (s)  : wall-clock for the whole Workflow({...}) call.
    - Token cost           : tokens/$ for the fan-out run (from the session's
                             transcript usage; see the runbook).
    - Single-pass baseline : the same PR reviewed by today's single-pass Judge
                             (defaults/roles/judge.md), for the A/B comparison.
-->

# Judge fan-out measurement results (issue #3748)

**Status: UNMEASURED.** Fill this in from a top-level session per the runbook.
Do not fabricate numbers.

## Corpus

3–5 PRs with known ground-truth outcomes (a mix of already-merged and
already-rejected). Record each PR's number and true outcome before running.

## Results

| PR # | Known outcome | Dimension findings | Verified findings | Precision (1 - unverified-nit rate) | Recall (dimensions caught) | Fan-out latency (s) | Token cost | Single-pass Judge baseline |
|------|---------------|--------------------|-------------------|-------------------------------------|----------------------------|---------------------|------------|----------------------------|
|      |               |                    |                   |                                     |                            |                     |            |                            |
|      |               |                    |                   |                                     |                            |                     |            |                            |
|      |               |                    |                   |                                     |                            |                     |            |                            |

## Aggregate

| Metric | Fan-out | Single-pass Judge |
|--------|---------|-------------------|
| Mean precision |  |  |
| Mean recall |  |  |
| Mean latency (s) |  |  |
| Mean token cost |  |  |

## Keep / kill call

_(Deferred operator action — requires the measured numbers above. Not part of
the #3748 builder deliverable.)_

- Decision:
- Rationale (grounded in the measured table, not speculation):
- If keep: follow-up proposing how the fan-out slots behind the sweep Judge
  phase behind `LOOM_JUDGE_FANOUT_EXPERIMENT` / a config flag.
