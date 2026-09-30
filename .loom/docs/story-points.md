# Story Points (Fibonacci size classes)

Curator rubric for sizing an issue at curation time: 1, 2, 3, 5, 8 or 13.
Epic #9429; derived in #9430. Points measure **size of the landed change**,
not urgency, risk or wall-clock.

## Assign points (Curator)

1. Estimate the change's **hand-written** lines (added + deleted) and files.
   Exclude generated EDA output (logs, JSON records, netlists, DEF).
2. Pick the row whose lines and files best match. Round to the nearer class.

| points | hand-written lines | files | model-normalized tokens (cross-check) |
|---|---|---|---|
| 1 | ~12 | 1 | ~14M |
| 2 | ~110 | 2 | ~19M |
| 3 | ~285 | 4 | ~30M |
| 5 | ~610 | 6 | ~50M |
| 8 | ~1,000 | 8 | ~73M |
| 13 | ~2,000 | 8+ | ~110M |

Values are per-class **medians**, not bounds; a class covers the range
around its median (midway to the neighbours on a log scale).

3. If lines and files disagree, trust lines, then bump one class if files
   land in a higher row. Tokens and wall-clock are secondary: use them only
   to sanity-check a finished landing, never to size a new issue.
4. **Too big for one issue**: anything you would size above 13, or 13 that
   spans independent concerns, must be split into sub-issues each sized on
   its own. Do not assign 21.

## Why landed size, not tokens

Sweep tokens scale as hand-written lines^0.25 (a fixed Curator/Judge/repo
floor dominates) and differ ~1.5x between Sonnet 5 and Opus 5, so raw tokens
cannot anchor a bound. The anchor is **landed size**: the mean of standardized
log hand-written lines, log hand-written files and log model-normalized tokens
(reliability alpha ~0.83; definition and LSI in #9466,
`observability/sweep-facts/landed-size.sql`). Wall-clock
(`loom.total_duration_sec`) is queue-sensitive and only a cross-check.

Rubric wording barely affects accuracy: seven variants differed by <0.04 on a
paired n=240 test. This document defines the units; it is not tuned prose.

## Provenance and limits

- **Source**: Cloudflare D1 `loom-fleet-telemetry`, `sweep.outcome` records
  since 2026-08-15 (26,260 records; at its row cap and evicting; tracked by the fleet-telemetry retention-cap issue).
  Live SigNoz retains ~7 days and cannot support this.
- **Filter (relaxed)**: landed PR, exactly one `judge` phase, no `doctor`
  phase: 284 of 329 landings. The strict filter (`doctor_cycles = 0`, one
  judge) is not yet applicable: `doctor_cycles` exists only since 2026-09-18
  (119 kept) and per-phase data is #9443.
- **Caveats**: tokens on only ~10% of sweeps (#9440), ~19% of token values
  implausible (#9454), tokens per sweep not per phase (#9443), rework cause
  unrecorded (#9444).
- **Status of the numbers**: the table is the tuning-set medians quoted in
  #9430. They were **not re-extracted** by the query artifact
  (`observability/story-points-queries.sql`); see
  `observability/story-points-evidence.md` for what ran and what remains.
  Bounds are provisional until SP4 is run against fitted params.

## Revision history

**Updating this rubric from calibration output is a normal, expected change,
not an incident.** The calibration loop (issue #9434,
`observability/story-points-calibration-queries.sql`) scores assigned points
against actual landed cost; when its CAL4 view reports a class `drifted`
outside the stated tolerance ([0.5x, 2x] of the claimed median, n >= 20),
moving that bound is the loop working. A revision:

1. records itself below with a marker, date, what moved, and the calibration
   run that motivated it;
2. adds its window row to `rubric_revisions` and its class medians to
   `rubric_classes` in the calibration queries file, so every calibration
   view groups by the marker — a mid-window change shows up as two
   populations, never a silent average;
3. keeps the markers identical across this doc and that SQL. The contract
   test (`loom-daemon/tests/story_points_calibration/static_contracts.rs`) fails if
   the two copies disagree, so neither can drift alone.

| revision | date | what changed | source |
|---|---|---|---|
| v1 | 2026-09-29 | Initial rubric: the #9430 tuning-set medians, quoted verbatim into the table above. No calibration output exists yet (the joined population is still empty), so v1 stands as issued. | #9520 |
