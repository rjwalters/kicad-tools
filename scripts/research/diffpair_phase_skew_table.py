#!/usr/bin/env python3
"""Phase-derived intra-pair skew limits for the diff pairs on boards 03/06/06rf/07 (Issue #7883).

Research only: this script **changes no limit**.  It prints the proposed
limit table of ``docs/research/diffpair-phase-skew-tolerance.md`` so the
numbers there can be regenerated from the repo's own physics.

Model (derived in the note)::

    v_p   = c / sqrt(eps_eff)                 telegrapher's equation, lossless TEM
    dt    = dL / v_p                          length mismatch -> time
    dphi  = 2 pi f dt                         time -> phase at the evaluation frequency
    |Sdd21| = cos(dphi / 2)                   differential amplitude kept
    |Scd21| = sin(dphi / 2)                   differential -> common-mode conversion

A tolerance ``T`` dB on mode conversion gives ``dphi_max = 2 asin(10^(T/20))``
and ``dL_max = dphi_max / (2 pi f) * v_p``.  ``f`` is the Nyquist frequency
(bit rate / 2), raised to the edge knee ``0.35 / t_r`` when the protocol
specifies a minimum rise time and that knee is higher.

``eps_eff`` comes from
:class:`kicad_tools.physics.transmission_line.TransmissionLine` at the class
trace width (microstrip, ``F.Cu``); inner layers are TEM striplines, so their
``eps_eff`` is the surrounding dielectric's ``eps_r`` (the highest of the
board's inner layers).  Committed
copper on boards 03 and 06rf uses inner layers too, so the proposed millimetre
limit is the time limit divided by the *slowest* copper layer's delay (the
layer-agnostic, conservative conversion a millimetre DRC rule needs).  The
coupled odd/even values from :class:`kicad_tools.physics.coupled_lines.CoupledLines`
are kept in ``--json`` output as a sensitivity check.

Usage::

    uv run python scripts/research/diffpair_phase_skew_table.py            # markdown
    uv run python scripts/research/diffpair_phase_skew_table.py --json     # machine-readable
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# The formula and the sourced operating points live in the one helper
# ``kicad_tools.physics.skew_budget`` (Issue #7892); this script only adds the
# per-board assumptions of the note.
from kicad_tools.physics import skew_budget as _sb  # noqa: E402
from kicad_tools.physics.skew_budget import (  # noqa: E402
    C_MM_PER_PS,
    KNEE_COEFF,
    dphi_max_rad,
    scd21_db,
)

#: Mode-conversion tolerances evaluated (dB, |Scd21| at the evaluation frequency).
#: -30 dB is the note's recommendation; -36 dB reserves half the phase budget
#: for skew sources routing cannot see (fibre weave, package, connector).
TOLERANCES_DB: tuple[float, ...] = (-30.0, -36.0)


@dataclass(frozen=True)
class Signal:
    """Operating point of a pair: bit rate and fastest permitted edge."""

    rate_gbps: float
    rate_source: str
    tr_min_ps: float | None = None
    tr_source: str = ""
    #: A strobe/clock whose crossing point is a timing quantity: the S/N
    #: framing does not set its limit (see the note).  Value = timing limit, ps.
    timing_limit_ps: float | None = None
    timing_source: str = ""


def _signal(point: _sb.OperatingPoint, rate_source: str | None = None) -> Signal:
    """A :class:`Signal` from one of the helper's sourced operating points."""
    return Signal(
        point.rate_gbps,
        rate_source or point.rate_source,
        point.edge_floor_ps,
        point.edge_source,
        point.timing_limit_ps,
        point.timing_source,
    )


