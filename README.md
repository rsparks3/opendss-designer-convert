# opendss-designer-convert

Converts **CYME** and **Synergi Electric** distribution models to **OpenDSS**.
It is the optional import plugin for OpenDSS Designer, and also works on its own from Python or the command line.

It is a trimmed fork of NREL's **DiTTo** (Distribution Transformation Tool):
DiTTo's CYME and Synergi readers and its OpenDSS writer, renamed to the
`opendss_designer_convert` package, with the rest of DiTTo removed and a
number of conversion bugs fixed (listed below).

**Milsoft WindMil is not supported.** The DiTTo this fork is based on has no
WindMil reader. The older, archived DiTTo has two (`windmil`, XML, and
`windmil_ascii`), but no WindMil sample data exists in either repository, so
nothing could be checked against real WindMil files and neither reader was
carried over. `convert("windmil", ...)` raises `ConversionError` saying so.

## Provenance and licence

- Upstream: <https://github.com/NREL-Distribution-Suites/ditto> (now served
  from `NLR-Distribution-Suite/ditto`), branch `main` at `f596f5b`
  (2026-10-07). This is the 2024-2026 rewrite of DiTTo on
  [grid-data-models](https://github.com/NLR-Distribution-Suite/grid-data-models).
  Its git history is kept in this repository, so authorship of every upstream
  line is preserved.
- The Synergi reader comes from upstream branch `te/synergi_reader`
  (`a25bba9`, 2026-05-08, open as upstream PR #68), merged so its history is
  kept too.
- The older DiTTo (<https://github.com/NREL/ditto>, now
  `NatLabRockies/ditto`, last change December 2023) is a different code base.
  Nothing from it is used except the Synergi sample databases for testing
  (see "Test data").
- Licence: BSD-3-Clause, as upstream. See [LICENSE](LICENSE): the original
  Alliance for Energy Innovation copyright notice is kept and a line added for
  the changes made here.

## Install

```
pip install opendss-designer-convert      # once published
pip install -e .                          # from a checkout, for now
pip install -e ".[dev]"                   # plus pytest, ruff and OpenDSSDirect.py for the tests
```

Python 3.11 or newer (grid-data-models requires 3.11). Pure Python: no
mdbtools, no compiled extension of its own, no network access at run time.
Developed and tested on Windows 11 with Python 3.12.

Runtime dependencies: `grid-data-models~=2.4.0`, `infrasys>=1.2`,
`NREL-altdss-schema>=0.0.3,<0.1`, `pydantic>=2.6`, `pandas>=2.0`,
`numpy>=1.26`, `access-parser>=0.0.6` (Apache-2.0, reads Access databases).
grid-data-models is heavy (it brings geopandas, pyarrow, h5py and plotly) and
infrasys pins `rich<14`; install into its own environment if that matters.

## Command line

```
opendss-designer-convert cyme Network.txt Equipment.txt Load.txt -o out/
opendss-designer-convert synergi Model.mdb Warehouse.mdb -o out/
```

Writes the OpenDSS files into `out/`, prints warnings to stderr and exits 0;
on failure prints `error: ...` and exits 1. Compile `out/Master.dss`.

## Python API: the plugin contract

```python
from opendss_designer_convert import FORMATS, Conversion, ConversionError, Format, convert

result = convert("cyme", [("Network.txt", net_bytes), ("Equipment.txt", eq_bytes),
                          ("Load.txt", load_bytes)])
result.main       # "Master.dss" - the file with `New Circuit.`
result.files      # [("Master.dss", "..."), ("Lines.dss", "..."), ..., ("BusCoords.csv", "...")]
result.warnings   # ["Load LOAD652_B has 0 kW and 0 kVAR. Skipping... (and 9 more like this)"]
```

```python
@dataclass(frozen=True)
class Format:
    key: str                     # "cyme" | "synergi"
    label: str                   # "CYME", "Synergi Electric"
    extensions: tuple[str, ...]  # (".txt",) | (".mdb",)
    help: str                    # which files to select
    max_files: int               # 3 | 2

FORMATS: dict[str, Format]       # only formats that work: "cyme", "synergi"

@dataclass
class Conversion:
    files: list[tuple[str, str]]  # (file name, OpenDSS text): every file the model needs
    main: str                     # the file that defines the circuit
    warnings: list[str]           # safe to show a user

class ConversionError(Exception):
    """str(exc) is safe to show a user (no paths, no tracebacks)."""

def convert(fmt: str, files: list[tuple[str, bytes]]) -> Conversion: ...
__version__: str
```

What `convert` guarantees, because it runs on a public web server:

- It works only in a directory from `tempfile.mkdtemp()`, removed in a
  `finally` whether the conversion succeeds or not. Uploaded files are written
  there under their base name only (directory parts are stripped); a name that
  is not plain letters, digits, `.`, `_` or `-` (at most 128 characters, not
  starting with a dot) is refused.
- No subprocesses, no network access, no reads or writes outside that
  directory. The Synergi reader is pure Python; there is no mdbtools fallback.
- At most `max_files` files and `MAX_TOTAL_BYTES` (256 MB) in total.
- Every failure is a `ConversionError` with a message meant for the user.
  Unexpected exceptions are logged with `logging` (logger
  `opendss_designer_convert`) and replaced by a generic message.
- Output is plain OpenDSS that OpenDSS Designer's importer takes: `Master.dss`
  plus the files it redirects to, all by bare file name; no `clear`, `solve`,
  `show`, `export`, `plot`, `set datapath` or similar. Bus coordinates, when
  the model has them, are a `BusCoords.csv` companion (`bus,x,y` per line)
  loaded by `Buscoords BusCoords.csv` at the end of `Master.dss`.
- Warnings logged by the readers and writer are returned in
  `Conversion.warnings`; messages from the same line of code are folded into
  one entry with a count, and the list is capped at 50 kinds.

Side effects to know about: while a conversion runs, the
`opendss_designer_convert` logger is lowered to WARNING if the host has it set
higher (so warnings can be collected), and loguru output from
grid-data-models and infrasys is disabled process-wide (it otherwise prints,
for example, every bus with an unconnected neutral to stderr).

### Which files to select

- **CYME**: the ASCII exports (`.txt`): the network file (with `[SECTION]` and
  `[NODE]`), the equipment file (`[CONDUCTOR]`, `[CABLE]`, `[TRANSFORMER]`...)
  and, if loads were exported separately, the load file (`[CUSTOMER LOADS]`).
  Files are recognised by content, so names and order do not matter, and one
  self-contained export holding all three also works. Without a load file the
  model converts with no loads, and a warning says so. ANSI (Windows-1252),
  UTF-8 and UTF-16 files are accepted. A load file with several load models
  converts the lowest-numbered one, with a warning.
- **Synergi**: the model database and the equipment warehouse database
  (`.mdb`). They are told apart by their tables (`InstSection` and `Node` for
  the model, `DevConductors` and `DevTransformers` for the warehouse); one
  database holding both also works.

## What is converted

| | CYME | Synergi |
|---|---|---|
| Source | `[SOURCE]` + `[SOURCE EQUIVALENT]` -> `New Circuit` (more sources -> extra `Vsource`) | `InstFeeders` -> `New Circuit` at the feeder's node |
| Lines | overhead by phase/geometry -> `LineGeometry` + `WireData`; unbalanced/cable -> `LineCode` (R, X, C matrices) | `InstSection` -> `LineGeometry` or `LineCode` (falls back to matrix impedance) |
| Switches, breakers, sectionalizers, fuses, reclosers | `Line ... Switch=True` with switch codes; open devices `open`ed | switches, breakers, fuses, reclosers likewise |
| Transformers | two-winding, three-winding, by-phase and center-tapped -> `XfmrCode` + `Transformer` | primary transformers (`InstPrimaryTransformers`) |
| Regulators | -> `Transformer` + `RegControl`; single-phase banks as one unit per phase | single-phase units with `RegControl` |
| Capacitors | fixed shunt capacitors | (none in the sample) |
| Loads | spot and distributed customer loads with ZIP model -> `Load` | `Loads` table -> `Load` |
| DER | not read | solar -> `PVSystem` (no sample to check it); generators and batteries are read but not written (a warning says how many) |
| Coordinates | node X/Y -> `BusCoords.csv` | `Node` X/Y -> `BusCoords.csv` |

Not converted (as upstream): capacitor controls (upstream issue #93; a warning
names any that are dropped), load shapes and time series, protection settings
and TCC curves beyond the device as a switch, Synergi distribution
transformers (`InstDTrans`; Synergi loads sit on the primary sections).

## What has been verified

Every conversion test converts, compiles the result with OpenDSSDirect.py
0.9.4 (`clear`, `compile`, `solve`) and checks that it compiles and converges.

| Format | Sample (from DiTTo's own repositories) | Version | Result |
|---|---|---|---|
| CYME | `tests/data/cyme_models/4_node` (IEEE 4-node) | CYME 8.00 | compiles, converges |
| CYME | `tests/data/cyme_models/13_node` (IEEE 13-node) | CYME 5.04 | compiles, converges; within 1 % of EPRI's IEEE 13 model (regulators off) |
| CYME | `tests/data/cyme_models/ieee_123_node` | CYME 8.00 | compiles, converges; within 1 % of EPRI's IEEE 123 model; the ganged regulator settles on the reference tap |
| Synergi | `m4.mdb` + `Warehouse.mdb` (a real 12.47 kV feeder, 827 nodes) | Synergi Electric 6.4.0 | compiles, converges with controls on; 179 loads, 3 regulators |

The "within 1 %" check is upstream's: minimum, maximum and mean node voltage
and feeder-head P and Q, with the source at 1.0 pu, regulators disabled and
taps at 1.

Not verified: other CYME versions (notably CYME 9, legacy issue #397),
imperial-unit CYME exports, CYME XML (`.sxst`) exports, other Synergi
versions or warehouses, Synergi models with capacitors, DER or reclosers,
and the electrical accuracy of the Synergi result (there is no Synergi
power-flow result to compare against). Linux is exercised only by the CI
workflow, not locally.

## Test data

- The CYME samples and the EPRI IEEE 13 and 123 reference models are in
  `tests/data/` (from upstream).
- The Synergi sample pair is **not committed**. It is the only Synergi data in
  any DiTTo repository: `tests/data/big_cases/synergi/m4.mdb` and
  `Warehouse.mdb` in commit `975e167` of the legacy repository (branch
  `fuse_wenbo`). It is real utility data (a Hawaiian Electric feeder with
  geographic coordinates), so it is left out of this repository; decide
  before publishing it. To fetch it:

  ```
  git clone --filter=blob:none --no-checkout https://github.com/NatLabRockies/ditto legacy-ditto
  git -C legacy-ditto show 975e167:tests/data/big_cases/synergi/m4.mdb > tests/data/synergi/m4.mdb
  git -C legacy-ditto show 975e167:tests/data/big_cases/synergi/Warehouse.mdb > tests/data/synergi/Warehouse.mdb
  ```

  `tests/data/synergi/*.mdb` is git-ignored. The Synergi tests skip when the
  files are absent; `ODC_SYNERGI_SAMPLES=<folder>` points them elsewhere.

## Changes from upstream DiTTo

Trimmed: the CIM reader and writer, the OpenDSS reader, the MCP server, the
typer CLI, the docs site, their tests and dependencies (rdflib, typer, mcp,
rich, loguru as a direct dependency, opendssdirect at run time). The package
is `opendss_designer_convert`, so it cannot clash with an installed `ditto`.

Fixed (each has a test):

- **Regulator banks (upstream #77).** A CYME bank of single-phase regulators
  was written as one three-phase transformer controlled by phase A alone. It
  is now one unit and one RegControl per phase; gang-operated three-phase units
  stay one transformer. The units of a bank are kept side by side rather than
  chained in series (which opened phases B and C).
- **Line-drop compensation.** Every RegControl was written with `Bus=` set to
  the regulator's own terminal, which makes OpenDSS ignore R and X. It is now
  omitted (with `PTPhase` for ganged units). With this, the IEEE 123 ganged
  regulator matches EPRI's reference tap.
- **Regulator starting tap.** The CYME tap was put on both windings, giving a
  1:1 ratio; the source-side winding now stays at 1.0.
- **Several sources (legacy #402, #422, #427).** A second source became a
  second `New Circuit`, discarding everything before it; it is now an extra
  `Vsource`, and the source meter no longer fails on several sources.
- **Several load models.** A CYME load file with more than one load model
  raised an error; the first is now converted, with a warning. A load file
  without `[CUSTOMER LOADS]` no longer raises `KeyError`.
- **Encodings.** CYME files were opened in the platform's default encoding;
  ANSI, UTF-8 and UTF-16 exports now all work on every platform.
- **sqrt(3).** Line-to-neutral conversion used 1.732, so 4.16 kV became
  4.1599 kV and voltage bases were duplicated.
- **Synergi without mdbtools (legacy #188, #425).** Upstream downloaded an
  mdbtools binary at run time and ran it in a subprocess for each table; the
  databases are now read in pure Python with access_parser.
- **Synergi feeder head.** Chosen with ties broken by set order, so the source
  moved between runs; it is now the node named after the feeder.
- **Synergi devices sharing a section** (a switch and a transformer) were
  written in parallel, so the closed switch shorted the transformer; they are
  now in series.
- **Synergi orientation.** Section from/to order is not the direction of flow;
  transformers are turned so the high-voltage winding faces the source, and
  regulators so winding 1 does.
- **Synergi voltages.** Bus voltages set while reading transformers made every
  line on those buses fail validation and vanish; voltages are now propagated
  from the source after reading.
- **Synergi missing transformer type.** A primary transformer whose type is
  missing from the warehouse is built from its name ("12.47/11.5KV 3000KVA")
  with a generic 0.5 % R / 6 % X and a warning, instead of being dropped.
- The OpenDSS writer accepts a `str` output path (upstream #12).

Known limitations:

- No WindMil (see the top of this file).
- Capacitor controls are not converted (upstream #93).
- The Synergi sample's warehouse lacks its 12.47/11.5 kV autotransformer, so
  that unit is generic; with it the feeder's far end solves at about 0.84 pu
  with the regulators at or near full boost. Whether that matches Synergi is unknown.
- CYME regulators are placed at the source end of their section; CYME's
  `Location` field is not read.
- Lines carry the neutral as an explicit fourth conductor (upstream's choice).

## Development

```
python -m pytest
ruff check .
```

The tests need `opendssdirect.py` (in the `test` extra). On Windows,
pytest's faulthandler is turned off in `pyproject.toml` because the OpenDSS
engine raises and catches its own exceptions, which faulthandler reports as
fatal.
