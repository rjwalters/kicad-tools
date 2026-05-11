"""Tests that the router and DRC manufacturer registries stay in sync.

The router (``kicad_tools.router.mfr_limits``) and the DRC manufacturer
profile registry (``kicad_tools.manufacturers``) each maintain their own
list of supported manufacturer names and aliases.  Historically these
could (and did) drift apart - see issue #2622, where ``jlcpcb-tier1``
was accepted by the router but rejected by DRC with
``Unknown manufacturer: 'jlcpcb-tier1'``.

This module enforces the invariant that **every canonical manufacturer
name and every alias known to the router resolves to a valid DRC
profile**.  If you add a manufacturer or alias to one registry you must
add it to the other, or this test will fail.

The DRC registry is allowed to contain additional names that the router
does not know about (e.g. ``flashpcb`` which is not currently a router
target), so the relationship is a subset, not equality.
"""

from __future__ import annotations

import contextlib
import io

from kicad_tools.manufacturers import (
    _ALIASES as DRC_ALIASES,
)
from kicad_tools.manufacturers import (
    _PROFILES as DRC_PROFILES,
)
from kicad_tools.manufacturers import (
    get_profile,
)
from kicad_tools.router.mfr_limits import (
    _MFR_ALIASES,
    MFR_LIMITS,
)


class TestRouterDRCRegistrySync:
    """Drift-prevention invariants between router and DRC registries."""

    def test_router_canonical_names_resolve_in_drc(self):
        """Every canonical MFR_LIMITS key resolves to a DRC profile."""
        router_names = set(MFR_LIMITS.keys())
        drc_known = set(DRC_PROFILES.keys()) | set(DRC_ALIASES.keys())

        missing = router_names - drc_known
        assert not missing, (
            f"Router knows manufacturer names that DRC does not: {sorted(missing)}. "
            f"Add them to kicad_tools.manufacturers._PROFILES or _ALIASES."
        )

    def test_router_aliases_resolve_in_drc(self):
        """Every router alias resolves to a DRC profile or alias."""
        router_aliases = set(_MFR_ALIASES.keys())
        drc_known = set(DRC_PROFILES.keys()) | set(DRC_ALIASES.keys())

        missing = router_aliases - drc_known
        assert not missing, (
            f"Router aliases are not recognised by DRC: {sorted(missing)}. "
            f"Add them to kicad_tools.manufacturers._ALIASES (mirroring the router)."
        )

    def test_all_router_names_construct_drc_profile(self):
        """get_profile() never raises for any router-known name or alias."""
        all_router_names = set(MFR_LIMITS.keys()) | set(_MFR_ALIASES.keys())

        for name in sorted(all_router_names):
            # Should not raise ValueError("Unknown manufacturer: ...")
            profile = get_profile(name)
            assert profile is not None, f"get_profile({name!r}) returned None"

    def test_jlcpcb_tier1_canonical_and_aliases(self):
        """All four router aliases for jlcpcb-tier1 resolve to the tier1 profile."""
        canonical = get_profile("jlcpcb-tier1")
        assert canonical.id == "jlcpcb-tier1"

        for alias in [
            "jlcpcb_tier1",
            "jlcpcb-capabilityplus",
            "jlcpcb_capabilityplus",
            "jlcpcb-capability-plus",
        ]:
            profile = get_profile(alias)
            assert profile is canonical, (
                f"Alias {alias!r} resolved to {profile.id!r}, expected 'jlcpcb-tier1'"
            )

    def test_jlcpcb_tier1_router_alias_table_matches_drc(self):
        """The router and DRC alias targets for jlcpcb-tier1 are identical.

        This guards against silent drift where one registry remaps an
        alias to a different canonical name than the other.
        """
        jlcpcb_tier1_router_aliases = {
            alias for alias, target in _MFR_ALIASES.items() if target == "jlcpcb-tier1"
        }
        jlcpcb_tier1_drc_aliases = {
            alias for alias, target in DRC_ALIASES.items() if target == "jlcpcb-tier1"
        }

        assert jlcpcb_tier1_router_aliases == jlcpcb_tier1_drc_aliases, (
            f"Router and DRC alias tables for jlcpcb-tier1 disagree.\n"
            f"  Router: {sorted(jlcpcb_tier1_router_aliases)}\n"
            f"  DRC:    {sorted(jlcpcb_tier1_drc_aliases)}"
        )

    def test_via_in_pad_supported_parity(self):
        """``via_in_pad_supported`` parity between router and DRC (issue #2635).

        For every manufacturer name known to BOTH the router's
        ``MFR_LIMITS`` table and the DRC profile registry, the
        ``via_in_pad_supported`` flag must agree across all stackup
        layer counts (2L, 4L, 6L).  If a future contributor flips this
        flag on one side but not the other, the router would refuse to
        place in-pad vias while DRC would silently accept them (or vice
        versa) -- this test catches that drift.

        Layer counts that the DRC profile doesn't model fall back to
        the closest available stackup (see ``ManufacturerProfile.get_design_rules``).
        We still assert parity per-layer to surface stackup-level
        regressions where the capability flag is missing on a specific
        block.
        """
        # Iterate the intersection: only names that exist in both
        # registries.  Aliases on one side that resolve to a canonical
        # name on the other are already covered by the resolver tests
        # above; here we focus on the capability-flag value itself.
        shared_names = set(MFR_LIMITS.keys()) & (set(DRC_PROFILES.keys()) | set(DRC_ALIASES.keys()))
        assert shared_names, "No shared manufacturer names between router and DRC"

        mismatches: list[str] = []
        for name in sorted(shared_names):
            router_flag = MFR_LIMITS[name].via_in_pad_supported
            drc_profile = get_profile(name)
            for layers in (2, 4, 6):
                drc_rules = drc_profile.get_design_rules(layers=layers, copper_oz=1.0)
                drc_flag = drc_rules.via_in_pad_supported
                if router_flag != drc_flag:
                    mismatches.append(f"{name} ({layers}L): router={router_flag}, DRC={drc_flag}")

        assert not mismatches, (
            "Router and DRC disagree on via_in_pad_supported for some "
            "manufacturer/layer combinations. Update the corresponding "
            "YAML stackup block (manufacturers/data/<mfr>.yaml) or the "
            "router's MfrLimits entry to restore parity:\n  " + "\n  ".join(mismatches)
        )


