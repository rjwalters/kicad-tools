# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.15.1] - 2026-07-13

### Summary

Patch release. Restores the `uv`-based install path (broken upstream by the
removal of `uv add --git` flags), hardens the JLCPCB offline-catalog parts flow,
and fixes two check/routing net-classification bugs.

### Added

- **Offline-catalog fallback in `LCSCClient.search()`** (#4131) — fall back to
  the local jlcparts catalog when the official API is unavailable

### Fixed

- **Installation via `uv`** (#4122) — use a `git+URL` direct reference instead
  of the removed `uv add --git` flags
- **Split jlcparts archive extraction** (#4125) — stream-extract multi-part
  archives that Python's `zipfile` rejects
- **Cross-type LCSC value matching** (#4130) — reject resistor-vs-capacitor
  value mismatches
- **Floating-pin hole-pair classification** (#4129) — classify floating-pin
  hole pairs as different-net, not same-net

## [0.15.0] - 2026-07-13

### Added

#### Router — Feasibility, Corridors & Coupled Routing

- **Monotonic feasibility certificate + constructive escape ordering** (#4089) — Certify escape feasibility and derive a constructive net ordering from it
- **Corridor attractor in the coupled joint-state cost loop** (#4088) — Bias the coupled diff-pair A* toward a shared corridor
- **Soft corridor reservation for cross-package diff pairs at escape** (#4090), plus **slack-budget corridor widening + slack-aware serpentine tuner** (#4092, Phase 1, flag-gated)
- **C++ port of the coupled diff-pair joint-state A\* loop** (#4069)
- **Scoped bundle river planner v1** (#4053, #4070) — Plan facing-column bus reversals
- **Reactive escape-freedom byte-lane reorder** (#4060, opt-in, off by default)
- **Congestion/escape-driven placement nudge** (#3872) and **joint region re-solve** (#3871) — Escape the 1:1-trade congestion minimum for placement-bound nets
- **Read-only stuck-net classifier** (#3867) — `kct net-status --why`; POUR_DISCONTINUOUS classification for pour-carried nets (#3905)
- **Deterministic iteration budgets** (#3879, #3882) — Load-independent `--deterministic-budget` with tuned per-net iteration cap, adopted in the chorus recipe
- **`--order-method` to wire RoutingOptimizer into `kct route`** (#3898)
- **45-degree geometry enforced by construction** at the copper-emission choke point (#3975)

#### Diff-Pair Routing

- **Coupled-pair budget-exit warnings** (#4106) — Warn when coupled pairs budget-exit to single-ended; compose escape routing with the diff-pair pre-pass (#3983)
- **Variable-gap parallel offset within the impedance band** for the shadow constructor (#3991), with **quantized shadow copper by construction + hard per-pair shadow budget** (#3988)

#### Checks, DRC & Validate

- **Stale zone-fill advisory + opt-in `--refill-zones` flag on `kct check`** (#4113) — Warn when copper is measured against possibly-stale stored fills; optionally shell out to `kicad-cli pcb drc --refill-zones --save-board` first
- **Net-relationship labels on hole-to-hole drill-clearance findings** (#4114) — Same-net/different-net tags, unconditional net display, and a BY RULE sub-count breakdown
- **Thin-copper sliver detection** via per-layer morphological open (#3853)
- **`silk_over_copper` / `silk_edge_clearance` DRC rules** (#3849)
- **Net-0 stray-copper bridge detection** between assigned nets (#3819)
- **Length-match measurements on default `kct check` output** + via-inclusive diff-pair skew (#3955)
- **Fail-loud missing-footprint preflight** + heuristic auto-assign (#3868)

#### Copper-LVS Gate (boards 01–07)

- **Label-free zone-pour copper extraction** via hole-aware shapely overlap (#3769); shared `write_lvs_report` helper (#3782)
- **Copper-LVS gates wired across boards 01–07** (#3782, #3784, #3788, #3798), with a `--lvs-only` e2e asserter and board-03 e2e CI job
- **Real LVS evidence** — wire boards 06/07 fixture schematics (#4013); emit `lvs.json` for boards 01+02 (#4001)
- **Hierarchical-schematic LVS** (#4112) — `_schematic_pin_to_net` now recurses `(sheet ...)` references, so LVS is no longer vacuous on multi-sheet designs

#### Parts / LCSC & 3D Models

- **LCSC/EasyEDA fetch-on-demand model resolver tier** (#4072, #4075) and **cross-library substitution tier** for generic footprint model refs (#4033)
- **LCSC tier applied to fleet-waived parts** (#4076); 3D model refs added to fleet footprints so gallery renders show components (#4012)
- **Offline JLCPCB parts catalog** (#4117) — `kct parts sync-catalog` downloads the yaqwsx/jlcparts SQLite dataset into the cache dir; `LCSCClient` falls back to it when the live API is unavailable
- **Official JLCPCB open-platform API backend, BYO access key** (#4119) — HMAC-SHA256 signed client; set `JLCPCB_ACCESS_KEY`/`JLCPCB_SECRET_KEY` to make the official API the preferred parts tier (official → anonymous → offline catalog); keyless behavior unchanged

#### Zones, CLI & Build

- **`zones add --bbox/--region` island-pour flags** (#3813)
- **`pcb move-footprint --absolute` coordinate mode** (#3809)
- **`kct route` routing flags** — monotone-certificate / cross-package / slack flags exposed on the CLI (#4103)
- **Per-step build timings + heartbeat** for silent subprocess steps (#3967)

#### Consumer Tooling / Install

- **Consumer-generic `.claude/commands/kct/` skills** (#4057, #4064) and **vendored portable CI gates into consumer `.kct/ci/`** (#4063)
- **`/kct:tapeout` skill** (#4115) — Guided workflow producing a complete fab-ready export bundle

### Fixed

- **Router correctness** — C++ grid corridor-reservation keep-out + attractor port (#4071, #4078); lateral-trace keep-out + per-reservation soft/hard flags (#4087); through-via measurement in match-group skew (#4030); min-hole-to-hole drill spacing at via sites (#3857); memory-forced auto-grid refuses to go coarser than clearance/2 (#3945); KiCad-canonical uuid/net ordering + seeded DRC-reroute UUIDs (#3957); budget-bounded negotiated rip-up reroutes (#3992); native-backend guard on the slow DDR reach test (#4093)
- **Diff-pair** — stop the impedance-coupling gap from freezing the coupled search (#4052, #4061); restore single-ended back-reference on mid-copy failure (#4082); early-abort the collapsed coupled pass so an all-pairs budget-exit ships the single-ended-equivalent result (#4107); via-length-aware match-group tuner (#3985); iteration-vs-wall-clock exit diagnostics (#3954)
- **Validate / DRC** — rotate pad offsets for solder-mask (#4083) and THT drill-clearance (#4067) violation locations; report DRC locations in sheet-absolute coordinates (#4048); model pads as true roundrect/oval geometry (#3835); dedicated `min_hole_to_hole` drill-to-drill spec (#3846); fail-loud when geometric DRC did not run (#3820, #3832)
- **Copper-LVS / connectivity** — layer-aware segment chainer drops phantom crossover shorts (#3792); via-into-pour / via-in-pad bonding (#3796); detect via-to-foreign-pour shorts (#3946); model pour nets per fill-island (#3947); union pour pads across all fill islands (#3777)
- **Zones / stitch** — carve foreign-net track segments out of zone fills (#3778); de-duplicate pour zones for deterministic board-07 fills (#3821); reject connectivity-fallback vias that graze foreign pour (#3930)
- **Build pipeline** — export before verify, stop misreporting the manifest gate as DRC failure (#3974); reject stale `*_routed.kicad_pcb` via mtime capture (#3982); delete the manufacturing bundle when VERIFY fails after EXPORT (#3980); block shorted-board bundles with a DRC safety floor (#3929)
- **Parser / schema / schematic** — synthesize the net table for KiCad 10 `--save-board` output (#4039); preserve multi-unit symbol structure on from-scratch save (#3885); multi-unit-aware net-query connectivity (#4038); emit companion sym-lib-table for synthesized power symbols (#3965)
- **PCB / report / site** — emit absolute pad angles when placing rotated footprints (#3903); cwd-independent pandoc PDF image resolution + dropped-figure detection (#4040); frame the interactive viewer on the board, not the drawing sheet (#4016); center every board on its drawing sheet + `kct pcb center-on-sheet` (#4015)
- **ERC** — suppress cross-sheet global-label false positives in `kct erc` (#3814)
- **Placement** — point the cmaes `ImportError` at the placement extra (#4111); score the current layout in `--dry-run` (#3964); contain components within the real Edge.Cuts outline (#3812)
- **Deps** — make `shapely` a core dependency and fail loud when absent (#3833)
- **Manufacturing / mfr** — write sibling `.kicad_pro` + Default netclass on `apply-rules` (#4109); read `gr_poly/gr_curve` outlines and warn on off-board placement in sync-netlist (#4110)
- **Export / BOM** — hard-fail `--auto-lcsc` with an actionable install hint when the `parts` extra is missing, instead of shipping an empty LCSC column at exit 0 (#4116)

### Changed

- **Board fixtures (00–07) hardened for the gallery** — Reconciled schematic↔PCB net drift to single net models (#3771, #3774), re-routed boards to 0 blocking DRC / clean copper-LVS (#4002, #4004, #4005), and added fail-fast route/verify gates (#3854, #4066, #4068). Board-05 phase tree and ISENSE Kelvin sense nets completed artifact-first (#3908, #3997, #3998)
- **CI gates** — unified the blocking-error counter across the two routed-DRC gates (#4029); activated diff-pair/match-group DRC rules via net-class sidecar in board-06 (#3834); deterministic CI-terminating budgets for board-05/06 re-routes (#3886, #3892)

## [0.14.0] - 2026-06-16

### Added

#### Demo Gallery Website (kicad-tools.org)

- **Astro demo gallery** (#3682, #3683, #3684) — Build-time board data loader, gallery index with one card per board, and per-board detail pages with renders, metrics, and downloads
- **Interactive PCB viewer** (#3692, #3693, #3706, #3708) — Embedded KiCanvas viewer on board detail pages with loading/error states, mobile CSS, and a bulletproof loading overlay
- **Gallery structure & chrome** (#3698, #3703) — Separate "Demo boards" vs "Project" sections (excludes `chorus-test`) and shared Header/Footer across gallery pages
- **LVS status chip in gallery** (#3754) — Surface schematic-vs-layout LVS status on board cards
- **DRC-aware "Ready" badge** (#3718) — Never show a "Ready" badge for boards with DRC violations
- **Cloudflare Pages deploy** (#3687, #3689, #3697) — Token-guarded manual deploy script and workflow (renders before board-metrics, with a Cloudflare account guard)
- **Live demo link** (#3710) — README now links to the live gallery at kicad-tools.org

#### Rendering & Board Metrics

- **`kct render`** (#3677) — Per-board 2D layer plots and 3D PNG renders
- **`kct board-metrics`** (#3678) — Emit a normalized `board.json` per board
- **2D layer plots as SVG** (#3701) — Emit 2D plots as SVG for kicad-cli 10 compatibility
- **Oblique 3D views** (#3704) — Map 3D front/back to oblique top/bottom views

#### PCB & Layout

- **`page_fit`** (#3715) — Auto-size the drawing sheet to the board and center it
- **Auto-size schematic sheet to content** (#3536) — Plus footprint census and off-board supercap docs

#### Checks & Verification

- **ERC/LVS/Manifest meta sub-checks for `kct check`** (#3755) — Roll up ERC, LVS, and manifest verification into `kct check`
- **Independent copper-extracted netlist LVS gate** (#3757)
- **Schematic ↔ routed-PCB LVS guard in board-00 recipe** (#3748, #3753)
- **Board 00 end-to-end regeneration CI gate** (#3751, #3756)
- **`clearance_segment_zone` / via-and-pad-vs-zone-fill DRC rules** (#3527, #3558, #3636) — Detect traces and vias/pads violating clearance against foreign zone fills
- **Connectivity DRC rule** (#3041, #3060) — Flag unrouted multi-pad nets
- **`kct fleet status` / `fleet ship-ready`** (#2832, #2843, #2932, #2939, #3099, #3113) — Survey routing and manufacturing readiness with a warn-only ship-ready gate

#### Routing & CLI

- **`kct route --preserve-existing`** (#3155, #3169) — Incremental routing mode
- **`kct route --net-class-map`** (#2996, #3000) and **`--length-match-groups`** (#2736) — Rich per-net-class routing and length-match tuning
- **`kct pcb lock-footprints` / `unlock-footprints`** (#2978)
- **`kct sch assign-footprints` + footprint suggestion** (#3158, #3173, #3175, #3182, #3196) — Bulk and ref-only footprint suggestion with pin-count validation
- **Auto PCB sizing** (#3352, #3359, #3404) — `--auto-pcb-size` with a sum-of-clearances area heuristic and edge-cut grow
- **Fine-pitch escape routing** (#3374, #3378) — Adaptive-radius escape detector with per-net-class clearance threading

### Fixed

- **Zone-fill foreign-pad clearance** (#3712) — Carve foreign-net antipads out of zone fills
- **Foreign-pad-metal traversal** (#3225, #3226, #3227, #3545, #3565) — Reject A* routes through static foreign-pad halos and sync the `pad_blocked` bitmap into the C++ grid
- **Euclidean clearance kernels** (#3232, #3248) — Switch trace- and via-clearance kernels from Chebyshev to Euclidean discs
- **Multi-layer via-barrel clearance** (#3487, #3517, #3522, #3578) — `clearance_segment_via` checks every layer a barrel spans; certify global-min clearance in R-tree queries
- **Board zone-fill refreshes** (#3552, #3576, #3584, #3725) — Refill stale zones on boards 02/04/05 to clear segment-vs-foreign-fill findings
- **Manufacturing manifest hashing** (#3529, #3572) — Write BOM/CPL CSVs with LF endings so manifest hashes match committed content
- **PCB viewer overlay** (#3706, #3708) — Emit the viewer overlay script as raw JS so it actually runs

### Changed

- **C++ router parity** (#864, #3657, #3654, #3659) — Align C++ standard-mode via/pad-clearance behavior with the Python reference path
- **Hybrid placement objective** (#3186, #3189) — Hard-constraint gate plus 10-term soft objectives for `kct placement optimize`

## [0.13.0] - 2026-04-28

### Added

#### Schematic Editing

New atomic schematic editing commands for fully programmatic circuit construction.

- **`add-component`** (#1872) — Place symbols from library into schematics with junction and wire awareness
- **`add-wire` / `remove-wire`** (#1883, #1881) — Place and delete wire segments by coordinate
- **`add-label`** (#1885) — Place net labels on schematic wires
- **`add-no-connect` / `disconnect`** (#1858) — Place no-connect flags and disconnect pins
- **`add-bypass-cap`** (#1940) — Composite command to insert decoupling capacitors with automatic wire routing
- **`add-pull-resistor`** (#1939) — Composite command to add pull-up/pull-down resistors with wire rerouting around crossings
- **`insert-inline`** (#2111) — Break a wire and insert a component inline
- **`remove-component`** (#2133) — Delete symbols with cleanup of exclusive wire segments
- **`set-value`** (#1871) — Update symbol `Value` property
- **`set-footprint`** (#1855) — Assign footprint to a schematic symbol
- **`set-reference`** (#2180) — Rename reference designators
- **`set-symbol-property`** (#2181) — Set boolean flags on symbols
- **`move-component`** (#2186) — Reposition symbols on the schematic
- **`reconnect-pin`** (#2183) — Atomic pin-to-net reassignment
- **`set-label-direction`** (#1882) — Change global/hierarchical label shape/direction
- **`re-annotate`** (#1897) — Batch reference designator annotation with `--unannotated-only` and `--include-power` flags
- **`repair-instances`** (#1971) — Repair or create missing project instances blocks in symbol definitions
- **`cleanup-wires`** (#2179, #1858) — Detect and remove sub-mm dangling wire stubs and duplicate wire segments

#### Schematic Inspection

- **`pin-map`** (#1903, #2201, #2237) — Resolved pin-to-net assignments with coordinates, traversal across full hierarchy, connected field, and synthetic `_local_N` nets for floating pins
- **`show-pins`** (#2200) — Enriched pin output with name, type, net, and position fields
- **`sch preflight`** (#1856) — Pre-layout schematic validation including pin/pad count checks and wire connectivity
- **`sch summary`** — Connectivity counts now aggregated correctly across all sheets (#1895)
- **`sch labels --type global`** (#1896) — Now scans all sheets in the hierarchy

#### Schematic Validation (`sch validate`)

- **I2C pull-up resistor check** (#2050) — Detect missing pull-ups on SDA/SCL nets
- **Connector pinout verification** (#1936) — Validate connector pins against known interface standards
- **Symbol-to-footprint pin/pad count mismatch** (#2113) — Check for footprint pad count mismatches
- **BOOT0 pull-down detection** (#2107) — STM32 BOOT0 pin configuration check
- **Missing NRST filter capacitor** (#2102) — STM32 NRST RC filter detection
- **SWD debug pin routing check** (#2112) — STM32 SWD pin connectivity audit
- **Matched channel symmetry check** (#2105) — Detect asymmetry in differential/parallel output filter channels
- **Power pin polarity errors** (#2078) — Detect VDD/GND swap errors
- **Unconnected component detection** (#2082) — Flag components with all pins floating
- **Zone fill check** (#2109) — Detect unfilled copper zones
- **Duplicate reference designator detection** (#1968, #1582) — Per-sheet and cross-sheet duplicate ref des checks
- **Missing instances blocks check** (#1933) — Detect symbols without project instances
- **Global label driver/receiver direction mismatch** (#1909) — Warn when label shapes are inconsistent
- **No-connect on input pins warning** (#1912) — Flag `NC` flags placed on driven input pins
- **Inconsistent global label shapes** (#2049) — Detect mismatched shapes across sheets
- **Pin assignment audit and power-short detection** (#2031) — Validate pin assignments and flag power shorts
- **`value_consistency` check** (#2234) — Flag mixed capacitor voltage rating formatting
- **Unnecessary footprint variety warning** (#2185) — Flag same-value passives with mixed footprint sizes
- **`net_undeclared` rule** (#2106) — Detect PCB pads referencing undeclared nets
- **Solder mask clearance, pad size, and PTH annular ring checks** (#1690)
- **Package-size constraint in LVS** (#1583) — Pass 3/4 LVS validation now checks package dimensions
- **LVS with hierarchical schematic support** (#1568) — Multi-pass matching

#### PCB Commands

- **`pcb sync-netlist`** (#1973, #1983) — Netlist-driven PCB synchronization with collision-safe renames, ambiguous warnings, and `--auto-rename`; `--remove-orphans` flag and standalone `remove-footprint` command (#1982)
- **`create-pcb`** (#1744) — New CLI command and MCP tool to create a PCB from a schematic
- **`pcb move-footprint`** (#2076) — Relocate components on the PCB
- **`pcb add-zone`** (#2079) — Create copper pours
- **`pcb edit-outline`** (#2081) — Manage Edge.Cuts contour
- **`pcb snap-rotation`** (#2080) — Normalize component rotation angles
- **`pcb zones`** (#2077) — Inspect zones and copper pours
- **`pcb reannotate`** (#1569) — Collision-safe batch reference renaming
- **`kct sync`** (#1562) — Reconcile schematic/PCB reference designators
- **`pcb summary`** — Board dimensions added to output (#2178); zone count always displayed (#2084)

#### Routing

- **AdaptiveGridRouter as default** (#1772) — Wired as default for `--grid auto`
- **Sub-grid escape routing** (#1610, #1831) — Integrated into default `route_all` pipeline; fine-zone resolution for dense IC pads
- **BlockRouter** (#1612) — Per-block detail routing with sub-Pathfinder
- **Block-aware routing** (#1616, #1599) — Inter-block net classification; `register_block` for protected-zone routing
- **GCD-based grid candidate generation** (#1762) — Off-grid pad alignment
- **Edge clearance constraint** (#2140) — Avoid board-edge DRC violations
- **Post-optimization DRC verify-and-nudge pass** (#1793)
- **Per-net-class trace widths** (#1546, #1691) — Applied during segment creation and A* search
- **Per-net-class clearance in pre-save validation** (#1664)
- **`--best-effort` flag** (#1759) — Continue pipeline past routing failures
- **`--no-cache` / `--clear-cache` flags** (#1826, #1630) — Route cache control exposed in CLI
- **Seeed/Seeed Fusion manufacturer aliases** (#2027) — Closest-match suggestions for manufacturer names
- **Exit code epilog and SIGINT disambiguation** (#2030)

#### Via Stitching (`stitch`)

- **Extended escape routing for dense IC power pins** (#1818)
- **`--blanket` mode** (#1789) — Grid-based via stitching across zones
- **Stitch step in build pipeline** (#1814) — Added between route and optimize
- **Micro-via retry and structured skip diagnostics** (#2139)
- **`fix-vias`** (#2134) — Detect and repair same-layer vias

#### Placement

- **`place-unplaced`** (#1994) — Grid placement for unplaced components
- **Fast targeted pad clearance nudge** (#1974)
- **Block-aware placement constraints** (#1598) — Reduced-dimensionality encoding for block placement
- **C++ force-directed placement engine** (#1722) — Batch interface with Python fallback
- **C++ AABB cost evaluator** (#1718) — With nanobind bindings
- **C++ evolutionary fitness evaluator** (#1723)
- **Auto-scale boundary forces by component density** (#2032)
- **`place-route` DRC fix-retry loop** (#2002) — `ClearanceRepairer` wired in

#### DRC & Validation

- **`ViolationCategory` enum** (#1975) — Fine-pitch solder mask bridge filtering
- **Footprint nudge for pad-pad clearance violations** (#2218)
- **`repair-clearance`** — Improved post-route clearance repair (#1683)
- **Net names in clearance violation output** (#2150)
- **`--verify` flag in `fix-drc`** (#2083)
- **C++ accelerated pad-to-pad clearance checking** (#1719)
- **Footprint-outside-board placement rule** (#2104)
- **Silkscreen text height auto-fix** (#1522)
- **`--suppress-library` flag** (#1985) — Suppress silkscreen warnings for library components

#### ERC

- **Cross-sheet power pin driver suppression** (#2015) — No more false-positive `power_pin_not_driven` for cross-sheet drivers
- **Re-attribution of violations to correct sheets** (#2236) — Hierarchical designs now show violations on the correct child sheet
- **Label/net names in ERC warning messages** (#1934)
- **`fix-erc`** (#2230) — Handle `unconnected_wire_endpoint` and `wire_dangling` violation types

#### Export & Manufacturing

- **Seeed manufacturer profile** (#2231) — Added to export command
- **Pandoc+TeX PDF rendering** — Manufacturing packages now include PDF reports
- **`--latest-only` flag** (#1679) — Flatten report into a single output directory
- **`--bom-source` flag** (#1561) — PCB-only BOM generation
- **`--keep-build-artifacts` flag** (#2022) — Preserve intermediate build files
- **Spec-aware BOM enrichment** (#1510) — LCSC part numbers from `.kct` project files
- **CPL output filters** (#1511) — Exclude THT and DNP components
- **LCSC API 403 fallback** (#1760) — Fall back to cached enrichment when JLCPCB API is unavailable
- **`-o/--output` flag** (#1632) — Output path for `kct build`
- **Auto-discovery of PCB files** (#1551) — `export` accepts directory paths
- **Design reports with narrative and assembly notes** (#1995)
- **DRC violation-type breakdown table in reports** (#2163)
- **Report stackup section and figure generation** (#1698)

#### Build Pipeline

- **`zones` step** (#1688) — Auto-create power/ground zones before routing
- **Silkscreen step** (#1689) — Ref des visibility and board markings
- **Export step** (#1686) — Added to `kct build` pipeline
- **Placement optimization step** (#1687) — Added to `kct build` pipeline
- **Edge.Cuts outline generation step** (#1505)
- **Zone fill re-enabled as default step** (#1749)

#### CLI & MCP

- **`kct stitch` subcommand** (#1613)
- **`kct check --output`** (#1537) — Persist DRC reports as JSON
- **`/review-schematic` and `/repair-schematic` skills** (#1868)
- **`/review-pcb` and `/repair-pcb` skills**
- **`/export-manufacturing` skill**
- **`report-review` and `report-revise` skills**
- **Prominent C++ backend warnings and CLI hints** (#1970)

### Changed

#### Schematic Infrastructure

- **Embedded `lib_symbols` used by default** (#2202, #2235) — `sch pins`, connection checks, and hierarchy traversal now use symbols embedded in the schematic file rather than requiring `--lib`
- **Wire-graph BFS for pin connectivity** (#2238, #2216) — All pin connectivity checks now use BFS over the wire graph instead of coordinate proximity
- **NetTie net-name tracing** (#2213) — `pin-map` traces net names through `Device:NetTie` symbols
- **Hierarchical label net merging** (#2114) — Improved net merging and component-count validation in netlist builder
- **S-expression tab indentation** (#2026) — `SExp.to_string()` now uses tab indentation for KiCad compatibility

#### PCB / Schema

- **`sync-netlist` uses PCB API for value updates** (#2215) — Routes changes through the API; orphan removal added
- **Footprint position/rotation/layer setters** (#1998) — Synced to S-expression on write
- **Board summary counts** (#1958) — Derived from S-expression tree instead of cache lists

#### Routing

- **`--grid` default changed from `0.25` to `auto`** (#1680) — Prevents DRC clearance violations on mixed-pitch boards
- **KiCad 9 name-only net format** (#1821, #1779, #1780) — Supported throughout router, zones, and DRC repair modules

#### Refactoring

- **`optimize/` consolidated into `optim/`** (#1861)
- **`find_kicad_cli()` consolidated** (#1743) — Single implementation in `cli/runner.py`
- **`SessionManager` consolidated** (#1740) — Single implementation in `tools/session.py`
- **`PatternAdapter` converted to module-level functions** (#1737)

### Fixed

#### Schematic

- **`cleanup-wires`** — Mid-segment stub detection, collinear overlap detection (#2194); uses actual pin positions instead of symbol centers (#2014); strict electrical connectivity for stub detection (#2203)
- **`add-component`** — Junction detection and standalone wire/junction commands (#1884); double-snap replaced with round and connection-aware targeting (#2052); emit `instances` block (#2061); snap rotated pin offsets to 1.27mm grid (#2062)
- **`add-pull-resistor`** — Wire crossing detection and L-shape reroute (#2063)
- **`add-bypass-cap` / `add-pull-resistor`** — Emit `instances` block (#2110)
- **`re-annotate`** — Handles unannotated components and multi-project instances; supports space-indented files (#1920); detects and annotates symbols missing instances blocks (#1976)
- **`sch-replace`** — Updates `lib_symbols` entry and instance pins on replacement (#2051); handles derived symbols with `extends` chains (#2154)
- **`sch-preflight`** — Resolves `extends` chains in pin/pad count check (#2153)
- **`set-label-direction`** — `--sheet` filter made case-insensitive (#1910)
- **Pin position calculation** (#2016, #2129) — Negated Y offset and post-rotation Y in `get_pin_position()`
- **Power symbol junction** (#2126) — Creates junction when power pin lands on wire midpoint
- **`lib_symbols` embedding and lookup** — Fixed for `add-component`
- **`LibraryManager.load_embedded`** (#1911) — Inline schematic symbol loading
- **`repair-instances`** (#2132) — Detects and replaces wrong project names in existing instances blocks
- **`sch connections`** (#2008) — Loads embedded `lib_symbols`
- **Derived symbol pin resolution** (#2160) — Resolves inherited pins using `extends` chains

#### ERC

- **False-positive `power_pin_not_driven`** (#2015) — Suppressed for cross-sheet drivers
- **False-positive `single_global_label`** (#1922) — Filtered for cross-sheet globals
- **False-positive `isolated_pin_label`** (#1937) — Filtered on sheets with no labels
- **Phantom `wire_dangling` violations** (#2217) — Filtered when no matching schematic coordinates
- **KiCad 10 `items` array label extraction** (#1938)
- **`wire_dangling` re-attribution coverage** (#2192, #2151) — Expanded to cover more hierarchy cases
- **`pin_assignment` suppression for MCU GPIO pins** (#2149) — On protocol nets

#### PCB / Schema

- **`sync-netlist`** — Uses `on_board` flag (not `is_virtual`) to filter components (#2232, #2212)
- **`create-pcb`** — Grid layout calculated from board dimensions to prevent overflow (#1996)
- **Board outline detection** (#1509) — Extended to handle `gr_rect` on Edge.Cuts
- **DRC board outline coordinates** (#2130) — Transformed to board-relative space
- **Net name resolution in DRC** (#2193) — Net numbers resolved to names in violation output
- **`fix-drc`** — All violation categories detected; non-repairable violations reported (#2059); aligned categories with `check` (#2083)
- **Same-component pad-to-pad false positives** (#2075) — Suppressed in DRC
- **`.kicad_pro` selection disambiguation** (#2127) — Auto-assigns power symbol references

#### Router

- **Sub-grid escape through neighbor clearance zones** (#1712)
- **C++ backend clearance rules in A* pathfinding** (#1711)
- **Inner-layer segment clearance violations** (#1801)
- **Via-to-via, via-to-pad, via-to-segment clearance** (#1800, #1650, #1797)
- **Segment-to-segment clearance from grid quantization** (#1682)
- **Same-net via merging on save** (#1800)
- **Net-0 orphan trace cleanup** (#1981)
- **Oscillation detection relaxed; full-reorder escape strategy** (#1827)
- **Origin-offset search in grid auto-selection** (#2038) — Mixed-pitch boards
- **Pad approach radius derived from pad geometry** (#1622)
- **Off-grid net skipping and per-net A* timeout** (#1609)
- **Escape candidates that violate inter-pad clearance rejected** (#1836)
- **SSOP odd-pad escape vias routed inward** (#1844)
- **SSOP fine-pitch lateral fan-out offset** (#1791)
- **Post-route clearance correction for all routing strategies** (#1790)

#### Stitch / Zones

- **`stitch`** — Infer inner plane layers from stackup when zones are missing (#2044); check zone fill polygons in via clearance (#1816); include through-hole pads (#1955); post-write verification (#1957); guard `shutil.copy` for same-file input/output (#1950)
- **`zones`** — UUID-based net restore; proximity threshold widened to 0.5mm (#1857); overlap detection and 4-layer stackup-aware zone assignment (#2042); KiCad 9 name-only net format (#1780)
- **Net-status zone fill connectivity accounting** (#2036)

#### Placement

- **Board origin subtracted from Edge.Cuts in optimize** (#2060)
- **Post-convergence slide-off pass for residual overlaps** (#2103)
- **Courtyard-aware clamping in placement optimizer** (#1956)
- **Iterative multi-pass conflict resolution in `cmd_fix`** (#1953)
- **Force-directed divergence prevention** (#1769) — 1/r² falloff and force clamping
- **Intermediate placement state saved on SIGINT/SIGTERM** (#2037)
- **Pad positions preserved when writing optimized placements** (#2207)

#### Export / Report

- **Markdown report preserved alongside PDF during flattening** (#2162)
- **Blank schematic sheets excluded from report figures** (#1992)
- **Blank schematic detection switched from PNG size to SVG size** (#2029)
- **ERC rendered as SKIPPED when not run** (#1503)
- **Routing status classifies zone-filled and single-pad nets separately** (#1838)
- **Only exported PCB included in project ZIP**
- **Report generator uses `ReportGenerator` class** (#1521)

#### CLI / Misc

- **`.kicad_pro` resolved to `.kicad_pcb`** (#1506) — In `validate --connectivity` and `report generate`
- **Spec path resolution for subdirectory `.kct` files** (#1631)
- **`--min-completion` controls exit code threshold** (#1951)
- **BOM LCSC part numbers preserved across regeneration** (#1606)
- **JLCPCB API 403 circuit breaker** (#1508)
- **JLCPCB 2oz copper min trace/clearance corrected to 6mil** (#1502)
- **`copper_weight` string values like `'2oz'` parsed correctly** (#1596)
- **Progress output flushed during long routing runs** (#1662)
- **`review-schematic` uses pre-computed pin-map data** (#2233) — Instead of LLM coordinate math

### Removed

- **atopile submodule** — No longer referenced by any source code
- **Dead GPU signal integrity kernel** (#1873)
- **Dead `layout/` module** (#1742)
- **4 dead standalone CLI scripts** (#1904)
- **6 dead GPU detection symbols** (#1721)
- **Dead `optimize/` package** (#1861) — Consolidated into `optim/`
- **4 dead functions** (#1741) — Zero callers
- **Dead `SessionExpiredError` and error-mapping code** (#1725)
- **Dead `detect_signal_type` and `assign_layer_preferences` router functions** (#1730)

## [0.12.0] - 2026-04-15

### Summary

Manufacturing-export release: a complete `kct export` package generator with pre-flight
validation, JLCPCB LCSC auto-matching, and an EXPORT pipeline stage — plus substantial
routing and DRC/ERC auto-repair improvements and KiCad 10 compatibility fixes.

### Added

#### Manufacturing Export

- **`kct export`** (#1469) — Manufacturing package generation (Gerber, drill, BOM, placement)
- **Pre-flight validation checklist** (#1475) — Validate a design before manufacturing export
- **BOM–CPL cross-reference check** (#1487) — Catch BOM/placement mismatches before export
- **LCSC auto-match** (#1473) — Auto-match LCSC part numbers during JLCPCB BOM export, with `--auto-lcsc` / `--no-auto-lcsc` flags (#1484)

#### Pipeline

- **EXPORT step** (#1472) — Final pipeline step after REPORT
- **REPORT step** (#1377) — Final pipeline step after AUDIT
- **FIX_ERC step** (#1378) — Automatic ERC remediation, with `fix-erc` command (#1379)
- **fix-silkscreen step** (#1376) — Inserted between ERC and fix-vias
- **DRC/ERC/verdict summary** (#1416) — Printed after pipeline completion
- Raised default `--max-passes` 3 → 20 (#1437) and `--max-displacement` 0.5mm → 2.0mm (#1429)
- ERC errors partitioned into blocking and non-blocking (#1406)

#### Routing & DRC Repair

- **All copper layers routable** including PLANE-type layers (#1474)
- **`--layers 4-all`** — 4-layer all-signal routing (#1466)
- **Local A\* rerouting** for infeasible clearance violations (#1399), with multi-segment cluster rerouting for grouped via violations (#1408)
- **Post-pass connectivity check** with automatic rollback (#1439)
- Handle `clearance_pad_segment` / `clearance_pad_via` violations in fix-drc (#1470)
- Partial-routing UX improvements with actionable suggestions (#1387, #1467)

#### Audit & Report

- **Analog component detection** with advisory audit action item (#1489)
- **Orphaned-footprint detection** — footprints on PCB but not in schematic (#1488)
- Zone connectivity treated as advisory when core checks pass (#1465, #1431)
- Five missing ERC violation types and suggestion handlers (#1385); non-electrical ERC violations classified as warnings (#1386)

### Fixed

- **KiCad 10 compatibility**: version-aware drill origin (#1486); recover `net_number` from PCB header for name-only net format (#1432)
- **fix-drc**: preserve trace terminal endpoints during corner chamfering (#1438); use audit step result for summary verdict instead of DRC-only (#1464)
- **zones**: restore per-element net assignments after zone fill (#1389); stable keys for net restoration under KiCad 10 net format (#1395)
- **export**: capitalize JLCPCB CPL layer values and wire aux-origin auto-detection (#1463)
- **report**: prevent double-nesting of figures path in `_embed_images` (#1384)

## [0.11.0] - 2026-04-12

### Added

#### Placement Optimization (`placement/`)

A complete placement optimization system with multiple strategies, cost evaluation,
and CLI/MCP integration for AI-driven component placement.

- **PlacementVector & Encoding** (#1223) - Define placement search space
  - Encode/decode component positions and rotations as optimization vectors
  - Board-relative coordinate system with boundary constraints

- **Cost Functions** (#1224, #1225, #1219) - Multi-objective placement evaluation
  - HPWL wirelength estimator using transformed pad coordinates
  - Overlap and boundary violation geometry detectors
  - Weighted cost function aggregator for combining objectives

- **Placement Strategies**
  - Force-directed and random seed heuristics (#1226)
  - CMA-ES evolutionary optimizer with `PlacementStrategy` ABC (#1227)
  - Bayesian Optimization strategy using Ax/BoTorch (#1235)

- **DRC Integration** (#1228) - Courtyard and pad spacing clearance checker

- **Evaluation Pipeline** (#1233) - Multi-fidelity evaluation
  - Fast/cheap evaluations for early filtering, full DRC for promising candidates

- **Netlist Graph Analysis** (#1231) - Connectivity-aware placement priors

- **Visualization** (#1230) - Optimization progress plots and convergence tracking

- **Benchmark Boards** (#1229) - Test boards for optimizer validation

- **CLI** (#1234) - `kct optimize-placement` command for CMA-ES optimization

- **MCP Tools** (#1236) - `optimize_placement` and `evaluate_placement` tools

#### Routing Orchestration

Unified multi-strategy routing with hierarchical planning and fine-pitch support.

- **Routing Orchestrator** (#1140, #1194, #1195, #1218) - Coordinate multiple routing strategies
  - Full pipeline strategy with via conflict resolution and clearance repair
  - Wire real router strategies into orchestrator

- **Hierarchical Router** (#1127) - Global routing foundation with channel assignment

- **Adaptive Grid** (#1157) - Fine grid near pads, coarse grid in open channels

- **Fine-Pitch Support**
  - Sub-grid routing for fine-pitch components (#1120)
  - Dog-leg routing for fine-pitch components (#1150)
  - Escape routing for SSOP/TSSOP packages

- **GPU-Accelerated Pathfinding** - Batch pathfinding for fine-grid routing

- **Via Conflict Management** (#1124) - Handle blocked pad access points

- **Automatic Power Plane Stitching** - Via stitching for power planes

- **CLI & MCP** (#1220) - `route-auto` command and MCP tool for orchestrator-based routing

#### DRC & Validation

- **Trace Clearance Repair** (#1121) - Nudge traces to fix DRC violations
- **Fab-Aware Severity** (#1187) - Reclassify DRC severity based on manufacturer capabilities
- **Post-Stitch DRC** (#1188) - `--drc` flag for zone fill and DRC validation after stitching

#### Type System

- **Interval Types** (#1170) - Parametric constraint system with unit-aware arithmetic
- **Typed Interface Ports** (#1184) - Type-checked circuit connections

#### Library & Tooling

- **Unused Symbol Detection** (#1222) - Find unused symbols/footprints in project libraries
- **MCP Client Setup** (#1186) - `kct mcp setup` auto-configures MCP clients
- **CLI Flags** - `--routing-aware` and `--check-routability` in unified CLI
- **C++ Backend Warnings** (#1125) - Improved discoverability and performance guidance
- **CI Pipeline** (#1165) - GitHub Actions CI with pytest, ruff, and mypy

### Changed

- **Command Protocol** (#1126) - Add command protocol for CLI migration
- **Type Consolidation** - Merge duplicate `Severity` and `Layer` enum types

### Removed

- **Dead Code Cleanup**
  - Remove stalled command protocol migration infrastructure (#1240)
  - Remove 4 dead methods from `router/core.py` and `spec/parser.py` (#1237)
  - Remove unused classes and functions from `exceptions.py` (~540 LOC) (#1167)
  - Remove unused `generate_grid_stress_test` function (#1215)
  - Remove 8 unused exports from `__all__` declarations (#1189)

### Fixed

- **Stitch Reliability** - Multiple fixes for via stitching correctness
  - Pad clearance checking to prevent shorts (#1143)
  - Copy `.kicad_pro` alongside PCB output for DRC compatibility (#1144)
  - Trace path clearance to prevent shorts from pad-to-via connections (#1142)
  - Clearance against other-net copper before placing vias (#1119)
  - Pad-to-via trace segments for electrical connectivity (#1116)
  - Remove invalid rotation parameter from via format (#1115)

- **Router Fixes**
  - Correct single-paren removal to fix invalid PCB output (#1118)
  - Cross-check annular ring requirement when sizing vias (#1117)
  - Add `start`/`end` properties to `Segment` class

- **S-Expression Parser** (#1163) - Fix bugs in `SExp.to_string()`
- **Coordinate System** (#1172, #1197) - Resolve test failures from API and coordinate changes
- **Placement Loader** - Convert footprint positions to board-relative on load

## [0.10.3] - 2026-01-24

### Added

#### GPU Acceleration (`acceleration/`)

Automatic GPU acceleration for computationally intensive operations with cross-platform support.

- **Backend Abstraction** (#1023) - Unified interface for GPU operations
  - `BackendType` enum: CUDA, Metal, CPU
  - Automatic backend detection and selection (CUDA > Metal > CPU)
  - Graceful fallback when GPU unavailable

- **GPU Configuration** (#1024) - Integration with PerformanceConfig
  - `[gpu]` section in `~/.config/kicad-tools/performance.toml`
  - Configurable backend, device ID, memory limits
  - Problem-size thresholds for GPU vs CPU selection

- **GPU Benchmarking** (#1025) - Calibrate command GPU support
  - `kicad-tools calibrate --show-gpu` displays GPU capabilities
  - Automatic GPU performance calibration

- **Optional Dependencies** (#1026) - GPU packages as optional extras
  - `uv pip install kicad-tools[cuda]` for NVIDIA GPU support
  - `uv pip install kicad-tools[metal]` for Apple Silicon support

- **GPU-Accelerated Operations**
  - Force-directed placement (#1027) - O(n²) pairwise force calculations
  - Evolutionary fitness evaluation (#1028) - Parallel population evaluation
  - Grid operations (#1029) - Large 3D array operations for routing
  - Signal integrity calculations (#1030) - Parallel trace pair analysis

### Changed

- **MCP Tool Registry** (#1083) - Unified tool registration between stdio and HTTP transports
  - New `tools/registry.py` with `ToolSpec` dataclass
  - Single source of truth for all 28 MCP tools
  - `server.py` reduced from ~1,700 to ~310 lines (82% reduction)

### Fixed

- **Acceleration**: Consolidate duplicate `GPUBackend`/`BackendType` enums (#1080)
  - Single `BackendType` enum as source of truth
  - Removed redundant `GPUBackend` from detection module

## [0.10.2] - 2026-01-18

### Summary

Programmatic PCB-construction release: a full board-editing API surface — trace routing,
copper pours/zones, silkscreen management, footprint import from schematic, and
placement optimization — alongside new schematic netlist-query and ERC capabilities.

### Added

#### PCB API

- **Trace routing API** for programmatic PCB routing (#926)
- **Copper pour / zone API** for ground planes (#927)
- **Silkscreen management APIs** for reference-designator visibility and placement (#931)
- **Manufacturing export API** — Gerber, drill, BOM, placement (#928)
- **Collision detection and DRC-aware placement validation** (#925)
- **`import_from_schematic()`** — programmatically import footprints (#919)
- **Zone-based placement optimization API** (#921)
- **Node insertion methods** for position control (#917)

#### Schematic API

- **Netlist query API** for verifying connectivity (#903)
- **Wire endpoint collision detection** (#904)
- **`run_erc()`** — invoke KiCad ERC programmatically (#906)
- **Power net connectivity validation** (#900)

### Fixed

- **PCB**: `update_footprint_position()` rotation now persists (#922)
- **PCB**: insert at-node after layer node in `add_footprint_from_file`
- **netlist**: parse reference from `(ref ...)` child node in `NetNode.from_sexp()`
- **schematic**: flatten symbols with `extends` for embedding in `lib_symbols` (#896); fix missing `lib_symbols` for complex symbol types (#892, #893)

## [0.10.1] - 2026-01-17

### Fixed

- **Schematic**: Fix `pin_position()` returning inverted Y coordinates (#889)
  - Pins were being vertically swapped due to incorrect Y-axis negation
  - This caused KiCad to crash when opening schematics with wires or no_connect flags at affected pin positions
  - Both symbol definitions and schematics use Y-down coordinates (the code incorrectly assumed symbol defs used Y-up)

## [0.10.0] - 2026-01-16

### Added

#### Design Pattern Library (`patterns/`)

Encode expert PCB design knowledge for agent use. Agents can instantiate validated patterns instead of solving common layouts from first principles.

- **Pattern Schema** (#823) - Foundational schema for PCB patterns with placement rules
  - `PCBPattern` base class for placement-based patterns
  - `IntentPattern` base class for constraint-based patterns
  - `Placement`, `PlacementRule`, `RoutingConstraint` schema types
  - Validation and spec compliance checking

- **Core Patterns** (#824) - Comprehensive library of validated design patterns
  - **Power**: `LDOPattern`, `BuckPattern` with thermal and decoupling placement
  - **Timing**: `CrystalPattern`, `OscillatorPattern` with load capacitor placement
  - **Interface**: `USBPattern`, `I2CPattern`, `SPIPattern`, `UARTPattern`, `EthernetPattern`
  - **Analog**: `ADCInputFilter`, `DACOutputFilter`, `OpAmpCircuit`, `SensorInterface`
  - **Protection**: `ESDProtection`, `OvercurrentProtection`, `OvervoltageProtection`, `ReversePolarityProtection`, `ThermalShutdown`

- **Pattern Validation & Adaptation** (#825)
  - `PatternValidator` checks instantiated patterns meet spec requirements
  - `PatternAdapter` generates pattern parameters for specific components
  - Component requirements database for automatic parameter lookup
  - Validation checks: distance, presence, trace length, value matching

- **User-Defined Patterns** (#826)
  - YAML pattern definitions with `PatternLoader`
  - Pattern definition DSL: `define_pattern()`, `placement_rule()`, `routing_constraint()`
  - `PatternRegistry` for registering and discovering patterns
  - Custom validation checks via `register_check()`

#### Explanation System (`explain/`)

Queryable explanations for design rules with spec references and fix suggestions.

- **Queryable Explanations** (#827)
  - `explain(rule_id, context)` returns contextualized rule explanations
  - `explain_violations(violations)` attaches explanations to DRC results
  - `explain_net_constraints(net)` explains why nets have certain constraints
  - Spec references with document name, section, and URL
  - Auto-generated fix suggestions with calculated deltas

- **Common Mistake Detection** (#828)
  - `MistakeDetector` identifies common PCB design mistakes
  - `detect_mistakes(pcb)` scans design for typical errors
  - Categories: decoupling, thermal, signal integrity, power distribution
  - Each mistake includes explanation and fix suggestions

- **Design Decision Rationale** (#829)
  - `DecisionStore` tracks design decisions with rationale
  - `record_decision()` captures why choices were made
  - `PlacementRationale`, `RoutingRationale` for structured tracking
  - `explain_placement()`, `explain_route()` retrieve decision context
  - Persistent storage for decision history

#### Multi-Resolution Abstraction (`design/`)

High-level operations that decompose into low-level commands automatically.

- **Multi-Resolution API** (#830)
  - **High-level**: `design.add_subsystem("power_supply", components=[...], near_edge="left")`
  - **Medium-level**: `optimizer.group_components(refs, strategy="power_supply")`
  - **Low-level**: `session.apply_move("U_REG", x=10, y=50)` (existing API)
  - Automatic decomposition of high-level commands
  - Consistent results across abstraction levels

- **Subsystem Types** (`design/subsystems.py`)
  - `POWER_SUPPLY`, `MCU_CORE`, `CONNECTOR`, `TIMING`, `ANALOG_INPUT`, `INTERFACE`
  - Optimization goals: thermal, routing, compact, signal integrity, mechanical
  - Built-in placement hints and typical component lists

- **Command Decomposition** (`design/decomposition.py`)
  - Breaks high-level operations into atomic moves
  - Pattern-aware placement strategies
  - Constraint propagation from subsystem to component level

### Changed

- **MCP Types Refactored** (#857) - Split `mcp/types.py` into domain-specific modules
  - `types/assembly.py`, `types/board.py`, `types/clearance.py`, `types/drc.py`
  - `types/gerber.py`, `types/intent.py`, `types/placement.py`, `types/routing.py`
  - `types/session.py`, `types/warnings.py`, `types/drc_delta.py`
  - Improved maintainability and reduced file size

## [0.9.3] - 2026-01-06

### Fixed

- **Pad/Via/Obstacle Clearance** (#587) - Include `trace_width/2` in clearance zone calculations
  - Pathfinder checks trace centers, but grid marking must account for trace edges
  - Fixes DRC violations where traces were placed too close to pads/vias
  - Affects `_add_pad_unsafe()`, `_add_pad_vectorized_unsafe()`, `_mark_via()`, `add_obstacle()`

- **build-native CMake Path** (#586) - Use `cpp/` directory CMakeLists.txt for pip-installed packages
  - Fixes `kct build-native` failing when installed via pip
  - Now correctly locates CMakeLists.txt in package installation directory

## [0.9.2] - 2026-01-06

### Added

#### Parallel Processing Infrastructure

- **Thread-Safe Grid** (`router/grid.py`) (#584)
  - Optional `thread_safe=True` parameter for `RoutingGrid`
  - RLock-based synchronization for concurrent grid access
  - `locked()` context manager for atomic multi-operation sequences
  - Zero overhead when disabled (default)

- **Parallel Routing Operations**
  - Parallelize Monte Carlo routing trials (#576)
  - Parallel fitness evaluation in evolutionary optimizer (#578)
  - Parallelize placement conflict detection (#577)
  - Parallelize congestion grid processing (#582)

- **C++ Backend Integration** (`router/core.py`) (#581)
  - Autorouter class now automatically uses C++ backend when available
  - Seamless fallback to Python implementation

#### New Commands

- **`kct build-native`** (`cli/__init__.py`) (#580)
  - Install C++ backend for ~100x performance boost
  - Automatic compilation with nanobind
  - `kct build-native --check` to verify installation

#### Configurable Units

- **Unit System** (`units.py`) (#574)
  - Configurable output units: millimeters (default) or mils
  - `kct config units mm|mils` to set preference
  - Affects all CLI output (congestion, spacing, dimensions)

### Fixed

- **mfr compare --layers** (#568) - Fix `--layers` flag being ignored in manufacturer comparison

## [0.9.1] - 2026-01-06

### Added

#### Router Performance Optimization (Phase 4)

- **C++/nanobind Core** (`router/cpp/`)
  - High-performance C++ implementation of core routing operations
  - nanobind bindings for Python integration
  - ~100x speedup potential for A* neighbor evaluation
  - ~100x speedup potential for route marking operations
  - Graceful fallback when C++ module is not built

- **Algorithm Improvements** (`router/pathfinder.py`)
  - Optimized grid operations for JLCPCB's 0.0635mm grid constraints
  - NumPy-based grid for improved performance
  - Benchmarking infrastructure with `--profile` flag (#554)

#### New Commands

- **`kct init` Command** (`cli/init_cmd.py`)
  - Initialize projects with manufacturer-specific design rules
  - Support for all registered manufacturers

- **Parts Suggest CLI** - Exposed parts suggestion feature as command-line tool

#### Manufacturers

- **FlashPCB** (`manufacturers/flashpcb.py`, `manufacturers/data/flashpcb.yaml`)
  - USA-based PCB fabrication and assembly house
  - 2 and 4 layer boards with 1oz/2oz copper
  - 5 mil trace/space, 8 mil minimum drill
  - 10" × 10" max board size
  - 2-sided assembly (down to 0201)
  - 3, 5, and 10 day lead times

### Fixed

- **Trace Blocking Radius** (#553) - Include clearance in trace blocking radius calculation for DRC compliance
- **fp_text Reference Format** (#565, #547) - Support `fp_text` reference format in placement fix apply
- **mfr export-dru FileNotFoundError** (#550) - Fix FileNotFoundError when running outside project
- **Trace Deletion** (#555) - Delete traces from sexp.children instead of sexp.values
- **Test Cleanup** (#557) - Remove unused pytest import in cpp_backend tests

### Changed

- **Version Management** - `__version__` now automatically reads from `pyproject.toml` via `importlib.metadata`

## [0.9.0] - 2026-01-06

### Added

#### Design Intent System (`kicad_tools.intent`)

Declare high-level design intent and automatically derive constraints:

- **Interface Declarations** (`intent/types.py`, `intent/constraints.py`)
  - `IntentDeclaration` - Declare design intent for net groups
  - `Constraint` - Auto-derived constraints from interface specs
  - `create_intent_declaration()` - Create declarations with automatic constraint derivation
  - `validate_intent()` - Validate declarations against design

- **Built-in Interface Specifications** (`intent/interfaces/`)
  - `USB2HighSpeedSpec`, `USB2FullSpeedSpec`, `USB3Spec` - USB with impedance/length matching
  - `SPISpec` - SPI bus with clock/data timing constraints
  - `I2CSpec` - I2C with pull-up and capacitance requirements
  - `PowerRailSpec` - Power rails with decoupling and current requirements

- **Interface Registry** (`intent/registry.py`)
  - `REGISTRY` - Global registry of interface types
  - Extensible for custom interface definitions
  - Auto-registration of built-in specs

- **MCP Integration** (`mcp/tools/intent.py`)
  - `declare_interface` - Declare interface intent via MCP
  - `list_interfaces` - List available interface types
  - `get_intent_status` - Check constraint satisfaction

#### Continuous Validation (`kicad_tools.drc`)

Real-time DRC during placement sessions:

- **Incremental DRC Engine** (`drc/incremental.py`)
  - `IncrementalDRC` - Efficient DRC with cached state
  - `SpatialIndex` - R-tree spatial indexing for O(log n) region queries
  - `check_move()` - Preview DRC impact without applying
  - `apply_move()` - Apply move and update cached state
  - Performance: <10ms incremental checks for 200+ components

- **DRC Delta in Responses** (`drc/incremental.py`)
  - `DRCDelta` - New vs resolved violations after changes
  - `Violation` - Rich violation details with location and items
  - Integrated into MCP `query_move` and `apply_move` responses

- **Predictive Warnings** (`drc/predictive.py`)
  - `PredictiveAnalyzer` - Anticipate problems before they occur
  - Routing difficulty estimation based on placement
  - Congestion analysis for component density
  - Intent risk checking for declared interfaces
  - Confidence-scored warnings with suggestions

#### Intelligent Failure Recovery (`kicad_tools.router`)

Root cause analysis and resolution strategies for failures:

- **Failure Analysis** (`router/failure_analysis.py`)
  - `RootCauseAnalyzer` - Determine why operations failed
  - `FailureCause` enum - CONGESTION, BLOCKED_PATH, CLEARANCE, etc.
  - `BlockingElement` - Identify what's blocking desired operations
  - `CongestionMap` - Grid-based congestion heatmap

- **Resolution Strategies** (`router/resolution.py`)
  - `ResolutionStrategy` - Actionable fix with difficulty rating
  - Multiple strategies per failure with trade-off analysis
  - Strategy types: MOVE_COMPONENT, ADD_VIA, REROUTE_NET, USE_LAYER
  - Difficulty estimation: EASY, MEDIUM, HARD

#### Context Persistence (`kicad_tools.mcp.context`)

Maintain design context across MCP sessions:

- **Decision Tracking** (`mcp/context.py`)
  - `Decision` - Record design decisions with rationale
  - `DecisionOutcome` - Track success/failure of decisions
  - Decision history for learning and explanation

- **Session Context** (`mcp/context.py`)
  - `SessionContext` - Extended session state
  - `AgentPreferences` - Learned preferences from behavior
  - `StateSnapshot` - Efficient state checkpoints

- **State Summaries** (`mcp/context.py`)
  - Compact state encoding for reduced token overhead
  - Incremental updates instead of full state
  - Queryable decision history

### Changed

- MCP session tools now return DRC delta in `query_move` and `apply_move` responses
- Placement sessions integrate with incremental DRC for real-time validation

### Dependencies

- `rtree>=1.0` - R-tree spatial indexing (optional, falls back to linear scan)

## [0.8.0] - 2026-01-05

### Added

#### MCP Server for AI Agent Integration (`kicad_tools.mcp`)

FastMCP-based server enabling AI agents like Claude to interact with KiCad designs:

- **Core Infrastructure** (`mcp/server.py`)
  - FastMCP server implementation with stdio and HTTP transports
  - Comprehensive error handling with actionable MCP responses
  - Session management for stateful operations
  - CLI: `kct mcp serve` (stdio), `kct mcp serve --http` (HTTP transport)

- **Analysis Tools** (`mcp/tools/analysis.py`)
  - `analyze_board` - Get board summary (layers, components, nets, dimensions)
  - `get_drc_violations` - Run DRC and return violations with locations
  - `measure_clearance` - Check clearance between components/nets

- **Export Tools** (`mcp/tools/export.py`)
  - `export_gerbers` - Generate Gerber files for manufacturing
  - `export_bom` - Generate bill of materials in various formats
  - `export_assembly` - Generate complete manufacturing package (BOM + pick-and-place)

- **Placement Tools** (`mcp/tools/placement.py`)
  - `placement_analyze` - Analyze current placement quality with metrics
  - `placement_suggestions` - Get AI-friendly placement recommendations

- **Session Tools** (`mcp/tools/session.py`)
  - `start_session` - Begin a placement refinement session
  - `query_move` - Preview effect of moving a component
  - `apply_move` - Apply a component move within session
  - `commit` - Commit session changes to file
  - `rollback` - Discard session changes

- **Routing Tools** (`mcp/tools/routing.py`)
  - `route_net` - Route a specific net with configurable strategy
  - `get_unrouted_nets` - List nets that need routing

#### Layout Preservation System (`kicad_tools.layout`)

Preserve component placement and routing when regenerating PCB from schematic:

- **Hierarchical Address Matching** - Match components by hierarchical path (e.g., `power.ldo.C1`)
- **Anchor-Based Positioning** - Calculate subcircuit offsets from anchor components
- **Net Remapping** - Handle net name changes during regeneration
- **Incremental Updates** - Only touch changed components, preserve manual adjustments

#### BOM Command Enhancements

- **Availability Checking** (`--check-availability` flag)
  - Check LCSC/JLCPCB stock availability for BOM parts
  - `--quantity` flag to specify board count (multiplies quantities)
  - Exit code 2 when parts are unavailable
  - CLI: `kicad-bom design.kicad_sch --check-availability --quantity 5`

- **JLCPCB Assembly Validation** (#510)
  - Validate BOM compatibility with JLCPCB assembly service
  - Check for missing LCSC part numbers
  - Verify part availability and assembly category
  - CLI: `kicad-bom design.kicad_sch --validate-jlcpcb`

#### Documentation

- **MCP Server Setup Guide** - Configuration for Claude Desktop and other MCP clients
- **Example Workflows** - End-to-end agent-driven PCB design examples

### Dependencies

- `fastmcp>=2.0,<3` - MCP server framework (optional, in `[mcp]` extra)
- `pydantic>=2.0` - Request/response validation (optional, in `[mcp]` extra)

## [0.7.2] - 2026-01-04

### Added

- `--format json` flag for `kct placement optimize` command (#449)

### Fixed

- Handle empty reference in BOM `grouped()` to prevent IndexError (#452)
- Net-status now correctly detects pad-to-zone connectivity (#451)
- Estimate cost reads components from PCB footprints (#448)

## [0.7.1] - 2026-01-04

### Added

- `--layers` option to autorouter for multi-layer board support (#426)
- Positional argument support to `validate --sync` command (#422)
- Auto-detect target layer from zones in stitch command (#417)
- `name` property to PadState for state export (#430)
- `graphic_items` property to PCB schema (#427)
- pyyaml added to required dependencies (#419)

### Fixed

- Reason agent reporting incorrect board size and layer count (#425)
- Parts availability command silent exit (#424)
- Net-status via-to-zone connectivity detection (#418)
- Router `--grid` and `--clearance` parameters being ignored (#423)

## [0.7.0] - 2026-01-04

### Added

#### Rich Error Diagnostics (`kicad_tools.exceptions`)

Compiler-style error reporting with actionable context:

- **Source Position Tracking** (`SourcePosition`)
  - File, line, and column tracking for all errors
  - Element type and board coordinates for PCB errors
  - Layer information for multi-layer issues

- **S-expression Snippet Extraction** (`SExpSnippetExtractor`)
  - Extract code context around error locations
  - Line numbers and visual markers
  - Complete element extraction by reference

- **Error Accumulation** (`ErrorAccumulator`)
  - Collect multiple errors instead of failing on first
  - Batch validation for comprehensive feedback
  - `ValidationErrorGroup` for aggregated reporting

- **Rich Terminal Rendering**
  - Syntax-highlighted error output
  - Color-coded severity levels
  - Visual separators for multi-error reports

#### Actionable Feedback (`kicad_tools.drc`, `kicad_tools.analysis`, `kicad_tools.constraints`)

Transform error codes into specific fix suggestions:

- **DRC Fix Suggestions** (`drc/suggestions.py`)
  - "Move C1 0.5mm left to clear U1 pad"
  - Specific component and direction recommendations
  - Clearance violation resolution strategies

- **ERC Root Cause Analysis** (`cli/erc_explain_cmd.py`)
  - Deep analysis of electrical rule violations
  - Root cause identification
  - Step-by-step fix instructions
  - CLI: `kct erc explain <schematic>`

- **Routing Congestion Analysis** (`analysis/congestion.py`)
  - Grid-based density hotspot detection
  - Track length and via count per area
  - Unrouted connection identification
  - Severity classification (LOW → CRITICAL)
  - CLI: `kct analyze congestion <pcb>`

- **Constraint Conflict Detection** (`constraints/conflict.py`)
  - Detects keepout/grouping/region conflicts
  - Conflict types: OVERLAP, CONTRADICTION, IMPOSSIBLE
  - Multiple resolution options with trade-off analysis
  - CLI: `kct constraints check <pcb>`

#### Design Quality Metrics (`kicad_tools.analysis`)

Proactive design quality analysis:

- **Trace Length Reports** (`analysis/trace_length.py`)
  - Per-net and per-segment length calculation
  - Automatic timing-critical net detection (CLK, USB, DDR, LVDS)
  - Differential pair skew calculation
  - Layer change tracking
  - Tolerance checking (actual vs target)
  - CLI: `kct analyze trace-lengths <pcb>`

- **Thermal Analysis** (`analysis/thermal.py`)
  - Heat source identification (regulators, MOSFETs, drivers)
  - Power dissipation estimation by package type
  - Thermal resistance calculations
  - Nearby heat source clustering
  - Copper area and via effectiveness estimation
  - Temperature rise prediction
  - CLI: `kct analyze thermal <pcb>`

- **Signal Integrity Estimates** (`analysis/signal_integrity.py`)
  - Crosstalk risk detection between adjacent traces
  - Impedance discontinuity analysis (width changes, vias, layer transitions)
  - High-speed net identification (USB, LVDS, MIPI, HDMI, DDR, Ethernet)
  - Coupling coefficient calculation
  - Risk level classification (LOW → HIGH)
  - CLI: `kct analyze signal-integrity <pcb>`

#### Cross-Domain Validation (`kicad_tools.validate`)

Consistency checks across design artifacts:

- **Schematic↔PCB Consistency** (`validate/consistency.py`)
  - Component matching between schematic and PCB
  - Net consistency verification
  - Reference designator, value, and footprint sync
  - CLI: `kct validate --consistency`

- **Net Connectivity Validation** (`validate/connectivity.py`)
  - Unrouted net detection
  - Partial connection (island) detection
  - Isolated pad identification
  - Actionable fix suggestions
  - CLI: `kct validate --connectivity`, `kct net-status <pcb>`

- **BOM↔Placement Verification**
  - Component count verification
  - Placement status for all BOM items
  - Missing component detection

#### Cost Awareness (`kicad_tools.cost`)

Manufacturing cost visibility:

- **Manufacturing Cost Estimation** (`cost/estimator.py`)
  - PCB fabrication cost breakdown
  - Component and assembly costs
  - Quantity-based pricing tiers
  - Manufacturer-specific costs (JLCPCB, PCBWay, OSHPark, Seeed)
  - Surface finish and color adjustments
  - Layer and thickness premiums
  - CLI: `kct estimate cost <pcb>`

- **Part Availability Checking** (`cost/availability.py`)
  - LCSC stock level queries
  - Availability status (AVAILABLE, LOW_STOCK, OUT_OF_STOCK, DISCONTINUED)
  - Lead time reporting
  - Minimum order quantity handling
  - Price break calculations
  - CLI: `kct parts availability <schematic>`

- **Alternative Part Suggestions** (`cost/alternatives.py`)
  - Suggest replacements for unavailable parts
  - Price difference comparison
  - Pin-compatible alternatives
  - Basic part preferences for JLCPCB assembly
  - CLI: `kct suggest alternatives <schematic>`

#### CLI Commands

New commands for v0.7.0 features:

- `kct erc explain <file>` - ERC root cause analysis with fix suggestions
- `kct analyze congestion <pcb>` - Routing congestion hotspots
- `kct analyze trace-lengths <pcb>` - Timing-critical trace analysis
- `kct analyze thermal <pcb>` - Thermal hotspot detection
- `kct analyze signal-integrity <pcb>` - Crosstalk and impedance analysis
- `kct constraints check <pcb>` - Constraint conflict detection
- `kct validate --consistency` - Schematic↔PCB sync check
- `kct validate --connectivity` - Net connectivity validation
- `kct estimate cost <pcb>` - Manufacturing cost estimation
- `kct parts availability <schematic>` - LCSC stock checking
- `kct suggest alternatives <schematic>` - Alternative part suggestions

## [0.6.0] - 2026-01-03

### Added

#### Intelligent Placement Engine (`kicad_tools.optim`)

Comprehensive placement optimization for PCB component positioning:

- **Functional Clustering** (`optim/clustering.py`)
  - `ClusterDetector` - Detects related component groups
  - `detect_functional_clusters()` - Find MCU+bypass caps, timing circuits, etc.
  - `ClusterType` enum: POWER, TIMING, INTERFACE, ANALOG

- **Thermal-Aware Placement** (`optim/thermal.py`)
  - `ThermalClass` - Heat source, heat sensitive, neutral classification
  - `classify_thermal_properties()` - Auto-detect thermal components
  - `detect_thermal_constraints()` - Generate separation constraints
  - Heat sources pushed to edges, separated from sensitive components

- **Signal Integrity Hints** (`optim/signal_integrity.py`)
  - `SignalClass` - CLOCK, HIGH_SPEED, DIFFERENTIAL, ANALOG, POWER, GENERAL
  - `classify_nets()` - Auto-classify nets by name patterns
  - `analyze_placement_for_si()` - Get SI warnings
  - `get_si_score()` - Placement quality metric

- **Edge Placement** (`optim/edge_placement.py`)
  - `detect_edge_components()` - Find connectors, mounting holes
  - `EdgeConstraint` - Keep components at board edges
  - `BoardEdges` - Edge detection and constraint generation

- **Keep-out Zones** (`optim/keepout.py`)
  - `KeepoutZone` - Define no-go areas
  - `create_keepout_from_component()` - Auto-generate from components
  - `load_keepout_zones_from_yaml()` - Load from config file
  - `validate_keepout_violations()` - Check placement against zones

#### Placement Constraints (`kicad_tools.optim`)

Declarative constraint system for component placement:

- **Component Grouping** (`optim/constraints.py`)
  - `GroupingConstraint` - Keep components together
  - `SpatialConstraint` - Position constraints
  - `validate_grouping_constraints()` - Check constraint satisfaction

- **Alignment** (`optim/alignment.py`)
  - `snap_to_grid()` - Grid alignment
  - `align_components()` - Row/column alignment
  - `distribute_components()` - Even spacing
  - `AlignmentConstraint` - Declarative alignment rules

#### Agent Integration (`kicad_tools.optim`)

AI-friendly APIs for placement optimization:

- **Placement Suggestions** (`optim/suggestions.py`)
  - `PlacementSuggestion` - Suggested position with rationale
  - `generate_placement_suggestions()` - Get improvement ideas
  - `explain_placement()` - Why is component here?
  - `suggest_improvement()` - Specific move suggestions

- **Iterative Refinement** (`optim/session.py`, `optim/query.py`)
  - `PlacementSession` - Stateful refinement session
  - `query_position()` - "What if I move X here?"
  - `query_swap()` - "What if I swap X and Y?"
  - `find_best_position()` - Optimal position search
  - `process_json_request()` - JSON API for agents

#### CLI Commands

New placement optimization commands:

- `kicad-tools placement optimize --cluster` - Enable clustering
- `kicad-tools placement optimize --thermal` - Enable thermal awareness
- `kicad-tools placement optimize --edge-detect` - Edge component detection
- `kicad-tools placement optimize --keepout FILE` - Load keepout zones
- `kicad-tools placement suggest` - Get suggestions
- `kicad-tools placement refine` - Interactive refinement

## [0.5.0] - 2026-01-02

### Added

#### Circuit Blocks (`kicad_tools.schematic.blocks`)

Reusable, tested circuit blocks for common schematic patterns:

- **MCUBlock** - Microcontroller with configurable bypass capacitors
- **CrystalOscillator** - Crystal/oscillator with load capacitors
- **USBConnector** - USB-B/Mini/Micro/Type-C with optional ESD protection
- **DebugHeader** - SWD, JTAG, and Tag-Connect programming headers
- **I2CPullups** - I2C bus pull-up resistors with optional filtering capacitors
- **ResetButton** - Reset switch with debounce capacitor and optional ESD protection
- **BarrelJackInput** - DC barrel jack with reverse polarity protection
- **USBPowerInput** - USB power input with fuse and ESD protection
- **BatteryInput** - Battery connector with protection circuitry
- **LDOBlock** - Linear regulator with input/output capacitors
- **LEDIndicator** - Status LED with current-limiting resistor
- **DecouplingCaps** - Decoupling capacitor placement helper

All blocks feature:
- Ports for inter-block wiring
- Configurable component values
- Optional protection circuits
- Factory functions for common configurations

#### Schematic Enhancements

- **Auto-layout** (`schematic.layout`)
  - Automatic symbol placement to avoid overlaps
  - Configurable spacing and alignment options

- **Netlist Sync Validation** (`validate.netlist`)
  - Compare schematic netlist to PCB netlist
  - Detect missing/extra components and nets
  - CLI: `kct validate-sync schematic.kicad_sch pcb.kicad_pcb`

#### API Refinements

- **Unified Project Class** (`kicad_tools.Project`)
  - Load complete KiCad projects (`.kicad_pro`)
  - Cross-reference schematics to PCBs
  - Find unplaced components
  - Export manufacturing packages

- **Actionable Error Messages** (`kicad_tools.exceptions`)
  - All exceptions include `error_code` field
  - Structured `to_dict()` for JSON serialization
  - Fix suggestions included in error messages

- **Progress Callbacks** (`kicad_tools.progress`)
  - `ProgressCallback` protocol for monitoring long operations
  - `ProgressContext` context manager for scoped progress
  - `create_json_callback()` for automation
  - Cancelable operations via callback return value
  - Integration with router, DRC, and export operations

#### Examples & Documentation

- **End-to-End Example** (`examples/05-end-to-end/`)
  - Complete workflow from schematic to manufacturing files
  - Demonstrates circuit blocks, routing, and export

- **Agent Integration Examples** (`examples/agent-integration/`)
  - Claude integration with tool definitions and prompts
  - OpenAI integration with function calling
  - Common utilities for error handling and API wrapping

### Changed

- Improved exception hierarchy with structured error information
- Router operations now support progress callbacks
- DRC checker supports progress reporting

## [0.4.0] - 2025-12-31

### Added

#### Library Management

- **Symbol Library Tools** (`kicad_tools.library`)
  - Create and save KiCad symbol libraries programmatically
  - Symbol creation with pin and property editing
  - Round-trip editing preserves existing content

- **Footprint Library Tools** (`kicad_tools.library`)
  - `FootprintLibrary` class for loading `.pretty` directories
  - Footprint creation and save support
  - Parametric footprint generators for common package types:
    - SOIC (8, 14, 16 pins)
    - QFP (32, 44, 48, 64, 100 pins)
    - QFN (16, 24, 32, 48 pins)
    - DFN (6, 8, 10, 12 pins)
    - BGA (grid-based ball patterns)
    - Chip resistors/capacitors (0402, 0603, 0805, 1206)
    - SOT variants (SOT-23, SOT-223, SOT-89)
    - Through-hole (DIP, SIP, pin headers)
  - CLI: `kct footprint generate <type>` for parametric generation

#### Pure Python DRC (no kicad-cli required)

- **DRCChecker** (`kicad_tools.validate`) - standalone design rule checking
  - Clearance checks: trace-to-trace, trace-to-pad, pad-to-pad, via-to-trace
  - Dimension checks: trace width, via drill, annular ring
  - Edge clearance checks: copper-to-board-edge minimum
  - Silkscreen checks: line width, text height, over-pad detection
  - Manufacturer rule presets for JLCPCB, PCBWay, OSHPark, Seeed
  - Support for 2-layer, 4-layer, and 6-layer configurations
  - CLI: `kct check board.kicad_pcb --mfr jlcpcb`

#### Datasheet Tools

- **Datasheet Infrastructure** (`kicad_tools.datasheet`)
  - Datasheet search across multiple sources
  - PDF download with local caching
  - PDF to markdown conversion using MarkItDown
  - Image extraction from PDF datasheets
  - Pin table extraction from datasheets
  - Package dimension extraction and footprint matching
  - Symbol generation from datasheet pin tables
  - End-to-end part import workflow
  - CLI: `kct datasheet search/download/convert/extract-images/extract-tables`

#### CLI Enhancements

- **Netlist analysis commands**: `kct netlist analyze/list/show/check/compare/export`
- **Footprint generation**: `kct footprint generate --list` shows available types

### Changed

- Manufacturer rule presets expanded to support 2, 4, and 6-layer configurations
- DRC checking can now run without kicad-cli installed

### Fixed

- Test suite compatibility with pure Python DRC checker

## [0.3.0] - 2025-12-31

### Added

#### Documentation & Examples

- **Comprehensive API documentation** with type hints for mypy --strict compliance
- **User guide and tutorials** in `docs/` directory
- **Example projects** demonstrating common workflows:
  - Schematic analysis
  - BOM generation
  - DRC checking
  - Autorouting
  - LLM-driven routing

#### CLI Enhancements

- **Progress indicators** for long-running operations with real-time feedback
- **Configuration file support** (`.kicad-tools.yaml`) for CLI default options
- **Interactive REPL mode** (`kct repl`) for multi-step workflows
- **Interactive routing preview** for visualizing routing decisions

#### Router Enhancements

- **Differential pair routing** with length matching support
- **Bus routing** for grouped signal routing
- **45° diagonal routing** for shorter, cleaner traces
- **Zone-aware routing** with flood fill and thermal relief generation
- **Staircase pattern compression** - optimizes alternating horizontal/diagonal micro-segments into clean paths

#### LLM Integration

- **PCB Reasoning Agent** (`kicad_tools.reasoning`) for LLM-driven layout decisions
  - State representation suitable for LLM prompts
  - Command vocabulary for routing actions
  - Feedback/diagnosis for failed operations
  - CLI: `kct reason` with `--analyze`, `--export-state`, `--interactive` modes

#### File Format Support

- **`.kicad_mod`** footprint library files
- **`.kicad_dru`** design rules files
- **KiCad 7+** net class handling compatibility

#### Footprint Validation

- **Standard library comparison** - validate footprints against KiCad's official library
  - Auto-detects KiCad library path (macOS, Linux, Windows)
  - Compares pad positions, sizes, and shapes with configurable tolerance
  - CLI: `kct footprint compare-standard`

#### Testing & Quality

- **Edge case test coverage** for parser and schema modules
- **Integration tests** with real KiCad project files
- **Performance benchmarks** for large board handling

### Changed

- **S-expression parser optimized** for 50%+ performance improvement on large files
- **README updated** to highlight agent-focused development goal
- **Custom exception hierarchy** with context and actionable suggestions

### Fixed

- Pad obstacle clearance calculation for PTH routing
- All linting and formatting issues

### Removed

- Unused numpy dependency (router now uses pure Python)
- Hardcoded project paths from CLI modules
- Orphaned CLI modules consolidated into unified CLI
- Duplicate S-expression implementations

## [0.2.0] - 2025-12-30

### Added

#### Manufacturing Readiness (Planned Features)

- **LCSC Parts Integration** (`kicad_tools.parts`)
  - `LCSCClient` for direct part lookups from JLCPCB's LCSC database
  - Part search with filtering by stock, category, and specifications
  - Local caching for offline use and reduced API calls
  - CLI: `kct parts lookup`, `kct parts search`, `kct parts cache`

- **Assembly Package Export** (`kicad_tools.export`)
  - Complete manufacturing packages (Gerbers + BOM + CPL) for fabrication
  - Multi-manufacturer format support (JLCPCB, PCBWay, OSHPark, Seeed)
  - CLI: `kct export assembly`, `kct export gerbers`, `kct export pnp`

- **Fluent Query API** (`kicad_tools.query`)
  - Django-ORM style filtering for symbols and footprints
  - `sch.symbols.filter(value="100nF")`, `pcb.footprints.by_reference("U1")`
  - Chainable filters with field lookups

- **Project Class** (`kicad_tools.project`)
  - Cross-reference schematics to PCBs
  - Unified project-level queries for finding unplaced components

#### Bonus Features (Beyond Roadmap)

- **A* Autorouter with Obstacle Awareness** (`kicad_tools.router`)
  - Intelligent obstacle detection and avoidance
  - Net-class aware routing strategies (power, clock, audio, digital)
  - Multi-layer support with automatic via placement
  - Negotiated and greedy routing strategies
  - `CommandInterpreter` for high-level routing commands
  - CLI: `kct route`

- **Trace Optimizer** (`kicad_tools.optim`)
  - Post-routing trace optimization
  - Length matching support for differential pairs

- **Manufacturer DRC Configuration**
  - Configurable design rules per manufacturer
  - Rule comparison tools between manufacturers
  - CLI: `kct mfr compare`

- **Footprint Validation & Repair** (`kicad_tools.footprints`)
  - Detect pad spacing issues, overlaps, courtyard violations
  - Automatic repair with configurable minimum gaps
  - CLI: `kct validate-footprints`, `kct fix-footprints`

- **Placement Conflict Detection** (`kicad_tools.optim`)
  - Detect component overlaps and courtyard violations
  - Conflict resolution suggestions

### Changed

- CLI version now dynamically reads from package metadata instead of hardcoded value

### Fixed

- Test for `LCSCClient.close()` method now patches at correct module level

## [0.1.0] - 2025-12-29

### Added

- **Core S-expression parser** with round-trip editing support
- **Schematic parsing** - symbols, wires, labels, hierarchy traversal
- **PCB parsing** - footprints, nets, traces, vias, zones
- **Symbol library parsing** - read and query KiCad symbol libraries
- **ERC report parsing** - parse KiCad Electrical Rules Check reports
- **DRC report parsing** - parse KiCad Design Rules Check reports
- **Manufacturer profiles** - design rules for JLCPCB, OSHPark, PCBWay, Seeed
- **PCB autorouter** - A* pathfinding with pluggable heuristics
  - Net class awareness (power, clock, audio, digital)
  - Multi-layer support with via management
  - Congestion-aware routing
- **Unified CLI** (`kct` or `kicad-tools`) with subcommands:
  - `kct symbols` - list symbols in schematics
  - `kct nets` - trace and analyze nets
  - `kct bom` - generate bill of materials
  - `kct erc` - run/parse ERC reports
  - `kct drc` - run/parse DRC reports with manufacturer rules
- **PCB tools** - `kicad-pcb-query` and `kicad-pcb-modify`
- **Library tools** - `kicad-lib-symbols`
- JSON output for all CLI commands

### Dependencies

- Python 3.10+
- numpy >= 1.20

[0.15.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.15.0
[0.14.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.14.0
[0.9.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.9.0
[0.8.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.8.0
[0.7.2]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.2
[0.7.1]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.1
[0.7.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.7.0
[0.6.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.6.0
[0.5.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.5.0
[0.4.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.4.0
[0.3.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.3.0
[0.2.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.2.0
[0.1.0]: https://github.com/rjwalters/kicad-tools/releases/tag/v0.1.0
