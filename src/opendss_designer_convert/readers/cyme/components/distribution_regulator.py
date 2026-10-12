import logging

from gdm.distribution.common.sequence_pair import SequencePair
from gdm.distribution.components.distribution_bus import DistributionBus
from gdm.distribution.components.distribution_regulator import DistributionRegulator
from gdm.distribution.controllers.distribution_regulator_controller import RegulatorController
from gdm.distribution.enums import ConnectionType, Phase, VoltageTypes
from gdm.distribution.equipment.distribution_transformer_equipment import (
    DistributionTransformerEquipment,
    WindingEquipment,
)
from gdm.quantities import ApparentPower, Current, Voltage
from infrasys.quantities import Time

from opendss_designer_convert.constants import LL_LN_CONVERSION_FACTOR
from opendss_designer_convert.readers.cyme.constants import ModelUnitSystem
from opendss_designer_convert.readers.cyme.cyme_mapper import CymeMapper

logger = logging.getLogger(__name__)


class DistributionRegulatorMapper(CymeMapper):
    def __init__(self, system, units=ModelUnitSystem):
        super().__init__(system, units=units)

    cyme_file = "Network"
    cyme_section = "REGULATOR SETTING"

    def parse(self, row, used_sections, section_id_sections, equipment_data):
        base_name = self.map_name(row)
        section_id = str(row["SectionID"])
        section = section_id_sections.get(section_id)
        if section is None:
            logger.warning(f"Section {section_id} not found for regulator {base_name}. Skipping")
            return None

        equipment_row = equipment_data.get(row["EqID"], None)
        phases = self.map_phases(row, section)
        if not phases:
            logger.warning(f"No phases found for regulator {base_name}. Skipping")
            return None

        buses = self.map_buses(section)

        if len(phases) > 1 and not self.is_three_phase_unit(equipment_row):
            # A bank of single-phase regulators (CYME equipment Type 0, e.g. the
            # IEEE 13 and 123 feeders): one transformer and one RegControl per
            # phase, each following its own phase. Writing the bank as one
            # three-phase transformer let only the phase-A control act on all
            # three phases (upstream issue #77).
            units = [
                (f"{base_name}_{self.map_phase_suffix(phase)}", [phase]) for phase in phases
            ]
        else:
            # A gang-operated three-phase regulator (Type 1), or a single unit.
            units = [(base_name, phases)]

        regulators = []
        for unit_name, unit_phases in units:
            single_unit = len(units) > 1
            equipment = self.map_equipment(
                row, equipment_row, unit_phases, buses, unit_name, single_unit=single_unit
            )
            controllers = self.map_controllers(
                row, equipment_row, unit_phases, buses[1], unit_name
            )
            if single_unit:
                for controller in controllers:
                    controller.name = unit_name
            try:
                regulators.append(
                    DistributionRegulator.model_construct(
                        name=unit_name,
                        buses=list(buses),
                        winding_phases=[unit_phases, unit_phases],
                        equipment=equipment,
                        controllers=controllers,
                    )
                )
            except Exception as e:
                logger.warning(f"Failed to create DistributionRegulator {unit_name}: {e}")
        if not regulators:
            return None
        used_sections.add(section_id)
        return regulators

    @staticmethod
    def is_three_phase_unit(equipment_row):
        """CYME [REGULATOR] Type: 0 = single-phase unit(s), 1 = three-phase unit."""
        try:
            return int(float((equipment_row or {}).get("Type", 0) or 0)) == 1
        except (TypeError, ValueError):
            return False

    def map_name(self, row):
        return row["SectionID"]

    def map_buses(self, section):
        from_bus = self.system.get_component(
            component_type=DistributionBus,
            name=section["FromNodeID"],
        )
        to_bus = self.system.get_component(
            component_type=DistributionBus,
            name=section["ToNodeID"],
        )
        return [from_bus, to_bus]

    def map_phases(self, row, section):
        phase_str = str(row.get("PhaseON") or section.get("Phase") or "")
        phases = []
        if "A" in phase_str:
            phases.append(Phase.A)
        if "B" in phase_str:
            phases.append(Phase.B)
        if "C" in phase_str:
            phases.append(Phase.C)
        return phases

    def map_equipment(
        self, row, equipment_row, phases, buses, regulator_name, single_unit=False
    ):
        total_taps = int(float((equipment_row or {}).get("Taps", 32) or 32))
        max_buck = float(row.get("MaxBuck", (equipment_row or {}).get("MaxBuck", 10)) or 10)
        max_boost = float(row.get("MaxBoost", (equipment_row or {}).get("MaxBoost", 10)) or 10)
        kva = float((equipment_row or {}).get("KVA", 100) or 100)
        if single_unit:
            # KVA is the bank rating; KVA_1 (when given) is one unit's.
            unit_kva = float((equipment_row or {}).get("KVA_1", 0) or 0)
            kva = unit_kva if unit_kva > 0 else kva

        connection = self.map_connection_type(row)
        kv_ln = float((equipment_row or {}).get("KVLN", 0) or 0)
        if kv_ln > 0:
            winding_voltage = Voltage(kv_ln, "kilovolt")
            primary_voltage_type = VoltageTypes.LINE_TO_GROUND
            secondary_voltage_type = VoltageTypes.LINE_TO_GROUND
        else:
            winding_voltage = buses[1].rated_voltage
            primary_voltage_type = (
                buses[0].voltage_type if buses[0].voltage_type else VoltageTypes.LINE_TO_LINE
            )
            secondary_voltage_type = (
                buses[1].voltage_type if buses[1].voltage_type else VoltageTypes.LINE_TO_LINE
            )
            if (
                len(phases) == 1
                and connection == ConnectionType.STAR
                and secondary_voltage_type == VoltageTypes.LINE_TO_LINE
            ):
                # A single wye unit sees line-to-neutral voltage.
                winding_voltage = Voltage(
                    winding_voltage.to("kilovolt").magnitude / LL_LN_CONVERSION_FACTOR,
                    "kilovolt",
                )
                primary_voltage_type = VoltageTypes.LINE_TO_GROUND
                secondary_voltage_type = VoltageTypes.LINE_TO_GROUND

        # The regulator boosts or bucks on its load-side winding; the source-side
        # winding stays at 1.0. Upstream put the same tap on both windings,
        # which made the starting ratio 1:1 whatever the CYME tap position was.
        primary_taps = [1.0 for _ in phases]
        secondary_taps = [
            self.map_tap_position(row, phase, total_taps, max_buck, max_boost) for phase in phases
        ]

        min_tap_pu = 1 - max_buck / 100
        max_tap_pu = 1 + max_boost / 100

        windings = [
            WindingEquipment.model_construct(
                name=f"{row['SectionID']}_primary",
                resistance=0.001,
                is_grounded=connection == ConnectionType.STAR,
                rated_voltage=winding_voltage,
                voltage_type=primary_voltage_type,
                rated_power=ApparentPower(kva, "kilova"),
                num_phases=len(phases),
                connection_type=connection,
                tap_positions=primary_taps,
                total_taps=total_taps,
                min_tap_pu=min_tap_pu,
                max_tap_pu=max_tap_pu,
            ),
            WindingEquipment.model_construct(
                name=f"{row['SectionID']}_secondary",
                resistance=0.001,
                is_grounded=connection == ConnectionType.STAR,
                rated_voltage=winding_voltage,
                voltage_type=secondary_voltage_type,
                rated_power=ApparentPower(kva, "kilova"),
                num_phases=len(phases),
                connection_type=connection,
                tap_positions=secondary_taps,
                total_taps=total_taps,
                min_tap_pu=min_tap_pu,
                max_tap_pu=max_tap_pu,
            ),
        ]

        return DistributionTransformerEquipment.model_construct(
            name=f"{row.get('EqID', 'REGULATOR')}_{regulator_name}",
            pct_no_load_loss=0.0,
            pct_full_load_loss=0.001,
            windings=windings,
            coupling_sequences=[SequencePair(0, 1)],
            winding_reactances=[0.001],
            is_center_tapped=False,
        )

    def map_connection_type(self, row):
        connection_map = {
            0: ConnectionType.STAR,
            1: ConnectionType.STAR,
            2: ConnectionType.DELTA,
            3: ConnectionType.OPEN_DELTA,
            4: ConnectionType.DELTA,
        }
        connection_number = int(float(row.get("Conn", 0) or 0))
        return connection_map.get(connection_number, ConnectionType.STAR)

    def map_tap_position(self, row, phase, total_taps, max_buck, max_boost):
        suffix = self.map_phase_suffix(phase)
        raw_tap = float(row.get(f"Tap{suffix}", 0) or 0)

        # CYME regulator taps are positions around neutral (0). Convert to pu.
        half_taps = max(total_taps / 2, 1)
        step_up = (max_boost / 100) / half_taps
        step_down = (max_buck / 100) / half_taps
        if raw_tap >= 0:
            return 1 + raw_tap * step_up
        return 1 + raw_tap * step_down

    def map_controllers(self, row, equipment_row, phases, controlled_bus, regulator_name):
        max_step = int(max(float(row.get("MaxBuck", 0) or 0), float(row.get("MaxBoost", 0) or 0)))
        pt_ratio = float(row.get("PT", (equipment_row or {}).get("PT", 1)) or 1)
        ct_primary = float(row.get("CT", (equipment_row or {}).get("CT", 0)) or 0)
        is_reversible = bool(
            int(float(row.get("Reversible", (equipment_row or {}).get("Reversible", 0)) or 0))
        )

        controllers = []
        for phase in phases:
            suffix = self.map_phase_suffix(phase)
            v_setpoint = float(row.get(f"Vset{suffix}", row.get("VsetA", 120)) or 120)
            bandwidth = float(row.get(f"BandWidth{suffix}", row.get("BandWidthA", 2)) or 2)
            ldc_r = float(row.get(f"Rset{suffix}", 0) or 0)
            ldc_x = float(row.get(f"Xset{suffix}", 0) or 0)
            use_ldc = abs(ldc_r) > 0 or abs(ldc_x) > 0

            controllers.append(
                RegulatorController.model_construct(
                    name=f"{regulator_name}_{suffix}",
                    delay=Time(0, "seconds"),
                    v_setpoint=Voltage(v_setpoint, "volt"),
                    min_v_limit=Voltage(v_setpoint - bandwidth / 2, "volt"),
                    max_v_limit=Voltage(v_setpoint + bandwidth / 2, "volt"),
                    pt_ratio=pt_ratio,
                    use_ldc=use_ldc,
                    is_reversible=is_reversible,
                    ldc_R=Voltage(ldc_r, "volt"),
                    ldc_X=Voltage(ldc_x, "volt"),
                    ct_primary=Current(max(ct_primary, 0.0), "ampere"),
                    max_step=max_step,
                    bandwidth=Voltage(bandwidth, "volt"),
                    controlled_bus=controlled_bus,
                    controlled_phase=phase,
                )
            )

        return controllers

    def map_phase_suffix(self, phase):
        if phase == Phase.A:
            return "A"
        if phase == Phase.B:
            return "B"
        if phase == Phase.C:
            return "C"
        return "A"
