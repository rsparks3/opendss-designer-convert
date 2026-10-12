import re

import pandas as pd
from access_parser import AccessParser
from gdm.distribution.enums import Phase

_POS_MAP = {0: Phase.A, 1: Phase.B, 2: Phase.C, 3: Phase.N}
_PHASE_ORDER = {Phase.A: 0, Phase.B: 1, Phase.C: 2, Phase.N: 3}


def parse_phases(phase_str: str) -> list[Phase]:
    """Parse a Synergi positional phase string into GDM Phase values.

    Position 0 → A, 1 → B, 2 → C, 3 → N. A space means absent.
    Examples:
      "ABCN" → [A, B, C, N]
      " B N" → [B, N]
      "  C " → [C]
    """
    phases = []
    for i, char in enumerate(phase_str):
        if i in _POS_MAP and char.strip():
            phases.append(_POS_MAP[i])
    return phases


def phases_without_neutral(phases: list[Phase]) -> list[Phase]:
    return [p for p in phases if p != Phase.N]


def sort_phases(phases: list[Phase] | set[Phase]) -> list[Phase]:
    return sorted(phases, key=lambda p: _PHASE_ORDER.get(p, 99))




def sanitize_name(name: str) -> str:
    name = str(name).strip()
    name = name.replace(" - ", "_")
    name = re.sub(r"[^\w.]", "_", name)
    name = re.sub(r"_+", "_", name)
    return name.strip("_")


def safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(str(value).strip())
    except (ValueError, TypeError):
        return default


def build_node_feeder_map(feeder_data, section_data) -> dict:
    """Build a mapping from node_id to feeder info for voltage and context lookups.

    Returns dict[node_id, {"feeder_id": str, "nominal_kvll": float, "sub_id": str}]
    """
    feeder_info = {}
    for _, row in feeder_data.iterrows():
        fid = str(row["FeederId"]).strip()
        nominal_kvll = safe_float(row.get("NominalKvll"), 12.47) or 12.47
        sub_id = str(row.get("SubstationId", "Unknown") or "Unknown").strip()
        feeder_info[fid] = {"nominal_kvll": nominal_kvll, "sub_id": sub_id}

    node_feeder_map: dict = {}
    for _, row in section_data.iterrows():
        fid = str(row.get("FeederId", "")).strip()
        from_node = str(row["FromNodeId"]).strip()
        to_node = str(row["ToNodeId"]).strip()
        if fid in feeder_info:
            info = {"feeder_id": fid, **feeder_info[fid]}
            node_feeder_map.setdefault(from_node, info)
            node_feeder_map.setdefault(to_node, info)

    return node_feeder_map


class SynergiDatabase:
    """A Synergi model or warehouse database (Microsoft Access .mdb).

    Read with ``access_parser``, a pure-Python Jet/ACE reader, so no mdbtools
    binary, subprocess or download is needed. Tables are parsed on first use
    and cached for the life of this object.
    """

    def __init__(self, path):
        try:
            self._db = AccessParser(str(path))
        except Exception as exc:  # access_parser raises bare errors on garbage
            raise ValueError("not a readable Microsoft Access database") from exc
        self._names = {name.lower(): name for name in self._db.catalog}
        self._tables: dict[str, pd.DataFrame] = {}

    def has_table(self, table_name: str) -> bool:
        return table_name.lower() in self._names

    def table(self, table_name: str) -> pd.DataFrame:
        key = table_name.lower()
        if key not in self._tables:
            if key not in self._names:
                raise KeyError(f"table {table_name} not found")
            data = self._db.parse_table(self._names[key]) or {}
            self._tables[key] = pd.DataFrame({col: pd.Series(vals) for col, vals in data.items()})
        return self._tables[key]


def read_synergi_data(database, table_name) -> pd.DataFrame:
    """Return one table of a Synergi database as a DataFrame."""
    if not isinstance(database, SynergiDatabase):
        database = SynergiDatabase(database)
    return database.table(table_name)
