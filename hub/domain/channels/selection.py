"""Channel profile selection without provider/protocol conditionals."""

from __future__ import annotations

from collections.abc import Iterable

from hub.domain.channels.entities import ChannelKind, ChannelProfile


class ChannelProfileUnavailable(LookupError):
    pass


class ChannelProfileCatalog:
    def __init__(self, profiles: Iterable[ChannelProfile]) -> None:
        self._profiles = {profile.name: profile for profile in profiles}
        if not self._profiles:
            raise ValueError("at least one channel profile is required")

    def get(self, name: str) -> ChannelProfile:
        try:
            return self._profiles[name]
        except KeyError as exc:
            raise ChannelProfileUnavailable(name) from exc

    def select(self, required_kinds: frozenset[ChannelKind]) -> ChannelProfile:
        candidates = [
            profile
            for profile in self._profiles.values()
            if required_kinds.issubset(profile.required_kinds)
        ]
        if not candidates:
            raise ChannelProfileUnavailable(",".join(sorted(kind.value for kind in required_kinds)))
        return min(candidates, key=lambda profile: (len(profile.required_kinds), profile.name))
