# Intra-pair skew limits from a differential-phase tolerance

Research note and proposal for Issue #7883. Compiled 2026-10-23 against `main` @ `c162396a8`.

**Status (2026-10-23):** ruled on #7883 (issuecomment-6370363844): Q-a −30 dB, Q-b knee, Q-c max rate = warning, Q-d derivation wins, Q-e time domain, Q-f DDR 2 ps. Step 1 (#7892) implements the budget in `src/kicad_tools/physics/skew_budget.py` and the time-domain `kct check`. The router and tuners follow in #7893, and the generator limits are removed in #7894.

**This note changes no limit.** It derives a physics-based skew limit, proposes a tolerance and
shows what that tolerance would do to every diff pair on boards 03, 06 (with the 06rf bench
fixture) and 07. The implementation follows in a separate PR, after an operator ruling on the
open questions in §8.

Each source is tagged **[read]** (I read the primary text for this note), **[via Q16]** (the
number is from `diffpair-skew-compensation.md` and I did not re-read the source) or
**[unverified]** (recalled or secondary; treat it as unconfirmed). Every other number was
computed here, either by `scripts/research/diffpair_phase_skew_table.py` with the repo's own
`ε_eff` model or by the stated arithmetic. `tests/test_diffpair_phase_skew_table.py` pins the
identities the note uses.

## Headline findings

1. **The framing holds.** A length mismatch ΔL delays one leg by Δt = ΔL·√ε_eff / c. At
   frequency f that is a phase difference Δφ = 2πfΔt. The pair keeps |Sdd21| = cos(Δφ/2) of its
   differential signal and converts |Scd21| = sin(Δφ/2) of it to common mode (§1). Three
   independent sources check out against this. Oracle measured differential insertion-loss nulls
   at f = 1/(2Δt), where cos(Δφ/2) = 0 [S1]. Xilinx UG583 states a P/N mismatch as a phase at
   Nyquist: 16 mil gives 4.8° at 5 GHz, 2.68 ps [X1]. TI says MIPI D-PHY intra-pair skew is
   *defined* by the mode-conversion S-parameters [TI1].
2. **Mode conversion always binds; differential loss never does.** Loss is second order in Δφ and
   conversion is first order. At a −30 dB conversion limit the differential loss is 0.004 dB. A
   1 dB loss budget would allow Δφ = 54°, which is −6.9 dB of conversion. The tolerance must
   therefore be stated as mode conversion, not as S/N loss.
3. **Evaluate at the Nyquist frequency** (bit rate / 2). Raise it to the edge knee
   0.35 / t_r,min only when the protocol specifies a minimum edge time and that knee lies above
   Nyquist. Of the pairs here, that applies to USB 2.0 FS and HS and to the LVDS driver. Do not
   use the 3rd harmonic (§2).
4. **Proposed tolerance: |Scd21| ≤ −30 dB at f_eval.** That is Δφ ≤ 3.62°, and at Nyquist it is
   exactly Δt ≤ UI/50, TI's published estimate for MIPI [TI1]. The stricter alternative is −36 dB
   (Δφ ≤ 1.82°, UI/100), which keeps half the budget for skew that routing cannot see: fibre
   weave, package and connector.
5. **At −30 dB every pair on these boards loosens in millimetres; none tightens.** The factors
   run from ×1.16 (06rf USB3, 0.5 → 0.58 mm) to ×24 (board 06 LVDS, 0.1 → 2.41 mm). At −36 dB,
   06rf USB3 tightens (0.5 → 0.29 mm) and PCIe Gen1 roughly holds (0.5 → 0.58 mm). DDR strobes
   are a timing quantity, not an S/N one, and keep a timing limit: UG583's 2 ps, which is
   0.29 mm (§5). Board 07 as committed (the SDRAM exerciser) has **no diff pairs**. Its pairs are
   in the generated 07rf regression fixture.
6. **The bigger change is the unit, not the number.** A time-based limit checks time, and the
   committed 06rf copper shows why that matters. `USB3_TX1` is matched to 0.015 mm in length, so
   it passes today, but one leg runs 23.6 mm on In2.Cu while the other stays on F.Cu. With the
   repo's delays (5.66 ps/mm outer, 6.94 ps/mm inner) that is **27.5 ps of skew, 6.9× the 4 ps
   budget**. A length-only check cannot see layer-dependent velocity (§5.3). Moving to time is the
   one place where the proposal *tightens*.
