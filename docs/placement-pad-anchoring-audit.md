# Placement pad-anchoring audit — 2026-08

**Issue:** #4831 (item 1 of 5 — the written audit; items 2-5 remain open)
**Originally audited at:** `784dac2b`
**Re-verified at:** `8a98a69f` (2026-08-21) — every anchor below re-checked
against the live tree, every measurement re-run; see §2.
**Scope:** read-only with respect to `src/`. Every migration candidate below
is a *stub for a future issue*, listed in this document rather than filed
(issue creation is serialized in this repo).

> **Citation style — symbol anchors, not line numbers (repo policy #4764).**
> This document originally cited source as `file.py:NNN`. Those citations
> rotted three times in the eight days after it landed: PRs #4857 (M1),
> #4863 (M2) and #4870 (M5) each shifted `cost.py`, `wirelength.py`,
> `multi_fidelity.py` and both optimizer front-ends, and each appended a
> "line citations below predate this patch" disclaimer rather than fixing
> them. By 2026-08-21, 60+ of the 86 line citations resolved to the wrong
> symbol: the line cited as `evaluate_placement` had drifted 20 lines into
> the body of `compute_domain_cohesion`, and the line cited as
> `compute_hpwl` had drifted onto an `Args:` line 26 lines above the
> function it claimed to point at.
>
> That is precisely the failure `tests/test_docs_source_citations.py`
> (issue #4764) exists to prevent — it *bans* `file.py:NNN` in policed
> docs and requires a **symbol + path** anchor instead, because a symbol
> survives a refactor and is machine-checkable. This document escaped the
> guard only because its glob list covered `docs/guides/*.md` and
> `docs/reference/*.md` but not top-level `docs/*.md`. It is now re-anchored
> on symbols, and `docs/*.md` is inside the guard (§2), so this class of rot
> cannot recur silently.
>
> Verify any claim below with `rg -n '<symbol>' <path>`.

## Status of the migration candidates

Three of the five candidates in §6 have landed since this audit was written.
They are recorded inline in §6 with their as-built notes; in brief:

| Stub | Status |
|---|---|
| **M1** — score the objective on pad-anchored wirelength | **Landed, opt-in** (#4857). `compute_wirelength` / `evaluate_placement` take an optional `pad_positions` map; `kct optimize-placement --pad-anchored-wirelength` supplies it. Default is still centre-anchored. The MCP front-end is *not* migrated. |
| **M2** — pad HPWL at multi-fidelity level 1 | **Landed, unconditional** (#4863). Fidelity ≥ 1 measures at pads; fidelity 0 unchanged. |
| **M3** — pad-address `max_distance` | Not implemented, not filed. |
| **M4** — cluster springs target the shared-net pad | Not implemented, not filed. |
| **M5** — report both estimators side by side | **Landed, report-only** (#4870). `compare_wirelength_estimators` feeds `kct optimize-placement --dry-run` and the MCP `evaluate_placement` response. |

M1's remaining tail (default-on, plus the MCP front-end) is still gated on the
evidence M5 now produces — and §4.1's fleet measurement says that evidence does
**not** yet support flipping the default.

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
diverge on our own fixtures and on the committed board fleet (§4).

---

## 1. Headline findings

1. **A pad-anchored HPWL estimator exists, is tested, and still has no call
   site in `src/`.** `compute_hpwl` in `src/kicad_tools/placement/wirelength.py`
   measures HPWL from real transformed pad coordinates. `rg -n 'compute_hpwl'
   src/` returns only its own definition, docstring mentions, and the
   `src/kicad_tools/placement/__init__.py` re-export — no caller. It is
   exercised only from `tests/test_placement_wirelength.py` and
   `tests/test_placement_benchmark.py`.
   **This finding is now partly resolved, but not by `compute_hpwl`.** M1/M2
   delivered pad anchoring through a different route: `build_pad_position_map`
   in `wirelength.py` produces a `(reference, pad_name) -> (x, y)` map that is
   passed *into* `compute_wirelength` in `src/kicad_tools/placement/cost.py`.
   That preserves `Net.weight` (which `compute_hpwl` ignores) and avoids an
   import cycle, at the cost of leaving `compute_hpwl` itself dead in `src/`.
   Whether to retire `compute_hpwl` or route it through the same weighting is
   an open tidy-up, not a pad-anchoring gap.
2. **The optimizer objective is still centre-anchored by default.**
   `evaluate_placement` in `cost.py` accepts `pad_positions` but defaults it to
   `None`, and `compute_wirelength` builds `pos_map` from component **centres**
   whenever a pin is absent from that map. Only the CLI opts in, and only
   behind `--pad-anchored-wirelength`.
3. **One of the two front-ends still throws its pads away.** Both decode a
   placement vector into `PlacedComponent` objects *with* transformed pads. The
   CLI keeps them when asked — `_evaluate` in
   `src/kicad_tools/cli/optimize_placement_cmd.py` calls
   `build_pad_position_map` under its `pad_anchored` flag. The MCP path does
   not: `_vector_to_placements` and `_evaluate_vector` in
   `src/kicad_tools/mcp/tools/optimize_placement.py` project straight down to
   centre-only `ComponentPlacement` and never pass `pad_positions`. The
   migration is not blocked on data availability anywhere.
4. **The older `optim` force-directed placer is already pin-anchored.** Its net
   springs pull *pin* coordinates, not centres — `compute_spring_force` and
   `total_wire_length` in `src/kicad_tools/optim/placement.py` both read
   `pin1.x`/`pin2.x`. So the repo already contains a working example of
   pad/pin anchoring; the newer `placement` package regressed to centres.
5. **The declarative `near`-equivalent constraint is centre-to-centre.**
   `_validate_max_distance` in `src/kicad_tools/optim/constraints.py` computes
   `comp.x - anchor.x` between component origins. This is the closest analogue
   in our tree to pcbplace's decap example, and it is unmigrated.
6. **"Anchor" already means three unrelated things in this repo** (§8). None of
   them is pad anchoring. Do not mistake `--anchor-weight` or
   `--placement-feedback-anchor` for prior art on this issue.

---

## 2. How to verify this document

Every source claim is anchored on a **symbol name plus a file path**. Check any
one of them with:

```bash
rg -n 'compute_wirelength' src/kicad_tools/placement/cost.py
```

If a symbol has been renamed, that `rg` returns nothing — which is the signal
to update this document, not to guess at a line number.

Three assertions in this document are machine-checked so they cannot rot
unnoticed:

| Assertion | Guard |
|---|---|
| No `file.py:NNN` citations here; every named symbol still exists in the file this doc names | `tests/test_docs_source_citations.py` (`docs/*.md` is inside `GUARDED_DOC_GLOBS`) |
| §4's fixture divergence numbers | `tests/test_placement_pad_anchored_wirelength.py` |
| §6 M2's level-0 vs level-1 anchoring split | `tests/test_multi_fidelity_pad_anchored.py` |

The load-bearing negative claim — that `compute_hpwl` has no caller in `src/` —
is a one-liner:

```bash
rg -n 'compute_hpwl' src/
# expect: only wirelength.py (def + docstrings), a multi_fidelity.py comment,
# and the placement/__init__.py re-export. No call site.
```

---

## 3. Inventory

Classification key:

- **centre** — the term measures distance/extent between component origins.
- **pad** — the term measures real transformed pad/pin coordinates.
- **body** — the term measures footprint bounding boxes / courtyards. Not a
  net-based term, so pad anchoring does not apply (see §7).
- **n/a** — bookkeeping, not a geometric measurement.

### 3.1 The optimizer objective — `src/kicad_tools/placement/cost.py`

`evaluate_placement` is *the* objective scored by `kct optimize-placement` (via
`_evaluate` in `src/kicad_tools/cli/optimize_placement_cmd.py`), by the MCP
`optimize_placement` / `evaluate_placement` tools in
`src/kicad_tools/mcp/tools/optimize_placement.py`, and by the registry handler
in `src/kicad_tools/mcp/tools/registry.py`.

All symbols below live in `src/kicad_tools/placement/cost.py` unless stated.

| Term | Symbol | Anchoring | Evidence |
|---|---|---|---|
| Wirelength (HPWL) | `compute_wirelength` | **centre** by default, **pad** when `pad_positions` is supplied (M1) | builds `pos_map` from `(p.x, p.y)`; a pin found in `pad_positions` uses its pad, otherwise falls back to `pos_map`. Called from `evaluate_placement` |
| Overlap | `compute_overlap` | body | AABB per component from `footprint_sizes` via `half_w, half_h` |
| Board boundary | `compute_boundary_violation` | body | half-extent box vs the `BoardOutline` |
| DRC clearance count | `compute_drc_violations` | body | pairwise bbox gap vs `min_gap` from `DesignRuleSet.min_clearance` |
| Compactness / area | `compute_area` | **centre** | docstring: "bounding-box area enclosing all component centers" |
| Block boundary | `compute_block_boundary_violation` | body | member bbox vs `BlockRegion` via `region_map` |
| Inter-block spacing | `compute_inter_block_spacing_violation` | body | per-block bbox union |
| HV creepage keepout | `compute_creepage_violation` | body | edge-to-edge bbox `gap_x`/`gap_y` vs the required creepage |
| Same-domain cohesion | `compute_domain_cohesion` | **centre** | `by_domain.setdefault(domain, []).append((p.x, p.y))`, and the explicit `del footprint_sizes  # unused: cohesion is measured between footprint centres` |
| Weighted-sum aggregation | `_weighted_sum_score` | n/a | |
| Lexicographic aggregation | `_lexicographic_score` | n/a | |
| Per-net weighting | `Net.weight` | n/a | multiplies whichever wirelength term is in use |

Three centre-anchored terms; the rest are body geometry or bookkeeping. Only
the first has been migrated, and only behind a flag.

### 3.2 The multi-fidelity evaluator — `src/kicad_tools/placement/multi_fidelity.py`

| Level | Symbol | Anchoring | Evidence |
|---|---|---|---|
| Fidelity 0 (HPWL) | `_evaluate_fidelity_0` | **centre** | calls `compute_wirelength(placements, nets)` with no pad map |
| Fidelity 1 (+DRC) | `_evaluate_fidelity_1` | **pad** wirelength (M2), **pad** DRC | calls `build_pad_position_map(placements_rich)` and passes it as `pad_positions`; `check_placement_drc` does real pad-to-pad clearance |
| Fidelity 2/3 (routing) | `_evaluate_global_routing` | n/a (inherits the fidelity-1 breakdown) | routability ratio only |

`_evaluate_fidelity_1` was the sharpest instance of the original pattern: it is
*handed* `PlacedComponent` objects with transformed pads (it needs them for
DRC) and still measured wirelength between centres. **M2 fixed this** (§6).
Fidelity 0 remains centre-anchored, since it accepts centre-only placements and
so cannot assume pads exist — which keeps the historical estimate reachable by
asking for `FidelityLevel.HPWL`.

Reachability note (honest scoping): `evaluate_placement_multifidelity` is
exported from `src/kicad_tools/placement/__init__.py` but is not called from any
CLI or MCP surface — `rg -n 'evaluate_placement_multifidelity' src/` matches
only the module itself and that re-export. It is library/test surface today, so
a migration there has lower blast radius *and* lower payoff than §3.1. This is
why M2 needed no opt-in flag while M1 did.

### 3.3 Seeds and priors

| Term | Symbol | Anchoring | Evidence |
|---|---|---|---|
| Force-directed seed | `force_directed_placement` in `src/kicad_tools/placement/seed.py` | **centre** | attraction from `_build_net_adjacency` — a shared-net *count* per component pair, applied to centre positions |
| Random seed | `random_placement` in `seed.py` | n/a | |
| Affinity graph | `build_affinity_graph` in `src/kicad_tools/placement/priors.py` | n/a (topological) | edge weight = shared-net count; pad-blind by construction |
| Schematic-proximity prior | `schematic_proximity_prior` in `priors.py` | **centre** | iterates positions toward the weighted centroid of neighbours |
| GP prior mean | `prior_mean_position` in `priors.py` | **centre** | weighted centroid of neighbour positions |
| Cluster / power-domain detection | `find_clusters`, `detect_power_domains`, `power_domain_clustering` in `priors.py` | n/a (topological) | |

### 3.4 Declarative constraints — `src/kicad_tools/optim/constraints.py`

This is the existing analogue of pcbplace's five-verb vocabulary (relevant to
#4831 item 4, which is **not** in this slice's scope).

| Constraint | Symbol | Anchoring | Evidence |
|---|---|---|---|
| `max_distance` (≈ `near`) | `_validate_max_distance` | **centre** | `dist = math.sqrt((comp.x - anchor.x) ** 2 + (comp.y - anchor.y) ** 2)` |
| `alignment` | `_validate_alignment` | **centre** | axis coordinate of component origins |
| `ordering` | `_validate_ordering` | **centre** | |
| `within_box` (≈ `fixed`/region) | `_validate_within_box` | **centre** | |
| `relative_position` | `_validate_relative_position` | **centre** | |
| Keepout (≈ `keepout`) | `validate_keepout_violations` in `src/kicad_tools/optim/keepout.py` | **centre point** | `cx, cy = fp.position` then `zone.contains_point` |

### 3.5 Other placement surfaces (for completeness)

| Surface | Symbol | Anchoring | Note |
|---|---|---|---|
| `kct placement check` diagnostics | `PlacementAnalyzer` in `src/kicad_tools/placement/analyzer.py` | body | real courtyard polygons; see `docs/placement-scoring.md` |
| Side-aware overlap/boundary | `compute_overlap`, `compute_boundary_violation` in `src/kicad_tools/placement/geometry.py` | body | richer siblings of the `cost.py` versions |
| C++ cost kernels | `compute_overlap_cpp`, `compute_boundary_violation_cpp`, `compute_drc_violations_cpp`, `create_batch_evaluator` in `src/kicad_tools/placement/cpp_backend.py` | body | the accelerated terms are exactly the body-geometry ones — **note for any migration**: there is no `compute_wirelength_cpp`, so a pad-anchored wirelength does not invalidate a C++ kernel |
| `kct placement refine` energy | `PlacementSession._compute_score` in `src/kicad_tools/optim/session.py` | **pad** (indirectly) | sums `total_wire_length()` (pin-anchored, §5) + spacing energy; its own docstring flags it as a physics-simulation energy proxy, explicitly *not* the optimizer objective |

---

## 4. Measured divergence between the two estimators (first-party)

Both estimators were run over the **same** committed placements in
`tests/fixtures/placement/benchmark_boards.json` (decoded through `encode` /
`decode` in `src/kicad_tools/placement/vector.py`, so pad transforms are the
production ones). Re-run at `8a98a69f`; all three rows reproduce exactly as
first measured.

| Fixture board | Components | Nets | `compute_wirelength` (centres) | `compute_hpwl` (pads) | Δ |
|---|---|---|---|---|---|
| `trivial_3_resistors` (known-optimal) | 3 | 4 | 12.000 mm | 7.200 mm | −40.0% |
| `simple_rc_filter` (reference) | 10 | 8 | 127.000 mm | 126.690 mm | −0.24% |
| `medium_mcu_board` (reference) | 20 | 17 | 382.000 mm | 339.230 mm | −11.2% |
| `stress_50_components` | 50 | — | *not measured* | *not measured* | no committed placement — the fixture carries only a `component_generation` block and the board is built programmatically in `tests/test_placement_benchmark.py` |

These three rows are pinned by `tests/test_placement_pad_anchored_wirelength.py`,
so a change to either estimator fails a test rather than silently invalidating
this table.

**Since M5, measuring a board needs no bespoke script.** Any board can be
measured with the shipped CLI, which reports both estimators for the layout as
committed:

```bash
uv run kct optimize-placement board.kicad_pcb --dry-run --format json \
  | jq .wirelength_estimators
# {"centre_anchored_mm": …, "pad_anchored_mm": …, "delta_mm": …,
#  "delta_pct": …, "scored": "centre", "pads_available": true, "pad_count": …}
```

Note the CLI's numbers differ from a direct `compute_hpwl` call on weighted
boards: the CLI honours `Net.weight` on *both* legs (see §6 M5), whereas
`compute_hpwl` ignores it. On the unweighted fixtures in the table they agree.

**What this does and does not show.** It shows the two estimators assign
materially different lengths to the *same* layout (up to 40% on a 3-part board,
11% on a 20-part board) — which bounds how differently they can *rank*
candidate layouts, and is the mechanism by which a pad-anchored objective could
find a different optimum. It does **not** show that optimizing against pads
produces shorter routed copper on our boards; nobody has run that experiment
here. The pad-anchored value happened to be lower on all three fixtures, but
that direction is not guaranteed in general — a pad can sit outside the
bounding box of its own component's centre, and §4.1 shows that happening.

### 4.1 Fleet measurement (M5)

The three-fixture table above was the *whole* evidence base when M1 shipped
opt-in. M5's reporting channel makes the same measurement available on any
board, so here is the committed fleet, measured with the command above over
`boards/0*/output/*_routed.kicad_pcb`. Re-run at `8a98a69f`; every row
reproduces the figures first recorded at `07649039`.

| Board | Pads | Centre-anchored | Pad-anchored | Δ |
|---|---|---|---|---|
| `00-simple-led` | 6 | 34.00 mm | 32.00 mm | −5.88% |
| `01-voltage-divider` | 8 | 58.00 mm | 55.46 mm | −4.38% |
| `02-charlieplex-led` | 34 | 262.00 mm | 274.70 mm | **+4.85%** |
| `03-usb-joystick` | 86 | 586.54 mm | 530.75 mm | −9.51% |
| `04-stm32-devboard` | 85 | 351.00 mm | 353.72 mm | **+0.77%** |
| `05-bldc-motor-controller` | 209 | 1732.00 mm | 1724.24 mm | −0.45% |
| `06-diffpair-test` | 198 | 1115.00 mm | 1097.81 mm | −1.54% |
| `07-matchgroup-test` | 244 | 1285.00 mm | 1233.12 mm | −4.04% |

**The finding that matters for M1's default flip: the sign is not constant.**
Two of eight fleet boards read *longer* at the pads (`02` by +4.85%, `04` by
+0.77%), which the §4 fixture table — three boards, all negative — could not
have revealed. The §4 caveat ("a pad can sit outside the bounding box of its
own component's centre") is therefore not hypothetical on our own boards.

What it still does **not** show: that optimizing against pads produces shorter
routed copper. These are two estimators reading one fixed layout; nobody has
yet run the optimizer both ways on a board and compared the routed result.
That experiment — not this table — is what should decide whether
`--pad-anchored-wirelength` becomes the default. **On the evidence to date, it
should not.**

---

## 5. Already pad-anchored (do not re-litigate as new work)

| Surface | Symbol | Status |
|---|---|---|
| Pad HPWL estimator | `compute_hpwl`, `compute_hpwl_breakdown` in `src/kicad_tools/placement/wirelength.py` | Pad-anchored via `_build_pad_lookup` + `_hpwl_for_net`, which read `pad.x`/`pad.y` from `TransformedPad` in `src/kicad_tools/placement/vector.py`. Tested in `tests/test_placement_wirelength.py` and `tests/test_placement_benchmark.py`. **Still has no call site in `src/`** — M1/M2 delivered pad anchoring through `build_pad_position_map` instead (§1). |
| Pad position map | `build_pad_position_map` in `wirelength.py` | The actual M1/M2 vehicle: turns `PlacedComponent` pads into the `(reference, pad_name) -> (x, y)` map that `compute_wirelength` consumes. |
| Estimator comparison | `compare_wirelength_estimators`, `WirelengthEstimatorReport` in `wirelength.py` | M5's report-only channel. Runs `compute_wirelength` twice — once without a pad map, once with — so anchoring is the *only* difference between the two numbers. |
| Per-footprint ratsnest | `compute_per_footprint_ratsnest` in `wirelength.py` | Pad-anchored (nearest-pad distances). **Is** used in `src/` — but only for *reporting*, not scoring, from `src/kicad_tools/mcp/tools/optimize_placement.py`. (The curator's scope note said it was test-only; it is not — corrected here.) |
| Placement DRC | `check_placement_drc` in `src/kicad_tools/placement/drc.py` | Pad-anchored pad-to-pad clearance, per its own docstring. Wired into fidelity ≥ 1 but **not** into `evaluate_placement`, which uses the bbox-count `compute_drc_violations` instead. |
| Pad transform machinery | `_transform_pad`, `decode` in `vector.py` | Produces absolute pad coordinates for every component on every decode. Pads are populated in both production front-ends (`PadDef` construction in `optimize_placement_cmd.py` and `mcp/tools/optimize_placement.py`). |
| Legacy force-directed placer | `compute_spring_force`, `total_wire_length`, `create_springs_from_nets` in `src/kicad_tools/optim/placement.py` | **Pin-anchored.** `Pin` in `src/kicad_tools/optim/components.py` stores absolute coordinates; `Spring` connects `(comp_ref, pin_num)` pairs. This is the behaviour pcbplace advocates, already shipping in the `optim` engine behind `kct placement refine`. |

Two honest consequences:

- The repo does **not** need to invent pad anchoring; it needs to stop
  discarding it in the newer `placement` package — which M1/M2 have now
  partly done.
- Any migration should state which of the two engines it is aligning, because
  they still disagree: `optim` measures pin-to-pin spring length while
  `placement` measures centre HPWL by default.

One partial exception worth naming: `_create_cluster_springs` in
`optim/placement.py` is *nominally* pin-anchored but picks `anchor_comp.pins[0]`
as "the first pin as proxy for center" — an arbitrary pad, not the electrically
relevant one. That is precisely the distinction pcbplace draws (near *its own
power pad*), so this counts as "pad-addressed but not pad-*meaningful*".

---

## 6. Migration candidates

Each unimplemented stub is a future issue **to be filed by a human / Champion /
Curator**, not by this audit. Benefit rationale is grounded in (a) pcbplace's
reported 59.5 → 12.7 mm wiring reduction from pad anchors, and (b) the
first-party divergence measured in §4 — with the honest caveat from §4
attached to both.

### M1 — Wire pad anchoring into `evaluate_placement` — **LANDED (opt-in)**

> **Shipped as:** `kct optimize-placement --pad-anchored-wirelength` plus the
> `pad_positions` argument on `compute_wirelength` / `evaluate_placement`.
> Implementation note: rather than calling `compute_hpwl` directly (which
> would have dropped `Net.weight`, and which `cost.py` cannot import without
> a cycle — `wirelength.py` imports `cost.py`), the pad coordinates are
> passed *into* `compute_wirelength` as a `(reference, pad_name) -> (x, y)`
> map built by `build_pad_position_map`. That keeps per-net weighting, keeps
> one net-iteration code path, and adds per-pin fallback to the component
> centre. The counter-note below about silently dropping `Net.weight` is
> therefore **resolved, not carried**.
>
> **Remaining M1 tail, deliberately deferred:** the MCP front-end
> (`_evaluate_vector` in `src/kicad_tools/mcp/tools/optimize_placement.py`)
> still discards its pads, and pad anchoring is opt-in rather than the
> default. Flipping the default needs fleet evidence — which M5 has now
> produced, and which **argues against flipping it** (§4.1: two of eight
> boards measure *longer* at the pads). The open question is no longer
> "measure the fleet" but "run the optimizer both ways and compare routed
> copper".

> **Stub title (as filed):** `feat(placement): score the optimizer objective on pad-anchored HPWL`
> **Scope:** give `evaluate_placement` access to transformed pads and use them
> for the wirelength term, keeping the centre-anchored path as the fallback
> when pads are absent. Update the benchmark expectations in
> `tests/test_placement_benchmark.py` and `docs/placement-scoring.md`.

**Expected benefit.** This is the term pcbplace identifies as the source of its
59.5 → 12.7 mm improvement. §4 shows the two estimators already disagree by
0.2-40% on our own fixtures, so the objective's ranking of candidate layouts
genuinely changes. It also makes rotation meaningful to wirelength for free:
under the centre-anchored path, rotating a part changes `compute_wirelength` by
exactly zero (only `p.x`/`p.y` are read), whereas pad coordinates rotate with
the part via `_transform_pad` — so the optimizer was searching a rotation
dimension the wirelength term could not see.

**Cost/risk.** This changes the score of *every* placement run (GA/BO/CMA-ES
trajectories, snapshots, board pipelines) — which is exactly why it shipped
opt-in. The pads are already computed and discarded, so the added compute is one
dict build per evaluation, not a new transform pass.

### M2 — Stop discarding pads in `_evaluate_fidelity_1` — **LANDED (unconditional)**

> **Shipped as:** `_evaluate_fidelity_1` in
> `src/kicad_tools/placement/multi_fidelity.py` now builds
> `build_pad_position_map(placements_rich)` and passes it to
> `compute_wirelength`. Fidelity ≥ 1 is therefore pad-anchored; fidelity 0
> (which accepts centre-only placements) is unchanged, so the historical
> estimate is still reachable by asking for `FidelityLevel.HPWL`. Levels 2
> and 3 build on the fidelity-1 breakdown and inherit the pad-anchored term.
>
> **Flag decision — unconditional, unlike M1.** M1 is opt-in because
> `evaluate_placement` is the objective of every production
> `kct optimize-placement` / MCP run. The multi-fidelity module is
> library/test surface (§3.2) — nothing else under `src/` calls it. Adding a
> second opt-in switch here would have bought no safety and left the
> information loss in place by default.
>
> **Implementation note.** As in M1, the pad coordinates are passed *into*
> `compute_wirelength` rather than calling `compute_hpwl` as this stub
> originally proposed. `compute_hpwl` ignores `Net.weight`, so a literal swap
> would have silently dropped per-net weighting at level 1 while level 0 kept
> honouring it. Only the wirelength term moves — overlap/boundary/area/DRC
> are untouched (§7).
>
> **Tests:** `tests/test_multi_fidelity_pad_anchored.py` (level 0 vs level 1 on
> the same input, rotation visible only at level 1, `Net.weight` survival,
> missing-pad fallback, level-2 inheritance, determinism).

> **Stub title (as filed):** `refactor(placement): use pad HPWL at multi-fidelity level 1, where pads are already required`
> **Scope:** in `_evaluate_fidelity_1`, measure wirelength at the pads the
> function is already required to carry, instead of at the centres it
> currently projects down to.

**Expected benefit.** Same mechanism as M1 (pcbplace's 59.5 → 12.7 mm; §4's
measured divergence), at the lowest possible cost: fidelity ≥ 1 already
*requires* `PlacedComponent` with transformed pads and already uses them for
DRC. The projection down to centres was pure information loss.

**Honest scope note.** Payoff today is small because nothing under `src/` calls
`evaluate_placement_multifidelity` (§3.2) — this is library/test surface. Its
value was as a low-risk pathfinder for M1, not as a user-visible win.

### M3 — Pad-address the `max_distance` constraint

> **Stub title:** `feat(optim): allow max_distance constraints to target a pad, not a component centre`
> **Scope:** extend `SpatialConstraint.max_distance` in
> `src/kicad_tools/optim/constraints.py` to accept `anchor="U1.14"`-style pad
> addressing, and teach `_validate_max_distance` to measure to the named pad
> instead of the anchor component's origin.

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
> **Scope:** in `_create_cluster_springs` in `src/kicad_tools/optim/placement.py`,
> replace the `pins[0]` "proxy for center" choice with the pad on the net the
> cluster is formed around.

**Expected benefit.** Same pcbplace mechanism at the cluster level: the spring
already pulls *a* pad (so the machinery exists — §5), just not the meaningful
one. Because the surrounding engine is already pin-anchored, this is a local
fix with no objective-wide blast radius.

**Honest counter-note.** Smaller and less certain than M1-M3; cluster springs
are a proximity heuristic, and `pins[0]` is often already the relevant pad on
two-pad passives. File only if the `optim` engine remains in use.

### M5 — Report both estimators before switching either — **LANDED (report-only)**

> **Shipped as:** `compare_wirelength_estimators` returning a frozen
> `WirelengthEstimatorReport` in `src/kicad_tools/placement/wirelength.py`,
> re-exported from `src/kicad_tools/placement/__init__.py` and consumed by two
> surfaces:
>
> - `kct optimize-placement --dry-run` prints a `Wirelength estimators: …`
>   line and, under `--format json`, emits a `wirelength_estimators` block
>   (`centre_anchored_mm`, `pad_anchored_mm`, `delta_mm`, `delta_pct`,
>   `scored`, `pads_available`, `pad_count`) — documented in
>   `docs/reference/machine-output.md`;
> - the MCP `evaluate_placement` response carries the same block, rounded to
>   4 dp, with `scored: "centre"` (that front-end is still centre-anchored —
>   the M1 tail).
>
> **Implementation note — both legs go through `compute_wirelength`.** The
> stub below proposed emitting `compute_hpwl` next to `CostBreakdown.wirelength`.
> That would have made the reported delta a *mixture* of two changes, because
> `compute_hpwl` ignores `Net.weight` while `CostBreakdown.wirelength` honours
> it: on any board using `--anchor-weight`, part of the "pad anchoring saves
> X mm" headline would actually have been "weighting silently vanished".
> Calling `compute_wirelength` twice — once without a pad map, once with —
> makes anchoring the only difference between the two numbers, which is the
> whole point of the measurement.
>
> **`scored` is a label, not a switch.** It records which estimator the
> caller's objective used, so a fleet aggregation can tell a measured board
> from a scored one. Both numbers are computed from the layout either way;
> `--pad-anchored-wirelength` changes which one lands in
> `CostBreakdown.wirelength`, never the reported pair.
>
> **`pads_available` guards a false negative.** A board decoded without pad
> geometry makes the two estimators identical by construction. Reporting that
> as a 0.00 mm delta would read as "pad anchoring buys nothing here"; the flag
> (and `pad_count`) marks it as *not measured* instead.
>
> **Deliberately not covered:** the full (non-`--dry-run`) optimize run's final
> score. `--dry-run` scores the layout as committed, which is exactly the fleet
> measurement M1's default flip needs; adding a second emission point inside
> the optimize path would widen the JSON contract without adding evidence.
>
> **Tests:** `tests/test_placement_wirelength_estimator_report.py`.

> **Stub title (as filed):** `feat(placement): report pad-anchored HPWL alongside the centre-anchored score`
> **Scope:** report the pad-anchored estimate next to `CostBreakdown.wirelength`
> in `kct optimize-placement --dry-run` and the MCP `evaluate_placement`
> response, without changing what is optimized.

**Expected benefit.** Converts §4's three-fixture measurement into fleet-wide
evidence at zero behavioural risk, so M1's real payoff (or absence) can be
argued from our own boards rather than from pcbplace's reported
59.5 → 12.7 mm. This was the cheapest way to de-risk M1 — and §4.1 shows it
earned its keep: the fleet contradicts the fixture set's uniformly-negative
delta, so M1's default flip is now argued *against* by first-party data.

**Honest counter-note (as written, before the build).** Purely additive
telemetry — it improves nobody's placement by itself, and if M1 is going to be
built regardless, M5 is skippable. *Resolution:* M1 shipped **opt-in**
precisely because the default flip lacked fleet evidence, so M5 stopped being
skippable and became the gate on M1's remaining tail. It still improves nobody's
placement on its own — that is the point of "report-only".

---

## 7. Declined (no migration recommended)

| Term | Why not |
|---|---|
| `compute_area` in `cost.py` | Centre-anchored, but it is a *compactness proxy*, not a connectivity term. A pad-anchored area (bbox of all pads) would mostly re-measure total footprint extent, which `compute_overlap` / `compute_boundary_violation` already constrain. pcbplace's write-up identifies no area/compactness pad-anchoring win. Re-proposing this needs new evidence, not just the observation that it reads `p.x`. |
| `compute_domain_cohesion` in `cost.py` | Centre-anchored by explicit design — its docstring and the `del footprint_sizes` line state that domains are clustered as a radius-of-gyration over centres. A voltage *domain* is a property of the whole part (an HV part's every pad is HV), so a pad-anchored variant would compute nearly the same field at N-pads cost. Decline unless a mixed-domain part (isolator, opto) motivates per-pad domains — which is a different feature, not a re-anchoring. |
| `compute_overlap`, `compute_boundary_violation`, `compute_drc_violations`, the block terms, `compute_creepage_violation` in `cost.py` | Body geometry, not net geometry. Pad anchoring is undefined for them. Note the DRC term *does* have a pad-accurate sibling (`check_placement_drc`) — unifying those is a **precision** question, not a pad-anchoring question, and is out of scope here. |
| `build_affinity_graph`, `find_clusters`, `detect_power_domains` in `priors.py` | Purely topological (shared-net counts). No coordinates read; nothing to anchor. |
| `force_directed_placement` in `seed.py`; `schematic_proximity_prior`, `prior_mean_position` in `priors.py` | Centre-anchored, but these produce *seeds/priors* that the objective then optimizes. Pad-anchoring a seed buys at most a slightly better starting point for a search whose own objective is still centre-anchored by default. Revisit only after M1's default flips; migrating them first is optimizing the wrong end. |
| `validate_keepout_violations` in `keepout.py` | Centre-*point* containment, so a part straddling a zone edge is missed. That is a body-geometry precision gap, not a pad-anchoring gap; recorded here so a future reader does not file it under this heading. |

---

## 8. "Anchor" in this repo means three other things

None of these is pcbplace's pad anchoring. Do not cite them as prior art.

1. **`--anchor-weight` (net weighting around locked parts).**
   `_compute_net_anchor_weight` in `src/kicad_tools/cli/optimize_placement_cmd.py`
   (and its twin in `src/kicad_tools/mcp/tools/optimize_placement.py`) returns
   `1.0 + anchor_weight * fraction` for nets touching a `(locked)` footprint,
   where `fraction` is `anchored_pins / total_pins`. "Anchored" here means
   *immovable*. The distance being weighted is still whichever HPWL the
   objective is configured for — centre-to-centre by default.
2. **`--placement-feedback-anchor` / `--placement-feedback-no-anchor`.**
   `_auto_detect_anchored_refs` and `_resolve_placement_feedback_anchors` in
   `src/kicad_tools/cli/route_cmd.py` compute the set of refs the
   route↔placement feedback loop may not move; the set is passed as
   `fixed_refs`. Again: immovability, not pads.
3. **`SpatialConstraint.max_distance(anchor=…)`.** Here `anchor` in
   `src/kicad_tools/optim/constraints.py` names the *reference component* of a
   proximity constraint — and, per §6 M3, it is measured centre-to-centre,
   making it a migration target rather than prior art.

pcbplace's sense — *a constraint expressed relative to a specific pad rather
than to the part origin* — has no existing name in this codebase. If a future
slice needs one, "pad-anchored" (as used throughout this document) is the term
to standardise on, kept clearly distinct from the "locked/immovable" sense
above.

---

## 9. Stub index

| Stub | Title | Status / depends on |
|---|---|---|
| M1 | `feat(placement): score the optimizer objective on pad-anchored HPWL` | **LANDED opt-in** (`--pad-anchored-wirelength`); MCP front-end + default-on remain, and §4.1 currently argues against default-on |
| M2 | `refactor(placement): use pad HPWL at multi-fidelity level 1, where pads are already required` | **LANDED unconditional** (fidelity ≥ 1 pad-anchored; fidelity 0 unchanged) |
| M3 | `feat(optim): allow max_distance constraints to target a pad, not a component centre` | Unfiled; overlaps #4831 item 4 |
| M4 | `fix(optim): pick the shared-net pad instead of pins[0] for cluster springs` | Unfiled; only if the `optim` engine stays in use |
| M5 | `feat(placement): report pad-anchored HPWL alongside the centre-anchored score` | **LANDED report-only** (`wirelength_estimators` on `--dry-run` + MCP `evaluate_placement`) |

Related documents: `docs/placement-scoring.md` (why the placement scoring
surfaces intentionally disagree, issue #3940) and
`docs/board-fleet-parity-audit-2026-06.md` (the audit-doc precedent this file
follows).
