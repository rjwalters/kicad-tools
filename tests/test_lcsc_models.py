"""Tests for the LCSC/EasyEDA fetch-on-demand 3D model tier.

Covers the minimal in-repo EasyEDA client (mocked, no real network),
cache hit/miss semantics, offline no-op safety, the fourth resolver tier in
``make_library_resolver``, ``${KCT_LCSC_3D_DIR}`` path-variable emission,
skip-on-miss (no dangling ref), and origin-authored offset math parity with
the #4045 offset machinery.

No test in this file makes a real network call: ``urllib.request.urlopen`` is
patched everywhere fetching is exercised, and a guard test asserts the
offline/cache-only path never even constructs a request.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.footprints.library_path import LibraryPaths
from kicad_tools.pcb import lcsc_model_transforms
from kicad_tools.pcb.lcsc_model_transforms import (
    LCSC_MODEL_TRANSFORMS,
    LcscModelTransform,
    TransformProvenance,
    entry_problems,
    lookup_transform,
    resolve_merged_transform,
    table_problems,
)
from kicad_tools.pcb.lcsc_models import (
    DEFAULT_CACHE_ENV_VAR,
    LCSC_MODEL_PATH_VAR,
    LcscModelEntry,
    _fetch_lcsc_step,
    _fmt_num,
    _parse_3d_uuid,
    fetch_enabled,
    lcsc_cache_dir,
    load_lcsc_mapping,
    resolve_lcsc_step,
    synthesize_model_block,
)
from kicad_tools.pcb.models3d import (
    add_model_refs_to_text,
    make_library_resolver,
)

# --------------------------------------------------------------------------
# Fixtures: a mocked EasyEDA component-info body + fake STEP bytes
# --------------------------------------------------------------------------

FAKE_UUID = "7b135d4c7d084b658994bacec4f3b635"
FAKE_STEP = b"ISO-10303-21;\r\nHEADER;\r\n/* fake step body */\r\nEND-ISO-10303-21;\r\n"

COMPONENT_INFO = json.dumps(
    {
        "success": True,
        "result": {
            "packageDetail": {
                "dataStr": {
                    "shape": [
                        "TRACK~0.5~1~gge1~100 100 200 100",
                        "SVGNODE~"
                        + json.dumps(
                            {
                                "gId": "gge5",
                                "attrs": {
                                    "c_width": "10",
                                    "uuid": FAKE_UUID,
                                    "title": "FakePart",
                                },
                            }
                        ),
                    ]
                }
            }
        },
    }
).encode()


def _urlopen_router(responses: dict[str, bytes]):
    """Build a fake ``urlopen`` returning bytes keyed by URL substring.

    Each value is served as an object with ``.read()`` usable as a context
    manager, matching ``urllib.request.urlopen``'s contract.
    """
    calls: list[str] = []

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        for needle, body in responses.items():
            if needle in url:
                return io.BytesIO(body)
        raise AssertionError(f"unexpected URL fetched: {url}")

    fake_urlopen.calls = calls  # type: ignore[attr-defined]
    return fake_urlopen


# --------------------------------------------------------------------------
# uuid parsing
# --------------------------------------------------------------------------


class TestParseUuid:
    def test_extracts_uuid_from_svgnode(self):
        assert _parse_3d_uuid(COMPONENT_INFO) == FAKE_UUID

    def test_top_level_uuid_fallback(self):
        body = json.dumps(
            {
                "result": {
                    "packageDetail": {
                        "dataStr": {"shape": ["SVGNODE~" + json.dumps({"uuid": "abc123"})]}
                    }
                }
            }
        ).encode()
        assert _parse_3d_uuid(body) == "abc123"

    def test_no_svgnode_returns_none(self):
        body = json.dumps(
            {"result": {"packageDetail": {"dataStr": {"shape": ["TRACK~x"]}}}}
        ).encode()
        assert _parse_3d_uuid(body) is None

    def test_malformed_json_returns_none(self):
        assert _parse_3d_uuid(b"not json") is None

    def test_missing_keys_return_none(self):
        assert _parse_3d_uuid(json.dumps({"result": {}}).encode()) is None
        assert _parse_3d_uuid(json.dumps({}).encode()) is None


# --------------------------------------------------------------------------
# Fetch client (mocked network)
# --------------------------------------------------------------------------


class TestFetchClient:
    def test_two_call_fetch_success(self):
        fake = _urlopen_router(
            {"/api/products/C50950/components": COMPONENT_INFO, FAKE_UUID: FAKE_STEP}
        )
        with mock.patch("urllib.request.urlopen", fake):
            step = _fetch_lcsc_step("C50950")
        assert step == FAKE_STEP
        # Two GETs: component-info then STEP.
        assert len(fake.calls) == 2
        assert "/api/products/C50950/components" in fake.calls[0]
        assert FAKE_UUID in fake.calls[1]

    def test_component_info_http_failure_returns_none(self):
        def boom(req, timeout=None):  # noqa: ANN001
            raise OSError("network down")

        with mock.patch("urllib.request.urlopen", boom):
            assert _fetch_lcsc_step("C50950") is None

    def test_no_uuid_skips_step_fetch(self):
        empty = json.dumps({"result": {"packageDetail": {"dataStr": {"shape": []}}}}).encode()
        fake = _urlopen_router({"/api/products/C1/components": empty})
        with mock.patch("urllib.request.urlopen", fake):
            assert _fetch_lcsc_step("C1") is None
        # Only the component-info call happened; STEP endpoint never hit.
        assert len(fake.calls) == 1

    def test_step_fetch_failure_returns_none(self):
        def fake_urlopen(req, timeout=None):  # noqa: ANN001
            url = req.full_url
            if "components" in url:
                return io.BytesIO(COMPONENT_INFO)
            raise OSError("step endpoint down")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            assert _fetch_lcsc_step("C50950") is None

    def test_oversize_step_body_rejected(self):
        # A rogue endpoint returns a body over the size cap: reject, don't cache.
        huge = b"ISO-10303-21;" + b"\x00" * (50 * 1024 * 1024 + 1)
        fake = _urlopen_router({"/api/products/C50950/components": COMPONENT_INFO, FAKE_UUID: huge})
        with mock.patch("urllib.request.urlopen", fake):
            assert _fetch_lcsc_step("C50950") is None

    def test_non_step_body_rejected(self):
        # A body without the ISO-10303-21 header is not a STEP file: reject.
        not_step = b"<html>totally not a step file</html>"
        fake = _urlopen_router(
            {"/api/products/C50950/components": COMPONENT_INFO, FAKE_UUID: not_step}
        )
        with mock.patch("urllib.request.urlopen", fake):
            assert _fetch_lcsc_step("C50950") is None


# --------------------------------------------------------------------------
# Cache-aware resolution
# --------------------------------------------------------------------------


class TestResolveCache:
    def test_cache_hit_makes_no_network_call(self, tmp_path):
        (tmp_path / "C50950.step").write_bytes(FAKE_STEP)

        def forbidden(req, timeout=None):  # noqa: ANN001
            raise AssertionError("network called on a cache hit")

        with mock.patch("urllib.request.urlopen", forbidden):
            path = resolve_lcsc_step("C50950", cache_dir=tmp_path, fetch=True)
        assert path == tmp_path / "C50950.step"

    def test_cache_miss_fetch_disabled_is_offline_noop(self, tmp_path):
        def forbidden(req, timeout=None):  # noqa: ANN001
            raise AssertionError("network called with fetch disabled")

        with mock.patch("urllib.request.urlopen", forbidden):
            assert resolve_lcsc_step("C50950", cache_dir=tmp_path, fetch=False) is None
        assert not (tmp_path / "C50950.step").exists()

    def test_cache_miss_fetch_enabled_writes_then_hits(self, tmp_path):
        fake = _urlopen_router(
            {"/api/products/C50950/components": COMPONENT_INFO, FAKE_UUID: FAKE_STEP}
        )
        with mock.patch("urllib.request.urlopen", fake):
            path = resolve_lcsc_step("C50950", cache_dir=tmp_path, fetch=True)
        assert path == tmp_path / "C50950.step"
        assert path.read_bytes() == FAKE_STEP
        assert len(fake.calls) == 2

        # Second resolution is a pure cache hit — no further network.
        def forbidden(req, timeout=None):  # noqa: ANN001
            raise AssertionError("second resolve should hit cache")

        with mock.patch("urllib.request.urlopen", forbidden):
            path2 = resolve_lcsc_step("C50950", cache_dir=tmp_path, fetch=True)
        assert path2 == path

    def test_fetch_failure_warns_and_returns_none(self, tmp_path):
        warnings: list[str] = []

        def boom(req, timeout=None):  # noqa: ANN001
            raise OSError("down")

        with mock.patch("urllib.request.urlopen", boom):
            path = resolve_lcsc_step("C50950", cache_dir=tmp_path, fetch=True, warn=warnings.append)
        assert path is None
        assert warnings and "C50950" in warnings[0]

    @pytest.mark.parametrize(
        "bad_id",
        [
            "../outside/pwned",  # path traversal into a sibling dir
            "C1/../../evil?x=",  # URL/path injection
            "../../../etc/whatever",  # ref-injection style traversal
            "not-a-c-number",  # non-C value
            "C",  # 'C' with no digits
            "",  # empty
        ],
    )
    def test_invalid_id_never_touches_fs_or_network(self, tmp_path, bad_id):
        # A malicious/malformed id must warn + return None and never write a
        # file, format a URL, or escape the cache dir.
        warnings: list[str] = []

        def forbidden(req, timeout=None):  # noqa: ANN001
            raise AssertionError("invalid id must not reach the network")

        with mock.patch("urllib.request.urlopen", forbidden):
            path = resolve_lcsc_step(bad_id, cache_dir=tmp_path, fetch=True, warn=warnings.append)
        assert path is None
        assert warnings and "invalid C-number" in warnings[0]
        # Nothing was written anywhere under (or above) the cache dir.
        assert not any(tmp_path.rglob("*.step"))
        assert not (tmp_path.parent / "outside" / "pwned.step").exists()


# --------------------------------------------------------------------------
# Cache dir / fetch-flag env resolution
# --------------------------------------------------------------------------


class TestConfig:
    def test_cache_dir_default(self, monkeypatch):
        monkeypatch.delenv(DEFAULT_CACHE_ENV_VAR, raising=False)
        assert lcsc_cache_dir() == Path.home() / ".cache" / "kicad-tools" / "lcsc-3d"

    def test_cache_dir_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv(DEFAULT_CACHE_ENV_VAR, str(tmp_path))
        assert lcsc_cache_dir() == tmp_path

    def test_fetch_enabled_flag(self, monkeypatch):
        monkeypatch.delenv("KCT_LCSC_FETCH", raising=False)
        assert fetch_enabled(True) is True
        assert fetch_enabled(False) is False

    @pytest.mark.parametrize("val", ["1", "true", "YES", "on"])
    def test_fetch_enabled_env_truthy(self, monkeypatch, val):
        monkeypatch.setenv("KCT_LCSC_FETCH", val)
        assert fetch_enabled(False) is True

    @pytest.mark.parametrize("val", ["0", "false", "", "no"])
    def test_fetch_enabled_env_falsy(self, monkeypatch, val):
        monkeypatch.setenv("KCT_LCSC_FETCH", val)
        assert fetch_enabled(False) is False


# --------------------------------------------------------------------------
# Sidecar loading
# --------------------------------------------------------------------------


class TestSidecar:
    def test_loads_lib_id_to_cnumber(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(json.dumps({"Module:Joystick_Analog": "C50950"}))
        assert load_lcsc_mapping(sidecar) == {
            "Module:Joystick_Analog": LcscModelEntry(lcsc="C50950")
        }

    def test_bare_string_form_carries_no_transform(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(json.dumps({"Module:Joystick_Analog": "C50950"}))
        entry = load_lcsc_mapping(sidecar)["Module:Joystick_Analog"]
        assert entry.rotate is None
        assert entry.offset is None

    def test_malformed_json_raises_value_error(self, tmp_path):
        sidecar = tmp_path / "bad.json"
        sidecar.write_text("{not json")
        with pytest.raises(ValueError, match="malformed"):
            load_lcsc_mapping(sidecar)

    def test_non_object_raises(self, tmp_path):
        sidecar = tmp_path / "list.json"
        sidecar.write_text("[1, 2]")
        with pytest.raises(ValueError, match="must be a JSON object"):
            load_lcsc_mapping(sidecar)

    def test_non_string_values_raise(self, tmp_path):
        sidecar = tmp_path / "num.json"
        sidecar.write_text(json.dumps({"Lib:Name": 123}))
        with pytest.raises(ValueError, match="string"):
            load_lcsc_mapping(sidecar)

    @pytest.mark.parametrize(
        "bad_value",
        [
            "../outside/pwned",  # path traversal
            "C1/../../evil?x=",  # URL injection
            "not-a-c-number",  # non-C value
            "C",  # 'C' with no digits
            "c50950",  # lowercase 'c'
        ],
    )
    def test_invalid_cnumber_is_build_error(self, tmp_path, bad_value):
        # An untrusted, malformed C-number in a committed sidecar must raise
        # (build error), never flow silently into the cache/URL/ref sinks.
        sidecar = tmp_path / "bad_cnumber.json"
        sidecar.write_text(json.dumps({"Module:Joystick_Analog": bad_value}))
        with pytest.raises(ValueError, match="invalid C-number"):
            load_lcsc_mapping(sidecar)


# --------------------------------------------------------------------------
# Sidecar object form: per-part rotate/offset overrides (#4584)
# --------------------------------------------------------------------------


class TestSidecarObjectForm:
    def test_object_form_with_rotate_and_offset(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(
            json.dumps(
                {
                    "Connector_PCIE:PCIE_Mini_Edge": {
                        "lcsc": "C444929",
                        "rotate": [0, 0, -90],
                        "offset": [1.5, -2, 0.25],
                    }
                }
            )
        )
        assert load_lcsc_mapping(sidecar) == {
            "Connector_PCIE:PCIE_Mini_Edge": LcscModelEntry(
                lcsc="C444929", rotate=(0.0, 0.0, -90.0), offset=(1.5, -2.0, 0.25)
            )
        }

    def test_object_form_fields_are_independently_optional(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(
            json.dumps(
                {
                    "A:RotateOnly": {"lcsc": "C1", "rotate": [0, 0, 90]},
                    "B:OffsetOnly": {"lcsc": "C2", "offset": [0, 0, 1]},
                    "C:Neither": {"lcsc": "C3"},
                }
            )
        )
        mapping = load_lcsc_mapping(sidecar)
        assert mapping["A:RotateOnly"] == LcscModelEntry("C1", rotate=(0.0, 0.0, 90.0))
        assert mapping["B:OffsetOnly"] == LcscModelEntry("C2", offset=(0.0, 0.0, 1.0))
        assert mapping["C:Neither"] == LcscModelEntry("C3")

    def test_mixed_bare_and_object_forms_in_one_sidecar(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(
            json.dumps(
                {
                    "Module:Joystick_Analog": "C50950",
                    "Connector_PCIE:PCIE_Mini_Edge": {"lcsc": "C444929", "rotate": [0, 0, -90]},
                }
            )
        )
        mapping = load_lcsc_mapping(sidecar)
        assert mapping["Module:Joystick_Analog"] == LcscModelEntry("C50950")
        assert mapping["Connector_PCIE:PCIE_Mini_Edge"].rotate == (0.0, 0.0, -90.0)

    def test_object_form_validates_cnumber_like_bare_form(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(json.dumps({"Lib:Name": {"lcsc": "../outside/pwned"}}))
        with pytest.raises(ValueError, match="invalid C-number"):
            load_lcsc_mapping(sidecar)

    def test_object_form_requires_lcsc_key(self, tmp_path):
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(json.dumps({"Lib:Name": {"rotate": [0, 0, 90]}}))
        with pytest.raises(ValueError, match="must contain a string 'lcsc'"):
            load_lcsc_mapping(sidecar)

    def test_unknown_key_is_rejected_not_ignored(self, tmp_path):
        # A typo'd "rotation" must fail loudly.  Silently ignoring it would
        # produce a green no-op -- exactly the failure this mechanism exists
        # to prevent.
        sidecar = tmp_path / "lcsc_models.json"
        sidecar.write_text(json.dumps({"Lib:Name": {"lcsc": "C1", "rotation": [0, 0, 90]}}))
        with pytest.raises(ValueError, match="unknown key"):
            load_lcsc_mapping(sidecar)

    @pytest.mark.parametrize(
        ("bad", "pattern"),
        [
            ([0, 0], "exactly 3 numbers"),
            ([0, 0, 0, 0], "exactly 3 numbers"),
            ("0,0,90", "exactly 3 numbers"),
            ({"z": 90}, "exactly 3 numbers"),
            ([0, 0, "90"], "only numbers"),
            ([0, 0, True], "only numbers"),
            ([0, 0, float("nan")], "only finite numbers"),
            ([0, float("inf"), 0], "only finite numbers"),
        ],
    )
    def test_malformed_triples_are_build_errors(self, tmp_path, bad, pattern):
        sidecar = tmp_path / "lcsc_models.json"
        # NaN/inf are not valid JSON but json.dumps emits them by default and
        # json.loads accepts them back -- exercise the loader's own guard.
        sidecar.write_text(json.dumps({"Lib:Name": {"lcsc": "C1", "rotate": bad}}))
        with pytest.raises(ValueError, match=pattern):
            load_lcsc_mapping(sidecar)

    def test_error_message_names_sidecar_path_and_key(self, tmp_path):
        sidecar = tmp_path / "board_sidecar.json"
        sidecar.write_text(json.dumps({"Lib:Name": {"lcsc": "C1", "offset": [0, 0]}}))
        with pytest.raises(ValueError) as exc:
            load_lcsc_mapping(sidecar)
        message = str(exc.value)
        assert str(sidecar) in message
        assert "offset" in message
        assert "Lib:Name" in message


# --------------------------------------------------------------------------
# Synthesized model block
# --------------------------------------------------------------------------

# The exact bytes the LCSC tier emitted before per-part overrides existed
# (#4584).  Pinned verbatim: with no override the output must not drift by a
# single byte, because every committed board artifact carries these.
IDENTITY_BLOCK = (
    '(model "${KCT_LCSC_3D_DIR}/C50950.step"\n'
    "\t(offset\n"
    "\t\t(xyz 0 0 0)\n"
    "\t)\n"
    "\t(scale\n"
    "\t\t(xyz 1 1 1)\n"
    "\t)\n"
    "\t(rotate\n"
    "\t\t(xyz 0 0 0)\n"
    "\t)\n"
    ")"
)


class TestSynthesize:
    def test_uses_portable_path_variable(self):
        block = synthesize_model_block("C50950")
        assert block.startswith(f'(model "{LCSC_MODEL_PATH_VAR}/C50950.step"')
        assert "${KCT_LCSC_3D_DIR}" in block
        assert "(offset" in block and "(xyz 0 0 0)" in block
        assert block.endswith(")")

    def test_no_override_is_byte_identical_to_the_historical_output(self):
        assert synthesize_model_block("C50950") == IDENTITY_BLOCK

    def test_explicit_zero_triples_are_indistinguishable_from_identity(self):
        # An author writing rotate=[0,0,0] must not produce different bytes
        # from omitting it -- otherwise a no-op entry would churn artifacts.
        assert (
            synthesize_model_block("C50950", rotate=(0.0, 0.0, 0.0), offset=(0.0, 0.0, 0.0))
            == IDENTITY_BLOCK
        )

    def test_rotate_only_golden(self):
        assert synthesize_model_block("C444929", rotate=(0.0, 0.0, -90.0)) == (
            '(model "${KCT_LCSC_3D_DIR}/C444929.step"\n'
            "\t(offset\n"
            "\t\t(xyz 0 0 0)\n"
            "\t)\n"
            "\t(scale\n"
            "\t\t(xyz 1 1 1)\n"
            "\t)\n"
            "\t(rotate\n"
            "\t\t(xyz 0 0 -90)\n"
            "\t)\n"
            ")"
        )

    def test_offset_only_golden(self):
        assert synthesize_model_block("C444929", offset=(1.5, -2.0, 0.25)) == (
            '(model "${KCT_LCSC_3D_DIR}/C444929.step"\n'
            "\t(offset\n"
            "\t\t(xyz 1.5 -2 0.25)\n"
            "\t)\n"
            "\t(scale\n"
            "\t\t(xyz 1 1 1)\n"
            "\t)\n"
            "\t(rotate\n"
            "\t\t(xyz 0 0 0)\n"
            "\t)\n"
            ")"
        )

    def test_rotate_and_offset_golden(self):
        assert synthesize_model_block(
            "C444929", rotate=(0.0, 0.0, -90.0), offset=(1.5, -2.0, 0.25)
        ) == (
            '(model "${KCT_LCSC_3D_DIR}/C444929.step"\n'
            "\t(offset\n"
            "\t\t(xyz 1.5 -2 0.25)\n"
            "\t)\n"
            "\t(scale\n"
            "\t\t(xyz 1 1 1)\n"
            "\t)\n"
            "\t(rotate\n"
            "\t\t(xyz 0 0 -90)\n"
            "\t)\n"
            ")"
        )

    def test_scale_is_never_touched_by_an_override(self):
        block = synthesize_model_block("C1", rotate=(1.0, 2.0, 3.0), offset=(4.0, 5.0, 6.0))
        assert "\t(scale\n\t\t(xyz 1 1 1)\n\t)\n" in block

    @pytest.mark.parametrize(
        "value", [0.0, -0.0, 1.0, -90.0, 1.5, -2.0, 0.25, 1.2699999999, 1e-9, 123456.789]
    )
    def test_number_formatting_matches_the_offset_machinery(self, value):
        # The synthesized block and a block ``_apply_offset_delta`` has
        # rewritten must format numbers identically, or an override would
        # change bytes purely by being re-run through the other formatter.
        from kicad_tools.pcb.models3d import _fmt_num as models3d_fmt_num

        assert _fmt_num(value) == models3d_fmt_num(value)


# --------------------------------------------------------------------------
# Packaged per-part transform table (#4584)
# --------------------------------------------------------------------------


GOOD_PROVENANCE = TransformProvenance(
    board="boards/06-diffpair-test",
    refdes="J3",
    verified="2026-08-04",
    command="kct render boards/06-diffpair-test",
)


class TestPackagedTransformTable:
    def test_every_entry_carries_render_provenance(self):
        """CI cannot render, so an entry may only enter the table if a human
        did -- and recorded it.  This is the structural gate that keeps an
        uncalibrated guess from landing green (the #4457 failure mode)."""
        problems = table_problems()
        assert problems == {}, f"unfit packaged transform entries: {problems}"

    def test_table_is_c_number_keyed(self):
        for key in LCSC_MODEL_TRANSFORMS:
            assert re.match(r"^C\d+$", key), f"table key {key!r} is not a C-number"

    def test_lookup_returns_none_for_unlisted_part(self):
        assert lookup_transform("C999999999") is None

    def test_calibrated_pcie_mini_edge_entry(self):
        # The part that motivated the mechanism (#4584): mini-PCIe edge socket
        # whose EasyEDA body is authored a quarter turn off its pad column.
        entry = lookup_transform("C444929")
        assert entry is not None
        assert entry.rotate == (0.0, 0.0, -90.0)
        assert entry.provenance is not None
        assert entry.provenance.refdes == "J3"

    # ---- the enforcing test must actually reject bad entries -------------

    def test_entry_without_provenance_is_rejected(self):
        problems = entry_problems("C1", LcscModelTransform(rotate=(0.0, 0.0, 90.0)))
        assert any("missing provenance" in p for p in problems)

    @pytest.mark.parametrize(
        ("prov", "pattern"),
        [
            (TransformProvenance("", "J3", "2026-08-04", "kct render x"), "provenance.board"),
            (TransformProvenance("b", "  ", "2026-08-04", "kct render x"), "provenance.refdes"),
            (TransformProvenance("b", "J3", "2026-08-04", ""), "provenance.command"),
            (TransformProvenance("b", "J3", "yesterday", "kct render x"), "provenance.verified"),
            (TransformProvenance("b", "J3", "2026-13-04", "kct render x"), "provenance.verified"),
            (TransformProvenance("b", "J3", "04/08/2026", "kct render x"), "provenance.verified"),
        ],
    )
    def test_incomplete_provenance_is_rejected(self, prov, pattern):
        problems = entry_problems(
            "C1", LcscModelTransform(rotate=(0.0, 0.0, 90.0), provenance=prov)
        )
        assert any(pattern in p for p in problems), problems

    def test_no_op_entry_is_rejected(self):
        problems = entry_problems("C1", LcscModelTransform(provenance=GOOD_PROVENANCE))
        assert any("neither rotate nor offset" in p for p in problems)

    def test_non_c_number_key_is_rejected(self):
        problems = entry_problems(
            "Connector_PCIE:PCIE_Mini_Edge",
            LcscModelTransform(rotate=(0.0, 0.0, 90.0), provenance=GOOD_PROVENANCE),
        )
        assert any("is not a C-number" in p for p in problems)

    @pytest.mark.parametrize(
        "bad_rotate",
        [(0.0, 0.0), (0.0, 0.0, 0.0, 0.0), [0.0, 0.0, 90.0], (0.0, 0.0, float("nan")), "0,0,90"],
    )
    def test_malformed_triple_is_rejected(self, bad_rotate):
        problems = entry_problems(
            "C1", LcscModelTransform(rotate=bad_rotate, provenance=GOOD_PROVENANCE)
        )
        assert problems

    def test_table_walk_fails_when_an_uncalibrated_entry_is_added(self, monkeypatch):
        """The end-to-end proof that the gate bites: inject a guessed entry
        with no provenance and the same check that guards the real table
        reports it."""
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C7654321",
            LcscModelTransform(rotate=(0.0, 0.0, 90.0)),
        )
        problems = table_problems()
        assert "C7654321" in problems
        assert any("missing provenance" in p for p in problems["C7654321"])


