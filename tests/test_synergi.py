"""Synergi -> OpenDSS on the sample model/warehouse pair from DiTTo's history.

The two databases are not committed (see README, "Test data"); these tests
skip when they are absent.
"""

from __future__ import annotations

import pytest

from opendss_designer_convert import ConversionError, convert

from .helpers import (
    HAVE_SYNERGI,
    SYNERGI_MODEL,
    SYNERGI_WAREHOUSE,
    check_output_is_plain,
    compile_and_solve,
    write_conversion,
)

pytestmark = pytest.mark.skipif(
    not HAVE_SYNERGI, reason="Synergi samples not present (see README, 'Test data')"
)


@pytest.fixture(scope="module")
def synergi_result():
    files = [
        (SYNERGI_WAREHOUSE.name, SYNERGI_WAREHOUSE.read_bytes()),
        (SYNERGI_MODEL.name, SYNERGI_MODEL.read_bytes()),
    ]
    return convert("synergi", files)


def test_synergi_sample_converts_compiles_and_converges(synergi_result, tmp_path):
    check_output_is_plain(synergi_result)
    solved = compile_and_solve(write_conversion(synergi_result, tmp_path))
    assert solved.converged
    assert solved.control_warning is None
    assert solved.buses > 800
    counts = solved.elements
    assert counts["vsource"] == 1
    assert counts["load"] == 179
    assert counts["regcontrol"] == 3
    # 3 single-phase regulators plus the 12.47/11.5 kV unit built from its name.
    assert counts["transformer"] == 4
    assert counts["line"] > 800  # lines, switches and fuses are all OpenDSS lines
    assert 4000 < solved.total_kw < 6000


def test_synergi_power_flow_without_controls(synergi_result, tmp_path):
    solved = compile_and_solve(write_conversion(synergi_result, tmp_path), controls=False)
    assert solved.converged
    assert solved.min_pu > 0.8


def test_synergi_source_is_the_feeder_node(synergi_result):
    """The source sits on the node named after the feeder, every time."""
    master = dict(synergi_result.files)["Master.dss"]
    assert "New Circuit.vsource_MIKILUA_4 Bus1=MIKILUA_4.1.2.3 " in master


def test_synergi_bus_coordinates(synergi_result):
    files = dict(synergi_result.files)
    rows = files["BusCoords.csv"].splitlines()
    assert len(rows) > 800
    bus, x, y = rows[0].split(",")
    assert float(x) and float(y)


def test_synergi_missing_warehouse_transformer_is_reported(synergi_result):
    assert any("not in the warehouse" in w for w in synergi_result.warnings)
    assert any("turned it" in w for w in synergi_result.warnings)


def test_synergi_needs_both_databases():
    with pytest.raises(ConversionError, match="warehouse"):
        convert("synergi", [("m4.mdb", SYNERGI_MODEL.read_bytes())])
    with pytest.raises(ConversionError, match="model"):
        convert("synergi", [("Warehouse.mdb", SYNERGI_WAREHOUSE.read_bytes())])


def test_synergi_two_models_are_refused():
    data = SYNERGI_MODEL.read_bytes()
    with pytest.raises(ConversionError, match="Two Synergi model databases"):
        convert("synergi", [("a.mdb", data), ("b.mdb", data)])
