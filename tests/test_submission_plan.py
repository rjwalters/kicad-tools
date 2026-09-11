"""Synthetic offline handoffs; no supplier responses or private board data."""

import csv
import hashlib
import io
import json

import pytest

from kicad_tools.export import submission_plan as sp


def digest(data):
    return hashlib.sha256(data).hexdigest()


def csv_bytes(headers, rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return b"\xef\xbb\xbf" + stream.getvalue().encode()


@pytest.fixture
def inputs(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    # 24 rows, 21 exact IDs, 85 unique placements. Last three rows repeat IDs.
    rows, placements = [], []
    cursor = 1
    for row in range(24):
        refs = [f"R{i}" for i in range(cursor, cursor + (4 if row < 13 else 3))]
        cursor += len(refs)
        rows.append(["10k", ",".join(refs), "R_0402", f"C{row % 21 + 1}", len(refs)])
        placements.extend(
            [[ref, "10k", "R_0402", "10.5000mm", "20mm", "90.0", "Top"] for ref in refs]
        )
    (bundle / "bom.csv").write_bytes(
        csv_bytes(["Comment", "Designator", "Footprint", "LCSC Part #", "Quantity"], rows)
    )
    (bundle / "cpl.csv").write_bytes(
        csv_bytes(
            ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"], placements
        )
    )
    (bundle / "gerbers.zip").write_bytes(b"PK\x03\x04\x00synthetic-exact-zip-bytes\xff\r\n")
    (source / "board.kicad_pcb").write_bytes(b"synthetic PCB\r\n")
    (source / "board.kicad_sch").write_bytes(b"synthetic schematic\n")
    sources = {
        role: sp.SourceEvidence(
            name, digest((source / name).read_bytes()), (source / name).stat().st_size
        )
        for role, name in [("pcb", "board.kicad_pcb"), ("schematic", "board.kicad_sch")]
    }
    args = {
        "bundle_root": bundle,
        "source_root": source,
        "source_evidence": sources,
        "bundle_files": ("gerbers.zip", "bom.csv", "cpl.csv"),
        "artifacts": {"gerber": "gerbers.zip", "bom": "bom.csv", "cpl": "cpl.csv"},
        "board_quantity": 1,
        "settings": sp.FactorySettings("jlcpcb", 4, ("top",)),
        "destination": tmp_path / "handoff",
    }
    refresh_manifest(args)
    return args


def refresh_manifest(args):
    bundle = args["bundle_root"]
    files = {
        name: {
            "sha256": digest((bundle / name).read_bytes()),
            "size": (bundle / name).stat().st_size,
        }
        for name in args["bundle_files"]
    }
    (bundle / "manifest.json").write_text(json.dumps({"version": "1.0", "files": files}))


def rewrite_csv(args, role, edit):
    path = args["bundle_root"] / args["artifacts"][role]
    rows = list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig"))))
    edit(rows)
    path.write_bytes(csv_bytes(rows[0], rows[1:]))
    refresh_manifest(args)


def test_exact_bytes_demand_and_determinism(inputs, tmp_path):
    before = {p.name: p.read_bytes() for p in inputs["bundle_root"].iterdir()}
    plan = sp.prepare_submission(**inputs)
    core = json.loads(plan.plan_bytes)
    assert plan.sha256 == digest(plan.plan_bytes)
    assert core["schema_version"] == 1
    assert core["states"] == {
        "preparation": "local-integrity-valid",
        "inventory": "unknown",
        "human_approval": "not-established",
        "factory_matching": "not-observed",
        "upload": "not-performed",
        "order": "not-performed",
    }
    assert len(core["demand"]) == 21
    assert sum(item["per_board"] for item in core["demand"]) == 85
    assert {item["catalog_id"]: item["per_board"] for item in core["demand"]} == {
        f"C{i}": (7 if i <= 3 else 4 if i <= 13 else 3) for i in range(1, 22)
    }
    assert all(item["required"] == item["per_board"] for item in core["demand"])
    matches = json.loads((plan.directory / "expected-matches.json").read_bytes())
    assert len(matches) == 85
    assert len({r["reference"] for r in matches}) == 85
    for role, name in inputs["artifacts"].items():
        assert (plan.directory / name).read_bytes() == before[name]
    assert before == {p.name: p.read_bytes() for p in inputs["bundle_root"].iterdir()}
    assert not (plan.directory / "bom.csv").stat().st_mode & 0o222
    again = sp.prepare_submission(**(inputs | {"destination": tmp_path / "again"}))
    assert again.plan_bytes == plan.plan_bytes
    assert sp.verify_submission(plan.directory, plan.sha256) == plan
    ten = sp.prepare_submission(
        **(inputs | {"destination": tmp_path / "ten", "board_quantity": 10})
    )
    assert ten.sha256 != plan.sha256
    assert sum(d["required"] for d in json.loads(ten.plan_bytes)["demand"]) == 850
    assert all(d["required"] == d["per_board"] * 10 for d in json.loads(ten.plan_bytes)["demand"])
    with pytest.raises(sp.SubmissionError, match="exists"):
        sp.prepare_submission(**inputs)


@pytest.mark.parametrize("quantity", [True, False, 0, -1, 1.5, "10", None])
def test_quantity_is_positive_integer(inputs, quantity):
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**(inputs | {"board_quantity": quantity}))
    assert not inputs["destination"].exists()


@pytest.mark.parametrize(
    "mutation", ["missing", "changed", "extra", "hash", "size", "source", "source-evidence"]
)
def test_integrity_failures_do_not_publish(inputs, mutation):
    bundle = inputs["bundle_root"]
    if mutation == "missing":
        (bundle / "gerbers.zip").unlink()
    elif mutation == "changed":
        (bundle / "bom.csv").write_bytes(b"modified")
    elif mutation == "extra":
        (bundle / "unexpected.txt").write_bytes(b"extra")
    elif mutation in ("hash", "size"):
        path = bundle / "manifest.json"
        data = json.loads(path.read_text())
        del data["files"]["bom.csv"]["sha256" if mutation == "hash" else "size"]
        path.write_text(json.dumps(data))
    elif mutation == "source":
        (inputs["source_root"] / "board.kicad_pcb").write_bytes(b"changed")
    else:
        inputs["source_evidence"] = {}
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)
    assert not inputs["destination"].exists()