# --------------------------------------------------------------------------
# Fourth resolver tier integration (via make_library_resolver)
# --------------------------------------------------------------------------

# A board footprint with a synthetic lib id no installed library covers, pads
# centered off origin so the offset math has a non-trivial delta.
PCB_LCSC = """(kicad_pcb
\t(version 20240108)
\t(generator "kicad_tools")
\t(footprint "Module:Joystick_Analog"
\t\t(layer "F.Cu")
\t\t(at 50 50)
\t\t(pad "1" thru_hole circle
\t\t\t(at 2.0 -1.27)
\t\t\t(size 1.7 1.7)
\t\t\t(drill 1.0)
\t\t\t(layers "*.Cu" "*.Mask")
\t\t)
\t)
)
"""


# An empty (no-pad) footprints tree: forces the installed-library tiers to
# miss so the LCSC tier is the only one that can resolve.
def _empty_library(tmp_path: Path) -> LibraryPaths:
    root = tmp_path / "footprints"
    root.mkdir(parents=True)
    return LibraryPaths(footprints_path=root, source="config")


class TestLcscTier:
    def test_cache_hit_resolves_and_emits_portable_ref(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "C50950.step").write_bytes(FAKE_STEP)
        lib = _empty_library(tmp_path)
        log: dict[str, str] = {}
        resolver = make_library_resolver(
            lib,
            lcsc_mapping={"Module:Joystick_Analog": "C50950"},
            lcsc_cache_dir=cache,
            lcsc_fetch=False,
            lcsc_log=log,
        )
        new_text, report = add_model_refs_to_text(PCB_LCSC, resolver)

        assert report.patched == ["Module:Joystick_Analog"]
        assert log == {"Module:Joystick_Analog": "C50950"}
        assert "${KCT_LCSC_3D_DIR}/C50950.step" in new_text
        # A portable variable, never an absolute cache path.
        assert str(cache) not in new_text

    def test_skip_on_miss_reports_unresolved_no_dangling_ref(self, tmp_path):
        # Empty cache, fetch disabled: the tier must skip, not emit a ref.
        cache = tmp_path / "cache"
        cache.mkdir()
        lib = _empty_library(tmp_path)
        resolver = make_library_resolver(
            lib,
            lcsc_mapping={"Module:Joystick_Analog": "C50950"},
            lcsc_cache_dir=cache,
            lcsc_fetch=False,
        )

        def forbidden(req, timeout=None):  # noqa: ANN001
            raise AssertionError("offline no-op must not touch the network")

        with mock.patch("urllib.request.urlopen", forbidden):
            new_text, report = add_model_refs_to_text(PCB_LCSC, resolver)

        assert report.patched == []
        assert report.unresolved == ["Module:Joystick_Analog"]
        assert "(model " not in new_text
        assert new_text == PCB_LCSC  # pure no-op

    def test_no_mapping_entry_is_unresolved(self, tmp_path):
        lib = _empty_library(tmp_path)
        resolver = make_library_resolver(lib, lcsc_mapping={"Other:Part": "C1"})
        _, report = add_model_refs_to_text(PCB_LCSC, resolver)
        assert report.unresolved == ["Module:Joystick_Analog"]

    def test_fetch_enabled_writes_cache_and_patches(self, tmp_path):
        cache = tmp_path / "cache"
        lib = _empty_library(tmp_path)
        fake = _urlopen_router(
            {"/api/products/C50950/components": COMPONENT_INFO, FAKE_UUID: FAKE_STEP}
        )
        resolver = make_library_resolver(
            lib,
            lcsc_mapping={"Module:Joystick_Analog": "C50950"},
            lcsc_cache_dir=cache,
            lcsc_fetch=True,
        )
        with mock.patch("urllib.request.urlopen", fake):
            new_text, report = add_model_refs_to_text(PCB_LCSC, resolver)
        assert report.patched == ["Module:Joystick_Analog"]
        assert (cache / "C50950.step").read_bytes() == FAKE_STEP
        assert "${KCT_LCSC_3D_DIR}/C50950.step" in new_text


