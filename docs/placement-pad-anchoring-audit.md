# Placement pad-anchoring audit — 2026-08

**Issue:** #4831 (item 1 of 5 — the written audit; items 2-5 remain open)
**Audit commit:** `784dac2b` (the `main` this branch was cut from)
**Date:** 2026-08-14
**Scope:** read-only. This audit changed **no** file under `src/`. Every
migration candidate below is a *stub for a future issue*, listed in this
document rather than filed (issue creation is serialized in this repo).

> **Status update (2026-08-14, later the same day): M1 has landed.**
> `compute_wirelength` and `evaluate_placement` now accept an optional
> `pad_positions` map (built by `build_pad_position_map`,
> `src/kicad_tools/placement/wirelength.py`), and
> `kct optimize-placement --pad-anchored-wirelength` supplies it from the
> pads the CLI already decoded. It is **opt-in**: with no map the score is
> byte-identical to the centre-anchored objective. The line-number citations
> below describe the tree *as audited*; `cost.py` line numbers shifted by the
> M1 patch. M2-M5 are unchanged and still unfiled.

## Why this audit exists

pcbplace (a generative placement engine described to the owner; see #4831 for
the write-up summary) reports that its single highest-leverage design decision
was **anchoring placement constraints to pads rather than to part centres** —
"keep each decoupling cap near *its own power pad*" instead of "near the chip
centre" reportedly cut a demo board's wiring from **59.5 mm to 12.7 mm** and
made rotation couple to pad position for free.

That number is **second-hand and not reproduced here** — treat it as the
motivation for looking, not as evidence about *our* boards. What this audit
does supply is a first-party inventory of which of our placement terms are
centre-anchored, plus a first-party measurement of how far the two estimators
diverge on our own committed fixtures (§4).

---

## 1. Headline findings

1. **A pad-anchored HPWL estimator already exists, is tested, and is not wired
   into the optimizer.** `compute_hpwl` (`src/kicad_tools/placement/wirelength.py:111`)
   measures HPWL from real transformed pad coordinates. The optimizer objective
   `evaluate_placement` (`src/kicad_tools/placement/cost.py:715`) instead calls
   `compute_wirelength` (`src/kicad_tools/placement/cost.py:228`), which builds
   `pos_map` from component **centres** (`src/kicad_tools/placement/cost.py:253`).
   Nothing under `src/` calls `compute_hpwl` — only
   `tests/test_placement_benchmark.py:50` and `tests/test_placement_wirelength.py:18`.
2. **The pads are already computed and then thrown away on every evaluation.**
   Both optimizer front-ends decode a placement vector into `PlacedComponent`
   objects *with* transformed pads and immediately project them down to
   centre-only `ComponentPlacement`:
   `_vector_to_placements` (`src/kicad_tools/cli/optimize_placement_cmd.py:142`)
   and `_vector_to_placements` (`src/kicad_tools/mcp/tools/optimize_placement.py:221`).
   The migration is therefore not blocked on data availability.
3. **The older `optim` force-directed placer is already pin-anchored.** Its net
   springs pull *pin* coordinates, not centres — `compute_spring_force`
   (`src/kicad_tools/optim/placement.py:1230`) and `total_wire_length`
   (`src/kicad_tools/optim/placement.py:2340`) both read `pin.x`/`pin.y`. So the
   repo already contains a working example of pad/pin anchoring; the newer
   `placement` package regressed to centres.
4. **The declarative `near`-equivalent constraint is centre-to-centre.**
   `_validate_max_distance` (`src/kicad_tools/optim/constraints.py:239`) computes
   `comp.x - anchor.x` between component origins. This is the closest analogue
   in our tree to pcbplace's decap example.
5. **"Anchor" already means three unrelated things in this repo** (§7). None of
   them is pad anchoring. Do not mistake `--anchor-weight` or
   `--placement-feedback-anchor` for prior art on this issue.

---

