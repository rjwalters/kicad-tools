# STM32 board procurement review

Use exact reviewed parts; substitutions need electrical and footprint review. U1 is corrected to Microchip MCP1825S-3302E/DB, whose real 1=VIN / 2=GND / 3=VOUT pinout matches this board. Do not install AMS1117. See DESIGN_REVIEW.md and issue #5011.

| Reference | Exact part | Supplier |
|---|---|---|
| U1 | MCP1825S-3302E/DB | [C148031](https://www.lcsc.com/product-detail/C148031.html) |
| U2 | STM32F103C8T6 | [C8734](https://www.lcsc.com/product-detail/C8734.html) |
| C1 | CL21A106KAYNNNE | [C15850](https://www.lcsc.com/product-detail/C15850.html) |
| C2 | CL21A106KAYNNNE | [C15850](https://www.lcsc.com/product-detail/C15850.html) |
| C3 | CC0805KRX7R9BB104 | [C49678](https://www.lcsc.com/product-detail/C49678.html) |
| Y1 | X49SD8MSB2SC | [C114147](https://www.lcsc.com/product-detail/C114147.html) |
| C10 | CL21C200JBANNNC | [C1798](https://www.lcsc.com/product-detail/C1798.html) |
| C11 | CL21C200JBANNNC | [C1798](https://www.lcsc.com/product-detail/C1798.html) |
| C12-C15 | CC0805KRX7R9BB104 | [C49678](https://www.lcsc.com/product-detail/C49678.html) |
| C16 | CL21A475KAQNNNE | [C1779](https://www.lcsc.com/product-detail/C1779.html) |
| R1 | 0805W8F3300T5E | [C17630](https://www.lcsc.com/product-detail/C17630.html) |
| R2 | 0805W8F1002T5E | [C17414](https://www.lcsc.com/product-detail/C17414.html) |
| D1 | 17-21SURC/S530-A2/TR8 | [C131244](https://www.lcsc.com/product-detail/C131244.html) |
| J1 | 2.54-1x6P | [C37208](https://www.lcsc.com/product-detail/C37208.html) |

## 2026-10-18 CPL rotation correction (issue #7200)

`cpl_jlcpcb.csv` is regenerated with the JLCPCB rotation profile
(`jlcpcb_rotations.yaml`) and in the Gerber/drill-origin frame (#7059). The
2026-09-10 package shipped these rotations uncorrected. Rotation changes are
placement changes, so this section records them explicitly:

| Reference | Footprint | Old rotation | New rotation |
|---|---|---|---|
| D1 | LED_0805_2012Metric | 0 | 180 |
| U1 | SOT-223-3_TabPin2 | 0 | 180 |
| U2 | LQFP-48_7x7mm_P0.5mm | 0 | 270 |

Raw KiCad `pos` reports 0 for all three; the new values come from the JLCPCB
profile, not from the board. **Pending human orientation review:** no person
has yet checked these against the JLC part footprints / datasheet pin-1 marks.
Do not order assembly from this CPL until that review is recorded here.