# --------------------------------------------------------------------------
# Offset math: origin-authored source_anchor composes with #4045 machinery
# --------------------------------------------------------------------------


class TestLcscOffset:
    def test_resolver_returns_origin_source_anchor(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "C50950.step").write_bytes(FAKE_STEP)
        lib = _empty_library(tmp_path)
        resolver = make_library_resolver(
            lib,
            lcsc_mapping={"Module:Joystick_Analog": "C50950"},
            lcsc_cache_dir=cache,
        )
        resolved = resolver("Module:Joystick_Analog")
        assert resolved is not None
        # Explicit origin (NOT None) so the delta becomes the full target
        # pad centroid, not a zero shift.
        assert resolved.source_anchor == (0.0, 0.0)

    def test_offset_equals_target_pad_centroid_with_y_negated(self, tmp_path):
        # Single pad at (2.0, -1.27): centroid == (2.0, -1.27); the emitted
        # model offset must be (2.0, +1.27, 0) — Y negated per the model frame.
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "C50950.step").write_bytes(FAKE_STEP)
        lib = _empty_library(tmp_path)
        resolver = make_library_resolver(
            lib,
            lcsc_mapping={"Module:Joystick_Analog": "C50950"},
            lcsc_cache_dir=cache,
        )
        new_text, report = add_model_refs_to_text(PCB_LCSC, resolver)
        assert report.patched == ["Module:Joystick_Analog"]
        # The inserted model's own offset carries the target pad-centroid delta.
        assert "(xyz 2 1.27 0)" in new_text


