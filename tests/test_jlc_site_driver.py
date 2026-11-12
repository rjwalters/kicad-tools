"""Fixture-driven tests for the attended JLC website driver (#8468).

The fixture is SYNTHETIC page state (not a recorded JLC page): these tests prove
the abort and gate logic, not the live selectors.
"""

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

from kicad_tools.export.factory_selection import ExpectedPart, SelectionMapping
from kicad_tools.export.submission_review import render_review_page  # noqa: F401
from kicad_tools.manufacturers import jlc_site_driver as drv

FIXTURE = Path(__file__).parent / "fixtures" / "jlc_site" / "order_page.json"
S = drv.SELECTORS
MAPPING = SelectionMapping(
    "Refs", "Selected", ("yes",), ("no",), catalog_id="Catalog", identity="catalog_id"
)
EXPECTED = [ExpectedPart("R1", "C1"), ExpectedPart("Y1", "C2")]
GOOD_CSV = b"Refs,Selected,Catalog\nR1,yes,C1\nY1,yes,C2\n"


class FakePage:
    def __init__(self, state, fail_on=None):
        self.state = copy.deepcopy(state)
        self.url = state["url"]
        self.log = []
        self.fail_on = fail_on

    def exists(self, sel):
        return sel in self.state["elements"]

    def options(self, sel):
        return self.state["elements"][sel]["options"]

    def select(self, sel, value):
        self.log.append(("select", sel, value))
        self.state["elements"][sel]["value"] = value

    def value(self, sel):
        return self.state["elements"][sel].get("value", "")

    def text(self, sel):
        return self.state["elements"][sel]["text"]

    def click(self, sel):
        self.log.append(("click", sel))
        if sel == S["save_to_cart"] and self.fail_on == "url":
            self.url = "https://jlcpcb.com/smtOrder/downloadNoMatchBomList"

    def set_file(self, sel, path):
        self.log.append(("file", sel, path.name))

    def wait_settled(self):
        pass

    def screenshot(self, path):
        path.write_bytes(b"png")


@pytest.fixture
def state():
    return json.loads(FIXTURE.read_text())


@pytest.fixture
def inputs(tmp_path):
    files = {}
    for role, name in (("gerber", "gerbers.zip"), ("bom", "bom.csv"), ("cpl", "cpl.csv")):
        (tmp_path / name).write_bytes(role.encode())
        files[role] = hashlib.sha256(role.encode()).hexdigest()
    return drv.SiteInputs(
        tmp_path / "gerbers.zip", tmp_path / "bom.csv", tmp_path / "cpl.csv", files
    )


SPEC = drv.SiteOrderSpec(4, 5, "1.6", "1", "1", "top", True)


def run(page, inputs, tmp_path, csv=GOOD_CSV, spec=SPEC, fab_review_signed=True):
    return drv.run_driver(
        page,
        spec,
        inputs,
        EXPECTED,
        MAPPING,
        lambda: csv,
        tmp_path / "shots",
        fab_review_signed=fab_review_signed,
    )


def test_no_cart_click_without_fab_review_signoff(state, inputs, tmp_path):
    page = FakePage(state)
    with pytest.raises(drv.DriverAbort, match="fab-review") as exc:
        run(page, inputs, tmp_path, fab_review_signed=False)
    assert exc.value.step == "fab-review"
    assert "tapeout-sign-off --stage fab-review" in exc.value.reason
    assert not clicked(page, "save_to_cart")
    assert any(e[0] == "file" for e in page.log)  # upload/preview still happened


def test_guided_checklist_reviews_viewer_before_cart(inputs):
    lines = drv.guided_checklist(SPEC, inputs)
    review = next(i for i, ln in enumerate(lines) if "gerber viewer" in ln)
    signoff = next(i for i, ln in enumerate(lines) if "--stage fab-review" in ln)
    cart = next(i for i, ln in enumerate(lines) if "Save to cart" in ln)
    assert review < signoff < cart
    assert not any("gerber viewer" in s for s in drv.SiteResult.remaining_human_steps)


def clicked(page, key):
    return ("click", S[key]) in page.log


def test_happy_path_stops_at_saved_cart(state, inputs, tmp_path):
    page = FakePage(state)
    result = run(page, inputs, tmp_path)
    assert result.state == "in-cart" and result.cart_url.endswith("abc123")
    assert result.quote_total == "$42.10" and result.lead_time == "5 days"
    assert set(result.uploaded_hashes) == {"gerbers.zip", "bom.csv", "cpl.csv"}
    assert page.state["elements"][S["pcba_type"]]["value"] == "Standard"
    assert clicked(page, "save_to_cart")
    assert all(s.screenshot for s in result.steps if s.name != "factory-match")
    assert result.as_dict()["match_diff"] == []


def test_missing_selector_aborts_with_screenshot_and_no_cart(state, inputs, tmp_path):
    del state["elements"][S["thickness"]]
    page = FakePage(state)
    with pytest.raises(drv.DriverAbort) as exc:
        run(page, inputs, tmp_path)
    assert exc.value.step == "thickness" and exc.value.screenshot
    assert (tmp_path / "shots" / exc.value.screenshot).exists()
    assert not clicked(page, "save_to_cart")


