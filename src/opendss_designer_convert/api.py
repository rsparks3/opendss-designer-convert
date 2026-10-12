"""The plugin contract: convert uploaded CYME or Synergi files to OpenDSS text.

Everything here is written to run on a public web server:

* work happens only inside a private temporary directory that is always
  removed, with uploaded files written under sanitised base names;
* no network access and no subprocesses;
* every failure surfaces as :class:`ConversionError` whose message is safe to
  show a user (no paths, no tracebacks); the detail goes to :mod:`logging`.

The readers and the writer (and their heavy dependencies) are imported only
when :func:`convert` runs, so importing this module to list
:data:`FORMATS` stays cheap.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_PACKAGE_LOGGER = "opendss_designer_convert"


@dataclass(frozen=True)
class Format:
    """A source format this package can convert."""

    key: str
    label: str
    extensions: tuple[str, ...]
    help: str
    max_files: int


@dataclass
class Conversion:
    """The OpenDSS model produced by :func:`convert`."""

    files: list[tuple[str, str]]
    main: str
    warnings: list[str]


class ConversionError(Exception):
    """Conversion could not proceed; str(exc) is safe to show a user (no paths, no tracebacks)."""


FORMATS: dict[str, Format] = {
    "cyme": Format(
        key="cyme",
        label="CYME",
        extensions=(".txt",),
        help=(
            "Select the CYME ASCII exports (.txt): the network file and the equipment "
            "file, plus the load file if loads were exported separately. A single "
            "self-contained export also works."
        ),
        max_files=3,
    ),
    "synergi": Format(
        key="synergi",
        label="Synergi Electric",
        extensions=(".mdb",),
        help=(
            "Select the Synergi model database (.mdb) and its equipment warehouse "
            "database (.mdb). If the equipment tables are inside the model database, "
            "the model database alone is enough."
        ),
        max_files=2,
    ),
}

# Formats people will look for that are not available, with the reason.
_UNAVAILABLE = {
    "windmil": (
        "Milsoft WindMil conversion is not available: there is no WindMil reader "
        "this converter could verify against real WindMil data."
    ),
}

#: Refuse uploads larger than this in total (bytes).
MAX_TOTAL_BYTES = 256 * 1024 * 1024

#: At most this many warnings are returned; the rest are counted.
MAX_WARNINGS = 50

_SAFE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def convert(fmt: str, files: list[tuple[str, bytes]]) -> Conversion:
    """Convert a CYME or Synergi model to OpenDSS.

    Args:
        fmt: A key of :data:`FORMATS` (``"cyme"`` or ``"synergi"``).
        files: ``(file name, content)`` pairs as uploaded. Only the base name
            is used; it must be plain letters, digits, dots, dashes or
            underscores.

    Returns:
        The OpenDSS files (all referenced by bare file name), the name of the
        one that defines the circuit, and warnings that are safe to show.

    Raises:
        ConversionError: the input is unsupported, missing, garbled or could
            not be converted. The message is safe to show a user.
    """
    key = str(fmt or "").strip().lower()
    if key in _UNAVAILABLE:
        raise ConversionError(_UNAVAILABLE[key])
    if key not in FORMATS:
        known = ", ".join(sorted(FORMATS))
        raise ConversionError(f"Unknown format '{str(fmt)[:40]}'. Available formats: {known}.")
    spec = FORMATS[key]
    uploads = _check_uploads(spec, files)

    _quiet_dependencies()
    tmpdir = Path(tempfile.mkdtemp(prefix="odc-"))
    collector = _WarningCollector(str(tmpdir))
    _attach(collector)
    try:
        src = tmpdir / "in"
        out = tmpdir / "out"
        src.mkdir()
        out.mkdir()
        if key == "cyme":
            system = _read_cyme(src, uploads, collector)
        else:
            system = _read_synergi(src, uploads, collector)
        return _write_opendss(system, out, spec, collector)
    except ConversionError:
        raise
    except Exception:
        logger.exception("Unexpected failure converting %s files", spec.label)
        raise ConversionError(
            f"The {spec.label} model could not be converted. The files may be incomplete, "
            "or the model may use something this converter does not support."
        ) from None
    finally:
        _detach(collector)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _quiet_dependencies() -> None:
    """Silence grid-data-models and infrasys, which log through loguru.

    They print to stderr by default (one GDM check lists every bus with an
    unconnected neutral), which on a web server only fills the log. This is
    process-wide, but only for those two libraries.
    """
    try:
        from loguru import logger as loguru_logger
    except ImportError:  # pragma: no cover - loguru comes with infrasys
        return
    loguru_logger.disable("gdm")
    loguru_logger.disable("infrasys")


# --------------------------------------------------------------------------- uploads


def _check_uploads(spec: Format, files) -> list[tuple[str, bytes]]:
    if not isinstance(files, (list, tuple)) or not files:
        raise ConversionError(f"No files were provided. {spec.help}")
    if len(files) > spec.max_files:
        raise ConversionError(
            f"Too many files: {spec.label} takes at most {spec.max_files} "
            f"({len(files)} were selected). {spec.help}"
        )
    seen: set[str] = set()
    total = 0
    uploads = []
    for item in files:
        try:
            raw_name, data = item
        except (TypeError, ValueError):
            raise ConversionError("Each file must be given as a (name, content) pair.") from None
        if isinstance(data, (bytearray, memoryview)):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise ConversionError("File contents must be bytes.")
        name = _safe_name(raw_name)
        if name.lower() in seen:
            raise ConversionError(f"The file name '{name}' was given twice.")
        seen.add(name.lower())
        if Path(name).suffix.lower() not in spec.extensions:
            accepted = ", ".join(spec.extensions)
            raise ConversionError(
                f"'{name}' is not a {spec.label} file: expected {accepted}. {spec.help}"
            )
        if not data:
            raise ConversionError(f"'{name}' is empty.")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ConversionError(
                f"The files are too large (more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB "
                "together)."
            )
        uploads.append((name, data))
    return uploads


def _safe_name(raw_name) -> str:
    base = str(raw_name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not _SAFE_NAME_RE.fullmatch(base):
        shown = base[:60] or "(no name)"
        raise ConversionError(
            f"'{shown}' is not a usable file name. Rename the file to plain letters, "
            "digits, dots, dashes or underscores and try again."
        )
    return base


# --------------------------------------------------------------------------- CYME

# Sections that identify each kind of CYME ASCII export.
_CYME_NETWORK = {"SECTION", "NODE"}
_CYME_EQUIPMENT = {
    "CONDUCTOR", "CABLE", "LINE", "LINE UNBALANCED", "TRANSFORMER",
    "THREE WINDING TRANSFORMER", "REGULATOR", "SWITCH", "BREAKER", "FUSE",
    "RECLOSER", "SHUNT CAPACITOR",
}  # fmt: skip
_CYME_LOAD = {"LOADS", "CUSTOMER LOADS"}
_CYME_HEADER_RE = re.compile(r"^\[([^\]\r\n]{1,80})\]", re.MULTILINE)


def _decode_text(name: str, data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            # CYME on Windows writes the ANSI code page.
            text = data.decode("cp1252", errors="replace")
    if "\x00" in text:
        raise ConversionError(f"'{name}' is not a text file, so it is not a CYME ASCII export.")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_cyme(src: Path, uploads, collector):
    roles: dict[str, list[str]] = {"network": [], "equipment": [], "load": []}
    for name, data in uploads:
        text = _decode_text(name, data)
        headers = {h.strip().upper() for h in _CYME_HEADER_RE.findall(text)}
        kinds = [
            kind
            for kind, marks in (
                ("network", _CYME_NETWORK),
                ("equipment", _CYME_EQUIPMENT),
                ("load", _CYME_LOAD),
            )
            if headers & marks
        ]
        if not kinds:
            raise ConversionError(
                f"'{name}' does not look like a CYME ASCII export (none of the expected "
                "[SECTION], [CONDUCTOR] or [CUSTOMER LOADS] blocks were found)."
            )
        (src / name).write_text(text, encoding="utf-8")
        for kind in kinds:
            roles[kind].append(name)

    network = _one_role(roles, "network", "network")
    if network is None:
        raise ConversionError(
            "None of the files is a CYME network export (it has the [SECTION] and [NODE] "
            "blocks). Select the network file as well."
        )
    # A file that is both network and equipment is a self-contained export; a
    # separate equipment (or load) file takes precedence over its copy.
    equipment = _one_role(roles, "equipment", "equipment", prefer_not=network)
    if equipment is None:
        raise ConversionError(
            "None of the files is a CYME equipment export (conductors, cables, "
            "transformers). Select the equipment file as well."
        )
    load = _one_role(roles, "load", "load", prefer_not=network)
    if load is None:
        collector.note("No CYME load file was selected, so the converted model has no loads.")
        load = network

    from opendss_designer_convert.readers.cyme.reader import Reader

    try:
        reader = Reader(src / network, src / equipment, src / load)
    except ConversionError:
        raise
    except Exception:
        logger.exception("CYME reader failed")
        raise ConversionError(
            "The CYME files could not be read. Check that they are the network, equipment "
            "and load exports of the same model, and that the export is complete."
        ) from None
    return reader.get_system()


def _one_role(roles, kind, label, prefer_not=None):
    names = roles[kind]
    if len(names) > 1 and prefer_not in names:
        names = [n for n in names if n != prefer_not]
    if len(names) > 1:
        raise ConversionError(
            f"More than one {label} file was selected ({', '.join(names)}). "
            f"Select one {label} file per model."
        )
    return names[0] if names else None


# --------------------------------------------------------------------------- Synergi

_SYNERGI_MODEL_TABLES = ("InstSection", "Node")
_SYNERGI_EQUIPMENT_TABLES = ("DevConductors", "DevTransformers")


def _read_synergi(src: Path, uploads, collector):
    from opendss_designer_convert.readers.synergi.utils import SynergiDatabase

    models, warehouses = [], []
    databases = {}
    for name, data in uploads:
        path = src / name
        path.write_bytes(data)
        try:
            db = SynergiDatabase(path)
        except Exception:
            logger.info("Not an Access database: %s", name, exc_info=True)
            raise ConversionError(
                f"'{name}' is not a readable Microsoft Access database, so it is not a "
                "Synergi model."
            ) from None
        databases[name] = db
        is_model = all(db.has_table(t) for t in _SYNERGI_MODEL_TABLES)
        is_warehouse = all(db.has_table(t) for t in _SYNERGI_EQUIPMENT_TABLES)
        if not (is_model or is_warehouse):
            raise ConversionError(
                f"'{name}' is an Access database but not a Synergi model or equipment "
                "warehouse (it has neither the InstSection nor the DevConductors table)."
            )
        if is_model:
            models.append(name)
        if is_warehouse:
            warehouses.append(name)

    if not models:
        raise ConversionError(
            "None of the databases is a Synergi model (with the InstSection table). "
            "Select the model database as well as the warehouse."
        )
    if len(models) > 1:
        only_models = [m for m in models if m not in warehouses]
        if len(only_models) != 1:
            raise ConversionError("Two Synergi model databases were selected. Select one.")
        models = only_models
    model = models[0]
    others = [w for w in warehouses if w != model]
    if len(others) > 1:
        raise ConversionError("Two Synergi warehouse databases were selected. Select one.")
    warehouse = others[0] if others else (model if model in warehouses else None)
    if warehouse is None:
        raise ConversionError(
            "The equipment warehouse database (with the DevConductors and DevTransformers "
            "tables) is missing. Select it together with the model database."
        )

    from opendss_designer_convert.readers.synergi.reader import Reader

    try:
        reader = Reader(databases[model], databases[warehouse])
    except ConversionError:
        raise
    except Exception:
        logger.exception("Synergi reader failed")
        raise ConversionError(
            "The Synergi databases could not be read. Check that the warehouse belongs to "
            "the model and that the model is complete."
        ) from None
    return reader.get_system()


# --------------------------------------------------------------------------- writing

_MASTER = "Master.dss"
_BUSCOORDS_DSS = "BusCoords.dss"
_BUSCOORDS_CSV = "BusCoords.csv"

# Commands that run, show, export or repoint something. A converted model is a
# circuit definition only.
_DROP_VERBS = {
    "clear", "solve", "show", "export", "plot", "visualize", "save", "dump", "fileedit",
    "docmd", "dosimplecmd", "doscmd", "cd", "compile", "buscoords", "latlongcoords",
    "makebuslist", "setkvbase", "setbusxy", "sample", "reset", "_initsnap",
}  # fmt: skip
_VERB_RE = re.compile(r"^\s*([A-Za-z_]+)")
_REDIRECT_RE = re.compile(r"^\s*redirect\s+[\"'(\[]?([^\"'()\[\]\s]+)", re.IGNORECASE)
_SETBUSXY_RE = re.compile(r"^\s*setbusxy\s+(\S+)\s+(\S+)\s+(\S+)", re.IGNORECASE)


def _write_opendss(system, out: Path, spec: Format, collector) -> Conversion:
    from opendss_designer_convert import __version__
    from opendss_designer_convert.writers.opendss.write import Writer

    try:
        Writer(system).write(out, separate_substations=False, separate_feeders=False)
    except Exception:
        logger.exception("OpenDSS writer failed")
        raise ConversionError(
            f"The {spec.label} model was read but could not be written as OpenDSS. It may "
            "use something this converter does not support."
        ) from None

    written = {p.name: p for p in out.iterdir() if p.is_file() and p.suffix.lower() == ".dss"}
    if _MASTER not in written:
        raise ConversionError(
            f"The {spec.label} model has no voltage source, so there is no circuit to "
            "write. Check that the network export includes its source (substation)."
        )
    if any(p.is_dir() for p in out.iterdir()):
        logger.warning("Writer created sub-folders; only top-level files are returned")

    coords = []
    if _BUSCOORDS_DSS in written:
        for line in written.pop(_BUSCOORDS_DSS).read_text(encoding="utf-8").splitlines():
            m = _SETBUSXY_RE.match(line)
            if m:
                coords.append(f"{m.group(1)},{m.group(2)},{m.group(3)}")

    texts = {name: path.read_text(encoding="utf-8") for name, path in written.items()}
    available = set(texts)
    cleaned = {name: _clean_dss(name, text, available) for name, text in texts.items()}

    header = (
        f"! Converted from {spec.label} by opendss-designer-convert {__version__} "
        "(a fork of NREL DiTTo)\n"
    )
    master_lines = cleaned[_MASTER].splitlines()
    if coords:
        master_lines.append(f"Buscoords {_BUSCOORDS_CSV}")
    cleaned[_MASTER] = header + "\n".join(master_lines) + "\n"

    order = [_MASTER] + _redirect_order(cleaned[_MASTER], available - {_MASTER})
    order += sorted(available - set(order))
    files = [(name, cleaned[name]) for name in order]
    if coords:
        files.append((_BUSCOORDS_CSV, "\n".join(coords) + "\n"))
    return Conversion(files=files, main=_MASTER, warnings=collector.messages())


def _clean_dss(name: str, text: str, available: set[str]) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines and lines[-1]:
                lines.append("")
            continue
        verb_match = _VERB_RE.match(stripped)
        verb = verb_match.group(1).lower() if verb_match else ""
        if verb in _DROP_VERBS:
            continue
        if verb == "set" and "voltagebases" not in stripped.lower():
            continue
        if verb == "calcv" or verb == "calcvoltagebases":
            lines.append("CalcVoltageBases")
            continue
        if verb == "redirect":
            m = _REDIRECT_RE.match(stripped)
            target = Path(m.group(1).replace("\\", "/")).name if m else ""
            if target in available and target != name:
                lines.append(f"Redirect {target}")
            else:
                logger.debug("Dropped redirect to %s in %s", target, name)
            continue
        lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def _redirect_order(master_text: str, names: set[str]) -> list[str]:
    order = []
    for line in master_text.splitlines():
        m = _REDIRECT_RE.match(line)
        if m and m.group(1) in names and m.group(1) not in order:
            order.append(m.group(1))
    return order


# --------------------------------------------------------------------------- warnings


_attach_lock = threading.Lock()
_attached = 0
_saved_level: int | None = None


def _attach(collector: logging.Handler) -> None:
    """Add a collector to the package logger, letting WARNING records through.

    If the host configured logging so that this package's warnings are
    dropped (e.g. root at ERROR), lower the package logger to WARNING while
    any conversion runs, so the user still gets them in Conversion.warnings.
    """
    global _attached, _saved_level
    package_logger = logging.getLogger(_PACKAGE_LOGGER)
    with _attach_lock:
        if _attached == 0:
            _saved_level = package_logger.level
            if package_logger.getEffectiveLevel() > logging.WARNING:
                package_logger.setLevel(logging.WARNING)
        _attached += 1
        package_logger.addHandler(collector)


def _detach(collector: logging.Handler) -> None:
    global _attached
    package_logger = logging.getLogger(_PACKAGE_LOGGER)
    with _attach_lock:
        package_logger.removeHandler(collector)
        _attached -= 1
        if _attached == 0 and _saved_level is not None:
            package_logger.setLevel(_saved_level)


class _WarningCollector(logging.Handler):
    """Collect this thread's package warnings as user-safe messages.

    Messages logged from the same line of code (one per skipped load, say)
    are folded into the first one with a count, so a large feeder gives a
    short list rather than thousands of lines.
    """

    def __init__(self, tmpdir: str):
        super().__init__(level=logging.WARNING)
        self._thread = threading.get_ident()
        self._tmp = {tmpdir, tmpdir.replace("\\", "/")}
        self._groups: dict[object, list] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread or record.name == __name__:
            return
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not break the conversion
            return
        self._add((record.name, record.lineno), message)

    def note(self, message: str) -> None:
        self._add(("note", message), message)

    def _add(self, key, message: str) -> None:
        text = (str(message).strip().splitlines() or [""])[0]
        for tmp in self._tmp:
            text = text.replace(tmp, "")
        text = text[:300].strip()
        if not text:
            return
        group = self._groups.get(key)
        if group is None:
            self._groups[key] = [text, 1, {text}]
        elif text not in group[2]:
            group[1] += 1
            group[2].add(text)

    def messages(self) -> list[str]:
        out = []
        for text, count, _ in self._groups.values():
            more = f" (and {count - 1} more like this)" if count > 1 else ""
            out.append(text + more)
        if len(out) > MAX_WARNINGS:
            dropped = len(out) - MAX_WARNINGS
            out = out[:MAX_WARNINGS] + [f"... and {dropped} more kinds of warning."]
        return out