7. **None of today's numbers came from a phase or time budget** (§4). The generic 0.5 mm is the
   old `match_tolerance = 0.5` length-matching default, carried into Phase 3H (#2647/#2649) as "a
   sane middle ground". 1.0 mm (USB2_HS) is the Q16 judgement value, rounded down from TI's
   1.27 mm and Toradex's 1.1 mm. The 06rf (0.5/0.5/0.3 mm), board 06 (0.1 mm) and 07rf
   (0.05/0.075 mm) values are explicit class fields that override the Q16 protocol table. **The
   "2× allowance" (#7649, Q35) is not a skew allowance.** It caps a skew bump's *excursion* at
   2× the pair gap (`MAX_EXCURSION_GAPS = 2.0`), and a derived limit leaves it alone (§6).

## 1. Derivation, and a check of the framing

**Telegrapher's equations.** For a lossless line with inductance L and capacitance C per unit
length, ∂V/∂z = −L ∂I/∂t and ∂I/∂z = −C ∂V/∂t. Together they give the wave equation, with
propagation constant β = ω√(LC). For a TEM or quasi-TEM line, LC = ε_eff/c², so the phase
velocity is v_p = ω/β = c/√ε_eff. Over an extra length ΔL:

    Δt = ΔL / v_p = ΔL · √ε_eff / c          Δφ = β · ΔL = 2π f Δt

Use c = 0.2998 mm/ps. For ε_eff = 3.0, v_p = 173 mm/ns (5.78 ps/mm). The issue's worked example
reproduces exactly: 0.5 mm → 2.89 ps → 2.60° at 2.5 GHz → −32.9 dB of conversion and 0.0022 dB of
loss.

**Mixed-mode decomposition.** Drive P with +V/2 and N with −V/2 at e^{jωt}, and let N arrive Δφ
late. With V_d = V_P − V_N and V_c = (V_P + V_N)/2:

    V_d = V/2 · e^{-jφ0} (1 + e^{-jΔφ}) = V · cos(Δφ/2) · e^{-j(φ0+Δφ/2)}
    V_c = V/4 · e^{-jφ0} (1 − e^{-jΔφ}) = j V/2 · sin(Δφ/2) · e^{-j(φ0+Δφ/2)}

Mixed-mode waves are normalised to the reference impedances Z_d = 2Z0 and Z_c = Z0/2. The
convention is the standard mixed-mode S-parameter one (Bockelman & Eisenstadt, 1995)
**[unverified: not re-read]**; the algebra below does not depend on the citation. With that
normalisation, |Scd21| / |Sdd21| = 2|V_c| / |V_d| = tan(Δφ/2). That gives:

    |Sdd21| = cos(Δφ/2)      differential S/N loss = −20 log10 cos(Δφ/2) dB
    |Scd21| = sin(Δφ/2)      mode conversion       =  20 log10 sin(Δφ/2) dB

The two add in power (cos² + sin² = 1), as a lossless reciprocal pair must. The framing in the
issue is right.

**Independent checks.**

- *Insertion-loss nulls.* cos(Δφ/2) = 0 at Δφ = π, that is at f = 1/(2Δt). Oracle measured skew
  "resonances at odd harmonics … calculated from the difference in electrical length". 9 ps/in
  over 10 in gave a null at "the 1st harmonic of 5.5 GHz" [S1, read]. 1/(2 × 90 ps) = 5.56 GHz.
- *Phase at Nyquist is how vendors already state it.* UG583 says "the 16 mil difference
  translates to a phase mismatch of 4.8° at 5 GHz, or 2.68 ps (0.0268 UI) at 10 Gb/s" [X1, read].
  2π × 5 GHz × 2.68 ps = 4.82°.
- *Mode conversion is the defining quantity.* TI's AM62x table gives the CSI differential-pair
  skew as "Must satisfy mode-conversion S-parameters", with footnote "Defined in MIPI D-PHY spec;
  includes sdc12, scd21, … General estimate is UI/50 (where UI = 400 ps for 1.25 GHz)"
  [TI1, read]. TI's LVDS Owner's Manual: "Intra-pair skew introduces common mode to
  differential-mode conversion, which increases the signal loss at higher frequencies"
  [LV2, read].

**Which effect binds.** For small Δφ, loss ≈ 4.34·(Δφ/2)² dB and conversion ≈ 20 log10(Δφ/2).
Conversion is first order and dominates by orders of magnitude:

| Δφ | Differential loss | Mode conversion |
|---|---|---|
| 1.82° (−36 dB tolerance) | 0.0011 dB | −36.0 dB |
| 3.62° (−30 dB tolerance) | 0.0043 dB | −30.0 dB |
| 7.24° (−24 dB) | 0.017 dB | −24.0 dB |
| 54° (a 1 dB loss budget) | 1.0 dB | −6.9 dB |

A dB budget on S/N loss would allow skews that dump about half the differential amplitude into
common mode. The issue's guess is confirmed: "mode conversion … binds first".

**What the model leaves out** (each is an assumption of the table):

- *Lossless, matched, lumped.* The mismatch is treated as one Δt at the receiver. Oracle shows
  that in **microstrip** *where* the compensation sits changes the conversion, while in
  stripline it barely matters [S1]. The Q16 locality gate (§6) covers this; the phase model does
  not.
- *Which ε_eff.* The extra length mostly sits in uncoupled copper (bumps, fan-outs), so the table
  uses the single-ended ε_eff. The coupled odd/even values bracket it: 06rf USB3 has odd 2.40,
  single-ended 2.88 and even 3.12, which is about ±10 % in delay. The script's `--json` output
  carries all three.
- *Skew that routing cannot see.* Fibre weave [TI1], package and connector skew all use the same
  phase budget. The −36 dB option sets half the budget aside for them.

**Time-domain view.** For a 0–100 % ramp of duration T on each leg, a delay Δt < T produces a
common-mode pulse of peak (Δt / 2T) × the per-leg swing. It also slows the differential edge from
T to T + Δt, while the crossing point moves by Δt/2 as a pure delay. Both effects grow linearly
with Δt/T, which is the time-domain form of the sin(Δφ/2) term at the edge frequency.

## 2. Which frequency to evaluate

Conversion grows with f, so the tolerance has to be met at the highest frequency at which the
signal still matters. There were three candidates:

- **Nyquist, f_N = R/2.** This is the fundamental of the 1010 pattern. Equalised serial links are
  specified and equalised up to about Nyquist. Peterson notes that a 224G PAM4 link needs 56 GHz
  of channel bandwidth (its Nyquist), not the knee-formula estimate [A3, read]. The vendor tables
  list Nyquist as each interface's "Frequency": USB HS "240 MHz (480 Mbps)", PCIe Gen1
  "1.25 GHz (2.5 GT/s)" and so on [T, read][TI2, read]. There is also an exact identity:
  **Δt = k·UI ⇔ Δφ(f_N) = k·180°**. So every UI-denominated skew spec is already a Nyquist-phase
  spec. UI/50 is 3.6°, and HDMI's 0.15·T_bit is 27°.
- **Knee, f_k = 0.35 / t_r.** 0.35 is ln 9 / 2π, the −3 dB bandwidth of a single-pole response
  with 10–90 % rise time t_r. Johnson & Graham's 0.5/t_r and the trapezoid breakpoint
  1/(π t_r) = 0.32/t_r are the same idea with different constants. Peterson confirms both 0.35
  and 0.5 prefactors are in use [A3, read]; the Johnson & Graham attribution is **[unverified]**.
- **3rd harmonic, 3·f_N.** It is not tied to the edge. For slow edges it carries little energy;
  for fast edges the knee already lies above it. It adds nothing over the other two.

**Choice.** f_eval = max(f_N, 0.35 / t_r,min), where t_r,min is the fastest edge the protocol or
part *permits*.

- *Why Nyquist is the floor.* A periodic trapezoid's line spectrum has an envelope that falls at
  −20 dB/decade from about 0.64·f_N up to 1/(π t_r), then at −40 dB/decade. The Fourier
  coefficients are A(τ/T₀)·sinc(nπτ/T₀)·sinc(nπ t_r/T₀). The converted common mode,
  envelope × sin(πfΔt) ≈ envelope × πfΔt, is therefore **flat** from Nyquist up to the knee. Its
  per-line peak is already reached at Nyquist. Evaluating Scd21 at Nyquist bounds the largest
  common-mode spectral line, which is also what per-frequency EMI limits measure. *(The trapezoid
  envelope is derived here, not cited.)*
- *Why the knee raises it when an edge floor exists.* Above Nyquist, |Scd21| keeps rising up to
  the knee while the signal still has significant content. A *ratio* tolerance (Scd21, as in
  D-PHY's mixed-mode masks) has to hold over that whole band. Radiated emission from common-mode
  current on an electrically short cable also grows with f **[unverified: textbook result
  (C. Paul), not re-read]**. This matters for slow-data, fast-edge links: USB 2.0 HS (480 Mb/s
  with edges ≥ 500 ps, so the knee is 0.70 GHz against 0.24 GHz Nyquist) and FS (12 Mb/s with
  edges ≥ 4 ns: 87 MHz against 6 MHz) [USB2, read]. For SerDes protocols that state no minimum
  edge (USB3, PCIe, D-PHY HS, HDMI), the protocol's own budget is in UI and the knee is not
  needed.
- *Using the edge floor, not the typical edge,* keeps the rule conservative. SN65LVDS1 gives no
  minimum, only typ 0.6 ns / max 1 ns [LV1, read]. The table uses the typical 0.6 ns and flags
  it.

## 3. Published skews, translated into the same phase

Each published limit is converted at the frequency its own source uses (Nyquist unless noted).
Δφ = 360°·f·Δt, and the conversion is 20 log10 sin(Δφ/2).

| Interface | Published limit | Scope | Δφ | Conversion | Source |
|---|---|---|---|---|---|
| USB 2.0 | cable D+/D− skew < 100 ps (T_SKEW) | cable | 8.6° at 240 MHz (25° at 0.70 GHz knee) | −22.5 dB (−13.2 dB) | [USB2, read] §7.1.3 |
| USB 2.0 HS | 50 mil | PCB | 2.1° at knee | −34.6 dB | [TI1, read] App. A |
| USB 2.0 HS | < 7.5 ps ≈ 1.1 mm | PCB | 1.9° at knee | −35.7 dB | [T, read] §7.4 |
| USB 3.0 | mated internal cable assembly < 15 ps | cable | 13.5° at 2.5 GHz | −18.6 dB | [USB3C, read] §2.2.2.4 |
| USB 3.x | "15 ps/m (TI recommends 5 mils)" | cable / PCB | 5 mil: 0.76° | −43.5 dB | [TI2, read] §2.2 |
| USB 3.0 | < 1 ps ≈ 150 µm | PCB | 0.9° | −42.1 dB | [T, read] |
| PCIe Gen1 / Gen3 | < 1 ps | PCB | 0.45° / 1.44° | −48.1 / −38.0 dB | [T, read] §7.1 |
| PCIe / USB3 / SATA / HDMI | 5 mil | PCB | 0.4–0.9° (1.25–3 GHz) | −42 to −50 dB | [TI1, read] |
| MIPI D-PHY | defined by mode-conversion S-params; estimate UI/50 | link | 3.6° | **−30.1 dB** | [TI1, read] |
| MIPI D-PHY | ±2 ps incl. package | PCB + package | 0.9° at 1.25 GHz | −42.1 dB | [X2, via Q16] |
| MIPI DSI/CSI | < 1 ps | PCB | 0.18° at 500 MHz | −56 dB | [T, read] §7.12–7.13 |
| HDMI / TMDS | source intra-pair 0.15·T_bit | source total | 27° | −12.6 dB | [TI2, read] §2.3 |
| HDMI / DVI | < 5 ps ≈ 0.75 mm | PCB | 3.0° at 1.65 GHz | −31.7 dB | [T, read] §7.7 |
| DDR4 DQS / CK | 2 ps (11 mil), T vs C | PCB | 1.15° at 1.6 GHz | −40 dB | [X1, read] |
| 10 Gb/s SerDes | 16 mil bend mismatch, worth fixing | PCB | 4.8° | −27.5 dB | [X1, read] |
| LVDS | < 1 ps | PCB | — | — | [T, read] §7.6 |

What this shows:

- **Channel and cable specs are loose:** −13 to −23 dB. They are whole-channel budgets, and the
  PCB is one contributor.
- **Vendor PCB rules are tight and do not depend on rate:** −35 to −56 dB. Toradex applies "1 ps"
  from 62.5 MHz Ethernet (0.04°) to 4 GHz PCIe (1.4°). TI applies "5 mil" to USB3, SATA, PCIe and
  HDMI alike. These are blanket margins, not phase-derived numbers. That is why the 2×-gap bump
  interim and the 0.127 mm Q16 values feel arbitrary against the physics.
- **The proposed −30 dB sits between the two.** It equals the one vendor number that *is*
  phase-derived (TI/MIPI UI/50), and it leaves the PCB a fraction of each cable budget (USB2
  100 ps, USB3 15 ps).

Not found: PCI-SIG, USB-IF (base PCB), HDMI or JEDEC spec text giving a PCB intra-pair number.
The spec-level numbers here reach us through vendor app notes, except USB 2.0's cable skew and
edge times, which come from the specification itself.

## 4. Where today's limits came from

| Limit | Applies to | Origin |
|---|---|---|
| 0.5 mm generic (`GENERIC_SKEW_MM`, `DEFAULT_SKEW_TOLERANCE_MM`, `effective_skew_tolerance(default=0.5)`) | any pair whose class declares no protocol and no field | Issue #2647 (Phase 3H) took the existing length-matching default `match_tolerance=0.5` (`router/length.py`, from `81eeb666c`) and named it the skew default. PR #2662 (closing #2649) added the DRC constant, commented "a sane middle ground". The calibration figures #2647 gives (USB 2.0 HS ~3 mm, USB 3.0/PCIe ~0.5–1 mm, MIPI ~1 mm, DDR4 ~0.5 mm) carry no source. |
| 2.5 mm `USB2_FS` | board 03 USB_D, USB_MCU_D | Renesas full-speed USB board-design guide, Table 2 (maintainer decision on #6443) |
| 1.0 mm `USB2_HS` | 06rf USB2_D | Q16 (#7521, commit `1e45f5ef3`): "judgement: rounded down from TI 50 mil (1.27 mm) and Toradex 1.1 mm" |
| 0.127 / 0.15 mm (USB3/SATA/PCIe/HDMI, MIPI/DDR) | protocol table only | Q16: TI/NXP/Renesas 5 mil; Toradex/Intel/NXP ~1 ps. **No pair on these boards uses them**, because explicit class fields win. |
| 0.5 mm (USB3, PCIe) / 0.3 mm (MIPI) | 06rf | `boards/06-diffpair-test/generate_design.py` class fields, "Phase 3H: USB 3.0 budget" (#2658, `0cea7587f`). They predate Q16 and override it. |
| 0.1 mm | board 06 LVDS | `boards/06-diffpair-test/assembled-demo/generate.py` (`d95b6eff3`), uncommented; it overrides the uncited LVDS table value of 0.5 mm |
| 0.05 / 0.075 / 0.05 mm (MIPI, HDMI, DQS) | 07rf | `boards/07-matchgroup-test/generate_design.py`, "Phase 3H: tight … budget" |
| 2× (Q35) | bump geometry, not skew | `diffpair_bump_gates.MAX_EXCURSION_GAPS = 2.0`: a skew bump may push out to 2× the nominal gap. AN10373's S1 < 2S puts it below 1× (Q27). Q35 made 2× the interim because a < 1× bump adds too little length; #7649 tracked the return to 1× and was closed with 2× kept. |

The issue cites #7798 for the "one helper" requirement. #7798 is the stale board 07 output; the
skew-limit dedupe is **#7797** (`pair_postpass_invariant.checker_skew_limit_mm`, used by
`DiffPairRouter._checker_skew_limit_mm` and the post-pass guard). §7 builds on that one.

## 5. The proposed limit table

### 5.1 Per protocol

`uv run python scripts/research/diffpair_phase_skew_table.py --protocols`. Millimetres use the
repo's legacy JLCPCB four-layer model (`Stackup.jlcpcb_4layer()`): F.Cu microstrip at
w = 0.2 mm is 5.57 ps/mm, and the slowest inner dielectric is 6.94 ps/mm. "Inner" is the
conservative layer-agnostic conversion a millimetre rule needs (§5.3).

| Protocol | Rate | t_r floor | f_eval, GHz (basis) | −30 dB: Δφ / Δt | mm outer / inner | −36 dB: Δt / mm inner | Today's table value |
|---|---|---|---|---|---|---|---|
| USB2_FS | 12 Mb/s | 4000 ps [USB2] | 0.087 (knee) | 3.62° / 115.2 ps | 20.66 / 16.60 | 57.7 ps / 8.32 | 2.5 mm |
| USB2_HS | 480 Mb/s | 500 ps [USB2] | 0.699 (knee) | 3.62° / 14.4 ps | 2.58 / 2.08 | 7.2 ps / 1.04 | 1 mm |
| USB3_SS Gen1 | 5 Gb/s | - | 2.500 (Nyquist) | 3.62° / 4.0 ps | 0.72 / 0.58 | 2.0 ps / 0.29 | 0.127 mm |
| USB3 Gen2 (what-if) | 10 Gb/s | - | 5.000 (Nyquist) | 3.62° / 2.0 ps | 0.36 / 0.29 | 1.0 ps / 0.15 | - |
| PCIE_G1 | 2.5 Gb/s | - | 1.250 (Nyquist) | 3.62° / 8.1 ps | 1.45 / 1.16 | 4.0 ps / 0.58 | 0.127 mm |
| PCIe Gen2 (what-if) | 5 Gb/s | - | 2.500 (Nyquist) | 3.62° / 4.0 ps | 0.72 / 0.58 | 2.0 ps / 0.29 | 0.127 mm |
| PCIe Gen3 (what-if) | 8 Gb/s | - | 4.000 (Nyquist) | 3.62° / 2.5 ps | 0.45 / 0.36 | 1.3 ps / 0.18 | 0.127 mm |
| MIPI_DPHY 1 Gb/s | 1 Gb/s | - | 0.500 (Nyquist) | 3.62° / 20.1 ps | 3.61 / 2.90 | 10.1 ps / 1.45 | 0.15 mm |
| MIPI_DPHY 2.5 Gb/s | 2.5 Gb/s | - | 1.250 (Nyquist) | 3.62° / 8.1 ps | 1.45 / 1.16 | 4.0 ps / 0.58 | 0.15 mm |
| HDMI_TMDS 1.4b | 3.4 Gb/s | - | 1.700 (Nyquist) | 3.62° / 5.9 ps | 1.06 / 0.85 | 3.0 ps / 0.43 | 0.127 mm |
| HDMI 2.0 (what-if) | 6 Gb/s | - | 3.000 (Nyquist) | 3.62° / 3.4 ps | 0.60 / 0.48 | 1.7 ps / 0.24 | 0.127 mm |
| LVDS 400 Mb/s | 400 Mb/s | 600 ps typ [LV1] | 0.583 (knee) | 3.62° / 17.3 ps | 3.10 / 2.49 | 8.7 ps / 1.25 | 0.5 mm (uncited) |
| DDR_DQS_CK DDR4-3200 | 3.2 Gb/s | - | 1.600 (Nyquist) | 3.62° / 6.3 ps | 1.13 / 0.91; **timing-limited: 2 ps = 0.29 mm** | 3.2 ps / 0.45 | 0.15 mm |

The rule in one line: **Δt_max = 2·asin(10^(T/20)) / (2π·f_eval)**, with T = −30 dB and
f_eval = max(R/2, 0.35/t_r,min). At Nyquist that simplifies to **Δt_max = UI/50**. The time limit
scales as 1/rate. The tolerance moves it by ×2 per 6 dB.

**DDR strobes and clocks are the exception.** A skew on DQS_T/DQS_C or CK_T/CK_C moves the
differential crossing point by Δt/2, and that crossing *is* the timing reference for DQ capture.
The binding budget is the DQ-to-DQS window (UG583: ±5 ps DQ to DQS, 2 ps T to C [X1, read]), not
S/N. The proposal keeps a sourced timing limit for this class and does not derive one.

### 5.2 Per pair, boards 03 / 06 / 06rf / 07

`uv run python scripts/research/diffpair_phase_skew_table.py`. The ε_eff is each board's own
stackup: board 03, board 06 and 06rf parse their `.kicad_pcb`, and 07rf uses
`Stackup.jlcpcb_4layer()`, which is what its own impedance sizing and `Stackup.from_pcb` use for
an unstacked four-layer board. Outer layers use the class trace width; the slowest inner layer is
the surrounding dielectric's ε_r, since a stripline is TEM. "Today" is the limit
`resolve_pair_thresholds` returns, with its own phase and conversion at f_eval. "Proposed" is the
−30 dB time limit at the slowest layer, or the timing limit for DQS.

| Board | Pair | Class (protocol) | Rate | f_eval, GHz (basis) | ps/mm outer / inner | Today, mm (at f_eval) | −30 dB: ps | mm outer / slowest | −36 dB: mm | Proposed, mm | Change |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 03 | USB_D+/USB_D- | HighSpeed (USB2_FS) | 12 Mb/s | 0.087 (knee) | 5.88 / 7.15 | 2.5 (0.56°, −46.2 dB) | 115.2 | 19.58 / 16.10 | 8.07 | **16.10** | loosen ×6.44 |
| 03 | USB_MCU_D+/USB_MCU_D- | HighSpeed (USB2_FS) | 12 Mb/s | 0.087 (knee) | 5.95 / 7.15 | 2.5 (0.56°, −46.2 dB) | 115.2 | 19.37 / 16.10 | 8.07 | **16.10** | loosen ×6.44 |
| 06 | LVDS1..4_P/N (4 pairs) | LVDS (LVDS) | 400 Mb/s | 0.583 (knee) | 5.96 / 7.15 | 0.1 (0.15°, −57.7 dB) | 17.3 | 2.90 / 2.41 | 1.21 | **2.41** | loosen ×24.1 |
| 06rf | MIPI_CLK, MIPI_D0 | MIPI (MIPI_DPHY) | 1 Gb/s | 0.500 (Nyquist) | 5.60 / 6.94 | 0.3 (0.37°, −49.7 dB) | 20.1 | 3.59 / 2.90 | 1.45 | **2.90** | loosen ×9.68 |
| 06rf | PCIE_RX, PCIE_TX | PCIe (PCIE_G1) | 2.5 Gb/s | 1.250 (Nyquist) | 5.60 / 6.94 | 0.5 (1.56°, −37.3 dB) | 8.1 | 1.44 / 1.16 | 0.58 | **1.16** | loosen ×2.32 |
| 06rf | USB2_D | USB2 (USB2_HS) | 480 Mb/s | 0.699 (knee) | 5.63 / 6.94 | 1.0 (1.75°, −36.3 dB) | 14.4 | 2.56 / 2.08 | 1.04 | **2.08** | loosen ×2.08 |
| 06rf | USB3_RX1, RX2, TX1, TX2 | USB3 (USB3_SS) | 5 Gb/s | 2.500 (Nyquist) | 5.66 / 6.94 | 0.5 (3.12°, −31.3 dB) | 4.0 | 0.71 / 0.58 | 0.29 | **0.58** | loosen ×1.16 |
| 07rf | DQS_P/DQS_N | DDR_DQS | 3.2 Gb/s (assumed) | 1.600 (Nyquist) | 5.60 / 6.94 | 0.05 (0.20°, −55.2 dB) | 6.3 | 1.12 / 0.91 | 0.45 | **0.29** (timing, 2 ps) | loosen ×5.77 |
| 07rf | MIPI_CLK, DAT0, DAT1 | MIPI_CSI_LANES | 2.5 Gb/s (assumed) | 1.250 (Nyquist) | 5.60 / 6.94 | 0.05 (0.16°, −57.3 dB) | 8.1 | 1.44 / 1.16 | 0.58 | **1.16** | loosen ×23.2 |
| 07rf | TMDS_D0, D1, D2 | HDMI_TMDS_LANES | 3.4 Gb/s (assumed) | 1.700 (Nyquist) | 5.60 / 6.94 | 0.075 (0.32°, −51.1 dB) | 5.9 | 1.06 / 0.85 | 0.43 | **0.85** | loosen ×11.4 |
| 07 | — | — | — | — | — | — | — | — | — | — | 0 diff pairs |

The script prints one row per pair; rows with identical class, width and stackup are merged here.
Rates: board 03 is USB 2.0 FS (Renesas row, 12 Mb/s). For board 06 the rate is the SN65LVDS1/2
link's 400 Mb/s [LV1]. The 06rf rates are from `boards/06-diffpair-test/regression-fixture/README.md`.
07rf is synthetic and declares no rate, so it is **assumed** to run at D-PHY 2.5 Gb/s (TI's
UI = 400 ps example), HDMI 1.4b at 3.4 Gb/s and DDR4-3200. Every 07rf row moves with that
assumption.

**What tightens, what loosens.**

- *Loosens at −30 dB, all 22 pairs, in mm.* Factors: board 06 LVDS ×24; 07rf MIPI ×23; 07rf HDMI
  ×11; 06rf MIPI ×9.7; board 03 USB2 FS ×6.4; 07rf DQS ×5.8 (timing); 06rf PCIe ×2.3; 06rf USB2
  ×2.1; 06rf USB3 ×1.16.
- *Tightens only under the stricter option or at higher rates.* At −36 dB, 06rf USB3 goes
  0.5 → 0.29 mm. PCIe Gen1 is about level (0.58 against 0.5). At −30 dB, USB 3.2 Gen2 at 10 Gb/s
  or PCIe Gen3 would be 0.29 / 0.36 mm, tighter than today's 0.5 mm 06rf class value.
- *Against the Q16 protocol table* (the values a class gets when it sets no explicit field), the
  derived limits are 4–19× looser: USB3 0.127 → 0.58 mm, MIPI 0.15 → 1.16–2.9 mm, HDMI
  0.127 → 0.85 mm.

### 5.3 Checking in time, not length: the one place it tightens

The committed `regression-fixture/diffpair_test_routed.kicad_pcb` is an older route
(`d95b6eff3`, 2026-09-10), not today's bench output, so its verdicts are not the bench's. It is
still useful, because its legs change layer. The script converts each leg to time: every
millimetre at the outer-layer delay, plus the extra inner-layer delay for track on In1/In2.Cu.

| 06rf pair | kct check skew (mm) | Same copper in time (ps) | Today (mm limit) | Proposed (−30 dB, ps) |
|---|---|---|---|---|
| USB3_TX1 | 0.015 | **27.5** (TX1+ 23.6 mm on In2.Cu, TX1− on F.Cu) | pass | **FAIL** (4.0) |
| USB3_RX1 | 1.810 | 68.0 | FAIL | FAIL (4.0) |
| USB3_RX2 | 2.196 | 16.6 | FAIL | FAIL (4.0) |
| USB3_TX2 | 2.790 | 15.8 | FAIL | FAIL (4.0) |
| USB2_D | 6.214 | 35.0 | FAIL | FAIL (14.4) |
| PCIE_RX / TX | 1.740 / 1.729 | 12.4 / 15.4 | FAIL | FAIL (8.1) |
| MIPI_CLK | 1.350 | 4.4 | FAIL | pass (20.1) |
| MIPI_D0 | 5.546 | 58.2 | FAIL | FAIL (20.1) |

Board 03's pairs (3.1 and 0.8 ps) and board 06's LVDS pairs (0 ps) pass both ways.

**F.Cu microstrip and inner-layer copper differ by about 23 % in delay here** (5.66 against
6.94 ps/mm). The difference follows from ε_eff and is not specific to the model: microstrip has
air above it. A pair whose legs spend different lengths on different layers carries a time skew
that length matching cannot see. `kct check`, the tuners and kicad-cli's `skew` rule all measure
millimetres today. So the move to a time limit is a real tightening for layer-asymmetric pairs,
even though every millimetre limit loosens.

Caveat: In1/In2.Cu on these boards are nominally planes, so ε_eff = ε_r of the adjacent
dielectric is an approximation for a track there. It still lands within the 4.05–4.6 range of
the materials either side.

Relation to the open failures. These are measured bench numbers from the issues, not re-run
here. Judged against the proposed millimetre limits at the slowest layer:

- **06rf USB2_D** (2.127 mm, #7869): 2.08 mm is marginal (fails by 0.05 mm). At the outer-layer
  conversion, 2.56 mm, it passes. A per-layer time check would decide it.
- **USB3_RX1** at the shipped 2× (0.45 mm after PR #7866) passes either way. At 1× (2.269 mm) it
  still fails against 0.58 mm.
- **USB3_RX2** (4.785 mm, #7867) fails either way.

A derived limit does not make #7864 or #7867 go away. It might close #7869's verdict without
#7869's fix, which is an argument for ruling before #7869 lands.

## 6. Interaction with the 2× allowance and the Q16 / Q32 bump rules

- **Q35 2× is a geometry cap, and a derived limit leaves it unchanged.** What changes is how many
  pairs need bumps at all. Where the derived limit exceeds a pair's pre-tune skew (USB2 FS, LVDS
  and MIPI at 1 Gb/s on these boards), the tuner has nothing to do and the cap never engages.
  USB3 at 0.58 mm still needs bumps. The 1× measurement in #7864 (2.269 mm) shows that 1× cannot
  reach even the derived limit. **The 2× interim does not become unnecessary.** The proposal adds
  no reason to retire it either, and #7649 already closed with 2× kept.
- **The signal-integrity cost of a 2× bump is small at these rates.** The two-conductor field
  solve (`CoupledLines.edge_coupled_microstrip`, `--bump-impedance`) gives the local Zdiff with
  the spacing opened by one or two gaps. This is a symmetric-spacing approximation of a one-sided
  push-out.

  | Board | Class | w / s (mm) | Zdiff at s | at 2s (1× bump) | at 3s (2× bump, Q35) |
  |---|---|---|---|---|---|
  | 03 | HighSpeed | 0.2 / 0.15 | 101.7 | 117.3 | 123.9 |
  | 06 | LVDS | 0.26 / 0.15 | 91.7 | 104.6 | 110.1 |
  | 06rf | MIPI, PCIe | 0.225 / 0.1 | 92.1 | 109.0 | 117.4 |
  | 06rf | USB2 | 0.25 / 0.075 | 82.0 | 98.0 | 106.7 |
  | 06rf | USB3 | 0.275 / 0.1 | 85.5 | 100.0 | 107.3 |
  | 07rf | DQS, MIPI, HDMI | 0.225 / 0.135 | 99.6 | 115.4 | 122.5 |

  A 2× bump raises Zdiff by about 25 %. Over 1 mm at USB3 Nyquist (5.1° electrical) its
  reflection is about |Γ0|·2 sin θ = 0.113 × 0.177, which is **−34 dB**; a 1× bump gives
  −37 dB. *(My arithmetic.)* That is electrically small, so it does not argue against keeping 2×.
  AN10373's S1 < 2S [N1, via Q16] remains the conservative rule.
- **Q16 bump gates are orthogonal and stay.** `MIN_JOG_WIDTHS = 3` (no sub-width stubs) and
  `MAX_SOURCE_DISTANCE_MM = 15` (compensate near the source) cover where the phase error exists
  *along* the pair. The end-to-end phase model cannot see that, and in microstrip it matters
  [S1]. A future note could derive the 15 mm locality figure from the same tolerance: the common
  mode generated over an uncompensated run D. That is not sourced or proposed here.
- **Q32 chamfers are unaffected.** Every bump corner at 45°, feet included, exists so that
  kicad-cli's coupled/uncoupled metric agrees with ours. Its rationale is two-engine agreement,
  not phase.
- **Uncoupled-length limits are unaffected.** They are per-terminal breakout allowances, not a
  skew quantity.

## 7. Implementation sketch (for the follow-up PR, after a ruling)

One resolver, one number, two consumers: `kct check` and the router. This extends the #7797
dedupe.

1. **Operating point on the protocol row.** `router/pair_protocols.py` `ProtocolDefaults` gains
   `rate_gbps: Sourced` (the protocol's maximum rate as the default), `edge_floor_ps: Sourced | None`
   and, for `DDR_DQS_CK`, `timing_skew_ps: Sourced`. `NetClassRouting` gains `data_rate_gbps` and
   `skew_conversion_db` overrides. The derivation is a pure function in a new
   `physics/skew_budget.py`:

   ```python
   def phase_skew_budget_ps(rate_gbps, edge_floor_ps=None, conversion_db=-30.0) -> SkewBudget:
       """dt_max = 2 asin(10^(T/20)) / (2 pi f_eval), f_eval = max(rate/2, 0.35/t_r)."""
   ```

   It has no dependency on the router, so `validate/` and `router/` can both import it. This is
   the module this note's script inlines.
2. **One resolution chain.** `validate/diffpair_engagement.resolve_pair_thresholds` (already "the
   single source") becomes: class mm field > class or protocol phase-derived > protocol table mm >
   generic. The result is `PairThresholds.skew_ps` plus `skew_mm`, the latter converted at the
   slowest copper layer of the board's stackup. Provenance reads, for example,
   `derived:-30 dB at Nyquist 2.5 GHz (USB3_SS 5 Gb/s, table rate)`. A derivation from an
   *assumed* (table-default) rate reports at `warning`; a declared rate reports at `error`.
   `NetClassRouting.effective_skew_tolerance` is today a second copy of the class > protocol >
   default chain. It must delegate to the resolver, and so must
   `pair_postpass_invariant.checker_skew_limit_mm`, so the router's tuners, the post-pass guard
   and `kct check` read one number.
3. **Stackup context.** The resolver needs per-layer delays. `net_class_map_from_path` already
   takes `pcb_path`, so the delays can be attached when the map is loaded (from
   `Stackup.from_pcb`), and the router passes its board stackup the same way. Without a stackup,
   the derived tier is skipped and today's chain applies, so standalone maps behave as they do now.
4. **Measure in time.** `kicad_length` already measures each leg segment by segment. Have it also
   return per-layer lengths, so `diffpair_length_skew` can compare
   |Σ L_layer·d_layer(P) − Σ L_layer·d_layer(N)| with `skew_ps` (§5.3). The tuners keep working in
   mm against `skew_mm`, which is conservative because it uses the slowest layer.
5. **kicad-cli parity.** The `.kicad_dru` `skew` rule stays in millimetres (`skew_mm`, slowest
   layer). Both engines must report 0 errors, so that conservative millimetre bound is what
   effectively ships. Whether pinned kicad-cli 10.0.6 has a time-domain skew constraint is
   **unverified**. Settle it by black-box probe only, never by reading KiCad source.
6. **Board classes.** 06rf, board 06 and 07rf set explicit millimetre fields that would keep
   overriding the derivation. The follow-up either drops them in favour of `pair_protocol` plus
   rate, or keeps them as deliberate overrides. That is an operator choice (Q-d below).
7. **Tests.** The identities in `tests/test_diffpair_phase_skew_table.py` move to the helper's
   tests. Add a parity test that `kct check` and `checker_skew_limit_mm` agree on a derived limit,
   and a layer-asymmetric pair (`USB3_TX1`-like) that passes in mm and fails in time.

## 8. Questions for the operator ruling

- **Q-a. Tolerance.** −30 dB (UI/50, recommended) or −36 dB (UI/100, keeps half for weave and
  package)?
- **Q-b. Edge knee.** Raise f_eval to 0.35/t_r,min when a protocol publishes an edge floor (USB2
  FS/HS, LVDS)? If not, Nyquist-only gives USB2_HS 6.05 mm, LVDS 7.04 mm and USB2_FS 235 mm.
- **Q-c. Rate source.** Use the protocol's maximum rate by default (warning severity) with a
  per-class `data_rate_gbps` override (error severity)?
- **Q-d. Explicit class millimetres.** Should the 06rf/06/07rf generator values (0.5/0.3/0.1/
  0.05/0.075 mm) give way to the derivation?
- **Q-e. Time-domain check.** Adopt it (§5.3), knowing it newly fails layer-asymmetric pairs that
  are length-matched?
- **Q-f. DDR strobes/clocks.** Keep them on a sourced timing limit (UG583 2 ps) rather than the
  phase rule?

## Sources

- [USB2] USB Implementers Forum, *Universal Serial Bus Specification Revision 2.0* (2000):
  §7.1.3 cable skew T_SKEW < 100 ps; §7.1.2.1 FS rise/fall 4–20 ns; §7.1.2.2 HS
  T_HSR/T_HSF ≥ 500 ps. Mirror: https://www.alsa-project.org/files/pub/datasheets/usb/usb_20.pdf
  (official: https://www.usb.org/document-library/usb-20-specification) [read]
- [USB3C] Intel, *USB 3.0 Internal Connector and Cable Specification* Rev 1.0 (2010), §2.2.2.4
  intra-pair skew < 15 ps. Mirror:
  https://www.hwtools.net/content/dam/doc/technical-specification/usb3-internal-connector-cable-specification.pdf
  [read]
- [TI1] TI SPRAAR7J, *High-Speed Interface Layout Guidelines* (rev. 2023), App. A (USB2 50 mil;
  USB3/PCIe/SATA/HDMI 5 mil; CSI mode-conversion footnote, UI/50); §2.1 fibre weave —
  https://www.ti.com/lit/an/spraar7j/spraar7j.pdf [read]
- [TI2] TI SLLA414A, *High-Speed Layout Guidelines for Signal Conditioners and USB Hubs* (rev.
  Jan 2026), §2.1–2.4 (frequencies as Nyquist; USB3 "15 ps/m (TI recommends 5 mils)"; HDMI
  source 0.15·T_bit; DP 20 ps) — https://www.ti.com/lit/an/slla414/slla414.pdf [read]
- [T] Toradex, *Layout Design Guide* v1.0 (2015), §7.1–7.13 per-interface "Max Frequency" and
  "Max intra-pair skew" — https://docs.toradex.com/102492-layout-design-guide.pdf [read]
- [X1] Xilinx UG583 v1.13, *UltraScale Architecture PCB Design* (2018): DDR4 DQS_T/DQS_C and
  CK_T/CK_C 2 ps / 11 mil, DQ to DQS ±5 ps; 16 mil → 4.8° at 5 GHz; balun phase imbalance
  < ~1.5°, CMRR > 30 dB. Mirror: https://0x04.net/~mwk/xidocs/ug/ug583-ultrascale-pcb-design.pdf
  [read]
- [X2] AMD UG863, Versal PCB design, MIPI D-PHY ±2 ps including package —
  https://docs.amd.com/r/en-US/ug863-versal-pcb-design/MIPI-D-PHY [via Q16; the page is
  script-rendered and did not fetch]
- [S1] E. Kunz, I. Novak et al. (Oracle), *Sources and Compensation of Skew in Single-Ended and
  Differential Interconnects*, Signal Integrity Journal, April 2017 (skew resonances at
  1/(2Δt); compensation location matters in microstrip) —
  https://www.signalintegrityjournal.com/ext/resources/article-images-2017/4120/4120_SIJ417_Skew_IN1.pdf
  [read]
- [LV1] TI SLLS373M, *SN65LVDS1, SN65LVDS2, SN65LVDT2* (rev. March 2024): driver 630 Mb/s,
  receiver 400 Mb/s, driver differential t_r typ 0.6 ns / max 1 ns —
  https://www.ti.com/lit/ds/symlink/sn65lvds1.pdf [read]
- [LV2] TI SNLA187, *LVDS Owner's Manual*, 4th ed., §7 (intra-pair skew → mode conversion) —
  https://www.ti.com/lit/ug/snla187/snla187.pdf [read]
- [A3] Z. Peterson, *Where Does the Knee Frequency Formula Come From?*, Altium Resources (2023,
  updated 2026) (0.35 and 0.5 prefactors; channel vs signal bandwidth; PAM4 Nyquist example) —
  https://resources.altium.com/p/where-does-knee-frequency-formula-come [read]
- [N1] Philips/NXP AN10373, *PCI Express PHY PCB Layout Guideline* (2005) (S1 < 2S) [via Q16]
- Renesas, *Guidelines for Full-Speed USB2.0 Board Design*, Table 2 (2.5 mm) [via #6443; not
  re-read]
- **[unverified]** M. Bockelman & W. Eisenstadt, "Combined differential and common-mode
  scattering parameters", IEEE T-MTT 1995 (mixed-mode reference impedances); H. Johnson &
  M. Graham, *High-Speed Digital Design* (knee 0.5/T_r); E. Bogatin, *Signal and Power
  Integrity — Simplified* (BW ≈ 0.35/RT); C. Paul, *Introduction to Electromagnetic
  Compatibility* (trapezoid spectral bounds; common-mode radiation ∝ f). Recalled, not re-read
  for this note. Each claim made from them is also derived in the text.

Not obtained: PCI-SIG CEM/Base, MIPI D-PHY, HDMI and JEDEC specification text. These are
members-only, and their numbers above reach us through vendor notes.