# Sources are keyed in the note's Sources list.
USB2_FS = _signal(_sb.USB2_FS)
USB2_HS = _signal(_sb.USB2_HS)
#: The note evaluates 06rf at the rates its README declares, not each
#: protocol's maximum (which ``kct check`` uses by default, Issue #7892).
USB3_G1 = Signal(5.0, "06rf README: USB 3.0, 5 Gb/s")
PCIE_G1 = Signal(2.5, "06rf README: PCIe Gen1, 2.5 Gb/s")
MIPI_1G = Signal(1.0, "06rf README: D-PHY, 1 Gb/s/lane")
#: Board 07's regression fixture is synthetic and declares no rate: use the
#: rate TI's D-PHY footnote evaluates (UI = 400 ps) [TI1].
MIPI_2G5 = _signal(_sb.MIPI_DPHY, "assumed: [TI1] UI = 400 ps (2.5 Gb/s)")
HDMI_14 = _signal(_sb.HDMI_14, "assumed: HDMI 1.4b, data up to 1.7 GHz [TI2]")
#: SN65LVDS1 specifies a typical 0.6 ns differential rise and no minimum, so
#: the typical value stands in for the floor (flagged in the note).
LVDS_400M = _signal(_sb.LVDS)
DDR4_3200 = _signal(_sb.DDR_DQS_CK, "assumed: DDR4-3200 (synthetic fixture)")


@dataclass(frozen=True)
class BoardSpec:
    key: str
    pcb: str | None
    net_class_map: str | None
    #: ``None``: load the generator's ``build_net_class_map()`` (07rf has no
    #: committed fixture PCB; ``Stackup.jlcpcb_4layer()`` is what its own
    #: impedance sizing and ``Stackup.from_pcb`` use for a 4-layer board).
    generator_dir: str | None
    signals: dict[str, Signal]
    note: str = ""


BOARDS: tuple[BoardSpec, ...] = (
    BoardSpec(
        "03",
        "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb",
        "boards/03-usb-joystick/output/net_class_map.json",
        None,
        {"HighSpeed": USB2_FS},
    ),
    BoardSpec(
        "06",
        "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb",
        "boards/06-diffpair-test/output/net_class_map.json",
        None,
        {"LVDS": LVDS_400M},
    ),
    BoardSpec(
        "06rf",
        "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb",
        "boards/06-diffpair-test/regression-fixture/net_class_map.json",
        None,
        {"USB2": USB2_HS, "USB3": USB3_G1, "PCIe": PCIE_G1, "MIPI": MIPI_1G},
    ),
    BoardSpec(
        "07",
        "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb",
        "boards/07-matchgroup-test/output/net_class_map.json",
        None,
        {},
        note="SDR SDRAM exerciser: single-ended only, no diff pairs",
    ),
    BoardSpec(
        "07rf",
        None,
        None,
        "boards/07-matchgroup-test",
        {"MIPI_CSI_LANES": MIPI_2G5, "HDMI_TMDS_LANES": HDMI_14, "DDR_DQS": DDR4_3200},
        note="regression fixture, generated (not committed)",
    ),
)


@dataclass
class Row:
    board: str
    pair: str
    net_class: str
    protocol: str | None
    rate_gbps: float
    f_nyquist_ghz: float
    f_knee_ghz: float | None
    f_eval_ghz: float
    width_mm: float
    gap_mm: float | None
    layers: str
    eps_eff: float
    eps_odd: float | None
    eps_even: float | None
    ps_per_mm: float
    eps_inner: float
    ps_per_mm_inner: float
    today_mm: float
    today_source: str
    today_phase_deg: float
    today_scd21_db: float
    derived_ps: dict[str, float]
    derived_mm: dict[str, float]
    derived_mm_inner: dict[str, float]
    nyquist_only_mm: dict[str, float]
    recommended_mm: float
    recommended_basis: str
    change: str
    sources: str
    #: Committed copper (``None`` for 07rf): skew as ``kct check`` measures it
    #: (vias contribute 0, ``num_copper_layers=4``) and the same copper
    #: converted to time layer by layer -- the quantity a derived limit checks.
    committed_skew_mm: float | None = None
    committed_skew_ps: float | None = None


def load_board(spec: BoardSpec):
    from kicad_tools.physics.stackup import Stackup

    pcb = None
    if spec.generator_dir is not None:
        sys.path.insert(0, str(REPO / "scripts" / "ci"))
        from net_class_map_resolver import build_net_class_map_for_board

        ncm = build_net_class_map_for_board(REPO / spec.generator_dir) or {}
        stackup = Stackup.jlcpcb_4layer()
    else:
        from kicad_tools.router.rules import net_class_map_from_path
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(REPO / spec.pcb))
        ncm = net_class_map_from_path(REPO / spec.net_class_map, pcb_path=REPO / spec.pcb)
        stackup = Stackup.from_pcb(pcb)
    return pcb, ncm, stackup


