# Synthetic routing input successor

Part of #5059. Original `regression-fixture/matchgroup_test.kicad_pcb` is preserved byte-for-byte
(SHA256 `a4b3e4cd05ba68737d1cee346a0bd166503b99c14cbdab3c29a8258d1aa2cee2`). This successor changes
only the long dimension of eight corner pads per synthetic 48-pad footprint
from 0.70 to 0.68 mm: U1-1, U1-12, U1-13, U1-24, U1-25, U1-36, U1-37, U1-48, U2-1, U2-12, U2-13, U2-24, U2-25, U2-36, U2-37, U2-48, U5-1, U5-12, U5-13, U5-24, U5-25, U5-36, U5-37, U5-48.

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