@pytest.mark.parametrize(
    "edit",
    [
        lambda rows: rows.append(rows[1]),
        lambda rows: rows[1].__setitem__(1, "R1,R1,R2,R3"),
        lambda rows: rows[1].__setitem__(1, ""),
        lambda rows: rows[1].__setitem__(3, "C0"),
        lambda rows: rows[1].__setitem__(4, "5"),
        lambda rows: rows.append(["x", "R1", "R_0402", "C999", "1"]),
        lambda rows: rows[0].append("Unknown"),
    ],
)
def test_strict_bom_rejects_ambiguity(inputs, edit):
    rewrite_csv(inputs, "bom", edit)
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)
    assert not inputs["destination"].exists()


@pytest.mark.parametrize(
    "edit",
    [
        lambda rows: rows.pop(),
        lambda rows: rows.append(rows[1]),
        lambda rows: rows.append(["R999", "10k", "R_0402", "10mm", "20mm", "90", "Top"]),
        lambda rows: rows[1].__setitem__(3, "nan"),
        lambda rows: rows[1].__setitem__(6, "Bottom"),
        lambda rows: rows[1].__setitem__(1, "different value"),
    ],
)
def test_strict_cpl_coverage_and_fields(inputs, edit):
    rewrite_csv(inputs, "cpl", edit)
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)


