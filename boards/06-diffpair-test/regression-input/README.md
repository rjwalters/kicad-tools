# Synthetic routing input successor

Part of #5059. Original `regression-fixture/diffpair_test.kicad_pcb` is preserved byte-for-byte
(SHA256 `d69b22100589c86892219723a26e4ba7a08954dc583043ff80e334e119f60143`). This successor changes
only the long dimension of eight corner pads per synthetic 48-pad footprint
from 0.70 to 0.68 mm: U3-1, U3-12, U3-13, U3-24, U3-25, U3-36, U3-37, U3-48.

The 0.8 mm pitch, pad centers, net assignments and other geometry are retained.
These are synthetic widened test footprints, regardless of their historical
library-style identifiers. The diagonal corner gap increases from 0.141421 to
0.155563 mm, above the unchanged 0.15 mm different-net SMD floor.

`recipe_output_dir(..., prepare=True)` stages this unrouted successor over the
archived input in `regression-output/`; the existing recipe must reroute and
pass the unchanged DRC, rule-coverage, reach and pour-connectivity gates.
The archived routed witness and production release outputs are not replaced.

The selection is explicit in `manifest.json`; staging verifies both archived
and successor SHA256 values before accepting the replacement.

## Explicit successor rule context

Schema2 also selects the routed-stem `.kicad_dru` in this directory. It is
byte-exact `merge_dru_floors(None, generate_dru(get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1), "jlcpcb"))` output. Both archived and successor sidecar
hashes are checked before staging; archived fixtures remain immutable. This
is an explicit new qualification context, not a claim about historical runs.

All valid archived scalar minima are retained: trace/general clearance0.1016mm,
via drill0.2mm, via diameter0.45mm, annulus0.1mm, copper-edge0.3mm,
silk width0.15mm and height1.0mm. Hole-to-edge0.4mm keeps its intended floor
with the current explicit Edge.Cuts scope and physical_hole_clearance constraint;
the old broad hole_clearance stanza was not an equivalent edge rule.
The invalid solder_mask_margin0.05mm and mask-dam pseudo-rule0.1mm are not emitted
by the current generator; those independent mask profile/checker minima remain.
Current explicit silk/SMD/PTH/inner-copper rules are added, not waived.

Staging permits only declared unrouted PCB successors and their stem-derived
DRU sidecars, never a routed PCB or arbitrary source-file replacement. Unknown
or modified archived rules fail their hash binding; this is not a generic
custom-rule migration or parser relaxation. The matching project stays byte-exact,
including any stronger project or net-class minima. Full fresh recipe/native
qualification remains required after this context change.
