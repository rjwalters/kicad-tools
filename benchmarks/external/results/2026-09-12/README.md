# External benchmark attempts — 2026-09-12

Issue #5297, website epic #5278. These measurements use tool commit
`696315f02c3f45c0f363e61fb5067a4f41721048` and the unchanged pinned upstream
inputs in `benchmarks/external/boards.toml`. The August reports in the parent
directory remain historical evidence and are not rewritten.

## Results

| Case | Raw outcome / measured artifact | Baseline → measured connections | Delta | Whole attempt |
|---|---|---|---|---|
| [pocketbeagle zero-touch](pocketbeagle.zero-touch.json) | failed / fallback_input | 94/296 → 94/296 | +0 | 19.65 s |
| [beagleconnect_freedom zero-touch](beagleconnect_freedom.zero-touch.json) | failed / fallback_input | 139/460 → 139/460 | +0 | 221.62 s |
| [strf zero-touch](strf.zero-touch.json) | partial / router_output | 23/98 → 21/98 | -2 | 149.33 s |
| [strf tuned](strf.tuned.json) | partial / router_output | 23/98 → 23/98 | +0 | 1273.32 s |

All four invocations produced reports within the declared budget; none was
interrupted or force-killed. Neither STRF case completed either USB pair.
The tuned label records the invoked protocol, not proof that its intended
settings took effect; see the parser limitation below. Per-case `.md` files
are the unedited harness renderings; `diagnostics/` retains raw attempt logs.

## Reproduction and interpretation

The native runtime is Linux x86_64, Python 3.12.14, KiCad CLI 10.0.5,
router C++ version 1.0.0/build 23, with a 4-CPU quota and a requested 12 GiB container memory limit.
The Docker VM has only 8,316,473,344 bytes of total memory (about 7.75 GiB),
so the configured limit does not imply 12 GiB was available. The amd64
processes run through Rosetta on an arm64 host: backend availability means
the C++ extension loaded, not execution on physical x86 hardware. These
timings must not be compared directly with native-host vendor performance.
`run-provenance.json` records the image identity, clean source verification,
commands, input/output SHA-256 hashes, and per-attempt timestamps and budgets.
The local image tag is `kicad-5165-doctor-runtime:local`; its digest identifies
this run but is not a promise that the image is publicly downloadable.

The exact orchestration scripts are retained as `run.sh.txt` and `run-cases.py.txt` (verbatim execution records).
For reproduction, provide an independent clean checkout of the stated tool
commit at `/evidence/source`, Python/uv, KiCad CLI and libraries, and the native
build toolchain. Place the scripts at `/evidence/run.sh` and
`/evidence/run_cases.py`; invoke `bash /evidence/run.sh` inside that runtime.
The launcher installs frozen development dependencies, builds the native
extensions, initializes KiCad libraries, and sets `PYTHONHASHSEED=42`.
Inputs are fetched at their manifest pins into an initially empty cache.

Every case uses `--seed 42 --mfr jlcpcb --layers 4
--kicad-cli-timeout 300 --format json`. The outer budget is 1,800 seconds per
whole harness invocation, including acquisition and measurement, followed by
SIGINT and up to 60 seconds of grace before forced termination. This budget is
separate from the 300-second KiCad DRC timeout and the report's routing-pass
time. A harness exit of zero means a report was collected, not that routing
succeeded. Supervisor timeouts and raw report outcomes are recorded separately.

The zero-touch cases use the shipped rules. Only STRF tuned adds `--tuned`,
using the unchanged `benchmarks/external/tuned_rules.py`: USB_D+/USB_D- and
USB_CONN_D+/USB_CONN_D- use coupled routing, 0.20 mm width and 0.15 mm gap;
SPI3_SCK, SPI3_MOSI, SPI3_MISO and SPI3_!CS use `via_size=0.4` mm. This is the
existing single-diameter mapping, **not** an exact reproduction of a separate
0.4 mm drill / 0.2 mm annular-ring specification. No impedance qualification
is implied.

JSON reports are copied byte-for-byte from the harness. Diagnostics are
retained separately. Third-party source, normalized and routed PCB files
remain untracked under the fetch-at-runtime policy; their hashes identify
the measured artifacts. Baseline connectivity is not new routing progress.
Native timing on a failed or partial attempt is not completed-routing
performance. Missing checks remain not run, and these records do not qualify
any board for manufacturing.

## Completed Beagle attempts

PocketBeagle reproduces the #4945 auto-grid refusal: the selected 0.127 mm
pitch exceeds clearance/2 (0.075 mm) after the 500,000-cell budget limit.
The router exits 1 without an output file. Its 94/296 connected baseline
(31.76%) is unchanged, with zero added connections, tracks or vias.

