"""Mapbox browser configuration, in one place.

The token reaches the browser, so only the public token is ever read here. When it
is missing the map card renders a configuration notice instead of a map and the
page carries on working — see :attr:`MapboxConfig.is_configured`.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

# Used when MAPBOX_STYLE_URL is unset or blank, so a missing style never leaves the
# map with nothing to draw. A custom style can be swapped in later without code.
DEFAULT_STYLE_URL = "mapbox://styles/mapbox/standard"


@dataclass(frozen=True)
class MapboxConfig:
    """What the browser needs to draw a map, and whether it can."""

    token: str
    style_url: str

    @property
    def is_configured(self) -> bool:
        return bool(self.token)


def get_mapbox_config() -> MapboxConfig:
    """Return the browser-side Mapbox configuration.

    A blank token is a valid, handled state — not an error. Every template that
    embeds a map checks ``is_configured`` and falls back to an empty state.
    """
    return MapboxConfig(
        token=(getattr(settings, "MAPBOX_PUBLIC_TOKEN", "") or "").strip(),
        style_url=(getattr(settings, "MAPBOX_STYLE_URL", "") or "").strip() or DEFAULT_STYLE_URL,
    )
