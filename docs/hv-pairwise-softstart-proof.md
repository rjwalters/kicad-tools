# HV pairwise avoidance: softstart rev-C proof run (2026-08-15)

Point-in-time working document — it describes the tree as of its date.

> **Re-scored (#4867's fix landed as #4868).** The root cause the first run
> isolated — the `abs()` collapse of signed potentials, described in
> [The root cause](#the-root-cause-signed-potentials-are-collapsed-to-magnitudes)
> — is fixed, and the routing arms have now been **re-run on top of it**.
> [The re-score](#the-re-score-same-recipe-corrected-table) is the current
> record and supersedes the pre-fix numbers; everything from
> [Verdict (pre-fix run)](#verdict-of-the-pre-fix-run-t4-fails--but-the-machinery-is-measurably-doing-its-job)
> downwards is kept verbatim as the record of the diagnosis.

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
4. **The 2 genuine gate leaks** — the only line item Phase 2's own machinery
   owns, and the smallest one on the list.
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
* a throwaway replay of `segment_pair_violation` over the finished board with and
  without `build_attach_zones(...)` — 60 lines, reproduced from the snippet in
  [Attributing the 26](#attributing-the-26-which-are-the-routers-fault); it
  imports only public router API. **Frame note for anyone re-running it:** read
  the segments *and* the attach zones from the same board file and do **not**
  apply the `pcb.board_origin` shift that `_pairwise_attach_zones` uses — that
  shift exists because the CLI audit's `Route` objects live in the router's
  internal frame, and applying it to file-frame copper silently moves every zone
  off the board (the exemption then waives nothing, and the replay over-reports
  8 leaks instead of 2);
* a `subthreshold_coverage_gap(...)` call over the same `vmap.json` for the
  `--hv-threshold` numbers — public API as of the increment that reported them.

No board artifacts are committed to this repo: routed boards are `DO_NOT_FAB`
work-in-progress and live in scratch, per the fixture repo's own policy.
