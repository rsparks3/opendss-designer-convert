"""The plugin contract and the web-server safety rules of convert()."""

from __future__ import annotations

import dataclasses
import socket
import subprocess
import tempfile

import pytest

import opendss_designer_convert as odc
from opendss_designer_convert import FORMATS, Conversion, ConversionError, Format, convert
from opendss_designer_convert import api as api_module

from .helpers import cyme_files


def test_public_api():
    assert isinstance(odc.__version__, str) and odc.__version__
    assert set(FORMATS) == {"cyme", "synergi"}
    for key, fmt in FORMATS.items():
        assert isinstance(fmt, Format)
        assert fmt.key == key
        assert fmt.label and fmt.help
        assert fmt.max_files >= 1
        assert all(e.startswith(".") and e == e.lower() for e in fmt.extensions)
    assert FORMATS["cyme"].label == "CYME"
    assert FORMATS["synergi"].label == "Synergi Electric"
    assert FORMATS["cyme"].extensions == (".txt",)
    assert FORMATS["synergi"].extensions == (".mdb",)
    with pytest.raises(dataclasses.FrozenInstanceError):
        FORMATS["cyme"].max_files = 99  # type: ignore[misc]
    assert {f.name for f in dataclasses.fields(Conversion)} == {"files", "main", "warnings"}
    assert issubclass(ConversionError, Exception)


def test_windmil_is_refused_plainly():
    with pytest.raises(ConversionError, match="WindMil"):
        convert("windmil", [("model.std", b"x")])


@pytest.mark.parametrize("fmt", ["", "dew", "gridlabd", None])
def test_unknown_format(fmt):
    with pytest.raises(ConversionError, match="Unknown format"):
        convert(fmt, [("a.txt", b"x")])


def test_format_key_is_case_insensitive():
    with pytest.raises(ConversionError, match="does not look like a CYME"):
        convert("CYME", [("a.txt", b"hello")])


def test_no_files():
    with pytest.raises(ConversionError, match="No files"):
        convert("cyme", [])


def test_too_many_files():
    files = [(f"f{i}.txt", b"x") for i in range(FORMATS["cyme"].max_files + 1)]
    with pytest.raises(ConversionError, match="Too many files"):
        convert("cyme", files)
    with pytest.raises(ConversionError, match="Too many files"):
        convert("synergi", [("a.mdb", b"x"), ("b.mdb", b"x"), ("c.mdb", b"x")])


@pytest.mark.parametrize(
    "name",
    [
        "",
        "..",
        ".",
        ".hidden.txt",
        "a b.txt",
        "name;rm.txt",
        "caf\u00e9.txt",
        "x\x00.txt",
        "a" * 200,
    ],
)
def test_unsafe_names_are_refused(name):
    with pytest.raises(ConversionError, match="not a usable file name"):
        convert("cyme", [(name, b"[SECTION]\n")])


def test_directory_parts_are_stripped():
    # Same content under traversal-style names: only the base name is used,
    # and the conversion proceeds as if the bare names had been given.
    files = [
        ("../../etc/" + name, data) if i == 0 else ("C:\\Users\\x\\" + name, data)
        for i, (name, data) in enumerate(cyme_files("4_node"))
    ]
    result = convert("cyme", files)
    assert result.main == "Master.dss"


def test_wrong_extension():
    with pytest.raises(ConversionError, match=r"expected \.txt"):
        convert("cyme", [("network.csv", b"[SECTION]\n")])
    with pytest.raises(ConversionError, match=r"expected \.mdb"):
        convert("synergi", [("model.accdb", b"x")])


def test_empty_and_duplicate_files():
    with pytest.raises(ConversionError, match="empty"):
        convert("cyme", [("a.txt", b"")])
    with pytest.raises(ConversionError, match="twice"):
        convert("cyme", [("a.txt", b"x"), ("A.TXT", b"y")])