class TestJLCPCBTier1DRCProfile:
    """Regression tests for the jlcpcb-tier1 DRC profile (issue #2622)."""

    def test_drc_checker_accepts_jlcpcb_tier1(self):
        """DRCChecker constructs without raising for manufacturer='jlcpcb-tier1'."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.create(width=50.0, height=50.0, layers=2)
        # Should not raise ValueError: Unknown manufacturer ...
        checker = DRCChecker(pcb, manufacturer="jlcpcb-tier1", layers=4)
        assert checker.design_rules is not None
        assert checker.manufacturer == "jlcpcb-tier1"

    def test_drc_checker_accepts_all_jlcpcb_tier1_aliases(self):
        """Every router alias for jlcpcb-tier1 also works in DRCChecker."""
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.validate import DRCChecker

        pcb = PCB.create(width=50.0, height=50.0, layers=2)
        for alias in [
            "jlcpcb-tier1",
            "jlcpcb_tier1",
            "jlcpcb-capabilityplus",
            "jlcpcb_capabilityplus",
            "jlcpcb-capability-plus",
        ]:
            checker = DRCChecker(pcb, manufacturer=alias, layers=4)
            assert checker.design_rules is not None, (
                f"DRCChecker(manufacturer={alias!r}) produced no design_rules"
            )

    def test_tier1_rules_at_least_as_strict_as_base_jlcpcb(self):
        """tier1 rules must never be looser than the base jlcpcb profile.

        DRC must surface at least as many violations on jlcpcb-tier1 as on
        plain jlcpcb so users opting into the tier1 profile do not get a
        silent regression.
        """
        base = get_profile("jlcpcb")
        tier1 = get_profile("jlcpcb-tier1")

        for layers in (2, 4, 6):
            base_rules = base.get_design_rules(layers=layers, copper_oz=1.0)
            tier1_rules = tier1.get_design_rules(layers=layers, copper_oz=1.0)

            # Fields where larger = looser (tier1 must be <=)
            for field in (
                "min_trace_width_mm",
                "min_clearance_mm",
                "min_via_drill_mm",
                "min_via_diameter_mm",
                "min_annular_ring_mm",
                "min_hole_diameter_mm",
                "min_copper_to_edge_mm",
                "min_hole_to_edge_mm",
                "min_silkscreen_width_mm",
                "min_silkscreen_height_mm",
                "min_solder_mask_dam_mm",
                "min_solder_mask_clearance_mm",
                "min_pad_size_mm",
            ):
                base_val = getattr(base_rules, field)
                tier1_val = getattr(tier1_rules, field)
                assert tier1_val <= base_val, (
                    f"{layers}-layer {field}: tier1={tier1_val} looser than "
                    f"base jlcpcb={base_val} (would be a DRC regression)"
                )

    def test_run_post_route_drc_no_unknown_manufacturer_message(self, tmp_path):
        """Running the post-route DRC step does not emit the regression message.

        Issue #2622: when the router calls ``run_post_route_drc`` with
        ``manufacturer='jlcpcb-tier1'``, stdout must NOT contain the
        ``Unknown manufacturer`` warning that was the original user-visible
        symptom of the broken DRC registry.
        """
        from kicad_tools.cli.route_cmd import run_post_route_drc
        from kicad_tools.schema.pcb import PCB

        # Create a minimal but valid PCB on disk so run_post_route_drc can
        # PCB.load() it.  Any small board is sufficient - DRC only runs
        # manufacturer-rule checks against whatever is present.
        pcb = PCB.create(width=50.0, height=50.0, layers=2)
        test_pcb = tmp_path / "tier1_regression.kicad_pcb"
        pcb.save(str(test_pcb))

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            run_post_route_drc(
                output_path=test_pcb,
                manufacturer="jlcpcb-tier1",
                layers=4,
                quiet=False,
            )

        output = buf.getvalue()
        assert "Unknown manufacturer" not in output, (
            f"Regression of issue #2622: 'Unknown manufacturer' surfaced "
            f"in stdout for jlcpcb-tier1:\n{output}"
        )
        # And the positive assertion: the DRC validation block ran.
        assert "DRC Validation" in output, (
            f"Expected DRC validation header in output, got:\n{output}"
        )

    def test_tier1_profile_has_via_in_pad_capability_documented(self):
        """The tier1 profile exists specifically for via-in-pad support.

        Until DesignRules grows a via_in_pad_supported field (follow-up
        scoped out of #2622), we at minimum require that the profile's
        name documents the Capability Plus tier so users can identify it.
        """
        profile = get_profile("jlcpcb-tier1")
        assert "Capability" in profile.name or "Plus" in profile.name, (
            f"jlcpcb-tier1 profile name should reference Capability Plus, got {profile.name!r}"
        )
