"""Which Provider kinds this Hub assembles, by configuration.

Composition registers a factory per kind; configuration lists the kinds to
enable. Unknown kinds are a configuration error at startup, not a silent
no-op, because a Provider that is not there makes every device it owned
offline without anything else looking unhealthy.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

Factory = Callable[[], Any]


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Factory] = {}

    def register(self, kind: str, factory: Factory) -> None:
        if kind in self._factories:
            raise ValueError(f"provider kind {kind!r} registered twice")
        self._factories[kind] = factory

    @property
    def known(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))

    def build(self, enabled: Iterable[str]) -> Mapping[str, Any]:
        wanted = list(enabled)
        unknown = sorted(set(wanted) - set(self._factories))
        if unknown:
            raise ValueError(
                f"unknown smart-home provider kind(s): {', '.join(unknown)}; known: {', '.join(self.known)}"
            )
        return {kind: self._factories[kind]() for kind in dict.fromkeys(wanted)}
