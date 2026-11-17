"""Fixture-board writer shared by the mask expansion rule tests (Issue #8847)."""

import uuid


def write_board(path, pads, setup="(pad_to_mask_clearance 0)"):
    body = ""
    for n, (shape, size, margin) in enumerate(pads, start=1):
        body += (
            f'(pad "{n}" smd {shape} (at {n * 3} 0) (size {size}) '
            f'(layers "F.Cu" "F.Mask") {margin} (uuid "{uuid.UUID(int=n)}"))'
        )
    path.write_text(f"""(kicad_pcb (version 20241229) (generator pcbnew)
(general (thickness 1.6)) (paper "A4")
(layers (0 "F.Cu" signal) (31 "B.Cu" signal) (38 "B.Mask" user) (39 "F.Mask" user))
(setup {setup})
(footprint "Fixture" (layer "F.Cu") (at 10 10 0) (property "Reference" "U1") {body}))""")
    return path
