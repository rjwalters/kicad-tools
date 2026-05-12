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

The `--length-match-groups` flag (Epic #2661 Phase 3H, see
`src/kicad_tools/cli/route_cmd.py:3475`) engages
`Autorouter.apply_match_group_tuning` after routing completes.

### Why both `--length-match-diffpairs` AND `--length-match-groups`

The pipeline runs `--length-match-diffpairs` **first** (within-pair
tuning) then `--length-match-groups` (cross-lane tuning) — see
`route_cmd.py:5265-5270`. Order matters for MIPI/HDMI lane groups
(guide 03): within-pair invariants must be established before
cross-lane tuning can preserve them via symmetric serpentine
insertion.

If you only have single-ended buses (DDR data byte, parallel address
bus, no diff pairs), you may pass `--length-match-groups` alone.

### `--seed` for reproducibility

The serpentine insertion uses `random` for tie-breaking when several
candidate segments are equally good. `--seed` makes two runs of `kct
route` produce byte-identical output (modulo UUIDs). Use it for CI
regression baselines.

## Standalone validation with `--net-class-map`

When you check a routed PCB outside the `kct route` pipeline, the
`match_group_length_skew` rule (guide 06) needs the group-membership
map. Emit a JSON sidecar from your board generator (Issue #2684):

```python
from kicad_tools.router.rules import net_class_map_to_dict
import json

# After building net_class_map: dict[str, NetClassRouting]
sidecar = net_class_map_to_dict(net_class_map)
with open("net_class_map.json", "w") as f:
    json.dump(sidecar, f, indent=2)
```

`net_class_map_to_dict` is at `src/kicad_tools/router/rules.py:1021`.
Board 03 (`boards/03-usb-joystick/generate_design.py`) is the
canonical emitter; board 07 follows the same pattern.

Then validate:

```bash
kct check routed.kicad_pcb \
    --mfr jlcpcb \
    --net-class-map net_class_map.json
```

Without `--net-class-map`, the rule short-circuits to zero violations
(the Phase 2.5G no-op semantic — see guide 06).

## Putting it all together

```bash
# 1. Generate the board (your script emits net_class_map.json)
python boards/07-matchgroup-test/generate_design.py

# 2. Route with both length-match passes
kct route boards/07-matchgroup-test/output/board.kicad_pcb \
    --differential-pairs \
    --length-match-diffpairs \
    --length-match-groups \
    --mfr jlcpcb \
    --seed 42 \
    -o boards/07-matchgroup-test/output/routed.kicad_pcb

# 3. Validate standalone (e.g. in CI on a separate runner)
kct check boards/07-matchgroup-test/output/routed.kicad_pcb \
    --mfr jlcpcb \
    --net-class-map boards/07-matchgroup-test/output/net_class_map.json
```

## See also

- [01-declaring-groups.md](01-declaring-groups.md) — declare groups
  so they show up in `net_class_map.json`.
- [06-drc-rule.md](06-drc-rule.md) — what
  `match_group_length_skew` checks and why the sidecar matters.