def pairs_of(ncm, pcb) -> list[tuple[str, str]]:
    from kicad_tools.router.diffpair_detection import build_synth_routing, detect_diff_pairs

    if pcb is not None:
        names = {n.number: n.name for n in pcb.nets.values() if n.name}
    else:
        names = {i + 1: name for i, name in enumerate(sorted(ncm))}
    synth, n2c = build_synth_routing(ncm)
    out = [
        (d.pair.positive.net_name, d.pair.negative.net_name)
        for d in detect_diff_pairs(names, net_class_routing=synth, net_to_class=n2c)
    ]
    return sorted(out)


def routed_layers(pcb, p: str, n: str) -> str:
    if pcb is None:
        return "(unrouted fixture)"
    ids = {net.number for net in pcb.nets.values() if net.name in (p, n)}
    layers = sorted({s.layer for s in pcb.segments if s.net_number in ids})
    return "+".join(layers) if layers else "(no copper)"


def committed_skew(pcb, ncm, p: str, n: str, ps_outer: float, ps_inner: float, stackup):
    """``(skew_mm, skew_ps)`` of a pair's committed copper, or ``(None, None)``.

    ``skew_mm`` is what ``kct check`` measures (the kicad-cli length model:
    pad-trimmed tracks plus via stackup height).  ``skew_ps`` converts the
    same per-leg lengths to time: every millimetre at the outer-layer delay,
    plus the extra inner-layer delay for the track length that runs on inner
    layers.  It is the quantity a time-domain derived limit would check.
    """
    if pcb is None:
        return None, None
    from kicad_tools.router.diffpair_length import DiffPairLengthTracker

    ids = {net.name: net.number for net in pcb.nets.values() if net.name in (p, n)}
    layers = len(stackup.copper_layers)

    def leg(name: str) -> tuple[float, float]:
        length = DiffPairLengthTracker.measure_net_from_pcb(
            pcb, ids[name], num_copper_layers=layers
        )
        inner = sum(
            math.hypot(seg.end[0] - seg.start[0], seg.end[1] - seg.start[1])
            for seg in pcb.segments_in_net(ids[name])
            if not stackup.is_outer_layer(seg.layer)
        )
        return length, length * ps_outer + inner * (ps_inner - ps_outer)

    (lp, tp), (ln, tn) = leg(p), leg(n)
    return abs(lp - ln), abs(tp - tn)