# --------------------------------------------------------------------------
# Per-part transform precedence: sidecar > packaged table > identity (#4584)
# --------------------------------------------------------------------------


def _lcsc_resolver(tmp_path, mapping, c_number="C50950"):
    """A tier-4-only resolver whose cache already holds *c_number*."""
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    (cache / f"{c_number}.step").write_bytes(FAKE_STEP)
    return make_library_resolver(
        _empty_library(tmp_path),
        lcsc_mapping=mapping,
        lcsc_cache_dir=cache,
    )


class TestTransformPrecedence:
    LIB_ID = "Module:Joystick_Analog"

    def test_no_entry_anywhere_stays_identity(self, tmp_path):
        resolver = _lcsc_resolver(tmp_path, {self.LIB_ID: "C50950"})
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 0)\n" in resolved.models[0]

    def test_packaged_table_applies_to_a_bare_string_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(rotate=(0.0, 0.0, -90.0), provenance=GOOD_PROVENANCE),
        )
        resolver = _lcsc_resolver(tmp_path, {self.LIB_ID: "C50950"})
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 -90)\n" in resolved.models[0]

    def test_sidecar_object_form_wins_over_the_packaged_table(self, tmp_path, monkeypatch):
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(rotate=(0.0, 0.0, -90.0), provenance=GOOD_PROVENANCE),
        )
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 180.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 180)\n" in resolved.models[0]

    def test_sidecar_object_form_applies_when_the_table_is_silent(self, tmp_path):
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 45.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 45)\n" in resolved.models[0]

    def test_merge_is_per_field_not_all_or_nothing(self, tmp_path, monkeypatch):
        # A sidecar overriding only rotate must NOT suppress a packaged offset.
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(
                rotate=(0.0, 0.0, -90.0), offset=(0.0, 0.0, 1.25), provenance=GOOD_PROVENANCE
            ),
        )
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 180.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 180)\n" in resolved.models[0]
        assert "(offset\n\t\t(xyz 0 0 1.25)\n" in resolved.models[0]

    def test_merge_is_per_field_in_the_other_direction(self, tmp_path, monkeypatch):
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(
                rotate=(0.0, 0.0, -90.0), offset=(0.0, 0.0, 1.25), provenance=GOOD_PROVENANCE
            ),
        )
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", offset=(0.0, 0.0, 3.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 -90)\n" in resolved.models[0]
        assert "(offset\n\t\t(xyz 0 0 3)\n" in resolved.models[0]

    def test_transform_log_names_each_field_source(self, tmp_path, monkeypatch):
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(
                rotate=(0.0, 0.0, -90.0), offset=(0.0, 0.0, 1.25), provenance=GOOD_PROVENANCE
            ),
        )
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "C50950.step").write_bytes(FAKE_STEP)
        log: dict[str, str] = {}
        resolver = make_library_resolver(
            _empty_library(tmp_path),
            lcsc_mapping={self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 180.0))},
            lcsc_cache_dir=cache,
            lcsc_transform_log=log,
        )
        resolver(self.LIB_ID)
        assert log == {
            self.LIB_ID: "rotate=(0, 0, 180) [sidecar] offset=(0, 0, 1.25) [packaged table]"
        }

    def test_identity_leaves_the_transform_log_empty(self, tmp_path):
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "C50950.step").write_bytes(FAKE_STEP)
        log: dict[str, str] = {}
        resolver = make_library_resolver(
            _empty_library(tmp_path),
            lcsc_mapping={self.LIB_ID: "C50950"},
            lcsc_cache_dir=cache,
            lcsc_transform_log=log,
        )
        resolver(self.LIB_ID)
        assert log == {}


