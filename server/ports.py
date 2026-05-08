"""Port types for node inputs and outputs.

Phase 0 keeps this as a flat string enum. We will refine into proper generic
types once we have more than the trivial node and need to distinguish between,
say, a single FITS frame and a sequence of them.
"""

from enum import StrEnum


class PortType(StrEnum):
    IMAGE_PNG = "image/png"
    IMAGE_FITS = "image/fits"
    SEQUENCE_FITS = "sequence/fits"
    MASTER_FITS = "master/fits"
    CHANNEL_TRIPLE = "channel_triple"
