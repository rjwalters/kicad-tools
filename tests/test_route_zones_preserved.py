"""Regression test: ``kct route`` write path must preserve input zones (issue #2770).

Issue #2770 reported that when ``kct route`` writes its output PCB,
zones present in the input were dropped from the output.  The curator's
audit (see issue #2770 comments) found the bug as described did NOT
reproduce on current ``main`` — the five write sites in
``route_cmd.py`` all correctly read the staged input via
``pcb_path.read_text()``, insert route fragments with
``_insert_sexp_before_closing``, and write back to ``output_path``.

Rather than close as not-reproducible, this module locks in the current
correct behavior as a regression-prevention test so a future refactor
of the write path cannot silently drop zones.

It covers:

1. **Building blocks** — ``_insert_sexp_before_closing`` preserves
   pre-existing ``(zone ...)`` blocks unchanged when inserting routes.
2. **Layer stackup escalation** — ``update_pcb_layer_stackup`` only
   touches the top-level ``(layers ...)`` definition and does NOT
   match ``(layer "F.Cu")`` lines inside zones (i.e. zones survive a
   2-layer to 4-layer rewrite).
3. **Zero-routes edge case** — when ``route_sexp`` is empty, the write
   path returns ``original_content`` unchanged so zones still survive.
4. **Pre-existing-zones idempotency** — running through the write path
   on a PCB that already contains zones does not duplicate or drop
   them.
5. **Source-level audit** — every PCB-write site in ``route_cmd.py``
   (discovered structurally from the AST, not by pinned counts) routes
   through the zone-preserving
   ``read_text + _insert_sexp_before_closing + write_text`` pattern so
   a future drift between them cannot silently regress one path.
   (Originally this pinned exact write-site counts — "five sites" —
   which went stale when the terminal save sites were consolidated
   into the central ``_write_routed_pcb`` helper; see issue #3445.)

Acceptance criteria for issue #2770:
- [x] Regression test added covering ALL FIVE ``output_path.write_text``
  call sites in ``route_cmd.py`` (audited via source-level scan).
- [x] Test asserts zones in output PCB == zones in input PCB
  (matched by ``(net_name "X")`` and ``(layer "Y")``).
- [x] Test includes a layer-escalation case to lock in
  ``update_pcb_layer_stackup`` zone preservation.
- [x] Test includes a zero-routes case (auto-pour created zones but
  router routed nothing).
- [x] Test catches the curator's "drop zones" reproduction mode (
  verified by temporarily breaking the write path).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# PCB fixture helpers
# ---------------------------------------------------------------------------


_PCB_HEADER_2L = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
"""


_PCB_NETS = """\
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "+5V")
  (net 4 "VMOTOR")
"""


_PCB_FOOTER = """\
  (gr_line (start 0 0) (end 50 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 0) (end 50 50) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 50 50) (end 0 50) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
  (gr_line (start 0 50) (end 0 0) (stroke (width 0.05) (type default)) (layer "Edge.Cuts"))
)
"""


def _zone_block(net_id: int, net_name: str, layer: str, uuid: str) -> str:
    """Render a minimal ``(zone ...)`` s-expression block.

    The block contains a ``(layer "...")`` child that is structurally
    identical to the entries inside the top-level ``(layers ...)``
    definition; this is the exact form that motivates the
    layer-stackup-vs-zones regression risk (lock-in test below).
    """
    return (
        f"  (zone\n"
        f"    (net {net_id})\n"
        f'    (net_name "{net_name}")\n'
        f'    (layer "{layer}")\n'
        f'    (uuid "{uuid}")\n'
        f"    (hatch edge 0.5)\n"
        f"    (connect_pads (clearance 0.2))\n"
        f"    (min_thickness 0.2)\n"
        f"    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3) (island_removal_mode 0))\n"
        f"    (polygon (pts (xy 5 5) (xy 45 5) (xy 45 45) (xy 5 45)))\n"
        f"  )\n"
    )


