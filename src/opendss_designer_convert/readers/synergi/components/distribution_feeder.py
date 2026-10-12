from gdm.distribution.components.distribution_feeder import DistributionFeeder

from opendss_designer_convert.readers.synergi.synergi_mapper import SynergiMapper
from opendss_designer_convert.readers.synergi.utils import sanitize_name


class DistributionFeederMapper(SynergiMapper):

    synergi_table = "InstFeeders"
    synergi_database = "Model"

    def parse(self, row, unit_type, section_id_sections, from_node_sections, to_node_sections):
        fid = str(row["FeederId"]).strip()
        return DistributionFeeder(name=sanitize_name(fid))
