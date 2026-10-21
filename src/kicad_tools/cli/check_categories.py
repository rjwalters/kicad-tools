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

#: Categories that zero-warning gates and committed-evidence producers
#: deselect for now (issue #7682).  ``model_coverage`` resolves models against
#: the *host's* KiCad 3D library, and neither the CI runner nor the pinned
#: toolchain image ships one yet, so its findings would make a board's verdict
#: and its recorded evidence depend on the machine (CLAUDE.md, #6962).  Plain
#: ``kct check`` still runs it as an advisory.
#:
#: Expires with #7683 (a pinned model set) and #7685 (every board at 100% on
#: it): ``tests/test_model_audit.py`` fails as soon as
#: ``kicad_tools.pcb.model_audit.PINNED_MODEL_DIR`` is set while this set still
#: names ``model_coverage``, so the deferral is removed deliberately rather than
#: lingering.  Never add a category here to hide a host-independent defect.
GATE_DEFERRED_CATEGORIES: frozenset[str] = frozenset({"model_coverage"})


def gate_skip_set(skip: set[str] | frozenset[str] = frozenset()) -> set[str]:
    """*skip* plus :data:`GATE_DEFERRED_CATEGORIES` (for ``run_selected_checks``)."""
    return set(skip) | GATE_DEFERRED_CATEGORIES


def gate_skip_argv(skip: set[str] | frozenset[str] = frozenset()) -> list[str]:
    """``kct check`` argv that deselects :data:`GATE_DEFERRED_CATEGORIES` (+ *skip*).

    ``--skip`` is a single comma-separated value (last one wins), so callers
    that also skip their own categories must pass them here.
    """
    names = sorted(gate_skip_set(skip))
    return ["--skip", ",".join(names)] if names else []
