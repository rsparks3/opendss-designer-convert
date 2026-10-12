"""CYME -> OpenDSS on DiTTo's CYME samples, checked by compiling with OpenDSS."""

from __future__ import annotations

import re

import numpy as np
import pytest

from opendss_designer_convert import convert

from .helpers import (
    CYME,
    CYME_SAMPLES,
    REFERENCE,
    check_output_is_plain,
    compile_and_solve,
    cyme_files,
    reduced_metrics,
    regulator_taps,
    write_conversion,
)

# What each sample must contain after conversion (OpenDSS element counts).
EXPECTED = {
    "4_node": {"vsource": 1, "line": 3, "transformer": 1, "load": 3},
    "13_node": {"vsource": 1, "transformer": 4, "regcontrol": 3, "capacitor": 2, "load": 17},
    "ieee_123_node": {"vsource": 1, "transformer": 8, "regcontrol": 7, "capacitor": 4, "load": 95},
}


@pytest.mark.parametrize("sample", CYME_SAMPLES)
def test_cyme_sample_converts_compiles_and_converges(sample, tmp_path):
    result = convert("cyme", cyme_files(sample))
    check_output_is_plain(result)
    assert result.main == "Master.dss"

    main = write_conversion(result, tmp_path)
    solved = compile_and_solve(main)
    assert solved.converged
    assert solved.control_warning is None
    for cls, count in EXPECTED[sample].items():
        assert solved.elements.get(cls, 0) == count, (cls, solved.elements)
    assert 0.8 < solved.min_pu <= solved.max_pu < 1.1
    assert solved.total_kw > 0


@pytest.mark.parametrize("sample", CYME_SAMPLES)
def test_cyme_bus_coordinates(sample, tmp_path):
    import opendssdirect as dss

    result = convert("cyme", cyme_files(sample))
    files = dict(result.files)
    assert "BusCoords.csv" in files
    rows = [line.split(",") for line in files["BusCoords.csv"].splitlines()]
    assert rows and all(len(r) == 3 for r in rows)
    for _, x, y in rows:
        float(x), float(y)
    assert "Buscoords BusCoords.csv" in files["Master.dss"]

    compile_and_solve(write_conversion(result, tmp_path))
    bus = rows[0][0]
    dss.Circuit.SetActiveBus(bus)
    assert dss.Bus.Coorddefined()
    assert dss.Bus.X() == pytest.approx(float(rows[0][1]))


@pytest.mark.parametrize(
    ("sample", "reference"),
    [
        ("13_node", REFERENCE / "ieee13" / "Master.dss"),
        ("ieee_123_node", REFERENCE / "123Bus" / "IEEE123Master.dss"),
    ],
)
def test_cyme_matches_ieee_reference(sample, reference, tmp_path):
    """Upstream DiTTo's acceptance test: within 1 % of EPRI's IEEE feeder models.

    Source at 1.0 pu, regulators off and taps at 1, as upstream compares.
    """
    main = write_conversion(convert("cyme", cyme_files(sample)), tmp_path)
    ref = reduced_metrics(reference)
    got = reduced_metrics(main)
    assert np.allclose(ref, got, rtol=0.01, atol=0.01), (ref, got)


def test_regulator_bank_is_one_unit_per_phase():
    """Upstream issue #77: a bank of single-phase regulators kept its phases apart."""
    files = dict(convert("cyme", cyme_files("13_node")).files)
    controls = [line for line in files["RegControllers.dss"].splitlines() if line.strip()]
    assert len(controls) == 3
    for phase, line in zip("ABC", controls):
        assert f"RegControl.650REG_{phase} " in line
        assert f"Transformer=650REG_{phase} " in line
        # Regulating at its own terminal: `Bus=` would switch line-drop compensation off.
        assert " Bus=" not in line
    units = re.findall(r"new Transformer\.650REG_([ABC]) Phases=1 ", files["Transformers.dss"])
    assert units == ["A", "B", "C"]
    # Source-side winding at tap 1.0; CYME's starting tap on the load side.
    assert "Taps=[1.0, 1.0625]" in files["Transformers.dss"]