def build_rows() -> tuple[list[Row], list[str]]:
    from kicad_tools.physics.coupled_lines import CoupledLines
    from kicad_tools.physics.transmission_line import TransmissionLine
    from kicad_tools.validate.diffpair_engagement import resolve_pair_thresholds

    rows: list[Row] = []
    notes: list[str] = []
    for spec in BOARDS:
        pcb, ncm, stackup = load_board(spec)
        pairs = pairs_of(ncm, pcb)
        if not pairs:
            notes.append(f"{spec.key}: 0 diff pairs ({spec.note})")
            continue
        tl = TransmissionLine(stackup)
        cl = CoupledLines(stackup)
        for p, n in pairs:
            nc = ncm.get(p) or ncm.get(n)
            cls = nc.name
            sig = spec.signals[cls]
            width = float(nc.trace_width)
            gap = getattr(nc, "intra_pair_clearance", None)
            eps = tl.microstrip(width, "F.Cu").epsilon_eff
            eps_odd = eps_even = None
            if gap:
                pr = cl.edge_coupled_microstrip(width, float(gap), "F.Cu")
                eps_odd, eps_even = pr.epsilon_eff_odd, pr.epsilon_eff_even
            ps_per_mm = math.sqrt(eps) / C_MM_PER_PS
            # Inner layers: a stripline is a TEM line in (near-)homogeneous
            # dielectric, so eps_eff = eps_r of the surrounding dielectric
            # (``Stackup.get_dielectric_constant`` averages the two sides).
            # ``TransmissionLine.stripline`` needs declared plane roles, which
            # these stackups do not carry.
            eps_in = max(
                stackup.get_dielectric_constant(lyr.name)
                for lyr in stackup.copper_layers
                if not stackup.is_outer_layer(lyr.name)
            )
            ps_in = math.sqrt(eps_in) / C_MM_PER_PS
            slowest = max(ps_per_mm, ps_in)
            f_n = sig.rate_gbps / 2.0
            f_k = KNEE_COEFF / (sig.tr_min_ps * 1e-3) if sig.tr_min_ps else None
            f_eval = max(f_n, f_k or 0.0)
            thr = resolve_pair_thresholds(ncm, p, n)
            today = float(thr.skew_mm)
            # Phase of today's limit, at the slowest layer (worst case).
            today_dphi = 2 * math.pi * f_eval * today * slowest * 1e-3
            derived_ps: dict[str, float] = {}
            derived_mm: dict[str, float] = {}
            derived_in: dict[str, float] = {}
            nyq_mm: dict[str, float] = {}
            for tol in TOLERANCES_DB:
                key = f"{tol:g}dB"
                dphi = dphi_max_rad(tol)
                dt = dphi / (2 * math.pi * f_eval) * 1e3  # ps (f in GHz)
                derived_ps[key] = dt
                derived_mm[key] = dt / ps_per_mm
                derived_in[key] = dt / slowest
                nyq_mm[key] = dphi / (2 * math.pi * f_n) * 1e3 / slowest
            rec_key = f"{TOLERANCES_DB[0]:g}dB"
            if sig.timing_limit_ps is not None:
                rec_mm = sig.timing_limit_ps / slowest
                basis = f"timing: {sig.timing_source}"
            else:
                rec_mm = derived_in[rec_key]
                basis = f"phase {rec_key} at {'knee' if f_k and f_k > f_n else 'Nyquist'}"
            ratio = rec_mm / today
            change = (
                "same" if 0.9 <= ratio <= 1.1 else ("loosen" if ratio > 1 else "tighten")
            ) + f" x{ratio:.2f}"
            src = sig.rate_source + (f"; {sig.tr_source}" if sig.tr_source else "")
            c_mm, c_ps = committed_skew(pcb, ncm, p, n, ps_per_mm, ps_in, stackup)
            rows.append(
                Row(
                    board=spec.key,
                    pair=f"{p}/{n}",
                    net_class=cls,
                    protocol=getattr(nc, "pair_protocol", None),
                    rate_gbps=sig.rate_gbps,
                    f_nyquist_ghz=f_n,
                    f_knee_ghz=f_k,
                    f_eval_ghz=f_eval,
                    width_mm=width,
                    gap_mm=float(gap) if gap else None,
                    layers=routed_layers(pcb, p, n),
                    eps_eff=eps,
                    eps_odd=eps_odd,
                    eps_even=eps_even,
                    ps_per_mm=ps_per_mm,
                    eps_inner=eps_in,
                    ps_per_mm_inner=ps_in,
                    today_mm=today,
                    today_source=thr.provenance.get("skew_mm", ""),
                    today_phase_deg=math.degrees(today_dphi),
                    today_scd21_db=scd21_db(today_dphi),
                    derived_ps=derived_ps,
                    derived_mm=derived_mm,
                    derived_mm_inner=derived_in,
                    nyquist_only_mm=nyq_mm,
                    recommended_mm=rec_mm,
                    recommended_basis=basis,
                    change=change,
                    sources=src,
                    committed_skew_mm=c_mm,
                    committed_skew_ps=c_ps,
                )
            )
    return rows, notes