class TestSplitCalibrationGuard:
    """A packaged ``offset`` is calibrated for its sibling ``rotate`` (#4636).

    ``offset`` is applied *after* ``rotate``, so an offset measured in one
    frame points somewhere else in another.  A sidecar that changes ``rotate``
    while silently inheriting such an offset must fail loudly instead of
    emitting an almost-right body -- the worst failure mode for a transform
    whose only real validation is a human looking at a render.
    """

    LIB_ID = "Module:Joystick_Analog"

    @staticmethod
    def _packaged(monkeypatch, **kwargs):
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(provenance=GOOD_PROVENANCE, **kwargs),
        )

    # -- the defect ---------------------------------------------------------

    def test_sidecar_rotate_inheriting_an_xy_offset_raises(self, tmp_path, monkeypatch):
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 90.0))}
        )
        with pytest.raises(ValueError) as excinfo:
            resolver(self.LIB_ID)
        message = str(excinfo.value)
        # Both sources named, plus the render that measured the offset.
        assert self.LIB_ID in message
        assert "C50950" in message
        assert "(1.5, -0.25, 0)" in message  # the packaged offset
        assert "(0, 0, -90)" in message  # the rotation it was calibrated under
        assert "(0, 0, 90)" in message  # the sidecar rotation
        assert "boards/06-diffpair-test" in message
        assert "J3" in message
        assert "2026-08-04" in message
        # And the remedy.
        assert "[0, 0, 0]" in message

    def test_message_names_only_the_invalidated_nonzero_components(self, tmp_path, monkeypatch):
        # Z is invariant under a Z-only rotation delta, so a nonzero Z must not
        # be cited as evidence even when X/Y are what trip the guard.
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, 0.0, 1.25))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 90.0))}
        )
        with pytest.raises(ValueError) as excinfo:
            resolver(self.LIB_ID)
        message = str(excinfo.value)
        assert "its X component (1.5)" in message
        assert "Y component" not in message  # zero -- nothing to invalidate
        assert "Z component" not in message  # rotation-invariant here

    def test_off_z_rotation_invalidates_a_z_only_offset_too(self, tmp_path, monkeypatch):
        # Tilting the body about X/Y moves it vertically, so "Z is
        # rotation-invariant" no longer holds.
        self._packaged(monkeypatch, offset=(0.0, 0.0, 1.25))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 15.0, 0.0))}
        )
        with pytest.raises(ValueError) as excinfo:
            resolver(self.LIB_ID)
        assert "its Z component (1.25)" in str(excinfo.value)

    def test_packaged_offset_without_a_rotate_is_calibrated_in_the_identity_frame(
        self, tmp_path, monkeypatch
    ):
        # No packaged ``rotate`` means the offset was measured at identity, so
        # any sidecar rotation still changes the frame.
        self._packaged(monkeypatch, offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 90.0))}
        )
        with pytest.raises(ValueError) as excinfo:
            resolver(self.LIB_ID)
        assert "(0, 0, 0)" in str(excinfo.value)

    def test_missing_provenance_still_produces_a_message(self, tmp_path, monkeypatch):
        # provenance is typed optional (a forgetful author gets a test failure,
        # not a TypeError) -- building the error must not AttributeError.
        monkeypatch.setitem(
            lcsc_model_transforms.LCSC_MODEL_TRANSFORMS,
            "C50950",
            LcscModelTransform(rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0)),
        )
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 90.0))}
        )
        with pytest.raises(ValueError, match="no provenance recorded"):
            resolver(self.LIB_ID)

    # -- what must stay legal ----------------------------------------------

    def test_z_only_offset_survives_a_z_only_rotation_change(self, tmp_path, monkeypatch):
        # The "STEP origin is off the board plane" knob is rotation-invariant
        # under a Z spin.  Over-rejecting here would break the documented use.
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(0.0, 0.0, 1.25))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 180.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 180)\n" in resolved.models[0]
        assert "(offset\n\t\t(xyz 0 0 1.25)\n" in resolved.models[0]

    def test_signed_zero_offset_components_count_as_zero(self, tmp_path, monkeypatch):
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(-0.0, -0.0, 1.25))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 180.0))}
        )
        assert resolver(self.LIB_ID) is not None

    def test_a_redundant_identical_rotate_does_not_change_the_frame(self, tmp_path, monkeypatch):
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, -90.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 -90)\n" in resolved.models[0]

    def test_restating_the_offset_acknowledges_it(self, tmp_path, monkeypatch):
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(
            tmp_path,
            {
                self.LIB_ID: LcscModelEntry(
                    "C50950", rotate=(0.0, 0.0, 90.0), offset=(-0.25, 1.5, 0.0)
                )
            },
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 90)\n" in resolved.models[0]

    def test_an_explicit_zero_offset_is_a_valid_acknowledgement(self, tmp_path, monkeypatch):
        # The documented escape hatch: sidecar (0, 0, 0) wins the merge and
        # suppresses the packaged value.
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(
            tmp_path,
            {
                self.LIB_ID: LcscModelEntry(
                    "C50950", rotate=(0.0, 0.0, 90.0), offset=(0.0, 0.0, 0.0)
                )
            },
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 90)\n" in resolved.models[0]

    def test_a_bare_string_sidecar_inherits_a_calibrated_pair(self, tmp_path, monkeypatch):
        # The normal path: nothing was overridden, so nothing is out of frame.
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(tmp_path, {self.LIB_ID: "C50950"})
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 -90)\n" in resolved.models[0]

    def test_the_symmetric_case_is_not_a_defect(self, tmp_path, monkeypatch):
        # A sidecar offset inheriting a packaged rotate is fine: its author
        # necessarily measured with the packaged rotation active.
        self._packaged(monkeypatch, rotate=(0.0, 0.0, -90.0), offset=(1.5, -0.25, 0.0))
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", offset=(0.0, 0.0, 3.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert "(rotate\n\t\t(xyz 0 0 -90)\n" in resolved.models[0]
        assert "(offset\n\t\t(xyz 0 0 3)\n" in resolved.models[0]

    def test_no_packaged_entry_means_nothing_to_inherit(self, tmp_path):
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, 45.0))}
        )
        assert resolver(self.LIB_ID) is not None

    # -- the committed sidecars (AC 4) --------------------------------------

    @pytest.mark.parametrize(
        "sidecar",
        [
            "boards/03-usb-joystick/lcsc_models.json",
            "boards/06-diffpair-test/lcsc_models.json",
        ],
    )
    def test_committed_sidecars_never_trip_the_guard(self, sidecar):
        """Both committed sidecars are bare-string form, so no packaged offset
        can ever be split by one -- assert it, so a future table addition
        cannot quietly break a board."""
        path = Path(__file__).resolve().parents[1] / sidecar
        mapping = load_lcsc_mapping(path)
        assert mapping
        for lib_id, entry in mapping.items():
            c_number = entry.lcsc if isinstance(entry, LcscModelEntry) else entry
            sidecar_rotate = entry.rotate if isinstance(entry, LcscModelEntry) else None
            sidecar_offset = entry.offset if isinstance(entry, LcscModelEntry) else None
            resolve_merged_transform(c_number, lib_id, sidecar_rotate, sidecar_offset)


