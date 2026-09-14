# Revision B implementation

The real 42-component sensored BLDC circuit, exact procurement, programming and
initial operating limits are documented in HARDWARE.md. The shared component
table and native schematic/PCB generator are in hardware.py. DRV8313PWPR and
TPS7A1650DGNR use datasheet-derived symbols; the driver uses the TI PWP0028C
land pattern. A local capacitor footprint preserves the native physical pads
and expresses its polarity mark as one text glyph.

routing.json contains the reviewed physical tracks, vias, ground/5V planes and
copper-pour-only keepouts. routing.py refuses to apply it if any actual component,
pad, placement or net changes. The reference route combines automatic routing
and explicit physical corrections; it is not an autorouter completion claim.
The synthetic via-centered ports used experimentally are absent from final output.

Fresh source reconstruction passed native KiCad refill/DRC with zero violations
and zero opens, native ERC with zero findings, label/copper LVS with 149 bound
pads and zero mismatches, and manufacturer checks with zero errors/warnings.
Native refill is an independent gate because copper LVS still suppresses some
power-island opens when the same net owns a zone (issue #4982).

Firmware compiles with warnings treated as errors. A host test covers all six
Hall states, valid phase pairs, single-bit Hall transitions and single-leg
commutation transitions. Physical motor operation and thermal performance remain
unmeasured; keep the documented conservative bring-up limits.

The rectangular gr_rect outline works around existing loader bug #4978 while
preserving the intended physical boundary. The old DRV8301 design is archived
under ../legacy_drv8301 and must not be mixed with revision B.