def test_ieee123_regulators_regulate_like_the_reference(tmp_path):
    """Reg 1 (150-149) is a ganged three-phase unit with R and X line-drop
    compensation; reg 4 (160-67) is three single-phase units. With controls on,
    the ganged unit settles on the same tap as EPRI's IEEE 123 model."""
    result = convert("cyme", cyme_files("ieee_123_node"))
    files = dict(result.files)
    assert "PTPhase=1" in files["RegControllers.dss"]
    assert re.search(r"Transformer\.150-149_1 Phases=3 ", files["Transformers.dss"])
    for phase in "ABC":
        assert f"new Transformer.160-67_{phase} Phases=1 " in files["Transformers.dss"]

    ref = regulator_taps(REFERENCE / "123Bus" / "IEEE123Master.dss")
    got = regulator_taps(write_conversion(result, tmp_path))
    assert got["150-149_1"] == pytest.approx(ref["reg1a"])


def test_single_self_contained_export(tmp_path):
    parts = [data for _, data in cyme_files("13_node")]
    combined = b"\r\n".join(p.rstrip() + b"\r\n" for p in parts)
    result = convert("cyme", [("model.txt", combined)])
    solved = compile_and_solve(write_conversion(result, tmp_path))
    assert solved.converged
    assert solved.elements.get("load") == EXPECTED["13_node"]["load"]


def test_file_order_and_names_do_not_matter(tmp_path):
    files = cyme_files("13_node")
    renamed = [(f"part{i}.txt", data) for i, (_, data) in enumerate(reversed(files))]
    solved = compile_and_solve(write_conversion(convert("cyme", renamed), tmp_path))
    assert solved.elements.get("load") == EXPECTED["13_node"]["load"]


def test_without_a_load_file_the_model_has_no_loads(tmp_path):
    result = convert("cyme", cyme_files("13_node", ("Network.txt", "Equipment.txt")))
    assert any("no loads" in w for w in result.warnings)
    solved = compile_and_solve(write_conversion(result, tmp_path))
    assert solved.converged
    assert solved.elements.get("load", 0) == 0


@pytest.mark.parametrize("encoding", ["utf-16", "cp1252", "utf-8-sig"])
def test_text_encodings(encoding, tmp_path):
    files = [
        (name, data.decode("latin-1").encode(encoding)) for name, data in cyme_files("4_node")
    ]
    solved = compile_and_solve(write_conversion(convert("cyme", files), tmp_path))
    assert solved.converged


def test_several_load_models_take_the_first(tmp_path):
    network, equipment, load = cyme_files("13_node")
    text = load[1].decode("latin-1")
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("[CUSTOMER LOADS]"))
    header = lines[start + 1]
    col = header.split("=", 1)[1].split(",").index("LoadModelID")
    end = start + 2
    while end < len(lines) and lines[end].strip():
        end += 1
    rows = lines[start + 2 : end]
    second = []
    for row in rows:
        cells = row.split(",")
        cells[col] = "2"
        second.append(",".join(cells))
    lines[end:end] = second
    load = ("Load.txt", "\r\n".join(lines).encode("latin-1"))

    result = convert("cyme", [network, equipment, load])
    assert any("2 load models" in w for w in result.warnings)
    solved = compile_and_solve(write_conversion(result, tmp_path))
    assert solved.elements.get("load") == EXPECTED["13_node"]["load"]


def test_second_source_does_not_replace_the_circuit(tmp_path):
    """Legacy DiTTo issues #402/#422/#427: a second source used to become a
    second `New Circuit`, which wipes out everything defined before it."""
    network, equipment, load = cyme_files("13_node")
    text = network[1].decode("latin-1")
    marker = "SUB650WYE,SUB650WYE-S2,650,0,650,4.160000,,0,,,,,,,,,"
    assert marker in text
    text = text.replace(marker, marker + "\r\nSUB680,SUB680-S2,680,0,650,4.160000,,0,,,,,,,,,")
    result = convert("cyme", [("Network.txt", text.encode("latin-1")), equipment, load])
    master = dict(result.files)["Master.dss"]
    assert master.count("New Circuit.") == 1
    assert len(re.findall(r"(?im)^new vsource\.", master)) == 1
    assert any("More than one voltage source" in w for w in result.warnings)
    solved = compile_and_solve(write_conversion(result, tmp_path))
    assert solved.converged
    assert solved.elements["vsource"] == 2
    assert solved.elements.get("load") == EXPECTED["13_node"]["load"]


def test_samples_are_the_expected_cyme_versions():
    versions = {
        sample: re.search(
            r"CYME_VERSION=([\d.]+)", (CYME / sample / "Network.txt").read_text("latin-1")
        ).group(1)
        for sample in CYME_SAMPLES
    }
    assert versions == {"4_node": "8.00", "13_node": "5.04", "ieee_123_node": "8.00"}
