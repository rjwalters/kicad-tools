"""Category ids accepted by ``kct check --only/--skip``.

Kept free of imports so ``cli.parser`` can build its help text from the same
list as ``check_cmd`` without pulling ``check_cmd``'s heavy dependencies
(numpy, router, validate) into every ``kct`` invocation (issue #6896).
"""

CHECK_CATEGORIES = [
    "physical_copper_gap",
    "ampacity",
    "path_ampacity",
    "clearance",
    "connectivity",
    "connector_access",
    "segment_zone",
    "via_zone",
    "copper_sliver",
    "courtyard_overlap",
    "dangling_copper",
    "diffpair_clearance_intra",
    "diffpair_length_skew",
    "diffpair_routing_continuity",
    "diffpair_uncoupled_length",
    "dimensions",
    "doc_drift",
    "edge",
    "impedance",
    "isolated_copper",
    "match_group_length_skew",
    "model_coverage",
    "netclass_clearance_copper",
    "netclass_floor",
    "netlist",
    "pad_grid",
    "pin1_marker",
    "placement",
    "routing_policy",
    "sch_fields",
    "silkscreen",
    "single_pad_net",
    "solder_mask",
    "spec_conformance",
    "mask_to_copper",
    "thermal_asymmetry",
    "via_in_pad",
    "via_type",
    "via_under_body",
    "width_consistency",
    "zero_length_segment",
    "zones",
]

#: Categories left out of the default ``kct check`` set until asked for with
#: ``--only <category>``.  Empty since #7683 (the pinned, digest-verified 3D
#: model subset, ``kct models sync``) made ``model_coverage`` default-on; the
#: mechanism stays in :func:`kicad_tools.cli.check_cmd.run_selected_checks` so a
#: future host-dependent category can be deferred in the ONE place without any
#: board's hashed gate script naming it.  Never add a category here to hide a
#: host-independent defect.
DEFERRED_OPT_IN_CATEGORIES: frozenset[str] = frozenset()