def render(rows: list[Row], notes: list[str]) -> str:
    t30, t36 = (f"{t:g}dB" for t in TOLERANCES_DB)
    out = [
        "| Board | Pair | Class (protocol) | Rate | f_eval, GHz (basis) | ps/mm outer / inner "
        "| Today, mm (at f_eval) | -30 dB: ps | mm outer / slowest | -36 dB: mm | Proposed, mm "
        "| Change |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        basis = "knee" if r.f_knee_ghz and r.f_knee_ghz > r.f_nyquist_ghz else "Nyquist"
        rate = f"{r.rate_gbps * 1000:g} Mb/s" if r.rate_gbps < 1 else f"{r.rate_gbps:g} Gb/s"
        out.append(
            f"| {r.board} | {r.pair} | {r.net_class} ({r.protocol or '-'}) | {rate} "
            f"| {r.f_eval_ghz:.3f} ({basis}) | {r.ps_per_mm:.2f} / {r.ps_per_mm_inner:.2f} "
            f"| {r.today_mm:g} ({r.today_phase_deg:.2f} deg, {r.today_scd21_db:.1f} dB) "
            f"| {r.derived_ps[t30]:.1f} | {r.derived_mm[t30]:.2f} / {r.derived_mm_inner[t30]:.2f} "
            f"| {r.derived_mm_inner[t36]:.2f} | **{r.recommended_mm:.2f}** | {r.change} |"
        )
    out.append("")
    out.append("Per pair: layers carrying committed copper; source of today's limit; basis:")
    out.append("")
    for r in rows:
        committed = ""
        if r.committed_skew_mm is not None and r.committed_skew_ps is not None:
            limit_ps = (
                r.derived_ps[t30]
                if "timing" not in r.recommended_basis
                else (r.recommended_mm * r.ps_per_mm_inner)
            )
            committed = (
                f"committed skew {r.committed_skew_mm:.3f} mm / {r.committed_skew_ps:.2f} ps "
                f"(today {'pass' if r.committed_skew_mm <= r.today_mm + 1e-6 else 'FAIL'}, "
                f"proposed {'pass' if r.committed_skew_ps <= limit_ps + 1e-6 else 'FAIL'} "
                f"against {limit_ps:.1f} ps); "
            )
        out.append(
            f"- {r.board} {r.pair}: {r.layers}; {committed}today `{r.today_source}`; "
            f"proposed = {r.recommended_basis}; rate {r.sources}; "
            f"Nyquist-only -30 dB = {r.nyquist_only_mm[t30]:.2f} mm"
        )
    for n in notes:
        out.append(f"- {n}")
    return "\n".join(out)


#: Per-protocol operating points for the protocol table (rate, t_r floor).
#: USB3 Gen2 and HDMI 2.0 are what-if rows: no board here carries them.
PROTOCOL_POINTS: tuple[tuple[str, str | None, Signal], ...] = (
    ("USB2_FS", "USB2_FS", USB2_FS),
    ("USB2_HS", "USB2_HS", USB2_HS),
    ("USB3_SS Gen1", "USB3_SS", USB3_G1),
    ("USB3 Gen2 (what-if)", None, Signal(10.0, "USB 3.2 Gen2, 10 Gb/s [TI2]")),
    ("PCIE_G1", "PCIE_G1", PCIE_G1),
    ("PCIe Gen2 (what-if)", "PCIE_G2_PLUS", Signal(5.0, "5 GT/s [T]")),
    ("PCIe Gen3 (what-if)", "PCIE_G2_PLUS", Signal(8.0, "8 GT/s [T]")),
    ("MIPI_DPHY 1 Gb/s", "MIPI_DPHY", MIPI_1G),
    ("MIPI_DPHY 2.5 Gb/s", "MIPI_DPHY", MIPI_2G5),
    ("HDMI_TMDS 1.4b", "HDMI_TMDS", HDMI_14),
    ("HDMI 2.0 (what-if)", "HDMI_TMDS", Signal(6.0, "HDMI 2.0b, data up to 3 GHz [TI2]")),
    ("LVDS 400 Mb/s", "LVDS", LVDS_400M),
    ("DDR_DQS_CK DDR4-3200", "DDR_DQS_CK", DDR4_3200),
)


def protocol_rows(width_mm: float = 0.2) -> list[str]:
    """Protocol table: evaluation frequency, phase/dB tolerance, time and mm limits.

    Millimetres use the legacy JLCPCB 4-layer model (``Stackup.jlcpcb_4layer``):
    microstrip at ``width_mm`` on F.Cu and the slowest inner dielectric.
    """
    from kicad_tools.physics.stackup import Stackup
    from kicad_tools.physics.transmission_line import TransmissionLine

    st = Stackup.jlcpcb_4layer()
    outer = math.sqrt(TransmissionLine(st).microstrip(width_mm, "F.Cu").epsilon_eff) / C_MM_PER_PS
    inner = max(
        math.sqrt(st.get_dielectric_constant(lyr.name)) / C_MM_PER_PS
        for lyr in st.copper_layers
        if not st.is_outer_layer(lyr.name)
    )
    lines = [
        f"Delay model: F.Cu microstrip w={width_mm:g} mm {outer:.2f} ps/mm; slowest inner "
        f"{inner:.2f} ps/mm (Stackup.jlcpcb_4layer).",
        "",
        "| Protocol | Rate | t_r floor | f_eval, GHz (basis) | -30 dB: dphi / dt | mm outer / "
        "inner | -36 dB: dt / mm inner | Today's table value |",
        "|---|---|---|---|---|---|---|---|",
    ]
    from kicad_tools.router.pair_protocols import PROTOCOL_DEFAULTS, parse_protocol

    for name, proto, sig in PROTOCOL_POINTS:
        f_n = sig.rate_gbps / 2.0
        f_k = KNEE_COEFF / (sig.tr_min_ps * 1e-3) if sig.tr_min_ps else None
        f_eval = max(f_n, f_k or 0.0)
        basis = "knee" if f_k and f_k > f_n else "Nyquist"
        d30, d36 = dphi_max_rad(-30.0), dphi_max_rad(-36.0)
        t30 = d30 / (2 * math.pi * f_eval) * 1e3
        t36 = d36 / (2 * math.pi * f_eval) * 1e3
        rate = f"{sig.rate_gbps * 1000:g} Mb/s" if sig.rate_gbps < 1 else f"{sig.rate_gbps:g} Gb/s"
        tr = f"{sig.tr_min_ps:g} ps" if sig.tr_min_ps else "-"
        row = PROTOCOL_DEFAULTS.get(parse_protocol(proto)) if proto else None
        table = "-"
        if row and row.max_skew_mm:
            table = f"{row.max_skew_mm.value:g} mm" + (
                "" if row.max_skew_mm.cited else " (uncited)"
            )
        extra = ""
        if sig.timing_limit_ps is not None:
            extra = f" (timing-limited: {sig.timing_limit_ps:g} ps = {sig.timing_limit_ps / inner:.2f} mm)"
        lines.append(
            f"| {name} | {rate} | {tr} | {f_eval:.3f} ({basis}) | {math.degrees(d30):.2f} deg / "
            f"{t30:.1f} ps | {t30 / outer:.2f} / {t30 / inner:.2f}{extra} | {t36:.1f} ps / "
            f"{t36 / inner:.2f} | {table} |"
        )
    return lines


def bump_impedance_rows() -> list[str]:
    """Zdiff of each class at its gap and with one leg pushed out 1x / 2x the gap."""
    from kicad_tools.physics.coupled_lines import CoupledLines

    lines = [
        "| Board | Class | w / s (mm) | Zdiff at s | at 2s (1x bump) | at 3s (2x bump, Q35) |",
        "|---|---|---|---|---|---|",
    ]
    seen: set[tuple[str, str]] = set()
    for spec in BOARDS:
        pcb, ncm, stackup = load_board(spec)
        cl = CoupledLines(stackup)
        for p, _n in pairs_of(ncm, pcb):
            nc = ncm[p]
            if (spec.key, nc.name) in seen:
                continue
            seen.add((spec.key, nc.name))
            w, s = float(nc.trace_width), float(nc.intra_pair_clearance)
            z = [cl.edge_coupled_microstrip(w, k * s, "F.Cu").zdiff for k in (1, 2, 3)]
            lines.append(
                f"| {spec.key} | {nc.name} | {w:g} / {s:g} | {z[0]:.1f} | {z[1]:.1f} | {z[2]:.1f} |"
            )
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="emit JSON rows instead of markdown")
    ap.add_argument("--protocols", action="store_true", help="also print the per-protocol table")
    ap.add_argument(
        "--bump-impedance",
        action="store_true",
        help="also print Zdiff with one leg pushed out 1x/2x the gap",
    )
    args = ap.parse_args(argv)
    rows, notes = build_rows()
    if args.json:
        print(json.dumps({"rows": [asdict(r) for r in rows], "notes": notes}, indent=2))
        return 0
    print(render(rows, notes))
    if args.protocols:
        print()
        print("\n".join(protocol_rows()))
    if args.bump_impedance:
        print()
        print("\n".join(bump_impedance_rows()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
