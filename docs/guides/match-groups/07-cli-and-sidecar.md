# 07 — CLI and JSON Sidecar Workflow

End-to-end: route a board with match-group tuning enabled, then
validate it standalone with the `--net-class-map` sidecar.

Reference board: [`boards/07-matchgroup-test`](../../../boards/07-matchgroup-test/).

## Route with match-group tuning

```bash
kct route board.kicad_pcb \
    --differential-pairs \
    --length-match-diffpairs \
    --length-match-groups \
    --seed 42 \
    -o routed.kicad_pcb
```

The `--length-match-groups` flag (Epic #2661 Phase 3H, registered in
`_main_impl` in `src/kicad_tools/cli/route_cmd.py`) engages
`Autorouter.apply_match_group_tuning` after routing completes.

### Why both `--length-match-diffpairs` AND `--length-match-groups`

`_main_impl` runs `--length-match-diffpairs` **first** (within-pair
tuning) then `--length-match-groups` (cross-lane) so within-pair
invariants (guide 03) are set before cross-lane serpentine insertion
preserves them. Single-ended-only buses (DDR data byte, address bus)
may pass `--length-match-groups` alone.

### `--seed` for reproducibility

`--seed` makes two runs of `kct route` byte-identical (modulo UUIDs) by
fixing the tie-break order for equally-good serpentine candidates. Use
it for CI regression baselines.

## Standalone validation with `--net-class-map`

When you check a routed PCB outside the `kct route` pipeline, the
`match_group_length_skew` rule (guide 06) needs the group-membership
map. Emit a JSON sidecar from your board generator (Issue #2684):

```python
from kicad_tools.router.rules import net_class_map_to_dict
import json

sidecar = net_class_map_to_dict(net_class_map)  # net_class_map: dict[str, NetClassRouting]
with open("net_class_map.json", "w") as f:
    json.dump(sidecar, f, indent=2)
```

`net_class_map_to_dict` lives in `src/kicad_tools/router/rules.py`;
`boards/03-usb-joystick/generate_design.py` is the canonical emitter.

```bash
kct check routed.kicad_pcb --mfr jlcpcb --net-class-map net_class_map.json
```

Without `--net-class-map`, the rule short-circuits to zero violations
(the Phase 2.5G no-op semantic — see guide 06).

### Declaring a swap group (Issue #5522, Phase 1 of Epic #5511, report-only)

A `NetClassRouting` entry may also carry `swap_group: str | None` — a
DECLARED (never inferred) name grouping nets whose pad binding to the
*secondary* facing component MAY be permuted to reduce facing-row
crossings. Narrower than `length_match_group`: a match group says
"these nets must arrive length-matched" (may include a fixed-binding
net, e.g. an unpaired DQS strobe); a swap group says "these bindings
may be permuted." Nets without the key are fixed by omission — there
is no top-level `swap_groups` block.

```json
{"DQ0": {"name": "DDR_DATA_BYTE_0", "length_match_group": "DDR_DATA_BYTE_0", "swap_group": "DDR_BYTE0"}}
```

When `kct net-status --why --format json` (guide 05) auto-discovers a
`swap_group` declaration and classifies a bundle as genuinely REVERSED,
each affected net's diagnosis gains a `swap_proposal`: the
crossing-minimising pad-to-net re-binding, plus before/after crossing
counts for both the declared group and the wider match group (residual
crossings against an undeclared sibling are reported, never hidden).
**Phase 1 is report-only** — no applicator exists.

## Putting it all together

```bash
# 1. Generate the board (your script emits net_class_map.json)
python boards/07-matchgroup-test/generate_design.py
# 2. Route with both length-match passes
kct route boards/07-matchgroup-test/output/board.kicad_pcb --differential-pairs --length-match-diffpairs --length-match-groups --mfr jlcpcb --seed 42 -o boards/07-matchgroup-test/output/routed.kicad_pcb
# 3. Validate standalone (e.g. in CI on a separate runner)
kct check boards/07-matchgroup-test/output/routed.kicad_pcb --mfr jlcpcb --net-class-map boards/07-matchgroup-test/output/net_class_map.json
```

## See also

- [01-declaring-groups.md](01-declaring-groups.md) — declare groups so they show up in `net_class_map.json`.
- [06-drc-rule.md](06-drc-rule.md) — what `match_group_length_skew` checks and why the sidecar matters.
