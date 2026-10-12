from gdm.distribution import DistributionSystem
from infrasys import Component

from opendss_designer_convert.enumerations import OpenDSSFileTypes
from opendss_designer_convert.writers.opendss.components.distribution_branch import (
    DistributionBranchMapper,
)


class MatrixImpedanceRecloserMapper(DistributionBranchMapper):
    def __init__(self, model: Component, system: DistributionSystem):
        super().__init__(model, system)

    altdss_name = "Line_LineCode"
    altdss_composition_name = "Line"
    opendss_file = OpenDSSFileTypes.RECLOSER_FILE.value

    def map_name(self):
        self.opendss_dict["Name"] = self.get_opendss_safe_name(self.model.name)

    def map_equipment(self):
        self.opendss_dict["LineCode"] = self.get_opendss_safe_name(self.model.equipment.name)

    def map_is_closed(self):
        # Require every phase to be enabled for the OpenDSS line to be enabled.
        self.opendss_dict["Switch"] = True

    def map_in_service(self):
        self.opendss_dict["Enabled"] = self.model.in_service

    def map_controller(self):
        pass
