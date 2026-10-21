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

#: Categories that are NOT in the default ``kct check`` set (issue #7682) and
#: run only when asked for: ``--only <category>`` or, for ``model_coverage``,
#: ``kct check --3d-models``.  ``model_coverage`` resolves models against the
#: *host's* KiCad 3D library, and neither the CI runner nor the pinned
#: toolchain image ships one yet, so running it by default would make every
#: verdict built on ``kct check`` -- ``--strict`` exit codes, board gates,
#: ``kct readiness`` evidence, ``run_drc``/``kct fix-drc`` -- depend on the
#: machine (CLAUDE.md, #6962).  The single place the deferral lives is
#: :func:`kicad_tools.cli.check_cmd.run_selected_checks`, so no board's hashed
#: gate script has to name it.  ``kct render``, ``kct pcb model-audit`` and
#: ``kct fleet status`` still report coverage.
#:
#: Expires with #7683 (a pinned model set) and #7685 (every board at 100% on
#: it): ``tests/test_model_audit.py`` fails as soon as
#: ``kicad_tools.pcb.model_audit.PINNED_MODEL_DIR`` is set while this set still
#: names ``model_coverage``, so the category is made default-on (and board
#: evidence re-recorded) deliberately rather than the deferral lingering.
#: #7683 shipped the subset (``kct models sync``; ``PINNED_MODEL_DIR`` is now an
#: explicit override, ``None`` by default) but kept this deferral: generated
#: boards still carry ``no_ref`` footprints, a host-independent warning that
#: would fail board 04's zero-warning reviewed gate on a fresh regen.  Drop
#: ``model_coverage`` here with #7685, once every board is at 100%.
#: Never add a category here to hide a host-independent defect.
DEFERRED_OPT_IN_CATEGORIES: frozenset[str] = frozenset({"model_coverage"})