## 2. How to verify this document

Every claim below cites `path:line`. Two commands re-check them against the
tree you are reading:

```bash
# 1. Does the cited symbol live at the cited line?
uv run python - <<'PY'
import ast, pathlib
for f, line in [("src/kicad_tools/placement/cost.py", 715),
                ("src/kicad_tools/placement/wirelength.py", 111)]:
    tree = ast.parse(pathlib.Path(f).read_text())
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.lineno == line:
            print(f, line, n.name)
PY

# 2. Is the pad-anchored estimator wired into the objective anywhere in src/?
rg -n "compute_hpwl" src/     # expect: only wirelength.py + placement/__init__.py exports
```

Line numbers drift. If a cite misses, `rg -n "<symbol>" <file>` and treat the
symbol name — not the number — as the identity of the claim.

---

## 3. Inventory

Classification key:

- **centre** — the term measures distance/extent between component origins.
- **pad** — the term measures real transformed pad/pin coordinates.
- **body** — the term measures footprint bounding boxes / courtyards. Not a
  net-based term, so pad anchoring does not apply (see §6).
- **n/a** — bookkeeping, not a geometric measurement.

### 3.1 The optimizer objective — `src/kicad_tools/placement/cost.py`

`evaluate_placement` (`cost.py:715`) is *the* objective scored by
`kct optimize-placement` (via `_evaluate`, `src/kicad_tools/cli/optimize_placement_cmd.py:163`),
by the MCP `optimize_placement`/`evaluate_placement` tools
(`src/kicad_tools/mcp/tools/optimize_placement.py:325` and `:603`), and by the
registry handler (`src/kicad_tools/mcp/tools/registry.py:1692`).

| Term | Symbol | Anchoring | Evidence |
|---|---|---|---|
| Wirelength (HPWL) | `compute_wirelength` (`cost.py:228`) | **centre** | `pos_map = {p.reference: (p.x, p.y) …}` at `cost.py:253`; called from `cost.py:773` |
| Overlap | `compute_overlap` (`cost.py:269`) | body | AABB from `footprint_sizes`, `cost.py:289` |
| Board boundary | `compute_boundary_violation` (`cost.py:301`) | body | half-extent box vs outline, `cost.py:328-337` |
| DRC clearance count | `compute_drc_violations` (`cost.py:342`) | body | pairwise bbox gap vs `min_clearance`, `cost.py:376-392` |
| Compactness / area | `compute_area` (`cost.py:396`) | **centre** | docstring: "bounding-box area enclosing all component centers", `cost.py:399` |
| Block boundary | `compute_block_boundary_violation` (`cost.py:421`) | body | member bbox vs `BlockRegion`, `cost.py:466-469` |
| Inter-block spacing | `compute_inter_block_spacing_violation` (`cost.py:474`) | body | per-block bbox union, `cost.py:514-521` |
| HV creepage keepout | `compute_creepage_violation` (`cost.py:553`) | body | edge-to-edge bbox gap vs required creepage, `cost.py:633-643` |
| Same-domain cohesion | `compute_domain_cohesion` (`cost.py:648`) | **centre** | `by_domain … append((p.x, p.y))` at `cost.py:700`; `del footprint_sizes  # unused: cohesion is measured between footprint centres` at `cost.py:691` |
| Weighted-sum aggregation | `_weighted_sum_score` (`cost.py:847`) | n/a | |
| Lexicographic aggregation | `_lexicographic_score` (`cost.py:862`) | n/a | |
| Per-net weighting | `Net.weight` (`cost.py:169`, field documented `cost.py:176-181`) | n/a | multiplies whichever wirelength term is in use |

Three centre-anchored terms; the rest are body geometry or bookkeeping.

### 3.2 The multi-fidelity evaluator — `src/kicad_tools/placement/multi_fidelity.py`

