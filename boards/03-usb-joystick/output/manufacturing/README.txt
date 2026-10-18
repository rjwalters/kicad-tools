USB joystick revision B — fabrication and assembly package

Order the exact four-layer stackup and Epoxy-filled & Capped POFV/VIPPO process in manufacturing-requirements.json. Do not use ordinary open or merely tented vias.

Use bom_jlcpcb.csv and cpl_jlcpcb.csv for assembly. Since issue #7239 they also list the through-hole J1 USB4085-GF-A for JLCPCB assembly (formerly a separate manual operation); procurement must review the J1 row before ordering (see procurement-review.json). Use exact reviewed supplier parts; no automatic substitutions.

After inspection, program the supplied 8 MHz firmware and fuses through J3 as described in firmware/README.md. Power the programmer/board from one regulated 5 V source with USB disconnected. No bootloader is required.

Gerbers are in gerbers/gerbers.zip; editable KiCad files are in kicad_project.zip. DESIGN_REVIEW.md documents electrical and layout review. Physical bring-up and USB qualification remain to be performed on assembled hardware.
