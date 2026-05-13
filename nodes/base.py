"""Node base class.

A Node is a typed, cacheable unit of work. Subclasses fill in `id`, `version`,
`cost`, `inputs`, `outputs`, `params_schema`, and `run()`. The runtime is
responsible for hashing, caching, and supplying the RunContext; nodes should
focus on the actual work.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel

from server.models import CostClass, Ref, RunContext
from server.ports import PortType


class Node[ParamsT: BaseModel](ABC):
    """Base class for all node implementations.

    Class attributes describe the node's contract; instance methods do the
    work. We use class attrs (rather than init-time fields) so the registry
    can introspect a node before constructing it.
    """

    id: ClassVar[str]
    """Stable identifier used in the cache key. Bump implicitly via version."""

    version: ClassVar[int] = 1
    """Bump when the node's behavior or output format changes."""

    cost: ClassVar[CostClass] = "cheap"

    uses_siril: ClassVar[bool] = False
    """True when this node shells out to Siril. The runtime mixes the Siril
    version string into the cache key so a Siril upgrade invalidates stale
    cached outputs automatically."""

    preview_approximate: ClassVar[bool] = False

    preview_display_ready: ClassVar[bool] = False
    # True when this node's FITS output is already display-ready (post-stretch);
    # skip autostretch in preview.

    preview_hidden: ClassVar[bool] = False
    # True for pre-stack sequence ops where the per-frame preview is technically
    # renderable but visually uninformative: a single calibrated frame, a
    # resampled subframe, a debayered raw, etc. The UI uses this to render the
    # node card without a thumbnail well so the eye isn't drawn to noise.

    inputs: ClassVar[dict[str, PortType]]
    outputs: ClassVar[dict[str, PortType]]
    params_schema: ClassVar[type[BaseModel]]

    optional_inputs: ClassVar[frozenset[str]] = frozenset()
    """Names of input ports that are optional. Unwired optional inputs are
    omitted from the dict passed to run() and from the cache hash."""

    @abstractmethod
    def run(
        self,
        inputs: dict[str, Ref],
        params: ParamsT,
        ctx: RunContext,
        out_dir: object,
    ) -> dict[str, Ref]:
        """Execute the node and return Refs for each declared output port.

        `out_dir` is a Path; declared as `object` here to avoid a Path import
        (kept narrow). Implementations should write each output file under
        out_dir and return Refs whose `path` points inside it. The runtime
        will commit those Refs into the cache once `run` returns successfully.
        """
        raise NotImplementedError
