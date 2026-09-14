STM32 development board — fabrication and assembly package

Order TWO layers, 1.6 mm FR4, 1 oz copper, tented through vias, and the PAID 0.15 mm minimum MECHANICAL via-hole option. See manufacturing-requirements.json. No laser microvias or via-in-pad process is used.

Use bom_jlcpcb.csv and cpl_jlcpcb.csv for 15 SMT placements. Y1 crystal and J1 SWD header require manual through-hole assembly, listed in manual-assembly-bom.csv. U1 MUST be Microchip MCP1825S-3302E/DB (C148031); AMS1117 has the wrong pinout for this board. No automatic substitutions.

Power with regulated 5 V only, maximum 6 V. Verify 3.3 V before attaching peripherals; then verify SWD programming, crystal, reset/BOOT0 and PB12 LED. Thermal/load qualification remains required on assembled hardware.

Gerbers: gerbers/gerbers.zip. Editable sources: kicad_project.zip. Engineering review: DESIGN_REVIEW.md. Source scripts: design-source/. Native DRC/ERC and reviewed Python checks accompany the final package.
