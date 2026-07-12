# Board 07 Phase 1a -- targeted rip-up knob matrix (#4050)

Evidence for Issue #4050 (child of epic #4049): evaluate the already-
exposed `--targeted-ripup` / `--max-ripups-per-net` knobs on the #3438
DDR-bundle-only repro. This is a **measurement-first** result -- no
router behavior was changed; the knobs were already fully wired.

## Reachability / documentation audit (Task 1 + 2)

- `--targeted-ripup` and `--max-ripups-per-net` are defined in **both**
  parser sites that `tests/test_cli_parser_drift.py` keeps in sync:
  `src/kicad_tools/cli/parser.py:2723,2736` (the canonical `kct route
  --help` renderer) and `src/kicad_tools/cli/route_cmd.py:7259,7276`.
- They forward into `route_all_negotiated(use_targeted_ripup=...,
  max_ripups_per_net=...)` at **all 7** `route_all_negotiated(` call
  sites in `route_cmd.py` (3651, 4542, 6627, 9450, 9523, 9618, 9661).
  The curation comment listed 6 dispatch blocks; the 7th (9661) also
  forwards both knobs correctly. **No dropped path was found; no code
  fix was required.**
- `--max-ripups-per-net` additionally reaches the non-negotiated flows
  (`route_all` / two-phase stall recovery) via
  `_apply_ripup_budget_override` (`route_cmd.py:2769`), invoked at 3560,
  4459, 6559, 8954. The negotiated path resolves the budget via
  `_targeted_ripup_budget` (`route_cmd.py:2788`), which honors an
  explicit `0` and defaults absent -> 3.
- Both flags render in `kct route --help` with current wording (Issue
  #3438 / #3470 references, DDR byte-lane / facing-QFN-column framing).

## The repro

The 11-net DDR byte-lane bundle (`DQ0-DQ7`, `DM0`, `DQS_P`, `DQS_N`;
`generate_pcb.py:121-133`) isolated from the committed unrouted
placement `boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb`
by skipping the other 23 board-07 nets, `--seed 42`. Global routing
confirms `11/11 nets have corridors` -- the skip list isolates exactly
the DDR bundle. Budgets: `--per-net-timeout 30 --timeout 300` (sized so
every run reaches convergence, not truncated).

## Result matrix

| Config                                  | Routed | Stranded | Best % | Wall  |
|-----------------------------------------|--------|----------|--------|-------|
| baseline (`--targeted-ripup` off)       | 10/11  | DQ3      | 91%    | 186s  |
| `--targeted-ripup --max-ripups-per-net 3` | 10/11 | DQ3      | 91%    | 182s  |
| `--targeted-ripup --max-ripups-per-net 5` | 10/11 | DQ3      | 91%    | 178s  |
| `--targeted-ripup --max-ripups-per-net 8` | 10/11 | DQ3      | 91%    | 191s  |
| `--targeted-ripup --max-ripups-per-net 12`| 10/11 | DQ3      | 91%    | 177s  |

(`--targeted-ripup` with no explicit budget resolves to 3, i.e. the
`--max-ripups-per-net 3` row.)

## Conclusion

**No improvement over baseline from any targeted rip-up configuration.**
Every cell converges to 10/11 with `DQ3` as the single stranded net
(`DQS_N` routes; its partner-column neighbor `DQ3` is displaced) --
exactly the #3438 stranding dynamic. Raising the per-net rip-up budget
from 3 to 12 does not recover `DQ3`; it only trades a few seconds of
wall time within run-to-run noise.

This is the expected negative result the epic anticipated: #3438's own
builder sessions never exceeded 9-10/11 with any knob combination, and
Phase 1a is evaluation-first. The measurement confirms that targeted
rip-up alone is insufficient for the facing-column bundle -- the
stranding is a joint-corridor-allocation problem (epic #4049 Phases 1b
`_apply_byte_lane_inner_priority` net ordering, and the decision-gated
Phase 3 river-router), not a rip-up-budget problem.

## Reproducing locally

Run each config **solo** (no concurrent routing jobs -- #3438 is
load-sensitive):

```bash
uv run kct build-native --check   # confirm C++ backend first

SKIP="+1V2,+1V8,A0,A1,A2,A3,A4,A5,A6,A7,GND,\
MIPI_CLK_N,MIPI_CLK_P,MIPI_DAT0_N,MIPI_DAT0_P,MIPI_DAT1_N,MIPI_DAT1_P,\
TMDS_D0_N,TMDS_D0_P,TMDS_D1_N,TMDS_D1_P,TMDS_D2_N,TMDS_D2_P"

uv run kct route boards/07-matchgroup-test/output/matchgroup_test.kicad_pcb \
  -o /tmp/ddr_out.kicad_pcb --skip-nets "$SKIP" \
  --seed 42 --per-net-timeout 30 --timeout 300 \
  --targeted-ripup --max-ripups-per-net 8
```
