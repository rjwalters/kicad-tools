# Pad-access expansion reassessment (Epic #5508, Phase 4 gate)

Issue #7534. Research only: no change to `src/`, tests, rules, clearances or
search budgets. Tree checked: `0293cce0d` (`main`, 2026-10-20). Issue states
below were read with `gh` on the same date and should be re-checked before
being cited later.

## Recommendation: **defer** Phase 4 (lattice / mesh parity)

Do not create or dispatch the lattice/mesh port. Keep the delivered grid-engine
mechanism as it is (it is cheap, tested and default-on). Reopen only on the
trigger in [What would reopen this](#what-would-reopen-this).

Reason in one paragraph: no board-scale access-loss instance is known on this
tree, on the grid engine or the lattice engine. The one demonstrated instance
is a synthetic 4-pad fixture. The failures that motivated the epic were
re-attributed by their own evidence to other owners. The one live lattice
failure that touches pad escape (#7463) is a pad-clearance geometry problem
(#7503), not a committed route sealing a pad. A port would add machinery and
runtime to an engine whose open problems it does not address.

## 1. What was delivered, and what kind of evidence backs it

| Item | State | Evidence kind |
|---|---|---|
| Phase 1a access set (`router/pad_access.py`) | closed, delivered | synthetic unit tests (`tests/router/test_pad_access_5508.py`, `..._tht_6185.py`) |
| Phase 1b commit journal and offline witness (`router/access_witness.py`, 1570 lines) | delivered | synthetic tests (`test_access_witness_5508.py`, `_replay_5508.py`, `_replay_6292.py`, `_released_grid_6346.py`) |
| Phase 1c board runs (#5518) | closed, evidence only | **board-scale**: `docs/diagnostics/issue-5518/2026-09-21/`, run tree `37171ac08` |
| Phase 2 commit-time gate (`router/pad_access_invariant.py`, #5891) | delivered, default **on** for the grid engine, opt-out `--no-pad-access-invariant` | synthetic tests (`test_pad_access_invariant_5891.py`); cost work in `4369758e4` ("1s instead of 15 minutes") |
| Phase 3a access-loss rip-up (`router/access_ripup.py`, #5913) | delivered, default on, same switch | synthetic: `test_access_ripup_5913.py::test_negotiated_run_recovers_a_strand_the_commit_gate_could_not_refuse` (4-pad Kelvin cluster) |
| Phase 3b coupled departure (#5914) | closed **not planned**, superseded. Not delivered | n/a. The sampler it targeted was never on `main`; its replay controls showed prior copper does not cause the Board07 coupled failures. Do not count its criterion as passed |
| #5921 re-pointing | closed, completed | removed the board-05 `ISENSE_*` and board-07 `U4.C2` criteria from the epic's acceptance set |

Re-run on this tree for this report: `tests/router/test_access_ripup_5913.py`
and `tests/router/test_pad_access_invariant_5891.py`, 61 passed (`--no-cov`).
That confirms the synthetic coverage is live. It says nothing about boards.

Distinction that matters for the decision: **everything delivered is proven on
synthetic fixtures; the only board-scale evidence is negative** (Phase 1c).

## 2. Current failures and existing lattice/mesh protection

### Board-scale instances

- Phase 1c named no committed route as the cause of `ISENSE_A-` (board 05),
  `U2.B1` (board 06) or `U4.C2` (board 07). The `DQ3` negative control passed,
  so the witness does discriminate. Caveat recorded there: the board-06
  option-OFF control never finished, and the board-05 predicate ran on two of
  four runs.
- #5921 (main `3f7ef486`) re-ran with the targeter live: board 05 witness
  fired 0 times (`ISENSE_A-` is a search-reach failure, #5509 territory);
  board 07 `U4.C2` is pour-served, so no failed net exists to target.
- `docs/research/kct-route-escape-flow-experiment.md` (finding 3): across all
  arms and boards `stranded = 0`; every unrouted terminal still had a legal
  first move.
- Net: **none found** in boards 00-09 for a stranding caused by committed
  copper. This is a report of absence on those runs and that tree, not a proof
  that no such case exists.

### Remaining owners (not duplicated here)

#5509 (one clearance kernel, open), #5398 (closed, completed 2026-10-17),
#5507 (closed, completed 2026-09-21), and the coupled-search issues named in
the #5914 close note (#6835, #6853, #5333). This report proposes no fix for
any of them.

### Existing lattice/mesh protections

The invariant is grid-engine only by construction: `Autorouter` builds the gate
lazily and the comment at `core.py:1993-1995` says the lattice/mesh strategies
never reach a guarded commit. Neither `router/lattice/` nor `router/mesh/`
imports `pad_access`, `access_witness` or `access_ripup`. What lattice has
instead:

- `escape_plan.py` (`plan_tapered_escapes`, foreign reservations), bounded
  reservations for tapered pad pairs, kernel-backed since #5509 Phase 3d.
- `kelvin.py` (`KelvinBranchGuard`), same-net Kelvin isolation.
- `obstacles.py` (`CommittedCopper`), exact-kernel checks on committed copper.

So the gap is real in the narrow sense that lattice has **no generic
"does this commit empty another pad's exits?" check**. The open question is
whether anything is currently harmed by it. Candidate checked:

- **#7463 (board 03 arm C, VBUS `U2.5` unconnected, open).** Log shows
  `decline[no-path-keepout-constrained]` against the VBUS via site reserved
  between USB_D's U2 pads (#6697). #7503's evidence is geometric: a 0.6 mm via
  fits at (37.8, 10.6) with 0.286 mm corner clearance against 0.2 mm required,
  but the lattice judges pads by an inflated bounding rectangle
  (`lattice/obstacles.py` `pad_rects`) and rejects it. The pad was not sealed
  by a committed route; legal sites are being rejected. That is the
  "search refused legal copper" category (the same shape #5410 was for the grid
  engine), owned by #7503/#7463. An access map built on the same square pad
  test would inherit the same error.

Conclusion for this section: **no concrete affected use case found.** The
nearest candidate is a clearance-geometry defect with its own owner and a
stated fix.

## 3. Product value and cost

Value of a lattice/mesh port if justified: it would extend a guarantee ("no
commit strands another pad") to the engine softstart rev-C and board 03 use.
The guarantee only has value where a committed route can actually seal a pad,
and nothing in the current evidence shows that happening on lattice boards.

Costs (estimates, with uncertainty; none were measured for a port):

| Cost | Estimate | Basis and uncertainty |
|---|---|---|
| Implementation | the grid mechanism is over 3,000 lines across `access_witness.py` (1570), `pad_access_invariant.py` (829), `access_ripup.py` (641) and `pad_access.py`, plus well over 100 tests. A lattice port would reuse the predicates but needs new commit hooks in a different search structure | order of weeks of builder work, wide error bars (the lattice commit points were not traced for this report) |
| Runtime | grid gate once cost 15 minutes before `4369758e4` brought it to ~1 s; a first port can repeat that curve | unmeasured on lattice; budget rules forbid compensating with larger budgets |
| Maintenance | a third engine carrying a parity contract with `test_grid_cpp_parity.py`-style tests | qualitative |
| Correctness risk | lattice pad masks are squares (#7503); an access set built on them would be wrong by exactly the margin #7463 shows | specific, evidenced |

Ecosystem registry (`uv run kct ecosystem list`, run on this tree): the
autorouter peers (KiCadRoutingTools, TraceMaker, freerouting, fastroute,
OrthoRoute, tscircuit, Topola, DeepPCB) show no entry or note (in the part of the list I read, plus the
research notes grepped for "access"/"strand") that offers a reusable pad-access invariant. `docs/research/kct-route-escape-flow-experiment.md`
(TraceMaker-inspired joint escape assignment) already returned a null result
and states the access-loss class "did not occur". The GPL peers (TraceMaker,
freerouting, fastroute) are ideas-only per `license_compat`. Nothing in the
registry is a cheaper route to this guarantee. I did not do new web research:
the registry plus the epic's own literature note were sufficient to show no
reusable code exists, and the decision does not turn on the literature.

## 4. Falsifiable plan, if the trigger is ever met

A future Phase 4 proposal should be accepted only with all of these:

1. **A captured or explicitly constructed representative case on a lattice
   board**, with a passing "clear the copper" control (removing the suspected
   sealing copper reopens the pad), exactly as the #5914 close note required
   for coupled pairs. A route-log decline alone is not enough.
2. **Negative controls**: a board where the witness stays silent on lattice
   with committed copper present, and a rejected-legal-copper case (the #7463
   shape) that must read as *non-empty* access.
3. **Unchanged rules and budgets**: same `DesignRules`, per-net timeouts and
   node caps; no new default-off knob; gate on the existing
   `enable_pad_access_invariant` switch.
4. **No-regression coverage**: boards 02-07 seed 42 routed-net counts and
   pinned-toolchain DRC no worse on both engines; diffpair benches 06b/06rf/07rf
   no worse; determinism suite unchanged.
5. **Measured runtime**: wall-time delta per board with and without the gate,
   reported, with a stated cap agreed before implementation.
6. **Two claims kept apart**: a preventative guarantee (synthetic lattice
   fixture analogous to #5913's) is not evidence of recovering a real board
   failure, and neither proves the other. The proposal must say which it
   claims.

## What would reopen this

Any one of: (a) a lattice-board route where the offline witness (run on the
lattice commit journal, which would first need to exist) names a committed
route that sealed a pad, with the clear-the-copper control passing; (b) #7503
lands and #7463 still fails with a sealed-by-route verdict instead of a
legal-site rejection; (c) a grid-engine board regression where the witness
fires and rip-up does not recover it.

Proposed follow-ups: **none**. In particular, no issue for a lattice commit
journal should be filed until (a) or (b) occurs, because a journal alone has
no consumer.

## Limits of this report

- Board-scale numbers are quoted from committed artifacts and issue records,
  not re-measured. No routing run, native build or kicad-cli run was made.
- I did not trace the lattice commit path, so the port cost is an estimate.
- The Phase 1c base tree (`37171ac08`) is older than this tree; later router
  changes could alter its verdicts. #5921 and the escape-flow note are the
  later corroboration.
- #5508 stays open and this report does not change its scope.