| Level | Symbol | Anchoring | Evidence |
|---|---|---|---|
| Fidelity 0 (HPWL) | `_evaluate_fidelity_0` (`multi_fidelity.py:266`) | **centre** | delegates to `compute_wirelength`, `multi_fidelity.py:276` |
| Fidelity 1 (+DRC) | `_evaluate_fidelity_1` (`multi_fidelity.py:292`) | **centre** wirelength, **pad** DRC | builds `simple_placements` by dropping pads (`multi_fidelity.py:306-314`) then calls `compute_wirelength` at `multi_fidelity.py:316`, while `check_placement_drc` (`src/kicad_tools/placement/drc.py:205`) does real pad-to-pad clearance |
| Fidelity 2/3 (routing) | `_evaluate_global_routing` (`multi_fidelity.py:511`) | n/a | routability ratio only |

`_evaluate_fidelity_1` is the sharpest instance of the pattern: it is *handed*
`PlacedComponent` objects with transformed pads (it needs them for DRC) and
still measures wirelength between centres.

Reachability note (honest scoping): `evaluate_placement_multifidelity`
(`multi_fidelity.py:374`) is exported from `src/kicad_tools/placement/__init__.py:57`
but is not called from any CLI or MCP surface — `rg -n "multi_fidelity" src/`
returns only the package `__init__`. It is library/test surface today, so a
migration there has lower blast radius *and* lower payoff than §3.1.

### 3.3 Seeds and priors

| Term | Symbol | Anchoring | Evidence |
|---|---|---|---|
| Force-directed seed | `force_directed_placement` (`src/kicad_tools/placement/seed.py:95`) | **centre** | attraction from `_build_net_adjacency` (`seed.py:65`) — a shared-net *count* per component pair, applied to centre positions |
| Random seed | `random_placement` (`src/kicad_tools/placement/seed.py:248`) | n/a | |
| Affinity graph | `build_affinity_graph` (`src/kicad_tools/placement/priors.py:191`) | n/a (topological) | edge weight = shared-net count, `priors.py:225-232`; pad-blind by construction |
| Schematic-proximity prior | `schematic_proximity_prior` (`src/kicad_tools/placement/priors.py:489`) | **centre** | iterates positions toward the weighted centroid of neighbours |
| GP prior mean | `prior_mean_position` (`src/kicad_tools/placement/priors.py:637`) | **centre** | weighted centroid of neighbour positions, `priors.py:664-673` |
| Cluster / power-domain detection | `find_clusters` (`priors.py:243`), `detect_power_domains` (`priors.py:308`), `power_domain_clustering` (`priors.py:619`) | n/a (topological) | |

### 3.4 Declarative constraints — `src/kicad_tools/optim/constraints.py`

This is the existing analogue of pcbplace's five-verb vocabulary (relevant to
#4831 item 4, which is **not** in this slice's scope).

| Constraint | Symbol | Anchoring | Evidence |
|---|---|---|---|
| `max_distance` (≈ `near`) | `_validate_max_distance` (`constraints.py:239`) | **centre** | `dist = sqrt((comp.x - anchor.x)**2 + (comp.y - anchor.y)**2)`, `constraints.py:266` |
| `alignment` | `_validate_alignment` (`constraints.py:282`) | **centre** | axis coordinate of component origins |
| `ordering` | `_validate_ordering` (`constraints.py:317`) | **centre** | |
| `within_box` (≈ `fixed`/region) | `_validate_within_box` (`constraints.py:360`) | **centre** | |
| `relative_position` | `_validate_relative_position` (`constraints.py:404`) | **centre** | |
| Keepout (≈ `keepout`) | `validate_keepout_violations` (`src/kicad_tools/optim/keepout.py:581`) | **centre point** | `zone.contains_point(cx, cy)` on `fp.position`, `keepout.py:598-601` |

### 3.5 Other placement surfaces (for completeness)

