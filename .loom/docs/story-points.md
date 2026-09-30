# Story Points (Fibonacci size classes)

**Paused as of 2026-09-30.** Curators no longer assign or refresh story-point
labels or body markers, and points do not gate curation. Existing estimates,
telemetry and the rubric below are retained for historical analysis and a
possible restart. Complexity routing remains active.

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

## The classes (what each row means)

Sized from what the issue demands, in order of how much each signal
mattered in the data: **how many distinct deliverables** it produces, **how
much verification** proving it needs, **how far** the change reaches, and
**whether the cause or design still has to be discovered**. Surface
features — body length, checkbox count, alarming words, generated-artifact
line counts — predicted almost nothing. Read what the issue actually
requires. (Class definitions adapted from the story-points experiment's
validated rubric v6: holdout ρ = 0.82 [0.74, 0.88], n = 133; rubric wording
variants scored within 0.04 ρ of each other, so these are definitions, not
tuned prose.)

**1 — the fixed floor.** The thinking is done and only the typing is left.
The file, often the line, is pinned; done is checked by one command or by
reading the result. No companion test, no second call site, no judgment
call — doc-only, no-behavior-change and "tests N/A" issues live here. A 1
is not free (repo load, checks and review are the floor), but none of the
cost is the change itself.

**2 — a 1 plus one obligation.** Still pre-diagnosed, but one extra thing
must also be true: the duplicated helper moves *and* its callers repoint;
the stale pointer is corrected *and* a check keeps it fresh. One
subsystem, a few files, verified by an existing suite or lint. Name the
extra obligation; if you cannot, it is a 1.

**3 — one mechanism, one real judgment.** The problem is located (one
subsystem, a handful of files), but finishing takes a round of
investigation, a design choice, or genuinely new test coverage: extending
a fix to a sibling case that behaves differently, a new rule with its own
tests, a red-on-main fix where proving correctness is the work. *Not yet a
5* while the change stays at one site and the cause is roughly known.

**5 — several coordinated deliverables that extend an existing mechanism.**
Two to four related sites or a repeated pattern, tests that must
demonstrably bite, docs or evidence updated, usually one embedded
decision. The phrasing is "mirror what the sibling verbs do",
"reuse the helper from #N", "sub-issue 1 of 4" — it extends, not invents.
*Not an 8* while it adds to an existing mechanism instead of building one.

**8 — a new capability with a real verification loop.** Built from nothing
and proven across more than one condition: a new simulation or
characterization campaign from scratch, a shared-tooling feature validated
on several targets, a cross-cutting feature spanning CLI, storage and
failure handling. Four to six acceptance criteria across subsystems, at
least one genuine design judgment, iteration expected. "Involves a
simulation" alone is not an 8; one named bug with a known fix never is.

**13 — several independent correctness claims, each proven.** Not code
volume: each claim needs its own verification loop, and they multiply —
an implementation matching a frozen reference plus must-fail negative
controls; UI across several views with persistence and end-to-end checks;
six or more separately testable items. Few landed examples exist at this
size: prefer 8 unless you can name at least two independent claims.

Anything above 13 — or a 13 spanning independent concerns — is step 4's
split, never a 21.

## Traps

- **Generated output**: simulation and EDA issues can regenerate tens of
  thousands of lines; judge the hand-written part.
- **Terse hides real work; verbose hides trivial work**: a two-line
  "ratify X" can require decision records; a long curated body can
  describe a one-file fix.
- **Repo weight**: heavy test and tooling surfaces raise the floor — the
  same fix costs more there; shift by at most one class.
- **Templated breadth**: porting many files that follow a sibling's
  template is cheaper than the same count of new ones.
- **Prose deliverables** (plans, specs, decision records, research notes)
  are cheap unless they require new measurements.

## Deciding

Picture the finished PR. Count the distinct deliverables; ask what must be
run to prove each one; ask whether anything must still be discovered or
designed. Then pick the row whose definition matches what the issue asks
for. Between two adjacent rows, choose by how much must be discovered or
verified, not by how much must be typed.

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
  phase: 284 of 332 landings — measured 2026-09-30 (#9521); the exclusion
  split is 42 on judge count, 6 on a doctor phase. The strict filter
  (`doctor_cycles = 0`, one judge) kept 119; per-phase data is #9443.
- **Caveats**: tokens on only ~10% of sweeps (#9440), ~19% of token values
  implausible (#9454), tokens per sweep not per phase (#9443), rework cause
  unrecorded (#9444).
- **Status of the numbers**: SP1/SP2 were executed 2026-09-30 against the D1
  cache snapshot (see `observability/story-points-evidence.md`): the filter
  accounting and the token medians are extracted, not quoted, and confirm the
  table's tokens column within the CAL4 tolerance. The hand-written
  lines/files columns could **not** be re-extracted — the #9466 emitters
  postdate the snapshot — and the experiment's own bucket table contradicts
  them at the top classes (non-monotonic, n ≤ 12), so the lines column stays
  **provisional** pending CAL4 drift on the labeled population; SP4's even-n
  median is verified aligned with SP2's.

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