BeagleConnect Freedom reproduces the #4946 placement refusal: SH1 has five
pads outside Edge.Cuts. The router exits 2 without an output file. Its
139/460 connected baseline (30.22%) is unchanged, with zero added copper.
The current checker cannot evaluate the custom geometry of U6.9, so
`kct_check.ran=false` and its counts remain null. This is the explicit
unsupported-geometry safety limit in `router/io.py`, not evidence that the
board passed. The KiCad DRC slot records 429 violations on the fallback input.
These observations do not change the status of the externally held issues.

Both generated reports classify these generic nonzero exits as `failed`,
with `artifact_source=fallback_input`; their raw outcomes are retained even
though stderr identifies the preflight gates. Their timing phases are
`failed`. PocketBeagle's zero in the KiCad DRC slot does not override its 394
internal-check errors or incomplete connectivity. The fresh Freedom baseline
also differs from the August measurement; this is a new measurement at a
new tool commit, not an edit to or an improvement over the historical run.

## STRF zero-touch result

The router wrote partial output and exited 2: 19 segments, 5.23 mm of tracks,
zero vias, and 21/98 measured connections (21.43%). The baseline was 23/98
(23.47%), so `newly_routed_connections=-2` is retained without clamping or
calling it improvement. Both checks fail (43 internal errors; 146 KiCad DRC
violations); neither USB pair is complete.

A separate strict net-status comparison identifies the delta on GND: 18
baseline islands become 20 output islands, with U3.33 and U4.21 newly
separated. Other nets' connectivity counts are unchanged. The normalizer
retains existing zone-fill polygons, whereas routing regenerates the fills;
this may explain the delta, but causal isolation has not been performed.
The result does not establish that the router removed two routed tracks or
that a new generic routing defect caused the change.

The tuned attempt's stderr records a C++ pathfinder failure followed by
pure-Python A* for `Net-(C1-Pad1)`. A native-available backend stamp is not
proof of exclusive C++ execution. Its elapsed time is mixed execution and
is excluded from completed native routing-performance claims here, whatever
the unmodified report's `timing.valid` field says.

## Parser limitation affecting both STRF protocols

Filed as #5302: the normalized board uses valid bare-atom net names, but
`load_pcb_for_routing()` and `_build_net_number_map()` at this tool commit
only recognize quoted names in the relevant path. Schema analysis sees 49
named nets; routing sees 28 and omits 21, including all eight USB/SPI tuning
entries. Quoting an otherwise identical `(net 41 USB_D+)` reference changes
it from a silently unassigned net0 to the intended USB_D+ net41 in that parser.

The tuned attempt was invoked with the declared sidecar, but its stderr
reports zero of eight entries matched. It therefore does **not** demonstrate
effective application of the intended USB/SPI tuning. Both protocol records
retain this defective-loader behavior rather than silently patching the
harness or modifying upstream board inputs. The generic repair and any
fresh post-fix measurement belong to a subsequent change and dated run.

## STRF tuned result

The router exited 1 with partial output: one 70.25 mm segment, zero vias,
and 23/98 measured connections (23.47%), equal to baseline. It reports 95
internal errors and 144 KiCad DRC violations; both USB pairs are incomplete.
The routing-pass time is 1,260.101 seconds and the complete harness invocation
is 1,273.317 seconds. Four logged C++-to-Python fallback messages mean this
is mixed execution, despite the raw report's native-available stamp.

The raw note saying the declared net-class map was "applied" describes the
invocation. The zero-of-eight match diagnostics and #5302 show why that note
must not be read as evidence of effective tuning. The original JSON and
markdown are preserved; this explanation supplies the missing limitation.
The stderr also records an off-angle segment serialization warning referring
to existing #3907; no completed or fabrication-ready routing is claimed.

## Verification and retained scope

All 3,703 tracked source paths match before and after collection. The two
STRF protocols have byte-identical normalized inputs. The provenance record
identifies acquired, normalized, routed, and actually measured artifact hashes;
it also hashes the report/diagnostic files and the compiled extensions.
All four raw reports retain their generated bytes. The August JSON reports,
rendered table, and README are byte-identical to the pinned tool checkout.

Validation: 133 relevant harness/metric tests and 77 site tests passed.
Direct site-loader checks verify the current reports, baseline deltas, timing
phases and rendered artifact labels. The loader/presentation change itself
belongs to #5298; this evidence change does not alter routing, normalization,
safety gates, tuning rules, or website discovery.

Pinned board licenses remain those recorded in the manifest: PocketBeagle
and BeagleConnect Freedom CC-BY-4.0, STRF MIT. Acquisition is at runtime;
no third-party PCB, schematic or project bundle is committed here. Vendor
reference notes remain original harness output and are not fresh vendor
measurements or a comparable performance claim.