class TestTransformComposition:
    """The override must compose with -- never replace -- the centroid delta."""

    LIB_ID = "Module:Joystick_Analog"

    def test_lcsc_tier_never_derives_an_orientation(self, tmp_path):
        # theta stays 0 for this tier by construction, which is what makes it
        # safe to write the override rotation verbatim: _apply_rotate_delta
        # can never compose with (and so double-apply) it.
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, -90.0))}
        )
        resolved = resolver(self.LIB_ID)
        assert resolved is not None
        assert resolved.source_orientation is None
        assert resolved.source_anchor == (0.0, 0.0)

    def test_rotate_is_verbatim_even_with_a_nonzero_pad_centroid(self, tmp_path):
        # PCB_LCSC's single pad sits at (2.0, -1.27), so the centroid delta is
        # nonzero.  The rotation must still be exactly what was authored.
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", rotate=(0.0, 0.0, -90.0))}
        )
        new_text, report = add_model_refs_to_text(PCB_LCSC, resolver)
        assert report.patched == [self.LIB_ID]
        assert "(xyz 0 0 -90)" in new_text

    def test_offset_override_adds_to_the_pad_centroid_delta(self, tmp_path):
        # centroid delta is (dx, -dy) = (2.0, +1.27); override adds (0.5, 0.25).
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", offset=(0.5, 0.25, 0.0))}
        )
        new_text, _ = add_model_refs_to_text(PCB_LCSC, resolver)
        assert "(xyz 2.5 1.52 0)" in new_text

    def test_offset_override_z_passes_through_untouched(self, tmp_path):
        # _apply_offset_delta never rewrites Z, so the authored Z survives the
        # centroid composition verbatim -- the knob for a STEP whose origin is
        # off the board plane.
        resolver = _lcsc_resolver(
            tmp_path, {self.LIB_ID: LcscModelEntry("C50950", offset=(0.0, 0.0, -3.5))}
        )
        new_text, _ = add_model_refs_to_text(PCB_LCSC, resolver)
        assert "(xyz 2 1.27 -3.5)" in new_text

    def test_rotate_and_offset_compose_independently(self, tmp_path):
        resolver = _lcsc_resolver(
            tmp_path,
            {
                self.LIB_ID: LcscModelEntry(
                    "C50950", rotate=(0.0, 0.0, -90.0), offset=(0.5, 0.25, 1.0)
                )
            },
        )
        new_text, _ = add_model_refs_to_text(PCB_LCSC, resolver)
        # The rotation does NOT rotate the centroid delta: source_anchor is
        # (0, 0) for this tier, so R_theta * source_anchor == (0, 0) for any
        # theta and the two are fully independent.
        assert "(xyz 2.5 1.52 1)" in new_text
        assert "(xyz 0 0 -90)" in new_text