def test_contents_must_be_bytes():
    with pytest.raises(ConversionError, match="bytes"):
        convert("cyme", [("a.txt", "text, not bytes")])


def test_size_limit(monkeypatch):
    monkeypatch.setattr(api_module, "MAX_TOTAL_BYTES", 10)
    with pytest.raises(ConversionError, match="too large"):
        convert("cyme", [("a.txt", b"x" * 11)])


def test_garbage_text_is_not_cyme():
    with pytest.raises(ConversionError, match="does not look like a CYME ASCII export"):
        convert("cyme", [("notes.txt", b"just some notes\nnothing to see\n")])


def test_binary_is_not_cyme():
    with pytest.raises(ConversionError, match="not a text file"):
        convert("cyme", [("network.txt", b"\x00\x01\x02[SECTION]\x00")])


def test_garbage_is_not_synergi():
    with pytest.raises(ConversionError, match="not a readable Microsoft Access database"):
        convert("synergi", [("model.mdb", b"definitely not an access database" * 100)])


def test_cyme_needs_network_and_equipment():
    files = dict(cyme_files("13_node"))
    with pytest.raises(ConversionError, match="equipment"):
        convert("cyme", [("Network.txt", files["Network.txt"])])
    with pytest.raises(ConversionError, match="network"):
        convert(
            "cyme",
            [("Equipment.txt", files["Equipment.txt"]), ("Load.txt", files["Load.txt"])],
        )


def test_two_network_files_are_refused():
    net = dict(cyme_files("13_node"))["Network.txt"]
    with pytest.raises(ConversionError, match="More than one network file"):
        convert("cyme", [("a.txt", net), ("b.txt", net)])


def test_unexpected_errors_are_wrapped(monkeypatch, caplog):
    from opendss_designer_convert.readers.cyme import reader as cyme_reader

    def boom(*args, **kwargs):
        raise RuntimeError(r"secret detail C:\private\path\Network.txt")

    monkeypatch.setattr(cyme_reader.Reader, "__init__", boom)
    with pytest.raises(ConversionError) as info:
        convert("cyme", cyme_files("4_node"))
    message = str(info.value)
    assert "secret" not in message and "private" not in message and "Traceback" not in message
    assert info.value.__suppress_context__
    # The detail is logged for the operator.
    assert "secret detail" in caplog.text


def test_writer_failures_are_wrapped(monkeypatch):
    from opendss_designer_convert.writers.opendss import write

    def boom(self, *args, **kwargs):
        raise KeyError("/tmp/odc-xyz/out")

    monkeypatch.setattr(write.Writer, "write", boom)
    with pytest.raises(ConversionError, match="could not be written as OpenDSS") as info:
        convert("cyme", cyme_files("4_node"))
    assert "/tmp" not in str(info.value)


def test_temp_dir_is_always_removed(monkeypatch):
    made = []
    real = tempfile.mkdtemp

    def spy(*args, **kwargs):
        path = real(*args, **kwargs)
        made.append(path)
        return path

    monkeypatch.setattr(api_module.tempfile, "mkdtemp", spy)
    convert("cyme", cyme_files("4_node"))
    with pytest.raises(ConversionError):
        convert("cyme", [("notes.txt", b"nothing")])
    assert len(made) == 2
    import os

    assert not any(os.path.exists(p) for p in made)


def test_no_subprocess_or_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("convert() must not start processes or open sockets")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result = convert("cyme", cyme_files("4_node"))
    assert result.files


def test_warnings_are_collected_even_when_logging_is_quiet(monkeypatch):
    import logging

    root = logging.getLogger()
    monkeypatch.setattr(root, "level", logging.CRITICAL)
    result = convert("cyme", cyme_files("13_node"))
    assert any("Skipping" in w for w in result.warnings)
    assert all(len(w) <= 340 for w in result.warnings)
    assert logging.getLogger("opendss_designer_convert").level == logging.NOTSET