| Surface | Symbol | Anchoring | Note |
|---|---|---|---|
| `kct placement check` diagnostics | `PlacementAnalyzer` (`src/kicad_tools/placement/analyzer.py:47`) | body | real courtyard polygons; see `docs/placement-scoring.md` |
| Side-aware overlap/boundary | `compute_overlap` (`src/kicad_tools/placement/geometry.py:93`), `compute_boundary_violation` (`geometry.py:144`) | body | richer siblings of the `cost.py` versions |
| C++ cost kernels | `compute_overlap_cpp` (`src/kicad_tools/placement/cpp_backend.py:145`), `compute_boundary_violation_cpp` (`cpp_backend.py:182`), `compute_drc_violations_cpp` (`cpp_backend.py:222`), `create_batch_evaluator` (`cpp_backend.py:260`) | body | the accelerated terms are exactly the body-geometry ones — **note for any migration**: there is no `compute_wirelength_cpp`, so a pad-anchored wirelength does not invalidate a C++ kernel |
| `kct placement refine` energy | `PlacementSession._compute_score` (`src/kicad_tools/optim/session.py:273`) | **pad** (indirectly) | sums `total_wire_length()` (pin-anchored, §5) + spacing energy; explicitly *not* the optimizer objective (`session.py:276-291`) |

---

## 4. Measured divergence between the two estimators (first-party)

Both estimators were run over the **same** committed placements in
`tests/fixtures/placement/benchmark_boards.json` (decoded through
`encode`/`decode`, `src/kicad_tools/placement/vector.py:320`/`:344`, so pad
transforms are the production ones):

| Fixture board | Components | Nets | `compute_wirelength` (centres) | `compute_hpwl` (pads) | Δ |
|---|---|---|---|---|---|
| `trivial_3_resistors` (known-optimal) | 3 | 4 | 12.000 mm | 7.200 mm | −40.0% |
| `simple_rc_filter` (reference) | 10 | 8 | 127.000 mm | 126.690 mm | −0.24% |
| `medium_mcu_board` (reference) | 20 | 17 | 382.000 mm | 339.230 mm | −11.2% |
| `stress_50_components` | 50 | — | *not measured* | *not measured* | no committed placement — the fixture carries only a `component_generation` block and the board is built programmatically in `tests/test_placement_benchmark.py` |

Reproduce with:

```bash
uv run python - <<'PY'
import json, pathlib
from kicad_tools.placement.cost import ComponentPlacement, Net, compute_wirelength
from kicad_tools.placement.vector import ComponentDef, PadDef, PlacedComponent, decode, encode
from kicad_tools.placement.wirelength import compute_hpwl

boards = json.loads(pathlib.Path("tests/fixtures/placement/benchmark_boards.json").read_text())["boards"]
for name, b in boards.items():
    if "components" not in b:
        continue
    defs = {c["reference"]: ComponentDef(
        reference=c["reference"],
        pads=tuple(PadDef(p["name"], p["local_x"], p["local_y"], p.get("size_x", .5), p.get("size_y", .5))
                   for p in c.get("pads", [])),
        width=c.get("width", 1.0), height=c.get("height", 1.0)) for c in b["components"]}
    nets = [Net(n["name"], tuple((p[0], p[1]) for p in n["pins"])) for n in b["nets"]]
    raw = b.get("known_optimal_placement") or b.get("reference_placement")
    order = [defs[r["reference"]] for r in raw]
    placed = [PlacedComponent(r["reference"], r["x"], r["y"], r.get("rotation", 0.0), r.get("side", 0), ())
              for r in raw]
    dec = decode(encode(placed), order)
    centre = compute_wirelength([ComponentPlacement(p.reference, p.x, p.y, p.rotation) for p in dec], nets)
    print(f"{name:22s} centre={centre:8.3f}  pad={compute_hpwl(dec, nets):8.3f}")
PY
```

