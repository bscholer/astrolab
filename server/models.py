"""Pydantic models for the load-bearing astrolab contracts.

Mirrors the README "Contracts" section. Stable shapes here let nodes, the
runtime, the cache, and (later) the catalog and UI all agree on data shapes.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .ports import PortType


class Ref(BaseModel):
    """Opaque handle to a cache entry produced by a node.

    A Ref points to a single output port's file under cache/<node_hash>/.
    Nodes consume Refs as inputs and produce Refs as outputs; they do not see
    raw paths outside the cache.
    """

    display_ready: bool = False
    """True when the FITS data is already in display range [0,1] post-stretch;
    preview generation should skip autostretch and just clip+scale."""

    model_config = ConfigDict(frozen=True)

    node_hash: str
    """Hash of the producing node's (id, version, inputs, params)."""

    port: str
    """Name of the output port that produced this Ref."""

    path: Path
    """Filesystem path to the artifact."""

    type: PortType
    """Declared type of this artifact."""


class NodeSpec(BaseModel):
    """One node entry inside a Template (the YAML representation, not the runtime Node)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    """Stable id within the template; used in cache key derivation."""

    kind: str
    """Lookup key into the node registry."""

    variant: str | None = None
    """For polymorphic nodes (e.g. stretch.ghs); None for fixed-kind nodes."""

    params: dict[str, Any] = Field(default_factory=dict)
    """Raw param dict; validated against the node's params_schema at resolve time."""

    inputs: dict[str, str] = Field(default_factory=dict)
    """Map of input_port -> '<source_node_id>.<source_port>'. Explicit edges only in Phase 0."""

    ui_depends_on: str | None = None
    """UI-only visibility gate: id of another node in the same template
    whose `enabled` param must be true for this node to render in the
    pipeline view. Walks transitively, so chained gates work
    (recombine -> replace -> extract). Has no runtime effect; disabled
    upstream nodes already passthrough at execution time, so the hide
    just spares the user UI noise about steps that won't change the
    output."""


class Template(BaseModel):
    """A pipeline as a file. Hand-editable, version-controlled, fork = copy."""

    model_config = ConfigDict(extra="forbid")

    id: str
    version: int = 1
    description: str = ""
    profile: str | None = None
    """Scope profile id; supplies defaults and ingest rules. Optional in Phase 0."""

    nodes: list[NodeSpec]
    outputs: dict[str, str] = Field(default_factory=dict)
    """Map of public_output_name -> '<node_id>.<port>'."""


class CalibrationSpec(BaseModel):
    """How a Job should resolve calibration frames."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["auto", "explicit", "none"] = "auto"
    master_ids: dict[str, int] = Field(default_factory=dict)
    """When mode='explicit': kind ('dark'|'flat'|'bias') -> master row id."""


class Job(BaseModel):
    """A concrete request to run a template against specific inputs."""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    template_version: int
    profile_id: str | None = None
    target_id: str | None = None
    session_ids: list[str] = Field(default_factory=list)
    calibration: CalibrationSpec = Field(default_factory=CalibrationSpec)
    param_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """node_id -> partial params merged over the template's defaults."""

    inputs: dict[str, Ref | list[Ref]] = Field(default_factory=dict)
    """External inputs into the pipeline, keyed by '<node_id>.<port>'.

    For nodes whose inputs are sourced from outside the DAG (e.g. the first
    node reading frames from disk), the runner reads from this map instead of
    from another node's output.

    List values are used for list-valued ports (see ports.LIST_PORTS), eg the
    calibrate node's `dark` port accepts a list of masters so one job can use
    different darks for different temperature / exposure bins.
    """

    preview_only: bool = False


class Profile(BaseModel):
    """Scope-specific knowledge: pipeline defaults + ingest rules.

    Phase 0 only uses the pipeline_defaults section. Ingest rules will be
    consumed by the catalog in Phase 1.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    description: str = ""
    pipeline_defaults: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """node_id (or kind) -> default param overrides applied before Job overrides."""

    ingest: dict[str, Any] = Field(default_factory=dict)
    """Opaque blob in Phase 0; structure firmed up in Phase 1."""


class RunContext(BaseModel):
    """Per-node-execution context provided by the runner.

    Phase 0 has the bare minimum: a scoped temp dir, a progress callback, a
    logger. The siril() callable lands in Phase 0+ once we ship the first Siril
    node; the field is intentionally absent here so the foundation runs on
    macOS without any Siril dependency.

    `cancel` is a cooperative cancellation token. The runner sets it when a
    job is superseded (eg the user tweaks a slider mid-run); long-running
    nodes should pass it to subprocess wrappers so they can terminate
    cleanly instead of finishing wasted work. Default is a fresh, never-set
    Event so unaware nodes Just Work.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    tmpdir: Path
    progress: Annotated[Callable[[float, str], None], Field(repr=False)]
    log: Annotated[logging.Logger, Field(repr=False)]
    cancel: Annotated[
        threading.Event,
        Field(repr=False, default_factory=threading.Event),
    ]
