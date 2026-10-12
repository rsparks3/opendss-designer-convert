import logging
import math
import re

import pandas as pd
from access_parser import AccessParser
from gdm.distribution.enums import Phase

logger = logging.getLogger(__name__)

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


def propagate_bus_voltages(system) -> int:
    """Carry nominal voltages from the source through transformers.

    The bus mapper gives every node its feeder's nominal voltage, which is
    wrong past a step or auto transformer (an 11.5 kV section behind a
    12.47/11.5 kV unit, say): loads there would be rated for the wrong kV.
    Walk outwards from the voltage source(s); a transformer sets the voltage
    of the buses on its far side from its winding ratings. Returns the number
    of buses whose voltage changed.
    """
    from collections import deque

    from gdm.distribution.components import (
        DistributionBus,
        DistributionRegulator,
        DistributionTransformer,
        DistributionVoltageSource,
    )
    from gdm.distribution.enums import VoltageTypes
    from gdm.quantities import Voltage

    graph = system.get_undirected_graph()
    buses = {bus.name: bus for bus in system.get_components(DistributionBus)}
    queue = deque()
    seen = set()
    for source in system.get_components(DistributionVoltageSource):
        if source.bus.name in buses and source.bus.name not in seen:
            seen.add(source.bus.name)
            queue.append(source.bus.name)

    def ln_kv(winding):
        kv = winding.rated_voltage.to("kilovolt").magnitude
        if winding.voltage_type == VoltageTypes.LINE_TO_LINE:
            kv = kv / math.sqrt(3)
        return kv

    changed = 0
    while queue:
        name = queue.popleft()
        here = buses[name]
        for _, other, data in graph.edges(name, data=True):
            if other in seen or other not in buses:
                continue
            kv = None
            if data.get("type") is DistributionRegulator:
                _regulator_face_source(
                    system.get_component(DistributionRegulator, data["name"]), name
                )
            if data.get("type") is DistributionTransformer:
                xfmr = system.get_component(DistributionTransformer, data["name"])
                _face_source(xfmr, name, ln_kv)
                names = [b.name for b in xfmr.buses]
                if other in names and len(xfmr.equipment.windings) > names.index(other):
                    kv = ln_kv(xfmr.equipment.windings[names.index(other)])
            if kv is None:
                kv = here.rated_voltage.to("kilovolt").magnitude
                if here.voltage_type == VoltageTypes.LINE_TO_LINE:
                    kv = kv / math.sqrt(3)
            bus = buses[other]
            new = Voltage(round(kv, 5), "kilovolt")
            old = bus.rated_voltage.to("kilovolt").magnitude
            if bus.voltage_type == VoltageTypes.LINE_TO_LINE:
                old = old / math.sqrt(3)
            if abs(old - kv) > 1e-3:
                changed += 1
            bus.rated_voltage = new
            bus.voltage_type = VoltageTypes.LINE_TO_GROUND
            seen.add(other)
            queue.append(other)
    return changed


def _face_source(xfmr, source_side_bus, ln_kv) -> None:
    """Turn a two-winding transformer so its high-voltage winding faces the source.

    Synergi section from/to nodes do not give the direction of flow, but the
    winding data is high side first. Fed from its low-voltage end, a step-down
    unit would step the feeder voltage *up*.
    """
    windings = xfmr.equipment.windings
    names = [b.name for b in xfmr.buses]
    if len(windings) != 2 or len(names) != 2 or source_side_bus not in names:
        return
    here = names.index(source_side_bus)
    if ln_kv(windings[here]) >= ln_kv(windings[1 - here]):
        return
    xfmr.buses = list(reversed(xfmr.buses))
    xfmr.winding_phases = list(reversed(xfmr.winding_phases))
    logger.warning(
        "Transformer %s is fed from its low-voltage side in the section data; turned it "
        "so the high-voltage winding faces the source",
        xfmr.name,
    )


def _regulator_face_source(regulator, source_side_bus) -> None:
    """Put a regulator's source-side winding (winding 1) on the bus nearer the source.

    As with transformers, the section's from/to order is not the direction of
    flow. A regulator written backwards regulates the wrong side: raising its
    tap lowers the voltage it is meant to raise.
    """
    names = [b.name for b in regulator.buses]
    if len(names) != 2 or names[0] == source_side_bus or source_side_bus not in names:
        return
    regulator.buses = list(reversed(regulator.buses))
    regulator.winding_phases = list(reversed(regulator.winding_phases))
    for controller in regulator.controllers or []:
        if getattr(controller, "controlled_bus", None) is not None:
            if controller.controlled_bus.name == names[1]:
                controller.controlled_bus = regulator.buses[1]
    logger.info("Regulator %s turned to face the source", regulator.name)
