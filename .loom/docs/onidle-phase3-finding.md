# Epic #5038 Phase 3 finding: `onIdle` activation and Class 3 residue

**Status**: Phase 3 complete. This is the written finding Phase 4 (`janitor`
role) is gated on — see #5038's "Suggested phasing" and #5489's acceptance
criteria.

## 1. `onIdle` is configured and confirmed firing

`autonomous.roleRunner.onIdle: ["auditor"]` has been live in this repo's own
`.loom/config.json` since PR #5052 (merged 2026-08-03, tracked by #5046).
Nothing in this phase needed to change that config — it was already the
correct, minimal activation `.loom/config.json` needs for the reasons in
[Section 3](#3-guide-was-deliberately-left-off-onidle).

Cross-checked against this host's live daemon log
(`~/.loom/daemon.log`) and the auditor role's own tick log
(`.loom/logs/role-auditor.log`):

- The daemon log contains direct `idle edge for
  /home/ubuntu/GitHub/loom — firing idle-triggered auditor run (#4364)`
  lines (`role_runner::plan_idle_runs`) — e.g. `2026-08-05T18:40:41.181` and
  `2026-08-05T20:52:34.203` in the current (rotated-since-`2026-08-05T10:05`)
  log.
- Each of those timestamps has a matching tick banner in
  `.loom/logs/role-auditor.log` (`==== loom-daemon role_runner: … role=auditor
  … ====`), confirming the log line and the actual spawned session are the
  same event, not a coincidence.
- Since `auditor` is deliberately **not** in the interval `roles` list (only
  in `onIdle` — see Section 3), every one of the 19 auditor tick banners
  recorded in `.loom/logs/role-auditor.log` between `2026-08-03T22:32` and
  `2026-08-05T20:52` (irregular spacing, 15 minutes to ~8 hours apart — the
  idle-edge signature, not a fixed interval) is necessarily an `onIdle` fire.
  This is real, multi-day operating history, not a single smoke-test tick.

This satisfies acceptance criterion 2 ("The onIdle path is confirmed to
actually fire … not just configured-but-dormant"). The new
[`check-onidle-status.sh`](../scripts/check-onidle-status.sh)
script (added by this PR) automates this cross-check going forward — run it
against any registered workspace to get a verified/dormant verdict per
configured `onIdle` role without hand-grepping logs:

```console
$ ./.loom/scripts/check-onidle-status.sh --root /home/ubuntu/GitHub/loom
onIdle status for /home/ubuntu/GitHub/loom
─────────────────────────────────
  auditor: fired 2x (last: 2026-08-05T20:52:34.203)
```

(Fire count is only for the current, not-yet-rotated daemon log — the 19-tick
multi-day history above comes from `role-auditor.log`, which is not rotated
on the same schedule.)

## 2. Class 3 residue: real, but auditor already narrows it a lot

Sampling the auditor's own tick output over the observed period (see
`.loom/logs/role-auditor.log`) shows the CI-aware step-0 short-circuit
working as designed most ticks — auditor confirms CI is green, reviews
`.loom/logs/guard-decisions.log` for new recurring patterns, and reports
"nothing to file" when there's nothing new. That is Class 3 work (a judgment
call — "is this guard pattern already tracked" — not a deterministic check),
and it is already being exercised, not merely configured.

What auditor's coverage does **not** reach, based on its own role definition
(`defaults/.claude/commands/loom/auditor.md`) and observed output:

- **File/doc-claim staleness** — "is this file genuinely orphaned", "is this
  doc claim still true" (the two examples named in #5038's Class 3
  definition) are not part of auditor's build/test/guard-review scope at all.
  Auditor validates that `main` builds, tests pass, and CI is green; it does
  not walk the tree looking for orphaned files or verify doc claims against
  current code.
- **Branch-abandonment judgment** ("is this branch abandoned or
  intentionally parked") — outside both auditor's (build health) and guide's
  (issue triage/prioritization) stated scope.

So: **yes, Class 3 has a residue beyond what Auditor/Guide already catch** —
the two concrete examples #5038 named (orphaned files, stale doc claims) and
branch-abandonment judgment are not covered by either role today. But the
residue is narrower than #5038's original framing suggested, for two
reasons:

1. **The guard-decision-pattern review already exercises the "which
   recurring symptom is genuinely unaddressed" judgment call** — the same
   fundamental class of question as "is this doc claim still true" — and
   found real, actionable Class 3 findings on this host without any new
   role (auditor is the one that filed against #5385 for the still-firing
   `worktree-write-confinement-unresolved-var` pattern in earlier ticks
   captured in the same log).
2. **The remaining residue (file staleness, doc-claim verification,
   branch abandonment) is a small, well-bounded set** — not the open-ended
   "who owns continuous maintenance" scope #5038 originally worried about.
   It does not obviously need a dedicated role; it could equally be an
   extension of auditor's own step-0 (adding a lightweight periodic
   "sample N files/docs for staleness" pass) rather than a new `janitor`.

## 3. `guide` was deliberately left off `onIdle`

`guide` already runs on the **interval** `roles` list in this repo's config
(`["curator", "champion", "judge", "doctor", "guide"]`) — it is not
idle-triggered, but it already ticks regularly and already owns issue
triage/prioritization, which is the slice of Class 3 the issue body
attributes to it. Adding `guide` to `onIdle` as well would give it two
independent trigger paths into the same debounce/in-progress-guard state
(`role_runner.rs`'s `IdleTrigger`/`InProgressGuard`) for no observed benefit
— guide's interval cadence already gives it regular execution, and nothing
in this phase's observation period showed guide's interval cadence being
insufficient. Per #5038's own anti-granularity-trap criterion ("no new
host-scoped-but-per-repo-scheduled failure mode"), adding a second,
overlapping trigger path is exactly the kind of scheduling-surface growth to
avoid without a demonstrated need. Recommendation: leave `guide` on the
interval list only; revisit if a future observation period shows interval
cadence is too coarse for triage-latency-sensitive work.

## 4. Recommendation for Phase 4

Phase 4 (`janitor` role) is **not clearly warranted** by this finding as a
new standalone role. The residue is real but narrow (file/doc staleness,
branch-abandonment judgment) and has a lower-cost alternative: extend
auditor's existing periodic pass rather than stand up a fourth periodic role
with its own dedup/scheduling/anti-#4736 machinery to design from scratch.
If a future observation period surfaces a materially larger or different
residue than what is documented here, re-open the Phase 4 question with that
evidence rather than proceeding on the original framing alone.

## 5. No new failure mode

This phase made no `.loom/config.json` change (criterion 1 was already
satisfied by #5052) and added one read-only diagnostic script. No new
scheduling surface, no new per-host cron, no new label, no new role. The
anti-granularity-trap criterion (#5038) is satisfied by construction — this
phase's only artifact is verification tooling, not new scheduled work.
