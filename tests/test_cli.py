"""The opendss-designer-convert command line."""

from __future__ import annotations

from opendss_designer_convert.cli import main

from .helpers import CYME, compile_and_solve


def test_cli_converts_cyme(tmp_path, capsys):
    folder = CYME / "13_node"
    args = ["cyme", *(str(folder / n) for n in ("Network.txt", "Equipment.txt", "Load.txt"))]
    assert main([*args, "-o", str(tmp_path / "out")]) == 0
    out = capsys.readouterr()
    assert "Master.dss" in out.out
    assert "warning:" in out.err
    assert compile_and_solve(tmp_path / "out" / "Master.dss").converged


def test_cli_reports_errors(tmp_path, capsys):
    bad = tmp_path / "notes.txt"
    bad.write_text("nothing here")
    assert main(["cyme", str(bad), "-o", str(tmp_path / "out")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "Traceback" not in err
    assert not (tmp_path / "out").exists()


def test_cli_missing_file(tmp_path, capsys):
    assert main(["cyme", str(tmp_path / "missing.txt"), "-o", str(tmp_path)]) == 2
    assert "cannot read" in capsys.readouterr().err
