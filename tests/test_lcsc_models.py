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
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.footprints.library_path import LibraryPaths
from kicad_tools.pcb.lcsc_models import (
    DEFAULT_CACHE_ENV_VAR,
    LCSC_MODEL_PATH_VAR,
    _fetch_lcsc_step,
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
        assert load_lcsc_mapping(sidecar) == {"Module:Joystick_Analog": "C50950"}

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
# Synthesized model block
# --------------------------------------------------------------------------


class TestSynthesize:
    def test_uses_portable_path_variable(self):
        block = synthesize_model_block("C50950")
        assert block.startswith(f'(model "{LCSC_MODEL_PATH_VAR}/C50950.step"')
        assert "${KCT_LCSC_3D_DIR}" in block
        assert "(offset" in block and "(xyz 0 0 0)" in block
        assert block.endswith(")")


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
