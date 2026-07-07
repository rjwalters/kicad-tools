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
| Diagnostics | `kct placement check` | courtyard-expanded polygon (margin, default 0.25 mm) — boolean/severity per pair | real KiCad DRC violations | **actual on-disk placement** |
| Optimizer objective (dry-run) | `kct optimize-placement --dry-run` | AABB overlap *area* (mm²), no margin | bbox clearance *count* | **actual on-disk placement** (fixed in #3940) |
| Optimizer objective (search) | `kct optimize-placement` initial/seed eval | AABB overlap *area* (mm²), no margin | bbox clearance *count* | seed / candidate populations during the search |
| Interactive energy | `kct placement refine` session | none (spacing energy proxy) | none explicit | live positions in the session |

## Why the numbers differ

There are **two independent axes** of difference:

### 1. Different geometry (courtyard vs. raw bounding box)

- `kct placement check`
  ([`PlacementAnalyzer`](../src/kicad_tools/placement/analyzer.py)) expands
  each footprint's pad bounding box by `courtyard_margin` before testing for
  overlap, and reports each conflicting pair as a human-readable
  warning/error. A pair that merely *touches* (0 mm gap) is flagged because
  the courtyard margins overlap.
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
