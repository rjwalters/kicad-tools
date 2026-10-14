# Routing investigation — not fabrication data

These snapshots are deliberately outside `output/`, so normal gallery/export
selection cannot prefer them over the current source. They have opens and/or
shorts. Never manufacture them.

| Attempt | Settings | Observed result |
|---|---|---|
| First | 0.10 mm grid, strict pad/layer checking, 90 s requested budget | 18/31 non-ground nets complete; 18 native violations, 68 unconnected items. |
| Fine | 0.05 mm grid, explicit 0.15 mm fine-pitch clearance, otherwise same | Initial 23/31 became 16 complete after route validation; 11,750 segments. Interrupted after more than four minutes; only raw partial snapshot saved. |

Ground was excluded pending deliberate reference-plane construction. The
current generation source includes subsequent C5/C10 placement corrections,
so these are historical attempts, not validation of the current layout.
`first-native-drc.json` was produced by native KiCad on `first.kicad_pcb`
with its recorded `.kicad_pro`/`.kicad_dru` sidecars. No native validation or
connectivity claim is made for the interrupted fine snapshot.

Reproduction (repository root; output directory must exist):

```sh
uv run python boards/09-usbc-pd-power/generate_design.py
mkdir -p /tmp/board09-route
uv run kct route boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb \
  -o /tmp/board09-route/attempt.kicad_pcb \
  --net-class-map boards/09-usbc-pd-power/output/net_class_map.json \
  --skip-nets GND --preserve-existing --strict-pad-clearance --strict-layers \
  --fine-pitch-clearance .15 --layers 4 --no-auto-layers --no-auto-pour \
  --no-cache --grid .05 --clearance .15 --via-diameter .6 --via-drill .3 \
  --timeout 90 --per-net-timeout 8 --no-placement-feedback
```

At present the requested timeout is not reliable as a whole-process deadline;
see [#5035](https://github.com/rjwalters/kicad-tools/issues/5035).
[Rule propagation #5032](https://github.com/rjwalters/kicad-tools/issues/5032)
means a fresh renamed destination also needs an explicit source-rule audit
before interpreting any native DRC pass. Reproducing the bug intentionally
leaves the destination untouched; a future release recipe must preserve rules.

Added reproductions to [escape geometry #4991](https://github.com/rjwalters/kicad-tools/issues/4991)
and [branch-current modeling #4980](https://github.com/rjwalters/kicad-tools/issues/4980).

## Known artifact: SENSE_P track on pad U3.9 (#6484)

Both snapshots carry the same `SENSE_P` F.Cu track,
(174.6, 103.5) -> (176.41, 104.045) in file coordinates, width 0.25 mm
(segment uuid `36c0cca9-5bc6-47e8-be23-8c7454622f97` in `first.kicad_pcb`,
the uuid `first-native-drc.json` reports). It reaches about 9 um into
pad `U3.9` (`SENSE_N`), and kicad-cli reports it under `shorting_items`. This
is a **known artifact and is intentionally left in place**: the files are
frozen historical evidence (committed 2026-09-10, before the exact-pad-outline
contact fix from #6452/#6453/#6470), and re-routing would destroy the
evidence they exist to preserve. The other pinned shorts in
`tests/test_copper_contact_parity.py::EXPECTED_SHORTS` (for example
`GND`/`SW`/`VIN` and `+5V_OUT`/`KELVIN_N`) are the same kind of artifact.

Root cause: the router, not the checker. These attempts were generated with
the then-current router and a 0.1 mm-eroded pad box for contact tests, so a
sub-0.01 mm intrusion was invisible to `kct`'s own checks. The checker now
uses exact pad outlines, which is why the overlap surfaced. The shipping
`output/usbc_pd_power.kicad_pcb` does not contain this short.
