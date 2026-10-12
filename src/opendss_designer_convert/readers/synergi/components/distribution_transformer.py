import logging
import re

from gdm.distribution.common import SequencePair
from gdm.distribution.components.distribution_bus import DistributionBus
from gdm.distribution.components.distribution_transformer import DistributionTransformer
from gdm.distribution.enums import ConnectionType, VoltageTypes
from gdm.distribution.equipment.distribution_transformer_equipment import (
    DistributionTransformerEquipment,
    WindingEquipment,
)
from gdm.quantities import ApparentPower, Voltage

from opendss_designer_convert.readers.synergi.synergi_mapper import SynergiMapper
from opendss_designer_convert.readers.synergi.utils import (
    parse_phases,
    phases_without_neutral,
    sanitize_name,
)

logger = logging.getLogger(__name__)

# Impedance given to a primary transformer built from its type name.
_GENERIC_PCT_R = 0.5
_GENERIC_PCT_X = 6.0
_TYPE_NAME_RE = re.compile(
    r"([\d.]+)\s*/\s*([\d.]+)\s*KV\b.*?([\d.]+)\s*KVA", re.IGNORECASE
)


class DistributionTransformerMapper(SynergiMapper):

    synergi_table = "InstPrimaryTransformers"
    synergi_database = "Model"

    def parse(self, row, unit_type, section_id_sections, from_node_sections, to_node_sections):
        equipment = self.map_equipment(row)
        if equipment is None:
            return None
        name = self.map_name(row)
        buses = self.map_bus(row, section_id_sections)
        if buses[0] is None or buses[1] is None:
            return None
        winding_phases = self.map_winding_phases(row)
        section_id = str(row["SectionId"]).strip()
        section = section_id_sections.get(section_id, {})
        from_node = str(section.get("FromNodeId", "")).strip()
        feeder, substation = self._lookup_feeder_substation(from_node)

        # Bus voltages are set after reading, by walking out from the source
        # (utils.propagate_bus_voltages). Setting them here, before the lines
        # are read, made every line touching these buses fail validation
        # (unequal end voltages) and drop out of the model.

        return DistributionTransformer(
            name=name,
            buses=buses,
            winding_phases=winding_phases,
            equipment=equipment,
            substation=substation,
            feeder=feeder,
        )

    def map_name(self, row):
        device_id = str(row.get("UniqueDeviceId", row["SectionId"])).strip()
        return sanitize_name(device_id)

    def map_winding_phases(self, row):
        phases = phases_without_neutral(parse_phases(row["ConnectedPhases"]))
        return [phases, phases]

    def map_bus(self, row, section_id_sections):
        section_id = str(row["SectionId"]).strip()
        section = section_id_sections.get(section_id, {})
        from_bus_name = sanitize_name(str(section.get("FromNodeId", "")).strip())
        to_bus_name = sanitize_name(str(section.get("ToNodeId", "")).strip())
        from_bus = None
        to_bus = None
        try:
            from_bus = self.system.get_component(component_type=DistributionBus, name=from_bus_name)
        except Exception:
            logger.warning(f"Transformer {section_id}: from bus {from_bus_name} not found")
        try:
            to_bus = self.system.get_component(component_type=DistributionBus, name=to_bus_name)
        except Exception:
            logger.warning(f"Transformer {section_id}: to bus {to_bus_name} not found")
        return [from_bus, to_bus]

    def map_equipment(self, row):
        equipment_name = str(row["TransformerType"]).strip()
        try:
            return self.system.get_component(component_type=DistributionTransformerEquipment, name=equipment_name)
        except Exception:
            pass
        equipment = self._equipment_from_type_name(row, equipment_name)
        if equipment is None:
            logger.warning(
                f"Transformer equipment {equipment_name!r} not found, skipping transformer"
            )
            return None
        logger.warning(
            f"Transformer type {equipment_name!r} is not in the warehouse; built a generic "
            f"{equipment.windings[0].rated_power.to('kilova').magnitude:g} kVA, "
            f"{equipment.windings[0].rated_voltage.to('kilovolt').magnitude:g}/"
            f"{equipment.windings[1].rated_voltage.to('kilovolt').magnitude:g} kV unit "
            f"from its name ({_GENERIC_PCT_R:g}% R, {_GENERIC_PCT_X:g}% X)"
        )
        self.system.add_component(equipment)
        return equipment

    def _equipment_from_type_name(self, row, equipment_name):
        """Read 'HV/LV KV ... KVA' from a type name such as '12.47/11.5KV 3000KVA 3PH'.

        Without this a primary transformer missing from the warehouse was
        dropped, and everything behind it ran at the wrong voltage.
        """
        m = _TYPE_NAME_RE.search(equipment_name)
        if not m:
            return None
        hv, lv, kva = (float(x) for x in m.groups())
        if hv <= 0 or lv <= 0 or kva <= 0:
            return None
        phases = phases_without_neutral(parse_phases(str(row.get("ConnectedPhases", "ABC"))))
        num_phases = 3 if len(phases) >= 3 else max(len(phases), 1)
        windings = []
        for i, (kv, code) in enumerate(
            ((hv, row.get("HighSideConnectionCode")), (lv, row.get("LowSideConnectionCode"))),
            start=1,
        ):
            code = str(code or "YG").upper()
            windings.append(
                WindingEquipment(
                    name=f"{equipment_name}_Winding_{i}",
                    resistance=_GENERIC_PCT_R / 2,
                    is_grounded="G" in code,
                    rated_voltage=Voltage(kv, "kilovolt"),
                    voltage_type=VoltageTypes.LINE_TO_LINE,
                    rated_power=ApparentPower(kva, "kilova"),
                    num_phases=num_phases,
                    connection_type=ConnectionType.DELTA if "D" in code else ConnectionType.STAR,
                    tap_positions=[1.0] * num_phases,
                    total_taps=32,
                    min_tap_pu=0.9,
                    max_tap_pu=1.1,
                )
            )
        return DistributionTransformerEquipment(
            name=equipment_name,
            pct_no_load_loss=0,
            pct_full_load_loss=_GENERIC_PCT_R,
            windings=windings,
            coupling_sequences=[SequencePair(1, 2)],
            winding_reactances=[_GENERIC_PCT_X],
            is_center_tapped=False,
        )
