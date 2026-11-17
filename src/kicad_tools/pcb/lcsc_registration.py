"""Auto-registration of an EasyEDA 3D body to a KiCad footprint (issue #7686).

An EasyEDA STEP body is authored in whatever orientation the part vendor chose,
so each LCSC part used to need a human render loop before its ``rotate`` /
``offset`` could enter :data:`~kicad_tools.pcb.lcsc_model_transforms.LCSC_MODEL_TRANSFORMS`.
EasyEDA publishes the part's *own* footprint pad geometry alongside the body.
Registering that pad set onto the pads of the KiCad footprint we are placing
(a 2D rigid fit: one rotation + one translation, matched by pad number) yields
the body's ``rotate``/``offset`` directly, together with a **residual** that
says how much to trust it.

* low residual   -> the transform is **derived** and auto-accepted;
* high residual  -> not accepted: fall back to the packaged table or flag the
  part for human calibration.  A wrong auto-guess is worse than none.

**Conventions** (shared with ``models3d``): both pad sets are 2D footprint
coordinates in mm, Y down.  ``theta`` is the CCW rotation (``x' = x cos - y
sin``) that maps source pads onto target pads; a written ``(rotate (xyz 0 0
rz))`` **is** the footprint-2D rotation it produces (see
``models3d._apply_rotate_delta``), so ``rz = theta``.  The EasyEDA footprint
origin is taken to coincide with the body's origin (the same "origin-authored"
assumption the rest of the LCSC tier makes).  The fitted translation ``t``
places that origin on the target footprint; the tier already adds the target
pad centroid ``c_t`` to the model offset, so the *override* returned here is
``t - c_t``, with model-frame Y negated.

**Ideas only, no code, from other projects.**  The approach is inspired by the
CPL rotation work in kicad-jlcpcb-tools (MIT).  easyeda2kicad is AGPL: none of
its code is used.  The EasyEDA response layout parsed below is an *unverified*
black-box reading -- ``parse_easyeda_pads`` is deliberately tolerant and returns
``[]`` rather than guessing when the layout is not what it expects.  No agent
has contacted EasyEDA to confirm it (operator ruling Q44); the live check is
tracked in the PR that introduced this module.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "EASYEDA_UNIT_MM",
    "MAX_RESIDUAL_MM",
    "MIN_MATCH_FRACTION",
    "RMS_RESIDUAL_MM",
    "EasyEdaPad",
    "RegistrationResult",
    "parse_easyeda_pads",
    "register_pads",
]

Triple = tuple[float, float, float]

#: One EasyEDA canvas unit is 10 mil.
EASYEDA_UNIT_MM = 0.254

#: Acceptance thresholds for a derived transform.
RMS_RESIDUAL_MM = 0.05
MAX_RESIDUAL_MM = 0.10
MIN_MATCH_FRACTION = 0.5


@dataclass(frozen=True)
class EasyEdaPad:
    """One pad of the EasyEDA footprint, in mm relative to its origin (Y down)."""

    number: str
    x: float
    y: float


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of :func:`register_pads`."""

    rotate: Triple
    """Model-frame ``(rx, ry, rz)``; only ``rz`` is non-zero."""
    offset: Triple
    """Override to ADD to the tier's pad-centroid offset (model frame, Y negated)."""
    theta_deg: float
    translation: tuple[float, float]
    """Where the EasyEDA origin lands on the target footprint (footprint 2D, mm)."""
    rms_mm: float
    max_mm: float
    matched: int
    total_target: int
    accepted: bool
    reason: str

    def describe(self) -> str:
        """One-line summary for reports."""
        verdict = "derived" if self.accepted else "NOT accepted"
        return (
            f"{verdict}: rotate z={self.rotate[2]:g} deg, offset "
            f"({self.offset[0]:g}, {self.offset[1]:g}, {self.offset[2]:g}) mm; "
            f"residual rms {self.rms_mm:.3f} mm, max {self.max_mm:.3f} mm over "
            f"{self.matched}/{self.total_target} pads"
            + ("" if self.accepted else f" ({self.reason})")
        )