# --------------------------------------------------------------------------
# _render_env registers the LCSC path variable
# --------------------------------------------------------------------------


class TestRenderEnv:
    def test_render_env_sets_lcsc_dir_default(self, tmp_path, monkeypatch):
        from kicad_tools.cli.runner import _render_env

        monkeypatch.delenv(DEFAULT_CACHE_ENV_VAR, raising=False)
        # Point KiCad model discovery at a real dir so env isn't None.
        model_dir = tmp_path / "3dmodels"
        model_dir.mkdir()
        monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(model_dir))
        env = _render_env(None)
        assert env is not None
        expected = str(Path.home() / ".cache" / "kicad-tools" / "lcsc-3d")
        assert env[DEFAULT_CACHE_ENV_VAR] == expected

    def test_render_env_lcsc_dir_env_not_overridden(self, tmp_path, monkeypatch):
        from kicad_tools.cli.runner import _render_env

        model_dir = tmp_path / "3dmodels"
        model_dir.mkdir()
        monkeypatch.setenv("KICAD10_3DMODEL_DIR", str(model_dir))
        monkeypatch.setenv(DEFAULT_CACHE_ENV_VAR, "/custom/lcsc")
        env = _render_env(None)
        assert env is not None
        assert env[DEFAULT_CACHE_ENV_VAR] == "/custom/lcsc"

    def test_render_env_sets_lcsc_dir_even_without_kicad_libs(self, tmp_path, monkeypatch):
        """LCSC cache is independent of the KiCad 3dmodels dir."""
        from kicad_tools.cli import runner

        for var in runner.KICAD_3DMODEL_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv(DEFAULT_CACHE_ENV_VAR, str(tmp_path / "lcsc"))
        # No KiCad model dir discoverable.
        monkeypatch.setattr(runner, "find_kicad_3dmodel_dir", lambda cli: None)
        env = runner._render_env(None)
        # DEFAULT_CACHE_ENV_VAR was already set -> inherit-as-is (None).
        assert env is None

    def test_render_env_injects_lcsc_when_only_lcsc_missing(self, tmp_path, monkeypatch):
        from kicad_tools.cli import runner

        for var in runner.KICAD_3DMODEL_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(DEFAULT_CACHE_ENV_VAR, raising=False)
        model_dir = tmp_path / "3dmodels"
        model_dir.mkdir()
        monkeypatch.setattr(runner, "find_kicad_3dmodel_dir", lambda cli: model_dir)
        env = runner._render_env(None)
        assert env is not None
        assert env[DEFAULT_CACHE_ENV_VAR] == str(lcsc_cache_dir())
