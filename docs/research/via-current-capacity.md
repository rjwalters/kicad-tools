# Via current capacity model (Issue #8745)

Screening model behind `kicad_tools.physics.ampacity.via_barrel_current_capacity`
and the layer-change check in `path_ampacity`. It is a **screening rule, not a
thermal simulation**: its failure mode of concern is a false pass, so every
assumption below is chosen on the conservative side or stated.

## Formula

The barrel is a hollow copper cylinder. Its conducting cross-section is the
plating annulus, `A = pi * t * (drill + t)` (t = plating thickness, annulus of
inner diameter `drill` and outer diameter `drill + 2t`, area taken at the
mean diameter). That area is fed to the IPC-2221 current/area relation used
by `width_for_current`:

    I = k * dT^0.44 * A_mil2^0.725

**Source and caveat.** IPC-2221 (Fig. 6-4 / Eq. 6-1 family) is specified for
*planar traces*, with `k = 0.048` external and `0.024` internal. Applying it
to a barrel annulus is a common calculator convention; IPC-2221 does not
specify it for vias. IPC-2152 treats vias and thermal paths separately and
generally gives more capacity than IPC-2221 for conductors with good heat
spreading, but it has no closed-form via equation either. Treat results at
moderate currents (2-6 A) as approximate; the 15 A vs one 0.3 mm via case is
flagged by roughly an 8x margin and is insensitive to every choice here.

## Choices

| Choice | Value | Why |
|---|---|---|
| Plating thickness | 20 um | IPC-6012 Class 2 **average** barrel plating (most fabs' default). Class 3 is 25 um average; Class 2 thin-area minimum is 18 um. Barrel plating is unrelated to foil weight (1 oz = 35 um). |
| `k` | 0.048 (external) | A barrel is in contact with the outer layer pads and the board's thermal mass, and the internal constant halves capacity (0.3 mm drill: 1.60 A -> 0.80 A), which would flag vias that fabs routinely use. External is the less pessimistic of the two; the plating-thickness choice carries the conservatism. |
| Temperature rise | 10 C | Matches `width_for_current`'s default so a via and the trace it feeds are judged to the same rise. |

## Resulting capacities (10 C rise, k = 0.048)

| Drill | 20 um | 18 um (thin-area min) |
|---|---|---|
| 0.2 mm | 1.22 A | - |
| 0.3 mm | 1.60 A | 1.48 A |
| 0.6 mm | 2.58 A | - |

Hand check, 0.3 mm drill: `A = pi * 0.020 * 0.320 = 0.02011 mm^2 = 31.2 mil^2`;
`I = 0.048 * 10^0.44 * 31.2^0.725 = 1.60 A`.

## Series vs. parallel

Only vias *proved parallel* share a layer change and have their capacities
summed: the members of a `_ViaArray` established by `resolve_current_path`.
Every other layer-change via is its own change, in path order, because each
carries the full path current. Distance is not a proxy for topology.
