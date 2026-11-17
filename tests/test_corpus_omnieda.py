"""Tests for the OmniEDA sample audit tool (issue #4830, slice 3).

``scripts/corpus/omnieda_sample.py`` is offline by construction -- the OmniLayout /
OmniRouting sample drops sit behind interactive Google Drive links and are
downloaded by hand -- so, unlike the slice-1/2 fetching scripts, it is safe for
the test suite to import. Everything below runs on synthetic records; no
third-party payload is vendored or fetched.

The properties worth pinning are the ones a future edit could quietly break:
flavor sniffing (a placement drop must not be mistaken for a routing drop, since
only the latter carries reference copper), and the gap census -- particularly
that an Eagle layer-19 "Unrouted" airwire is **not** counted as routed copper.
That single confusion would inflate any route-vs-human completion score.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "scripts" / "corpus"

# The corpus tooling lives under scripts/, not in the installed package (same
# convention as tests/test_corpus_manifest.py).
sys.path.insert(0, str(CORPUS_DIR))

from omnieda_sample import (  # noqa: E402  (sys.path manipulation above)
    FLAVOR_LAYOUT,
    FLAVOR_ROUTING,
    FLAVOR_UNKNOWN,
    GAP_AIRWIRE_AS_ROUTING,
    GAP_BLIND_BURIED_VIA,
    GAP_COPPER_POUR,
    GAP_CURVED_COPPER,
    GAP_INNER_LAYER,
    GAP_MISSING_ROTATION,
    GAP_NO_SCHEMATIC,
    GAP_NOTES,
    GAP_PLACEMENT_WITHOUT_GEOMETRY,
    GAP_TH_PAD_NO_DIAMETER,
    audit_record,
    load_sample,
    render_report,
    sniff_flavor,
)


def _placement_record(**overrides: Any) -> dict[str, Any]:
    """A minimal OmniLayout-shaped record with no gaps except the schematic one."""
    record: dict[str, Any] = {
        "netlist": [{"name": "GND", "contactref": [{"element": "R1", "pad": "1"}]}],
        "board_boundary": {
            "segment_details": [{"start": [0, 0], "end": [10, 0], "layer": "20", "curve": 0.0}],
            "unit": "mm",
            "closed": True,
            "dxf_details": [],
        },
        "ic_library": [
            {
                "element_name": "R1",
                "library_name": "lib",
                "package_name": "0603",
                "position": [1.0, 2.0],
                "rotation": "R90",
                "smd": [
                    {
                        "name": "1",
                        "position": [0, 0],
                        "width": 0.8,
                        "length": 0.8,
                        "rotation": "R0",
                        "layer": "1",
                    }
                ],
                "pads": [],
                "holes": [],
            }
        ],
        "ic_position": [{"element_name": "R1", "position": [1.0, 2.0], "rotation": "R90"}],
        "ic_dimension": [],
    }
    record.update(overrides)
    return record


def _routing_record(**overrides: Any) -> dict[str, Any]:
    record = _placement_record()
    record.update(
        {
            "routing_wires": [
                {
                    "start": [0, 0],
                    "end": [1, 0],
                    "width": 0.2,
                    "layer": "1",
                    "curve": 0.0,
                }
            ],
            "routing_vias": [{"pos": [1, 1], "drill": 0.3, "extent": "1-16"}],
            "copper_pours": [],
            "Clearance": {"unit": "mm", "rules": {"wire": 0.2, "restrict": 0.0}},
            "board_semantics": {
                "eagle_version": "9.6.2",
                "designrules": {"name": "Some-DRU"},
            },
        }
    )
    record.update(overrides)
    return record


class TestSniffFlavor:
    def test_placement_drop_is_not_mistaken_for_a_routing_drop(self) -> None:
        assert sniff_flavor(_placement_record()) == FLAVOR_LAYOUT

    def test_routing_keys_promote_the_flavor(self) -> None:
        assert sniff_flavor(_routing_record()) == FLAVOR_ROUTING

    def test_non_omnieda_payloads_are_unknown(self) -> None:
        assert sniff_flavor({"version": 20221018, "footprints": []}) == FLAVOR_UNKNOWN
        assert sniff_flavor([1, 2, 3]) == FLAVOR_UNKNOWN
        assert sniff_flavor("(kicad_pcb ...)") == FLAVOR_UNKNOWN

    def test_unknown_records_are_audited_but_empty(self) -> None:
        audit = audit_record("stray.json", {"nope": True})
        assert audit.flavor == FLAVOR_UNKNOWN
        assert audit.components == 0
        assert audit.gaps == {}


class TestAuditCounts:
    def test_clean_placement_record_reports_only_the_schematic_gap(self) -> None:
        audit = audit_record("b.json", _placement_record())
        assert audit.flavor == FLAVOR_LAYOUT
        assert (audit.components, audit.placements) == (1, 1)
        assert (audit.nets, audit.connections) == (1, 1)
        assert (audit.smd_pads, audit.th_pads) == (1, 0)
        assert audit.boundary_segments == 1
        assert audit.boundary_closed is True
        assert audit.gaps == {GAP_NO_SCHEMATIC: 1}

    def test_routing_record_captures_copper_and_rules(self) -> None:
        audit = audit_record("b.json", _routing_record())
        assert audit.flavor == FLAVOR_ROUTING
        assert audit.copper_wires == 1
        assert audit.copper_layers == ["1"]
        assert audit.vias == 1
        assert audit.clearance_mm == 0.2
        assert audit.eagle_version == "9.6.2"
        assert audit.designrule_set == "Some-DRU"

    def test_singleton_lists_collapsed_by_the_eagle_export_are_tolerated(self) -> None:
        # Eagle-derived JSON writes a bare object where a one-element list was.
        record = _placement_record()
        record["netlist"] = {
            "name": "GND",
            "contactref": {"element": "R1", "pad": "1"},
        }
        record["ic_library"] = record["ic_library"][0]
        record["ic_position"] = record["ic_position"][0]
        audit = audit_record("b.json", record)
        assert (audit.nets, audit.connections) == (1, 1)
        assert (audit.components, audit.placements) == (1, 1)
        assert audit.gaps == {GAP_NO_SCHEMATIC: 1}

    def test_a_schematic_payload_clears_the_schematic_gap(self) -> None:
        audit = audit_record("b.json", _placement_record(schematic={"sheets": []}))
        assert GAP_NO_SCHEMATIC not in audit.gaps


class TestGapCensus:
    def test_placement_without_geometry_is_counted(self) -> None:
        record = _placement_record()
        record["ic_position"].append(
            {"element_name": "GHOST", "position": [0, 0], "rotation": "R0"}
        )
        assert audit_record("b.json", record).gaps[GAP_PLACEMENT_WITHOUT_GEOMETRY] == 1

    def test_missing_rotation_is_counted(self) -> None:
        record = _placement_record()
        del record["ic_library"][0]["rotation"]
        assert audit_record("b.json", record).gaps[GAP_MISSING_ROTATION] == 1

    def test_through_hole_pad_without_diameter_is_counted(self) -> None:
        record = _placement_record()
        record["ic_library"][0]["pads"] = [
            {"name": "1", "position": [0, 0], "drill": 0.4},
            {"name": "2", "position": [1, 0], "drill": 0.4, "diameter": 0.9},
        ]
        audit = audit_record("b.json", record)
        assert audit.th_pads == 2
        assert audit.gaps[GAP_TH_PAD_NO_DIAMETER] == 1

    def test_layer_19_airwire_is_not_copper(self) -> None:
        record = _routing_record()
        record["routing_wires"].append(
            {"start": [0, 0], "end": [5, 5], "width": 0.0, "layer": "19", "curve": 0.0}
        )
        audit = audit_record("b.json", record)
        assert audit.copper_wires == 1  # the airwire is excluded
        assert audit.gaps[GAP_AIRWIRE_AS_ROUTING] == 1

    def test_curved_copper_is_counted_but_still_copper(self) -> None:
        record = _routing_record()
        record["routing_wires"].append(
            {"start": [0, 0], "end": [5, 5], "width": 0.2, "layer": "16", "curve": 42.0}
        )
        audit = audit_record("b.json", record)
        assert audit.copper_wires == 2
        assert audit.gaps[GAP_CURVED_COPPER] == 1

    def test_inner_layers_counted_once_per_layer(self) -> None:
        record = _routing_record()
        for layer in ("2", "2", "15", "16"):
            record["routing_wires"].append(
                {
                    "start": [0, 0],
                    "end": [1, 1],
                    "width": 0.2,
                    "layer": layer,
                    "curve": 0.0,
                }
            )
        audit = audit_record("b.json", record)
        assert audit.copper_layers == ["1", "2", "15", "16"]
        assert audit.gaps[GAP_INNER_LAYER] == 2

    def test_blind_or_buried_via_is_counted(self) -> None:
        record = _routing_record()
        record["routing_vias"].append({"pos": [2, 2], "drill": 0.3, "extent": "1-2"})
        audit = audit_record("b.json", record)
        assert audit.vias == 2
        assert audit.gaps[GAP_BLIND_BURIED_VIA] == 1

    def test_copper_pours_are_counted(self) -> None:
        record = _routing_record()
        record["copper_pours"] = [{"signal": "GND", "layer": "16"}]
        audit = audit_record("b.json", record)
        assert audit.pours == 1
        assert audit.gaps[GAP_COPPER_POUR] == 1

    def test_every_gap_constant_has_a_note(self) -> None:
        # A gap with no note is a number nobody can act on.
        record = _routing_record()
        record["ic_position"].append({"element_name": "GHOST", "position": [0, 0]})
        del record["ic_library"][0]["rotation"]
        record["ic_library"][0]["pads"] = [{"name": "1", "position": [0, 0], "drill": 0.4}]
        record["routing_wires"].append(
            {"start": [0, 0], "end": [1, 1], "width": 0.0, "layer": "19", "curve": 0.0}
        )
        record["routing_wires"].append(
            {"start": [0, 0], "end": [1, 1], "width": 0.2, "layer": "2", "curve": 9.0}
        )
        record["routing_vias"].append({"pos": [2, 2], "drill": 0.3, "extent": "1-2"})
        record["copper_pours"] = [{"signal": "GND", "layer": "16"}]
        audit = audit_record("b.json", record)
        assert set(audit.gaps) == set(GAP_NOTES)
        assert all(GAP_NOTES[key] for key in audit.gaps)


class TestSampleLoadingAndReport:
    def test_load_sample_reads_nested_json_and_survives_garbage(self, tmp_path: Path) -> None:
        (tmp_path / "nested").mkdir()
        good = tmp_path / "nested" / "board.json"
        good.write_text(json.dumps(_placement_record()), encoding="utf-8")
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        records = dict(load_sample(tmp_path))
        assert set(records) == {"broken.json", str(Path("nested") / "board.json")}
        assert "__error__" in records["broken.json"]
        assert sniff_flavor(records[str(Path("nested") / "board.json")]) == FLAVOR_LAYOUT

    def test_report_lists_boards_and_pooled_gaps(self) -> None:
        audits = [
            audit_record("alpha.json", _placement_record()),
            audit_record("beta.json", _routing_record()),
        ]
        text = render_report(audits)
        assert "alpha.json" in text and "beta.json" in text
        assert FLAVOR_ROUTING in text
        assert f"{GAP_NO_SCHEMATIC:<38}" in text
        assert "on 2/2 boards" in text

    def test_report_is_explicit_when_nothing_was_recognized(self) -> None:
        text = render_report([audit_record("x.json", {"nope": True})])
        assert "unrecognized: 1" in text
        assert "--sample" in text
