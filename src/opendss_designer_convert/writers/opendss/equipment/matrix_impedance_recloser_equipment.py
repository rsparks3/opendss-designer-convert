from gdm.distribution import DistributionSystem
from infrasys import Component

from opendss_designer_convert.enumerations import OpenDSSFileTypes
from opendss_designer_convert.writers.opendss.equipment.matrix_impedance_branch_equipment import (
    MatrixImpedanceBranchEquipmentMapper,
)


class MatrixImpedanceRecloserEquipmentMapper(MatrixImpedanceBranchEquipmentMapper):
    def __init__(self, model: Component, system: DistributionSystem):
        super().__init__(model, system)

    altdss_name = "LineCode_ZMatrixCMatrix"
    altdss_composition_name = "LineCode"
    opendss_file = OpenDSSFileTypes.RECLOSER_CODES_FILE.value

    def map_name(self):
        self.opendss_dict["Name"] = self.get_opendss_safe_name(self.model.name)

    def map_controller(self):
        # Not mapped in OpenDSS
        pass
