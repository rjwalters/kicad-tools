# Placement scoring: four surfaces, three metrics

kicad-tools exposes several commands that report a "score" or "overlap/DRC"
verdict for a component placement. Historically these produced numbers that
disagreed on the *same* layout, which made it impossible for a user to tell
whether a placement was actually good. This document explains what each
surface measures, why they differ, and which one to trust for which purpose.

> Context: [issue #3940](https://github.com/rjwalters/kicad-tools/issues/3940).
> The most severe defect — `optimize-placement --dry-run` scoring a freshly
> generated force-directed *seed* instead of the actual on-disk placement —
> is fixed. The remaining differences below are **intentional** and are
> documented here rather than force-unified, because the surfaces serve
> genuinely different audiences.

## The surfaces at a glance

| Surface | Command | Overlap metric | DRC metric | Layout scored |
|---------|---------|----------------|------------|---------------|
| Diagnostics | `kct placement check` | real `F.CrtYd`/`B.CrtYd` polygon (positive-area intersection); pads-bbox + margin (default 0.25 mm) fallback when no courtyard artwork — boolean/severity per pair | real KiCad DRC violations | **actual on-disk placement** |
| Optimizer objective (dry-run) | `kct optimize-placement --dry-run` | AABB overlap *area* (mm²), no margin | bbox clearance *count* | **actual on-disk placement** (fixed in #3940) |
| Optimizer objective (search) | `kct optimize-placement` initial/seed eval | AABB overlap *area* (mm²), no margin | bbox clearance *count* | seed / candidate populations during the search |
| Interactive energy | `kct placement refine` session | none (spacing energy proxy) | none explicit | live positions in the session |

## Why the numbers differ

There are **two independent axes** of difference:

### 1. Different geometry (courtyard vs. raw bounding box)

- `kct placement check`
  ([`PlacementAnalyzer`](../src/kicad_tools/placement/analyzer.py)) reads each
  footprint's **real** `F.CrtYd`/`B.CrtYd` courtyard polygon (via the shared
  [`geometry.courtyard`](../src/kicad_tools/geometry/courtyard.py) helpers, the
  same geometry `kct check`'s courtyard-overlap DRC rule and KiCad use) and
  flags a pair when their courtyard polygons intersect with strictly positive
  area, so `kct placement check` and `kct check` agree on courtyard overlaps
  (issue #4182). For footprints with **no** resolvable courtyard artwork it
  falls back to the legacy approximation: the pad bounding box expanded by
  `courtyard_margin`, tested as an axis-aligned rectangle overlap. Under that
  fallback a pair that merely *touches* (0 mm gap) is flagged because the
  courtyard margins overlap; under the real-polygon path exactly-touching
  courtyards (zero-area intersection) do **not** conflict, matching KiCad.
- The optimizer objective
  ([`evaluate_placement`](../src/kicad_tools/placement/cost.py)) measures the
  raw axis-aligned bounding-box overlap **area** in mm², with **no** margin.
  The same touching pair contributes **zero** overlap.

So for touching footprints the two surfaces disagree *by construction* — up to
the courtyard margin. This is intentional: the analyzer produces actionable
per-violation diagnostics, while the optimizer needs a smooth, continuous
objective (area) that a search algorithm can descend.

### 2. Different layout (the `--dry-run` seeding bug — now fixed)

Before #3940, `optimize-placement --dry-run` did **not** evaluate the layout on
disk. It called `_generate_seed(...)` (force-directed placement) and scored
*that*, so the reported `ovl`/`drc` reflected a randomized arrangement, not the
footprints as placed. The optimizer's own "initial eval" line scored yet
another arrangement (the best of a random CMA-ES initial population). That is
why the same board could read `ovl=58.26 drc=23` (dry-run seed),
`ovl=1.87 drc=5` (random initial population), and `1 warning`
(`placement check`) all at once.

`--dry-run` now encodes the **current** footprint positions
(`_read_current_vector`) and scores them, so its overlap/DRC numbers reflect
the layout on disk and agree with a direct `evaluate_placement()` call on the
decoded current vector (verified by
[`tests/placement/test_scoring_parity.py`](../tests/placement/test_scoring_parity.py)).

## Which surface should I trust?

- **"Is my hand placement manufacturable?"** → `kct placement check`. It uses
  courtyard geometry and real KiCad DRC, and reports *what* to fix.
- **"Is the optimizer making progress / is this candidate feasible in the
  optimizer's own terms?"** → `optimize-placement --dry-run` and the optimizer
  progress output. These share one objective, so their scalars are directly
  comparable to each other.
- **`kct placement refine` score** is a *physics-simulation energy proxy*
  (`wire_length + energy * 0.1`), used only to drive the interactive
  hill-climb. It is **not** comparable to the other surfaces' overlap/DRC
  numbers and should not be read as a placement-quality verdict.

## Practical guidance

- A clean `kct placement check` (0 warnings/errors) is the manufacturability
  gate. `--dry-run` feasibility (`Feasible: True`) is the *optimizer's* gate
  and uses looser, margin-free geometry — it can report feasible while
  `placement check` still flags a courtyard nibble.
- When you optimize from a good hand placement, **seed the optimizer from it**
  rather than a random population; otherwise CMA-ES searches a layout space
  disconnected from your placement and can regress wirelength substantially.

## HV-aware placement: the creepage-keepout term (issue #4373)

By default the optimizer objective is **voltage-blind**: the wirelength term
pulls connected pins together and the DRC term enforces a single
`min_clearance` (0.2 mm) uniformly across every pad pair. Nothing keeps a
150 V mains net away from a 1.65 V reference. On a mains board this drives the
optimizer to pack high- and low-voltage copper together, and the only way to
reach a creepage-feasible floorplan was a hand-authored zoned layout.

`optimize-placement` can now be made **HV-aware** by supplying a voltage or
domain input. This activates an extra hard-feasibility term,
`compute_creepage_violation`, alongside overlap/DRC/boundary:

- Each footprint is assigned an HV **domain** (a voltage cluster).
- For every pair of footprints in **different** domains, the edge-to-edge gap
  is compared against the **required creepage** for that domain pair, looked up
  in `kicad_tools.creepage.standards` at the cross-domain `|ΔV|` (step-up rule,
  no interpolation — the same table `kct creepage` uses).
- A pair closer than its requirement contributes a shortfall, and any non-zero
  creepage shortfall makes the placement **infeasible** — so under the default
  lexicographic mode the optimizer refuses to converge on an HV-too-close
  layout, exactly as it does for overlap or DRC.

Absent a voltage/domain input the term stays dormant and scores are
**byte-identical** to the historical objective (regression-safe opt-in,
mirroring `--anchor-weight`).

### Same-domain clustering: the cohesion term

The creepage keepout pushes *different* domains **apart**; the complementary
`compute_domain_cohesion` term pulls a *same* domain **together**. Whenever a
domain partition is supplied, each domain with two or more member footprints
contributes a radius-of-gyration-style spread penalty: the centroid of its
members is computed and each member's Euclidean distance to that centroid is
summed. Minimizing it packs every voltage domain into a compact zone instead of
leaning on wirelength to incidentally cluster it.

Unlike creepage, cohesion is a **soft preference, not a feasibility gate**. It
is deliberately excluded from `is_feasible` and from the infeasible branch of
the lexicographic score — a spread-out domain is suboptimal, never infeasible —
so it only shapes the layout once a placement is already feasible. Its weight
(`cohesion`, default `1.0`) is kept small relative to the hard-constraint
weights so it can never override feasibility. The term needs only the domain
partition (no per-pair voltages), so it fires on both the `--voltage-map` and
`--hv-domains` paths, and stays dormant (contributing `0.0`) when no HV input is
supplied — preserving the byte-identical guarantee above.

### Input contract

Two mutually-exclusive sources:

- **`--voltage-map v.json`** — a flat `{net_name: volts}` object (reuses the
  per-net ΔV model format). Each footprint's domain is the *name of its
  highest-magnitude-voltage net* (a footprint touching a 150 V mains net lands
  in that mains domain); the domain's voltage is that magnitude.

  ```json
  {"/AC_LINE": 150, "/AC_NEUTRAL": 150, "/REF_1V65": 1.65, "/GATE_BUS": 12}
  ```

- **`--hv-domains d.json`** — the manual fallback so the feature works
  standalone. Keys are domain ids; each carries a list of `fnmatch` ref globs
  and a representative voltage:

  ```json
  {
    "mains":  {"refs": ["J1", "F1", "R1", "R2"], "voltage": 150},
    "signal": {"refs": ["U3", "R10", "C5"],      "voltage": 3.3}
  }
  ```

  A ref matching more than one domain resolves to the higher-voltage domain.

### Tuning knobs

| Flag | Default | Meaning |
|------|---------|---------|
| `--creepage-standard` | `iec60664` | Standard for the required-distance lookup (`iec60664`/`iec62368`). |
| `--pollution-degree` | `2` | IEC pollution degree (1/2/3). |
| `--material-group` | `IIIa` | Insulation material group (I/II/IIIa/IIIb). |
| `--hv-threshold` | `30.0` | Minimum cross-domain `|ΔV|` (V) that triggers a keepout. Lower-difference domain pairs rely on normal DRC clearance, so low-voltage/low-voltage nets are not over-segregated. |
| `--weights '{"creepage": …}'` | `1e5` | Weight applied to the creepage shortfall (a hard-feasibility term). |
| `--weights '{"cohesion": …}'` | `1.0` | Weight applied to the same-domain clustering penalty (a soft preference; see above). |

A `|ΔV|` above the highest tabulated creepage row raises a loud
`StandardLookupError` (surfaced as an `Error:` and exit code 1) — the tool
never silently extrapolates a safety distance.

### Guarded sense taps (auto-detected)

Some low-voltage nets are *derived from* an HV net (e.g. `V_AC_SENSE_RAW` off
`AC_LINE` through a divider) and cannot be pushed away — they need a guard ring
rather than separation. `compute_creepage_violation` accepts an `exempt_pairs`
set that excludes such `(HV-ref, tap-ref)` pairs from the keepout while keeping
the tap constrained against *other* domains.

Derived taps are now **auto-detected** from a `--voltage-map` by
`detect_derived_tap_exempt_pairs` (`placement/hv_domains.py`), reusing the
existing `--hv-threshold` — no new knob. The heuristic:

1. Each net in the voltage map is HV when `|V| >= --hv-threshold`, else LV.
2. An LV net `t` is a **derived tap of** HV net `h` when they share at least one
   common component ref (the bridging divider/limiter resistor touches both
   nets) and `|V_t| < |V_h|`.
3. For each derived `(t, h)`, every ref on `t` and ref on `h` whose **domains
   differ** is added to `exempt_pairs` — so the tap-side footprints are exempt
   from *their parent* HV domain, while the bridging ref (which resolves to the
   HV domain) never self-exempts and the tap keeps its keepout against
   *unrelated* HV domains.

Each detected tap prints a guard advisory (unless `--quiet`), e.g.
`guarded tap: /V_AC_SENSE_RAW derived from /AC_LINE - route with a guard
trace/ring`. This path is voltage-map only: the `--hv-domains` declaration has
no per-net data, so no taps are auto-exempted there. Generating the actual guard
trace/ring copper remains out of scope (see #4372).
