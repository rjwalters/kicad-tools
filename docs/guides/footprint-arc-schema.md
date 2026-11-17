# Footprint arc coordinates

`FootprintGraphic.mid` preserves the midpoint of a modern
`fp_arc (start ...)(mid ...)(end ...)`. For legacy center/angle arcs, the shared
`_arc_points_from_sexp` normalizer derives genuine on-arc start, midpoint and
end coordinates from the signed sweep. The three points remain footprint-local;
parsing does not apply placement or rotation again. An explicit modern midpoint
wins if a legacy angle also appears.

The optional field follows `uuid`, preserving existing positional constructor
arguments. Non-arc graphics retain `mid=None`. `GraphicArc` uses the same
normalizer with its existing tolerant defaults; this does not introduce strict
malformed-geometry validation. Programmatic objects with missing arc data should
not be treated as validated geometry merely because a default exists.

Recovery mirroring reflects all three parsed points and their raw coordinates.
A legacy arc's sweep changes sign under reflection so save/reload describes the
same reflected curve. The original source serialization remains authoritative;
adding the parsed field does not rewrite arcs into a different encoding.

Consumer audit for #5112:

- `geometry/courtyard.py` models rect, polygon and chained line courtyards; it
  explicitly skips circle/arc courtyards. This change does not resolve them.
- `validate/rules/silkscreen.py:_stroke_geometry` models line/rect strokes;
  circle/arc strokes remain outside its geometry coverage.
- `validate/rules/copper_sliver.py` consumes footprint polygons on copper,
  not arc strokes. Arc midpoint availability does not broaden that check.
- `recovery/applicator.py` consumes parsed graphics during footprint reflection;
  it now mirrors the midpoint together with the other modeled coordinates.

This schema change does not implement #4884's separate outline chaining work,
complete curve-aware geometry consumers, or any manufacturing qualification.