def _make_pcb_with_zones(num_zones: int = 4) -> str:
    """Build a 2-layer PCB containing ``num_zones`` distinct zones.

    Each zone is bound to a different power net so the regression test
    can match zones unambiguously by ``(net_name "X")``.
    """
    nets = [
        (1, "GND", "F.Cu"),
        (2, "+3.3V", "F.Cu"),
        (3, "+5V", "B.Cu"),
        (4, "VMOTOR", "B.Cu"),
    ]
    assert num_zones <= len(nets), "fixture only supplies 4 zones"
    parts = [_PCB_HEADER_2L, _PCB_NETS]
    for idx in range(num_zones):
        nid, name, layer = nets[idx]
        parts.append(_zone_block(nid, name, layer, f"zone-{idx}-uuid"))
    parts.append(_PCB_FOOTER)
    return "".join(parts)


def _extract_zone_signatures(pcb_text: str) -> set[tuple[str, str]]:
    """Extract a set of ``(net_name, layer)`` tuples — one per zone.

    Matching by (net_name, layer) is the user-visible identity of a
    zone; uuids are random and polygon geometry can be reformatted, so
    those fields are intentionally NOT part of the signature.
    """
    signatures: set[tuple[str, str]] = set()
    # KiCad 8 form: (zone ... (net_name "GND") ... (layer "F.Cu") ...)
    for m in re.finditer(
        r"\(zone\b(?P<body>.*?)\n\s*\)\n",
        pcb_text,
        re.DOTALL,
    ):
        body = m.group("body")
        name_match = re.search(r'\(net_name\s+"([^"]+)"\)', body)
        # First (layer "...") inside the zone body — *not* any nested
        # layer reference inside a filled_polygon.
        layer_match = re.search(r'\(layer\s+"([^"]+)"\)', body)
        if name_match and layer_match:
            signatures.add((name_match.group(1), layer_match.group(1)))
    return signatures


# ---------------------------------------------------------------------------
# Sanity: the helper extracts the signatures we expect
# ---------------------------------------------------------------------------


class TestSignatureExtractor:
    """Sanity-check ``_extract_zone_signatures`` on a known fixture."""

    def test_extracts_all_zones_from_four_zone_fixture(self):
        pcb = _make_pcb_with_zones(num_zones=4)
        sigs = _extract_zone_signatures(pcb)
        assert sigs == {
            ("GND", "F.Cu"),
            ("+3.3V", "F.Cu"),
            ("+5V", "B.Cu"),
            ("VMOTOR", "B.Cu"),
        }

    def test_extracts_zero_zones_when_none_present(self):
        pcb = _PCB_HEADER_2L + _PCB_NETS + _PCB_FOOTER
        assert _extract_zone_signatures(pcb) == set()


# ---------------------------------------------------------------------------
# Building-block test: _insert_sexp_before_closing preserves zones
# ---------------------------------------------------------------------------


class TestInsertSexpBeforeClosingPreservesZones:
    """The core helper used by all route write sites must not drop zones."""

    def test_route_insertion_keeps_all_input_zones(self):
        """Inserting a route s-expression into a PCB with N zones yields a PCB with N zones."""
        from kicad_tools.cli.route_cmd import _insert_sexp_before_closing

        pcb_in = _make_pcb_with_zones(num_zones=4)
        zones_in = _extract_zone_signatures(pcb_in)
        assert len(zones_in) == 4

        route_sexp = (
            '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") '
            '(net 2) (uuid "seg-uuid-1"))'
        )

        pcb_out = _insert_sexp_before_closing(pcb_in, route_sexp)

        zones_out = _extract_zone_signatures(pcb_out)
        assert zones_out == zones_in, (
            "Zones present in input PCB must all be present in output PCB; "
            f"missing: {zones_in - zones_out}, extra: {zones_out - zones_in}"
        )

    def test_zero_routes_returns_zones_intact(self):
        """``output_content = original_content`` branch: no routes, zones survive."""
        from kicad_tools.cli.route_cmd import _insert_sexp_before_closing

        pcb_in = _make_pcb_with_zones(num_zones=4)
        zones_in = _extract_zone_signatures(pcb_in)

        # Mirror the "no routes generated" path in all four route_with_*
        # functions: route_sexp is empty so write path falls through to
        # ``output_content = original_content``.  We don't invoke the
        # helper here; we directly verify the no-op semantics those
        # paths depend on.
        pcb_out = pcb_in  # what the live code assigns when route_sexp is empty

        zones_out = _extract_zone_signatures(pcb_out)
        assert zones_out == zones_in

        # But also: passing an empty route_sexp through the helper must
        # still preserve zones (defensive — a future caller might pass
        # an empty string directly rather than skipping the call).
        pcb_via_helper = _insert_sexp_before_closing(pcb_in, "")
        assert _extract_zone_signatures(pcb_via_helper) == zones_in

    def test_combined_zone_plus_route_sexp_preserves_input_zones(self):
        """Main route save (line 5527) inserts ``zone_sexp + route_sexp`` together.

        That code path joins two fragments with ``\\n  ``.  Verify that
        the combined insertion still preserves *input* zones (the
        appended ``zone_sexp`` would be additional auto-pour zones, not
        a replacement for the existing zones)."""
        from kicad_tools.cli.route_cmd import _insert_sexp_before_closing

        pcb_in = _make_pcb_with_zones(num_zones=2)  # GND, +3.3V on F.Cu
        zones_in = _extract_zone_signatures(pcb_in)

        # Mirror the line-5511 combined fragment shape.
        zone_sexp = _zone_block(99, "EXTRA_POUR", "B.Cu", "auto-pour-uuid").strip()
        route_sexp = (
            '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") '
            '(net 1) (uuid "seg-uuid-1"))'
        )
        combined = zone_sexp + "\n  " + route_sexp

        pcb_out = _insert_sexp_before_closing(pcb_in, combined)
        zones_out = _extract_zone_signatures(pcb_out)

        # All original zones must still be present.
        assert zones_in.issubset(zones_out), (
            f"Input zones must survive combined insertion; missing: {zones_in - zones_out}"
        )
        # And the auto-pour zone appears in the output too.
        assert ("EXTRA_POUR", "B.Cu") in zones_out


