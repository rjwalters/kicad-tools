# Demo manufacturing readiness — 2026-09-09

This audit used kicad-tools 0.20.0 with the local fixes described below and
KiCad 10.0.6. Reports are stored in each board's `output/readiness/`;
`output/readiness.json` binds the result to SHA256 hashes of source, rules,
procurement specifications, evidence and manufacturing artifacts. The gallery
must invalidate the result if any of those inputs change.

## Current disposition

| Board | Disposition | Remaining work |
|---|---|---|
| 00 Simple LED | Assembly ready | D1 and J1 are manual through-hole assembly after SMT. |
| 01 Voltage divider | Assembly ready | J1 and J2 are manual through-hole assembly after SMT. |
| 02 Charlieplex LED | Assembly ready | Replaced with a real ATtiny85 circuit; see the updated board readiness report and HARDWARE.md. |
| 03 USB joystick | Assembly ready | Real 8 MHz ATmega32U4 HID circuit; explicit four-layer filled/capped via process and manual J1 assembly. |
| 04 STM32 dev board | Assembly ready | Correct MCP1825S regulator, eight repaired via positions, explicit paid 0.15 mm mechanical-drill option; J1/Y1 manual assembly. |
| 05 BLDC controller | Assembly ready | Real DRV8313 controller; six manual parts, compiled firmware, conservative documented motor bring-up limits. |
| 06 Four-channel LVDS demonstrator | Assembly ready | Real SN65LVDS1/2 circuits; corrected outer-layer routing and drill positions, three manual Samtec headers and external regulated 3.3 V. |
| 07 SDR SDRAM demonstrator | Assembly ready | Real STM32F429/IS42S16400J, six-layer JLC06161H-2116A, 40 SMT plus three manual headers; firmware and reviewed timing/load evidence supplied. |

All numbered demo boards 00–07 passed manufacturer checks, including ERC/LVS/manifest checks,
and independent native KiCad DRC after zone refill. Their per-rule warning
breakdowns are empty in both engines: all assembly-affecting warning counts
are zero. No warning acknowledgements or rule waivers were added.

