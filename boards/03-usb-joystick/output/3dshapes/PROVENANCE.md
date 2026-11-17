# Project-local 3D bodies (Issue #7685)

| Ref | Footprint | File | Source tier | License |
|---|---|---|---|---|
| J2 | `Connector_JST:JST_PH_S5B-PH-SM4-TB_1x05-1MP_P2.00mm_Horizontal` | `JST_PH_S5B-PH-SM4-TB.wrl` | (ii) project-local, generated | original work of the kicad-tools project (same license as this repository) |

The stock STEP for this footprint is absent from KiCad 10.0.7 and from upstream
`kicad-packages3D` (master, 10.0 and 9.0), so the library footprint carried a dangling ref.
The body is a nominal-envelope box model (12.0 x 5.5 x 6.0 mm housing plus hold-down tabs)
written by `scripts/boards/generate_local_3d_bodies_7685.py`; it uses no vendor or EasyEDA
geometry.  It is authored in the footprint frame, so the model node has an identity
offset/rotation.  No LCSC fetch is involved, so there is no C-number or STEP sha256 to record.

`top-view-calibration.png` is the `kicad-cli pcb render --side top` of the routed board: J2's
signal pads sit outside the housing and its mounting pads under it, as on the real part.

J3 (`Joystick:Samtec_TSM-103-01-T-DV`) keeps a **generic proxy**: a stock SMD 2x03 header
body that approximates, but is not, the Samtec TSM-103-01-T-DV.