# ---------------------------------------------------------------------------
# Layer-escalation regression: update_pcb_layer_stackup must not eat zones
# ---------------------------------------------------------------------------


class TestLayerEscalationPreservesZones:
    """Lock in that the 2L->4L stackup rewrite leaves ``(zone ...)`` blocks alone.

    Risk: ``update_pcb_layer_stackup`` regex-matches the top-level
    ``(layers ...)`` block.  Each zone also contains a ``(layer "F.Cu")``
    child.  If a future refactor loosens the regex, zones with nested
    ``(layer ...)`` could be partly or wholly consumed.

    These tests assert that:
      1. After a 2->4 layer rewrite, all zones survive byte-for-byte
         in their ``(net_name, layer)`` identity.
      2. The combined route_cmd.py:2030-2062 path (rewrite stackup,
         then insert routes) preserves zones end-to-end.
    """

    def test_2L_to_4L_rewrite_preserves_input_zones(self):
        """``update_pcb_layer_stackup(2->4)`` must not match anything inside zones."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_in = _make_pcb_with_zones(num_zones=4)
        zones_in = _extract_zone_signatures(pcb_in)
        assert len(zones_in) == 4

        pcb_out = update_pcb_layer_stackup(pcb_in, target_layers=4)

        # The stackup rewrite must have happened (4 copper layers now).
        copper_layer_count = len(re.findall(r'\(\d+\s+"[^"]+\.Cu"\s+\w+', pcb_out))
        assert copper_layer_count == 4, (
            f"Expected 4 copper layers after 2->4 escalation, got {copper_layer_count}"
        )

        # AND zones survive unchanged.
        zones_out = _extract_zone_signatures(pcb_out)
        assert zones_out == zones_in, (
            f"Zones must survive layer-stackup rewrite; "
            f"missing: {zones_in - zones_out}, extra: {zones_out - zones_in}"
        )

    def test_2L_to_6L_rewrite_preserves_input_zones(self):
        """Same property must hold for 2->6 escalation."""
        from kicad_tools.cli.route_cmd import update_pcb_layer_stackup

        pcb_in = _make_pcb_with_zones(num_zones=4)
        zones_in = _extract_zone_signatures(pcb_in)

        pcb_out = update_pcb_layer_stackup(pcb_in, target_layers=6)

        copper_layer_count = len(re.findall(r'\(\d+\s+"[^"]+\.Cu"\s+\w+', pcb_out))
        assert copper_layer_count == 6
        assert _extract_zone_signatures(pcb_out) == zones_in

    def test_escalation_then_route_insertion_preserves_zones(self):
        """End-to-end mirror of the live write site at ``route_cmd.py:2080-2109``.

        ``original_content = pcb_path.read_text()`` ->
        ``original_content = update_pcb_layer_stackup(...)`` ->
        ``output_content = _insert_sexp_before_closing(original_content, route_sexp)`` ->
        ``output_path.write_text(output_content)``.
        """
        from kicad_tools.cli.route_cmd import (
            _insert_sexp_before_closing,
            update_pcb_layer_stackup,
        )

        pcb_in = _make_pcb_with_zones(num_zones=4)
        zones_in = _extract_zone_signatures(pcb_in)

        # Step 1: read original (simulated)
        original_content = pcb_in
        # Step 2: rewrite stackup for 4L escalation
        original_content = update_pcb_layer_stackup(original_content, target_layers=4)
        # Step 3: insert routes
        route_sexp = (
            '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") '
            '(net 2) (uuid "seg-uuid-1"))'
        )
        output_content = _insert_sexp_before_closing(original_content, route_sexp)

        zones_out = _extract_zone_signatures(output_content)
        assert zones_out == zones_in


# ---------------------------------------------------------------------------
# Source-level audit: every PCB-write site routes through the
# zone-preserving helper (sites discovered from the AST, not pinned counts)
# ---------------------------------------------------------------------------

_ZONE_HELPER = "_insert_sexp_before_closing"
_CENTRAL_WRITER = "_write_routed_pcb"


def _route_cmd_module_ast() -> ast.Module:
    """Parse ``route_cmd.py`` into an AST for structural inspection."""
    from kicad_tools.cli import route_cmd

    return ast.parse(Path(route_cmd.__file__).read_text())


def _functions_in(tree: ast.Module):
    """Yield every function definition in the module (including nested)."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _shallow_walk(func: ast.AST):
    """Walk a function's own body without descending into nested defs.

    Nested functions are visited separately by ``_functions_in``, so
    skipping them here attributes each call site to its *innermost*
    enclosing function.
    """
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            stack.extend(ast.iter_child_nodes(node))