Each ready board has a regenerated BOM, SMT-only CPL, Gerber/drill archive,
KiCad project ZIP, schematic PDF, front/back assembly PDFs, procurement review,
check reports, assembly instructions and a full SHA256 manifest. The complete
package is `output/manufacturing.zip`; its contents match the checked
`output/manufacturing/` directory. Project ZIP PCB/schematic contents were
compared byte-for-byte with current source files. Part identity corrections and
supplier sources are recorded in `manufacturing/procurement-review.md` and
[issue #4971](https://github.com/rjwalters/kicad-tools/issues/4971).

All eight complete packages pass the current release gates. Manufacturing
readiness does not imply that an assembled unit has been tested: firmware
programming and the documented bench bring-up remain outstanding. The synthetic
06/07 fixtures are archived separately and never supply assembly readiness.

Board07's final archive contains 63 files and its readiness record binds 143
source/evidence/artifact hashes. The independently refilled PCB, project and
custom rules match the released sources byte for byte. Its firmware occupies
1,276 flash bytes and tests all 8 MiB at 48 MHz SDCLK. Follow the 5 V ±5%,
0–30 °C and 230 mA continuous bench envelope in its assembly instructions.

## Repair experiments

The archived synthetic board 07's J1 orientation experiment and reproduction commands are in
[`astra-j1-orientation.md`](../boards/07-matchgroup-test/diagnostic-runs/astra-j1-orientation.md).
Correcting the authored differential-pair geometry, routing with cache disabled,
and fixing small-deficit length tuning allows the MIPI block to satisfy its
full declared electrical constraints. This does not complete the DDR/HDMI
blocks. That historical fixture remains separate from the real SDRAM assembly and
its current readiness evidence.

On a temporary zone-refilled copy of board 05, stitching added 57 vias and
reduced native unconnected items from 57 to 29 without other error-level
findings, but introduced 30 dangling-via warnings. That incomplete experiment
was not promoted. Evidence was added to the existing
[board-05 issue #4410](https://github.com/rjwalters/kicad-tools/issues/4410).

## Tool findings

| Issue | Finding | This workspace |
|---|---|---|
| [#4966](https://github.com/rjwalters/kicad-tools/issues/4966) | Footprint rotation leaves absolute pad angles stale | Fixed with save/reload regression tests. |
| [#4967](https://github.com/rjwalters/kicad-tools/issues/4967) | Router progress counts empty/partial route entries as completed | Filed. |
| [#4968](https://github.com/rjwalters/kicad-tools/issues/4968) | Placement proposer cannot suggest 90-degree endpoint alignment | Filed with measured A/B evidence. |
| [#4969](https://github.com/rjwalters/kicad-tools/issues/4969) | Board-07 authored pair geometry and emitted sidecar disagree | Canonical geometry corrected. |
| [#4970](https://github.com/rjwalters/kicad-tools/issues/4970) | Gallery Ready and manufacturing downloads rely on stale/misleading artifacts | Fresh hash-bound readiness and correctly labeled complete/Gerber/source downloads implemented. |
| [#4971](https://github.com/rjwalters/kicad-tools/issues/4971) | Incorrect component identities/packages in demo BOMs | Verified source corrections applied to 00/01/02/04; remaining redesign/procurement blockers recorded. |
| [#4972](https://github.com/rjwalters/kicad-tools/issues/4972) | Route cache ignores effective recipe and net-class sidecar contents | Cache identity now includes both; regression tests pass. |
| [#4973](https://github.com/rjwalters/kicad-tools/issues/4973) | Stitch JSON hides native DRC verdict behind operation success | Filed. |
| [#4974](https://github.com/rjwalters/kicad-tools/issues/4974) | Missing footprint attributes let THT parts into SMT-only CPL | Shared electrical-pad classification and preflight fixed. |
| [#4975](https://github.com/rjwalters/kicad-tools/issues/4975) | Length tuner overshoots small deficits with a fixed amplitude | Deficit-scaled tuning and regressions implemented. |
| [#4976](https://github.com/rjwalters/kicad-tools/issues/4976) | Label-LVS loses KiCad 10 name-only net bindings after native save | Fixed with real-syntax and vacuity regressions; native evidence retained independently. |
| [#4977](https://github.com/rjwalters/kicad-tools/issues/4977) | No scriptable runner for the complete readiness/tapeout contract | Filed; current evidence was produced by explicit orchestration. |
| [#4982](https://github.com/rjwalters/kicad-tools/issues/4982) | Copper-LVS suppresses real opens on any zone-owning power net | Filed; independent native DRC prevents false Ready. |
| [#4984](https://github.com/rjwalters/kicad-tools/issues/4984) | Symmetric group tuning produces unequal P/N splice spans | Filed with an executable reproduction. |

## Reverification

For each board, use its recorded fab target and current schematic. Run
`kct check` with a JSON report, then independent `kicad-cli pcb drc
--refill-zones` on a copy with matching project/rule files. Parse both error
and unconnected-item arrays; native process exit zero alone is not a pass.
Review warning counts by rule and procurement/package compatibility before
export. Regenerate drawings and the full manifest, compare ZIP source contents,
and emit new readiness evidence following `docs/board-json-schema.md`.

Finally run `kct board-metrics --all` and build the site. The data producer and
site loader both verify report hashes; a copied stale report cannot restore
Ready. The private external board remains excluded from staging and routes.

## Assembled redesign follow-up

The user approved real-component circuit redesigns for boards 02, 03, 06 and 07.
Board 02 now uses an ATtiny85 with ISP and compiled scanning firmware. Board 03
uses the actual 44-pin ATmega32U4 USB circuit. Board 06 replaces synthetic
protocol endpoints with real LVDS links; board 07 uses STM32F429 and SDR SDRAM
for an electrically meaningful match-group demonstration. The latter designs
remain under validation until their current readiness reports pass.

Additional tool findings: [#4988](https://github.com/rjwalters/kicad-tools/issues/4988)
(main CLI lacks no-auto-pour), [#4989](https://github.com/rjwalters/kicad-tools/issues/4989)
(joined silkscreen corners falsely overlap; fixed with regressions),
[#4991](https://github.com/rjwalters/kicad-tools/issues/4991) (passive escape vias),
and [#4992](https://github.com/rjwalters/kicad-tools/issues/4992)
(round-pad bounding boxes falsely short to planes; actual-shape fix).

Further findings from the assembled redesigns:

| Issue | Finding | Disposition |
|---|---|---|
| [#4990](https://github.com/rjwalters/kicad-tools/issues/4990) | BOM-excluded symbols counted as missing supplier parts | Filed. |
| [#4993](https://github.com/rjwalters/kicad-tools/issues/4993) | Invalid motor-demo charge-pump/power connections and power footprints | Replaced by validated DRV8313 revision B with compiled firmware. |
| [#4994](https://github.com/rjwalters/kicad-tools/issues/4994) | Named JLC3313 preset contains incompatible 7628 construction | Filed; demos specify verified factory stackups explicitly. |
| [#4995](https://github.com/rjwalters/kicad-tools/issues/4995) | BOM auto-enrichment substitutes generic LCSC parts for explicit non-LCSC MPNs | Filed; board06 uses fresh exports with auto-matching disabled and a separate manual-assembly BOM. |
| [#4996](https://github.com/rjwalters/kicad-tools/issues/4996) | Floating-point rounding rejects exact-minimum PTH annular rings | Fixed with regression coverage. |
| [#4997](https://github.com/rjwalters/kicad-tools/issues/4997) | Bottom microstrip reads exterior paste instead of inward dielectric | Fixed with physics regressions. |
| [#4998](https://github.com/rjwalters/kicad-tools/issues/4998) | Manufacturer profiles omit layer-dependent PTH annular-ring limits | Separate PTH floors and native rules implemented; completed boards rechecked. |

Board07 also reproduced existing [#4978](https://github.com/rjwalters/kicad-tools/issues/4978): a four-line Edge.Cuts boundary silently gets a 65 × 56 mm routing domain. Its generator now emits the same physical rectangle as `gr_rect`; no duplicate issue was created or physical boundary relaxed.

Board06 reproduces clean native DRC, ERC, label/copper LVS and pair checks from source. Its assembled-hardware and historical compatibility tests pass (97 tests). Hardware operation still needs bench testing; readiness records fabrication/assembly review and software verification.

[#4999](https://github.com/rjwalters/kicad-tools/issues/4999) exposed a critical
native-rule export bug: unsupported `solder_mask_margin` syntax causes KiCad 10
to ignore the entire custom-rule file. Removing it also revealed incorrect
hole-clearance mappings disguised as hole-to-edge and solder-mask-dam checks.
The exporter now emits supported, correctly scoped rules; Python mask checks
remain active. Boards 00, 01, 04 and 06 were rechecked after regenerating these
rules: native DRC has zero violations and zero unconnected items on each.
Their source archives, evidence, manifests and readiness hashes were refreshed.

- [#5000](https://github.com/rjwalters/kicad-tools/issues/5000): board03's USB HID
  firmware builds and its binary descriptor is verified. The initial identity
  blocker is resolved with the permitted shared joystick pair 16C0:27DC and
  domain-prefixed serial; suspend/current bench qualification remains pending.
- [#5001](https://github.com/rjwalters/kicad-tools/issues/5001): inner-plane
  stitching serialized standard vias with partial layer spans. Standard barrels
  now span F.Cu/B.Cu while retaining the selected contact plane; explicit
  micro-via spans are preserved. All 299 stitch-command tests pass.

The main CLI gap in #4988 is also fixed locally: explicit `--auto-pour` and
`--no-auto-pour` choices survive the outer parser and dispatcher. Its 11-test
forwarding suite passes. Stitch #5001 additionally passed 65 related tests and
a real board03 dry-run: all 19 VCC placements retain target In2.Cu and use
F.Cu/B.Cu physical spans.

[#5002](https://github.com/rjwalters/kicad-tools/issues/5002) explains a board07
routing failure: preparation ignored the authored 0.0994 mm dielectric and
resized 0.160 mm signals to 0.375 mm using a generic stackup. The fixed path
uses the explicit PCB construction; an isolated failed link now routes after
preparation. Seventeen focused stackup-bridge and board07 tests pass. Full-board
routing and timing checks remain required before promotion.

Two further board07 routing defects have reproducible issues:

- [#5004](https://github.com/rjwalters/kicad-tools/issues/5004): fine-pitch
  same-component clearance exceptions silently bypass authored pad clearance.
  An explicit strict-clearance mode is implemented and used by the real demo;
  the compatibility default remains a follow-up. A fresh multi-net checkpoint
  has zero native violations, versus 46 with the former behavior, but still
  has unrouted connections and is not manufacturing-ready.
- [#5005](https://github.com/rjwalters/kicad-tools/issues/5005): off-grid pad-edge
  search seeds ignore trace radius and repeatedly produce invalid escape tails.
  Strict mode now insets the seed/goal region. The actual failed DQ1 link routes
  in approximately 0.6 seconds after the fix. Combined focused coverage for
  the clearance/seed changes passes 176 tests (three existing skips).

The final exported boards 00, 01, 04 and 06 now use the native-refilled canonical
copper. A second independent refill produces zero filled-polygon symmetric
area difference on every poured layer. Source archives match those canonical
boards. This strengthens the orchestration requirement tracked in #4977:
final rules must precede canonical refill, and export must use that checked
copper rather than an older cached fill.

[#5007](https://github.com/rjwalters/kicad-tools/issues/5007) tracks false
`zone_fill_disabled` / `zone_no_net` warnings for legitimate copper-pour
keepout rule areas. The checker now excludes parsed keepouts; a mixed-zone
regression preserves both warnings on a genuinely unfinished ordinary pour.
All 50 zone-fill and isolated-copper tests pass.

Board03's supply review changed the MCU crystal and firmware clock to 8 MHz:
16 MHz was not guaranteed at the low end of USB bus voltage after losses.
The rebuilt HID firmware retains the 48 MHz USB PLL and 125 kHz ADC clock,
uses a 3.4 V nominal brownout threshold, and passes compiled descriptor checks.

[#5006](https://github.com/rjwalters/kicad-tools/issues/5006) records the missing
validated per-board fabrication override contract: native constraint emission
overwrites board03's reviewed 0.45 mm PTH hole spacing with a generic 0.50 mm
floor. The board recipe restores its documented factory-supported setting
before native validation; the shared tool still needs a consistent override
mechanism for both Python and native checks.

The final factory-process audit reopened board04: its generic tier1 profile
accepted seven SMT via-in-pad drills without an explicit eligible POFV order.
[#5009](https://github.com/rjwalters/kicad-tools/issues/5009) tracks the missing
process eligibility/ordering contract. Board04 now passes after all eight overlapping drills were moved off SMT lands and connectivity was independently rechecked. Its 0.15 mm drills use the documented extra-cost small-via option for two-layer manufacture; no filled/capped via process is needed.

Board05 has completed source regeneration, native DRC/ERC, manufacturer checks,
LVS, exact-part procurement, firmware, drawings and package integrity checks.
Its 42-part revision B has 36 SMT placements and six manual parts; conservative
motor bring-up limits and unperformed physical/thermal tests are documented.
[#5008](https://github.com/rjwalters/kicad-tools/issues/5008) tracks missing-model
diagnostics discovered when its native render omitted two IC bodies.


The final physical audit produced additional reproducible findings:

| Issue | Finding | Disposition |
|---|---|---|
| [#5011](https://github.com/rjwalters/kicad-tools/issues/5011) | Board04 schematic and PCB agreed on the wrong AMS1117 pinout | Replaced with the correctly pinned real MCP1825S-3302E/DB; source, procurement and package revalidated. |
| [#5012](https://github.com/rjwalters/kicad-tools/issues/5012) | Via-in-pad DRC missed partial drill/SMT-land overlaps and some pad angles | True-outline intersection fixed; 124 relevant tests pass. Repaired 11 board02 and 14 board05 holes with clean source replays; board06 has four verified source repairs. |
| [#5013](https://github.com/rjwalters/kicad-tools/issues/5013) | Router can serialize standard vias with partial physical spans | Filed with board07 evidence; ordinary manufacture requires F.Cu-to-B.Cu barrels. |
| [#5014](https://github.com/rjwalters/kicad-tools/issues/5014) | Declared reference planes remain routable and acquire signal tracks | Filed with board06/07 evidence. Board06 source now applies hard inner-layer exclusions. Board07 is being routed with explicit six-layer plane reservations. |

Board04's final package contains 15 SMT placements and two manually assembled
through-hole parts. Native DRC/ERC, Python manufacturer checks and 54-pad LVS
all pass with zero findings; independent refills reproduce both copper layers.
Boards02/05 were fully re-exported after their drill repairs, including drawings,
firmware, renders and source ZIPs; all package and readiness hashes were verified.

Board06 now has a fresh complete package from the corrected source replay: 188 signal segments confined to F.Cu/B.Cu, standard through-vias, zero SMT drill overlaps, native DRC/ERC and 108-pad LVS passing. All four LVDS pair checks pass, and a second refill has zero copper-area difference on both reference planes.


[#5016](https://github.com/rjwalters/kicad-tools/issues/5016) exposed an incorrect
asymmetric-stripline impedance calculation. A shared boundary-element geometry
solver now handles finite copper thickness and both reference planes. Three
independent finite-volume extrapolations agree within 0.001 ohm; exact centered
thin-strip references and physical limiting cases are also tested. The focused
physics/impedance suite passes 190 tests. Reproducible evidence and model limits
are in [the investigation](investigations/stripline-5016/README.md).


Two related stackup defects are fixed and separately tracked:
[#5017](https://github.com/rjwalters/kicad-tools/issues/5017) dropped native
`addsublayer` dielectric strata, and [#5018](https://github.com/rjwalters/kicad-tools/issues/5018)
treated intervening signal copper as a reference plane. Native power-layer
roles and all dielectric sublayers now survive extraction. Board07's inner
signal geometry resolves to 0.13/1.078 mm reference-plane gaps; 150 focused
stackup/physics/ampacity tests pass. Its uniform 0.18 mm traces calculate to
51.72 ohm on the outer layers and 48.09 ohm on the inner layers using the
documented effective-dielectric approximation, within the 45–55 ohm target.

The final gallery summary also uses verified structured readiness metrics when
custom manufacturing reports omit the old Markdown table. Boards03/04 now
consistently show Ready, zero DRC findings and 100% routing in both producer and
site display logic; this completes another part of #4970 without weakening hash
verification. The site builds locally and passes 57 tests plus Astro checking.
Publishing is pending access to the Cloudflare account required by the existing
deployment script's account guard.


A final serialized-via audit of ready boards00–06 found only standard physical
F.Cu-to-B.Cu spans: 0/2/28/151/29/136/63 vias respectively. No microvia or
blind/buried designation remains in those released sources. The need to bind
validator implementation identity to future readiness runs is also recorded in
#4977: source/package hashes alone cannot detect a checker semantic change.


[#5019](https://github.com/rjwalters/kicad-tools/issues/5019) was reproduced while
moving board07's decouplers to the back: `pcb-modify flip` changes the footprint
side but leaves its pads on front copper, and cannot find modern Reference
properties. The CLI now shares the independently tested recovery mirror implementation;
123 CLI, native-golden and legacy-format tests pass. The redesign also uses
that verified transform. The intermediate placement candidate preserved all 212 then-present
bindings and nonpower fanout geometry. The final design adds two interplane
capacitors and has 216 bindings, with routing and silkscreen cleanup completed.

[#5020](https://github.com/rjwalters/kicad-tools/issues/5020) records the missing
per-driver external load budgets. STM32's SDRAM clock characterization uses
15 pF, while data/address use 30 pF. Board07 must include receiver capacitance
and the complete through-via barrel in its load screening. The documented
1 pF per-via engineering reserve is approximate; it does not turn typical MCU
pin capacitance into a guaranteed maximum or establish hardware qualification.

Board07's reviewed six-layer physical source is now reproducible through
`real_design/build_source.py`. It verifies immutable fixture hashes and compares
all 43 package identities and 216 connected pads with fresh electrical
generation before producing a draft source. Bus routing, timing and final
manufacturing validation remain separate release gates.

[#5021](https://github.com/rjwalters/kicad-tools/issues/5021) tracks reusable,
symmetric clock-to-signal spacing for tracks, pads and through vias. Board07
exposed a route-order dependency when a clock respected three-width spacing
to tracks but only manufacturing clearance to a foreign via. The local router
now applies the same electrical spacing to vias; general tool support remains
open. The final candidate completes all four auxiliary routes and every bus
connection, with zero native errors or opens.

[#5022](https://github.com/rjwalters/kicad-tools/issues/5022) records synthetic
CI recipe/output confusion introduced by keeping assembled demos beside legacy
routing fixtures. Coverage gates now explicitly route into an isolated folder,
select that PCB and its adjacent sidecar, and retain the existing synthetic
baselines only for that fixture. Board07's old default output is also isolated.
The original synthetic PCB/schematic/sidecar remain archived for regression
coverage. Focused isolation/sidecar tests pass 44 cases; legacy match-group and
diff-pair gate tests pass another 102 cases.


[#5023](https://github.com/rjwalters/kicad-tools/issues/5023) exposed export/check
relaxing reviewed native clearances to factory capability minima. Both project
settings and overriding custom DRU rules must preserve the reviewed values;
otherwise the exported planes change on refill even when DRC passes. The
persistent `KCT_PRESERVE_BOARD_RULES=1` opt-in now retains stricter minima,
netclass clearances and explicit severities. Factory requirements can still
raise a lower authored floor. Legacy stock-default relaxation is unchanged.
All 31 export/native-rule tests pass. The final board07 copper is byte-identical
through a second independent native refill under the emitted rules.

The board07 electrical review includes actual via travel in length matching,
separate 15 pF clock and 30 pF data/address load screens, receiver-load reserves,
and symmetric clock track/via spacing. The final minimum non-pad clock gap is
0.5792 mm against a 0.54 mm target. Short package escapes and native neighboring
TSOP pad geometry are explicitly reviewed in `real_design/engineering/`; the
model screens do not imply IBIS corner qualification or tested hardware.

The final rule-preservation fix also covers `kct check --emit-dru`, which had a
separate renderer bypassing project preservation. Both emission paths now share
the same renderer; board07 additionally verifies project/DRU byte identity
through its entire validation sequence. Fixes remain local and their GitHub
issues remain open until integrated.

Final verification on the frozen release passes all 44 focused export/check/
native-rule tests. The gallery builds 11 pages and passes 57 tests; all eight
numbered demo pages show Assembly ready. Both public and built download copies
match each checked manufacturing archive byte for byte. Production publication
remains blocked by the existing Cloudflare account guard; no deployment was
performed to the mismatched account.

## Production publication — 2026-09-10

Published the verified gallery using the chezmoi-managed scoped personal Pages
credentials after checking the unchanged account identity pin and authenticated
project access. Deployment: https://98014430.kicad-tools.pages.dev . All eight
numbered pages at https://kicad-tools.org show Assembly ready; every live
manufacturing ZIP matches its local checked archive SHA256. This resolves the
publication blocker recorded above.

[#5027](https://github.com/rjwalters/kicad-tools/issues/5027) records the guard's
incorrect reliance on account information from `wrangler whoami` when using a
limited Pages token. The local fix supports explicit account verification plus
authenticated Pages access; three regression cases pass. Credentials remain
outside the repository, and the issue stays open until integration.
