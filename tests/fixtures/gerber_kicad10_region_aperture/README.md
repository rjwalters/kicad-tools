# Gerber fixtures: KiCad 10 zero-size aperture and self-touching zone-fill region

These fixtures pin the two legal KiCad 10 constructs that the strict Gerber
reader (`src/kicad_tools/validate/gerber_geometry.py`) wrongly rejected
(Issue #8947, split from #8939):

- `kicad10_simple_led_zone_fill_B_Cu.gbl` — **cause 3** (self-touching
  region contour). Extracted verbatim from a committed KiCad 10.0.6 export:
  `boards/00-simple-led/output/manufacturing/gerbers/gerbers.zip` →
  `simple_led_routed-B_Cu.gbl`, keeping the file header (X2 metadata, format,
  units, aperture list) and the copper-pour `G36`/`G37` region block
  (lines 45–324 of the source file, which carry a contour Shapely calls
  invalid because the pour pinches off at a point). Pad flashes from the
  source layer are dropped so the fixture isolates the region: both this
  reader and the gerbonara oracle (`tests/_mask_gerbonara_oracle.py`) return
  the identical region area `457.199286 mm^2`.

- `kicad10_zero_size_aperture_region_mode.gbr` — **cause 2** (zero-size
  aperture defined, selected, and used only for region mode). **Synthetic**,
  not a KiCad export: the committed 10.0.6 exports contain no
  `%ADD..C,0.000000*%` (the construct is conditional on something those
  exports do not exercise), so this fixture models the documented KiCad
  behaviour of selecting a zero-width circular aperture as the current
  aperture while emitting `G36`/`G37` regions. Defining, selecting, and
  filling a region with it is legal; flashing or stroking it is not
  (covered by inline cases in `tests/test_gerber_geometry_8939.py`).

Before issue #8947 both fixtures failed with `Invalid region contour` /
`Nonpositive aperture`; both must parse afterwards.
