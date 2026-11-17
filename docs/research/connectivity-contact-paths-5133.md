# Existing physical contact paths in the Board06 extractor (#5133)

The frozen Board06 file was not missing the proposed U1.17/U1.32 stitching.
The extractor omitted two existing contacts:

- U1.17 reaches a through via on F.Cu and a B.Cu trace. That trace contacts
  stored +3V3 fill away from the via. The old pour step only examined pads and
  vias, so the final trace-to-fill edge never entered the partition.
- U1.32 overlaps an off-centre through-via annulus at its pad edge. Testing
  whether the via centre was inside an eroded pad discarded that contact.

The correction models individual hole-aware fill solids, complete trace-width
capsules, via annuli, and existing terminals on their actual copper layers.
A fill's zone identity or declared net never joins disconnected solids. All
physical contacts remain label-free, so contacting foreign copper exposes a
short instead of being skipped. MultiPolygon fill repairs are split into
individual solids as well as separate stored fill entries.

Via-to-pad contact uses the supported raw pad outline and positive-area overlap
with the actual outer-diameter-minus-drill annulus. It reuses the corrected
shared pad polygon from PR #5230, including the negative KiCad rotation sign.
Unsupported pad shapes retain the earlier centre-only policy; no new raw box
fallback is introduced for them. Pad-to-fill erosion and the existing
pad/trace and via/trace contact-depth policies remain unchanged. The pour graph
inherits those established terminal bonds and emits only components that
contain actual fill, rather than bypassing guards for unrelated conductors.

PR #5150's removal of the public zone-ownership waiver is a dependency. The
public copper-LVS test asserts the repaired paths to J1.A8 (+3V3) and J1.A1
(GND) directly, with 198 schematic-bound pads and no reported opens/shorts.
An independent full-partition comparison against the immutable pre-fix module
found exactly those two unions (28 components to 26), with every other pad
relationship unchanged. This supports the saved-copper checker regression,
not a native or full-board qualification assertion. Raw frozen-file tests
independently construct positive-area paths and verify SHA256
`e6ab8780fa72e727342544e1063b0f3e86054862848c6c8bc4cd15ae83e62178` unchanged.

Synthetic controls cover each missing path, real gaps, positive/negative and
cardinal rotations, wrong layers, restricted via spans, cutouts, separate fill
islands, and foreign-net shorts. The former synthetic via-pour negative fed
its via across the pour on F.Cu, which itself created a short. Its source pad
and feed trace now use B.Cu, preserving the intended narrow-antipad short and
wide-antipad clean controls while isolating the via contact being tested.

No native KiCad run or stored-fill freshness claim is made. The remaining
legacy approximations and duplicate physical pad identity work (#5217) are
separate concerns; this change does not assert complete board correctness.
