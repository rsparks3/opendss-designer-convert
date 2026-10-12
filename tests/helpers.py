"""Shared test helpers: sample locations, writing a Conversion, running OpenDSS."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import opendssdirect as dss

DATA = Path(__file__).parent / "data"
CYME = DATA / "cyme_models"
REFERENCE = DATA / "opendss_circuit_models"
CYME_SAMPLES = ["4_node", "13_node", "ieee_123_node"]

# The Synergi samples are not committed (see README, "Test data"). Put them in
# tests/data/synergi/ or point ODC_SYNERGI_SAMPLES at a folder holding them.
SYNERGI_DIR = Path(os.environ.get("ODC_SYNERGI_SAMPLES", DATA / "synergi"))
SYNERGI_MODEL = SYNERGI_DIR / "m4.mdb"
SYNERGI_WAREHOUSE = SYNERGI_DIR / "Warehouse.mdb"
HAVE_SYNERGI = SYNERGI_MODEL.is_file() and SYNERGI_WAREHOUSE.is_file()

# Commands a converted model must never contain.
FORBIDDEN = re.compile(
    r"^\s*(solve|show|export|plot|visualize|save|dump|clear|compile|cd|doscmd|docmd|"
    r"fileedit|set\s+datapath|set\s+editor)\b",
    re.IGNORECASE,
)
REDIRECT = re.compile(r"^\s*(redirect|buscoords)\s+(\S+)", re.IGNORECASE)


def cyme_files(sample: str, names=("Network.txt", "Equipment.txt", "Load.txt")):
    return [(name, (CYME / sample / name).read_bytes()) for name in names]


def write_conversion(result, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in result.files:
        (folder / name).write_text(text, encoding="utf-8")
    return folder / result.main


def check_output_is_plain(result) -> None:
    """Every reference is a bare name that exists; nothing runs or exports."""
    names = {name for name, _ in result.files}
    assert result.main in names
    assert any("New Circuit." in text for name, text in result.files if name == result.main)
    for name, text in result.files:
        assert "/" not in name and "\\" not in name
        for line in text.splitlines():
            assert not FORBIDDEN.match(line), f"{name}: {line}"
            m = REDIRECT.match(line)
            if m:
                assert m.group(2) in names, f"{name} references missing {m.group(2)}"


@dataclass
class Solved:
    converged: bool
    control_warning: str | None
    buses: int
    total_kw: float
    min_pu: float
    max_pu: float
    elements: dict[str, int]


def compile_and_solve(main: Path, controls: bool = True) -> Solved:
    dss.Text.Command("clear")
    dss.Text.Command(f'compile "{main.resolve()}"')
    if not controls:
        dss.Text.Command("set controlmode=off")
    warning = None
    try:
        dss.Text.Command("solve")
    except Exception as exc:  # OpenDSS reports control non-convergence as an error
        if "Max Control Iterations" not in str(exc):
            raise
        warning = str(exc).splitlines()[0]
    volts = [v for v in dss.Circuit.AllBusMagPu() if v > 0.05]
    elements: dict[str, int] = {}
    for full in dss.Circuit.AllElementNames():
        cls = full.split(".", 1)[0].lower()
        elements[cls] = elements.get(cls, 0) + 1
    return Solved(
        converged=bool(dss.Solution.Converged()),
        control_warning=warning,
        buses=dss.Circuit.NumBuses(),
        total_kw=-dss.Circuit.TotalPower()[0],
        min_pu=min(volts),
        max_pu=max(volts),
        elements=elements,
    )


def reduced_metrics(master: Path) -> list[float]:
    """Upstream DiTTo's comparison: source at 1.0 pu, regulators off, taps at 1.

    Returns min, max and mean node voltage (pu) and feeder-head P and Q.
    """
    dss.Text.Command("clear")
    dss.Text.Command(f'redirect "{master.resolve()}"')
    dss.Text.Command("Batchedit vsource..* pu=1.0")
    dss.Text.Command("Batchedit regcontrol..* enabled=false")
    dss.Text.Command("Batchedit Transformer..* taps=[1, 1]")
    dss.Solution.Solve()
    p, q = dss.Circuit.TotalPower()
    volts = dss.Circuit.AllBusMagPu()
    return [min(volts), max(volts), sum(volts) / len(volts), p, q]


def regulator_taps(master: Path) -> dict[str, float]:
    """Winding-2 taps of every transformer after a solve with controls on."""
    dss.Text.Command("clear")
    dss.Text.Command(f'compile "{master.resolve()}"')
    dss.Text.Command("set maxcontroliter=100")
    dss.Text.Command("solve")
    taps = {}
    for name in dss.Transformers.AllNames():
        dss.Transformers.Name(name)
        dss.Transformers.Wdg(2)
        taps[name] = round(dss.Transformers.Tap(), 5)
    return taps
