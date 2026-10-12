import logging
from collections import defaultdict, deque

from gdm.distribution.components import DistributionVoltageSource
from gdm.distribution.components.distribution_bus import DistributionBus
from gdm.distribution.components.distribution_regulator import DistributionRegulator
from gdm.distribution.distribution_system import DistributionSystem
from gdm.distribution.enums import ConnectionType, VoltageTypes
from gdm.quantities import Voltage
from infrasys import Component
from pydantic import ValidationError

import opendss_designer_convert.readers.cyme as cyme_mapper
from opendss_designer_convert.constants import LL_LN_CONVERSION_FACTOR
from opendss_designer_convert.readers.cyme.constants import ModelUnitSystem
from opendss_designer_convert.readers.cyme.utils import network_truncation, read_cyme_data
from opendss_designer_convert.readers.parallel import ParallelBranchSerializer
from opendss_designer_convert.readers.reader import AbstractReader

logger = logging.getLogger(__name__)


def _load_model_sort_key(model_id: str):
    """Numeric IDs in numeric order first, then anything else alphabetically."""
    try:
        return (0, float(model_id), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(model_id))


class Reader(ParallelBranchSerializer, AbstractReader):
    # Order of components is important
    component_types = [
        "DistributionBus",  # First as other components connect to buses
        "DistributionVoltageSource",
        "MatrixImpedanceRecloserEquipment",
        "MatrixImpedanceRecloser",
        "MatrixImpedanceSwitchEquipment",
        "MatrixImpedanceSwitch",
        "MatrixImpedanceFuseEquipment",
        "MatrixImpedanceFuse",
        "DistributionCapacitor",
        "DistributionLoad",
        "BareConductorEquipment",
        "MatrixImpedanceBranchEquipment",
        "GeometryBranchEquipment",
        "GeometryBranchByPhaseEquipment",
        "GeometryBranch",
        "DistributionRegulator",
        "DistributionTransformerByPhase",
        "DistributionTransformer",
        "DistributionTransformerThreeWinding",
        "MatrixImpedanceBranch",  # This must be last as it includes a catch-all for unrecognized branches
    ]

    def __init__(
        self,
        network_file,
        equipment_file,
        load_file,
        load_model_id=None,
        substation_names=None,
        feeder_names=None,
        raise_on_validation_error=False,
    ):
        self.validation_errors = []
        self.raise_on_validation_error = raise_on_validation_error
        self.system = DistributionSystem(auto_add_composed_components=True)
        self.read(
            network_file,
            equipment_file,
            load_file,
            load_model_id,
            substation_names=substation_names,
            feeder_names=feeder_names,
        )

    def _get_unit_system(self, network_file):
        # Default to SI if unit system not specified in file
        with open(network_file, encoding="utf-8", errors="replace") as f:
            for line in f:
                if f"[{ModelUnitSystem.SI.value}]" in line:
                    return ModelUnitSystem.SI
        return ModelUnitSystem.IMPERIAL

    def _build_section_maps(self, network_file):
        node_feeder_map = {}
        node_substation_map = {}
        section_data = read_cyme_data(
            network_file,
            "SECTION",
            node_feeder_map=node_feeder_map,
            node_substation_map=node_substation_map,
            parse_feeders=True,
            parse_substation=True,
        )
        section_id_sections = section_data.set_index("SectionID").to_dict(orient="index")
        from_node_sections = (
            section_data.groupby("FromNodeID")
            .apply(lambda df: df.to_dict(orient="records"))
            .to_dict()
        )
        to_node_sections = (
            section_data.groupby("ToNodeID")
            .apply(lambda df: df.to_dict(orient="records"))
            .to_dict()
        )
        return (
            section_id_sections,
            from_node_sections,
            to_node_sections,
            node_feeder_map,
            node_substation_map,
        )

    def _get_mapper(self, component_type, unit_system):
        mapper_name = component_type + "Mapper"
        if not hasattr(cyme_mapper, mapper_name):
            logger.warning(f"Mapper {mapper_name} not found. Skipping.")
            return None, mapper_name
        mapper = getattr(cyme_mapper, mapper_name)(self.system, units=unit_system)
        return mapper, mapper_name

    def _get_mapper_sections(self, mapper):
        all_cyme_sections = mapper.cyme_section
        if isinstance(all_cyme_sections, str):
            all_cyme_sections = [all_cyme_sections]
        return all_cyme_sections

    def _build_argument_handler(
        self,
        section_id_sections,
        from_node_sections,
        to_node_sections,
        node_feeder_map,
        node_substation_map,
        used_sections,
        load_file,
        load_record,
        equipment_file,
        cyme_section,
    ):
        return {
            "DistributionCapacitorMapper": lambda: [section_id_sections],
            "DistributionBusMapper": lambda: [
                from_node_sections,
                to_node_sections,
                node_feeder_map,
                node_substation_map,
            ],
            "DistributionVoltageSourceMapper": lambda: [],
            "DistributionLoadMapper": lambda: [
                section_id_sections,
                read_cyme_data(load_file, "LOADS", index_col="DeviceNumber"),
                load_record,
            ],
            "GeometryBranchMapper": lambda: [used_sections, section_id_sections, cyme_section],
            "BareConductorEquipmentMapper": lambda: [],
            "GeometryBranchEquipmentMapper": lambda: [
                read_cyme_data(equipment_file, "SPACING TABLE FOR LINE", index_col="ID")
            ],
            "MatrixImpedanceSwitchEquipmentMapper": lambda: [],
            "MatrixImpedanceSwitchMapper": lambda: [used_sections, section_id_sections],
            "MatrixImpedanceFuseEquipmentMapper": lambda: [],
            "MatrixImpedanceFuseMapper": lambda: [used_sections, section_id_sections],
            "MatrixImpedanceRecloserEquipmentMapper": lambda: [],
            "MatrixImpedanceRecloserMapper": lambda: [used_sections, section_id_sections],
            "GeometryBranchByPhaseEquipmentMapper": lambda: [
                read_cyme_data(equipment_file, "SPACING TABLE FOR LINE", index_col="ID")
            ],
            "DistributionTransformerThreeWindingMapper": lambda: [
                used_sections,
                section_id_sections,
                read_cyme_data(
                    equipment_file, "THREE WINDING TRANSFORMER", index_col="ID"
                ).to_dict("index"),
            ],
            "DistributionTransformerMapper": lambda: [
                used_sections,
                section_id_sections,
                read_cyme_data(equipment_file, "TRANSFORMER", index_col="ID").to_dict("index"),
            ],
            "DistributionTransformerByPhaseMapper": lambda: [
                used_sections,
                section_id_sections,
                read_cyme_data(equipment_file, "TRANSFORMER", index_col="ID").to_dict("index"),
            ],
            "DistributionRegulatorMapper": lambda: [
                used_sections,
                section_id_sections,
                read_cyme_data(equipment_file, "REGULATOR", index_col="ID").to_dict("index"),
            ],
            "MatrixImpedanceBranchEquipmentMapper": lambda: [],
            "MatrixImpedanceBranchMapper": lambda: [
                used_sections,
                section_id_sections,
                cyme_section,
            ],
        }

    def _flatten_components(self, components):
        filtered = [c for c in components if c is not None]
        return [item for c in filtered for item in (c if isinstance(c, list) else [c])]

    def _add_components_if_missing(self, components):
        for comp in components:
            if not self.system.has_component(comp):
                self.system.add_component(comp)

    def _parse_components_for_mapper(self, mapper, data, args):
        def parse_row(row):
            return mapper.parse(row, *args)

        return self._flatten_components(data.apply(parse_row, axis=1))

    def _parse_phase_components(self, mapper, data):
        phases = []
        all_components = []
        for phase in ["A", "B", "C"]:
            phases.append(phase)
            all_components.extend(self._parse_components_for_mapper(mapper, data, [phases]))
        return all_components

    def _post_process_network(self, substation_names, feeder_names):
        self.serialize_parallel_branches()

        self.assign_bus_voltages()

        if substation_names is not None or feeder_names is not None:
            self.system = network_truncation(
                self.system, substation_names=substation_names, feeder_names=feeder_names
            )
            print("Finished truncation")

        for component_type in self.system.get_component_types():
            components = self.system.get_components(component_type)
            for c in components:
                if not self.system.has_component(c):
                    self.system.add_component(c)

    def read(
        self,
        network_file,
        equipment_file,
        load_file,
        load_model_id=None,
        substation_names=None,
        feeder_names=None,
    ):
        phase_elements = {
            "MatrixImpedanceBranchEquipmentMapper",
            "MatrixImpedanceRecloserEquipmentMapper",
            "MatrixImpedanceSwitchEquipmentMapper",
            "MatrixImpedanceFuseEquipmentMapper",
        }

        load_record = {}
        used_sections = set()

        (
            section_id_sections,
            from_node_sections,
            to_node_sections,
            node_feeder_map,
            node_substation_map,
        ) = self._build_section_maps(network_file)

        unit_system = self._get_unit_system(network_file)

        for component_type in self.component_types:
            logger.info(f"Parsing Type: {component_type}")
            mapper, mapper_name = self._get_mapper(component_type, unit_system)
            if mapper is None:
                continue

            cyme_file = mapper.cyme_file
            all_cyme_sections = self._get_mapper_sections(mapper)

            for cyme_section in all_cyme_sections:
                data = self._prepare_data(
                    cyme_file, cyme_section, load_model_id, network_file, equipment_file, load_file
                )

                argument_handler = self._build_argument_handler(
                    section_id_sections,
                    from_node_sections,
                    to_node_sections,
                    node_feeder_map,
                    node_substation_map,
                    used_sections,
                    load_file,
                    load_record,
                    equipment_file,
                    cyme_section,
                )
                args = argument_handler.get(mapper_name, lambda: [])()
                if mapper_name in phase_elements:
                    components = self._parse_phase_components(mapper, data)
                else:
                    components = self._parse_components_for_mapper(mapper, data, args)
                self._add_components_if_missing(components)

        self._post_process_network(substation_names, feeder_names)

        self._validate_model()

    def _add_components(self, components: list[Component]):
        """Internal method to add components to the system."""

        if components:
            for component in components:
                try:
                    component.__class__.model_validate(component.model_dump())
                except ValidationError as e:
                    for error in e.errors():
                        err_info = [
                            component.name,
                            component.__class__.__name__,
                            error["loc"][0] if error["loc"] else "On model validation",
                            error["type"],
                            error["msg"],
                        ]

                        self.validation_errors.append(err_info)

    def _validate_model(self):
        if self.validation_errors:
            for name, kind, field, error, message in self.validation_errors:
                logger.warning(
                    "Validation: %s %s field %s: %s (%s)", kind, name, field, message, error
                )
            if self.raise_on_validation_error:
                raise ValueError(
                    f"{len(self.validation_errors)} validation error(s) in the CYME model"
                )
            logger.warning(
                f"Validation warnings detected in CYME reader: {len(self.validation_errors)} issue(s). "
                "Continuing because raise_on_validation_error=False."
            )

    def _prepare_data(
        self, cyme_file, cyme_section, load_model_id, network_file, equipment_file, load_file
    ):
        if cyme_file == "Network":
            data = read_cyme_data(network_file, cyme_section)
        elif cyme_file == "Equipment":
            data = read_cyme_data(equipment_file, cyme_section)
        elif cyme_file == "Load":
            data = read_cyme_data(load_file, cyme_section)
            if data.empty or "LoadModelID" not in data.columns:
                return data
            if load_model_id is not None:
                data = data[data["LoadModelID"] == str(load_model_id)]
                logger.info(f"Filtered Load data by LoadModelID: {load_model_id}")
            else:
                model_ids = sorted(data["LoadModelID"].unique(), key=_load_model_sort_key)
                if len(model_ids) > 1:
                    # A load file can carry several load models (seasons,
                    # scenarios). Take the first rather than refusing.
                    chosen = model_ids[0]
                    logger.warning(
                        "The load file has %d load models (%s); converted load model %s",
                        len(model_ids),
                        ", ".join(model_ids),
                        chosen,
                    )
                    data = data[data["LoadModelID"] == chosen]

        else:
            raise ValueError(f"Unknown CYME file {cyme_file}")

        return data

    def get_system(self) -> DistributionSystem:
        return self.system

    def assign_bus_voltages(self):
        """
        Assign bus voltages by traversing the system from voltage sources outward using BFS.
        Voltage is assigned based on the voltage source and transformer ratings.
        """

        observed_buses = set()
        observed_components = set()

        bus_obj_map = self._create_bus_obj_map()

        bus_queue = self._start_queue_w_voltage_sources()

        while bus_queue:
            current_bus_name = bus_queue.popleft()
            current_voltage, current_voltage_type = self._get_current_bus_voltage(current_bus_name)
            observed_buses.add(current_bus_name)
            conn_objs = bus_obj_map[current_bus_name]
            for obj in conn_objs:
                if self._component_already_observed(obj, observed_components):
                    continue
                self._propagate_component_voltage(
                    obj,
                    current_bus_name,
                    current_voltage,
                    current_voltage_type,
                    observed_buses,
                    bus_queue,
                )

    def _get_current_bus_voltage(self, bus_name):
        current_bus = self.system.get_component(DistributionBus, name=bus_name)
        return current_bus.rated_voltage, current_bus.voltage_type

    def _component_already_observed(self, obj, observed_components):
        component_key = (obj.__class__.__name__, obj.name)
        if component_key in observed_components:
            return True
        observed_components.add(component_key)
        return False

    def _transformer_winding_voltage(self, winding):
        voltage = winding.rated_voltage
        voltage_type = winding.voltage_type
        if (
            winding.connection_type == ConnectionType.STAR
            and winding.num_phases > 1
            and voltage_type == VoltageTypes.LINE_TO_LINE
        ):
            voltage = Voltage(
                voltage.to("kilovolt").magnitude / LL_LN_CONVERSION_FACTOR,
                "kilovolt",
            )
            voltage_type = VoltageTypes.LINE_TO_GROUND
        return voltage, voltage_type

    def _assign_transformer_bus_voltage(self, obj, bus_index, bus, current_voltage):
        for winding_index, winding in enumerate(obj.equipment.windings):
            voltage, voltage_type = self._transformer_winding_voltage(winding)
            if winding_index == bus_index and voltage != current_voltage:
                bus.voltage_type = voltage_type
                bus.rated_voltage = voltage
                logger.info(
                    f"Assigned voltage {voltage} and type {voltage_type} to bus {bus.name} based on transformer {obj.name} winding {winding_index}"
                )
                return

    def _propagate_component_voltage(
        self,
        obj,
        current_bus_name,
        current_voltage,
        current_voltage_type,
        observed_buses,
        bus_queue,
    ):
        is_transformer = obj.__class__.__name__ == "DistributionTransformer"
        for bus_index, bus in enumerate(obj.buses):
            if bus.name == current_bus_name:
                continue
            if is_transformer:
                self._assign_transformer_bus_voltage(obj, bus_index, bus, current_voltage)
            else:
                bus.rated_voltage = current_voltage
                bus.voltage_type = current_voltage_type

            if bus.name not in observed_buses:
                bus_queue.append(bus.name)

    def _start_queue_w_voltage_sources(self):
        bus_queue = deque()

        voltage_sources = list(self.system.get_components(DistributionVoltageSource))

        for vsource in voltage_sources:
            # Source equipment stores per-phase voltage as L-N.
            # Bus rated_voltage is propagated in L-N form and converted in writers when needed.
            vsource.bus.rated_voltage = vsource.equipment.sources[0].voltage
            vsource.bus.voltage_type = VoltageTypes.LINE_TO_GROUND
            bus_queue.append(vsource.bus.name)

        return bus_queue

    def _create_bus_obj_map(self):
        bus_obj_map = defaultdict(list)
        for component_type in self.system.get_component_types():
            component_list = list(self.system.get_components(component_type))
            for comp in component_list:
                if hasattr(comp, "buses"):
                    for bus in comp.buses:
                        bus_obj_map[bus.name].append(comp)

        return bus_obj_map

    def serialize_parallel_branches(self):
        super().serialize_parallel_branches()
        # A CYME regulator senses its own load-side terminal (with line-drop
        # compensation). Serializing may have moved that terminal onto a new
        # bus, so point each controller at where the regulator now ends.
        for regulator in self.system.get_components(DistributionRegulator):
            for controller in regulator.controllers or []:
                controller.controlled_bus = regulator.buses[1]
