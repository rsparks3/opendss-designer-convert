"""Convert CYME and Synergi Electric distribution models to OpenDSS.

A trimmed fork of NREL's DiTTo (BSD-3-Clause), packaged as a plugin for
OpenDSS Designer. The public API is the plugin contract:

    from opendss_designer_convert import FORMATS, convert, ConversionError

    result = convert("cyme", [("network.txt", b"..."), ("equipment.txt", b"...")])
    result.main      # "Master.dss"
    result.files     # [(file name, OpenDSS text), ...]
    result.warnings  # messages safe to show a user
"""

__version__ = "0.1.0"

from opendss_designer_convert.api import (
    FORMATS,
    Conversion,
    ConversionError,
    Format,
    convert,
)

__all__ = ["FORMATS", "Conversion", "ConversionError", "Format", "convert", "__version__"]
