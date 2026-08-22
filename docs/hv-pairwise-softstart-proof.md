# HV pairwise avoidance: softstart rev-C proof runs (2026-08-15 → 2026-08-21)

Running record of the #4507 T4 manual criterion, newest run first. Each section
describes the tree as of its own date.

> **Current record: [the 2026-08-21 fifth pass](#the-2026-08-21-fifth-pass-fixture-access-root-caused-code-side-re-verified-complete).**
> Root-causes *why* every pass since the third one has been unable to reach
> the physical fixture (a worktree-relative symlink depth bug, filed
> separately as #4925), confirms independently that this specific host also
> lacks the rev-C data even from the primary checkout, and does a fresh,
> from-scratch re-read of every file the #4507 tracking issue names to verify
> Ask item 4 and the attribution sub-task are both still complete on `main`
> with no new gap found. No code change in this pass — see that section for
> why, and for the recommended next step (operator/host action, not another
> code-search pass).
>
> Immediately below: [the 2026-08-21 fourth
> pass](#the-2026-08-21-fourth-pass-the-coupledfat-path-had-its-own-pairwise-gap-too),
> which continued the third pass's still-open `/LED_A_NEG`↔`/SCAP_POS`
> attribution. A code-level (not fixture) audit of every emission path the
> lattice engine can commit copper through found a second, independently-real
> case (c) defect: the diff-pair coupled/fat routing path's finish-gate
> re-verification checked a foreign PAD's SCALAR clearance but never its
> pairwise (HV) requirement, unlike the same re-verification's already-
> pairwise-aware check of foreign COMMITTED trace/via copper. Fixed with a
> synthetic regression test (`test_pair_declines_when_emitted_leg_violates_
> pairwise_pad_requirement`, `tests/router/lattice/test_coupled_pairs.py`).
> That pass could NOT re-run the physical softstart rev-C fixture either (it
> is local-only hardware-fixture data not available in this environment), so
> it did not claim to have confirmed this explains `/LED_A_NEG`↔`/SCAP_POS`
> specifically — only that the gap was real, reproducible synthetically, and
> matched that residual's exact geometry shape (a routed TRACE against a
> foreign PAD). A separate, out-of-scope-for-#4507 candidate was also found
> and filed as #4910 (non-cardinal pad-rotation AABB approximation in the
> search-time obstacle model) rather than guessed into that fix.
>
> Below that, retained as the record of the diagnosis: [the 2026-08-21 third
> pass](#the-2026-08-21-third-pass-the-attribution-sub-task-finds-and-fixes-a-genuine-search-time-defect)
> (the preserved-net-id fix, 3 of 4 prior residuals), [the 2026-08-21 second
> pass](#the-2026-08-21-second-pass-the-widened-gate-attributes-all-6-residuals)
> (Ask item 4's kernel widening, and the attribution question this pass
> resolves), [the 2026-08-21 first
> re-run](#the-2026-08-21-re-run-the-router-gate-is-clean-and-what-that-leaves)
> (the frame-correction fix and the "gate does not look" root cause that pass
> closes), [the 2026-08-16 re-score](#the-re-score-same-recipe-corrected-table)
> (#4867's `abs()` sign collapse fixed by #4868), and from [Verdict (pre-fix
> run)](#verdict-of-the-pre-fix-run-t4-fails--but-the-machinery-is-measurably-doing-its-job)
> downwards the original pre-fix measurement.

## The 2026-08-21 fifth pass: fixture access root-caused, code-side re-verified complete

`main` @ `da10bac8` (PR #4911, the fourth pass's own commit), C++ backend
build 21. This pass had two goals: (1) actually get the physical fixture and
run the recipe, retiring the fourth pass's "not available in this
environment" note with either a real re-run or a *specific, evidenced*
reason it still can't happen; (2) failing that, do a fresh independent
re-read of every file #4507 names to make sure a fifth pass isn't needed for
a reason a code review would have caught.

### Fixture access: two independent, now-confirmed reasons

**Reason 1 — the symlink is worktree-depth-broken, filed as #4925.**
`boards/external/softstart` is a *relative* symlink
(`../../../softstart/hardware/kicad`) calibrated for the primary checkout
sitting directly under the same parent as the sibling `softstart` fixture
repo. A Loom-managed worktree (`.loom/worktrees/issue-N/`, or this repo's
daemon-managed `.claude/worktrees/agent-<id>/`) nests two levels *inside*
the checkout, so the same three `../` lands inside the worktree tooling
directory instead — a path that does not exist. Confirmed directly:

```
$ readlink boards/external/softstart
../../../softstart/hardware/kicad

# from the primary checkout:
$ realpath boards/external/softstart
/Users/…/GitHub/softstart/hardware/kicad                                  # correct, exists

# from this pass's worktree:
$ realpath boards/external/softstart
/Users/…/GitHub/kicad-tools/.claude/worktrees/softstart/hardware/kicad    # wrong, does not exist
```

This is very likely *why* the third and fourth passes (both run in Builder
worktrees, per Loom's normal dispatch) reported "no physical fixture was
available in this environment" — not a statement about the host, but an
artifact of every builder always running from a worktree. Filed as #4925
(out of #4507's scope — a repo-infra path bug, not a pairwise-clearance
defect) with a suggested fix (git-common-dir-relative resolution or an env
var override) for whoever picks it up.

> **Update (#4925 fixed).** `tests.conftest.resolve_external_boards_dir()`
> now locates the *primary* checkout's `boards/external/` via `git rev-parse
> --git-common-dir` (stable across every worktree of the repo) instead of
> deriving it from the calling worktree's own `__file__` location, so a
> Builder worktree no longer misreads "unreachable from here" as "absent on
> this host." `tests/test_placement.py::TestSoftstartRevCOffBoard` (the
> consumer this doc's own recipe shares fixture-path conventions with) now
> uses it. Set `KICAD_TOOLS_EXTERNAL_BOARDS_DIR=/path/to/boards/external` to
> override the resolution entirely on a layout the heuristic can't infer.
> This does **not** change Reason 2 below — a future pass still needs a host
> whose primary checkout has current rev-C outputs generated, resolvable or
> not.

**Reason 2 — even the primary checkout's sibling repo lacks rev-C data.**
Fixing #4925 would not by itself unblock T4 on *this* host: checking the
sibling `softstart` repo directly (bypassing the symlink entirely,
`/Users/…/GitHub/softstart`) shows only a Rev B design. There is no
`hardware/kicad/output_revc/` tree, no `softstart_revc.kicad_pcb`, and none
of the `vmap.json` / `net_class_map.json` / `creepage_class_map.json`
sidecars this doc's recipe reads — `generate_design.py` in that repo has no
revision flag and only ever produces the committed Rev B outputs. Every
prior pass's own convention ("No board artifacts are committed to this repo
… routed boards are `DO_NOT_FAB` work-in-progress and live in scratch") is
consistent with this: the rev-C proof inputs were always generated fresh,
outside version control, in whatever environment ran the recipe, and are not
persisted anywhere this pass can reach. **So this pass could not run the
recipe either, for a second, independent, host-level reason — not a repeat
of the same "not available" note, but two separately-verified causes.**

### Code-side re-verification: still complete, no new gap found

With the fixture confirmed unreachable, this pass instead did a from-scratch
read (not relying on prior passes' own conclusions) of every file the
#4507 tracking issue's "Affected Files" list names:

- `pairwise_clearance.py`: `_segments_pairwise_violation` (the shared
  moving-vs-foreign walk) takes `foreign_pads` and checks
  trace-vs-pad/via-vs-pad explicitly (module docstring at the top of that
  function enumerates the coverage); `board_pairwise_violations` /
  `board_pad_geometry` / `board_trace_routes` (added by PR #4885, widened by
  #4887) thread real pad geometry from the written board file. Confirmed
  present and unchanged since #4887/#4908.
- `lattice/pairwise.py`: `LatticePairwise.exempt_seg_pt` /
  `exempt_pt_pt` / `exempt_seg_seg` are all present and probe the correct
  closest-gap midpoint per the #4506 exemption contract; no dead helper or
  missing probe shape found.
- `lattice/obstacles.py`: `pairwise_pad_blocked` grows each foreign pad's
  keep-out per-pair (`own_half + required(net, pad.net) − agent_radius`) and
  is consulted from every ordinary (non-coupled) emission call site
  (`_route_search`'s edge/node/via exploration, `_scan_stubs`,
  `_escape_stubs`) — matching the fourth pass's own read. The one
  call site that did NOT consult it (`_route_pair_impl`'s `finish()`
  re-verification loop, for foreign PAD copper specifically) was fixed in
  PR #4911: that loop in `src/kicad_tools/router/lattice/pathfinder.py`
  now calls `pairwise_pad_blocked` itself.
- `route_cmd.py`: the post-route audit docstring at line ~4578 already says
  "trace-vs-trace (same layer), trace-vs-via and via-vs-via" — updated past
  the stale "trace-to-trace, same layer only" wording the issue quoted, with
  an explicit note that pad copper needs the board-file replay
  (`board_pairwise_violations`) instead. Current, not stale.
- `scripts/replay_pairwise_gate.py`: already documents (and implements) the
  pad-geometry companion to `board_trace_routes`, with a `--no-pad-geometry`
  flag for the pre-#4507 scope as a control.
- `tests/router/test_pairwise_clearance.py`,
  `tests/router/test_pairwise_board_replay_4507.py`,
  `tests/router/lattice/test_coupled_pairs.py`: all carry the trace↔pad,
  via↔pad, via↔trace, and (from PR #4911) coupled-path-pad regression cases
  the revised Ask item 4 calls for.

No further case (c) defect turned up. Combined with the fourth pass's own
"what was ruled out" read (every ordinary emission call site already
consults the predicate) and this pass's confirmation of the one call site
that didn't (now fixed), the code side of #4507's revised Ask item 4 and its
attribution sub-task are believed complete as of `main` @ `da10bac8`. Full
test suite for this area re-run clean on this pass: 364 passed, 1 skipped
(`tests/router/lattice/test_coupled_pairs.py`,
`tests/router/test_pairwise_clearance.py`,
`tests/router/test_pairwise_cpp_parity.py`,
`tests/router/test_pairwise_python_search.py`,
`tests/router/test_pairwise_board_replay_4507.py`,
`tests/router/test_lattice_pairwise_preserved_net_id_4507.py`,
`tests/router/test_pairwise_ripup_diagnostics_4507.py`,
`tests/router/test_post_pass_pairwise_gate_4766.py`,
`tests/test_route_subthreshold_coverage_4507.py`,
`tests/test_placement.py`).

### What T4 needs now

Not more code search — a run of this doc's recipe against the actual
softstart rev-C inputs, which requires either (a) a host whose primary
checkout (or a directory named via `KICAD_TOOLS_EXTERNAL_BOARDS_DIR`, now
that #4925 is fixed) has current rev-C outputs generated and sitting at
`hardware/kicad/output_revc/` with the sidecar files this recipe reads, or
(b) direct operator access to regenerate those inputs. Neither is a code
change this repo's automated pipeline can make on its own. Recorded here so
the next pass (or a human) does not have to re-derive reasons 1 and 2 above
from scratch.

## The 2026-08-21 fourth pass: the coupled/fat path had its own pairwise gap too

This pass continues the third pass's open question for the one genuine
residual that did NOT reproduce the preserved-net-id defect:
`/LED_A_NEG`↔`/SCAP_POS`. The third pass's own targeted checks had already
shown that (a) both nets resolve to their correct ids with the correct
2.0mm requirement, and (b) replaying `LatticeObstacleModel.pairwise_pad_
blocked` directly against the exact violating geometry (LED_A_NEG's emitted
segment vs. the real SCAP_POS pad) correctly reports it as blocked in
isolation. That combination is itself informative: if the predicate that
gates ordinary (non-coupled) lattice routing would have refused this exact
geometry, then either (i) this geometry was never actually offered to that
predicate during the real negotiated run, or (ii) it was emitted through a
DIFFERENT code path that does not consult the predicate at all.

**No physical fixture was available in this environment** (`hardware/kicad/
output_revc/` is a local-only hardware-fixture tree per this doc's own
convention, not present in this worktree) — this pass is a static/synthetic
attribution, not a re-run of the recipe. It documents what a full read of
every lattice emission path found, and fixes the one genuine gap that read
turned up, with a synthetic regression test rather than a guessed live fix.

### What was ruled out

Read end to end, every geometry-emitting call site the ORDINARY (single-net,
non-coupled) lattice search uses consults `pairwise_pad_blocked` consistently
whenever a projection is installed: the A* edge/node/via exploration loop
(`_route_search`, three call sites — edge, node, via-hop), the pad-escape
dogleg scan (`_scan_stubs`, both the candidate-node probe and each dogleg
leg), and the tapered-neck escape fallback (`_escape_stubs`, which re-enters
`_scan_stubs`). Emission itself (`_emit`) is "path IS copper" — a straight
`Segment`-per-lattice-step conversion with `merge_collinear` fusing
contiguous EXACT-collinear runs (tolerance 1e-4mm, four orders of magnitude
below the 0.175mm residual shortfall) and no other post-fit stage, so nothing
after the search can introduce unchecked geometry into an ordinary route.
This rules out (i) for every *ordinary* connection.

### What was found: the diff-pair coupled/fat path's finish gate

`LatticePathfinder._route_pair_impl` (issue #4270's diff-pair "fat centerline"
router) is (ii): a documented, deliberate exception. Its centerline SEARCH
skips pairwise widening entirely (`coupled.committed_seg_clear_grown`'s own
docstring: "an engaged diff pair is a same-domain signal pair by
construction; giving the whole grown envelope a multi-mm HV keep-out ... would
make every coupled run near mapped copper decline outright"). That docstring
also claims the emitted legs are re-covered end to end by re-verification:
"`_pair_attach`/re-verification go through `CommittedCopper.seg_clear`
(pairwise-aware when a projection is installed)".

That claim is half true. The `finish()` re-verification loop in
`_route_pair_impl` (the authoritative accept/reject gate for every emitted
leg, offset body AND pad-attach doglegs alike) checks two things per segment:

```python
if pads_block_segment_grown(self.obstacles, a, b, layer, pair_nets, 0.0):
    return None, "pair-leg-blocked"
if not committed.seg_clear(a, b, layer, net):
    return None, "pair-leg-blocked"
```

`committed.seg_clear` IS pairwise-aware (it is the ordinary `CommittedCopper`
method, which consults its installed `pairwise` projection against foreign
COMMITTED trace/via copper) — but `pads_block_segment_grown`
(`lattice/coupled.py`) is a pure-scalar check against foreign PAD keep-outs;
it has no `pairwise` parameter and never did. So the "covered end-to-end"
claim held for foreign committed copper but not for foreign PAD copper — and
`/LED_A_NEG`↔`/SCAP_POS` is exactly a routed-TRACE-vs-foreign-PAD pair, the
one geometry class this path never re-checked against the pairwise
requirement.

Whether LED_A_NEG was ever actually routed through this path on the real
board is unconfirmed (it would require the suffix-based diff-pair detector,
`_POS_NEG_PATTERN` in `router/diffpair.py`, to have matched a same-prefix
`LED_A_POS`/`LED_A_NEG` pair — plausible given softstart rev-C's `_POS`/`_NEG`
bipolar-rail naming convention across many of its nets — AND the net class
either of them resolves to have `coupled_routing=True` in
`net_class_map.json`, which this pass cannot inspect without the fixture).
Independent of that, the gap itself is real and reproducible on a synthetic
board with no dependency on the fixture, so it is fixed here rather than left
as a report-only finding.

### The fix

`LatticePathfinder._route_pair_impl`'s `finish()` loop now additionally
consults `self.obstacles.pairwise_pad_blocked(a, b, layer, net, trace_w /
2.0, 0.0, self._pairwise)` for each emitted leg segment, guarded by
`self._pairwise is not None` (byte-identical dormancy without
`--voltage-map`, matching every other #4507/#4602 predicate). This is
checked at the LEG's own single-trace half-width with no extra surcharge —
mirroring exactly how the non-fat search's own `pairwise_pad_blocked` call in
`_route_search` is parameterised — so a coupled pair is never over-
constrained by its own fat pitch envelope, only by what its actual emitted
copper requires. A violation declines the pair honestly (`"pair-leg-pairwise-
blocked"`), consistent with #4270/#3906's "never ship a short, decline with a
named reason" discipline; it does not fall back to splitting the pair into
uncoupled legs.

### Regression coverage

`tests/router/lattice/test_coupled_pairs.py::
test_pair_declines_when_emitted_leg_violates_pairwise_pad_requirement` — a
synthetic two-layer board with an engaged diff pair and one foreign pad
placed where the pair's own (otherwise-legal) fat route runs: close enough to
violate a mapped 2.0mm pairwise requirement, but far enough to clear the
ordinary scalar floor. Before the fix the pair routed straight over it
(`pair_outcomes == "coupled"`); after the fix it declines with
`"pair-leg-pairwise-blocked"`. A control with no pairwise projection
installed on the identical board (including the same foreign pad) still
couples cleanly, proving the decline is the pairwise requirement and not the
corridor or the pad's ordinary scalar keep-out.

### What remains open

`/LED_A_NEG`↔`/SCAP_POS` itself is still not confirmed fixed — only a real,
independently-verified gap of the right geometric shape is now closed. A
future pass with fixture access should re-run this doc's recipe to check
whether the residual count actually drops. If it does not, the pad-rotation
AABB gap filed as #4910 (unconfirmed, out of #4507's own scope since it
affects ordinary DRC clearance too, not just pairwise) is the next candidate
to check against that specific pad's actual rotation angle.

## The 2026-08-21 third pass: the attribution sub-task finds (and fixes) a genuine search-time defect

Fifth run of the same recipe, same fixture inputs (`softstart_revc.kicad_pcb`
md5 `7d82599e…`, `net_class_map.json` `9184a661…`, `vmap.json` `cc72a701…`,
`creepage_class_map.json` `6c3b324a…` — identical to every prior run in this
file), `main` @ `c5d2127d` (PR #4887, the commit the previous section's "6"
figure was measured against, plus this issue's own attribution-sub-task diff)
with the C++ backend at build 21. Steps 1 and 2 only, same as every run since
the frame-correction pass.

### What this pass answers

The previous section named the open question explicitly: for each of the 4
genuine findings, is it (c) a genuine search-time gap in the lattice's
existing pairwise-aware pad/via widening, or (a)/(b) an inherent trade-off of
a board that only completes 44/77 (57%) signal nets under placement pressure?

**Answer: 3 of the 4 are case (c) -- a real, fixable search-time defect, not a
placement trade-off -- and it is now fixed. The 4th does not reproduce that
defect and remains open.**

### The defect: preserved nets silently resolve to net id 0

`Autorouter._net_name_to_id()` (`src/kicad_tools/router/core.py`) builds the
net-name → net-id reverse map that
`Autorouter._lattice_pairwise_projection()` (#4602) uses to project the
name-keyed `DesignRules.pairwise_clearance` table into the net-id space the
(deliberately geometry-only, #4597) lattice search consumes. Before this pass
it was built from `self.net_names` plus a `pad.net`-keyed fallback over
`self.all_pads` -- **both of which read `Pad.net`.**

On a *filtered* pass (`--nets` / `--skip-nets` / `--region` / `--complete` --
every composition step, and therefore every step of this recipe's own step 2)
the board loader (`router/io.py`) rewrites every NON-routable net's pads to
`net_num = 0` so they act as anonymous clearance obstacles: `pad.net_name`
survives, `pad.net` collapses. So `_net_name_to_id()` mapped **every**
preserved/fixed net's name onto the same sentinel id, 0 -- and
`LatticePairwise.required_by_pair` (keyed by REAL net-id pairs, since the
actual committed copper for a preserved net keeps ITS true id, sourced
separately via `router/lattice/pathfinder.py`'s `_seed_fixed_copper`) never
had an entry any query against that net's true id could find.
`pairwise.required(moving_net, real_preserved_id)` silently returned `0.0`:
the search-time HV avoidance #4602/#4507 built was dormant for exactly this
class, on **every** `--complete` run (which always has a preserved/fixed net
set -- `--complete` is precisely how this recipe's own step 2 runs).

This is the *same defect class* issue #4622 already named and fixed for a
different consumer: the net-class-map sidecar merge
(`_resolve_net_class_map_domains` in `route_cmd.py`), which was widened with
`router.existing_routes` (preserved routes carry each net's TRUE id straight
from the board's copper, never rewritten by the filtered-pass loader).
`_net_name_to_id()` never got the matching fix. It now does: any entry that
is missing or collapsed to 0 is corrected from `existing_routes`; a
genuinely-resolved (non-zero) id from the two existing sources is never
overridden.

Confirmed with a targeted diagnostic against the real board (`step1.kicad_pcb`
+ the same `net_class_map.json`/`vmap.json`, short-circuited with
`--timeout 3` so the pairwise projection is built without paying for the
full negotiation):

```
before the fix:
  required_by_pair size: 1748  (of a possible 1922 cross-pairs)
  /GATE_BUS_POS(22) <-> /I_SENSE_OUT(0): required=3.2   <- WRONG id AND value
  /I_SENSE_OUT(0) <-> /SCAP_NEG(50): required=1.4        <- WRONG id (I_SENSE_OUT)
  /PRECHARGE_POS(0) <-> /V_BANK_NEG_MID(96): required=1.2  <- WRONG id (PRECHARGE_POS)
  /LED_A_NEG(33) <-> /SCAP_POS(52): required=2.0         <- correct (both step2-native)

after the fix:
  required_by_pair size: 1893  (of a possible 1922; the remaining 29 have no
                                 existing_routes copper at all -- unroutable
                                 unless/until something commits some)
  /GATE_BUS_POS(22) <-> /I_SENSE_OUT(31): required=1.6   <- fixed
  /I_SENSE_OUT(31) <-> /SCAP_NEG(50): required=1.4        <- fixed
  /PRECHARGE_POS(43) <-> /V_BANK_NEG_MID(96): required=1.2  <- fixed
  /LED_A_NEG(33) <-> /SCAP_POS(52): required=2.0          <- unchanged (already correct)
```

`/I_SENSE_OUT` and `/PRECHARGE_POS` are precisely the two nets step 1 routes
and step 2 preserves -- the two-step composition this recipe (and the
documented HV-outer recipe generally) always uses.

### Re-running the recipe with the fix: 6 → 3 board-level census fails

Same recipe, same inputs, step1 output byte-identical to the previous section
(the fix is a no-op on step 1: nothing is preserved yet). Step 2 (`--complete`)
re-run on the fixed tree:

| Measure | previous section | **this pass (fixed)** |
|---|---|---|
| Signal nets routed | 50 (6/7 + 44/77) | **50 (6/7 + 44/77)** -- unchanged |
| Wall clock, step 2 | 1758.3s of 5790.0s budget | **1441.8s of 5790.0s budget** |
| Board-level census fails | **6** | **3** |
| — genuine (not #4506-exempted) | 4 | **1** |
| — #4506-exempted (scope difference, not a defect) | 2 | **2** (unchanged) |

```
$ python3 hardware/kicad/creepage_triage.py step2_fixed.kicad_pcb creepage_fixed.json
== board (3) ==
  /AC_LINE           /AC_NEUTRAL          have=0.600 req=1.60 dV=150
  /AC_NEUTRAL        /FUSED_LINE          have=0.600 req=1.60 dV=150
  /LED_A_NEG         /SCAP_POS            have=1.825 req=2.00 dV=180
```

The first two are the same mains-cluster attach-zone exemption the previous
section already attributed (`J1`/`J2`/`F1`, waived by the router's gate,
scored by the exemption-blind census) -- unaffected by this pass, reproduces
identically. **The 3 fixed pairs (`/GATE_BUS_POS`↔`/I_SENSE_OUT`,
`/I_SENSE_OUT`↔`/SCAP_NEG`, `/PRECHARGE_POS`↔`/V_BANK_NEG_MID`) no longer
appear anywhere in the census, at any location, with or without the
attach-zone exemption applied** -- confirmed with
`scripts/replay_pairwise_gate.py --no-attach-zones` too, which is the wider
(pre-exemption) scope and would have shown them if the search had merely
routed around the CENSUS's specific reported point while leaving a shortfall
elsewhere.

Signal net completion is byte-identical (50/82, no routability cost) --
consistent with this being a correctness fix to how a requirement is
LOOKED UP, not a change to how tightly the search is constrained.

### What remains: `/LED_A_NEG` ↔ `/SCAP_POS`, still open

This pair does **not** reproduce the id-resolution defect: both nets are
step2-native (routed together, not preserved from step 1), and the targeted
diagnostic above shows both resolve to their correct ids (33, 52) with the
correct requirement (2.0mm) **before and after** this fix. A second targeted
check -- replaying `LatticeObstacleModel.pairwise_pad_blocked` directly against
the exact violating geometry (LED_A_NEG's emitted segment vs. the real SCAP_POS
pad polygon and position, both read from the routed board) -- confirms the
static predicate correctly reports this exact geometry as blocked in
isolation. So neither the id-projection (fixed here) nor the static
pad-widening predicate itself is at fault for this specific pair; the
remaining candidates are a subtler search-time interaction (e.g. an emission
path this run did not isolate) or a genuine congestion trade-off given the
44/77 (57%) completion rate. Not guessed at further here -- named as the next
attribution target, with the isolation technique (diagnostic id/value dump +
direct predicate replay against real board geometry) that resolved the other
3 available for whoever picks it up.

### Reproducing this pass

```bash
# Steps 1-2 identical to the previous section's Commands (same recipe, same inputs).
# Step 1 is unaffected by the fix and can be reused verbatim from a prior run.
uv run kct route step1.kicad_pcb -o step2_fixed.kicad_pcb \
  --complete --complete-exclude-nets "GND,+3.3V" --complete-report report_fixed.json \
  --net-class-map net_class_map.json --voltage-map vmap.json \
  --creepage-standard iec60664 --pollution-degree 2 --material-group IIIa \
  --manufacturer jlcpcb --copper 2 --layers 4

uv run kct creepage step2_fixed.kicad_pcb --voltage-map vmap.json \
  --net-class-map creepage_class_map.json --standard iec60664 \
  --pollution-degree 2 --material-group IIIa --working-voltage 250 \
  --waive-same-footprint --format json > creepage_fixed.json
python3 hardware/kicad/creepage_triage.py step2_fixed.kicad_pcb creepage_fixed.json

uv run python scripts/replay_pairwise_gate.py step2_fixed.kicad_pcb --voltage-map vmap.json
```

Regression coverage (unit-level, does not need the fixture):
`tests/router/test_lattice_pairwise_preserved_net_id_4507.py`.

No board artifacts are committed to this repo, as with every prior run in this
file.

## The 2026-08-21 second pass: the widened gate attributes all 6 residuals

Fourth run of the same recipe, same fixture inputs (`softstart_revc.kicad_pcb`
md5 `7d82599e…`, `net_class_map.json` `9184a661…`, `vmap.json` `cc72a701…`,
`creepage_class_map.json` `6c3b324a…` — identical to every prior run in this
file), `main` @ `cb1a3cc9` (PR #4885, the commit the previous section's "17"
figure was measured against, plus this issue's own kernel-widening diff) with
the C++ backend at build 21. Steps 1 and 2 only, same as the immediately
preceding run — step 3 (`kct zones hv-keepout`) is still unaddressed.

**What changed in the code, not the board**: `segment_pair_violation` and its
callers (`route_pairwise_violation`, `find_pairwise_violations`,
`board_pairwise_violations`, and the `_audit_pairwise_clearance` in-run audit)
were trace↔trace only, by the documented design the previous section names.
This pass adds trace↔via, via↔via (for free from `Route.vias`, which every
caller already carried) and trace↔pad / via↔pad (new — `board_pad_geometry()`
resolves true pad copper from the board file, sheet-absolute, the same frame
`board_trace_routes()` and `board_attach_zones()` already use). The routing
search itself (C++ `Grid3D::validate_route`, `lattice/obstacles.py`'s
`pairwise_pad_blocked`) is untouched — this is an audit/replay-visibility
fix, not a search change, per the attribution this run performs below.

**Caveat on the numbers**: step 2 is a wall-clock-budgeted `--complete` pass
(`elapsed 1758.3s of a 5790.0s budget` this run), and no board artifact from
the previous section's run was retained (softstart's own no-board-artifacts
policy — see "Reproducing" at the bottom of this file). So this run's 44/77 +
6/7 signal-net counts happening to match the previous section's are not proof
of a bit-identical board; treat the two runs' census counts (6 here vs. 17
there) as two independent measurements of the same recipe, not a diffed
before/after of one board. What this run *does* establish, on its own copper,
is complete: every board-level census fail it produced is accounted for.

| Measure | T4 requires | **this run** |
|---|---|---|
| Signal nets routed | 100 % | 50 (6/7 + 44/77) |
| Cross-pairs in the matrix | = census | 1922 |
| Board-level census fails, after step 2 | **0** | **6** |
| — attributed to a genuine, now-visible pairwise shortfall | — | **4** |
| — attributed to a #4506-exempted pairwise shortfall (scope difference, not a defect) | — | **2** |
| — unattributed | 0 | **0** |
| Router's own gate on the finished board (trace/via/pad, #4506 zones honoured) | 0 | **4** (was silently 0) |

### Attribution: every one of the 6 traced to a specific copper primitive

`kct creepage` (the census, `--waive-same-footprint`) reports 6 board-level
fails on this run's `step2.kicad_pcb`:

```
/AC_LINE          <-> /AC_NEUTRAL       0.600 mm (req 1.600 mm)
/AC_NEUTRAL       <-> /FUSED_LINE       0.600 mm (req 1.600 mm)
/GATE_BUS_POS     <-> /I_SENSE_OUT      0.400 mm (req 1.600 mm)
/SCAP_NEG         <-> /I_SENSE_OUT      0.400 mm (req 1.400 mm)
/V_BANK_NEG_MID   <-> /PRECHARGE_POS    1.014 mm (req 1.200 mm)
/LED_A_NEG        <-> /SCAP_POS         1.825 mm (req 2.000 mm)
```

The widened `scripts/replay_pairwise_gate.py` (backed by
`board_pairwise_violations`, the identical kernel the router's own audit
uses) reproduces **4 of the 6 exactly**, mm-for-mm, with the #4506 exemption
applied (the router's real operating scope):

```
$ uv run python scripts/replay_pairwise_gate.py step2.kicad_pcb --voltage-map vmap.json
  pairwise violations (trace/via/pad, with #4506 attach zones): 12  (8 net pairs)
    /GATE_BUS_POS <-> /I_SENSE_OUT: 0.400 mm against 1.600 mm  <- via-involved
    /I_SENSE_OUT <-> /SCAP_NEG: 0.400 mm against 1.400 mm      <- via-involved
    /PRECHARGE_POS <-> /V_BANK_NEG_MID: 1.014 mm against 1.200 mm  <- via-involved
    ...
    /LED_A_NEG <-> /SCAP_POS: 1.825 mm against 2.000 mm        <- pad-involved
```

(The other 4 pairs the widened gate reports — `/GATE_NEG_A`↔`/LED_K_NEG`,
`/GATE_POS_A`↔`/LED_K_POS`, `/V_BANK_POS_MID`↔`/SCAP_POS`,
`/AC_NEUTRAL`↔`/AC_LINE` at 1.484 mm — are real findings too, just at a
*different* location than the census's own reported minimum for that net
pair; `kct creepage` is net-pair-keyed and reports only the tightest gap per
pair, and for three of those four the tighter gap is a `same_footprint`
pad-vs-pad instance this router-copper gate does not check by design — pads
that never move are a placement question, not something search-time
avoidance can act on.)

The other 2 (`/AC_LINE`↔`/AC_NEUTRAL`, `/AC_NEUTRAL`↔`/FUSED_LINE`) reproduce
exactly, mm-for-mm, **only with the #4506 exemption disabled**:

```
$ uv run python scripts/replay_pairwise_gate.py step2.kicad_pcb --voltage-map vmap.json --no-attach-zones
    /AC_NEUTRAL <-> /FUSED_LINE: 0.600 mm against 1.600 mm
    /AC_NEUTRAL <-> /AC_LINE: 0.600 mm against 1.600 mm
```

Both sit inside a rated connector/fuse-holder footprint's own attach copper
(`J1`/`J2`/`F1` — the mains input cluster), the same waiver class as the `Q7`/
`Q8` MOSFET packages the first re-run's frame correction confirmed. The
router's gate sees this copper and correctly does not flag it, under the
scope #4506 gives it; `kct creepage` has no concept of that exemption and
scores the pair at its true minimum regardless. This is the same "census/
attach-zone scope difference is expected, not a bug" finding the pre-fix
run's own "What would have to change for T4 to pass" list already named —
this run is the first evidence that it, specifically, is what the remaining
non-genuine fails are.

**Net result: zero of this run's 6 board-level census fails are unattributed.**
Before this pass, all 6 (like the previous section's 17) were invisible to the
router's own gate by construction — "the gate does not look." After it, every
one is either a genuine finding the gate now reports (4) or a documented,
intentional exemption-scope difference (2). "What T4 needs now" item 1 from
the previous section — widen the pairwise gate past trace↔trace — is **MET**.

### What is still open

The 4 genuine findings are real, but this pass does not establish *why* the
search placed that copper — whether `lattice/obstacles.py`'s existing
pairwise-aware pad/via widening (`pairwise_pad_blocked`, the `pairwise.
required(...)` call sites) failed to avoid it on nets it did route (a
residual search-time gap, case (c) from the curator's framing), or whether it
is an inherent trade-off of a run that only completed 44/77 (57 %) signal
nets under documented `CONGESTION_SATURATED`/`BUDGET_STARVED` placement
pressure (case (a)/(b) territory — a search that had no clean alternative to
offer, on a board that is not fully routed regardless). Resolving that
needs comparing the lattice's obstacle grid at each of the 4 locations
against what it should have computed, which is deeper than this run's
audit-kernel change touches. Named here as the open follow-up, not assumed
either way.

The `--hv-threshold` policy question, the `kct zones hv-keepout` margin bug,
and the 27–32 unrouted-net placement wall are all unchanged from the previous
section and remain out of this issue's scope for the reasons already given
there.

### Reproducing this run

```bash
# Steps 1-2 identical to the previous section's Commands (same recipe, same inputs).
uv run kct creepage step2.kicad_pcb --voltage-map vmap.json \
  --net-class-map creepage_class_map.json --standard iec60664 \
  --pollution-degree 2 --material-group IIIa --working-voltage 250 \
  --waive-same-footprint --format json > creepage_step2.json
python3 hardware/kicad/creepage_triage.py step2.kicad_pcb creepage_step2.json

uv run python scripts/replay_pairwise_gate.py step2.kicad_pcb --voltage-map vmap.json
uv run python scripts/replay_pairwise_gate.py step2.kicad_pcb --voltage-map vmap.json --no-attach-zones
uv run python scripts/replay_pairwise_gate.py step2.kicad_pcb --voltage-map vmap.json --no-pad-geometry
```

No board artifacts are committed to this repo (routed boards are
`DO_NOT_FAB` work-in-progress and live in scratch, per the fixture repo's own
policy) — as with every prior run in this file.

## The 2026-08-21 re-run: the router gate is clean, and what that leaves

Third run of the same recipe. Same fixture inputs, **byte-identical** to both
earlier runs (`softstart_revc.kicad_pcb` md5 `7d82599e…`, `net_class_map.json`
`9184a661…`, `vmap.json` `cc72a701…`, `creepage_class_map.json` `6c3b324a…`),
now on Linux against `main` @ `0efe6218` with the C++ backend at build 21. Steps
1 and 2 only — step 3 was skipped deliberately (the 2026-08-16 finding that
`kct zones hv-keepout --clearance 1.6` makes this board *worse* stands, and is
unaddressed).

| Measure | T4 requires | **2026-08-21** | 2026-08-16 | 2026-08-15 pre-fix | matrix off |
|---|---|---|---|---|---|
| Signal nets routed | 100 % | **50** (6/7 + 44/77) | 50 / 82 | 49 / 82 | 50 / 82 |
| Cross-pairs in the matrix | = census | **1922** | 1922 | 1823 | dormant |
| Board-level census fails, after step 2 | **0** | **17** | 25 | 57 | 107 |
| **Router's own gate on the finished board** | 0 | **0** | 2 | 3 | 54 |
| Wall clock (step 1 + step 2) | — | 6:43 + 25:05 | — | 19 min | 15 min |

Baseline sanity, unchanged across all three runs: the census on the **placed,
unrouted** board is 2997 pairs, 24 raw fails, all `same_footprint`, **0
board-level** — byte-for-byte the fixture's own 2026-08-06 record. Every fail
below is copper this run laid down.

### The frame correction: the replay was scored in the wrong coordinate space

The 2026-08-16 run reported **2 genuine gate leaks** (`/I_SENSE_OUT`↔`/SCAP_NEG`,
0.600 mm against 1.400 mm) and called them "the only line item Phase 2's own
machinery owns". They were an artifact of the ad-hoc replay's coordinate frame,
and the "Reproducing" note that recorded the convention had it **backwards**:

* `PCB.load` detects the `Edge.Cuts` origin and reports footprint positions
  **board-relative** (`schema/pcb.py::_detect_board_origin`). On softstart rev-C
  that origin is `(68.5, 55.0)`: `Q8` is `(at 186.5 117.0)` in the file and
  `(118.0, 62.0)` after loading.
* `(segment ...)` and `(via ...)` coordinates in the same file — and every
  `Segment` the router works with — are **sheet-absolute**.

So attach zones built straight off `PCB.load(...).footprints` sit 68.5 mm ×
55 mm away from the copper they are meant to waive. The production resolver has
always applied that shift (`cli/route_cmd.py::_pairwise_attach_zones`); only the
throwaway replay did not, and the shift is silently bidirectional — a misplaced
rectangle both fails to waive the copper it should and waives whatever unrelated
copper it lands on, so a wrong frame does not announce itself as an error.

Replayed in the correct frame on the 2026-08-21 board:

```
$ uv run python scripts/replay_pairwise_gate.py step2.kicad_pcb \
      --voltage-map vmap.json --dru 0.15
step2.kicad_pcb: 84 mapped nets, 1922 cross-pairs, DRU floor 0.150 mm
  trace-vs-trace pairwise violations (with #4506 attach zones): 0  (0 net pairs)

$ ... --no-attach-zones
  trace-vs-trace pairwise violations (without #4506 attach zones): 14  (2 net pairs)
    /GATE_NEG_A <-> /LED_K_NEG: 0.600 mm against 1.400 mm at (186.100, 116.600)
    /GATE_POS_A <-> /LED_K_POS: 0.600 mm against 1.400 mm at (142.100, 116.600)
```

The 14 waived instances are two rated MOSFET packages' own attach copper (`Q7`,
`Q8`) — #4506 working as designed on real geometry. **The router's own gate has
no finding on this board**, which is also what the run's inline post-route audit
said: it printed nothing. Step 1's output scores 0 the same way.

`/I_SENSE_OUT`↔`/SCAP_NEG`, the pair the previous run named, is **5.167 mm**
apart in trace copper against a 1.400 mm requirement.

To stop this recurring, the replay is no longer a throwaway: it is
`scripts/replay_pairwise_gate.py` over
`router.pairwise_clearance.board_pairwise_violations`, which reads copper and
zones from the same file in the same frame, and `tests/router/
test_pairwise_board_replay_4507.py` pins the behaviour at three board origins
(including `(0, 0)`, the one a frame-blind replay still gets right).

### What the residual 17 actually are — and why the gate cannot see them

Every one of the 17 is copper this run added (the placed board scores 0
board-level). Taking each failing pair and finding the copper primitives that
actually carry its minimum distance:

| Governing geometry | Count | Audited by the router's pairwise gate? |
|---|---|---|
| routed **trace ↔ foreign pad** | 8 | **No** |
| routed **via ↔ foreign pad** | 5 | **No** |
| routed **via ↔ foreign trace** | 4 | **No** |
| routed **trace ↔ foreign trace** | **0** | Yes — and it reports none |

That is the finding this run replaces "2 genuine leaks" with, and it is a
sharper one: **the residual is not a search that leaks, it is a gate that does
not look.** `segment_pair_violation` — the kernel behind the lattice's
search-time avoidance (#4602), the #4588 post-route audit and this replay — is
*trace↔trace only*, exactly as #4431 Phase 1 defined it. #4507's own Ask names
the asymmetry in item 3: "the C++ path walks **all** copper (segments, vias,
pads), unlike Phase 1's trace↔trace-only Python check". Phase 2 armed the C++
`Grid3D`/`Pathfinder` path, which does walk all copper — but the engine that
routes this board is the lattice, and the audit shared by every engine is the
trace-only one. So the copper the board actually fails on is, by construction,
invisible to both.

Four of the 17 are additionally sub-`--hv-threshold` pairs (11.7 V – 27.0 V:
`/PRE_D_POS`↔`/PRECHARGE_POS`, `/PRE_D_NEG`↔`/PRECHARGE_NEG`,
`/PGND`↔`/GATE_RTN_POS`, `/RTN_COM_NEG`↔`/GATE_RTN_NEG`), i.e. below the 30 V
default and therefore absent from the matrix entirely — the policy question
#4876 reported and did not change. The run says so itself:

```
NOTE: 811 further pair(s) sit below the 30V --hv-threshold yet still require
more than the 0.150mm DRU floor (worst: +12V<->PRE_D_NEG at 27V -> 0.530mm).
```

### What T4 needs now

Re-derived from this run; item 1 is the one that is #4507's own machinery.

1. **Widen the pairwise gate past trace↔trace** — pad and via copper, on the
   Python/lattice side, with the #4506 attach-zone waiver applied the same way.
   All 17 residual fails are in that class, and none of them can be fixed by a
   search whose acceptance predicate cannot see the geometry. The C++ side
   already walks all copper, so the shape of the check exists to mirror.
2. **`--hv-threshold` policy** (4 of 17) — a board that must pass `kct creepage`
   cleanly has to route with `--hv-threshold 0`. Reported since #4876; whether
   the *default* should change is an owner call.
3. **`kct zones hv-keepout` margin** — unaddressed; the pass still carves voids
   6–7 µm short of the requested clearance (2026-08-16 numbers below). Step 3 is
   not worth running until it is fixed.
4. **32 nets still unrouted** — `PLACEMENT_BOUND` / `CONGESTION_SATURATED` /
   `BUDGET_STARVED`. Placement capacity, not pairwise; unchanged in character
   since 2026-07-21.

---

This is the record of the **T4 manual criterion** of issue
[#4507](https://github.com/rjwalters/kicad-tools/issues/4507) (router Phase 2 of
epic #4431, pairwise HV-isolation clearance):

> **Manual**: softstart rev-C loop (`hardware/kicad/output_revc/LAYOUT_NOTES.md`
> recipe) — route completes with 0 board-level pairwise creepage census fails
> (was 111 cluster-relaxed / 79% blanket).

Every other #4507 criterion is automated and green on `main`. T4 is the one that
cannot live in CI: softstart rev-C is a **local-only** consumer fixture
(`github.com/rjwalters/softstart`, deliberately no CI presence here), so the
proof is a documented local run. This file is that record.

## The re-score: same recipe, corrected table

Re-run on `main` @ `090086b4` (C++ backend build 21) with the **same** fixture at
`7800b046`, the **same** four input files at the same md5s, and the same three
commands. The only difference from the pre-fix run below is that
`build_pairwise_clearance_table()` and the CLI's `--voltage-map` preload now
difference the potentials **as supplied** (#4868), so the banner prints
`84 mapped nets, 1922 cross-pairs` instead of 1823.

### Verdict: **T4 still FAILS** — 25, not 0

| Measure | T4 requires | **post-fix (this run)** | pre-fix | control: matrix off |
|---|---|---|---|---|
| Signal nets routed | 100 % | **50 / 82 (61 %)** | 49 / 82 | 50 / 82 |
| Cross-pairs in the matrix | = census | **1922** | 1823 | dormant |
| Board-level census fails, after step 2 | **0** | **25** | 57 | 107 |
| Board-level census fails, after step 3 (HV keepouts) | **0** | **35** | 42 | — (pass not run) |
| Router's own gate on the finished board (same-layer track↔track, #4506 zones honoured) | 0 | **2** | 3 | 54 |
| Router's inline post-route audit | 0 | **1** | 5 | (dormant) |

The headline: **restoring the 99 missing pairs and correcting 240 more did not
cost routability and did not grow the leak set.** #4867's acceptance criteria
expected the leaks to *grow* before shrinking; on this board they shrank in every
column — one net *more* routed (50 vs 49), less than half the board-level census
fails (25 vs 57 at the same step), and one fewer router-gate leak (2 vs 3).

Wall clock is **not** comparable this time: the grid arm ran concurrently on the
same machine (step 1 258 s, step 2 1291 s vs 109 s / 1032 s uncontended).
Nothing else in the recipe changed.

### Two new findings, neither of them a search defect

**1. `--hv-threshold` hides pairs the census scores (405 of them here).**
`build_pairwise_clearance_table()` omits every pair whose `|ΔV|` is below
`--hv-threshold` (30 V by default), leaving it at the scalar DRU floor. The
census has no threshold: it looks IEC 60664-1 up at the pair's *actual* `|ΔV|`,
and the standard's low-voltage rows are **above** a 0.2 mm fab floor:

```
mapped-net pairs below the 30 V threshold requiring more than the DRU floor : 811
  ...of which the census actually scores on this board                      : 405
their census requirement                                : 0.40 / 0.42 / 0.45 / 0.48 / 0.50 / 0.53 mm
their |ΔV| range                                        : 1.6 V .. 28.0 V
what the router requires for them                       : 0.200 mm (the DRU floor)
```

This is not the sign collapse returning — those 405 pairs difference correctly,
they are simply *below the threshold*. It is nonetheless load-bearing for T4:
**5 of the 25 residual board-level fails are sub-30 V pairs** (e.g.
`/PRE_D_POS`↔`/PRECHARGE_POS`, 0.400 mm against 0.420 mm at 11.7 V), and after
step 1 alone it was 10 of 36. No search can close them, because the requirement
never reaches the search.

The threshold itself is deliberate policy — it exists so LV↔LV pairs are not
over-segregated — so this run does **not** propose changing the default. What it
does propose (and #4507's next increment ships) is that the run *say so*: the
`--voltage-map` banner now reports the hidden-pair count, the worst pair, and the
`--hv-threshold 0` escape hatch, instead of printing a cross-pair count that
reads like full coverage. Verified end to end: at `--hv-threshold 0` the pairs
enter the matrix and the #4588 audit fails the copper the census was already
failing.

**2. The HV keepout pass now makes this board worse (25 → 35).** Pre-fix, step 3
retired 15 fails (57 → 42). Post-fix it *adds* 15 and retires 5:

```
kct zones hv-keepout --clearance 1.6 --refill   →  8 voids planned (pre-fix: 5)
  retired : 5 genuine fails (0.425-0.967 mm against 1.3-1.6 mm)
  created : 15 marginal fails at 1.5931-1.5942 mm against 1.600 mm
```

Every one of the 15 is the *same* defect: the carved void leaves the pour edge
**6-7 µm short** of the clearance it was asked for, so a pair that was not
failing before the pass fails by 0.4 % of its requirement afterwards. Carving at
exactly the required clearance has no margin for the polygon discretisation of
the void, and the refill lands inside. This is `zones/hv_keepout.py`, not the
router search — recorded here (with the numbers to reproduce it) rather than
fixed in a Phase 2 PR. **The best board this recipe produces is therefore step 2
at 25, and step 3 should be run with a clearance margin (or not at all) until
that is addressed.**

### What the residual 25 actually are

| Class | Count | Can Phase 2's search fix it? |
|---|---|---|
| Involves a plane pour (`GND` / `+3.3V` auto-pour copper) | 9 | No — the router's gate is trace↔trace; pour geometry is the census' remit (#3901) |
| Sub-30 V pair, absent from the matrix by `--hv-threshold` | 5 (1 also pour) | No — the requirement never reaches the search |
| Pad/via geometry or placement-fixed copper | 12 | Partly — but only 1 net pair of the 12 is same-layer track↔track |
| **Router's own gate on the finished board** | **2 instances, 1 net pair** | Yes — `/I_SENSE_OUT`↔`/SCAP_NEG`, 0.600 mm against 1.400 mm on B.Cu |

The 2 remaining gate leaks are what the router is genuinely responsible for, down
from 3 pre-fix and 54 with the matrix off. Replaying `segment_pair_violation`
over the finished board with and without the #4506 attach zones:

```
step2.kicad_pcb: 1940 segments over 55 nets, 135 attach zones, 1922 cross-pairs
  track-track pairwise violations WITH #4506 attach zones :  2  (1 net pair)
  track-track pairwise violations WITHOUT attach zones    :  8  (3 net pairs)
```

The 6 waived instances are the same two optocoupler pairs the pre-fix run found
(`/GATE_POS_A`↔`/LED_K_POS`, `/GATE_NEG_A`↔`/LED_K_NEG`, 0.892-1.000 mm inside
the part's own pad bbox) — #4506 working as designed on real geometry, twice.

> **Corrected 2026-08-21.** The replay above was run with **unshifted** attach
> zones, i.e. 68.5 mm × 55.0 mm away from the copper they were meant to waive,
> so both counts in that code block are unreliable and the "2 genuine gate
> leaks" row in the table above is an artifact. See
> [The frame correction](#the-frame-correction-the-replay-was-scored-in-the-wrong-coordinate-space):
> correctly framed, the gate reports **0** on a board routed by the same recipe,
> and `/I_SENSE_OUT`↔`/SCAP_NEG` is 5.167 mm apart in trace copper.

### The grid arm, re-run

Same third arm as before (the one that exercises #4507's *own* C++ kernels rather
than the lattice's): **0 / 82 nets** (pre-fix 2 / 82), 55 partial, 27 with no
segments, 22 min 44 s, 12 HV pairwise violations in the copper it did commit. The
fine-pitch escape wall this board hits is unchanged and predates #4431; the
stricter table costs it the two nets it used to finish.

`FAILURE_PAIRWISE_BLOCKED` (#4860) fired on **8** nets (pre-fix 10), naming six
distinct blockers:

| drained net | blocker reported | blocker's name |
|---|---|---|
| `/SCAP_NEG_RTN`, `/GATE_NEG_A` | `blocking net id 33` | `/LED_A_NEG` |
| `/V_AC_SENSE_RAW`, `/V_BUS_DVDT` | `blocking net id 4` | `/AC_LINE` |
| `/PRECHARGE_NEG` | `blocking net id 50` | `/SCAP_NEG` |
| `/STATUS_LED` | `blocking net id 54` | `/SRC_NEG` |
| `/SRC_NEG` | `blocking net id 51` | `/SCAP_NEG_RTN` |
| `/GATE_RTN_NEG` | `blocking net id 21` | `/GATE_BUS_NEG` |

The right-hand column is a lookup this run had to do by hand — the operator-facing
gap the pre-fix run flagged in passing. #4507's next increment closes it: the
diagnostic resolves the id through the pathfinder's own net map, falling back to
the bare id only when no map is threaded. Confirmed on this same board (a
shorter, `--timeout 420` replay of the arm above, on the patched tree):

```
Net /SCAP_NEG_RTN: C++ pathfinder gave up (open set drained with expansions
  refused by pairwise (HV-isolation) clearance (blocking net /LED_A_NEG));
  falling back to the pure-Python A* ...
```

— the same refusal that printed `blocking net id 33` two paragraphs above.

### What T4 needs now

The pre-fix list's item 1 (the sign collapse) is **done**. The rest, re-derived
from this run:

1. **The `--hv-threshold` policy question** (5 of 25). A board that must pass
   `kct creepage` cleanly has to route with `--hv-threshold 0`, which no
   documentation said and no run reported. Now reported; whether the *default*
   should change is an owner call, not a Builder one.
2. **Pour-vs-trace scope** (9 of 25). The router's gate audits trace copper; the
   census measures pours too. Either the gate grows a pour-aware pass or T4 is
   scored on a pour-free board. Not a Phase 2 deliverable (#3901).
3. **`kct zones hv-keepout` margin** (the 25 → 35 regression above).
4. ~~**The 2 genuine gate leaks**~~ — **withdrawn 2026-08-21**: a frame artifact
   of the ad-hoc replay, not copper. The line item Phase 2's own machinery owns
   is instead "widen the gate past trace↔trace", see
   [What T4 needs now](#what-t4-needs-now) at the top.
5. **32 nets still unrouted** — `PLACEMENT_BOUND` / `CONGESTION_SATURATED` /
   `BUDGET_STARVED`, unchanged in character since 2026-07-21. Placement capacity,
   not pairwise.

## Verdict of the pre-fix run: **T4 FAILS** — but the machinery is measurably doing its job

> Everything from here down is the original 2026-08-15 pre-fix measurement,
> retained as the record of the diagnosis. Its counts are superseded by
> [the re-score](#the-re-score-same-recipe-corrected-table) above.

| Measure | T4 requires | matrix **on** | control: matrix **off** |
|---|---|---|---|
| Signal nets routed | 100 % | **49 / 82 (60 %)** | 50 / 82 (61 %) |
| Board-level pairwise creepage census fails | **0** | **42** | **107** |
| Router's own gate on the finished board (same-layer track↔track) | 0 | **3** | **54** |
| Wall clock (step 1 + step 2) | — | 19 min | 15 min |

Both arms are the same recipe on the same placed board with the same tool build;
the only difference is whether `--voltage-map` is supplied. So the search-time
pairwise avoidance is **not** a paper feature on this board: it removes **54 → 3**
same-layer HV track↔track violations (94 %) for the cost of **one net** and
**~22 % wall time**. That also retires the fixture journal's 2026-08-06
conclusion that "turning on the enforcement costs ~40 % of routability" — on
current `main` it costs ~2 %.

It still is not zero, and T4 asks for zero. The headline of this run is therefore
not the count but the **root cause it isolated**, which was not previously known
and is not visible from any automated test in this repo:

> **`build_pairwise_clearance_table()` collapses signed net potentials with
> `abs()` before differencing them, so on a bipolar (±) voltage map the router's
> pairwise matrix silently under-constrains 17.6 % of the board's cross-pairs —
> including every POS↔NEG bank pair, where the census requires 3.20 mm and the
> router requires 0.**

Details in [The root cause](#the-root-cause-signed-potentials-are-collapsed-to-magnitudes)
below. Until that is fixed, T4 cannot pass on *any* HV board whose voltage map is
signed about its reference — which is the normal encoding for a ± bank topology,
and is exactly what epic #4431 exists to serve.

## Run configuration

Everything below ran locally on macOS (darwin 25.6.0), single machine, no CI.
Three arms were run: the recipe **with** the pairwise matrix, the same recipe
**without** it (control), and a **grid/negotiated** arm that exercises #4507's own
C++ kernels rather than the lattice's.

| Input | Value |
|---|---|
| Fixture repo | `github.com/rjwalters/softstart` @ `7800b046973be7464a6c491557bcfd48aa3e413f` (branch `hw/route-revc-v0200`), **read-only** |
| Board | `hardware/kicad/output_revc/softstart_revc.kicad_pcb`, md5 `7d82599e21c1de8d3faab3e94f444515` (160 × 100 mm, 4-layer, JLCPCB 2 oz outer / 0.5 oz inner) |
| Routing net-class map | `output_revc/net_class_map.json` md5 `9184a661061ccd3005a96a4bec73d9d8` |
| Voltage map | `output_revc/vmap.json` md5 `cc72a701a6c4c3c335859bcc6029ab94` — 84 nets, **signed** volts about `GND = AC_NEUTRAL` (13 nets negative) |
| Census membership map | `output_revc/creepage_class_map.json` md5 `6c3b324ad533db38e136036c2dd4baa6` (37 HV-class nets) |
| Tool | this repo, `main` @ `2d5fc9b2`, C++ router backend **build version 21** (`kct build-native --check`) |

Inputs were **copied** into a scratch directory and routed there; the softstart
working tree was never written to (`kct route` writes a `net_class_map.json`
sidecar next to its *output*, which is why the copy matters — see #4428).

### Commands

Recipe per `LAYOUT_NOTES.md` "Recipe that terminated" (2026-08-06 entry), plus
that entry's own next-step (a), the HV plane keepout pass:

```bash
# Step 1 — HV backbone forced onto the outer layers
kct route softstart_revc.kicad_pcb -o step1.kicad_pcb \
  --route-engine lattice --strategy basic --layers 2 \
  --nets "/AC_NEUTRAL,/FUSED_LINE,/PRECHARGE_POS,/RF_EN,/I_SENSE_OUT,/V_BANK_POS_SENSE,/V_AC_SENSE" \
  --net-class-map net_class_map.json \
  --voltage-map vmap.json \
  --creepage-standard iec60664 --pollution-degree 2 --material-group IIIa \
  --manufacturer jlcpcb --copper 2

# Step 2 — bounded completion pass over everything else
kct route step1.kicad_pcb -o step2.kicad_pcb \
  --complete --complete-exclude-nets "GND,+3.3V" --complete-report report.json \
  --net-class-map net_class_map.json --voltage-map vmap.json \
  --creepage-standard iec60664 --pollution-degree 2 --material-group IIIa \
  --manufacturer jlcpcb --copper 2 --layers 4

# Step 3 — HV plane keepouts (LAYOUT_NOTES 2026-08-06 "next session" item (a))
kct zones hv-keepout step2.kicad_pcb -o step3_keepout.kicad_pcb \
  --net-class-map creepage_class_map.json --clearance 1.6 --refill

# Gate — the census T4 is scored against
kct creepage <board> --voltage-map vmap.json --net-class-map creepage_class_map.json \
  --standard iec60664 --pollution-degree 2 --material-group IIIa \
  --working-voltage 250 --waive-same-footprint --format json
```

## Measured results

### Baseline reproduces the fixture's own record exactly

Census on the **placed, unrouted** board: 2997 pairs, **24 raw fails, all
`same_footprint`, 0 board-level**, `gate_passed: true`. That is byte-for-byte the
figure `LAYOUT_NOTES.md` records for 2026-08-06, so the harness is sound and every
board-level fail below is copper this run laid down.

### Step 1 — HV backbone

| | with `--voltage-map` | control, no `--voltage-map` |
|---|---|---|
| Nets connected | **7 / 7** | 7 / 7 |
| Wall time | 109 s | 32 s |
| Pairwise banner | `84 mapped nets, 1823 cross-pairs (route-time rejection + post-route audit)` | absent (dormant) |

DRC after step 1: 275 errors, **all ampacity** (the expected pre-`reinforce`
state — softstart's 15 A backbone is buttressed with 16 AWG wire by design), 0
clearance errors.

Census after step 1: 37 board-level fails, of which **33 involve a plane pour**
that `kct route`'s auto-pour created (`Auto-pour: created 2 zone(s) for +3.3V,
GND`) and 4 do not. Same-layer track↔track fails: **0**.

### Step 2 — completion pass

* **42 / 76** unconnected nets routed (55 %); 3 partial, 31 with no segments at
  all. Control arm (no `--voltage-map`): **43 / 75**, i.e. the matrix costs
  exactly one net on this board.
* 1031.8 s of a 5610 s budget — converged on its own, `deadline` not hit. Wall
  clock 17 min 56 s (control: 14 min 05 s).
* Combined with step 1: **49 / 82 signal nets (60 %)**. That is *identical* to
  the 2026-08-06 v0.20.0 session's 49/82 — five merged Phase-2 increments later,
  this board's completion has not moved.
* The 31 no-path nets self-classify as `PLACEMENT_BOUND` /
  `CONGESTION_SATURATED` / `BUDGET_STARVED`, with the recurring
  `manufacturer 'jlcpcb' does not support via-in-pad` tier note. **This residual
  is a placement problem, not a pairwise problem** — it is the same wall the
  fixture's journal has recorded since 2026-07-21.
* **The post-route audit now speaks.** It reported 5 HV pairwise clearance
  violations (2 distinct net pairs) inline in the failure banner. On 2026-08-06
  the same board printed *nothing* — that regression was #4699, fixed by #4756,
  and this run is its independent confirmation on the real board.

### Step 3 — HV plane keepouts, and the final census

`kct zones hv-keepout --clearance 1.6 --refill` planned 5 voids over In1.Cu/In2.Cu.

| Census (waiving same-footprint pairs) | raw fails | waived | **board-level** |
|---|---|---|---|
| placed board (baseline) | 24 | 24 | **0** |
| after step 1 | 60 | 23 | **37** |
| after step 2 | 73 | 16 | **57** |
| after step 3 (HV keepouts) | 58 | 16 | **42** |
| control arm, matrix off (no keepout pass) | 120 | 13 | **107** |

The keepout pass retired 15 of the 31 plane-involved fails. The 42 that remain
break down as 16 still plane-involved and 26 conductor↔conductor.

The control arm's **107** is the number to hold the 42 against: it is the same
recipe with `--voltage-map` withheld, and it lands squarely in the range T4's own
parenthetical records for the pre-Phase-2 era ("was 111 cluster-relaxed"). The
matrix moves this board 107 → 42 board-level fails; T4's target is 0.

### Attributing the 26: which are the router's fault?

`kct creepage` is net-pair-keyed and reports only each pair's **minimum**
distance, over pads *and* tracks (`kind: "conductor"` for both), and it has no
concept of the #4506 attach-zone exemption. So the census count alone cannot say
what the router was responsible for avoiding. To settle it, the router's **own
gate kernel** (`segment_pair_violation` — the same call the post-route audit
makes) was replayed over every same-layer track pair on the finished board, once
with the #4506 attach zones and once without:

```
matrix ON  (step3_keepout.kicad_pcb): 1885 segments, 135 attach zones
  track-track pairwise violations WITH #4506 attach zones :  3
  track-track pairwise violations WITHOUT attach zones    :  5

matrix OFF (control ctl2.kicad_pcb) : 2091 segments, 135 attach zones
  track-track pairwise violations WITH #4506 attach zones : 54
  track-track pairwise violations WITHOUT attach zones    : 57
```

**Genuine leaks (3)** — the router's own gate says this copper is bad:

| net a | net b | layer | gap | required |
|---|---|---|---|---|
| `/I_SENSE_OUT` | `/SCAP_NEG` | B.Cu | 0.400 mm | 1.400 mm |
| `/I_SENSE_OUT` | `/SCAP_NEG` | F.Cu | 0.600 mm | 1.400 mm |
| `/FUSED_LINE` | `/NTC_SENSE` | F.Cu | 0.931 mm | 1.600 mm |

**Waived by an attach zone (2)** — `/GATE_POS_A`↔`/LED_K_POS` and
`/GATE_NEG_A`↔`/LED_K_NEG`, both 0.892 mm against 1.400 mm, both inside an
optocoupler's own pad bounding box. That is #4506 working as designed
(manufacturer-qualified functional insulation inside a rated package); the census
cannot see the exemption, which is a *known* scope difference, not a defect.

Crucially, those 3 are **exactly** what the router's own post-route audit printed
during the run (its 5 instances collapse to the same 2 net pairs over the same 3
pair-layer keys). The audit is neither silent nor under-reporting any more; it is
*correct about its own matrix*. The matrix is the problem.

## The root cause: signed potentials are collapsed to magnitudes

The independent geometric classification of the census output
(softstart's own `classify_creepage_fails.py`) finds **8** same-layer track↔track
shortfalls on the final board. Two of them are the gate leaks above, two are the
attach-zone waivers above — and **four** the router's gate does not consider
violations at all. Those four are the tell:

| net a (V) | net b (V) | census delta-V | census requires | router requires |
|---|---|---|---|---|
| `/FUSED_LINE` (+150) | `/GATE_BUS_NEG` (−150) | 300 V | 3.20 mm | **0** (pair absent) |
| `/LED_A_POS` (+90) | `/SCAP_NEG` (−90) | 180 V | 2.00 mm | **0** (pair absent) |
| `/LED_K_POS` (+90) | `/SCAP_NEG` (−90) | 180 V | 2.00 mm | **0** (pair absent) |
| `/SCAP_NEG` (−90) | `/SRC_POS` (+150) | 240 V | 2.50 mm | 1.25 mm |

Every one of them is a POS↔NEG pair.

`build_pairwise_clearance_table()`
(`src/kicad_tools/router/pairwise_clearance.py`) normalises its input as

```python
normalised = {_norm_net_key(name): abs(float(v)) for name, v in net_voltages.items()}
```

and only then hands it to `build_required_by_domain_pair`, which differences the
values it is given. The census
(`creepage/engine.py`, `VoltageInterval`) differences the **signed** endpoints:
`dv = max(|a.hi − b.lo|, |b.hi − a.lo|)`.

Two consumers, one `vmap.json`, opposite interpretations. For a net at +150 V and
one at −150 V the census sees 300 V and the router sees **0 V** — below the 30 V
`hv_threshold`, so the pair is not merely under-required, it is **absent from the
matrix**, invisible to every engine's search *and* to the post-route audit.

Measured over softstart's own 84-net map:

```
cross-pairs, signed (census semantics)    : 1922
cross-pairs, magnitude (router semantics) : 1823      <- the number the banner prints
pairs MISSING entirely from the router matrix : 99
pairs present but UNDER-required              : 240
total under-constrained                       : 339 / 1922  (17.6 %)
worst: V_RSV_NEG <-> V_RSV_POS   census 3.20 mm   router 0.00 mm
```

Every one of the eight worst-shortfall rows is a POS↔NEG pair at the full 300 V
bank span — the requirement collapses hardest precisely where the isolation
matters most. The
`HV pairwise clearance: 84 mapped nets, 1823 cross-pairs` banner is itself the
artifact: 1823 rather than 1922 because 99 pairs were differenced to zero.

The function's docstring says the input may be "signed or magnitude; magnitudes
are taken internally". That is the defect stated as a feature: the two encodings
are not interchangeable, the router cannot tell them apart, and softstart's map
declares itself signed in its own `_comment` ("signed volts about GND=AC_NEUTRAL,
non-isolated"). The correct behaviour is to difference the values as supplied —
matching the census, which is the standard the gate is scored against.

This is a **safety-gate under-constraint**, not merely a missed optimisation: a
`--voltage-map` run can print a reassuring banner, pass its own post-route audit,
and still commit ±300 V copper at the DRU floor.

**Resolved by #4867.** Potentials are now differenced as supplied, and the
sidecar is read through `router.pairwise_clearance.load_signed_voltage_map`
(the census' own parse contract) rather than placement's magnitude-only loader.
Replaying the block above against the same `vmap.json` on the fixed tree:

```
cross-pairs, signed (post-fix)                : 1922   <- what the banner now prints
cross-pairs, magnitude (pre-fix)              : 1823
pairs that RE-ENTER the matrix                :   99
pairs previously UNDER-required               :  240
total previously under-constrained            :  339 / 1922  (17.6 %)
pairs LOST by the fix                         :    0
worst: AC_LINE <-> {BOOST_B,GATE_BUS,GATE_DRV,SRC,TRK}_NEG   3.20 mm (was 0.00)
```

## Which engine actually ran — and what that means for #4507

Worth stating plainly, because it bounds what this run does and does not prove
about *this issue's* code:

`--complete` selects the **lattice** engine (it says so: `--complete: routing
unconnected links on the lattice engine`). The lattice has its own search-time
pairwise implementation — `router/lattice/pairwise.py` (`LatticePairwise`, #4602)
— which is *not* the C++ `Grid3D` / `Pathfinder` domain-matrix machinery that
#4507's Phase 2 shipped (#4510/#4511/#4780/#4791/#4849/#4860). Both read the same
`DesignRules.pairwise_clearance` table, which is why the root cause above hits
both, but the recipe that terminates on this board exercises the lattice search.

So this run was repeated on the grid/negotiated engine — where #4507's own kernels
live — as a third arm:

```bash
kct route softstart_revc.kicad_pcb -o grid.kicad_pcb \
  --route-engine grid --strategy negotiated --layers 4 \
  --net-class-map net_class_map.json --voltage-map vmap.json \
  --creepage-standard iec60664 --pollution-degree 2 --material-group IIIa \
  --manufacturer jlcpcb --copper 2 --timeout 1200 --per-net-timeout 20 \
  --max-cells 25000000
```

Result: **2 / 82 nets** (55 partial, 25 with no segments), 23 min 35 s, 12 HV
pairwise violations in copper it did commit. That is the fixture journal's old
verdict unchanged (it records 4/82 at 0.05 mm grid, 3/82 at 0.03175 mm): the grid
engine cannot route this board at all, for reasons that predate #4431 — 77 % of
pads are off-grid at the auto-selected 0.05 mm resolution, and the log is a wall
of `no path (C++ A* open set exhausted)` fine-pitch escape failures.

The arm is not wasted, though: it is the first observation of **#4860's
`FAILURE_PAIRWISE_BLOCKED` firing on a real HV board**. Ten nets drained the C++
open set specifically on cross-domain refusals and said so —

```
Net /LED_K_NEG: C++ pathfinder gave up (open set drained with expansions refused
  by pairwise (HV-isolation) clearance (blocking net id 51)); falling back to the
  pure-Python A*
```

— for `/GATE_NEG_A`, `/GATE_RTN_NEG`, `/LED_A_NEG`, `/LED_K_NEG`,
`/PRECHARGE_NEG`, `/SCAP_NEG_RTN`, `/SRC_NEG`, `/STATUS_LED`, `/V_AC_SENSE_RAW`
and `/V_BUS_DVDT`. Before #4860 every one of these was an undifferentiated
`FAILURE_NO_PATH`. Note the operator-facing gap: the message names an integer
net **id**, not the net name (consistent with the older
`FAILURE_VIA_VIA_BLOCKED` phrasing it mirrors, so this is a shared wording
choice rather than a regression — but on a real board `blocking net id 51` is not
actionable without a lookup).

**Bottom line for #4507's own code**: softstart rev-C is today a proof of the
*shared* `DesignRules.pairwise_clearance` table (which the lattice consumes), not
of the C++ grid search. A T4 that certifies #4507's own search-time avoidance
needs either a board the grid engine can route, or the grid engine's fine-pitch
escape wall cleared first — neither of which is a Phase 2 deliverable.

## What would have to change for T4 to pass

Nothing on this list is a Phase 2 regression: they are, respectively, a
table-construction defect shared by every engine, its downstream residual, a
placement capacity wall, and a definitional mismatch between two gates.

1. **Fix the sign collapse** (above). Without it no bipolar HV board can be
   gate-clean, because the gate is not asking for the right numbers. This is the
   only blocker on the list that is a correctness defect rather than a capacity
   limit.
2. **Close the 3 genuine leaks.** Once (1) lands the leak set will *grow* before
   it shrinks (99 pairs re-enter the matrix), so measure again after the fix
   rather than extrapolating from 3.
3. **The 31 no-path nets are a placement problem.** Every one classifies
   `PLACEMENT_BOUND` / `CONGESTION_SATURATED` / `BUDGET_STARVED` with a
   via-in-pad tier note. The fixture's own journal has proposed the levers
   (`spatial_keepouts` corridors per #4605, board resize, 6-layer). None of them
   is a #4507 deliverable.
4. **The census/attach-zone scope difference is expected, not a bug** — but it
   means "0 board-level census fails" and "0 router-gate violations" are
   different acceptance targets. T4 asks for the former; the fixture's own
   `creepage_triage.py` gate is the honest scorer of it.

## #4507 criterion status after this run

> Re-scored against the post-#4868 run. Rows 1-3 and T1-T3 are unchanged; the
> "observed live" evidence in 2b / 3 and the T4 row carry the **re-score's**
> numbers, with the pre-fix figures in parentheses.
>
> **T4 superseded 2026-08-21**: still FAILS, now at **17** board-level fails with
> **0** router-gate findings — the "2 router-gate leaks" cited below were a
> replay frame artifact. Current numbers and the residual's real composition are
> in [the 2026-08-21 re-run](#the-2026-08-21-re-run-the-router-gate-is-clean-and-what-that-leaves).

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | C++ `validate_route` + search-time check consume a domain-id array + domain-pair matrix | **MET** | `Grid3D::set_pairwise_domains` / `pairwise_required_clearance` (#4510 via #4524/#4533) |
| 2 | Search-time avoidance: hard blocking + halo/pricing widening around HV copper | **MET** | C++ kernels + `pairwise_avoidance_cost` (#4511); Python-fallback mirrors (#4791); soft gradient (#4849); nearest-band pricing (#4859) |
| 2b | Failed avoidance must be *actionable*, not silent | **MET** | `FAILURE_PAIRWISE_BLOCKED` + blocker net → `via_blocked_ripup` (#4860). **Observed live here**: 8 (pre-fix 10) nets on the grid arm drained specifically on cross-domain refusals and named their blocker — by *id* in this run, by *name* after the follow-up increment |
| 3 | #4506 attach-zone exemption threaded into the C++ check | **MET** | `Grid3D::set_attach_zones` / `attach_zone_exempts(..., layer)`; layer scoping in #4780. **Confirmed live on a real board here**: 6 of 8 (pre-fix 2 of 5) track-track shortfalls waived inside an optocoupler's pad bbox |
| T1 | Parity: C++ and Python validators agree | **MET** | `tests/router/test_pairwise_cpp_parity.py` |
| T2 | Two-domain fixture converges | **MET** | `test_two_domain_board_converges_with_search_time_avoidance` |
| T3 | Backward compat: no `--voltage-map` → unchanged routes | **MET** | `test_no_voltage_map_route_leaves_grid_dormant` + this run's control arm (dormant, no banner, 7/7 in 32 s) |
| **T4** | **softstart rev-C: route completes, 0 board-level census fails** | **FAILS — re-scored, and no longer blocked on a correctness defect** | **50/82 nets (was 49); 25 board-level fails after step 2 (was 57), 35 after step 3 (was 42); 2 router-gate leaks (was 3); 1922 cross-pairs (was 1823). The sign collapse is fixed and did *not* grow the leak set. What remains: 9 pour-scope + 5 sub-`--hv-threshold` + 12 pad/placement fails and 32 unrouted nets — none of them a Phase 2 search defect** |

## Reproducing

Everything is deterministic given the fixture and this repo at `090086b4`
(pre-fix run: `2d5fc9b2`) with the C++ backend at build 21. The four analysis
helpers used above are:

* softstart's own `hardware/kicad/classify_creepage_fails.py` (census → track↔track
  vs pad-involved), used unmodified;
* softstart's own `hardware/kicad/creepage_triage.py` (the fixture's gate);
* ~~a throwaway replay of `segment_pair_violation` over the finished board with
  and without `build_attach_zones(...)` — 60 lines, reproduced from the snippet
  in [Attributing the 26](#attributing-the-26-which-are-the-routers-fault)~~
  **superseded, and its frame note was wrong — see
  [The frame correction](#the-frame-correction-the-replay-was-scored-in-the-wrong-coordinate-space).**
  Use `scripts/replay_pairwise_gate.py` (backed by
  `router.pairwise_clearance.board_pairwise_violations`), which resolves the
  copper and the #4506 zones from the same file in the same frame;
* a `subthreshold_coverage_gap(...)` call over the same `vmap.json` for the
  `--hv-threshold` numbers — public API as of the increment that reported them.

No board artifacts are committed to this repo: routed boards are `DO_NOT_FAB`
work-in-progress and live in scratch, per the fixture repo's own policy.
