# Issue #5518 — Phase 1c access-loss witness runs (boards 05 / 06 / 07)

*Epic #5508, Phase 1c. **Evidence only**: no routing, recipe, budget, seed or
clearance change of any kind, and no pad was "rescued". The only source-tree
files this PR adds are the ones in this directory. The run tree's
`git diff --stat` was empty for every arm — the capture harness was injected
via `PYTHONPATH` as a `sitecustomize` observer (retained in
`captured-scripts.tar.gz`), never as a source edit.*

## Headline result

**No committed route was named as the cause of any of the four motivating
pads.** Every explicitly interrogated terminal — board-05 `ISENSE_A-`,
board-06 `U2.B1`, board-07 `U4.C2`, and the board-07 `DQ3` negative control —
reports the same verdict shape:

| `access_at_escape_end` | `first_closed_at` | `final_access` | `closing_nets` |
|---|---|---|---|
| `non-empty` | `null` | `non-empty` | `[]` |

Against [the 1b format note's reading table](../../access-witness.md#reading-it-correctly)
that is row 2 verbatim: *"Nothing stranded it. The pad was reachable
throughout and the search declined to reach it — issue #5509's category, not
this epic's."*

**The predicate is not stuck on `non-empty`, though.** In board-07 `run2`'s
*second* capture the default (untargeted) witness fires on eight pads —
`U5.1` … `U5.8` (nets `A0` … `A7`) — with `first_closed_at`
`{"pass": "routing", "iteration": 0}` and `first_closed_kind: "commit"`. Their
`closing_copper_class` is **`board_edge`** and `closing_refs` is
`["<board-edge>"]`, i.e. the copper that rejected the last candidate is the
board outline, not a route. So the witness does report closures on this tree —
it just never names a *committed route* as one. See the board-07 section; two
oddities in those entries are recorded there and filed.

Consequences for the epic, stated plainly:

- **#5398 (board-05 `ISENSE_A-`) is not reproduced as an access-loss instance**
  under the 1a/1b predicate on this tree. Be precise about the evidence base:
  the predicate was evaluated on **two** of the four runs — `run3` and `run4`,
  the instrumented pair; `witness-board05.json`'s `runs.run1` / `runs.run2`
  carry `harness_dumps: []` and `offline_replays: []` and contribute only
  wall-time and artifact rows. Across those two runs the six offline replays
  agree. All four runs *do* agree on the **router-side** picture (identical
  failed-net set, identical `blocked_path` causes, `Total nets routed: 41`) —
  but that agreement is read from the recipe logs, which this package
  deliberately does not commit, so it is not checkable from these JSON files.
- **#5504 (board-06 `U2.B1`) is not an access-loss instance** in the option-ON
  arm (2 repeats, byte-identical copper). The option-OFF control did **not
  finish** — see "Incomplete work" below; that row is PARTIAL, not clean.
- **#5507 (board-07 `U4.C2`) is not an access-loss instance**; it belongs to the
  recipe-local pour-escape model, which is the redirect the issue body
  anticipated for this row.
- **The `DQ3` negative control PASSES** — and, unlike the earlier draft of this
  package claimed, it is corroborated: in the very same evaluation that
  reports `DQ3` reachable, eight other pads come back `empty` with a named
  closing record. The predicate discriminates.

Phase 2 should therefore **not** be scoped as "stop the router stranding pads on
boards 05/06/07": on this tree, at these clearances, none of the four
interrogated pads is stranded by committed copper. The epic's remaining premise
rests on the 1a fixture (#5516) rather than on any of the three production
boards — with the board-06 OFF control still owed.

## Acceptance criteria — status

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | `kct build-native --check` reports `available` before any board run | **PASS** | verbatim output under "Provenance" (re-verified after the runs; identical) |
| 2 | Board-05 `ISENSE_A-` witness; PASS only if `closing_nets ⊇ {U3.43, U3.45, their vias, B-}` **and** closure is post-prephase | **FAIL (recorded verbatim)** | `witness-board05.json`; `closing_nets == []` in all 6 offline replays across 2 instrumented runs and both layer-escalation attempts. Also: U3.43/U3.45 are not on `ISENSE_A-` on this tree |
| 3 | Board-06 `U2.B1` witness with `clear_avoidance_after_connection=True`; named set cross-posted for #5504 | **PASS (option-ON arm); control PARTIAL** | `witness-board06.json`; the named set is the **empty set**. The kwarg is `True` by default on the run tree (#5609), so the ON arm is plain `main`. The OFF control did not finish — see "Incomplete work" |
| 4 | Board-07 `U4.C2` recorded as one of the outcomes, with a recommendation for #5507 | **PASS** | `witness-board07.json`; outcome = **non-empty** (the issue's second outcome). Recommendation: redirect #5507 to the recipe-local pour-escape model |
| 5 | Board-07 `DQ3` negative control: access non-empty **and** `closing_nets == []` | **PASS** | `witness-board07.json`, both `DQ3` terminals (U1.28, U2.4). No 1a/1b defect filed on this account |
| 6 | Every routed `.kicad_pcb` cross-gated with `kicad-cli pcb drc --refill-zones`; no artifact committed | **PARTIAL** | `drc-summary.json`, 17 artifacts. The two OFF-arm runs produced no routed board to gate |
| 7 | `README.md` + `manifest.json` (+ JSON + scripts tarball) committed; manifest carries provenance and SHA256 of every output | **PASS** | this directory |
| 8 | `git status --porcelain` clean in the main checkout | **PASS** | all work done in `.loom/worktrees/issue-5518`; see the PR body |

## Incomplete work

**The board-06 option-OFF control arm did not finish.** Both repeats
(`06-off-1`, `06-off-2`, started 09:04:59Z / 09:05:09Z) reached the diff-pair
re-route stage and stalled on the last net:

```
    Re-routing net 5/5: USB3_RX1-... (1660.2s)
Net USB3_RX1-: post-route clearance validation rejected every candidate (6 attempts); ...
Net USB3_RX1-: C++ pathfinder gave up (post-route clearance validation failed;
  exhausted 5 resume attempts); falling back to the pure-Python A*
  (typically 10-100x slower -- this net may take minutes).
```

Both processes were still inside that pure-Python A* (which the recipe runs with
`timeout=None`) after **1 h 45 m**, having produced no further log output for the
last **1 h 13 m** of that, and were terminated (`SIGTERM`, `RC=143` recorded in
both `.times` files at 10:50:25Z) by the builder at the evidence budget.
Two earlier attempts at the same arm reached the identical stall and were
likewise killed (`board06-off-1.aborted.log`, RC=143 at 08:06:42Z;
`board06-off-1.killed-session.log`, killed during a session roll). So this is not
a one-off: the OFF arm reproducibly stalls at `USB3_RX1-`, at the same
coordinates (`seg-pad@(111.000,57.500) gap=0.1125`, `seg-seg@(109.600,58.000)
gap=0.0856`), which is the fallback pattern of #5599. The host was concurrently
running other worktrees' board-06 jobs, so the elapsed times are an upper bound
on a quiet machine, not a measurement.

Consequences, stated so a reviewer does not have to infer them:

- The OFF/ON comparison the curator asked for **was not made**. Nothing in this
  package supports a claim about what the avoidance leak does to `U2.B1`.
- The `off-1 vs off-2` repeat-identity check is `comparable: false`.
- Two of the six board-06 DRC gates are missing (no artifact was written).

The ON arm — the arm the AC actually names — is complete, in duplicate, on
byte-identical copper.

## Provenance

- **Run date**: 2026-09-21 (UTC; all `.times` stamps in this package are UTC)
- **Run tree**: `origin/main` @ `37171ac08263f973bdad5df349f8f8c9c8f80086`
  (`perf(ci): exclude kicad-cli --help/--version probes from the native permit gate (#5624)`)
- **Host**: macOS 27.0, arm64, 28 cores. All runs local — a docs-only PR trips
  no board CI job (`changes.code` excludes `docs/**` and `**/*.md`) and both
  board-07 CI jobs are gated off behind `vars.BOARD_07_CI_ENABLED`.
- **`kicad-cli`**: 10.0.6 (`/opt/homebrew/bin/kicad-cli`)
- **`uv run kct build-native --check`** in the worktree, before any board run
  (AC item 1), verbatim:

  ```
  C++ backend: available (version 1.0.0)
    build version: 42 (required: 42)
    extension: router_cpp.cpython-312-darwin.so
  Placement backend: available (version 2.1.0)
  DRC backend: available (version 1.0.0)
  ```

### PR #5497 state — the board-06 arms are the other way round from the issue body

The issue body asks for board-06 "with PR #5497's `clear_avoidance_after_connection=True`".
That PR has since merged, and a later commit flipped the default:

| Commit | Meaning |
|---|---|
| `74a9dc8ad7de2790781f738b4d392437974089b1` | PR #5497 head at curation (kwarg added, default `False`) |
| `9a4c58bebc9700c77b3e9d5c0c9cb07b29e2beb0` | PR #5497 as merged, 2026-09-16T19:05Z |
| `af4909495c8e917e8ef9e3a8160dca97025d5c4a` | #5609 — flipped the default to `True` |

On the run tree, `CppPathfinder.route(..., clear_avoidance_after_connection: bool = True)`
(`src/kicad_tools/router/cpp_backend.py:2024`). So:

- **the option-ON arm the issue asks for is plain `origin/main`** — no scratch
  merge, no cherry-pick, no source delta; and
- **the option-OFF control forces the kwarg `False`** from the harness
  (`KCT_5508_FORCE_AVOIDANCE_LEAK=1`), reproducing the pre-#5504 avoidance leak.

This is the opposite of the scratch-merge recipe the curator wrote against the
then-unmerged PR, and it is strictly simpler: the arm of interest needs no
source modification at all.

## Board-05 `ISENSE_A-` (#5398)

**Entry point** (curator option (a), *not* `design.py`'s default `main()`, which
builds revision B from reviewed copper and never searches):

```
uv run python boards/05-bldc-motor-controller/legacy_drv8301/design.py <out-of-tree dir>
```

Its `route_pcb()` flag list is the seed-7 / 900 s / `jlcpcb-tier1` /
`--escape-corridor-reservation` recipe #5398 isolated. **4 runs**
(`05-run1` … `05-run4`), 15 m 10 s – 15 m 17 s each, all `RC=1`.

### Terminal-identity correction (recorded verbatim, as the AC requires)

The issue body's expected closing set names pads **U3.43 / U3.45**. On this tree
those pins are **not on `ISENSE_A-`**:

| Pin | Net (read from the generated `bldc_controller.kicad_pcb`) |
|---|---|
| U3.33 | `ISENSE_A-` |
| U3.44 | `ISENSE_A-` |
| U3.43 | `BST_B` |
| U3.45 | `GATE_AL` |

`ISENSE_A-` is carried by **R12.2, U10.5, U3.33, U3.44**. The witness verdicts
below are recorded against the pads that actually carry the net.

**Where the body's expectation came from is now visible, and it is instructive.**
`kct net-status --why` on the same artifact classifies the net as
`PLACEMENT_BOUND` and lists exactly:

```
[PLACEMENT_BOUND] ISENSE_A-
  unconnected pads: R12.2, U10.5, U3.44
  blocking nets:    BST_B, GATE_AL, PWM_BH
  evidence:         pad reachable (360 deg lane) but in a dense cluster
                    (0 same-group + 161 foreign obstructions, nearest strict
                    blocker 0.500mm); ripping the few strict blockers cannot
                    clear the surrounding copper
```

`BST_B` is U3.43 and `GATE_AL` is U3.45. The epic's expected
`closing_nets ⊇ {U3.43, U3.45, …}` is the *finished-board inference's*
`blocking nets` list, re-read as a causal claim — precisely the conflation the
1b format note says the witness exists to prevent ("a line-of-sight scan cannot
distinguish copper that closed a pad from copper that merely ended up near it").
`net-status` itself says the pad is **reachable**, which agrees with the witness.

### Witness verdict

The main `kct route` pass exhausts its 900 s `--timeout` during layer
escalation on this host — the same exit shape as #5398's pinned baseline
(`route_exit: 124` at `6d5edcb`) — so the 1b sidecar is never written (see
"Witness-surface gap" below). The harness therefore dumps the worker router's
**commit journal** plus a router snapshot at each `route_all*` return and at the
deadline unwind, and `board05_offline_replay.py` replays the witness **offline**
against a router rebuilt from the unrouted input plus the captured rules and
escape-endpoint overrides (the in-process pattern #5398's `capture_prephase_v2.py`
used, adapted).

Per-pad verdicts, identical across **both instrumented runs**, **both
layer-escalation attempts**, and **both replay generations** (six replays in
total: three at the loader's default layer stack and three with the attempt's
stack passed explicitly) — the run-to-run agreement the AC names as the
evidence:

| Pad | `access_at_escape_end` | `final_access` | `first_closed_at` | `closing_nets` |
|---|---|---|---|---|
| R12.2 | `non-empty` | `non-empty` | `null` | `[]` |
| U10.5 | `non-empty` | `non-empty` | `null` | `[]` |
| U3.33 | `non-empty` | `non-empty` | `null` | `[]` |
| U3.44 | `non-empty` | `non-empty` | `null` | `[]` |

`ISENSE_C-` (R10.2, U10.7, U3.34) and `SWCLK` (U10.24, J4.3) — the other
`blocked_path` failures — show the same shape.

**AC verdict for this row: the expected PASS condition does NOT hold.**
`access_at_escape_end == non-empty` *does* match #5398's pinned prephase capture
on `7f168061` (so the AC's "closure is post-prephase" precondition is satisfied
vacuously), but no commit in any run's journal ever emptied an `ISENSE_A-`
access set, so `closing_nets ⊇ {U3 pads, vias, B-}` cannot hold. Recorded
verbatim, with the diff against #5398's isolation being: same prephase state,
no closure event.

### Router-side facts the witness sits on (all 4 runs agree)

*Provenance of this subsection, stated so a reader does not mistake it for AC
evidence: every line below is quoted from the four `board05-run*.log` recipe
logs, which are retained in the run worktree's `5508-1c-scratch/` and are **not
committed** here (see "Files"). Unlike the witness verdicts above, these
quotations are not checkable against this package's JSON.*

- Failed-net set at attempt-1 completion is **identical in all four runs**:
  `ISENSE_A-`, `ISENSE_A+`, `ISENSE_C-`, `SWCLK`, all `blocked_path`
  (`Failure causes: {'blocked_path': 8}` across the two attempts).
  `Total nets routed: 41`.
- `ISENSE_A-` fails in the C++ backend with **`no path (C++ A* open set
  exhausted)`** on its *first* routing attempt (15.6 s into the pass), then
  falls back to the Python A*. An open-set exhaustion with a non-empty access
  set is a search-reach failure, not an access closure.
- `BLOCKED_BY_COMPONENT` rip-up for `ISENSE_A-` displaces 24 siblings on
  R12/U10/U3 (25 in `run4`'s attempt 2, which also sweeps in `ISENSE_C-`) and
  **"reroute did not converge"**; then **"Relief rescue: `ISENSE_A-` has NO
  relief path (round 1) — rolling back"**. `ISENSE_A+` *is* rescued via a
  conflict-free relief path in the same round ("`BLOCKED_BY_COMPONENT` rip-up
  resolved 1/4 net(s)") — yet it still appears in the "Failed nets" block that
  the pass prints afterwards. Both lines are quoted verbatim; this package does
  not reconcile them, and the witness verdicts do not depend on which is right.
- Attempt 2 (4L ALL-SIG) is killed by the 900 s `--timeout` mid-search
  (`interrupted_stage: layer-escalation`, `reason: timeout`).

This is a coherent picture: legal first moves exist, and the search cannot
assemble them into a pad-to-pad path within budget. That is #5509 / #5511
territory, not "a commit took the last exit".

## Board-06 `U2.B1` GND (#5504)

**Recipe** (unchanged), on the copied `regression-fixture/` inputs, shadow phase
left OFF (`KCT_BOARD06_SHADOW` unset — the constructor default, and what the CI
runs that stranded `U2.B1` used):

```
uv run python boards/06-diffpair-test/generate_design.py <out> --step route --seed 42
```

Board-06 routes **in-process** (`Autorouter.route_all_with_diffpairs`), so no
`kct route` sidecar can exist for it by construction. The harness replays the
witness in-process at route return and at `atexit`, and interrogates `U2.B1`
**explicitly** via `replay(pad_keys=[("U2","B1")])` — GND is a pour net the trace
router never attempts, so `U2.B1` is never in the default `stranded_terminals`
set and the default witness would never mention it. (`witness_for_router()`
returns `None` at every board-06 capture point: the default tracked set was
empty.)

### Verdict, option-ON arm (plain `origin/main`, kwarg default `True`)

Two repeats, `RC=0`, 11 m 17 s and 10 m 57 s. `U2.B1` at every capture point
(`negotiated-return`, `diffpair-return`, `atexit`), both repeats:

| Pad | net | `access_at_escape_end` | `final_access` | `first_closed_at` | `closing_nets` | `reopened` |
|---|---|---|---|---|---|---|
| U2.B1 | `GND` (net 0) | `non-empty` | `non-empty` | `null` | `[]` | `false` |

Journal at `atexit`, identical in both repeats: **177 records — 100 commits,
77 rips, across 6 stages**, `truncated: false`.

**The named set the AC asks to cross-post for #5504's owner is the empty set.**
No commit in either repeat closed `U2.B1`'s access. If #5504's 0.333 mm² island
is real on this tree, its cause is not a committed route taking `U2.B1`'s last
legal exit.

### Verdict, option-OFF control (`clear_avoidance_after_connection=False`)

**None — the control did not produce one.** Both repeats were terminated
incomplete; see "Incomplete work" at the top of this file for the stall, the
three attempts it took, and what that costs the reading. `witness-board06.json`
records the `off-1` / `off-2` entries with `present: true`, no routed artifact
and no harness capture, and with a `.times` END line that records
`END 2026-09-21T10:50:25Z RC=143` — a builder termination, not a completed run —
so the absence is legible in the data and not only in this prose.

The control is not an acceptance criterion for #5518 (the AC names the option-ON
witness for `U2.B1`), but it *is* what would have turned the ON-arm result into a
statement about the avoidance leak. Re-running it needs either a quiet host or a
bound on the pure-Python fallback.

### Repeat identity (the #4593 bimodality rule)

The curator's rule is: compare arms paired mode-for-mode, never from a single
run per side, unless the two same-arm repeats are uuid-normalized identical — in
which case pairing is trivially satisfied. Measured:

| Pair | Comparable? | uuid-normalized identical? |
|---|---|---|
| `on-1` vs `on-2` | yes | **yes** — `diffpair_test_routed.kicad_pcb` is byte-identical modulo `(uuid …)` tokens |
| `off-1` vs `off-2` | **no** | neither run produced a routed artifact (see "Incomplete work") |

So for the ON arm the pairing requirement is trivially satisfied and no mode
classification is needed; for the OFF arm the question does not arise. **No
cross-arm comparison is made anywhere in this package**, because only one arm
has data — which is exactly the single-run-per-side comparison #4593 forbids.

**Caveat worth recording**: on byte-identical copper (modulo `(uuid …)` tokens),
`kicad-cli pcb drc --refill-zones` reported **563** violations for `06-on-1` and
**561** for `06-on-2` (clearance findings 17 vs 15, every other rule bucket
equal). Since the two boards' copper is identical, that ±2 comes from the
checker's own zone refill, not from the router. Not investigated further — out
of scope here, and flagged only so a future reader does not mistake it for a
routing difference. Note also that `kicad-cli`'s violation counts are **not** the
#4593 mode signals (those are `kct check` rule ids); mode classification from
`kicad-cli` totals would be an error.

## Board-07 `U4.C2` +1V2 (#5507) and `DQ3` negative control (#5410)

**Recipe** (unchanged), synthetic `regression-fixture/` inputs only — `real_design/`
(the SDR-SDRAM revision, no DQS) was not touched:

```
uv run python boards/07-matchgroup-test/generate_design.py <out> --step route --seed 42
```

Two runs: `07-run1` 40 m 28 s `RC=1`, `07-run2` 40 m 35 s `RC=1`. Both exhaust
the recipe's `--timeout 2400` (`run2`: *"routing deadline exceeded during
placement-delta-feedback"*), so neither produced a canonical
`matchgroup_test_routed.kicad_pcb` and **neither wrote a sidecar** — the same
witness-surface gap as board-05, contradicting the curator's expectation that
board-07's CLI subprocess would surface the sidecar unaided. `run2` carries the
harness (`KCT_5508_TARGET_PADS=U4.C2,U1.28,U2.4`), which is where these verdicts
come from.

### Default witness — two captures, and they do *not* agree

`run2` produced two in-process captures, both at a `negotiated-return` point.
Clearance the witness used at both: `trace_width 0.2`, `trace_clearance 0.15`,
`via_clearance 0.2`, `min_hole_to_hole 0.5`.

| Capture | journal | witness | tracked pads | verdicts |
|---|---|---|---|---|
| `.01` (08:54:06Z) | 23 records, 23 commits, 0 rips, 3 stages | `record_count 23`, `evaluations 46`, `truncated false` | 16 | all `non-empty` / `null` / `non-empty` / `[]` |
| `.02` (09:13:19Z) | 107 records, 84 commits, 23 rips, 5 stages | `record_count 107`, `evaluations 166`, `truncated false` | 26 | 18 as above; **8 report `empty`** |

The 16 pads in `.01` are J1.1, J1.3, J2.2, J2.4, J2.5, J2.7, J2.8, J3.8, U3.1,
U3.3, U4.B2, U4.B3, U4.B4, U4.B5, U4.B6, U5.7 — the tracked set is derived from
the failing nets at that moment, so it changes between captures.

**The eight `empty` entries in `.02`** are `U5.1`…`U5.8`, carrying nets
`A0`…`A7` (the J3↔U5 address rail). Verbatim for `U5.1`:

```json
{ "ref": "U5", "pin": "1", "net": 27, "net_name": "A0",
  "access_at_escape_end": "empty", "final_access": "empty",
  "first_closed_at": {"pass": "routing", "iteration": 0},
  "first_closed_index": 46, "first_closed_kind": "commit",
  "closing_nets": ["net0"], "closing_refs": ["<board-edge>"],
  "closing_copper_class": ["board_edge"], "closing_markings": [],
  "reopened": false }
```

`U5.8` is the same with `first_closed_index: 52`. **The closing copper is the
board outline** (`board_edge`), so even where the predicate fires, it does not
accuse a committed route — which is the epic's actual invariant. Note that
`U5.7` reports `non-empty` in `.01` and `empty` in `.02`: the two captures are
different journal prefixes, so this is a progression, not a contradiction.

**Two oddities in these entries, recorded and filed rather than fixed here:**

1. `access_at_escape_end` is `empty` *and* `first_closed_at` is non-null. The
   format note's reading table has no such row: row 3 (`empty` / `null` /
   `empty`) is "placement stranded it". Either the escape-end reference point
   sits after journal record 46 in this five-stage journal, or the baseline and
   the first-closure scan disagree. **This package did not resolve which**, and
   the distinction matters for Phase 2 — it is the difference between "placement
   stranded these eight pads" and "a commit did".
2. `closing_nets: ["net0"]` for copper whose `closing_copper_class` is
   `board_edge`. The board outline is not a net; `net0` is a synthetic label
   leaking into a field a reader will read as a net name.

### `U4.C2` (+1V2) — the #5507 row

| Pad | net | `access_at_escape_end` | `final_access` | `first_closed_at` | `closing_nets` |
|---|---|---|---|---|---|
| U4.C2 | `+1V2` | `non-empty` | `non-empty` | `null` | `[]` |

This is the issue body's **second** outcome for this row ("or **non-empty** — the
latter sends #5507 back to the recipe-local pour-escape model"), which is also
consistent with the curator's **third** outcome (the #5507 open originates in the
frozen `a37f5d6` integration and may not reproduce on a fresh regen).

**Recommendation for #5507, as the AC requires it be stated:** redirect to the
recipe-local pour-escape model — `boards/06-diffpair-test/pour_escape.py::find_escape`
as called from `_repair_pour_connectivity` — and **not** to Epic #5508. `+1V2` is
in the recipe's `--skip-nets GND,+1V2,+1V8`, so the trace router never attempts
it and no committed trace can close it; on this tree the witness confirms its
access set is never emptied. #5507 is a pour-connectivity/escape question.

### `DQ3` negative control — **PASSES**

Both `DQ3` terminals, interrogated explicitly:

| Pad | net | `access_at_escape_end` | `final_access` | `first_closed_at` | `closing_nets` | `reopened` |
|---|---|---|---|---|---|---|
| U1.28 | `DQ3` (net 7) | `non-empty` | `non-empty` | `null` | `[]` | `false` |
| U2.4 | `DQ3` (net 7) | `non-empty` | `non-empty` | `null` | `[]` | `false` |

`DQ3` is one of the five seed-invariant opens CI asserts
(`--expect-opens "DQ3,DQ4,MIPI_DAT0_N,TMDS_D0_N,TMDS_D1_N"`), and it remains
unrouted here — while the witness correctly declines to blame any commit for it.
**AC item 5 is satisfied** (`non-empty`, `closing_nets == []`), so no 1a/1b
defect is filed on that account.

## Clearance values the witness used (#5509 caveat)

Recorded, not corrected — per the issue's explicit instruction.

| Board | `trace_width` | `trace_clearance` | `via_clearance` | `min_hole_to_hole` | `grid_resolution` | manufacturer |
|---|---|---|---|---|---|---|
| 05 | 0.2 | **0.15** | 0.20 | 0.5 | 0.0635 | `jlcpcb-tier1` |
| 06 | see `witness-board06.json` per capture | | | | | |
| 07 | 0.2 | **0.15** | 0.20 | 0.5 | 0.127 | `jlcpcb` |

The #5509 0.15-vs-0.20 delta is therefore live on **both** board-05 and board-07,
and the independent checker sees it: `kicad-cli` reports board-05 clearance
violations of the form *"clearance 0.2000 mm; actual 0.1723 mm"* — copper the
router considered legal at 0.15 that KiCad rejects against the 0.20 project rule.

**Direction of the caveat's effect on this phase's conclusion.** A resolver that
under-states clearance makes the access predicate *more* permissive about
via sites, i.e. **more** likely to call an access set non-empty. A narrower
(correct, 0.20) clearance could in principle turn some of these `non-empty`
verdicts into `empty` ones. So the negative result here is **not** a fortiori
safe in the "no closure happened" direction, and the earlier draft of this
package asserted that it was — corrected here. What the delta cannot do is
manufacture a *closing commit*: `first_closed_at` is `null` because no journal
record ever transitioned a tracked set from non-empty to empty, and re-running
the predicate at 0.20 would have to be done to know whether a closure appears.
**That re-run is Phase 2's to make, and is the single most valuable follow-up
from this phase.**

## Witness-surface gap found (filed, not fixed)

**`kct route` writes no `<stem>.access_witness.json` on a deadline-terminated
run** — exactly the runs where a stranding witness matters most. Verified two
ways:

1. **Location**: `_write_access_witness_sidecar` (`src/kicad_tools/cli/route_cmd.py:2717`)
   is called only from the normal save path (`route_cmd.py:3253`), reached after
   the route returns and the canonical output is written.
2. **The deadline path never gets there**: `route_deadline.py` renames the
   output to `*_timeout_unverified_*` and copies only `.kicad_pro` / `.kicad_dru`
   beside the quarantined board (`route_deadline.py:225-255`).

Observed on 6 of 6 deadline-killed runs in this package (board-05 ×4,
board-07 ×2): `find out -name '*.access_witness.json'` returns **nothing**, and
`grep -c 'Access-witness sidecar'` is **0** in every recipe log — while
`.route.json` / `.timeout.json` receipts *were* written, each carrying
`"route_exit_code": 124`, which is what identifies them as the deadline
machinery's receipts rather than the normal save path's. This is the reason
boards 05 and 07 needed a harness at all.

All three observations below are filed as **#5639** rather than fixed here, per
this issue's scope guard ("Do not treat a negative result as a reason to 'fix'
the witness in this PR; file it."). Two further ones from the same surface:

- the `empty` + non-null `first_closed_at` combination described in the
  board-07 section, which the format note's reading table does not cover; and
- `closing_nets: ["net0"]` on copper classified `board_edge`.

Board-06 is a separate case, not a defect: it routes in-process, so there is no
`kct route` invocation for a sidecar to hang off at all. Any witness for a
recipe that routes in-process has to come from the library surface.

## What this evidence does *not* establish

Stated explicitly so Phase 2 is not over-scoped on it:

- **It does not prove no access-loss bug exists.** It shows that *these four
  pads*, on *this tree*, at *this resolver's clearances*, were not closed by a
  commit. #5509's delta is unresolved in the direction that matters (above).
- **The `DQ3` control shows discrimination, not correctness.** It shows the
  witness does not report a *false closure* for an unrouted-but-reachable pad,
  and the board-07 `.02` capture shows the predicate is capable of returning
  `empty` with a named record in the same evaluation. What neither shows is
  that a *route*-caused closure would be named correctly: no closure in this
  package has `closing_copper_class` `route_segment` or `route_via`. That
  direction is still only covered by 1a's fixture (#5516). A reader should
  resist reading four `non-empty` verdicts as four independent confirmations;
  they are one predicate, evaluated repeatedly.
- **The eight board-07 `empty` verdicts are not adjudicated.** Because of
  oddity (1) above, this package cannot say whether they are a placement
  stranding or a commit stranding. Do not cite them as either.
- **Board-05's offline replay is a reconstruction.** The grid raster is rebuilt
  from the unrouted input; search-time raster markings (pad-channel budget tags,
  corridor reservations) are not replayed. Per the 1b format note those are
  descriptive labels that never participate in a legality decision, but the
  reconstruction is not the live router's own memory.
- **Board-05's attempt-2 stackup needed a second replay, and it agrees.** Attempt
  2 escalates to 4L ALL-SIG; the first-pass replay rebuilt the raster against
  the loader's default (the authored 4L SIG-GND-PWR-SIG stack). Every dump was
  therefore replayed again with the attempt's stack passed explicitly
  (`--layer-stack`), and both reconstructions return the same verdicts. Both
  generations are retained in `witness-board05.json` so the agreement is
  checkable rather than asserted.
- **One host, `kicad-cli` 10.0.6, macOS.** Local blocking counts are
  non-authoritative per #3822.

## DRC cross-gate

Every routed `.kicad_pcb` these runs produced was cross-gated with
`kicad-cli pcb drc --refill-zones`, **record-only** — violations here are
evidence about the router's output, and nothing was fixed. Per-artifact counts,
rule histograms and the narrowest observed clearance are in `drc-summary.json`;
the raw reports stay in the scratch tree (not committed — see below).

Headline counts:

| Artifact class | violations | unconnected items |
|---|---|---|
| board-05 `*_completed_unverified_*` (4 runs) | 118 – 141 | 86 |
| board-05 `*_timeout_unverified_*` (4 runs) | 108 – 173 | 88 – 89 |
| board-06 `diffpair_test_routed` ON (2 repeats) | 563 / 561 | 0 |
| board-06 `diffpair_test_routed` OFF (2 repeats) | *not gated — no artifact produced* | — |
| board-07 `matchgroup_test_routed_partial` (`run2`) | 223 | 167 |
| board-07 `*_timeout_unverified_*` (`run1` and `run2`) | 24 each | 190 each |

## Files

| File | Content |
|---|---|
| `manifest.json` | provenance (git SHA, PR #5497 SHAs, `build-native` verdict, per-board wall-times) + SHA256 of every other file here |
| `witness-board05.json` | per-run journal-dump inventory, the offline replay witnesses, and the `net-status --why` classification of the same artifacts |
| `witness-board06.json` | both arms × both repeats: harness captures, targeted `U2.B1` witness, repeat-identity check |
| `witness-board07.json` | `run1`/`run2` harness captures incl. the default 16-terminal witness, `U4.C2`, and the `DQ3` negative control |
| `drc-summary.json` | the `kicad-cli pcb drc --refill-zones` cross-gate, per artifact |
| `captured-scripts.tar.gz` | the exact harness, drivers, replay and composition scripts run, with a `README` inside |

**Not committed**, deliberately: the routed boards, DRC reports, recipe logs and
raw journal dumps (the board-05 dumps alone are ~3.6 MB of JSON). They live in
the run worktree's `5508-1c-scratch/` and are reproducible from the captured
scripts. No `boards/*/output/*` artifact was regenerated or committed.

## Reproducing

```bash
uv run kct build-native --check          # must report available first
tar xzf captured-scripts.tar.gz && cd captured-scripts

./run_board05.sh 1                        # board-05, legacy DRV8301 entry point
./run_board06_arm.sh on 1                 # board-06, plain main == option ON
./run_board06_arm.sh off 1                # board-06, forced avoidance leak
./run_board07_witness.sh 1                # board-07, harness targets U4.C2 + DQ3

# board-05 witness is replayed offline from a journal dump:
uv run python board05_offline_replay.py \
    --input  out/05-run3/bldc_controller.kicad_pcb \
    --dump   witness-board05-run3.escape-return.01.json \
    --pad-keys "R12.2,U10.5,U3.33,U3.44" --label run3-attempt1

./run_drc_batch.sh 05-run3 06-on-1 07-run2   # kicad-cli cross-gate
```
