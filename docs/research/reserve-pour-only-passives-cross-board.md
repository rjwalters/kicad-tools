# `--reserve-pour-only-passives` across boards 03-07 (Issue #7794)

**Date:** 2026-10-22. **Tree:** main `94ecb394e`, C++ router build 64.
**Host:** 18-core macOS arm64, shared with other sweep agents. Runs were
sequential, with a 1-minute load of 4-9.

**Recommendation: flip the default in a follow-on PR, and keep an opt-out.**
On board 05 the flag closes both remaining pour links. On every other board
it changes no graded outcome: no signal net is lost, no DRC count moves on
either engine, and no board gate changes. This PR does **not** change the
default.

## What the flag does

`kct route --reserve-pour-only-passives` (PR #7574, Issue #7573) passes
`min_pour_footprint_pads=2` to `select_plane_via_pads`. The pre-route
plane-via reservation (#6401) then also holds a via site for 2-pad parts
whose pads all sit on served pour nets, for example a +5V/GND decoupling cap.
It only adds sites. The baseline sites do not change (#7573, 2026-10-20 run).

Only the `kct route` CLI reads the flag. Of the board recipes:

| Board | What CI runs | Can the flag reach it? |
|---|---|---|
| 03 | `generate_design.py` applies the reviewed routing plan (`use_saved_plan=True`) | no: no router call |
| 04 | `kct route` (auto-skipped pours, reservation on) | yes |
| 05 | `redesign/build.py` replays committed copper | no: no router call (#7573) |
| 06 | in-process `route_pcb()`, `reserve_plane_via_sites(grid, ["VBUS_USB"])` with no pad selection | no: there is no CLI to pass it to, and that call already serves every VBUS_USB SMD pad whatever the footprint size |
| 07 | `kct route --skip-nets GND,+1V2,+1V8 --reserve-skipped-plane-vias` | yes, but it adds no sites (see below) |

So the table below has two kinds of row. **Recipe** rows are the board's
own CI recipe with the flag added to its `kct route` call. **Harness** rows
are `scripts/research/expand_route_experiment.py route` at scale 1.0, under
the #6508 protocol. Board 06 has a third kind, **generic**, because the
harness `route` subcommand refuses a board that carries copper.

## Method

Toolchain: every run, the route included (its oracle stage calls
kicad-cli), went through CI's pinned image, kicad-cli 10.0.6
(`kicad/kicad@sha256:1869...`). The wrapper entered
`kicad_toolchain.evidence_toolchain()` with `KCT_KICAD_DOCKER=1`.
`KCT_KICAD_DOCKER=1` **alone does not** reroute the harness's kicad-cli.
`find_kicad_cli()` found the host's Homebrew 10.0.7 until the evidence
toolchain put its docker shim first on `PATH`. A first pass on the host's
10.0.7 gave byte-identical copper for every harness row (03/04/05, both
arms), so the version did not matter here, but the table is the pinned
pass.

- **Harness (03, 04, 05):** `expand_route_experiment.py route --boards <B>
  --scales 1.0 --modes uniform --grid-policy area --seed 42 --timeout 4500
  --extra "--no-auto-layers --deterministic-budget --per-net-iterations
  1000000 --no-placement-delta-feedback --deterministic-rescue
  --no-fallback-bidirectional [--reserve-pour-only-passives]"`, with
  `PYTHONHASHSEED=42`. The harness itself pins `PYTHONHASHSEED=0` for the
  route subprocess.
- **Recipe (04, 07):** `generate_design.py <scratch dir>` (07: `--step all
  --seed 42`, exactly as CI). A scratch driver wraps `subprocess.run` and
  appends `--reserve-pour-only-passives` to the recipe's `kct route` argv in
  the "on" arm. It prints the full argv either way, so the logs show the
  flag was passed. Nothing in `boards/` was written.
- **Generic (06):** the harness's own `run_route` + `grade` on the unscaled
  committed `diffpair_test.kicad_pcb`. That board carries its LVDS copper, so
  the harness subcommand refuses it. Same #6508 extras, plus the committed
  `net_class_map.json`.
- **Columns.** *Routed nets* is the router's signal tally, then
  `NetStatusAnalyzer` nets complete (pours included).
  *Unconnected pour links* is the oracle stage's `N -> M unconnected pour
  link(s)` line. *`kct check --mfr jlcpcb`* is that CLI with
  `--format json` on the routed file. *kicad-cli DRC* is
  `pcb drc --refill-zones --all-track-errors` on the pinned image, reported
  as errors / unconnected. *Gate* is the board's own CI gate, run the
  way CI runs it, plus out-of-process copper LVS against the board's
  schematic. *Copper* is a hash of the sorted segment/via/arc list with
  UUIDs stripped.

## Board 05: U2-29 pad-edge via (deliverable 1)

The #7573 run (2026-10-20) had one `via_at_pad_edge` warning with the flag
on: an oracle GND via 0.02 mm from U2-29's exposed-pad mask opening. #7604
(`pad_edge_gap` in `router/oracle_completion.py`) has landed since. The
re-measurement, flag on, #6508 protocol + `--no-fallback-bidirectional`,
pinned toolchain:

```
$ KCT_KICAD_DOCKER=1 python pinned.py kct check \
    b05-on/05-uniform-1/bldc_controller_routed.kicad_pcb --mfr jlcpcb
DRC:       FAILED   (68 rules checked, 1 error(s), 137 warning(s))
BY RULE:
  silkscreen_line_width: 136 warnings
  connector_edge_distance: 5 infos
  connectivity: 1 error          # VCP: C5.1 stranded (placement_bound, both arms)
  silk_overlap: 1 warning
```

**0 `via_at_pad_edge`** with the flag on, and 0 with it off. The warning
total is 137 in both arms. On 2026-10-20 the flag-on run had 138, one of
them that via. The U2-29 case is closed by #7604, and the flag does not
bring it back.

## Results

| Board | Kind | Arm | Reserved sites | Routed nets (router / complete) | Unconnected pour links | `kct check --mfr jlcpcb` err / warn | kicad-cli DRC (pinned) err / unconnected | Board gate | Copper |
|---|---|---|---|---|---|---|---|---|---|
| 05 | harness | off | 13 | 32/33 / 34/37 | 43 -> **2** (C10 +5V, C10 GND) | 1 (VCP) / 137 | 0 / 3 | blocking 0; copper-LVS 3 opens (+5V C10, GND C10, VCP) | `96b162d8` |
| 05 | harness | on | 25 | 32/33 / **36/37** | 43 -> **0** | 1 (VCP) / 137, 0 `via_at_pad_edge` | 0 / **1** | blocking 0; copper-LVS 1 open (VCP) | `7c9df836` |
| 03 | harness | off | 27 | 24/25 / 25/27 | 21 -> 1 (GND F.Cu zone island) | 14 / 47 | 2 / 2 | copper-LVS 2 opens (GND C1.2-C11.2, VBUS C12.1-J1.A9) | `d5729bc6` |
| 03 | harness | on | 37 | 24/25 / 25/27 | 21 -> 1 (same) | 14 / 47 | 2 / 2 | same 2 opens | `2d4f804f` |
| 04 | harness | off | 13 | 9/9 / 12/12 | 18 -> 0 | 0 / 0 | 0 / 0 | copper-LVS clean | `d935d8d6` |
| 04 | harness | on | 29 | 9/9 / 12/12 | 18 -> 0 | 0 / 0 | 0 / 0 | copper-LVS clean | `d935d8d6` (identical) |
| 04 | recipe | off | 13 | 9/9 | 17 -> 0 | 71 / 0 | 0 / 0 | `check_routed_drc.py` strict-0 PASS; `check_net_status` 0; copper-LVS clean | `9c5a58d9` |
| 04 | recipe | on | 29 | 9/9 | 17 -> 0 | 71 / 0 | 0 / 0 | same, all PASS | `9c5a58d9` (file byte-identical) |
| 06 | generic | off | 16 | 16/16 / 18/18 | 54 -> 0 | 4 / 2 | 4 / 0 | copper-LVS clean | `cc5088c3` |
| 06 | generic | on | 46 (50 pads) | 16/16 / 18/18 | 54 -> 0 | 4 / 2 | 4 / 0 | copper-LVS clean | `5c8a2a9b` |
| 06 | recipe | either | 5 (VBUS_USB, all pads) | flag unreachable: a full `--step all --seed 42` run with the injection driver made 0 `kct route` calls | | | | | |
| 07 | recipe | off | 11 | 31/31 | 158 -> 0 | 7 / 42 | 1 / 0 | `check_matchgroup_coverage` OK (31/31, 0 unallowed, 7 identity-allowed); `check_net_status` 0; copper-LVS clean | `2e620e72` |
| 07 | recipe | on | 11 | 31/31 | 158 -> 0 | 7 / 42 | 1 / 0 | same, all PASS | `2e620e72` (file byte-identical) |

Wall time did not move with the flag: 05 204 s / 229 s, 03 240 s / 288 s,
04 harness 82 s / 94 s, 06 608 s / 609 s (off / on). Load was 4-9 and
differed between runs.

### Notes per board

- **05.** This is the improvement that motivated the flag. It reproduces on
  the pinned toolchain: both C10 stitch links close, and kicad-cli goes from
  3 unconnected items to 1. VCP is the only open in either arm
  (`placement_bound`, U2.3-C5.1). It is not a pour net and has nothing to do
  with the reservation. `sweep_wall_clock` is still live (#4159/#6329), as
  in #7573, but the copper was stable across the host and pinned passes.
- **03.** Every graded number is identical; the copper differs. The flag
  adds 10 sites. It also adds one new reservation note,
  `signal escape blocked on one side: R3.2 (by C1.1)`. That is a pad-access
  cost with no outcome change: R3's net still routes. The 14 `--mfr jlcpcb`
  errors include 11 `via_in_pad`, because board 03 is a `jlcpcb-tier1`
  board. Graded at its own tier, the harness reports 3 errors in both arms
  (VBUS connectivity, USB_D skew, USB_D uncoupled length). The GND pour
  island is C11.2's starved thermal spoke (#7498). The harness applies that
  board's keep-out only under `pairs`, not under `route`, so it shows up in
  both arms. Board 03's CI path is a plan replay, which the flag cannot
  reach.
- **04.** The flag adds 16 sites, and the routed copper is byte-identical
  in both the harness and the recipe. The recipe's 71 `--mfr jlcpcb` errors
  are the via dimensions of board 04's reviewed paid 0.15/0.30 mm drilling
  process (#5009), and they are the same in both arms. The board's CI gate
  (`check_routed_drc.py`, strict-0 at `jlcpcb-tier1` with the reviewed-process
  validator) passes in both arms.
- **06.** The generic CLI run adds 30 served pads (+3V3/GND decoupling
  caps) and changes the copper, but every graded number is identical. The 4
  LVDS skew errors on both engines come from the preserved committed pair
  copper, which this run does not tune; they appear in both arms. The
  board's recipe cannot see the flag.
- **07.** The flag adds no sites. `--reserve-skipped-plane-vias` serves only
  the non-ground skipped pours (+1V2, +1V8). A +1V2/GND cap is therefore not
  "pour-only" against the served set, so `min_pour_footprint_pads` has
  nothing to admit. The routed file is byte-identical, and every CI gate
  passes in both arms.

## Regressions

No board is worse with the flag on in any column. The one recorded cost is
board 03's one-sided escape block on R3.2, which changed no outcome. The
reserved keep-out is larger: 05 has +12 sites, 03 +10, 04 +16 and 06 +30.
On these five boards and this seed it never cost a signal net.

## Recommendation

**Flip the default, in its own PR, with a `--no-reserve-pour-only-passives`
opt-out.** The evidence for it:

1. Board 05 gains two pour links, and that is the only outcome change
   across five boards.
2. No signal net is lost and no DRC count moves on either engine; all four
   CI-relevant gates (04 strict-0, 07 match-group coverage, net status,
   copper LVS) are unchanged.
3. The U2-29 pad-edge via is gone with the flag on (#7604).

Caveats the flip PR should carry:

- **One seed per board.** Copper changed on 03, 05 and 06, so a different
  seed could expose a case where an extra site costs a signal net. The flip
  PR's own CI run of the board 04 and 07 recipes is the regression guard.
  Those are the only CI recipes the flag reaches.
- **The flip does nothing for boards 03, 05 and 06's CI.** Their recipes
  never pass through the CLI flag. Board 06's in-process
  `reserve_plane_via_sites(..., ["VBUS_USB"])` already serves every pad.
- **Not a subset flip.** No board here wants the flag off, and the CLI has
  no per-board notion to key a subset on.

## Addendum (2026-10-22, Issue #4410): default flipped, board 05 re-baseline

The default is now **on**, and `--no-reserve-pour-only-passives` opts out.
Board 05 was re-measured before the flip on main `8435ea01c` with the
same harness protocol, seed 42 and pinned kicad-cli 10.0.6. Host load was
15-49:

| Arm | Nets complete | `kct check --mfr jlcpcb` err | kicad-cli err / unconnected |
|---|---|---|---|
| off | 34/37 | 1 (VCP) | 0 / 3 |
| on | 36/37 | 1 (VCP) | 0 / 1 |

These are the same numbers as the table above. With the default flipped and the flag omitted, the same run reserves 25 sites and reaches 36/37 (124 vias, 1136.4 mm), matching the flag-on arm. **VCP is not
placement-bound**, although `kct net-status --why` labels it so. One
`kct route <routed> --complete --seed 42` pass on the flag-on output routed
it in 81 s (7 segments, 0 vias, 6.8 mm). The board then graded 37/37:
`kct check --mfr jlcpcb` DRC 0 errors, ERC passed, LVS (label and copper)
0 mismatches; pinned kicad-cli 0 errors and 0 unconnected. The grid route
never chains that completion pass itself.

**Measurement trap.** The first flag-on run graded 34/37 with 44
unconnected items. That happened because the route's oracle stage hit
`kicad-cli exited with status 137`, an OOM kill in a docker VM shared with
other agents' containers. The route log marks such a run `unverified`; the
harness row does not. Check `route.log` for `status 137` before trusting a
board-05 row with pour links open.
