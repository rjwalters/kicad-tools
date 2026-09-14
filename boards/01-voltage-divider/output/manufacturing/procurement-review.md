# Board01 Voltage Divider — procurement reconciliation

## Current package — 2026-09-10

The committed [project.kct](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/01-voltage-divider/project.kct),
[BOM](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/01-voltage-divider/output/manufacturing/bom_jlcpcb.csv) and
[SMT CPL](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/01-voltage-divider/output/manufacturing/cpl_jlcpcb.csv)
agree on the following corrected selections and assembly treatment.

| Reference | Reviewed manufacturer part | LCSC | Current footprint / assembly |
|---|---|---|---|
| R1, R2 | 0805W8F1002T5E | C17414 | 10 kΩ, 0805 resistors; SMT CPL includes R1 and R2 |
| J1, J2 | ZX-PZ2.54-1-2PZZ | C7501260 | 2-pin 2.54 mm through-hole headers; hand-solder, excluded from SMT CPL |

## Fleet audit context

The source corrections and redesigns were integrated in commit
[d95b6eff](https://github.com/rjwalters/kicad-tools/commit/d95b6eff369c1479c4dce6d4a7bc2b4ef73f9350).
The original fleet findings below describe the retired designs:

- Board02 is now a real ATtiny85-20PU circuit. Its [hardware guide](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/02-charlieplex-led/HARDWARE.md) specifies the pinout, programming and manual through-hole assembly; the synthetic MCU warning below is historical.
- Board03 revision B uses the real 44-pin ATmega32U4-AU. See its [design review](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/03-usb-joystick/DESIGN_REVIEW.md) and [assembly/firmware guide](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/03-usb-joystick/README.md). [Issue #5000](https://github.com/rjwalters/kicad-tools/issues/5000) still tracks USB firmware identity and suspend qualification as of this reconciliation.
- Board05 revision B replaces the former shunt/buck topology with the DRV8313-based design. Its [hardware guide](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/05-bldc-motor-controller/redesign/HARDWARE.md) and [README](https://github.com/rjwalters/kicad-tools/blob/27ea2695a91c632faed2df8fc518c5a077e6d2d7/boards/05-bldc-motor-controller/README.md) describe the current BOM, conservative operating targets and pending motor/thermal bench validation. The old DRV8301 source is an archived regression witness; [#4993](https://github.com/rjwalters/kicad-tools/issues/4993) and the part-reconciliation issues [#5046](https://github.com/rjwalters/kicad-tools/issues/5046)/[#5047](https://github.com/rjwalters/kicad-tools/issues/5047) are closed.

The broader [procurement audit #4971](https://github.com/rjwalters/kicad-tools/issues/4971) remains the cross-board tracker. These references do not establish live supplier stock, assembly-service eligibility, or completed hardware qualification.

## Evidence and limits

This 2026-09-10 update reconciles documentation with committed source/BOM/CPL
identities at `27ea2695a91c632faed2df8fc518c5a077e6d2d7`. The supplier links in the dated audit below retain their
original provenance; no new supplier lookup or stock check was performed.
The corrected BOM/CPL already exist in this package. This update replaces the
procurement document and refreshes its manifest/archive/readiness hashes only.
Existing native DRC, electrical check, board geometry, and manufacturing output
bytes remain the previously verified evidence; no new native refill or bench
verification is claimed. Follow the package README for assembly handling.

## Historical audit — 2026-09-09

The following audit and its subsequent local-correction notes are preserved
verbatim for provenance. Its references to “current blockers,” a synthetic MCU,
and pending regeneration describe that earlier state and are superseded by the
reconciliation above; they are not ordering instructions for this package.

<details>
<summary>Original fleet procurement audit and correction notes (historical)</summary>

Demo manufacturing BOMs can be geometrically DRC-clean while ordering electrically unrelated or physically incompatible parts. Audited the committed `boards/00`–`05` `project.kct`, current schematic/PCB values and footprints, and LCSC primary product pages on 2026-09-09.

Concrete blockers (supplier pages linked):

| Board/reference | Required by BOM/PCB | Assigned supplier item | Defect |
|---|---|---|---|
| 03 U1 | MCU, TQFP-32 7x7mm 0.8mm | [C44854](https://www.lcsc.com/product-detail/C44854.html), ATMEGA32U4-AU QFP-44 10x10mm | MCU does not fit; cannot fix with ID substitution without reconciling symbol, pinout and layout. |
| 05 R10–R12 | 5mΩ 2512 current sense resistor | [C76662](https://www.lcsc.com/product-detail/C76662.html), TDK C3216X7R1E155KT000N 1.5uF 1206 ceramic capacitor | Wrong component type/value/package. |
| 05 U1 | LM2596-5.0 fixed 5V buck | [C29781](https://www.lcsc.com/product-detail/C29781.html), LM2596SX-ADJ/NOPB | Adjustable variant substituted into fixed-output circuit. |
| 00 D1 | 5mm THT LED | [C84256](https://www.lcsc.com/product-detail/C84256.html), NCD0805R1 SMT0805 LED | Wrong assembly/package. |
| 00 J1; 01 J1/J2 | 2-pin male header | [C49257](https://www.lcsc.com/product-detail/C49257.html), 3-pin header | Wrong pin count. |
| 02 D1–D9; 04 D1; 05 D4 | Red LED0805, intended17-21SURC/S530-A2/TR8 | [C72038](https://www.lcsc.com/product-detail/C72038.html), yellow0603 | Wrong package and MPN. Intended part is [C131244](https://www.lcsc.com/product-detail/C131244.html). |
| 04 C3; 05 C2/C7/C8 | 100nF0805 | [C1525](https://www.lcsc.com/product-detail/C1525.html), 100nF0402 | Wrong package. |
| 05 C6 | 10uF0805 | C1525, 100nF0402 | Wrong value and package. |
| 04/05 C10/C11 | 20pF0805 | [C1554](https://www.lcsc.com/product-detail/C1554.html), 20pF0402 | Wrong package. Intended CL21C200JBANNNC is [C1798](https://www.lcsc.com/product-detail/C1798.html). |
| 04/05 Y1 | THT HC49, 8MHz | [C12674](https://www.lcsc.com/product-detail/C12674.html), HC49S-SMD | Wrong assembly. |

Board02 U1 is explicitly synthetic: `generate_design.py:330` describes a stand-in MCU footprint; `design_spec.py:129` assigns GPIO1–4, VCC7, GND5/6/8. Do not infer that a real ATtiny can be ordered for it. The BOM's C57215 could not be independently resolved and is unverified, not demonstrated valid.

C1525 defects were previously fixed in closed #3590/#3597, but source `project.kct` still prescribes bad assignments. This issue covers source provenance and remaining fleet defects; it is not requesting another copy of the passive package guard. Existing `check_lcsc_against_cache` accepts unknown/missing cache records, which is not sufficient evidence of procurement correctness.

Suggested resolution: correct verified source MPN/ID pairs; treat THT as manual assembly; require explicit verified identities for active ICs; reconcile board03 MCU symbol/pinout/footprint; choose a real 5mΩ2512 shunt and fixed5V buck on05; then regenerate BOM/CPL/manifests and record what remains unverified. Do not label bundles assembly-ready solely on DRC or nonempty LCSC columns. Track unverified05 bulk capacitors (220uF/470uF currently0805), input/output current-rated connectors, and missing IDs separately.

Duplicate check: open issue list plus searches for C76662/C44854/C1554/C29781/C49257 and procurement; no open issue covers these defects. #3209/#3216 concern missing BOM footprint columns; #4410 concerns routing/shorts, not procurement.

Local source corrections applied after filing (project.kct only; BOMs/manifests still require parent regeneration):

| Board | Corrected references/MPN/ID | Assembly |
|---|---|---|
|00|J1 ZX-PZ2.54-1-2PZZ C7501260; D1 EVERLIGHT333-2SURD/S530-A3 C87271; R1 C17630 already verified|J1/D1 manual THT; R1 SMT|
|01|J1/J2 ZX-PZ2.54-1-2PZZ C7501260; R1/R2 C17414 already verified|Headers manual THT; resistors SMT|
|02|D1–D9 intended17-21SURC/S530-A2/TR8 corrected to C131244; R1–R4 C17630 verified|LED/resistor SMT; U1 remains synthetic and unresolved|
|04|C3 CC0805KRX7R9BB104 C49678; C10/C11 CL21C200JBANNNC C1798; D1 intended17-21SURC/S530-A2/TR8 C131244; J1 male2.54-1x6P C37208; Y1 X49SD8MSB2SC C114147|J1/Y1 manual THT; remaining SMT|
|04|Added explicit verified entries for U2 STM32F103C8T6 C8734, C12–C15 C49678, C16 CL21A475KAQNNNE C1779, R2 C17414|Avoid relying on matcher for these known parts|

Sources for corrections:
- https://www.lcsc.com/product-detail/C7501260.html (2-pin male2.54mm,3A; manufacturer drawing https://datasheet.lcsc.com/datasheet/pdf/8d58661dd06d5589770f936547580a11.pdf?productCode=C7501260)
- https://www.lcsc.com/product-detail/C87271.html (red5mm THT, Vf2V; manufacturer datasheet https://en.everlight.com/wp-content/plugins/ItemRelationship/product_files/pdf/333-2SURD-S530-A3.pdf)
- https://www.lcsc.com/product-detail/C17630.html (330ohm0805)
- https://www.lcsc.com/product-detail/C17414.html (10kohm0805)
- https://www.lcsc.com/product-detail/C131244.html (red0805 exact intended MPN)
- https://www.lcsc.com/product-detail/C49678.html (100nF50V0805)
- https://www.lcsc.com/product-detail/C1798.html (20pF50V0805 exact intended MPN)
- https://www.lcsc.com/product-detail/C37208.html (6-pin male2.54mm THT)
- https://www.lcsc.com/product-detail/C114147.html (8MHz20pF HC49 THT; manufacturer drawing https://datasheet.lcsc.com/datasheet/pdf/7aa50fcc25c40eb654f0738d3a2cd1ed.pdf?productCode=C114147)
- https://www.lcsc.com/product-detail/C8734.html (STM32F103C8T6 LQFP48 7x7)
- https://www.lcsc.com/product-detail/C1779.html (4.7uF25V0805)
- https://www.lcsc.com/product-detail/C6186.html (AMS1117-3.3 SOT223; unchanged correct)
- https://www.lcsc.com/product-detail/C15850.html (10uF25V0805; unchanged correct)

Caveats: this is identity/package verification, not a claim of hardware bring-up or live JLCPCB assembly eligibility. Bulk caps/current/thermal ratings and custom footprint geometry on05 remain unresolved. Board03 is a real pinout/layout redesign to use ATmega32U4. Board02 cannot be called a completed standalone MCU design. No generators, schematics, boards or export artifacts edited by this audit.

</details>
