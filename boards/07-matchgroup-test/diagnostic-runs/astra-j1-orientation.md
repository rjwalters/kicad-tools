# Astra J1 orientation experiment — 2026-09-09

Astra-assisted A/B testing found a placement candidate that the classifier-to-delta proposer cannot currently express: rotate the source connector 90° to align a horizontal pad row with a receiver's vertical column.

### Measured board-07 case

Main `b8d5f595`, J1 held at board-relative `(15,55)` and rotated from 0° to -90° on F.Cu. All six MIPI nets were stripped and rerouted together while preserving other copper. Both arms used seed 42, `PYTHONHASHSEED=42`, a 180s budget, four layers, negotiated routing, the existing net-class sidecar, and length-match tuning.

| Measurement | Committed board | Unchanged-angle reroute | J1 -90° reroute |
|---|---:|---:|---:|
| Connected MIPI nets | 5/6 | 4/6 | 6/6 |
| Native KiCad unconnected items | 5 | 6 | 4 |
| Other native error-level DRC | 0 | 0 | 0 |
| Native warnings | 31 | 31 | 31 |

Native checks used refilled zones and the same committed `.kicad_pro` / `.kicad_dru` for every arm. The candidate retains the four DDR/HDMI opens; all other previously connected nets stay connected. Correct absolute pad rotation is essential: see #4966.

**This first candidate improved connectivity but failed sign-off checks.** Its MIPI pair skews were 0.083 / 5.587 / 2.680 mm versus 0.050 mm; continuity was 4.2% / 51.2% / 0.0% versus 85%; group skew was 4.928 mm. `kct check` reported 15 total errors versus the previous committed board's 13 (the newly connected pair activated additional checks). This first candidate was not promoted.

## Reproduction

These commands describe the initial A/B against `b8d5f595` artifacts and its
old sidecar; use that revision's input files to repeat the historical test.
For the current corrected repair, use `repair_mipi.py` as described below.

Apply the `pcb move-footprint` pad-angle fix tracked in #4966 first. The
following leaves committed artifacts intact. Run the two routes sequentially
so CPU contention does not distort the comparison. A nonzero route exit is
expected for these incomplete boards; the commands deliberately do not enable
shell `set -e`.

```sh
experiment_dir=$(mktemp -d /tmp/kct-j1-orientation.XXXXXX)
mkdir -p "$experiment_dir/control" "$experiment_dir/j1-rotated"
mipi_nets=MIPI_CLK_P,MIPI_CLK_N,MIPI_DAT0_P,MIPI_DAT0_N,MIPI_DAT1_P,MIPI_DAT1_N
board_source=boards/07-matchgroup-test/output/matchgroup_test_routed

uv run kct pcb strip "$board_source.kicad_pcb" --nets "$mipi_nets" \
    -o "$experiment_dir/control/input.kicad_pcb"
uv run kct pcb move-footprint "$experiment_dir/control/input.kicad_pcb" \
    --ref J1 --to 15 55 --rotation -90 \
    -o "$experiment_dir/j1-rotated/input.kicad_pcb"

for arm in control j1-rotated; do
  PYTHONHASHSEED=42 uv run kct route "$experiment_dir/$arm/input.kicad_pcb" \
    -o "$experiment_dir/$arm/routed.kicad_pcb" --nets "$mipi_nets" \
    --preserve-existing --manufacturer jlcpcb --strategy negotiated \
    --no-auto-layers --layers 4 --seed 42 --timeout 180 \
    --deterministic-budget \
    --net-class-map boards/07-matchgroup-test/output/net_class_map.json \
    --length-match-groups > "$experiment_dir/$arm/route.log" 2>&1

  uv run kct net-status "$experiment_dir/$arm/routed.kicad_pcb" \
    --format json > "$experiment_dir/$arm/net-status.json"
  uv run kct check "$experiment_dir/$arm/routed.kicad_pcb" \
    --net-class-map boards/07-matchgroup-test/output/net_class_map.json \
    --format json --output "$experiment_dir/$arm/kct-check.json"

  # Retain the committed project's rules for a like-for-like native check.
  cp "$experiment_dir/$arm/routed.kicad_pcb" "$experiment_dir/$arm/verified.kicad_pcb"
  cp "$board_source.kicad_pro" "$experiment_dir/$arm/verified.kicad_pro"
  cp "$board_source.kicad_dru" "$experiment_dir/$arm/verified.kicad_dru"
  kicad-cli pcb drc --refill-zones --save-board --format json \
    --output "$experiment_dir/$arm/native-refilled.json" \
    "$experiment_dir/$arm/verified.kicad_pcb"
done
```