def test_exclusions_cannot_silently_filter_frozen_uploads(inputs):
    with pytest.raises(sp.SubmissionError, match="exclusion"):
        sp.prepare_submission(**(inputs | {"exclusions": {"R1": "dnp"}}))
    plan = sp.prepare_submission(**(inputs | {"exclusions": {"J1": "tht", "R999": "dnp"}}))
    assert json.loads(plan.plan_bytes)["exclusions"] == {"J1": "tht", "R999": "dnp"}
    assert len(json.loads((plan.directory / "expected-matches.json").read_bytes())) == 85


@pytest.mark.parametrize(
    "path", ["../bom.csv", "./bom.csv", "x/../bom.csv", "/bom.csv", "x//bom.csv", "x\\bom.csv"]
)
def test_noncanonical_paths(inputs, path):
    inputs["artifacts"]["bom"] = path
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)


def test_duplicate_and_ambiguous_names(inputs):
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**(inputs | {"bundle_files": (*inputs["bundle_files"], "bom.csv")}))
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(
            **(inputs | {"output_names": {"gerber": "same", "bom": "same", "cpl": "cpl.csv"}})
        )
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(
            **(
                inputs
                | {"output_names": {"gerber": "plan.json", "bom": "bom.csv", "cpl": "cpl.csv"}}
            )
        )


@pytest.mark.parametrize("kind", ["file", "directory", "source", "root"])
def test_symlinks_rejected(inputs, tmp_path, kind):
    if kind == "root":
        link = tmp_path / "bundle-link"
        link.symlink_to(inputs["bundle_root"], target_is_directory=True)
        inputs["bundle_root"] = link
    elif kind == "directory":
        (inputs["bundle_root"] / "outside").symlink_to(
            inputs["source_root"], target_is_directory=True
        )
    else:
        target = (
            (inputs["source_root"] / "board.kicad_pcb")
            if kind == "source"
            else (inputs["bundle_root"] / "gerbers.zip")
        )
        outside = tmp_path / "outside"
        outside.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(outside)
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)


@pytest.mark.parametrize("mutate", ["artifact", "source", "manifest", "extra", "write-failure"])
def test_mutation_during_copy_is_atomic(inputs, monkeypatch, mutate):
    write = sp._write_bytes
    changed = False

    def racing_write(path, data):
        nonlocal changed
        write(path, data)
        if not changed:
            changed = True
            if mutate == "write-failure":
                raise OSError("simulated write failure")
            target = {
                "artifact": inputs["bundle_root"] / "gerbers.zip",
                "source": inputs["source_root"] / "board.kicad_pcb",
                "manifest": inputs["bundle_root"] / "manifest.json",
                "extra": inputs["bundle_root"] / "extra",
            }[mutate]
            target.write_bytes(b"mutated")

    monkeypatch.setattr(sp, "_write_bytes", racing_write)
    with pytest.raises((sp.SubmissionError, OSError)):
        sp.prepare_submission(**inputs)
    assert not inputs["destination"].exists()
    assert not list(inputs["destination"].parent.glob(".handoff.stage-*"))


def test_identity_binds_raw_records_settings_and_sources(inputs, tmp_path):
    first = sp.prepare_submission(**inputs)
    rewrite_csv(
        inputs, "bom", lambda rows: rows.__setitem__(slice(1, None), list(reversed(rows[1:])))
    )
    reordered = sp.prepare_submission(**(inputs | {"destination": tmp_path / "reordered"}))
    assert first.sha256 != reordered.sha256  # Raw BOM hash changes despite same semantics.
    assert (first.directory / "expected-matches.json").read_bytes() == (
        reordered.directory / "expected-matches.json"
    ).read_bytes()
    changed = sp.prepare_submission(
        **(
            inputs
            | {
                "destination": tmp_path / "settings",
                "settings": sp.FactorySettings("jlcpcb", 2, ("top",)),
            }
        )
    )
    assert changed.sha256 != reordered.sha256
    path = inputs["source_root"] / "board.kicad_pcb"
    path.write_bytes(b"new source")
    inputs["source_evidence"]["pcb"] = sp.SourceEvidence(
        path.name, digest(path.read_bytes()), path.stat().st_size
    )
    source = sp.prepare_submission(**(inputs | {"destination": tmp_path / "newsource"}))
    assert source.sha256 != reordered.sha256


