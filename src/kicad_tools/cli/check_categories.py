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
    "mask_to_copper",
    "via_in_pad",
    "via_type",
    "via_under_body",
    "width_consistency",
    "zero_length_segment",
    "zones",
]