def _to_float(value: object) -> float | None:
    try:
        out = float(str(value))
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def parse_easyeda_pads(component_info: bytes | dict) -> list[EasyEdaPad]:
    """Extract the footprint pads from an EasyEDA component-info body.

    Reads ``result.packageDetail.dataStr`` : the tilde-delimited ``PAD~shape~cx~cy~w~h~
    layer~net~number~...`` shape strings, in canvas units relative to
    ``dataStr.head.(x, y)``.  Only the centre and the pad number are used.
    Returns ``[]`` when the layout is not recognised (never raises, never guesses).
    """
    try:
        doc = json.loads(component_info) if isinstance(component_info, bytes) else component_info
        data_str = doc["result"]["packageDetail"]["dataStr"]
        shapes = data_str["shape"]
    except (KeyError, TypeError, ValueError):
        return []
    if not isinstance(shapes, list):
        return []
    head = data_str.get("head") if isinstance(data_str, dict) else None
    hx = _to_float(head.get("x")) if isinstance(head, dict) else None
    hy = _to_float(head.get("y")) if isinstance(head, dict) else None
    hx, hy = hx or 0.0, hy or 0.0
    pads: list[EasyEdaPad] = []
    for entry in shapes:
        if not isinstance(entry, str) or not entry.startswith("PAD~"):
            continue
        fields = entry.split("~")
        if len(fields) < 9:
            continue
        cx, cy = _to_float(fields[2]), _to_float(fields[3])
        number = fields[8].strip()
        if cx is None or cy is None or not number:
            continue
        pads.append(
            EasyEdaPad(
                number=number,
                x=(cx - hx) * EASYEDA_UNIT_MM,
                y=(cy - hy) * EASYEDA_UNIT_MM,
            )
        )
    return pads


def _group_by_number(points: Sequence[tuple[str, float, float]]) -> dict[str, tuple[float, float]]:
    """Average same-numbered pads (multi-pad nets, thermal-via arrays)."""
    acc: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for number, x, y in points:
        acc[number].append((x, y))
    return {
        n: (sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps))
        for n, ps in acc.items()
    }


def _round(value: float, places: int) -> float:
    out = round(value, places)
    return 0.0 if out == 0 else out


def register_pads(
    source: Sequence[EasyEdaPad],
    target: Sequence[tuple[str, tuple[float, float]]],
    *,
    rms_tol: float = RMS_RESIDUAL_MM,
    max_tol: float = MAX_RESIDUAL_MM,
    min_fraction: float = MIN_MATCH_FRACTION,
) -> RegistrationResult | None:
    """Fit EasyEDA *source* pads onto KiCad *target* pads (matched by pad number).

    *target* is ``[(pad_number, (x, y)), ...]`` as returned by
    ``models3d._pad_positions``.  Returns ``None`` when there are fewer than two
    distinct matching pad numbers or the matched pads have no spread to fit a
    rotation from; otherwise a :class:`RegistrationResult` whose ``accepted``
    flag applies the residual thresholds.
    """
    src = _group_by_number([(p.number, p.x, p.y) for p in source])
    tgt = _group_by_number([(n, xy[0], xy[1]) for n, xy in target])
    names = sorted(set(src) & set(tgt))
    if len(names) < 2:
        return None
    n = len(names)
    cs = (sum(src[k][0] for k in names) / n, sum(src[k][1] for k in names) / n)
    ct = (sum(tgt[k][0] for k in names) / n, sum(tgt[k][1] for k in names) / n)
    dot = cross = spread = 0.0
    for k in names:
        sx, sy = src[k][0] - cs[0], src[k][1] - cs[1]
        tx, ty = tgt[k][0] - ct[0], tgt[k][1] - ct[1]
        dot += sx * tx + sy * ty
        cross += sx * ty - sy * tx
        spread += sx * sx + sy * sy
    if spread < 1e-6:
        return None
    theta = math.atan2(cross, dot)
    c, s = math.cos(theta), math.sin(theta)
    # t = c_t - R c_s : where the EasyEDA origin lands on the target footprint.
    tx0 = ct[0] - (c * cs[0] - s * cs[1])
    ty0 = ct[1] - (s * cs[0] + c * cs[1])
    sq = 0.0
    worst = 0.0
    for k in names:
        px = c * src[k][0] - s * src[k][1] + tx0
        py = s * src[k][0] + c * src[k][1] + ty0
        d = math.hypot(px - tgt[k][0], py - tgt[k][1])
        sq += d * d
        worst = max(worst, d)
    rms = math.sqrt(sq / n)

    # The tier already adds the target pad centroid (all target pads) to the
    # model offset, so only the remainder is an override; model Y is negated.
    # ``models3d._pad_anchor`` averages every pad node, not one per number.
    cx = sum(xy[0] for _, xy in target) / len(target)
    cy = sum(xy[1] for _, xy in target) / len(target)
    ox, oy = tx0 - cx, -(ty0 - cy)
    theta_deg = _round(math.degrees(theta), 2)
    if theta_deg <= -180.0:
        theta_deg += 360.0

    reason = ""
    if n < max(2, math.ceil(min_fraction * len(tgt))):
        reason = f"only {n} of {len(tgt)} target pads matched by number"
    elif rms > rms_tol or worst > max_tol:
        reason = f"residual above {rms_tol:g}/{max_tol:g} mm (rms/max): needs human calibration"
    return RegistrationResult(
        rotate=(0.0, 0.0, theta_deg),
        offset=(_round(ox, 3), _round(oy, 3), 0.0),
        theta_deg=theta_deg,
        translation=(tx0, ty0),
        rms_mm=rms,
        max_mm=worst,
        matched=n,
        total_target=len(tgt),
        accepted=not reason,
        reason=reason,
    )
