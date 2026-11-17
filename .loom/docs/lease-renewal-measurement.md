# Lease Renewal Write-Volume Measurement (Epic #6165, Phase 1: #6181)

Epic #6165's Phase 1 is split into three issues: write the lease record once,
at dispatch (#6179 / PR #6186), keep it fresh for the sweep's lifetime
(#6180 / PR #6183), and — this document — **measure what that actually costs
in forge writes**, against the epic's own projection and against the forge's
rate limits, "at current fleet size and at 2× (per the epic's own \"Success
criteria\")." This is explicitly load-bearing input to the human approval
gate the epic requires before Phase 2 (reclamation, the phase that changes
behavior) is allowed to start.

## Status as of this measurement (2026-08-14)

**No real fleet renewal-write data exists yet to measure.** This is not a
negative fleet-behavior finding — it is a rollout-timing gap, checked and
confirmed rather than assumed, per the flag the curator left on #6181:

- The commit that writes the first lease record on dispatch (`dffd58bc`, PR
  #6186) and the renewal-loop script (`ad7100b7`, PR #6183) are both on
  `main` — `git merge-base --is-ancestor dffd58bc HEAD` on this checkout
  returns true.
- But the **running** `loom-daemon` binary on every host reachable from this
  session predates that commit:
  - `studio-host` (this host): `loom-daemon 0.18.44 (commit 70ce2544, built
    2026-08-13T17:08:56Z)`
  - `laptop-host` (reachable via `ssh laptop-host`): `loom-daemon 0.18.43 (commit
    f9d9e028, built 2026-08-13T16:34:12Z)`
  - `loom-worker-2`: unreachable from this session (SSH connection timed
    out) — status unknown, treated as unverified rather than assumed clean
    or stale.
  - A daemon only writes/renews lease comments from the binary it is
    *currently running*, not from what is merged to `main` — a rebuild +
    restart is required on each host before any of it can produce real
    writes.
- Confirmed directly rather than inferred from the above: a forge-wide
  search for the exact lease marker string,
  `gh api search/issues -f q='repo:rjwalters/loom "loom:lease host=" in:comments'`,
  returns exactly **3** hits — #6181, #6183, #6186 — all three of which are
  this epic's own issues/PRs, whose *bodies* discuss and quote the marker
  format as documentation. None of the matches is an actual comment whose
  body **starts with** `<!-- loom:lease host=`; zero real lease records
  exist anywhere in the repo as of this measurement.

**Given that, this document does two things**: (1) it establishes the
reproducible methodology and the exact commands a future run should use once
real data exists, and (2) it computes a **projected** (not measured) estimate
directly from the epic's own stated design parameters, so the operator
approval gate has *something* concrete to evaluate now rather than nothing —
explicitly labeled as projected throughout, never presented as a measured
result.

### Why this session did not trigger a daemon rebuild/restart to unblock a real measurement

The curator's guidance for this issue was to confirm (or trigger) a daemon
rebuild+restart before starting the observation window. This session
confirmed the gap (above) but deliberately did not trigger a restart, for
two independent reasons:

1. **Risk.** Both reachable hosts have other sweeps actively running at the
   time of this check (this issue's own sweep among them, plus several
   others visible in `~/.loom/sweeps.json` on `studio-host`). A daemon
   restart is a fleet-operational action with a real chance of disrupting
   in-flight work; a single-issue Builder session restarting shared
   infrastructure mid-flight, without coordinating with whatever else is
   running, is out of proportion to what this issue needs.
2. **It would not have produced a valid sample in this session's time
   budget anyway.** The acceptance criteria ask for "a representative
   period (long enough to include several full sweep lifecycles)." Sweeps
   in this fleet commonly run 30 minutes to multiple hours (the epic body
   itself cites "a 2h sweep renews ~24 times" as the reference case). A
   single Builder session cannot productively wait that long — the honest
   answer this session can give is the methodology plus a labeled
   projection, with a concrete follow-up trigger (below), not a rushed
   restart producing a still-too-thin sample.

## Methodology (reproducible)

The measurement is fleet-wide-observable from the forge alone — no SSH
access to peer hosts is required for the actual write-volume data itself
(only used above, opportunistically, to check daemon versions). Every lease
write and renewal lands as a `gh`-visible issue/PR comment on this shared
repo, with the writing host's identity embedded in the marker
(`<!-- loom:lease host=<hostname> sweep=<sweep-id> -->` — see
[`lease-record.md`](lease-record.md)).

### 1. Enumerate current lease activity (point-in-time snapshot)

```bash
# Find every issue/PR whose comments mention the lease marker string.
gh api search/issues -f q='repo:rjwalters/loom "loom:lease host=" in:comments' \
  --jq '.items[] | {number, title, url}'

# For each candidate <N>, list only the comments whose body's FIRST LINE is
# the marker -- a substring match on the marker text elsewhere in a comment
# (e.g. this very document, once merged) is NOT a lease record; only
# `startswith()` on the marker prefix is authoritative, per lease-record.md.
gh api repos/rjwalters/loom/issues/<N>/comments --paginate \
  --jq '.[] | select(.body | startswith("<!-- loom:lease host=")) |
        {id, created_at, updated_at,
         marker: (.body | split("\n")[0]),
         renewed: (.body | contains("<!-- loom:lease-renewed "))}'
```

`renewed: true` means at least one renewal PATCH has landed on that comment
since it was created — it does **not** tell you how many. GitHub's REST
comments endpoint exposes only the comment's *current* state, not an edit
history or count, so a single snapshot cannot derive a renewal *count* on
its own.

### 2. Compute an actual writes/min rate (two snapshots, diffed)

1. Run step 1's queries, save the output with a wall-clock timestamp `T0`.
2. Wait `N` minutes (long enough to span at least one renewal interval —
   `SWEEP_LEASE_RENEW_INTERVAL_SECS`, default 300s / 5 min, per
   [`lease-renewal.md`](lease-renewal.md)).
3. Re-run step 1's queries at `T1`.
4. Diff by comment `id`:
   - A comment `id` present at `T1` but not `T0` is one **initial write**.
   - A comment `id` present at both, whose `updated_at` advanced, is one
     **renewal write** (each such comment counts as exactly one renewal in
     this window, even if it renewed more than once between `T0` and `T1` —
     a tighter sampling interval reduces that undercount risk).
5. `writes/min = (initial writes + renewal writes) / (T1 - T0 in minutes)`,
   broken out per host (parsed from each comment's `marker` field) if the
   sample supports it.

### 3. Correlate with local sweep counts where available

`~/.loom/sweeps.json` (`loom-daemon/src/sweep_journal.rs`) gives concurrent
sweep counts and durations for **the host you're running on only**. For
peer hosts, this methodology sees lease writes on the forge but not the full
sweep lifecycle underneath them — state that scope boundary explicitly
rather than presenting a partial view as complete fleet coverage.

### 4. Rate-limit comparison

```bash
gh api rate_limit --jq '{core: .resources.core, graphql: .resources.graphql, search: .resources.search}'
```

Lease writes and renewals are REST calls (`POST`/`PATCH` on
`issues/{n}/comments`), so they draw against the **`core`** REST budget, not
`graphql`. Re-check this close to the actual measurement window — it is a
live, moving counter reflecting *all* `gh`/API usage on the token, not just
lease traffic.

**Shared, not per-host.** This repo's `gh` credential resolves to the
`x-access-token` account on every host checked (`studio-host`, `laptop-host`) —
the placeholder identity GitHub assigns to GitHub App installation tokens.
Per prior fleet verification (a single `loom-fleet-dispatch` App
installation, id `4486636`, installed once for this repo), this is very
likely **one shared per-repo budget that every host's daemon draws from**,
not an independent per-host allowance. That materially changes the "at 2×
fleet size" math: doubling the number of hosts does not double the available
budget the way it would with per-host tokens — it doubles the load on the
*same* shared ceiling.

## Projected estimate (NOT measured — see Status above)

Computed directly from the epic's own stated design parameters (Epic #6165
body, "Parameters" and "Rollout"), not from observed traffic:

| Scenario | Concurrent sweeps | Renewal interval | Projected writes/min |
|---|---|---|---|
| Current fleet (3 hosts, cap 8 concurrent/host) | ≤24 | ~5 min | ≤24 / 5 ≈ **~5/min** |
| 2× fleet size (6 hosts, same per-host cap) | ≤48 | ~5 min | ≤48 / 5 ≈ **~10/min** |

(Initial writes are one-per-dispatch and small relative to the renewal
stream for any sweep running longer than one renewal interval — e.g. the
epic body's own reference case, "a 2h sweep renews ~24 times," so initial
writes are folded into "negligible" here rather than itemized separately.)

Against the measured `core` REST budget at the time of this write-up
(**8500/hr ≈ 141.7/min**, `used=12` at the moment of the snapshot above —
see the exact command in §4 to re-check):

- Current fleet: ~5/min ÷ ~141.7/min ≈ **~3.5%** of the shared budget.
- 2× fleet size: ~10/min ÷ ~141.7/min ≈ **~7%** of the shared budget.

Both are comfortably inside the budget **on this projection**, consistent
with the epic's own stated expectation ("Comfortably inside rate limits").
But this is an estimate built from the design's stated parameters, not
observed traffic, and it does not account for the budget's other consumers
(dispatch, Judge, Curator, Champion, Doctor, Guide, Hermit, Auditor, and any
concurrent manual `gh` usage all draw from the same shared ceiling) — a real
measurement could land higher if lease traffic turns out to be bursty rather
than the steady rate this projection assumes, or if the shared-budget
interaction with everything else pushes total usage closer to the ceiling
than lease writes alone would suggest.

**Sensitivity**: writes/min scales linearly, inversely with the renewal
interval. Both the interval (`SWEEP_LEASE_RENEW_INTERVAL_SECS`) and the
per-host concurrency cap are configurable; a future measurement pass that
finds real usage closer to the ceiling than this projection expects should
consider raising the interval before revisiting the design itself — Phase 2
(reclamation)'s expiry threshold is 3× the renewal interval, so this is a
config-level lever, not an architecture-level one.

## Sample size and coverage — explicit caveats

- **Zero real writes observed** (see Status). This document cannot yet
  satisfy the acceptance criterion "measured write volume compared against
  the ~5 writes/min projection" with actual data — only with the projection
  itself, computed from design parameters. Everything under "Projected
  estimate" above is a **model**, not a measurement.
- Even after a fleet-wide daemon rollout, a "representative period... long
  enough to include several full sweep lifecycles" per this issue's own
  acceptance criteria likely needs several hours of continuous fleet
  activity to be credible — a single Builder session's time budget cannot
  produce that on its own.
- Coverage is inherently partial per host for anything beyond raw lease
  write counts (§3 above) — full sweep-lifecycle correlation is only
  possible for the host running the measurement.
- `loom-worker-2` was unreachable during this pass; its daemon version and
  any lease activity it may already be producing are unverified, not
  assumed absent.

## Follow-up: how to complete this measurement with real data

1. Confirm (or trigger) a daemon rebuild + restart on every fleet host, and
   record the rebuild/restart time — that is the earliest possible start of
   a valid observation window, never earlier (a `main` merge alone changes
   nothing about what a *running* binary does).
2. Let the fleet run its normal dispatch load for a period spanning several
   full sweep lifecycles (hours, not minutes) after that.
3. Re-run §1–§4 of the Methodology above (two snapshots, diffed, plus a
   fresh `gh api rate_limit` check close to the window).
4. Replace the "Projected estimate" section above with a "Measured" section
   reporting the real writes/min, per-host breakdown where available, and
   the real headroom against the rate limit at that point — at current
   fleet size and, if feasible, a temporarily inflated concurrency cap to
   approximate the "2×" comparison without literally doubling host count.
5. Post the updated findings as a comment on Epic #6165 (not just this
   document) — the epic's own required input to the operator's Phase 1
   review before Phase 2 may start.

## Why this measurement matters beyond Phase 1: the single-dispatcher mitigation

Epic #6165's own success criteria (added when the epic was structured into
phases) list this write-volume measurement and the exit condition of the
fleet's **interim single-dispatcher mitigation** side by side, deliberately —
they are the same decision from two angles. That mitigation (tracked in the
fleet operator's own operations tracker, outside this repo) is what a fleet
adopts when cross-host reclamation correctness is unproven: restrict dispatch
to one host at a time, giving up roughly two-thirds of fleet dispatch capacity
to avoid the duplicate-build failure mode Epic #6165's own body measured at
125M+ tokens for a single incident. That mitigation's exit condition — "worker
dispatch can be re-enabled with evidence rather than hope" — needs two things
this document (plus the phases around it) supplies: correctness evidence (the
lease gate, Phase 2 #6286, refusing to reclaim a fresh claim regardless of
peer-claim channel state) and cost evidence (this document: renewal write
volume comfortably inside the shared forge rate-limit budget at current fleet
size, ~3.5%, and at 2× fleet size, ~7%, both PROJECTED — see "Projected
estimate" above). Re-enabling multi-host dispatch is a decision for whoever
owns that mitigation to make, not this one, but the evidence it needs about a
fleet's forge-write economics lives here.

## See also

- [`lease-record.md`](lease-record.md) — the marker format this measurement
  greps for.
- [`lease-renewal.md`](lease-renewal.md) — the renewal cadence
  (`SWEEP_LEASE_RENEW_INTERVAL_SECS`) this measurement's projection is
  computed against.
- Epic #6165 — the phase gate this measurement is required input for.
- The fleet's interim single-dispatcher mitigation (tracked outside this repo,
  in the fleet operator's own operations tracker) — the restriction this
  measurement, plus the lease's correctness guarantee, is required evidence
  for lifting.
