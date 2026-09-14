# Four-channel LVDS assembled demonstrator

Four real SN65LVDS1 drivers send LVTTL header inputs over terminated LVDS
pairs to SN65LVDS2 receivers and output headers. The board uses an explicitly
reviewed four-layer JLC7628 construction, length-matched differential pairs,
and ordinary through vias outside SMT lands.

See [the assembled design guide](assembled-demo/README.md) for exact components,
source regeneration, routing checks, fabrication settings and bench bring-up.
The complete fabrication/assembly download is `output/manufacturing.zip`;
current readiness is recorded in `output/readiness.json`. Three Samtec headers
require manual through-hole assembly. No firmware is required.

The historical synthetic routing fixture and benchmark notes are archived in
[regression-fixture](regression-fixture/README.md); they are not assembled hardware.
