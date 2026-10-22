# Intra-pair skew compensation and uncoupled-length budgets

Research note for the kicad-tools diff-pair skew fixer. Compiled 2026-10-19.
Each source is tagged **[read]** (primary text read directly) or **[snip]** (number taken from a search-result excerpt of the page; the page itself could not be fetched, e.g. Intel docs redirect). Conversion used throughout: ~6.7 ps/mm (150 µm/ps; Toradex [T] and UG583's 169.5 ps/in [X1] agree). 1 mil = 0.0254 mm.

## Headline findings

1. **KiCad upstream already excludes tuning patterns from the uncoupled total.** A KiCad developer confirmed on the forum (June 2026) that counting meanders as uncoupled "was indeed a bug", fixed by commit `7afb67fb` "pcbnew: exclude tuning patterns from diff-pair uncoupled length total" [K3]. Our "combined uncoupled length may not grow" rule therefore copies behaviour that KiCad itself treats as a bug. Caveat: the exclusion applies to KiCad *tuning-pattern objects*. Bumps our router emits as plain track segments will still count in kicad-cli DRC.
2. **Every vendor that addresses placement says to compensate near the mismatch source, segment by segment.** That is the bend or pad end, and each via-delimited segment is matched on its own (TI [TI1][TI2], NXP/Philips [N1], Renesas [R1], Toradex [T], Cadence [C1], Altium blog [A2]). Altium's tool docs are the outlier: static phase matching may go "anywhere along the length of the shorter net" [A1].
3. **No standard defines a "max uncoupled length".** It appears only as vendor breakout rules. Examples: PCIe pin/ball entry ≤45 mil [N1], PCIe breakout ≤250 mil [R1], and "compensate within 15 mm of the bend; don't propagate asynchronously >15 mm" [T]. KiCad ships no default and reports nothing until a custom rule is set [K1][K2].
4. **Bump geometry has a consensus.** Inside the bump the trace spacing S1 must stay below 2× the spacing S of the unbumped pair: AN10373 says the maximum distance between traces in a serpentine section "should be less than two times the distance between traces in a non-serpentine section" [N1][T]. The bump's push-out beyond the nominal gap is therefore **less than 1× the gap** (operator ruling Q27 on #7521); an earlier version of this note misquoted the rule as "excursion ≤ 2× gap". kct applies 2× as an interim (operator ruling Q35; issue #7649 tracks the return to < 1×). Each jog ≥3× trace width (15 mil at W = 5 mil) [N1][T]. TI's worked example is W = 6, A = 8 mil → B ≥ 16 mil, C ≥ 18 mil [TI2]. Small offsets under-deliver: current "scoots" around a shallow bulge, and bulges can't reliably correct skews below about 1 ps [S1].

## 1. Technique

- **Placement.** TI: add serpentine "as close to the mismatched ends as possible" [TI1][TI2]. Philips/NXP AN10373: "added near the location where the mismatch occurs"; in breakouts, "as close as possible to signal pins without introducing any tight bends" [N1]. Renesas Tsi381: match "on a segment-by-segment basis rather than only as an overall measurement" [R1]. Toradex: place it "at the origin of the length mismatching"; for bends, "close to the bend with a maximum distance of 15mm"; opposite bends <15 mm apart cancel and need no serpentine; vias split a pair into segments that must each be matched [T]. Cadence: "ideally placed at or around the bend where the length diverges" [C1, snip]. Altium blog (Peterson): "apply the length tuning exactly where skew arises", using small sections near the breakout bends [A2].
- **Physics nuance.** Oracle/SI Journal: in **stripline**, where even and odd modes travel at the same speed, the location of compensation barely changes mode conversion. In **microstrip** it "will significantly affect" the result [S1]. Locality matters most on outer layers.
- **Few small or one long?** No source endorses one long uncoupled detour. Altium/Peterson prefer short structures and small sections at bends [A2], and Altium's phase tuner uses sawtooth "to minimize the disruption to the impedance balance" [A1]. UG583's preferred bend fix is a small jog-out at the corner. It cut a 16 mil (2.68 ps) bend mismatch to a 0.75° phase error, and Xilinx notes "the uncoupled section of the jog-out must not be widened" [X1].
- **Lengthen the short leg, or something else?** First choice is to avoid the skew. Use symmetric breakout and balanced left/right bends [N1][R1][T], and use transceiver polarity swap rather than P/N crossover vias [X1]. Next comes "a small loop to the shorter trace" in the breakout, which Toradex calls "preferred… as opposed to creating a serpentine" [T]. Then come serpentine bumps on the short leg. Opposite-sense bends are also a valid compensator (<0.05 ps residual in stripline) [S1].
- **IPC.** IPC-2141A is a controlled-impedance design guide. Nothing found in IPC-2141A or IPC-2251 sets skew or uncoupled-length budgets [I1, snip].

## 2. Per-interface limits

All limits below are intra-pair (P vs N) unless noted. Time limits are converted at 6.7 ps/mm.

| Interface | Intra-pair skew limit | Type | Uncoupled-length guidance | Sources |
|---|---|---|---|---|
| MIPI D-PHY (CSI-2/DSI) | <1 ps ≈ 0.15 mm (Toradex); ±2 ps ≈ 0.3 mm incl. package (AMD Versal); spec-defined via mode-conversion S-params, "general estimate UI/50" (8 ps at 2.5 Gb/s) (TI) | time | None stated. TI notes the pairs "must be loosely coupled" (LP mode is single-ended) | [T][X2][TI1] |
| MIPI C-PHY | ±2 ps between lines within a trio, incl. package | time | None (trio, no P/N) | [X3] |
| USB 2.0 HS | 50 mil = 1.27 mm (TI AM-series); <7.5 ps ≈ 1.1 mm (Toradex); 150 mil (TI SWCA124) | length / time | None stated | [TI1][T][TI3, snip] |
| USB 3.x SS | 5 mil = 0.127 mm (TI); <1 ps ≈ 0.15 mm (Toradex) | length / time | None stated | [TI1][TI2][T] |
| PCIe Gen2/3/4 | 5 mil (TI, Philips, Renesas); <1 ps ≈ 0.15 mm (Toradex, Gen1–3); ±1 mil (Intel Agilex PCIe 3.0 and transceiver tiles) | length / time | Pin/ball entry ≤45 mil (1.14 mm) with multiple bends; breakout ≤250 mil (6.35 mm); widen uncoupled runs >100 mil to 7 mil | [TI1][N1][R1][T][IN1, snip] |
| HDMI/TMDS | Source total 0.15·Tbit (spec, via TI; my arithmetic: ≈44 ps at 3.4 Gb/s, ≈25 ps at 6 Gb/s); PCB: 5 mil (TI), <5 ps ≈ 0.75 mm (Toradex) | time (spec) / length | None stated | [TI2][TI1][T] |
| DDR4 DQS / CK | 2 ps ≈ 12 mil (0.3 mm) (Xilinx UG583); <1 ps for DQS/DQS# and CLK/CLK#, matched in time incl. package (Intel) | time | None stated | [X1][IN2, snip] |
| LPDDR4 DQS / CK | within 1 ps (NXP i.MX 8M Plus HDG) | time | None stated | [N2, snip] |
| (generic SerDes) | ~2.5 ps at 10 Gb/s is the bend-mismatch scale UG583 calls worth fixing | time | Toradex: ≤15 mm asynchronous propagation | [X1][T] |

## 3. Budget versus limit

- **The skew limit is about time; the uncoupled limit is about geometry.** Skew limits come from rise time or UI. Ritchey: tolerance = receiver edge time × velocity, e.g. a 60 ps edge gives ±150 mil, and LVDS 400 ps gives ±1.2 in [RS]. Peterson: the window is "a bit less than the rise time", minus jitter allowance [A2]. TI/MIPI: UI/50 [TI1]. A rise-time rule of thumb of 1/10 tr appears only in forum-grade sources.
- **Uncoupled length is budgeted as fixed per-end allowances, never as a fraction of total length.** Sourced numbers: 45 mil pad entry [N1], 50 mil without a reference plane [N1], 250 mil breakout [R1], 15 mm asynchronous run [T]. No source budgets uncoupled length as a fraction of total length or of wavelength.
- **KiCad.** `diff_pair_uncoupled (max …)` has **no default**. A pair counts as uncoupled where its tracks "are not parallel or if they are outside the range set by a diff_pair_gap constraint". The default severity is Error, and nothing is checked without a custom rule [K1][K2]. KiCad's diff-pair *length* tuner works only on coupled portions, and its *skew* tuner "will add length to the shorter member" [K2]. Tuning-pattern geometry (min/max amplitude, spacing, corner radius, single-sided) has per-type defaults under Board Setup → Length-tuning Patterns, with no recommended values given [K2]. Tuning patterns are excluded from the uncoupled total (see Headline 1) [K3].

## 4. Intra-pair versus group matching

| | Intra-pair (P vs N) | Inter-pair / group |
|---|---|---|
| Tolerance scale | ~1–2 ps (0.13–0.3 mm), USB2 ~1.1–1.3 mm | DDR4 DQ–DQS ±10 ps / ±58 mil [X1] or ±3.5 ps [IN2]; LPDDR4 DQ ±50 ps vs DQS, DQS ±75 ps vs CK [N2]; MIPI clock-to-data 0.1 UI = 40 ps [TI1], 0.075 UI [X2], 10 ps ≈ 1.5 mm [T]; HDMI 0.20·Tchar [TI2], 150 ps ≈ 22 mm [T]; PCIe 550 mil total [TI1], 1.6 ns ≈ 240 mm [T] or none [TI2] |
| Pattern | Small bumps / sawtooth on the **short leg only**, at the mismatch [A1][N1][T] | Accordion/trombone on **both legs together**, keeping the pair coupled. KiCad diff-pair length tuning is coupled-only [K2]. Altium/Peterson: put inter-pair tuning symmetrically along the pair [A2, snip]. Watch self-crosstalk between accordion folds [A1] |
| Where | Local, per via-segment | Anywhere convenient; usually away from breakouts |
| Gotcha | Over-small bumps under-compensate [S1] | Don't over-match embedded-clock links (PCIe, SATA, USB3): "additional meander just introduces other signal quality problems" [T] |

## Recommended defaults for our net classes

**Skew (`skew … within_diff_pairs`) defaults**

| Class | Max skew | Basis |
|---|---|---|
| USB2 | 1.0 mm | Below TI's 1.27 mm [TI1] and Toradex's 1.1 mm [T]. *Judgement: rounded down* |
| USB3 / PCIe / HDMI / SATA | 0.127 mm (5 mil) | [TI1][N1][R1]; Toradex 0.15 mm [T] |
| MIPI D-PHY | 0.15 mm | Toradex [T]; AMD ±2 ps incl. package [X2] |
| DDR4/LPDDR4 DQS, CK | 0.15 mm (~1 ps) | Intel <1 ps [IN2]; NXP 1 ps [N2]. *Judgement: length proxy for a time rule, ignores package delay* |

**Uncoupled-length rule (replaces "may not grow")**

1. **Exclude skew-compensation bumps from the uncoupled metric,** matching KiCad's own fix [K3]. Gate the bumps with geometry rules instead:
   - excursion < 1 × nominal gap, strictly (S1 < 2S, with S1 the spacing inside the bump) [N1][T]. *Interim (operator ruling Q35 on #7521): kct currently allows up to 2 × the gap, because at < 1 × each bump adds too little length to close most skews. Issue #7649 tracks source-side skew avoidance and the return to < 1 ×.*
   - each jog ≥ 3 × W [N1][T];
   - the bump lies within 15 mm of the mismatch source and in the same via-delimited segment [T][R1];
   - the result reduces |skew| to within the class limit.

   For kicad-cli DRC to agree, the bumps must either be emitted as KiCad tuning patterns or be covered by a custom rule. *Judgement: whether to emit generator objects is an implementation choice.*

   *Update (operator ruling Q32 on #7521):* black-box probes on pinned kicad-cli 10.0.6 (`kicad-tuning-pattern-uncoupled-exclusion.md`) showed that a tuning-pattern bump leaves the partner copper beside it counted as uncoupled in full, while a 45°-chamfered plain-copper bump reads only its two legs (≈ 2.83 · h). kct therefore emits skew bumps as chamfered plain copper, not tuning patterns, and does not relabel partner copper as pattern members.

   *Update (#7546):* only the **crown** chamfers (leg to flat top) are needed. Probes on pinned kicad-cli 10.0.6 show the flat top couples to the partner once those corners carry a 45° chamfer of at least half a trace width. A **foot** chamfer (host line to leg) only leaves the partner copper beside it uncoupled, by the chamfer's own length. With square feet a loop costs the partner two crown chamfers instead of four chamfers: on a 0.2 mm pair at skew 0.5 mm the figure is 0.72 mm, against 0.94 mm with four chamfered corners. kct's tuner falls back to square feet when its four-corner ladder cannot close a pair.
2. **Breakout allowance:** per pair end, ≤ 1.14 mm (45 mil) per trace for PCIe-class nets [N1], so ≤ 2.3 mm per trace for two ends. Combined P+N, as KiCad sums it, that is about 4.6 mm. *Judgement: doubling AN10373's per-ball figure for two ends and two legs is my extrapolation.* This is looser than our measured 0.57–0.85 mm, so those values are not spec-derived. Keep them as regression baselines, not as limits.
3. **Ceiling:** total asynchronous run ≤ 15 mm [T]; Renesas caps breakout regions at 6.35 mm [R1].
4. **MIPI:** use the same rule. TI's note that MIPI pairs are loosely coupled [TI1] means bumps disturb impedance less.
5. **Not sourced:** any fraction-of-length or wavelength-based uncoupled rule, a bump-count limit, and the 1/10-rise-time skew rule. Treat these as judgement calls if they are adopted.

## Sources

- [TI1] TI SPRAAR7J, *High-Speed Interface Layout Guidelines* (2023) — https://www.ti.com/lit/an/spraar7j/spraar7j.pdf [read]
- [TI2] TI SLLA414A, *High-Speed Layout Guidelines for Signal Conditioners and USB Hubs* (2026) — https://www.ti.com/lit/an/slla414/slla414.pdf [read]
- [TI3] TI SWCA124 (USB 2.0, 150 mil) — https://ti.com/lit/pdf/SWCA124 [snip]
- [N1] Philips/NXP AN10373, *PCI Express PHY PCB Layout Guideline* (2005) — https://community.nxp.com/pwmxy87654/attachments/pwmxy87654/powerquicc/2284/2/philips_PCIe_pcb_routing_guide.pdf [read]
- [N2] NXP IMX8MPHDG, *i.MX 8M Plus Hardware Developer's Guide* Rev. 1 — https://community.nxp.com/pwmxy87654/attachments/pwmxy87654/imx-processors/229659/1/IMX8MPHDG.pdf [snip]
- [R1] Renesas (IDT) *Tsi381 Board Design Guidelines* — https://www.renesas.com/en/document/gde/tsi381-board-design-guidelines [read]
- [T] Toradex *Layout Design Guide* v1.0 (2015) — https://docs.toradex.com/102492-layout-design-guide.pdf [read]
- [X1] Xilinx UG583 v1.13, *UltraScale Architecture PCB Design* (2018) — https://0x04.net/~mwk/xidocs/ug/ug583-ultrascale-pcb-design.pdf (mirror) [read]
- [X2] AMD UG863, Versal PCB design, MIPI D-PHY — https://docs.amd.com/r/en-US/ug863-versal-pcb-design/MIPI-D-PHY [read]
- [X3] AMD UG863, MIPI C-PHY — https://docs.amd.com/r/en-US/ug863-versal-pcb-design/MIPI-C-PHY [read]
- [IN1] Intel/Altera Agilex PCIe 3.0 and F/E/P-tile guidelines (±1 mil) — https://www.intel.com/content/www/us/en/docs/programmable/853726/current/general-guidelines-for-3-0-interface.html , https://www.intel.com/content/www/us/en/docs/programmable/683864/current/f-tile-pcb-design-guidelines.html [snip]
- [IN2] Intel Agilex 7 M-Series EMIF, DDR4 discrete skew matching — https://intel.com/content/www/us/en/docs/programmable/772538/23-3-5-0-0/skew-matching-guidelines-for-ddr4-discrete.html [snip]
- [A1] Altium Designer docs, *Length Tuning* — https://www.altium.com/documentation/altium-designer/pcb/high-speed-design/length-tuning [read]
- [A2] Altium Resources (Z. Peterson), *Differential Pair Length Matching: Best Practices* — https://resources.altium.com/p/differential-pair-length-matching-best-practices-signal-integrity [read]
- [C1] Cadence PCB blog, *Inter-pair skew* (2023) — https://resources.pcb.cadence.com/blog/2023-inter-pair-skew [snip]
- [S1] Kunz, Novak et al. (Oracle), *Sources and Compensation of Skew in Single-Ended and Differential Interconnects*, SI Journal 2017 — https://www.signalintegrityjournal.com/ext/resources/article-images-2017/4120/4120_SIJ417_Skew_IN1.pdf [read]
- [RS] L. Ritchey, *A Treatment of Differential Signaling and Its Design Requirements*, Speeding Edge 2008 — https://www.speedingedge.com/PDF-Files/DiffSigDesign.pdf [read]
- [P1] Proto Express / Sierra Circuits, *How to Route Differential Pairs in KiCad* (±5 mil example, 3W serpentine spacing) — https://www.protoexpress.com/blog/how-to-route-differential-pairs-in-kicad/ [read]
- [K1] KiCad 9.0 PCB Editor manual (DRC: `diff_pair_uncoupled`, `skew`, `diff_pair_gap`) — https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html [read]
- [K2] KiCad 10.0 PCB Editor manual (custom rules, tuning tools and patterns) — https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html [read]
- [K3] KiCad forum, *How do the "diff_pair_*" constraints work?* (June 2026; commit 7afb67fb) — https://forum.kicad.info/t/how-do-the-diff-pair-constraints-work/69785 [read]
- [I1] IPC-2141A table of contents — https://www.ipc.org/TOC/IPC-2141A.pdf [snip]

Not found: a Lattice layout note with P/N skew numbers, and any USB-IF, PCI-SIG or HDMI spec text giving PCB uncoupled length. The spec-level numbers above reach us only through vendor app notes.