def test_unoffered_option_is_never_guessed(state, inputs, tmp_path):
    state["elements"][S["quantity"]]["options"] = ["10", "15"]
    page = FakePage(state)
    with pytest.raises(drv.DriverAbort, match="not offered"):
        run(page, inputs, tmp_path)
    assert not any(e[0] == "select" and e[1] == S["quantity"] for e in page.log)
    assert not clicked(page, "save_to_cart")


def test_changed_readback_aborts(state, inputs, tmp_path):
    class Snap(FakePage):
        def select(self, sel, value):
            super().select(sel, value)
            if sel == S["thickness"]:
                self.state["elements"][sel]["value"] = "1.2"

    with pytest.raises(drv.DriverAbort, match="read-back"):
        run(Snap(state), inputs, tmp_path)


@pytest.mark.parametrize(
    "csv",
    [
        b"Refs,Selected,Catalog\nR1,yes,C1\n",  # omitted
        b"Refs,Selected,Catalog\nR1,yes,C1\nY1,yes,C2\nR9,yes,C3\n",  # extra
        b"Refs,Selected,Catalog\nR1,yes,C1\nY1,yes,C99\n",  # substituted
        b"Refs,Selected,Catalog\nR1,yes,C1\nY1,no,C2\n",  # unselected THT
    ],
)
def test_match_gate_blocks_cart(state, inputs, tmp_path, csv):
    page = FakePage(state)
    with pytest.raises(drv.DriverAbort) as exc:
        run(page, inputs, tmp_path, csv=csv)
    assert exc.value.step == "factory-match"
    assert not clicked(page, "save_to_cart")


def test_changed_input_file_refused_before_upload(state, inputs, tmp_path):
    (tmp_path / "bom.csv").write_bytes(b"tampered")
    page = FakePage(state)
    with pytest.raises(drv.DriverAbort, match="changed since"):
        run(page, inputs, tmp_path)
    assert not any(e[0] == "file" and e[2] == "bom.csv" for e in page.log)


def test_landing_on_disallowed_path_aborts(state, inputs, tmp_path):
    with pytest.raises(drv.DriverAbort, match="disallowed"):
        run(FakePage(state, fail_on="url"), inputs, tmp_path)


def test_foreign_host_aborts(state, inputs, tmp_path):
    state["url"] = "https://example.com/quote"
    with pytest.raises(drv.DriverAbort, match="allowed hosts"):
        run(FakePage(state), inputs, tmp_path)


@pytest.mark.parametrize(
    "spec",
    [
        drv.SiteOrderSpec(3, 5, "1.6", "1", None, "top", False),
        drv.SiteOrderSpec(4, 5, "1.5", "1", "1", "top", False),
        drv.SiteOrderSpec(4, 5, "1.6", "1", None, "top", False),
        drv.SiteOrderSpec(2, 5, "1.6", "1", "1", "top", False),
        drv.SiteOrderSpec(2, 0, "1.6", "1", None, "top", False),
        drv.SiteOrderSpec(2, 5, "1.6", "3", None, "top", False),
    ],
)
def test_unmappable_fields_refuse_before_touching_page(state, inputs, tmp_path, spec):
    page = FakePage(state)
    with pytest.raises(drv.DriverAbort, match="cannot map"):
        run(page, inputs, tmp_path, spec=spec)
    assert page.log == []


def test_attended_only():
    with pytest.raises(drv.DriverAbort):
        drv.require_attended(lambda: False)
    drv.require_attended(lambda: True)
    with pytest.raises(drv.DriverAbort):
        drv.run_driver(None, SPEC, None, EXPECTED, MAPPING, lambda: b"", Path("x"), attended=False)


def test_tht_selects_standard_pcba():
    assert drv.map_order_fields(SPEC)["pcba_type"] == "Standard"
    assert (
        drv.map_order_fields(drv.SiteOrderSpec(2, 5, "1.6", "1", None, "top", False))["pcba_type"]
        == "Economic"
    )


def test_guided_checklist_covers_every_step(inputs):
    text = "\n".join(drv.guided_checklist(SPEC, inputs))
    assert "THT" in text and "STOP" in text


def test_static_no_network_or_order_paths():
    src = Path(drv.__file__).read_text()
    tree = ast.parse(src)
    banned_imports = {"requests", "urllib3", "httpx", "http", "socket", "aiohttp", "subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & banned_imports
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] not in banned_imports
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"goto", "request", "fetch", "urlopen", "evaluate"}
    lowered = src.lower()
    for word in ("checkout", "payment", "purchase", "place_order"):
        assert word not in lowered
    # Disallowed paths appear only inside the DISALLOWED_PATHS tuple.
    for path in drv.DISALLOWED_PATHS[2:]:
        assert src.count(path) == 1


def test_protocol_has_no_navigation():
    assert not hasattr(drv.SitePage, "goto") and not hasattr(drv.SitePage, "request")