**What this does and does not show.** It shows the two estimators assign
materially different lengths to the *same* layout (up to 40% on a 3-part board,
11% on a 20-part board) — which bounds how differently they can *rank*
candidate layouts, and is the mechanism by which a pad-anchored objective could
find a different optimum. It does **not** show that optimizing against pads
produces shorter routed copper on our boards; nobody has run that experiment
here. The pad-anchored value happened to be lower on all three fixtures, but
that direction is not guaranteed in general — a pad can sit outside the
bounding box of its own component's centre.

---

## 5. Already pad-anchored (do not re-litigate as new work)

| Surface | Symbol | Status |
|---|---|---|
| Pad HPWL estimator | `compute_hpwl` (`src/kicad_tools/placement/wirelength.py:111`), `compute_hpwl_breakdown` (`wirelength.py:140`) | Pad-anchored via `_build_pad_lookup` (`wirelength.py:60`) + `_hpwl_for_net` (`wirelength.py:79`), which read `pad.x`/`pad.y` from `TransformedPad` (`src/kicad_tools/placement/vector.py:142`). Tested in `tests/test_placement_wirelength.py:18` and `tests/test_placement_benchmark.py:460`, `:572`, `:670`. **Not called from anywhere in `src/`.** |
| Per-footprint ratsnest | `compute_per_footprint_ratsnest` (`wirelength.py:191`) | Pad-anchored (nearest-pad distances, `wirelength.py:243-247`). **Is** used in `src/` — but only for *reporting*, not scoring: `src/kicad_tools/mcp/tools/optimize_placement.py:42` (import), `:553`, `:686`. (The curator's scope note said it was test-only; it is not — corrected here.) |
| Placement DRC | `check_placement_drc` (`src/kicad_tools/placement/drc.py:205`) | Pad-anchored pad-to-pad clearance, per its own docstring (`drc.py:220-224`). Wired into fidelity ≥ 1 (`multi_fidelity.py:325`) but **not** into `evaluate_placement`, which uses the bbox-count `compute_drc_violations` instead. |
| Pad transform machinery | `_transform_pad` (`vector.py:254`), `decode` (`vector.py:344`) | Produces absolute pad coordinates for every component on every decode. Pads are populated in both production front-ends: `src/kicad_tools/cli/optimize_placement_cmd.py:486-496` and `src/kicad_tools/mcp/tools/optimize_placement.py:129`. |
| Legacy force-directed placer | `compute_spring_force` (`src/kicad_tools/optim/placement.py:1230`), `total_wire_length` (`optim/placement.py:2340`), `create_springs_from_nets` (`optim/placement.py:830`) | **Pin-anchored.** `Pin` (`src/kicad_tools/optim/components.py:35`) stores absolute coordinates; `Spring` (`optim/components.py:187`) connects `(comp_ref, pin_num)` pairs. This is the behaviour pcbplace advocates, already shipping in the `optim` engine behind `kct placement refine`. |

Two honest consequences:

- The repo does **not** need to invent pad anchoring; it needs to stop
  discarding it in the newer `placement` package.
- Any migration should state which of the two engines it is aligning, because
  they currently disagree: `optim` measures pin-to-pin spring length while
  `placement` measures centre HPWL.

One partial exception worth naming: `_create_cluster_springs`
(`src/kicad_tools/optim/placement.py:897`) is *nominally* pin-anchored but picks
`comp.pins[0]` as "a proxy for center" (`optim/placement.py:920-922`, `:934`) — an
arbitrary pad, not the electrically relevant one. That is precisely the
distinction pcbplace draws (near *its own power pad*), so this counts as
"pad-addressed but not pad-*meaningful*".

---

## 6. Migration candidates

Each stub is a future issue **to be filed by a human/Champion/Curator**, not by
this audit. Benefit rationale is grounded in (a) pcbplace's reported
59.5 → 12.7 mm wiring reduction from pad anchors, and (b) the first-party
divergence measured in §4 — with the honest caveat from §4 attached to both.

### M1 — Wire `compute_hpwl` into `evaluate_placement` — **LANDED (opt-in)**

> **Shipped as:** `kct optimize-placement --pad-anchored-wirelength` plus the
> `pad_positions` argument on `compute_wirelength` / `evaluate_placement`.
> Implementation note: rather than calling `compute_hpwl` directly (which
> would have dropped `Net.weight`, and which `cost.py` cannot import without
> a cycle — `wirelength.py` imports `cost.py`), the pad coordinates are
> passed *into* `compute_wirelength` as a `(reference, pad_name) -> (x, y)`
> map. That keeps per-net weighting, keeps one net-iteration code path, and
> adds per-pin fallback to the component centre. The counter-note below about
> silently dropping `Net.weight` is therefore **resolved, not carried**.
> Remaining M1 surface, deliberately deferred: the MCP front-end
> (`src/kicad_tools/mcp/tools/optimize_placement.py`) still discards its
> pads, and pad anchoring is opt-in rather than the default — flipping the
> default needs fleet evidence (see M5).

> **Stub title:** `feat(placement): score the optimizer objective on pad-anchored HPWL`
> **Scope:** give `evaluate_placement` (`cost.py:715`) access to transformed pads
> (either by accepting `PlacedComponent` or by an optional pad-lookup argument)
> and use `compute_hpwl` (`wirelength.py:111`) for the wirelength term, keeping
> `compute_wirelength` as the fallback when pads are absent. Update the
> benchmark expectations in `tests/test_placement_benchmark.py` and
> `docs/placement-scoring.md`.

**Expected benefit.** This is the term pcbplace identifies as the source of its
59.5 → 12.7 mm improvement. §4 shows the two estimators already disagree by
0.2-40% on our own fixtures, so the objective's ranking of candidate layouts
genuinely changes. It also makes rotation meaningful to wirelength for free:
today, rotating a part changes `compute_wirelength` by exactly zero (only
`p.x`/`p.y` are read at `cost.py:253`), whereas pad coordinates rotate with the
part (`_transform_pad`, `vector.py:254`) — so the optimizer currently searches a
rotation dimension the wirelength term cannot see.

**Cost/risk.** This changes the score of *every* placement run (GA/BO/CMA-ES
trajectories, snapshots, board pipelines). The pads are already computed and
discarded (`optimize_placement_cmd.py:142`, `mcp/tools/optimize_placement.py:221`),
so the added compute is one dict build per evaluation
(`_build_pad_lookup`, `wirelength.py:60`), not a new transform pass. Deliberately
excluded from this audit-only slice.

**Honest counter-note.** `compute_hpwl` ignores `Net.weight` (`cost.py:169`),
which `compute_wirelength` honours (`cost.py:265`) and which the
`--anchor-weight` feature depends on (§7). A straight swap would silently drop
per-net weighting; the follow-up must carry the weight through.

### M2 — Stop discarding pads in `_evaluate_fidelity_1`

> **Stub title:** `refactor(placement): use pad HPWL at multi-fidelity level 1, where pads are already required`
> **Scope:** in `_evaluate_fidelity_1` (`multi_fidelity.py:292`), replace the
> `compute_wirelength(simple_placements, nets)` call at `multi_fidelity.py:316`
> with `compute_hpwl(placements_rich, nets)`.

**Expected benefit.** Same mechanism as M1 (pcbplace's 59.5 → 12.7 mm; §4's
measured divergence), at the lowest possible cost: fidelity ≥ 1 already
*requires* `PlacedComponent` with transformed pads (`multi_fidelity.py:457-461`)
and already uses them for DRC. The one-line downgrade at `multi_fidelity.py:306-314`
is pure information loss.

**Honest scope note.** Payoff today is small because nothing under `src/` calls
`evaluate_placement_multifidelity` (§3.2) — this is library/test surface. Its
value is as a low-risk pathfinder for M1, not as a user-visible win.

### M3 — Pad-address the `max_distance` constraint

> **Stub title:** `feat(optim): allow max_distance constraints to target a pad, not a component centre`
> **Scope:** extend `SpatialConstraint.max_distance` (`constraints.py:46`, factory
> at `constraints.py:57`) to accept `anchor="U1.14"`-style pad addressing, and
> teach `_validate_max_distance` (`constraints.py:239`) to measure to the nearest
> pad on the named net/pad instead of `anchor.x`/`anchor.y` (`constraints.py:266`).

**Expected benefit.** This is a *direct* transcription of pcbplace's headline
example: their 59.5 → 12.7 mm came from re-expressing "cap near chip" as "cap
near its own power pad". Our `max_distance` is that constraint, expressed the
losing way. On a large IC the centre-vs-pad error is bounded below by half the
package diagonal — e.g. for a 10 mm QFP, a decap satisfying "within 5 mm of
centre" can be ~10 mm of copper from the pad it decouples.

**Honest counter-note.** Unlike M1/M2 this needs a user-facing syntax decision
(how a pad is addressed in YAML) and overlaps #4831 item 4 (the declarative
constraint spec). It may be better folded into that slice than shipped alone.