def test_reverify_rejects_tampering_and_extra_files(inputs):
    plan = sp.prepare_submission(**inputs)
    plan.directory.chmod(0o700)
    (plan.directory / "extra").write_text("extra")
    with pytest.raises(sp.SubmissionError):
        sp.verify_submission(plan.directory, plan.sha256)


def test_live_refresh_is_explicitly_unsupported():
    with pytest.raises(NotImplementedError, match="milestone B"):
        sp.refresh_inventory(None)


@pytest.mark.parametrize(
    "settings",
    [
        sp.FactorySettings("unknown-factory", 4, ("top",)),
        sp.FactorySettings("jlcpcb", True, ("top",)),
        sp.FactorySettings("jlcpcb", 0, ("top",)),
        sp.FactorySettings("jlcpcb", 4, ()),
        sp.FactorySettings("jlcpcb", 4, ("top", "top")),
        sp.FactorySettings("jlcpcb", 4, ("arbitrary-vendor-code",)),
    ],
)
def test_invalid_settings(inputs, settings):
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**(inputs | {"settings": settings}))
    assert not inputs["destination"].exists()


def test_no_network_or_clients_for_preparation(inputs, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        pytest.fail("Offline preparation attempted network I/O")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("JLCPCB_API_KEY", "must-not-appear-in-plan")
    plan = sp.prepare_submission(**inputs)
    assert b"must-not-appear-in-plan" not in plan.plan_bytes
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**(inputs | {"board_quantity": 0}))


def test_nested_exact_paths_and_explicit_output_names(inputs):
    bundle = inputs["bundle_root"]
    (bundle / "gerbers").mkdir()
    (bundle / "gerbers.zip").rename(bundle / "gerbers" / "actual.zip")
    inputs["bundle_files"] = ("gerbers/actual.zip", "bom.csv", "cpl.csv")
    inputs["artifacts"]["gerber"] = "gerbers/actual.zip"
    refresh_manifest(inputs)
    plan = sp.prepare_submission(
        **(
            inputs
            | {
                "output_names": {
                    "gerber": "fabrication.zip",
                    "bom": "assembly.csv",
                    "cpl": "placement.csv",
                }
            }
        )
    )
    assert (plan.directory / "fabrication.zip").read_bytes() == (
        bundle / "gerbers" / "actual.zip"
    ).read_bytes()


def test_no_legacy_filename_fallback(inputs):
    bundle = inputs["bundle_root"]
    (bundle / "nested").mkdir()
    (bundle / "gerbers.zip").rename(bundle / "nested" / "gerbers.zip")
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)


def test_duplicate_manifest_keys_are_not_last_value_wins(inputs):
    path = inputs["bundle_root"] / "manifest.json"
    path.write_text(
        '{"files":{},"files":' + json.dumps(json.loads(path.read_text())["files"]) + "}"
    )
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)


def test_destination_bytes_verified_before_publish(inputs, monkeypatch):
    original = sp._write_bytes

    def corrupt(path, data):
        original(path, data + b"corruption")

    monkeypatch.setattr(sp, "_write_bytes", corrupt)
    with pytest.raises(sp.SubmissionError, match="Destination bytes"):
        sp.prepare_submission(**inputs)
    assert not inputs["destination"].exists()


def test_standard_export_headers_without_quantity(inputs):
    rewrite_csv(inputs, "bom", lambda rows: [row.pop() for row in rows])
    plan = sp.prepare_submission(**inputs)
    assert sum(item["per_board"] for item in json.loads(plan.plan_bytes)["demand"]) == 85