def _called_names(func: ast.AST) -> set[str]:
    """Names of functions/methods called directly in *func*'s own body."""
    names: set[str] = set()
    for node in _shallow_walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def _pcb_write_calls(func: ast.AST) -> list[ast.Call]:
    """``<path>.write_text(<content_variable>)`` calls in *func*'s own body.

    PCB writes in ``route_cmd.py`` pass a *content variable* (e.g.
    ``output_content``) that was built upstream via the zone-preserving
    helper.  Report/export writes pass inline expressions instead
    (``json.dumps(...)``, ``"\\n".join(...) + ...``, string literals), so
    classifying by argument shape discovers the PCB-write sites no matter
    how many there are or what the receiving path variable is called.

    If this classification ever over-matches a genuinely non-PCB write,
    the failing test's message explains how to proceed — that is the
    desired behavior for a guard (a reviewable false positive beats a
    silently stale count).
    """
    calls: list[ast.Call] = []
    for node in _shallow_walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_text"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            calls.append(node)
    return calls


class TestRouteWriteSitesPattern:
    """Structural audit: every PCB-write site preserves zones.

    Earlier versions of this class pinned exact write-site counts
    (exactly 5 ``write_text`` sites, >= 6 ``_insert_sexp_before_closing``
    occurrences).  A refactor consolidated the terminal save sites into
    one central helper (``_write_routed_pcb``) and the counts went stale
    while the *behavior* stayed correct (issue #3445).

    These tests now discover the write sites programmatically from the
    AST and assert the property that actually matters: every function
    that writes PCB text to disk either IS the central zone-preserving
    write path, calls it, or builds its content via
    ``_insert_sexp_before_closing`` directly.  No absolute counts — the
    guard survives consolidation and expansion alike.
    """

    def test_central_write_path_is_zone_preserving(self):
        """``_write_routed_pcb`` is the single consolidated write path.

        Structurally verify it (1) reads the staged input fresh via
        ``read_text``, (2) inserts route fragments via the
        zone-preserving ``_insert_sexp_before_closing`` helper,
        (3) gates output through ``_validate_sexp_parentheses``, and
        (4) writes atomically (``fsync`` + ``os.replace``).
        """
        tree = _route_cmd_module_ast()
        central = [f for f in _functions_in(tree) if f.name == _CENTRAL_WRITER]
        assert len(central) == 1, (
            f"Expected exactly one definition of ``{_CENTRAL_WRITER}`` in "
            f"route_cmd.py, found {len(central)}.  If the central write "
            f"path was renamed, update _CENTRAL_WRITER in this test module "
            f"and confirm the new path still preserves zones."
        )
        called = _called_names(central[0])
        for required in ("read_text", _ZONE_HELPER, "_validate_sexp_parentheses"):
            assert required in called, (
                f"``{_CENTRAL_WRITER}`` no longer calls ``{required}``; "
                f"the central PCB write path must read the staged input, "
                f"insert routes via the zone-preserving helper, and "
                f"validate parentheses before writing."
            )
        for required in ("fsync", "replace"):
            assert required in called, (
                f"``{_CENTRAL_WRITER}`` no longer calls ``{required}``; "
                f"the central write path must stay atomic "
                f"(tmp + fsync + os.replace, issue #2808)."
            )

    def test_every_pcb_write_site_routes_through_zone_preserving_helper(self):
        """Every function that writes PCB text must preserve zones.

        Discovers PCB-write call sites from the AST (see
        ``_pcb_write_calls``) and asserts each enclosing function either
        is ``_write_routed_pcb`` itself, calls it, or calls
        ``_insert_sexp_before_closing`` directly.
        """
        tree = _route_cmd_module_ast()
        pcb_writers: list[str] = []
        offenders: list[str] = []
        for func in _functions_in(tree):
            writes = _pcb_write_calls(func)
            if not writes:
                continue
            pcb_writers.append(func.name)
            called = _called_names(func)
            if (
                func.name != _CENTRAL_WRITER
                and _CENTRAL_WRITER not in called
                and _ZONE_HELPER not in called
            ):
                lines = ", ".join(str(w.lineno) for w in writes)
                offenders.append(f"{func.name} (write_text at line {lines})")

        # Sanity: the discovery heuristic still finds the write paths at
        # all.  If this fires, the write-site *shape* changed (e.g. no
        # longer ``path.write_text(content_var)``) — update
        # ``_pcb_write_calls`` to match the new shape rather than
        # weakening the assertion.
        assert pcb_writers, (
            "No PCB-write sites discovered in route_cmd.py; the "
            "discovery heuristic in _pcb_write_calls is stale.  Update "
            "it to match the current write-site shape so the "
            "zone-preservation audit keeps its teeth."
        )
        assert _CENTRAL_WRITER in pcb_writers, (
            f"``{_CENTRAL_WRITER}`` was not discovered as a PCB-write "
            f"site; either it was renamed (update _CENTRAL_WRITER) or "
            f"its write no longer matches the discovery heuristic."
        )
        assert not offenders, (
            f"PCB-write site(s) bypass the zone-preserving write path: "
            f"{offenders}.  Every function that writes PCB text must "
            f"either call ``{_CENTRAL_WRITER}`` or build its content "
            f"via ``{_ZONE_HELPER}`` so input zones survive routing."
        )

    def test_every_pcb_write_site_reads_original_first(self):
        """Every PCB-writing function must read the staged input first.

        Replaces the old ``count >= 5`` pin on ``pcb_path.read_text()``:
        instead of counting occurrences module-wide, assert each
        discovered PCB-writing function performs a ``read_text`` (or
        delegates to ``_write_routed_pcb``, which does).
        """
        tree = _route_cmd_module_ast()
        offenders: list[str] = []
        for func in _functions_in(tree):
            if not _pcb_write_calls(func):
                continue
            called = _called_names(func)
            if "read_text" not in called and _CENTRAL_WRITER not in called:
                offenders.append(f"{func.name} (line {func.lineno})")
        assert not offenders, (
            f"PCB-write site(s) do not read the staged input via "
            f"read_text() before writing: {offenders}.  Without a fresh "
            f"read of the input, zones cannot be preserved end-to-end."
        )

    def test_central_writer_is_actually_used_by_save_sites(self):
        """At least one save site outside the helper calls it.

        Guards against the consolidation becoming dead code: if every
        caller migrated off ``_write_routed_pcb`` to some new write
        mechanism, the audit above could pass vacuously.  No exact
        caller count is pinned — only existence.
        """
        tree = _route_cmd_module_ast()
        external_callers = [
            func.name
            for func in _functions_in(tree)
            if func.name != _CENTRAL_WRITER and _CENTRAL_WRITER in _called_names(func)
        ]
        assert external_callers, (
            f"No function in route_cmd.py calls ``{_CENTRAL_WRITER}``; "
            f"the central zone-preserving write path appears unused.  "
            f"If save sites moved to a new helper, point _CENTRAL_WRITER "
            f"at it and confirm it preserves zones."
        )