### M4 — Make cluster springs target the electrically relevant pad

> **Stub title:** `fix(optim): pick the shared-net pad instead of pins[0] for cluster springs`
> **Scope:** in `_create_cluster_springs` (`optim/placement.py:897`), replace the
> `pins[0]` "proxy for center" choice (`optim/placement.py:920-922`, `:934`) with the
> pad on the net the cluster is formed around.

**Expected benefit.** Same pcbplace mechanism at the cluster level: the spring
already pulls *a* pad (so the machinery exists — §5), just not the meaningful
one. Because the surrounding engine is already pin-anchored, this is a local
fix with no objective-wide blast radius.

**Honest counter-note.** Smaller and less certain than M1-M3; cluster springs
are a proximity heuristic, and `pins[0]` is often already the relevant pad on
two-pad passives. File only if M1 lands and the `optim` engine remains in use.

### M5 — Report both estimators before switching either

> **Stub title:** `feat(placement): report pad-anchored HPWL alongside the centre-anchored score`
> **Scope:** emit `compute_hpwl` next to `CostBreakdown.wirelength`
> (`cost.py:98`) in `kct optimize-placement --dry-run` and the MCP
> `evaluate_placement` response (`mcp/tools/optimize_placement.py:603`), without
> changing what is optimized.

**Expected benefit.** Converts §4's three-fixture measurement into fleet-wide
evidence at zero behavioural risk, so M1's real payoff (or absence) can be
argued from our own boards rather than from pcbplace's reported
59.5 → 12.7 mm. This is the cheapest way to de-risk M1.

