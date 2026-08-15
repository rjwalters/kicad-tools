# Crossing-tail census report (`KCT_CROSSTAIL_CENSUS_REPORT`)

A **report-only**, machine-readable aggregate of the diff-pair crossing-tail
legality census. It answers one question about a board — *how much of the
crossover via-site lattice is legal at all?* — as data rather than as scraped
stdout.

Issue [#4799]. The measurement itself is older ([#4580], budget-corrected in
[#4635]); this document covers the structured capture layered on top of it.

## What is being measured

`DiffPairRouter._synthesize_crossing_tail` builds each layer-crossing tail of a
shadow-constructed differential pair by enumerating a **225-entry lattice** of
`(v1, v2)` via-site candidates and shipping the **first legal one** in sorted
order. That first-legal loop says nothing about the rest of the lattice, so two
very different worlds look identical from the outside:

* an **ordering** problem — many sites are legal, and a better sort key could
  pick a kinder one; versus
* a **saturation** problem — almost nothing is legal, and no key can help.

With `KCT_CROSSTAIL_CENSUS=1` the loop scans the whole lattice instead of
stopping at the first legal candidate, and prints one header per crossover:

```
[crosstail-census] net=MIPI_CLK- head=(…) goal=(…) legal=2/225 distinct_v1=1 census_s=0.0164
```

The route that ships is unchanged (still the first legal candidate); the census
credits its own incremental wall clock back to the shadow phase's budget so a
census-on run and a census-off run get the same effective downstream deadlines
([#4635]).

## Enabling the report

```bash
KCT_CROSSTAIL_CENSUS=1 \
KCT_CROSSTAIL_CENSUS_REPORT=/tmp/census.json \
KCT_BOARD06_SHADOW=1 PYTHONHASHSEED=42 \
  uv run python boards/06-diffpair-test/generate_design.py --step route --seed 42
```

* `KCT_CROSSTAIL_CENSUS=1` — turns the underlying census on. Without it nothing
  is measured and the report is an honest *not applicable*.
* `KCT_CROSSTAIL_CENSUS_REPORT=<path>` — writes the JSON document to `<path>`
  at interpreter exit (parent directories are created). Unset ⇒ no file.

Board scripts shell out (`kicad-cli`, helper `python` runs) and every child
process inherits the environment, so each child reaches the same exit hook with
nothing measured. **An empty flush never overwrites a report that has
records** — the child stands down and says so on stderr, leaving the parent's
measurement intact. A run that measured something always writes.

The human-readable summary is printed at the end of the diff-pair phase
whenever the census is on, with or without the report path. Verbatim from
board-06 at seed 42 (2026-08-14, shadow-ON):

```
[crosstail-census-summary] 166 crossover(s) scanned, verdict=saturated
[crosstail-census-summary]   saturated (legal=0): 150/166 (90.4%)
[crosstail-census-summary]   no ordering lever (legal>0, distinct_v1<=1): 7/16 (43.8% of unsaturated)
[crosstail-census-summary]   inert (no ordering key could change the outcome): 94.6%
[crosstail-census-summary]   distinct_v1 max=6 credited census_s total=1.2800
[crosstail-census-summary]   advisory: inert >= 90.0% -- ordering levers are inert; the constraint is upstream in placement / escape planning (non-blocking)
```

Both headline figures match what `boards/06-diffpair-test/README.md` already
documented from the free-text census (150/166 saturated, 1.28 s credited) —
reproduced through the report path rather than re-derived by inspection.

### Why an environment variable, not `kct route --census-report`

The census only fires under `DiffPairRouter` with shadow construction enabled,
and the only caller that does that today is `boards/06-diffpair-test`'s
`generate_design.py --step route` — a board script driving the router API
directly, not `kct route`. A CLI flag would be unreachable from the one run
that produces data, while an env var composes with the two flags that already
gate this measurement. The document itself follows the `--format json`
conventions in [machine-output.md](machine-output.md) (single document, sorted
keys, `schema_version` / `generated_at`), so a future CLI surface can emit it
unchanged.

## Document schema (v1)

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-14T22:37:32.773075+00:00",
  "report": "crosstail-census",
  "census_enabled": true,
  "summary": {
    "applicable": true,
    "crossovers_scanned": 166,
    "saturated": 150,
    "saturated_pct": 90.4,
    "unsaturated": 16,
    "no_ordering_lever": 12,
    "no_ordering_lever_pct": 75.0,
    "inert_pct": 97.6,
    "distinct_v1_max": 4,
    "census_s_total": 1.28,
    "verdict": "saturated",
    "saturated_threshold_pct": 90.0
  },
  "crossovers": [
    {
      "net_name": "MIPI_CLK-",
      "head": [12.7, 44.45],
      "goal": [15.24, 46.99],
      "legal": 2,
      "total": 225,
      "distinct_v1": 1,
      "census_s": 0.0164,
      "saturated": false,
      "no_ordering_lever": true
    }
  ]
}
```

### Per-crossover record

| Field | Meaning |
|-------|---------|
| `net_name` | Net of the crossover's head pad |
| `head` / `goal` | `[x, y]` mm of the tail's endpoints |
| `legal` / `total` | Size of the legal set / lattice size (225) |
| `distinct_v1` | Distinct first-barrel sites among the legal candidates |
| `census_s` | Incremental seconds this crossover's scan cost — the [#4635] credit |
| `saturated` | `legal == 0` |
| `no_ordering_lever` | `legal > 0 and distinct_v1 <= 1` |

`legal`, `total`, `distinct_v1` and `census_s` are the same values the
`[crosstail-census]` header prints, computed once and rendered twice.

### Summary

| Field | Meaning |
|-------|---------|
| `applicable` | `crossovers_scanned > 0` — see "Not applicable" below |
| `crossovers_scanned` | Records collected in this process |
| `saturated` / `saturated_pct` | Crossovers with nothing legal, as a share of all scanned |
| `unsaturated` | `crossovers_scanned - saturated` |
| `no_ordering_lever` / `_pct` | Legal-but-single-site crossovers; the percentage's **denominator is `unsaturated`** ("of the crossovers that had a choice, how many had no real choice") |
| `inert_pct` | `(saturated + no_ordering_lever) / crossovers_scanned` — the share where **no ordering key could have changed the outcome** |
| `distinct_v1_max` | Largest legal-set site count seen (the best ordering lever on the board) |
| `census_s_total` | Sum of the per-crossover credits |
| `verdict` | See below |
| `saturated_threshold_pct` | The threshold the verdict used (default 90.0) |

## Interpreting it (advisory, non-blocking)

| Verdict | Condition | Reading |
|---------|-----------|---------|
| `not-applicable` | nothing scanned | This run never synthesized a shadow-constructed crossover. **Not** a 0%-saturation clean bill of health. |
| `saturated` | `saturated_pct >= saturated_threshold_pct` | The lattice offers almost nothing. Ordering keys are inert; the constraint lives upstream in placement / escape planning. |
| `no-ordering-lever` | `inert_pct >= saturated_threshold_pct` (but not saturated) | Legal sets exist but each has a single site — same conclusion, different mechanism. |
| `ordering-levers-available` | otherwise | Enough crossovers have a real choice that a better sort key could plausibly move results. |

The default threshold is **90%** (`SATURATED_PCT_ADVISORY_THRESHOLD`), taken
from the board-06 precedent: its measured saturation sits in the 90–95% band,
while an open synthetic lattice measures near 0%.

**This is documentation, not a gate.** No code branches on `verdict`, no exit
code reflects it, and nothing about routing changes when it flips. Wiring
saturation into an actual go/no-go or steering decision is deliberate follow-up
work, as is a genuine *pre*-routing predictor (the census's legality checks
consult order-dependent state — drills already placed by earlier crossovers,
the escape-channel registry keyed on which nets are still unrouted, and the
pair's own guide route — so it cannot simply be hoisted ahead of the first A*
expansion).

## Replaying it before the next route (`--census-advisory`)

The document above is still a post-mortem: it lands at the end of the run that
produced it, after the shadow phase has already spent its budget. **Loading it
at the start of the *next* run** is what turns the census into a leading
indicator — same measurement, read earlier:

```bash
# run N: measure (see "Enabling the report" above) -> census.json
# run N+1: predict, before the first A* expansion
uv run kct route board.kicad_pcb --census-advisory census.json
```

`KCT_CROSSTAIL_CENSUS_ADVISORY=<path>` is the equivalent for callers that do
not go through `kct route` (board scripts drive the router API directly); the
flag wins when both are set. The block prints to **stderr**, before any router
or component loading:

Verbatim, replaying board-06's own census report (the 2026-08-14 seed-42
shadow-ON run documented above) against `diffpair_test_routed.kicad_pcb`:

```
[crosstail-advisory] pre-route prediction from census.json, 8.0h old
[crosstail-advisory]   prior run: 166 crossover(s), 150 saturated (90.4%), inert 94.6%, verdict=saturated
[crosstail-advisory]   board cross-check: 9/9 report net(s) still on this board (100.0% coverage)
[crosstail-advisory]   predicted this run: 150/166 saturated crossover(s) (90.4%)
[crosstail-advisory]   worst nets: PCIE_TX- 86/88, MIPI_CLK- 22/24, USB3_TX1- 20/26, USB2_D+ 6/10, PCIE_RX- 5/7 (+4 more)
[crosstail-advisory]   reading: ordering levers are inert here -- the diff-pair shadow phase will spend its budget without a lever to pull; the constraint is upstream in placement / escape planning
[crosstail-advisory]   ADVISORY ONLY -- this route is unchanged by the above (#4799)
```

The headline figures are the ones the census printed 28 minutes into the run
that produced the report (150/166, 90.4%, `inert 94.6%`) — the point is
entirely *when* they are on screen.

This is a *replay*, not a placement-only predictor: it inherits the previous
run's ordering, so its accuracy decays as the design moves. Two guards keep it
honest rather than confident:

* **Board cross-check.** The report's nets are matched against the nets
  declared by the board being routed. Nets that no longer exist do not
  contribute to the predicted counts, so a report is not allowed to predict
  saturation for copper that is gone.
* **Staleness.** When fewer than **50%** of the report's nets still exist on
  the board (`STALE_COVERAGE_PCT_THRESHOLD`), the advisory sets `stale` and
  suppresses the prediction — a report from a *different* design must not be
  reported as this board's forecast. 50% is deliberately generous: a handful of
  renamed nets keeps a same-board report usable.

The prediction is suppressed the same way for a report that scanned nothing
(`verdict: "not-applicable"`, boards 05 / 07 — see below); `applicable` is
`false` in both cases and the block says which.

`--census-advisory` **cannot fail a route.** A missing, unreadable, malformed
or foreign file prints one `[crosstail-advisory] no prediction: …` line and the
route proceeds; the preflight returns 0 unconditionally, unlike the
`_offboard_preflight` gate it is modelled on. Anomalies that are *not* fatal —
a schema version this build does not know, a report written with the census
disabled, a stored summary that disagrees with its own records (the advisory
re-aggregates from the records and says so) — surface as `WARNING:` lines.

### Advisory fields

`CrossingTailAdvisory.to_dict()` is the machine form of the block above (for
tooling; `kct route` prints only the text):

| Field | Meaning |
|-------|---------|
| `source` / `generated_at` / `age_seconds` | Which report, written when, how stale |
| `applicable` | The prediction is usable: the prior run measured something **and** the board cross-check accepted it |
| `stale` | Coverage fell below `STALE_COVERAGE_PCT_THRESHOLD` |
| `board_nets_known` | Whether the board's nets could be read at all (`false` ⇒ cross-check skipped, every report net contributes) |
| `nets_in_report` / `nets_on_board` / `coverage_pct` | The cross-check |
| `predicted_crossovers` / `predicted_saturated` / `_pct` | Restricted to nets still on the board |
| `prior_summary` | The re-aggregated summary block, identical in shape to the report's own |
| `nets[]` | Per-net roll-up (`crossovers`, `saturated`, `saturated_pct`, `no_ordering_lever`, `inert`, `present_on_board`), worst first |
| `warnings[]` | The `WARNING:` lines, verbatim |

## Not applicable ≠ zero

Only board-06 exercises this path today. Board-05 has no differential pairs;
board-07 never calls `route_all_with_diffpairs`. Running either with the report
enabled produces a valid document with `crossovers_scanned: 0`,
`applicable: false` and `verdict: "not-applicable"` — deliberately distinct
from a `0.0` saturation figure, which would read as a clean result for a board
that was never measured. Board-07's own routing failure mode (#3438, pad-array
bundle congestion) is structurally different and **outside** what this census
can see.

## Cost

The capture is a dataclass construction and two list appends per crossover,
performed *after* the census has stamped and credited its incremental cost —
i.e. in the same uncredited, bounded tail as the census's own `print` calls.
Measured overhead is a few microseconds per crossover against a per-crossover
census cost measured in milliseconds, so the report is free relative to the
census it aggregates, and the census remains the only meaningful cost
(`census_s_total` in the report; ~1.3 s across a whole board-06 shadow phase).
The JSON document is written once, at exit.

If the report cannot be written (unwritable path, read-only directory), the
flush prints a diagnostic line to stderr and returns — it never raises, on the
`_offboard_preflight` precedent that report-only surfaces must not block a
route that already succeeded.

### Cost of the advisory

A leading indicator that costs what it is trying to avoid is not one, so this
is the number the eventual go/no-go wiring will need. Measured on board-06's
own 166-crossover report against `diffpair_test_routed.kicad_pcb`
(2026-08-15, same machine as the run that produced the report):

| Step | Wall clock |
|------|-----------|
| `LoadedCensusReport.from_path` (parse + rehydrate 166 records) | 0.0009 s |
| `board_net_names` (parse the board, 26 nets) | 0.1468 s |
| `build_advisory` (roll-up, cross-check, verdict) | 0.0008 s |
| **Total preflight** | **0.149 s** |
| the census this replays, inside the routing run | 1.27 s (`census_s_total`) |
| the board-06 `--step route` run that produced it | ~1680 s (28 min) |

So the prediction costs **~0.01% of the route it precedes** and ~12% of the
in-route census it replays; the board parse dominates it, and everything else
is noise. Its own copy of the census's cost is zero — the measurement already
happened, in a previous process.

## API

```python
from kicad_tools.router.crosstail_census import (
    CENSUS_COLLECTOR,  # process-wide collector
    CrossingTailCensusRecord,  # one crossover
    CrossingTailCensusSummary,  # the aggregate
    write_report,  # write the JSON document explicitly
)

summary = CENSUS_COLLECTOR.summary()
print(summary.saturated_pct, summary.verdict)
write_report("census.json")
```

`DiffPairRouter._census_records` holds the same records for a single router
instance, reset at the start of every `route_all_with_diffpairs` call.

The replay side:

```python
from kicad_tools.router.crosstail_advisory import (
    LoadedCensusReport,  # parse + rehydrate a report
    board_net_names,  # nets of the board about to be routed (None = unknown)
    build_advisory,  # report + board nets -> prediction
    emit_advisory,  # the two above + print; never raises
)

advisory = build_advisory(
    LoadedCensusReport.from_path("census.json"),
    board_net_names("board.kicad_pcb"),
)
print(advisory.applicable, advisory.predicted_saturated, advisory.summary.verdict)
```

`LoadedCensusReport.from_path` raises `CensusReportError` for anything that is
not a readable census report — `emit_advisory` (and therefore `kct route`)
catches it.

[#3438]: https://github.com/rjwalters/kicad-tools/issues/3438
[#4580]: https://github.com/rjwalters/kicad-tools/issues/4580
[#4635]: https://github.com/rjwalters/kicad-tools/issues/4635
[#4799]: https://github.com/rjwalters/kicad-tools/issues/4799
