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
