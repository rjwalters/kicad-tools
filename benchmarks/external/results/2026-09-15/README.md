# BeagleConnect Freedom: preserved partial routing

Issue #5348, parent #4946. This is a new measurement; previous results remain unchanged.

## Observed result

The unchanged pinned zero-touch input produced 497 segments, one via and 193.06 mm
of trace. Physical completion increased from 118/460 to 197/460 connections
(42.83%; 79 additional connections). The router returned **2 (partial)**; the
benchmark command returned 0 because report generation succeeded.

`SH1` is placement-invalid. Its entire `GND` net remains excluded, including
on-board terminals. The final audit verified exact protected footprint, copper
and net-membership snapshots, unchanged input hashes, no newly routed GND
segments, and positive-length new eligible copper. The saved board hash is
`174a307de2299b741959b4885aeb5d53a702a2f899301f38598a10d7d289cbae`.

The report's 54 complete nets and routing-stage progress counts have different
scopes; use the independently measured 197/460 physical connections for board
completion. Placement-blocked nets do not become successes through denominator
changes.

## Producer and reproduction

Measured source: `0e8a9a9da0809e6c69620eff1541ff6e94ed58a0`, clean tracked tree.
The imported C++ extension was verified before timing. Native sources were
unchanged since its fresh build at `df762472`. The observed partial-route time
was 2175.907 seconds across the default configuration search, including Python
fallbacks recorded by the router. This is not a pure-C++ speed claim.

```sh
uv run --frozen --extra dev kct build-native --force --verbose
uv run --frozen --extra dev kct bench external \
  --board beagleconnect_freedom --skip-fetch \
  --output-dir /tmp/5348-beagle-0e8a9a9d -v
```

`--skip-fetch` reuses the manifest-pinned source; it does not change routing
rules. Input and binary hashes, upstream commit, commands and local artifact
locations are in [provenance.json](provenance.json). The source and output boards
are intentionally not vendored. The source cache must first be populated through
the normal external-board fetch workflow.

Subsequent changes on this branch initialize the native fill GUI only outside
Linux and correct policy help text. They are not attributed to this measured
commit. Headless Linux filling was separately checked in the official KiCad
10.0 container: all 30 native zone-preservation cases passed, including numeric
and name-only nets, multilayer zones, project/custom/group clearances, empty
zones and holes. The final macOS native-fill and CLI parser suites passed all 66 tests.
Repository Ruff check/format passed; mypy reported 1421 existing errors within
the 1472-error baseline, with no new type errors.

## Limits

This verifies useful partial routing and preservation, not manufacturing
acceptance or a DRC-clean board. Native DRC reports 383 violations, including
199 invalid-outline findings. `kct check` cannot run because its checker rejects
custom pad U6.9. The source already exhibited this checker limitation and
invalid-outline findings. The final report retains the actual violations;
none are waived. The run also warned about unsupported track/via keepout rule
areas and inactive differential-pair checks without declarations. No input,
manufacturing constraint, grid rule or routing budget was relaxed.

See [acceptance-inspection.json](acceptance-inspection.json) for the exact audit
checks and [report.zero-touch.md](report.zero-touch.md) for the unedited summary.
