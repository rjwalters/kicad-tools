# Project-local 3D bodies (Issue #7685)

| Ref | Footprint | File | Source tier | License |
|---|---|---|---|---|
| J1 | `Connector_USB:USB_C_Receptacle_HRO_TYPE-C-31-M-12` | `USB_C_Receptacle_HRO_TYPE-C-31-M-12.wrl` | (ii) project-local, generated | original work of the kicad-tools project |
| RSH1 | `Resistor_SMD:R_Shunt_Vishay_WSK2512_6332Metric_T1.19mm` | `R_Shunt_Vishay_WSK2512.wrl` | (ii) project-local, generated | original work of the kicad-tools project |
| L1 | `board09:SRP7050TA` | `SRP7050TA.wrl` | (ii) project-local, generated | original work of the kicad-tools project |

None of these has an upstream STEP: no HRO Type-C model exists upstream, the WSK2512 shunt is
absent (only `WSKW0612`, a different size, exists) and `board09:SRP7050TA` is a custom
footprint.  Each body is a nominal-envelope box model written by
`scripts/boards/generate_local_3d_bodies_7685.py` and authored in the footprint frame (identity
offset/rotation).  No vendor or EasyEDA geometry is used, so there is no LCSC C-number or STEP
sha256 to record.  `top-view-calibration.png` is the `kicad-cli pcb render --side top` used to
check position and orientation against the pads and courtyards.
