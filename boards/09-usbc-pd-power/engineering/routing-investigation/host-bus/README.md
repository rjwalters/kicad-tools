# Final four host-bus nets

These are diagnostic inputs/logs, not release artifacts. Both sources use the
same placement and completed power/support copper. SCL, SDA, MON_ALERT and
PD_ALERT remain open; the refilled name-only source has zero native violations
and ten unconnected items. The command JSON files record exact router arguments.
Copy the canonical output project, schematic and local libraries into a scratch
directory and install the chosen source as `usbc_pd_power.kicad_pcb` to reproduce.
Adapt only scratch paths in the saved commands; use the same project basename.

- Native KiCad's refilled name-only source: router recognizes zero target nets
  and incorrectly reports success. Known bug #4983.
- Numbered source, 0.1 mm grid: zero of four nets complete, 29.1 seconds.
- Numbered source, 0.05 mm grid: zero of four nets complete, 99.6 seconds.
  Both strict outer-layer experiments only partially connect MON_ALERT.

The canonical generated PCB now contains a manual route completing all four
nets on this placement, using outer layers only; native refill gives zero
violations and zero opens. It is a feasible comparison for improving the
router's obstacle escape and multi-terminal routing. See the parent engineering
review for thermal and procurement gates still blocking manufacture.

Host-bus benchmark request: [#5072](https://github.com/rjwalters/kicad-tools/issues/5072).

## Executable benchmark

From the repository root, with the development dependencies installed (`uv sync
--frozen --extra dev`) and `kicad-cli` on PATH:

```sh
uv run python boards/09-usbc-pd-power/engineering/routing-investigation/host-bus/benchmark.py --results /tmp/board09-host-bus-results
```

The results directory must not already exist. Both committed command JSON files
run sequentially, keeping their strict settings, four target nets, seed 42,
eight iterations and net-class map. Each case copies the canonical project,
schematic and libraries into its own directory, installs the numbered source,
and preserves the project under **both** `usbc_pd_power` and `support` basenames.
Repository PCB/project artifacts are never written. The router subprocess has a
300-second safety cap; timings are observations, not CI thresholds. For comparable
performance, use the same router backend/build and avoid concurrent workloads.
`uv run kct build-native` builds the optional C++ extension; backend selection and
fallbacks appear in each `router.log`.

`results.json` contains the environment, exact commands, router return codes,
wall time spent in routing (including process startup, excluding independent
validation), per-net physical pad connectivity, preservation checks, and native
validation results. Each case retains its own `result.json`, `router.log`, output
PCB, and native DRC JSON/log. The final output, including zones refilled by
`kicad-cli pcb drc --refill-zones --save-board`, is what is measured; a raw partial
snapshot is never treated as canonical output.

A target is `complete` only when **all** its original pads occur in one physical
copper component, `partial` when at least two but not all are connected, and
`unrouted` when every pad is separate. Missing/single-pad targets are `missing`;
missing or unreadable output yields `unknown`. Neither can claim completion.
`largest_component_pads` reports the largest physical group, so an unrouted
multi-terminal net normally has a value of 1. Connectivity uses the existing
label-independent pad partition extractor; native DRC remains an independent
mandatory gate, including possible shorts that a partition alone cannot reject.

`success: true` additionally requires a zero router exit code, all four targets
preserved, unchanged placement/pad geometry, preserved existing segments/vias
and zone definitions, outer-layer target tracks/zones, and a native-refilled
report with zero violations and zero opens. Through vias between the outer
layers are permitted. Zone fill polygons may change during refill. Missing
KiCad produces `native_validation.status: unavailable`, never a clean report.
The script exits zero when both measurements finish even if routing fails;
automation should inspect `success`, not interpret the process exit as routing
success. No routing heuristics or clearances are changed by this diagnostic.

Fast regression coverage:

```sh
uv run pytest tests/test_board09_host_bus_benchmark.py tests/test_benchmarks.py --no-cov
```

## Measured run, 2026-09-10

[Machine-readable measurements](measured-results.json) record KiCad 10.0.6,
Python 3.12.13 and the host platform, against source revision `8801f88c`.
The sweep host had concurrent work, so compare completion before comparing time.

| Grid | Router elapsed | Complete / partial / unrouted | Native violations / opens |
| --- | --- | --- | --- |
| 0.1 mm | 40.69 s | 0 / 1 / 3 (MON_ALERT partial) | 0 / 9 |
| 0.05 mm | 82.43 s | 0 / 0 / 4 | 0 / 10 |

Both canonical routed outputs retained existing copper/zone definitions and
placement, and their target copper remained on outer layers. Neither passed the
success gate. In particular, the fine-grid result does not promote a few emitted
segments to a connected branch. Logs showed C++ search exhaustion or clearance
rejection followed by Python fallback; investigating those routing limitations
is subsequent work. A separate fresh native refill of the manual canonical PCB
in scratch returned zero violations and zero opens, confirming the comparison
remains feasible without claiming that the autorouter solved it.
