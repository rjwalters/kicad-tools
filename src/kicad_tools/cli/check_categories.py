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

#: Categories that are NOT in the default ``kct check`` set and run only when
#: asked for (``--only <category>``).  Empty since #7685: ``model_coverage`` was
#: deferred by #7682 while the audit depended on the host's KiCad 3D library
#: and generated boards still carried ``no_ref`` footprints; #7683 shipped the
#: pinned model subset and #7685 brought every board to full coverage, so the
#: category is default-on.  A footprint's *absent library* is only a stderr note
#: (``missing_root``, never a finding), so a verdict and its serialized report
#: built on ``kct check`` stay host-independent (CLAUDE.md, #6962).  The
#: mechanism stays for a future category that must be opt-in.  Never add a category here to hide a
#: host-independent defect.
DEFERRED_OPT_IN_CATEGORIES: frozenset[str] = frozenset()
