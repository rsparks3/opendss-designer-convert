from abc import ABC

from opendss_designer_convert.readers.cyme.constants import ModelUnitSystem


class CymeMapper(ABC):
    def __init__(self, system, units=ModelUnitSystem):
        self.system = system
        self.units = units
