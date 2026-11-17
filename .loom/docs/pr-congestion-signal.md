# PR Congestion Signal (`loom-daemon forge pr-congestion`, #9063 Phase 1)

Report-only. Champion's PR Auto-Merge Batch Processing pass runs
`loom-daemon forge pr-congestion` from the repo root at the start of the pass
and copies its verdict line (queue depth, story points, congested y/n, bundle
estimate) into the pass summary.

This is a *measurement*, not a decision input: it changes nothing about which
PRs merge, in what order, or how — the sequential oldest-first drain is
unchanged. Do **not** hand-bundle PRs into merge trains, do not hold, delay,
or reorder a PR because the report says "congested": the failure mode this
guard prevents is an agent improvising a merge train from the signal before
#9063's design questions (bundle compatibility, partial-failure semantics)
are settled by an operator ruling. If the command fails, note that in one
line and continue the pass — a missing measurement is never a blocker.
