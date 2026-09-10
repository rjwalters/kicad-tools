# Historical demo-board algorithm witnesses

Geometry captured from `ff4287fe5aabb7cc922979fa631af25d69cf5ab8`, before the real hardware redesign in `d95b6eff`.
These fixtures preserve the topology, net IDs, pad counts, and known defects used by router, connectivity, and schematic regression tests. They are not manufacturing artifacts. Live board release/DRC gates continue to inspect `boards/*/output`.

Existing byte-identical regression witnesses are referenced by relative symlinks to avoid duplicating PCB data.

- `02-charlieplex-led/output/charlieplex_3x3.kicad_pcb`: SHA-256 `1345d70c6dfecd796150b904b7ba688e1036761affd13bbce56b686914d1ff4b`
- `02-charlieplex-led/output/charlieplex_3x3_routed.kicad_pcb`: SHA-256 `6b45107600fc9992138c7431dafa7ca0a3713b6cf547943d4405301212005b87`
- `03-usb-joystick/output/usb_joystick_routed.kicad_pcb`: SHA-256 `f4ee2d9b1cf82f10aa86d5ac8277118518d0d9a9ba9de2c3abdafe69eee14397`
- `03-usb-joystick/output/usb_joystick.kicad_sch`: SHA-256 `fce9cfd4cd75d895a8a007b0d0d14baa56df4f0821b1113abb98412762489d21`
- `05-bldc-motor-controller/output/bldc_controller.kicad_pcb`: SHA-256 `655dad571ae1eeab28c20404ace172263710425fd132ff191982f3aba42041ce`
- `05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb`: SHA-256 `e1b116769ab75b8998b90bc094f4bafe4d428a444c5207d1f7317efdf2efc9eb`
- `05-bldc-motor-controller/output/bldc_controller.kicad_sch`: SHA-256 `e25e02188f5fc3aadb7b52ca753f82dff27a5418fae0229fe67bcd6034cc5391`
- `07-matchgroup-test/output/matchgroup_test.kicad_pcb`: SHA-256 `a4b3e4cd05ba68737d1cee346a0bd166503b99c14cbdab3c29a8258d1aa2cee2`
- `07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb`: SHA-256 `0c4b677fd475123123b04cc8c87d6cf0197aef66a0948242d511a013bdc0aa39`
- `05-bldc-motor-controller/output/manufacturing/bom_jlcpcb.csv`: SHA-256 `eea06d8c1abd76dc2527161f8c645e626476e3997bbbb3ccad6db826b475b6fd`
- `01-voltage-divider/output/voltage_divider.kicad_pcb`: SHA-256 `0d0de6e7cba49e6a76c386b1ce7bcf142f36745b53b6787fa8dad8dc2eb1712f`
- `03-usb-joystick/output/usb_joystick_routed.kicad_pro`: SHA-256 `f47fa7215c1ecdd33f5eb8002c907923309cb038f9410c36761f2a80a4fe40df`
- `03-usb-joystick/output/usb_joystick_routed.kicad_dru`: SHA-256 `94bc1f9eeef8c85bc94b3ff618e19a9778846e85868bb80dbb82e2b2109770ab`