# ---------------------------------------------------------------------------
# Behavioral test: end-to-end through the central _write_routed_pcb path
# ---------------------------------------------------------------------------


class TestWriteRoutedPcbPreservesZones:
    """Exercise the consolidated ``_write_routed_pcb`` write path on disk.

    The structural audit above proves the source *shape*; these tests
    prove the *behavior*: zones present in the input PCB survive a real
    write through the central helper, including the layer-escalation and
    empty-route variants.
    """

    def test_route_write_preserves_zones(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _write_routed_pcb

        pcb_path = tmp_path / "input.kicad_pcb"
        pcb_path.write_text(_make_pcb_with_zones(num_zones=4))
        output_path = tmp_path / "out_routed.kicad_pcb"
        zones_in = _extract_zone_signatures(pcb_path.read_text())
        assert len(zones_in) == 4

        route_sexp = (
            '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") '
            '(net 2) (uuid "seg-uuid-1"))'
        )
        written = _write_routed_pcb(pcb_path, output_path, route_sexp)

        assert written == output_path
        output_text = output_path.read_text()
        assert "(segment" in output_text
        zones_out = _extract_zone_signatures(output_text)
        assert zones_out == zones_in, (
            f"Central write path dropped zones; "
            f"missing: {zones_in - zones_out}, extra: {zones_out - zones_in}"
        )
        # Atomic write must not leave its tmp sibling behind.
        assert not output_path.with_suffix(output_path.suffix + ".tmp").exists()

    def test_escalated_4L_write_preserves_zones(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _write_routed_pcb

        pcb_path = tmp_path / "input.kicad_pcb"
        pcb_path.write_text(_make_pcb_with_zones(num_zones=4))
        output_path = tmp_path / "out_routed.kicad_pcb"
        zones_in = _extract_zone_signatures(pcb_path.read_text())

        route_sexp = (
            '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") '
            '(net 2) (uuid "seg-uuid-1"))'
        )
        _write_routed_pcb(pcb_path, output_path, route_sexp, layer_count=4)

        output_text = output_path.read_text()
        copper_layer_count = len(re.findall(r'\(\d+\s+"[^"]+\.Cu"\s+\w+', output_text))
        assert copper_layer_count == 4, (
            f"Expected 4 copper layers after escalated write, got {copper_layer_count}"
        )
        assert _extract_zone_signatures(output_text) == zones_in

    def test_empty_route_sexp_write_preserves_zones(self, tmp_path: Path):
        """Empty ``route_sexp`` writes the original back — zones intact."""
        from kicad_tools.cli.route_cmd import _write_routed_pcb

        pcb_path = tmp_path / "input.kicad_pcb"
        pcb_path.write_text(_make_pcb_with_zones(num_zones=4))
        output_path = tmp_path / "out_routed.kicad_pcb"
        zones_in = _extract_zone_signatures(pcb_path.read_text())

        _write_routed_pcb(pcb_path, output_path, "")

        assert _extract_zone_signatures(output_path.read_text()) == zones_in


# ---------------------------------------------------------------------------
# Behavioral test: end-to-end through the partial-save write path
# ---------------------------------------------------------------------------


class TestPartialSaveWritePathPreservesZones:
    """Exercise the ``_save_partial_results`` write site.

    This is the easiest write path to invoke
    end-to-end without standing up a full Autorouter.  It exercises
    the read-text + insert + write_text triplet against real disk
    state, catching any future change that bypasses the helper.

    ``_save_partial_results`` is a no-arg function that reads its state
    from the module-level ``_interrupt_state`` dict (so the SIGINT
    handler can call it).  The test mutates that dict directly to
    inject the test router + paths, then restores it on teardown.
    """

    def test_partial_save_does_not_drop_zones(self, tmp_path: Path):
        """Save partial routes from a fake router and verify zones survive."""
        from unittest.mock import MagicMock

        from kicad_tools.cli import route_cmd

        # Stage a PCB on disk with zones.
        pcb_in = _make_pcb_with_zones(num_zones=4)
        pcb_path = tmp_path / "input.kicad_pcb"
        pcb_path.write_text(pcb_in)
        output_path = tmp_path / "out_routed.kicad_pcb"
        zones_in = _extract_zone_signatures(pcb_in)

        # Build a minimal stand-in router that only needs to:
        #   - have a truthy ``routes`` attribute (list len > 0)
        #   - return a route s-expression from ``to_sexp()``
        #   - return some statistics dict
        fake_router = MagicMock()
        fake_router.routes = [object()]  # any non-empty iterable
        fake_router.to_sexp.return_value = (
            '(segment (start 10 10) (end 20 10) (width 0.2) (layer "F.Cu") '
            '(net 2) (uuid "seg-uuid-1"))'
        )
        fake_router.get_statistics.return_value = {
            "nets_routed": 1,
            "segments": 1,
            "vias": 0,
        }

        # Save & swap interrupt state so we can drive _save_partial_results.
        original_state = dict(route_cmd._interrupt_state)
        try:
            route_cmd._interrupt_state.update(
                {
                    "router": fake_router,
                    "output_path": output_path,
                    "pcb_path": pcb_path,
                    "quiet": True,
                    # Default branch: no best-completed attempt, so the
                    # helper writes to *_partial.kicad_pcb.
                    "best_completed_attempt": False,
                }
            )
            saved = route_cmd._save_partial_results()
        finally:
            route_cmd._interrupt_state.clear()
            route_cmd._interrupt_state.update(original_state)

        assert saved is True

        # The saved file is at output_path.with_stem(stem + "_partial").
        partial_path = output_path.with_stem(output_path.stem + "_partial")
        assert partial_path.exists(), f"Expected partial output at {partial_path}"

        # Zones must be present in the partial output.
        zones_out = _extract_zone_signatures(partial_path.read_text())
        assert zones_out == zones_in, (
            f"Partial-save write path dropped zones; "
            f"missing: {zones_in - zones_out}, extra: {zones_out - zones_in}"
        )

        # And the original input PCB is unchanged.
        assert _extract_zone_signatures(pcb_path.read_text()) == zones_in


# ---------------------------------------------------------------------------
# Mutation test: confirm the test catches the "drop zones" failure mode
# ---------------------------------------------------------------------------


class TestRegressionGuardCatchesDropZonesMutation:
    """Verify the regression test as a whole would catch the bug as described.

    The curator's recommended acceptance criterion: "The new test
    catches the curator's reproduction-failure mode (verified by
    temporarily breaking the write path — e.g. replace
    ``original_content`` with ``re.sub(r'\\(zone\\b.*?\\n\\)', '',
    original_content, flags=re.DOTALL)`` — and confirming the new
    test fails)."

    We can't mutate the live source from inside the test suite without
    a real mutation-testing harness, but we can demonstrate that the
    signature extractor catches the precise s-expression deletion that
    a buggy write path would perform.  If this test passes today and a
    future refactor adds a write path that strips zones, the building-
    block tests above would fail in exactly the same way the assertion
    below detects.
    """

    def test_signature_extractor_detects_dropped_zones(self):
        """Confirm the extractor flags a PCB with zones stripped."""
        pcb_with = _make_pcb_with_zones(num_zones=4)
        zones_with = _extract_zone_signatures(pcb_with)

        # Simulate the curator's reproduction mutation: strip all zones
        # by regex.  This is exactly what a buggy write path would do.
        pcb_without = re.sub(
            r"  \(zone\b.*?\n  \)\n",
            "",
            pcb_with,
            flags=re.DOTALL,
        )
        zones_without = _extract_zone_signatures(pcb_without)

        # Sanity: the mutation actually dropped all the zones.
        assert zones_with == {
            ("GND", "F.Cu"),
            ("+3.3V", "F.Cu"),
            ("+5V", "B.Cu"),
            ("VMOTOR", "B.Cu"),
        }
        assert zones_without == set()

        # The comparison the live tests perform would fail loudly:
        with pytest.raises(AssertionError):
            assert zones_without == zones_with
