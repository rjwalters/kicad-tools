# Clearance-kernel epic (#5509): acceptance audit

Audit for [#7577](https://github.com/rjwalters/kicad-tools/issues/7577), the operator-authorized
bounded audit of epic [#5509](https://github.com/rjwalters/kicad-tools/issues/5509)
(*one clearance kernel -- search, commit and final validation must agree with kicad-cli DRC*).

This is an evidence-to-criteria reconciliation built from **existing artifacts only**. It is not a
fresh measurement, not a board certification, and not a closeout of #5509.

## Provenance and limits

| item | value |
|---|---|
| Tree inspected | `5d2a52bd3ee2b91f284ed10875027d6206c8f806` (`origin/main`, 2026-10-20). #7577 cites `3e7c5310a`; main has since advanced and the audit reads the newer tree. |
| `docs/clearance-conformance.md` | Last changed in `e3ebf91f1` (2026-10-01). It stamps **no** generating SHA (by design, so it regenerates byte-identically from the seed range), so its percentages are historical rows, **not** a run of the tree above. |
| Tests run for this audit | **None.** Every verdict below comes from reading source, committed docs, committed board evidence and issue state. |
| Native router extension in the audit worktree | **Not installed** (`uv run kct build-native --check` -> `C++ backend: not installed`; placement and DRC backends also not installed). Nothing here exercised `router_cpp`. |
| kicad-cli on the audit host | `/usr/bin/kicad-cli`, 10.0.6 (matches the CI image pin noted in `.github/workflows/ci.yml`). It was **not** invoked. |
| Not examined | The reasons board 09 is `blocked`; every epic PR's diff line-by-line; `tests/router/test_board_route_determinism.py` results. |

Verdict vocabulary: **met** (an artifact demonstrates the criterion and I read it), **unmet** (an
artifact shows the criterion does not hold), **not verified** (no artifact in hand establishes it;
this includes "an artifact exists but I did not or could not check what it claims"). A closed issue
and the existence of a file are not receipts.

## How to read the conformance numbers

`docs/clearance-conformance.md` reports each consumer at **its own rule values**, and separately gates
migrated groups at **the project netclass clearance kicad-cli applies**. Three axes get conflated if
this is not kept straight:

1. **Geometry at matched rule values** -- gated for migrated groups (`MIGRATED_GROUPS` in
   `tests/conformance/report.py:309`: 1, 2, 4, 5, 6, 7, 8, 9, 10, 15, 16, 17, 18).
2. **Production rule selection** -- which value a real `kct route` run resolves. The adapters hard-wire
   the router's `trace_clearance` (0.15 mm) against kicad-cli's `Default` netclass (0.20 mm), so the
   corpus does **not** exercise the Phase 2 resolver. The large "under-reject" cells (26-49 %) are
   attributed in the doc to that rule axis, not to geometry.
3. **Full board validation** -- native DRC on committed boards, which is outside the corpus entirely.

A pass on axis 1 proves nothing about axis 2 or 3.

## Epic #5509 criterion-to-evidence table

Sources of criteria: the epic body's Phases and Success Criteria, and the curator's section 7
"Acceptance criteria additions".

| # | criterion | artifact / evidence | revision / tool identity | coverage | verdict | reason |
|---|---|---|---|---|---|---|
| E1 | Success: conformance suite, 0 disagreements with kicad-cli across the fuzz corpus | `docs/clearance-conformance.md` "kernel" control row: 0.0 % / 0.0 % (0/1336 pairs, seeds 0-199); consumer table | doc regenerated at/before `e3ebf91f1`; kicad-cli version not stamped in the doc; generator `tests/conformance/generator.py` | kernel: full 0-199 (historical). Merge gate: seeds `CI_SEEDS = (0, 1, 2)` for consumers (`test_corpus.py:72`), `range(6)` for corpus truth (`test_corpus_truth.py:47`) | **Unmet** for the criterion as written; kernel row **not verified** at HEAD | Six of 19 consumer groups (3, 11, 12, 13, 14, 19) are report-only and their rows are auto-`xfail(strict=False)`; their documented rates are non-zero (e.g. 12/13: 45.3 % under-reject). The kernel itself is 0/0 historically but was not re-run. |
| E2 | Success: no in-repo board route "completes" and then fails the final clearance gate | Committed `boards/0N-*/output/readiness.json` + `readiness*/native-drc*.json` + `kct-check.json` (see "Board evidence" below) | Boards 00-03: engine commit recorded, kicad-cli 10.0.6, `dirty: true`. Boards 04-07: **no `engine` block**, no kicad-cli version recorded | Boards 00-07: `status: ready`, native DRC violations 0, kct check errors 0. Board 08: no `readiness.json` found. Board 09: `blocked`, 5 blockers (not examined) | **Not verified** | Supportive saved results, but 04-07 do not record which kicad-cli produced them, 00-03 were produced from a dirty tree, and the native-toolchain requirement in project CLAUDE.md ("Native evidence") cannot be confirmed from the artifacts. Nothing here certifies any board. |
| E3 | Ongoing regression objective (operator ruling 2026-10-19): new "router: preserve/honor/validate physical X" findings get reproducible fixtures and tracked fixes | Recorded verbatim in "Finite-closeout interpretation" below | -- | -- | **Recorded (not a pass/fail row)** | Replaces the original zero-future-issues criterion. Does not erase present findings or waive the concrete rows. |
| P1 | Phase 1 acceptance: per-consumer disagreement table in `docs/`, with #5398 and #5410 as named fixtures | `docs/clearance-conformance.md`; fixtures under `tests/fixtures/conformance/` listed in the doc's "Named fixtures" | Table generated by `uv run python -m tests.conformance.report --seeds 0-199` | 19 rows + kernel control row; four named fixtures: `issue5398-seg-via-0p18-order`, `issue5410-dqs-n-halo-vs-legal-via`, `search-vs-commit-seg-via-max`, `roundrect-corner-gap` | **Met** (on source inspection) | Table, fixtures and per-row `notes` columns exist. Not re-run. |
| P2a | Phase 2: the #5398 0.15/0.20 case resolves to 0.20 everywhere | `tests/router/test_clearance_resolver.py` (`test_issue5398_resolves_to_020_for_every_pair_kind`, `..._through_the_route_cli`), `src/kicad_tools/router/clearance_resolver.py`, `cli/route_cmd.py` `_decide_route_clearance` (thin adapter over the resolver), `tests/router/test_via_clearance_resolution_6280.py`, `test_route_auto_clearance_resolution_6283.py` | Source at `5d2a52bd3`; tests not run | Unit level, CLI entry point included | **Not verified** end-to-end | The resolver is unit-tested, but the conformance corpus never feeds the resolved value into the consumer rows (axis 2 above), so no artifact shows "0.20 everywhere" across consumers. The doc rows still describe the 0.15 vs 0.20 gap as an open "#5398 / #5654 defect" although #5645 and #5654 are closed. |
| P2b | Phase 2: order-asymmetry test passes in both orders | `test_clearance_resolver.py::test_resolution_is_independent_of_argument_order` (resolver); `tests/router/test_clearance_kernel_fixtures.py::test_issue5398_is_rejected_in_both_insertion_orders` (kernel, plus a `router_cpp` twin); fixture `issue5398-seg-via-0p18-order` | not run | Resolver and kernel level. The **commit gates themselves** (groups 12/13) are measured "via-first" only | **Not verified** for the commit gates | Resolver and kernel symmetry are tested; whether the Python/C++ commit validators (`grid.py:4930/5249/5347`, `grid.cpp:1664`) are order-symmetric is not shown by any conformance row (both are unmigrated and via-first only). |
| P2c | Phase 2: #4507 Python/C++ pairwise scope matches | `test_clearance_resolver.py::test_resolver_matches_the_cpp_pairwise_scope`, `tests/router/test_pairwise_cpp_parity.py`, `test_coupled_kernel_4507.py`; #4507 closed 2026-09-23 | not run; native ext not built here | Pairwise scope; group 14 row is "Scalar (dru) behaviour", the HV domain-matrix path (`Grid3D::pairwise_required_clearance`) is **not measured** | **Not verified** | Parity tests need `router_cpp`, which this host lacks. HV/domain-matrix path has no oracle corpus (doc says "Phase 2's corpus"; none found). |
| P3a | Phase 3: search-time reject of a candidate the validator accepts is a test failure | Groups 1, 2, 4-10 gated in `MIGRATED_GROUPS`; `test_corpus.py::test_adapter_agrees_with_kicad_cli`, `test_migrated_consumer_items_are_hard_gates` | Gate: `CI_SEEDS (0,1,2)`, requires kicad-cli + adapter | Migrated groups are compared with **kicad-cli directly**, not with the validator. Group 3 (C++ A* blocked-cell kernel, the A* acceptance read side) is **not** migrated | **Partially met** | Stronger than the criterion for gated rows (ground truth, not the validator). But group 3 is report-only (52.0 % over-reject at its own rule values) and the validators (12/13) are themselves ungated, so "search vs validator" is not pinned for the real A* acceptance path. Groups 1/2 carry a pinned quantisation ledger (`QUANTISATION_LEDGER`, 5 entries) rather than zero over-rejection. |
| P3b | Phase 3: #5410's DQ3 sequence routes without the indexed-queue workaround | #5410 is **OPEN**, labelled `loom:blocked`; related #5333 (Board07 coupled pre-pass) open `loom:blocked` | issue state 2026-10-20 | -- | **Unmet** | The tracking issue for the concrete board symptom is open. The geometry half is covered by fixture `issue5410-dqs-n-halo-vs-legal-via`, but that fixture's own consumer row still rejects because of the adapter's both-sides dilation (documented, quantisation). |
| P3c | Phase 3: no reach regression on boards 00-07 | `tests/router/test_board_route_determinism.py`, board baselines | not run | -- | **Not verified** | Epic scope guard 2 names this gate on every Phase 3 PR. No saved result was read for this audit; PR check history was not examined. |
| P4a | Phase 4: `kct check` and `kicad-cli pcb drc --refill-zones` report the same clearance error count on boards 00-07 | Group 18 row (`kct_check`) gated on five pair kinds (seg-seg, seg-via, via-via, pad-seg, pad-via) plus copper-edge; committed `kct-check.json` and `native-drc*.json` per board | Group 18 percentages are the **pre-migration** measurement ("not yet re-measured at that scale"); board evidence per E2 | Zone pairs excluded from group 18. Board evidence shows kct errors 0 and native violations 0 on 00-07 | **Not verified** | Equality of two zeros is a vacuous receipt for this criterion; no board in the saved evidence has a non-zero clearance count to compare. Whether refill state matches is recorded only for 00-03 (`refilled-native/`). |
| P4b | Phase 4: duplicate arithmetic deleted (hermit pass) | #5858 closed 2026-10-17 with slices #6960, #6963, #6994 (also closed); `router/search_radius.py`, `router/clearance_resolver.py`, `router/clearance_kernel.py` | tree `5d2a52bd3` | See "Post-Phase-4 arithmetic boundary" | **Partially met** | Radius arithmetic was unified (slices 2-4) and consumers 15-18 were switched. Survivors are real and not catalogued in any committed doc. |
| C1 | Phase 1a table lists every group (19 rows) with a rate and corpus size; unmeasured groups listed as "not measured" | Doc status line: "19 of 19 consumer groups are wrapped by an adapter" | doc at `e3ebf91f1` | 19 rows, each with `200 cases (seeds 0-199)` | **Met** (on source inspection) | Several rows carry explicit "Not measured" sub-entry-points (see group table). |
| C2 | The #5398 seg/via case, #5410 DQ3/DQS_N case and the search-vs-commit asymmetry are committed fixtures that fail against at least one current consumer and pass against kicad-cli | `tests/fixtures/conformance/*`, `tests/conformance/test_named_fixtures.py`, doc "Named fixtures" | not run | Four fixtures | **Partially met** | Fixtures are committed and pinned to kicad-cli's verdict. The "fails against at least one current consumer" half has intentionally lapsed for `search-vs-commit-seg-via-max` (the doc says both resolve 0.15 mm and accept since #5661); it now guards against regression instead. |
| C3 | After Phase 4 the `grep -rn "clearance"` check shows threshold selection only in the resolver and geometry only in the kernel; survivors documented | `grep -rl clearance src/kicad_tools --include=*.py --include=*.cpp --include=*.hpp` -> 347 files; `docs/` has no survivor list | tree `5d2a52bd3` | Count only; no per-file triage was done | **Unmet** (not documented) / **not verified** (whether the property holds) | The word `clearance` is pervasive, so the grep cannot by itself pass. The criterion requires a documented list of deliberate survivors; none exists. See next section. |
| C4 | No epic PR changes any value in `mfr_limits.py`, `rules.py` constants, or a board `.kicad_dru` | `git log --since=2026-09-16 -p -- src/kicad_tools/router/mfr_limits.py` / `router/rules.py` / `boards/**/*.kicad_dru`, scanned for `min_clearance=` and constant lines | tree `5d2a52bd3` | Scan limited to clearance-valued lines on those paths | **Not verified** (no counter-evidence found) | No `min_clearance=` table-value edit and no `.kicad_dru` clearance **value** edit turned up in the scan; `.kicad_dru` clearance lines in `9012e027c` are deletions of sidecars (receipts), not value changes. Separately, non-epic `76f5d366b` (#6561) made `resolve_clearance` layer/copper-aware, which changes the *selected* fab floor (e.g. jlcpcb 4-layer 0.1016 mm instead of the flat 0.127 mm) when `layers` is passed; the owner should confirm that is intended under the epic's "values read-only" guard. |

## Consumer-group reconciliation (19 rows)

Percentages are the doc's historical rows at each consumer's **own** rule values (seeds 0-199, 400 kicad-cli
invocations, refilled and as-is). "Gated" = in `MIGRATED_GROUPS`, so a geometry disagreement at project
rules fails the suite on `CI_SEEDS (0, 1, 2)`. "Report-only" = auto-`xfail(strict=False)`.

| group | consumer | status | over / under-reject (own rules) | entry points / pair kinds the row does not cover | verdict |
|---|---|---|---|---|---|
| 1 | Python grid halo marking (`occupancy`) | Gated (3a) | 51.2 % / 0.0 % | Cell-set answer, no mm gap; quantisation ledger (3 entries on seeds 0-2) records residual over-rejection | Met for under-rejection; over-rejection is a **pinned exception**, not zero |
| 2 | C++ grid halo marking | Gated (3a) | 52.0 % / 0.0 % | Routed copper only (grid never marks pads); ledger 2 entries | Same as 1 |
| 3 | C++ A* blocked-cell kernel | **Report-only** | 52.0 % / 0.0 % | `is_via_blocked` sibling via candidates | Not verified (ungated) |
| 4 | Python route-halo geometry | Gated (3b) | 0.0 % / 43.0 % | Under-reject attributed in doc to the 0.15-vs-0.20 rule band (asserted pair-by-pair) | Met on geometry; rule selection not covered |
| 5 | C++ route geometry | Gated (3b) | 0.0 % / 43.0 % | `Pathfinder` wrappers unbound, not measured | Met on geometry |
| 6 | Fixed-copper predicate | Gated (3f) | 0.0 % / 36.8 % | Both halves driven | Met on geometry |
| 7 | C++ coupled rail check | Gated (3c) | 0.0 % / 43.0 % | **Pad pairs not measured** | Met on geometry for measured kinds |
| 8 | Python coupled / diff-pair predicates | Gated (3c) | 0.0 % / 26.7 % | `find_intra_pair_clearance_violations` **not measured** (corpus declares no diff pairs) | Met on geometry for measured entry points |
| 9 | Lattice obstacles | Gated (3d) | 0.0 % / 43.0 % | Routed copper only; `pairwise=None` path | Met on geometry |
| 10 | Mesh obstacles | Gated (3e) | 0.0 % / 46.6 % | Seg-candidate pairs only; committed vias and `fixed_fills` not in this row; `copper-edge` residual is a rule reading | Met on geometry for measured kinds |
| 11 | Escape router / stitcher (`via_clearance.py`) | **Report-only** | 2.9 % / 35.9 % | `_min_clearance_to_neighbors`, `_validate_segment_relaxed` **not measured** | Not verified (ungated) |
| 12 | Python commit gate (`grid.py` validators) | **Report-only** | 0.0 % / 45.3 % | **Via-first insertion order only** (other order = #5398) | Not verified; order symmetry unshown |
| 13 | C++ commit gate (`Grid3D::validate_route`) | **Report-only** | 0.0 % / 45.3 % | Via-first only; pads carry no roundrect/oval flag (`roundrect-corner-gap` mechanism) | Not verified; known pad-model gap |
| 14 | Pairwise net-class matrix | **Report-only** | 0.0 % / 48.8 % | `Grid3D::pairwise_required_clearance` (dormant until a domain matrix is set) **not measured** | Not verified (ungated; HV path unmeasured) |
| 15 | Optimizer collision checks | Gated (4a) | 12.5 % / 14.3 % | Percentages measured **before #5625**; `ignore_overflow` left at stricter default | Gated, but row numbers are stale and non-zero as published |
| 16 | Match-group tuning | Gated (4b) | 0.0 % / 26.2 % | Percentages are **pre-migration**; `_post_insertion_clearance_detail_pair_group` **not measured** | Gated; published row stale ("follow-up full regen pending") |
| 17 | DRC nudge repair | Gated (4c) | 0.0 % / 46.5 % | `_via_drill_overlaps_bbox`, `_via_edge_sweep_clear` **not measured** | Met on geometry for the measured gate |
| 18 | `kct check` ClearanceRule | Gated (4d) for five non-zone kinds | 4.7 % / 0.0 % | **Zone pairs excluded**; pre-migration percentages; regen pending | Gated; zone pairs and fresh rates not verified |
| 19 | Incremental placement DRC (`drc_cpp`) | **Report-only** | 0.0 % / 47.6 % | Pad model is a `max(w,h)/2` disc; short-side pad pair kind **not measured** | Not verified (ungated) |

Totals: **13 gated, 6 report-only** (3, 11, 12, 13, 14, 19). Control row (kernel itself): 0/0 historically.

Other cross-cutting facts:

- **Intentional exceptions.** `QUANTISATION_LEDGER` (`tests/conformance/test_corpus.py:114`) holds 5 pinned
  over-rejection entries (groups 1 and 2, seeds 0-2), and `test_named_fixtures.FIXTURE_QUANTISATION_LEDGER`
  holds the fixture analogue. They are deliberate; a vanished or new entry fails the gate. They are not
  a relaxation of any clearance value. `REFINEMENT_GATED_GROUPS` is empty.
- **Boundary band.** Zero boundary pairs were reported in every row (corpus note "Pairs in the boundary band: 0").
  The tolerance policy (scope guard 6) is therefore not exercised near the +/-1 um threshold by this corpus.
- **Fill state.** Both as-is and refilled runs are recorded; zone pairs are scored **only on the refilled
  run**, and the doc explains KiCad's refill backs off by the applied clearance, so zone rows cannot show
  under-rejection by construction.
- **Native extension.** `requires_adapter` / `requires_kicad_cli` skip rather than fail when a backend or
  kicad-cli is missing. A run on a host like this one (no `router_cpp`) would skip the native-backed
  adapter rows instead of failing them; CI runs inside `kicad/kicad:10.0` (digest-pinned, 10.0.6).
- **Corpus size.** Gate runs use 3 seeds (`test_corpus.CI_SEEDS`) while published rates use 200; a
  disagreement that appears only on seeds 3-199 would not redden the merge gate.

## Post-Phase-4 arithmetic and deletion boundary

Delivered and readable in the tree: the kernel (`router/clearance_kernel.py` + native twin, parity-tested by
`tests/router/test_clearance_kernel_parity.py`); the resolver (`router/clearance_resolver.py`);
`router/clearance_shapes.py` and `validate/clearance_shapes.py` as the shape-building layer; one search-radius
helper (`router/search_radius.py`, #6960/#6994); consumers 15-18 switched (#5854-#5857).

**Intentional or evident survivors with their own arithmetic** (not catalogued by any committed doc; this list
is from the conformance report and a spot read, not an exhaustive triage):

- Commit validators `grid.py` `validate_segment_clearance` (:4930), `validate_via_clearance` (:5249),
  `validate_via_to_via_clearance` (:5347) and `Grid3D::validate_route` (`grid.cpp:1664`). `grid.py` imports the
  kernel's `copper_gap`, but groups 12/13 remain report-only, so whether the validators' verdicts now equal the
  kernel's is not pinned by the oracle.
- `router/via_clearance.py` escape/stitch predicates (group 11).
- `router/pairwise_clearance.py` / `Grid3D::pairwise_required_clearance` (group 14).
- `drc/cpp/src/drc_clearance.cpp` / `drc/cpp_backend.py` placement DRC, intentionally a conservative disc
  (group 19). #5857's title says it would "resolve drc/cpp exemption"; the doc keeps group 19 report-only.
- Pre-existing deferrals recorded on #5509 and not filed: the `diffpair_routing.py:5569-5574` routed-halo mark
  (`_remark_route_cells`) and the "via search radius uses `via_clearance` alone" lead (sites 2, 11, 14, 16, 21).

No new exemptions are invented here, and no clearance value, tolerance, allowance, threshold or required
coverage is relaxed by this document.

## Board evidence (existing saved artifacts)

Read from `boards/0N-*/output/readiness.json` and the files it references. This restates saved results; it is
not a qualification.

| board | `status` | `checked_at` | native DRC violations | kct check errors | recorded toolchain |
|---|---|---|---|---|---|
| 00-simple-led | ready | 2026-10-19T20:30Z | 0 (as-is and refilled) | 0 | kicad-cli 10.0.6; `dirty: true` |
| 01-voltage-divider | ready | 2026-10-18T22:00Z | 0 (as-is and refilled) | 0 | kicad-cli 10.0.6; `dirty: true` |
| 02-charlieplex-led | ready | 2026-10-19T23:30Z | 0 (as-is and refilled) | 0 | kicad-cli 10.0.6; `dirty: true` |
| 03-usb-joystick | ready | 2026-10-19T22:00Z | 0 (as-is and refilled) | 0 (1 warning) | kicad-cli 10.0.6; `dirty: true` |
| 04-stm32-devboard | ready | 2026-10-19T23:30Z | 0 | 0 | **not recorded** |
| 05-bldc-motor-controller | ready | 2026-10-19T20:30Z | 0 | 0 | **not recorded** |
| 06-diffpair-test | ready | 2026-10-19T21:00Z | 0 | 0 | **not recorded** |
| 07-matchgroup-test | ready | 2026-10-19T09:00Z | 0 | 0 | **not recorded** |
| 08-precision-acquisition | no `readiness.json` found | -- | -- | -- | -- |
| 09-usbc-pd-power | blocked (5 blockers, not examined) | 2026-10-08 | -- | -- | -- |

Independent gates that this epic must not stand in for, and which stay open: **#5410** (route-halo physical
refinement, `loom:blocked`), **#3438** (Board07 / DDR bundle, `loom:epic`, open) with the related **#5333**, and
the native-DRC evidence requirements in project CLAUDE.md ("Native evidence: produce it on the pinned KiCad
toolchain"). The audit does not claim that this epic's implementation certifies any board.

## Finite-closeout interpretation (operator ruling, verbatim)

> The original 'Zero new router: preserve/honor/validate physical X issues needed after Phase 3' is now an
> ongoing regression objective, not a permanent closeout condition about all future bugs. New findings still
> require reproducible fixtures and appropriate tracked fixes. All concrete conformance, rule-selection and
> board-validation requirements remain in force.

Source: operator comment on #5509 (2026-10-19 Pacific), and the updated Success Criteria in its body. Audit
completion alone does not establish that the remaining criteria pass and does not authorize closing #5509.

## Missing evidence and smallest next checks

All items reuse an existing owner; nothing here opens a campaign. "Executor" is who should run it; cost is an
estimate from artifacts, or **unknown**.

| # | gap | smallest justified check | prerequisites | executor | cost / runtime | owning issue |
|---|---|---|---|---|---|---|
| N1 | E1 / P3a / group rows have no run against the audited tree | One targeted, labelled run: `uv run pytest tests/conformance/test_corpus.py tests/conformance/test_named_fixtures.py --no-cov` (kernel control + migrated gates over `CI_SEEDS`), reported separately from the historical table | `uv run kct build-native` (router, DRC and placement backends), kicad-cli on the pinned image (`KCT_KICAD_DOCKER=1` on a Mac; on Linux the CI `kicad/kicad:10.0` container) | CI `test` job (already runs it) or an operator box with the pinned image; not a shared dispatch worker | Unknown. Per-case kicad-cli cost not measured here; the doc's full regen is 400 invocations, `CI_SEEDS` is 3 of 200 seeds | #5509 (record result on the parent) |
| N2 | Groups 15, 16, 18 rows are pre-migration percentages; "follow-up full regen pending" | Regenerate the report: `uv run python -m tests.conformance.report --seeds 0-199 --out docs/clearance-conformance.md` and diff | As N1 | Operator or a pinned-toolchain job | 400 kicad-cli invocations (per the doc); wall time unknown. The doc says this was "impractical" in-PR | #5855, #5857 (follow-ups named in their rows); no open issue found |
| N3 | Six report-only groups (3, 11, 12, 13, 14, 19) have no gate; group 12/13 order symmetry unmeasured | Decide per group: migrate (own phase issue), or record as deliberate survivor in the doc with its reason (e.g. group 19's conservative disc) | Operator ruling on whether groups 12/13 must be kernel-driven | Curator then Builder | Unknown | **No open owning issue found** -> file at `loom:triage` |
| N4 | Production rule selection is not tied to the oracle (axis 2) | Add one end-to-end case: run the resolver on the `issue5398-seg-via-0p18-order` project and assert the consumers receive 0.20 | N1 environment | Builder | Small, unknown | #5645/#5654 are closed; **no open owner found** -> file at `loom:triage` |
| N5 | P3b / board symptom: DQ3 sequence without the indexed-queue workaround | None new; closure of the owning blocked issue is the check | Existing blockers on #5410/#5333/#3438 | Owners of those issues | Unknown | #5410, #5333, #3438 |
| N6 | P3c: no reach regression on boards 00-07 | Read the CI result of `tests/router/test_board_route_determinism.py` on `main`, or run it once | Native backend | CI | Unknown | #5509 |
| N7 | E2 / P4a: 04-07 readiness lacks toolchain identity; 00-03 are `dirty` | Re-run `kct readiness ... --verify` on the pinned toolchain for the affected boards **only if** the board owners want these as epic evidence. This audit does not authorize regenerating boards. | Pinned toolchain; separate authorization | Board owners | Unknown | Per-board readiness owners; not authorized here |
| N8 | C3: no documented survivor list | Add the survivor list above (or its corrected version) to `docs/clearance-conformance.md` as part of N3's decision | N3 | Builder | Small | N3 (new triage issue) |
| N9 | C4 scan was limited | Check epic PR diffs for `mfr_limits.py` / `rules.py` / board `.kicad_dru` value hunks and confirm `76f5d366b` (#6561) is intended | None | Reviewer | Small | #6561 owner; none for the epic scan |

Nothing above changes a clearance value, tolerance, allowance, threshold or required coverage.

## Bottom line

On the evidence read, the epic's **structure** is delivered (kernel, oracle, resolver, 13 of 19 consumer groups
gated), but its concrete criteria are **not** yet demonstrated: the zero-disagreement criterion fails as written for
6 report-only groups, the post-Phase-4 survivor documentation is absent, production rule selection is not
connected to the oracle, #5410 remains open, and board-level evidence is unverified for toolchain identity. #5509
stays open.
