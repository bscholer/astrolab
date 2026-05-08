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

    preview_approximate: ClassVar[bool] = False

    inputs: ClassVar[dict[str, PortType]]
    outputs: ClassVar[dict[str, PortType]]
    params_schema: ClassVar[type[BaseModel]]

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
