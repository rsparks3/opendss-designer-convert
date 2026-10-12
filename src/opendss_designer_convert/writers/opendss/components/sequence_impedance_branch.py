from gdm.distribution import DistributionSystem
from infrasys import Component

from opendss_designer_convert.enumerations import OpenDSSFileTypes
from opendss_designer_convert.writers.opendss.components.distribution_branch import (
    DistributionBranchMapper,
)


class SequenceImpedanceBranchMapper(DistributionBranchMapper):
    def __init__(self, model: Component, system: DistributionSystem):
        super().__init__(model, system)

    altdss_name = "Line_LineCode"
    altdss_composition_name = "Line"
    opendss_file = OpenDSSFileTypes.LINES_FILE.value

    def map_name(self):
        self.opendss_dict["Name"] = self.get_opendss_safe_name(self.model.name)

    def map_equipment(self):
        self.opendss_dict["LineCode"] = self.get_opendss_safe_name(self.model.equipment.name)

    def map_in_service(self):
        self.opendss_dict["enabled"] = self.model.in_service
