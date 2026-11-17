# HV keepout clearance preservation (#5399)

## Geometry contract

`zones hv-keepout --clearance D` preserves the requested minimum around the
copper represented by the existing collector. It does not change HV net
classification, voltage policy, own-pour exclusions, target layers or the
track/via permissions of the generated rule areas. Arc/custom-pad coverage is
not extended: unsupported pad shapes still use the collector's rectangle.

The former `union.buffer(D)` loses clearance in two independent ways:

- Shapely's 16-segment-per-quadrant circular primitives are inscribed. Their
  maximum source sagitta is `R * (1 - cos(pi/64))`, where R bounds the radii
  of the selected pads, via discs and trace caps.
- `keepout_node` rounds vertices to 0.01 mm. An edge can move inward by up to
  `sqrt(2) * 0.005 mm`. Native coordinates add a bounded 1 nm per axis.

The new offset includes those two allowances and divides by `cos(pi/32)`
to enclose the offset arcs themselves. The latter conservatively allows an
arc step twice the nominal step when the segment count at an arbitrary
polygon corner is rounded. This is an outward geometry bound; no validator
threshold or shared formatter changes. Edge-clearance validation explicitly
recognizes rule areas as non-copper while continuing to check real unassigned
and filled/unfilled zones.

## Reproduction

Use the retained diagnostic Step2 board from #4507 comment 5674161444, with its
matching project/rules and unchanged maps, in a disposable directory:

```sh
kct zones hv-keepout input.kicad_pcb --net-class-map creepage_class_map.json \
  --clearance 1.6 --refill --format json
kct creepage input.kicad_pcb --voltage-map vmap.json \
  --net-class-map creepage_class_map.json --standard iec60664 \
  --pollution-degree 2 --material-group IIIa --working-voltage 250 \
  --waive-same-footprint --format json
kct net-status input.kicad_pcb --format json
kicad-cli pcb drc input.kicad_pcb --format json --output native.json
kct check input.kicad_pcb --drc-only --allow-incomplete --format json
```

Input board SHA256:
`e033d305f051a868a56de1c13a3187ffe964dcbba09bb2081b45e71f0f3e1bd7`.
Voltage map:
`3b2452a5169be3b09416259b8c9de7b6b8ae1af7f11f525530c00a2e3fe85d55`.
Class map:
`6f0a1caf64339cbd1db4b469a389f939ca9a8fb04e82476d736cf23268748e2b`.

Observed on KiCad 10.0.6, 2026-09-15 UTC, using the source change in this PR
based on main 58ee27d6; independent census verifier 638fe93:

| Check | Existing Step3 | Corrected Step3 |
|---|---:|---:|
| Board-level census failures |25|18|
| Fill-governed failures |7|0|
| Other conductor failure rows |18|18, identical|
| F2.2 /PM12_L to GND fill, In1.Cu |1.593914788 mm|1.609224023 mm|
| Strict connectivity |90/99 complete,81 unconnected pads|unchanged|
| GND/+3.3V island counts |51/18|unchanged|
| Native DRC |33 violations,80 unconnected items|unchanged|

Output board SHA256:
`f5a2b9d58fc1d752050d44885c0d2b50cb641cc01546f2abb4d2dc6e99240c32`.

Native findings remain 16 hole-to-hole, 8 annular-width, 5 crossings, 2 forbidden
items, 1 silk overlap and1 copper sliver. Python originally introduced six
false edge-clearance findings on the enlarged Net0 keepout polygons; the
producer/validator regression now excludes those non-copper rule areas.
The actual 12 Python errors (5 crossings, 7 incomplete signal nets) remain.
The existing incomplete plane connectivity is reported, not repaired or waived.
No declared current-path sidecar was supplied, so this is a connectivity
non-regression check, not ampacity or manufacturing qualification.

## Regression coverage and integration limits

The tests independently measure analytic circle/capsule/rectangle/roundrect
copper against serialized keepouts over multiple rotations, translations and
distances. Six native fixtures refill real foreign-net copper and measure the
saved fill, requiring the surrounding pour to remain present. Running the
initial four native regressions against immutable 638fe93 fails all four;
the corrected implementation passes. Separate tests witness coordinate
rounding on a straight edge and sagitta before serialization.

The real-board replay validates this bounded geometry fix, not exact-head
routing convergence. Its DRU contains existing fabrication floors, not the
exported HV domain block. #4507 must execute the existing #4508 export path,
then provide full routing, power and dual-engine validation. Epic #4431 still
requires 100% routing and zero board-level census failures. Always run the
post-route keepout/native-refill step before the final census; Step2 alone
has no keepout areas and cannot establish this property.
