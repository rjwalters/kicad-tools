"""Rule areas (keepout zones) are not mask-attribution objects (issue #9234).

A footprint-embedded keepout -- the stock RF-module footprint shape -- plots
no copper and has no mask presence, yet it was treated as a required native
object, so ``coverage.mask_to_copper`` read ``unknown`` for every board using
one. These are the hermetic controls: the inventory skips rule areas on both
sides, real zones stay inventoried, and a missing/ambiguous-object reason now
names the object's type and parent footprint instead of only a uuid. The
native end-to-end witness lives in ``test_mask_copper_native.py``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid as uuid_module
from pathlib import Path

import pytest

from tests.test_mask_export_geometry import _board


#: A stock RF-module-shaped footprint: one pad plus a keepout rule area on
#: all copper layers, and a board-level keepout beside it. ``_board`` numbers
#: uuids in source order: footprint 1, pad 2, footprint keepout 3, board
#: keepout 4.
def _keepout_board(flags: str) -> str:
    return f"""(footprint "RF1" (layer "F.Cu") (at 50 50)
      (property "Reference" "RF1" (at 0 -3) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Mask"))
      (zone (layers "F.Cu" "B.Cu")
        (keepout {flags})
        (name "") (hatch edge 0.508)
        (polygon (pts (xy -2 -2) (xy 2 -2) (xy 2 2) (xy -2 2)))))
      (zone (layers "F.Cu" "B.Cu")
        (keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed)
          (copperpour not_allowed) (footprints not_allowed))
        (name "") (hatch edge 0.508)
        (polygon (pts (xy 20 20) (xy 24 20) (xy 24 24) (xy 20 24))))"""


_ALL_NOT_ALLOWED = (
    "(tracks not_allowed) (vias not_allowed) (pads not_allowed) "
    "(copperpour not_allowed) (footprints not_allowed)"
)
_KEEPOUT_BOARD = _keepout_board(_ALL_NOT_ALLOWED)

#: The same footprint carrying a real (filled) zone instead of the keepout,
#: plus a board-level filled zone: both are plotted copper and must stay
#: required native objects. Same uuid numbering as ``_KEEPOUT_BOARD``.
_REAL_ZONE_BOARD = """(footprint "RF1" (layer "F.Cu") (at 50 50)
      (property "Reference" "RF1" (at 0 -3) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Mask"))
      (zone (net 0) (net_name "") (layer "F.Cu") (min_thickness .1) (fill yes)
        (polygon (pts (xy -2 -2) (xy 2 -2) (xy 2 2) (xy -2 2)))
        (filled_polygon (layer "F.Cu") (pts (xy -2 -2) (xy 2 -2) (xy 2 2) (xy -2 2)))))
      (zone (net 0) (net_name "") (layer "F.Cu") (min_thickness .1) (fill yes)
        (polygon (pts (xy 20 20) (xy 24 20) (xy 24 24) (xy 20 24)))
        (filled_polygon (layer "F.Cu") (pts (xy 20 20) (xy 24 20) (xy 24 24) (xy 20 24))))"""

_UUID = {n: str(uuid_module.UUID(int=n)) for n in (1, 2, 3, 4)}


def _parse(board: Path):
    from kicad_tools.sexp import parse_string

    return parse_string(board.read_text())


def _inventoried(board: Path):
    from kicad_tools.validate.mask_export_geometry import _inventory

    return [f["source_uuid"] for f in _inventory(_parse(board))]


@pytest.mark.parametrize(
    "flags",
    [
        _ALL_NOT_ALLOWED,
        # A rule area that permits copper pour is still a rule area: the pour
        # inside belongs to another zone object, never to this one.
        "(tracks allowed) (vias allowed) (pads allowed) (copperpour allowed) (footprints allowed)",
    ],
    ids=["all-not-allowed", "copperpour-allowed"],
)
def test_rule_areas_never_enter_the_inventory(tmp_path, flags):
    board = _board(tmp_path / "board.kicad_pcb", _keepout_board(flags))
    inventoried = _inventoried(board)
    assert _UUID[3] not in inventoried  # footprint-embedded rule area
    assert _UUID[4] not in inventoried  # board-level rule area
    assert _UUID[2] in inventoried  # the pad stays a plotted object


def test_rule_area_with_a_net_is_still_excluded(tmp_path):
    body = _KEEPOUT_BOARD.replace("(zone (layers", '(zone (net 1) (net_name "N") (layers', 1)
    board = _board(tmp_path / "board.kicad_pcb", body)
    assert _UUID[3] not in _inventoried(board)


def test_real_zones_stay_inventoried(tmp_path):
    """The skip keys on the ``(keepout ...)`` child, never on ``zone`` itself.

    A real (filled) zone is plotted copper: footprint-embedded or board
    level, it must remain a required native object (#9234 AC).
    """
    inventoried = _inventoried(_board(tmp_path / "board.kicad_pcb", _REAL_ZONE_BOARD))
    assert _UUID[3] in inventoried  # real footprint-embedded zone
    assert _UUID[4] in inventoried  # real board-level zone
    assert _UUID[2] in inventoried


def test_source_descriptions_name_kind_and_parent_footprint(tmp_path):
    from kicad_tools.validate.mask_copper_geometry import _source_descriptions
    from kicad_tools.validate.mask_export_geometry import _inventory

    tree = _parse(_board(tmp_path / "board.kicad_pcb", _REAL_ZONE_BOARD))
    parents = {}
    for parent in tree.find_all("footprint") + tree.find_all("module"):
        field = parent.find_child("uuid")
        if field:
            parents[field.get_string(0)] = parent
    descriptions = _source_descriptions(_inventory(tree), parents)
    assert descriptions == {
        _UUID[2]: "pad:rect in footprint RF1",
        _UUID[3]: "zone in footprint RF1",
        _UUID[4]: "zone",
    }


def _worker_payload(errors, objects=None, board: Path | None = None):
    data = {
        "schema": "kct.native-mask-objects.v1",
        "native_version": "10.0.6",
        "errors": list(errors),
        "objects": objects or {},
        "max_error_mm": 0.0,
    }
    if board is not None:
        data["source_sha256"] = hashlib.sha256(board.read_bytes()).hexdigest()
    return json.dumps(data)


def test_missing_object_reasons_name_type_and_parent():
    from kicad_tools.validate.mask_copper_geometry import _read_worker_output

    named, silent = "uuid-named", "uuid-silent"
    data = _read_worker_output(
        _worker_payload([f"{named}: missing/ambiguous native object"]),
        {named, silent},
        {named: "zone in footprint RF1", silent: "pad:rect in footprint RF1"},
    )
    assert data["errors"] == [
        f"{named}: missing/ambiguous native object (zone in footprint RF1)",
        f"Missing native object: {silent} (pad:rect in footprint RF1)",
    ]
    # Without descriptions the #9009 shapes are unchanged.
    plain = _read_worker_output(
        _worker_payload([f"{named}: missing/ambiguous native object"]), {named, silent}
    )
    assert plain["errors"] == [
        f"{named}: missing/ambiguous native object",
        f"Missing native object: {silent}",
    ]


def _recording_fake_native(monkeypatch, *, requests: list):
    """Fake both native passes; record each worker ``request.json`` content."""

    def fake_run(cmd, **kw):
        cmd = list(cmd)
        if cmd[-1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "10.0.6\n", "")
        if "export" in cmd and "gerbers" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        board = Path(cmd[-3])
        requests.append(json.loads(Path(cmd[-2]).read_text()))
        Path(cmd[-1]).write_text(_worker_payload([], board=board))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)


def test_worker_request_omits_rule_areas_and_reasons_carry_parents(monkeypatch, tmp_path):
    """End to end on a faked native pair: the keepout uuid is never requested.

    Pre-fix behaviour: the footprint keepout landed in ``source_uuids`` and
    the worker's reply made the whole assessment incomplete with a bare-uuid
    reason. Post-fix only the pad is requested; the still-missing pad reason
    (this fake returns no objects at all) names its kind and parent.
    """
    from kicad_tools.validate.mask_copper_geometry import inspect_attributed_mask_geometry

    monkeypatch.setattr("kicad_tools.cli.kicad_toolchain.docker_fallback_enabled", lambda: False)
    monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: Path("/host/kicad-cli"))
    requests: list[dict] = []
    _recording_fake_native(monkeypatch, requests=requests)
    board = _board(tmp_path / "board.kicad_pcb", _KEEPOUT_BOARD)
    result = inspect_attributed_mask_geometry(
        board, native_python_command=["/host/python3"], scratch_dir=tmp_path
    )

    assert requests and requests[0]["source_uuids"] == [_UUID[2]]
    assert not [e for e in result.errors if _UUID[3] in e or _UUID[4] in e]
    assert f"Missing native object: {_UUID[2]} (pad:rect in footprint RF1)" in result.errors


def test_incomplete_mask_names_its_parity_category_without_widening_meta(tmp_path):
    """#9234: fab_dfm_parity names the category; the meta rollup does not move.

    ``meta_checks.incomplete_categories`` keeps its #9062 content (the run's
    incompleteness is already named there via ``mask_to_copper``); the new
    run-state entry lives in the ``fab_dfm_parity`` section itself, and the
    verdict/exit code stay keyed on the static ``not_covered`` rows.
    """
    from kicad_tools.cli.check_cmd import DRCResults, evaluate_check_verdict
    from kicad_tools.validate.fab_dfm_parity import format_parity_stanza
    from kicad_tools.validate.mask_copper import MaskCopperAssessment

    def verdict_for(coverage: str, manufacturer: str | None):
        results = DRCResults(
            mask_copper_assessments=[
                MaskCopperAssessment(
                    coverage=coverage,
                    reasons=[
                        f"{_UUID[3]}: missing/ambiguous native object (zone in footprint RF1)"
                    ],
                )
            ],
            rules_checked=1,
        )
        return evaluate_check_verdict(
            tmp_path / "x.kicad_pcb",
            results,
            mask_copper_default=True,
            manufacturer=manufacturer,
        )

    verdict = verdict_for("incomplete", "jlcpcb")
    assert "solder_mask_opening_exposing_trace" in verdict.fab_dfm_parity.incomplete_categories
    assert verdict.meta.incomplete_categories == ("mask_to_copper",)
    assert verdict.verdict == "incomplete"
    stanza = format_parity_stanza(verdict.fab_dfm_parity).splitlines()
    assert any("INCOMPLETE (this run): solder_mask_opening_exposing_trace" in ln for ln in stanza)
    assert not any("kct has no rule for the categories above" in ln for ln in stanza)

    # A completed mask check, or a non-JLCPCB run, names nothing.
    assert verdict_for("complete", "jlcpcb").fab_dfm_parity.incomplete_categories == ()
    assert verdict_for("incomplete", None).fab_dfm_parity is None
