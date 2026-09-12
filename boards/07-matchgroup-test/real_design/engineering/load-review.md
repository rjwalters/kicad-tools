# Board07 independent load review

Read-only review; physical layout and checkers unchanged. Missing driver-load gate filed as [#5020](https://github.com/rjwalters/kicad-tools/issues/5020), after searches for capacitance and SDRAM/load issues.

| Item | Published value | Provenance / limitation |
|---|---:|---|
| STM32F429 I/O pin CIO | 5 pF typical | No maximum; DS9405 Rev13 Table57 p136 |
| SDRAM address/control CIN | 3.8 pF maximum | RevG1 p14 |
| SDRAM CLK CCLK | 3.5 pF maximum | RevG1 p14 |
| SDRAM DQ CI/O | 6.5 pF maximum | RevG1 p14 |
| MCU data/address timing external CL | 30 pF | Tables103/105 footnote1, characterization condition |
| MCU SDCLK timing external CL | 15 pF | Same footnote; separate clock budget |

[ST primary datasheet](https://www.st.com/resource/en/datasheet/stm32f429zi.pdf). The source's own pin capacitance must not be added again to external output CL. DQ requires evaluation in both directions. ST lists an official IBIS archive, but its download was unavailable during this bounded review; numerical package/C_comp corner values were not verified. Do not invent a separate package maximum or equate typical with maximum. Firmware currently selects OSPEEDR=11.

[ISSI primary datasheet](https://www.issi.com/ww/pdf/42-45s16400j.pdf). Capacitance table conditions are Ta0–25°C, VDD=VDDQ=3.3±0.3V, f=1MHz; industrial operating temperature does not extend those stated capacitance conditions. These are device pin specifications; adding an arbitrary package capacitor on top risks double counting. An IBIS model instead requires its explicit C_comp and package parasitics.

## Via allowance and implementation recommendation

[TI SLYT335 p30](https://www.ti.com/lit/pdf/slyt335) gives the approximate full-via capacitance:

`Cvia[pF] = 1.41 * er * T[inches] * Dpad / (Dantipad - Dpad)`

For board07, use the entire1.6mm barrel even if signal travel stops at an inner layer. With er4.6, .5mm pad and .8mm antipad (.15mm edge clearance), estimate is0.681pF. At .127mm clearance it is0.804pF. A sensitivity assumption er5 and thickness1.76mm gives0.814pF/.962pF respectively. Thus1pF per physical via is a reasonable declared engineering allowance when actual minimum clearance is at least.127mm. Count each physical via once, including dangling barrel stubs, not once per layer. Formula uses pad diameter, not the .2mm finished drill; drill matters for barrel inductance and more complete field models. It is an approximation with reserve, not a proven upper bound; inspect actual filled-plane antipads and fabrication tolerances.

For MCU-driven clock require `Ctrace_tree + Nvia*1pF + 3.5pF <= 15pF`; the interconnect allowance is11.5pF. Data writes use receiver6.5pF; address/control use3.8pF. DQ reads require the MCU receiver allowance, whose published maximum is unknown. A deliberate10pF MCU design reserve can be recorded but must be labeled an assumption, not a vendor limit.

If retaining a separate30pF whole-net bookkeeping budget, known nominal end-pin sums are clock8.5pF, address/control8.8pF, DQ11.5pF; all include ST typical and are not worst-case bounds. Passing whole-net30pF never substitutes for clock external15pF.

## Remaining validation

Extract final trace tree, every physical via, actual antipads and unmodeled PCB land capacitance. Preserve the distinction between estimates, assumed reserves and maximum specifications. Confirm MCU package/input corners via applicable IBIS or ST data; confirm operating-temperature scope with vendor data if claiming industrial limits. Check both DQ directions, slew/overshoot/undershoot, setup/hold and simultaneous-switching effects with actual package/driver models or prototype measurement. A48MHz clock does not remove fast-edge SI requirements. Current trace-only20pF gate cannot establish these driver-load budgets.

### Status as of #5134

- **Via capacitance**: `validate.py`'s `external_load()` gate now computes per-via capacitance with the SLYT335 formula above from measured geometry -- the via's own pad diameter (`Via.size`), the antipad clearance the native `--refill-zones` pass just applied (read back from the board's own merged `min_clearance` design rule, not a separately assumed constant), and the stackup's thickness-weighted average dielectric constant (`Stackup.from_pcb`). The flat 1 pF/via allowance is retained as an explicit fallback whenever any of those inputs is unavailable or the physical via list doesn't match the counted via total, so the gate never silently mixes measured and assumed contributions. This closes the first "remaining validation" item above for board07's own construction; it does not generalize to a board with a different antipad topology (e.g. a via with a thermal-relief spoke rather than a full annular antipad) without further work.
- **MCU package/input capacitance corners**: still unconfirmed. ST's IBIS archive remained unreachable from this environment during #5134 as it was during the original review -- there is no network access to fetch and parse it from an automated Builder pass. The 10 pF DQ (SDRAM-drives/MCU-receives direction) allowance stays labeled `"engineering reserve"` in the gate's output rather than being upgraded to a sourced maximum. A human with access to ST's IBIS model download (or a prototype measurement) is needed to close this; track under #5134 if reopened, or file a fresh issue once IBIS access is available.
- **DQ bidirectional evaluation**: `external_load()` now evaluates both directions explicitly -- SDRAM-drives/MCU-receives (10 pF engineering reserve) and MCU-drives/SDRAM-receives (6.5 pF ISSI maximum) -- and reports both in `external_load_estimates[net]["directions"]`, rather than folding them into one opaque number. The worse direction still governs the single `estimated_external_load_pf` field for backward compatibility, and happens to remain the MCU-receiver reserve today; a future net-class change that gave the two directions different limits would now be caught instead of silently passing.
- **Slew/overshoot/undershoot/setup-hold/SSN**: explicitly out of scope for this checker. `validate.py` is a static lumped-capacitance preflight over placed geometry; it has no driver I/V curves, transmission-line time-domain model, or measured prototype data, and none of those belong in a build-time PCB checker. That modeling belongs to hardware bring-up (scope/TDR measurement on the assembled board) or a dedicated SI simulation tool (e.g. an IBIS-based SPICE/HyperLynx-class flow) fed by the same geometry this gate already extracts. This decision is unchanged from the original review; #5134 does not add a lightweight screen for it.