The measured run used the committed sheet-centered geometry for both arms,
not a fresh full-board generation. This is a targeted repair experiment;
it does not establish the result of regenerating and routing the whole board.
KiCad zone refill also verifies the GND mounting holes moved by J1's rotation.

## Tool findings

- #4966: `pcb move-footprint --rotation` changed footprint orientation without
  changing absolute pad copper angles. Fixed locally; the new regression failed
  before the fix, then all 40 focused tests passed. Ruff lint/format passed.
- #4967: negotiated routing printed `Progress: 6/6 nets routed total` while
  reporting two unrouted nets immediately afterward. The progress count uses
  dictionary cardinality, including empty and partial route entries.
- #4968: the orientation-candidate feature request records this A/B result and requires
  mechanical and electrical validation before accepting such moves.

The initial A/B candidate remained experimental. Raw local artifacts from that
run are in `/tmp/kct-astra-gallery/`.

## Corrected coupled repair, promoted 2026-09-09

The later repair keeps J1 at -90 degrees and replaces only the six MIPI routes.
The canonical sidecar preserves the authored 0.100 mm gap and sizes MIPI/HDMI
width to 0.225 mm for the authored 100 ohm ±10% target (102.3 ohm model).
The previous serialized 0.375/8.425 mm geometry was stale (#4969). DQS remains
0.150/0.100 mm with no added impedance target.

`repair_mipi.py` runs coupled routing with `--no-cache`, seed 42, four layers,
a 360-second overall budget and 120-second per-pair budget. All three pairs
route `coupled-ok`. It then applies the pair skew tuner, including the small
deficit amplitude fix in #4975, and the existing scalar match-group optimizer
to a common six-route target with explicit partner clearances. The symmetric
group splice assumes matching P/N host segments and is unsuitable for these
unequal escape segments (#4984). The final validation still uses the unchanged
authored three-pair group definition. No electrical constraints are bypassed.

The script restored the original `.kicad_pro` and `.kicad_dru` before native
zone refill and verified all 244 pad/net mappings against the input. A fresh
script reproduction from the original baseline produced the promoted board:

```sh
uv run python boards/07-matchgroup-test/repair_mipi.py /tmp/board07-repair \
  --input /path/to/historical/matchgroup_test_routed.kicad_pcb
```

The default input is the current committed routed board; the absolute J1
rotation also supports already-repaired input. Expected overall exit status
is 2. This is a targeted repair; fresh full-board generation remains unmeasured.

| Check | Previous committed board | Promoted targeted repair |
|---|---:|---:|
| Connected signal nets | 26/31 | 27/31 |
| Connected MIPI nets | 5/6 | 6/6 |
| Native unconnected items | 5 | 4 |
| Other native error-level DRC | 0 | 0 |
| Native warnings | 31 | 31 |
| Full `kct check` errors | 13 | 8 |
| MIPI pair and group checks | Failing | Passing |

The remaining opens are `DQ3`, `DQ4`, `TMDS_D0_N`, `TMDS_D1_N`; the remaining
pair quality errors are DDR/HDMI skew and continuity. The board remains
partial, with zero waivers. Reproduction evidence is in
`/tmp/kct-astra-gallery/reproduce-repair/`: `route.log`, `mipi-tuning.json`,
`native-drc.json`, `check.json`, and `net_class_map.json`. Native name-only net
references were converted back to numeric references for older consumers;
the final native recheck (`promoted-native.json`) confirms the same four opens
and no other errors. `promoted-check.json` includes the full schematic/ERC/LVS
checks at the committed path. All 785 non-MIPI segments and 200 non-MIPI vias
are geometrically identical to the baseline. The reproduced
`matchgroup_test_routed.kicad_pcb` and sidecar were copied into `output/`;
LVS was regenerated. Cache inputs omitting electrical settings are tracked in
#4972; `--no-cache` makes this repair independent of old entries.
