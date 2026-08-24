"""External autorouter benchmark measurement layer (Epic #4932, issue #4934).

Emits the DeepPCB-comparable metric tuple (completion %, via count,
wirelength in mm, wall-clock runtime) plus this project's stricter gates
(``kct check``, the mandatory ``kicad-cli pcb drc --refill-zones``
cross-gate, diff-pair completion) as a JSON report with a stable schema,
and renders it as a per-board markdown table.

See ``docs/benchmark-external-report-schema.md`` for the schema contract.
"""

from .metrics import (
    PROTOCOL_TUNED,
    PROTOCOL_ZERO_TOUCH,
    SCHEMA_URL,
    SCHEMA_VERSION,
    BackendInfo,
    BenchmarkReport,
    CompletionMetrics,
    CopperMetrics,
    DiffPairCompletion,
    KctCheckSummary,
    KicadCliDrcSummary,
    TimingMetrics,
    build_timing,
    collect_report,
    measure_completion,
    measure_copper,
    measure_diff_pairs,
    probe_backend,
    run_kct_check,
    run_kicad_cli_drc,
)
from .render import render_markdown, render_report_markdown

__all__ = [
    "PROTOCOL_TUNED",
    "PROTOCOL_ZERO_TOUCH",
    "SCHEMA_URL",
    "SCHEMA_VERSION",
    "BackendInfo",
    "BenchmarkReport",
    "CompletionMetrics",
    "CopperMetrics",
    "DiffPairCompletion",
    "KctCheckSummary",
    "KicadCliDrcSummary",
    "TimingMetrics",
    "build_timing",
    "collect_report",
    "measure_completion",
    "measure_copper",
    "measure_diff_pairs",
    "probe_backend",
    "render_markdown",
    "render_report_markdown",
    "run_kct_check",
    "run_kicad_cli_drc",
]
