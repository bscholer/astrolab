"""Node registry.

Maps a `kind` (and optional `variant`) to a Node implementation class. Node
modules register themselves at import time via the `@register` decorator.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nodes.base import Node


_REGISTRY: dict[tuple[str, str | None], type[Node]] = {}


def register(
    kind: str, variant: str | None = None
) -> Callable[[type[Node]], type[Node]]:
    """Decorator: register a node implementation under (kind, variant)."""

    def decorator(cls: type[Node]) -> type[Node]:
        key = (kind, variant)
        if key in _REGISTRY:
            existing = _REGISTRY[key]
            if existing is not cls:
                raise RuntimeError(
                    f"node registry collision for {key}: "
                    f"{existing.__module__}.{existing.__qualname__} vs "
                    f"{cls.__module__}.{cls.__qualname__}"
                )
        _REGISTRY[key] = cls
        return cls

    return decorator


def lookup(kind: str, variant: str | None = None) -> type[Node]:
    try:
        return _REGISTRY[(kind, variant)]
    except KeyError as exc:
        known = sorted(f"{k}{f'/{v}' if v else ''}" for k, v in _REGISTRY)
        raise KeyError(
            f"no node registered for kind={kind!r} variant={variant!r}; "
            f"known: {known}"
        ) from exc


def all_kinds() -> list[tuple[str, str | None]]:
    return sorted(_REGISTRY.keys(), key=lambda k: (k[0], k[1] or ""))