@pytest.mark.parametrize(
    "mutation", ["boolean-size", "invalid-hash", "missing-source-path", "same-source-path"]
)
def test_source_evidence_is_complete_and_unambiguous(inputs, mutation):
    evidence = inputs["source_evidence"]["pcb"]
    if mutation == "boolean-size":
        evidence = sp.SourceEvidence(evidence.path, evidence.sha256, True)
    elif mutation == "invalid-hash":
        evidence = sp.SourceEvidence(evidence.path, "not-a-sha256", evidence.size)
    elif mutation == "missing-source-path":
        evidence = sp.SourceEvidence("missing.kicad_pcb", evidence.sha256, evidence.size)
    else:
        evidence = inputs["source_evidence"]["schematic"]
    inputs["source_evidence"]["pcb"] = evidence
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**inputs)
    assert not inputs["destination"].exists()


def test_destination_inside_frozen_input_is_rejected(inputs):
    with pytest.raises(sp.SubmissionError):
        sp.prepare_submission(**(inputs | {"destination": inputs["bundle_root"] / "handoff"}))
    assert not (inputs["bundle_root"] / "handoff").exists()


def test_claimed_destination_is_not_touched(inputs):
    lock = inputs["destination"].parent / ".handoff.prepare-lock"
    lock.write_bytes(b"peer owns publication")
    with pytest.raises(sp.SubmissionError, match="claimed"):
        sp.prepare_submission(**inputs)
    assert lock.read_bytes() == b"peer owns publication"
    assert not inputs["destination"].exists()


def _context_signature(context):
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.capitals,
        context.clamp,
        dict(context.traps),
        dict(context.flags),
    )


@pytest.mark.parametrize("rounding", ["ROUND_DOWN", "ROUND_UP", "ROUND_HALF_EVEN"])
def test_public_plan_ignores_decimal_context(inputs, tmp_path, rounding):
    from decimal import localcontext

    # More significant digits than even the default 28-digit context, plus
    # an exponent outside the deliberately hostile caller context.
    exact_x = "10.5000000000000000000000000000000000000000000001"
    rewrite_csv(inputs, "cpl", lambda rows: rows[1].__setitem__(3, exact_x + "mm"))
    rewrite_csv(inputs, "cpl", lambda rows: rows[1].__setitem__(4, "1e-1000mm"))
    normal = sp.prepare_submission(**inputs)
    matches = json.loads((normal.directory / "expected-matches.json").read_bytes())
    assert matches[0]["x_mm"] == exact_x
    assert matches[0]["y_mm"] == "1E-1000"
    with localcontext() as context:
        context.prec = 2
        context.rounding = rounding
        context.Emin, context.Emax = -1, 1
        context.capitals, context.clamp = 0, 1
        for signal in context.traps:
            context.traps[signal] = True
        before = _context_signature(context)
        other = sp.prepare_submission(**(inputs | {"destination": tmp_path / "hostile"}))
        assert _context_signature(context) == before
    assert other.plan_bytes == normal.plan_bytes
    assert other.sha256 == normal.sha256
    assert (other.directory / "expected-matches.json").read_bytes() == (
        normal.directory / "expected-matches.json"
    ).read_bytes()


@pytest.mark.parametrize("value", ["1000001", "-1000001", "1000000.000000000000000000000000000001"])
def test_numeric_limit_is_exact_under_rounding(inputs, value):
    from decimal import localcontext

    rewrite_csv(inputs, "cpl", lambda rows: rows[1].__setitem__(3, value))
    with localcontext() as context:
        context.prec = 2
        context.rounding = "ROUND_DOWN"
        before = _context_signature(context)
        with pytest.raises(sp.SubmissionError, match="out-of-range"):
            sp.prepare_submission(**inputs)
        assert _context_signature(context) == before
    assert not inputs["destination"].exists()


def test_invalid_decimal_does_not_change_caller_flags(inputs):
    from decimal import localcontext

    rewrite_csv(inputs, "cpl", lambda rows: rows[1].__setitem__(3, "not-a-number"))
    with localcontext() as context:
        for signal in context.traps:
            context.traps[signal] = False
        before = _context_signature(context)
        with pytest.raises(sp.SubmissionError):
            sp.prepare_submission(**inputs)
        assert _context_signature(context) == before
