# OmniLayout / OmniRouting recon (2026-08-15)

*Point-in-time working document — issue [#4830](https://github.com/rjwalters/kicad-tools/issues/4830),
slice 3 of 4. Describes omnieda.com as of 2026-08-15.*

## Verdict: **adapt the protocol, drop the data**

| Question | Answer |
|----------|--------|
| Adopt the dataset as a corpus? | **No** — no licence grant of any kind, and the public drop is 5 hand-picked boards behind Google Drive links. |
| Adopt the tooling? | **No** — the only code released is a matplotlib board renderer; the evaluation harness that computes the leaderboard metrics is not published. |
| Adapt anything? | **Yes** — the *metric vocabulary and evaluation protocol*, which is the sharpest external definition of "routed well" we have found, and which we can implement against our own boards and the (properly licensed) open-schematics corpus at zero licence risk. |
| Build a JSON → `.kicad_pcb` converter? | **Not now.** It is technically feasible (gap list below is short and bounded), but there is nothing licensed to convert. Re-evaluate on the trigger below. |

**Re-evaluation trigger.** Both papers promise a release ("We will open-source
all benchmark data, evaluation code, and tool interfaces" — OmniRouting
abstract). If the 1,681-design set lands with an explicit data licence — on
Hugging Face, versioned — this recon should be re-run: the converter work is
estimated at days, not weeks, and the reference routings would be the first
human-authored copper this repo has ever measured against.

---

## 1. What was actually measured

Everything below was downloaded on **2026-08-15** and characterized locally.
Nothing from these archives is committed to this repository.

| Artifact | Source | Bytes | SHA-256 |
|----------|--------|-------|---------|
| OmniLayout sample data (5 JSON boards) | Drive `1Lz6Cbjd4ea78fUHPVNIP4rX3yvrIahEs` | 76,029 | `6b360b4ffffa9f78f5d1a1e441ae31907eef4c85d2261c908ca4baf8977fc77d` |
| OmniLayout visualization code | Drive `1B12M7Gutv7xn9l956G8caaWkmyMJ0Gb3` | 26,166 | `5a7eac5f45fe88c29dd1a9501274c970099d6d9531f0785dd945b7cace2aa000` |
| OmniRouting sample data (`five_boards.zip`) | Drive `1ECPGYEQNGtf1Y6FA5otz73DyYL_N20Rb` | 49,031,553 | `31f69023f5557af319b90cc36d0d5116f962809cb3485d29f5542fca453fc81e` |
| OmniRouting visualization code | Drive `1FtoFcszuL10ngoBfbJ0b2aBfy9Yt_lto` | 19,252 | `8cd6ae756e80ab2dd8fed5f5f7dc3b0a0664ab0559dfca760a9312283753d63b` |

Reproduce (the Drive links are interactive; the `confirm=t` form bypasses the
virus-scan interstitial for the large archive — **download to scratch space,
never into the repo**):

```bash
SCRATCH=$(mktemp -d)
curl -sSL "https://drive.google.com/uc?export=download&id=1Lz6Cbjd4ea78fUHPVNIP4rX3yvrIahEs" \
  -o "$SCRATCH/omnilayout-data.zip"
curl -sSL "https://drive.usercontent.google.com/download?id=1ECPGYEQNGtf1Y6FA5otz73DyYL_N20Rb&export=download&confirm=t" \
  -o "$SCRATCH/omnirouting-data.zip"
unzip -q "$SCRATCH/omnilayout-data.zip" -d "$SCRATCH/layout"
unzip -q "$SCRATCH/omnirouting-data.zip" -d "$SCRATCH/routing"
chmod -R u+rwX "$SCRATCH/routing"   # the zip ships mode-0444 directories

uv run python scripts/corpus/omnieda_sample.py --sample "$SCRATCH/layout"
uv run python scripts/corpus/omnieda_sample.py \
  --sample "$SCRATCH/routing/five_boards/source_conversion/graph" --out "$SCRATCH/audit.json"
```

Two gotchas worth recording: the "visualization code" downloads are named
`.zip` by Drive but are plain `.py` files, and the OmniRouting archive uses
Windows path separators and read-only directory modes.

## 2. What OmniLayout is

Two sibling LLM benchmarks published from the same 1,681-design collection by
Lu et al. (Penn State / SJTU / Microsoft Research / Binghamton), hosted at
<https://www.omnieda.com/>:

- **OmniLayout** — *placement* reasoning. [arXiv:2607.03261v2](https://arxiv.org/abs/2607.03261)
  (3 Jul 2026, ACL ARR 2026). 1,681 schematic-coupled layouts, 77.24K placement
  instances; four tasks (geometric IC placement, routability-aware placement,
  electrical functionality, agentic tool use).
- **OmniRouting** — *routing* reasoning. [arXiv:2608.04434v1](https://arxiv.org/abs/2608.04434)
  (5 Aug 2026). Same 1,681 designs, stated as 2–8 routing layers, 109.9K
  components, 245.4K pads/SMDs, 219.8K annotated nets, with engineer-authored
  reference routings.

The site's `arXiv` / `Code` / `Data` / `Hugging Face` buttons are dead
(`href="#"`); the *only* live artifacts are the four Drive files above. A
Hugging Face API search for `omnilayout` / `omnirouting` returns nothing, and no
public GitHub repository exists — the OmniRouting manifest even records its
pipeline by local path (`D:\github\Omnirouting\...`).

**These are not KiCad designs.** Every record is a JSON transcription of an
Eagle 9.6.2 `.brd`: Eagle layer numbers (1 Top, 16 Bottom, 19 Unrouted, 20
Dimension), Eagle rotation strings (`R90`, `MR270`), Eagle `contactref`
netlists, Eagle arc "curve" bulges, Eagle DRU parameter names (`mdWireVia`,
`rvViaOuter`). The five sample boards are public open hardware: Adafruit
FunHouse, Adafruit HalloWing M4, SparkFun Qwiic Ultrasonic (TCT40), Tiny BLE
v1.0, Seeed WM1302 LoRa concentrator.

### The two drops are not the same data

The OmniRouting drop is a **strictly richer re-export of the same five boards**
and is the one to look at if this is ever revisited. Beyond adding reference
copper it back-fills geometry the OmniLayout drop omits — e.g. FunHouse pad
`U3/43_10` is `{drill: 0.4}` in the OmniLayout record and
`{drill: 0.4, diameter: 0.908, diameter_source: "drc_restring"}` in the
OmniRouting one. All 12 diameter-less through-hole pads on that board are
resolved in the routing export.

## 3. Schema vs KiCad-native

Measured with `scripts/corpus/omnieda_sample.py` (offline; ships with this PR).

**OmniLayout drop** — placement only, no copper:

| Board | Components | Nets | Pads |
|-------|-----------:|-----:|-----:|
| Adafruit FunHouse | 88 | 61 | 332 |
| Adafruit HalloWing M4 | 78 | 81 | 370 |
| SparkFun Ultrasonic TCT40 | 62 | 40 | 166 |
| Tiny BLE v1.0 | 122 | 123 | 389 |
| Seeed WM1302 915 SPI | 226 | 242 | 697 |

**OmniRouting drop** — same boards plus reference copper:

| Board | Copper wires | Vias | Pours | Copper layers | Min clearance (mm) |
|-------|-------------:|-----:|------:|---------------|-------------------:|
| Adafruit FunHouse | 857 | 124 | 2 | 1, 16 | 0.2032 |
| Adafruit HalloWing M4 | 1287 | 157 | 2 | 1, 16 | 0.2032 |
| SparkFun Ultrasonic TCT40 | 411 | 73 | 3 | 1, 16 | 0.1778 |
| Tiny BLE v1.0 | 690 | 146 | 5 | 1, 15, 16 | 0.1524 |
| Seeed WM1302 915 SPI | 1857 | 747 | 7 | 1, 15, 16 | 0.0889 |

Field-level mapping onto `kicad_tools.schema.pcb`:

| OmniEDA field | KiCad-native counterpart | Convertible? |
|---------------|--------------------------|--------------|
| `ic_library[].smd/pads/holes` | `Footprint` + `Pad` | Yes — rect/round SMD and drilled pads all fit `Pad(shape=…)` |
| `ic_position[]` (`position`, `rotation` `R*`/`MR*`) | `Footprint.position` / rotation / layer flip | Yes — `MR*` is a back-side mirror |
| `netlist[].contactref` | `Net` + pad net assignment | Yes |
| `board_boundary.segment_details` (+ `curve`) | `EdgeContour` / `GraphicArc` | Yes — Eagle bulge → arc |
| `routing_wires[]` on layers 1..16 | `Segment` | Yes, **except** curved wires (see gaps) |
| `routing_vias[]` (`extent: "1-16"`) | `Via` | Yes |
| `copper_pours[]` (Eagle polygon) | `Zone` | Partly — `rank`/`isolate`/`thermals` have no 1:1 KiCad meaning |
| `Clearance.rules`, `board_semantics.designrules` | `Setup` / manufacturer rules | Partly — Eagle DRU names need a translation table |
| `board_semantics.classes` | net classes | Yes |
| `board_semantics.layers` (134–184 entries) | `StackupLayer` | Needs synthesis; only 2 of 16 copper layers are populated on 3/5 boards |
| *schematic* | `Schematic` | **Absent** — see gaps |

### Mappability gap census (pooled over the 5 routing-drop boards)

| Gap | Count | Boards | Consequence |
|-----|------:|-------:|-------------|
| `component-missing-rotation` | 121 | 5/5 | `ic_library` entry has no rotation; converter must fall back to `ic_position` |
| `airwire-counted-as-routing` | 61 | 1/5 | Eagle layer-19 "Unrouted" stubs shipped inside `routing_wires` |
| `placement-without-footprint-geometry` | 32 | 5/5 | element is placed but has no library entry — no pads to emit |
| `curved-copper-segment` | 25 | 1/5 | `schema.pcb` models copper as `(segment …)` only; needs polyline approximation |
| `copper-pour-polygon` | 19 | 5/5 | Eagle polygon → KiCad `Zone`, parameters lossy |
| `no-schematic-payload` | 5 | 5/5 | no schematic in *any* record, despite "schematic-coupled" |
| `inner-copper-layer` | 2 | 2/5 | >2-layer stackup must be synthesized |
| `through-hole-pad-without-diameter` | 16 (OmniLayout drop only) | 2/5 | annular ring must be inferred from the DRU |

Three of these are more than converter chores:

1. **The airwires make the "ground truth" not fully routed.** The SparkFun
   board ships 61 GND wires on Eagle layer 19 (*Unrouted* — ratsnest stubs with
   `width: 0.0`), and its `board_semantics.errors.approved` list carries matching
   `19,…` approved-error hashes. The GND net is pour-connected, not
   trace-connected. Any route-vs-human harness that treats `routing_wires`
   as copper would both inflate wire counts and mis-score net completion.
2. **"Schematic-coupled" is a property of the paper, not of the release.** No
   record in either drop carries a schematic, so the LVS / net-status
   validation-breadth motivation in #4830 gets nothing from this data.
3. **The declared layer count is not the populated layer count.** Tiny BLE and
   WM1302 declare layers 1, 2, 15, 16 active, but no reference copper *or* pour
   appears on layer 2 in the export. Either those boards route on 1/15/16, or
   the export dropped a plane. Unresolved — and it must be resolved before any
   layer-count-sensitive metric derived from this data is trusted.

## 4. Licence: none granted

| Asset | Licence found | Evidence |
|-------|---------------|----------|
| omnieda.com site | none | no licence/terms text anywhere in either page's source |
| Sample data archives | none | no `LICENSE`/`COPYING` in either zip (5 JSONs; 89 files) |
| Visualization scripts | none | no header, no licence string |
| Both papers | **CC BY-NC-ND 4.0** | arXiv licence link on 2607.03261 and 2608.04434 |
| Underlying boards | third-party, non-SPDX | e.g. `adafruit/Adafruit-FunHouse-PCB` and `sparkfun/SparkFun_Ultrasonic_Distance_Sensor-Qwiic` are `NOASSERTION` (custom hardware licences, typically CC BY-SA with attribution/share-alike duties) |

The only *stated* licence covers the papers and is both **non-commercial** and
**no-derivatives** — the two properties a benchmark corpus most needs to not
have. The data itself carries no grant at all, and the redistribution does not
surface the upstream hardware licences it is derived from. That is
disqualifying for vendoring, for a committed pointer manifest, and for
publishing any derived board artifact.

Contrast with what slice 2 already landed: `bshada/open-schematics` is
**CC-BY-4.0**, KiCad-native, versioned on Hugging Face, and pinned by
`sha256` in `scripts/corpus/manifests/open-schematics-sample.json`. For every
purpose #4830 lists — parser hardening, routability ground truth,
placement/router benchmarking, LVS breadth — it is the better corpus and it is
already wired up.

## 5. The part worth keeping: the metric protocol

OmniRouting's metric vocabulary is well specified and close to ours. Mapping it
onto `BenchmarkResult` (see [Routing Benchmark Suite](../guides/benchmarking.md)):

| OmniEDA metric | Definition | kicad-tools today |
|----------------|------------|-------------------|
| NRR | % of required nets connected **and** free of DRC violations | partial — `nets_fully_routed / nets_total` does **not** require DRC-clean |
| PRR | % of DRC-clean pad-to-pad connections | missing — we score at net granularity only |
| Open / PShort / NShort | % of nets affected by opens / physical shorts / logical shorts | partial — `kct net-status` and LVS decide this per net, but not as a rate split |
| Total / Clr. / Dist. | mean violation counts, split by cause | partial — `drc_violations` is a single board-level `error_count` |
| Polygon | % of outputs carrying a copper pour | available via zones; not reported |
| TWL | routed centerline length | `total_length_mm` |
| #Vias / #Layers | via and layer counts | `total_vias`; layers not reported |
| PR | fraction of inputs producing a loadable output | n/a for a deterministic router |
| RT | runtime | `routing_time_sec` |
| OO / OoB / HPWL / NC / NS (placement) | overlap area, out-of-bounds count, half-perimeter wirelength, crossings, net separation | HPWL yes (`optim`); overlap/OoB enforced but not *scored*; NC/NS missing |

The three worth adopting, in order, all of which are generic library capability
and none of which need their data:

1. **DRC-attributed net completion (NRR).** Today a net counts as routed if its
   pads share a connected component; a net routed *through* a clearance
   violation still scores. Attributing DRC errors to nets and reporting a
   clean-completion rate is a strictly better regression signal.
2. **Pad-pair routability (PRR).** Net-level completion hides partial progress
   on high-fanout nets; a pad-pair denominator makes a 40-pad GND net comparable
   to a 2-pad signal.
3. **Violation split by cause (Clr. / Dist.) and the open/physical-short/
   logical-short split.** We have all the underlying checks; we report a scalar.

## 6. Recommended next steps

1. **Do not** add OmniEDA data to `scripts/corpus/manifests/` or any fetch path.
   Slice 4's capacity-predictor calibration and route-vs-human harness should be
   built on the open-schematics manifest instead — it is CC-BY-4.0, KiCad-native,
   and each record already pairs a schematic with a human-completed board.
2. **File the metric work as its own issue(s)** against the benchmark suite:
   DRC-attributed NRR, pad-pair PRR, and the violation-cause split, citing this
   document for the external definitions. These are the durable outcome of this
   recon.
3. **Keep `scripts/corpus/omnieda_sample.py`.** It is the re-evaluation
   instrument: point it at any future drop and the gap census answers "has the
   blocking data actually appeared?" in one run.
4. **Watch for the promised open-source release.** Concretely: a Hugging Face
   dataset under an explicit licence, or a GitHub repo with the evaluation
   harness. On either, re-run this recon; the converter scope is the gap table
   in §3 (footprint synthesis, Eagle rotation/mirror semantics, arc bulges,
   pours, stackup) and the airwire filter is a hard prerequisite.
5. **If a converter is ever built**, put it in `scripts/corpus/` as a dev tool,
   not in the installed package — this repo emits KiCad; an Eagle-JSON reader is
   research scaffolding, not product surface.

## Attribution

- T. Lu, K. Lin, M. Wang, et al., "OmniLayout: A Schematic-Coupled Multimodal
  Benchmark for Constraint-Aware Geometric Reasoning in PCB Layout,"
  arXiv:2607.03261, 2026.
- T. Lu, K. Lin, Z. Dong, et al., "OmniRouting: A Semantic-Coupled Multimodal
  Benchmark for Constraint-Aware Spatial Reasoning in PCB Routing,"
  arXiv:2608.04434, 2026.

Sample boards are third-party open hardware (Adafruit, SparkFun, Seeed); their
own hardware licences govern any use of the underlying designs.