**Honest counter-note.** Purely additive telemetry — it improves nobody's
placement by itself, and if M1 is going to be built regardless, M5 is skippable.

---

## 7. Declined (no migration recommended)

| Term | Why not |
|---|---|
| `compute_area` (`cost.py:396`) | Centre-anchored, but it is a *compactness proxy*, not a connectivity term. A pad-anchored area (bbox of all pads) would mostly re-measure total footprint extent, which `compute_overlap`/boundary already constrain. pcbplace's write-up identifies no area/compactness pad-anchoring win. Re-proposing this needs new evidence, not just the observation that it reads `p.x`. |
| `compute_domain_cohesion` (`cost.py:648`) | Centre-anchored by explicit design — its docstring (`cost.py:685`) and `del footprint_sizes` (`cost.py:691`) state that domains are clustered as a radius-of-gyration over centres. A voltage *domain* is a property of the whole part (an HV part's every pad is HV), so a pad-anchored variant would compute nearly the same field at N-pads cost. Decline unless a mixed-domain part (isolator, opto) motivates per-pad domains — which is a different feature, not a re-anchoring. |
| `compute_overlap` / `compute_boundary_violation` / `compute_drc_violations` / block terms / `compute_creepage_violation` (`cost.py:269`, `:301`, `:342`, `:421`, `:474`, `:553`) | Body geometry, not net geometry. Pad anchoring is undefined for them. Note the DRC term *does* have a pad-accurate sibling (`check_placement_drc`, `drc.py:205`) — unifying those is a **precision** question, not a pad-anchoring question, and is out of scope here. |
| `build_affinity_graph` (`priors.py:191`), `find_clusters` (`priors.py:243`), `detect_power_domains` (`priors.py:308`) | Purely topological (shared-net counts). No coordinates read; nothing to anchor. |
| `force_directed_placement` (`seed.py:95`), `schematic_proximity_prior` (`priors.py:489`), `prior_mean_position` (`priors.py:637`) | Centre-anchored, but these produce *seeds/priors* that the objective then optimizes. Pad-anchoring a seed buys at most a slightly better starting point for a search whose own objective is still centre-anchored. Revisit **only after** M1 lands; migrating them first is optimizing the wrong end. |
| `validate_keepout_violations` (`keepout.py:581`) | Centre-*point* containment (`keepout.py:598`), so a part straddling a zone edge is missed. That is a body-geometry precision gap, not a pad-anchoring gap; recorded here so a future reader does not file it under this heading. |

---

## 8. "Anchor" in this repo means three other things

None of these is pcbplace's pad anchoring. Do not cite them as prior art.

1. **`--anchor-weight` (net weighting around locked parts).**
   `_compute_net_anchor_weight` (`src/kicad_tools/cli/optimize_placement_cmd.py:532`)
   sets `Net.weight = 1 + anchor_weight * (anchored_pins / total_pins)` for nets
   touching a `(locked)` footprint. "Anchored" = *immovable*. The distance being
   weighted is still centre-to-centre HPWL (`cost.py:265`).
2. **`--placement-feedback-anchor` / `--placement-feedback-no-anchor`.**
   `_auto_detect_anchored_refs` (`src/kicad_tools/cli/route_cmd.py:2817`) and
   `_resolve_placement_feedback_anchors` (`route_cmd.py:2851`) compute the set of
   refs the route↔placement feedback loop may not move; the set is passed as
   `fixed_refs` (`route_cmd.py:3017`, `:3166`). Again: immovability, not pads.
3. **`SpatialConstraint.max_distance(anchor=…)`.** Here `anchor`
   (`src/kicad_tools/optim/constraints.py:57`) names the *reference component* of
   a proximity constraint — and, per §6 M3, it is measured centre-to-centre,
   making it a migration target rather than prior art.

pcbplace's sense — *a constraint expressed relative to a specific pad rather
than to the part origin* — has no existing name in this codebase. If a future
slice needs one, "pad-anchored" (as used throughout this document) is the term
to standardise on, kept clearly distinct from the "locked/immovable" sense above.

---

## 9. Stub index (nothing filed)

| Stub | Title | Depends on |
|---|---|---|
| M1 | `feat(placement): score the optimizer objective on pad-anchored HPWL` | **LANDED opt-in** (`--pad-anchored-wirelength`); MCP front-end + default-on remain |
| M2 | `refactor(placement): use pad HPWL at multi-fidelity level 1, where pads are already required` | — (low risk, good pathfinder for M1) |
| M3 | `feat(optim): allow max_distance constraints to target a pad, not a component centre` | overlaps #4831 item 4 |
| M4 | `fix(optim): pick the shared-net pad instead of pins[0] for cluster springs` | M1 (only if `optim` stays in use) |
| M5 | `feat(placement): report pad-anchored HPWL alongside the centre-anchored score` | — (de-risks M1) |

Related documents: `docs/placement-scoring.md` (why the placement scoring
surfaces intentionally disagree, issue #3940) and
`docs/board-fleet-parity-audit-2026-06.md` (the audit-doc precedent this file
follows).
