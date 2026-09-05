"""Map context for pages that belong to other SCM apps.

The shipment and container detail pages are not being replaced — they are being
extended with a map card. These builders keep everything that card needs in one
place, so neither of those views grows its own idea of how a map is configured and
neither has to import a Mapbox detail.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.urls import reverse

from apps.scm.containers.workspace import get_container_workspaces

from .map_positions import build_map_positions
from .mapbox import get_mapbox_config
from .selectors import get_container_visibility, get_shipment_eta_history, get_shipment_visibility


@dataclass(frozen=True)
class ContainerMapPositions:
    """One container's canonical markers, named so a template can ask for each.

    A tiny read model rather than two context keys: the template's question is
    "is there a current position, and is there a destination", and answering it by
    filtering a list in the template would put the physical-over-tracking
    distinction into the markup.
    """

    positions: list = field(default_factory=list)

    @property
    def current(self):
        """The accepted or carrier-derived current position, or None."""
        return next((position for position in self.positions if position.is_current), None)

    @property
    def destination(self):
        return next((position for position in self.positions if not position.is_current), None)

    @property
    def has_plottable_current(self) -> bool:
        """True when the map can actually draw where this container is.

        False covers two different situations — nowhere known, and a known place
        with no coordinates — and the template distinguishes them by asking
        ``current`` as well.
        """
        current = self.current
        return current is not None and current.is_plottable


def get_location_map_context(location) -> dict:
    """Map card context for the existing location workspace.

    No team argument: the location was already fetched team-scoped by the view, and
    the marker is the place itself. The count the marker carries comes from the
    endpoint, which is team-scoped in its own right.
    """
    return {
        "mapbox": get_mapbox_config(),
        "map_mode": "location",
        "map_data_url": reverse("visibility:location_map_data", args=[location.pk]),
    }


def get_shipment_map_context(team, shipment) -> dict:
    """Map card context for the existing shipment detail page."""
    return {
        "mapbox": get_mapbox_config(),
        "map_mode": "shipment",
        "map_data_url": reverse("visibility:shipment_map_data", args=[shipment.pk]),
        "visibility": get_shipment_visibility(team=team, shipment=shipment),
        "eta_history": get_shipment_eta_history(team=team, shipment=shipment),
    }


def get_container_map_context(team, container, workspace=None) -> dict:
    """Map card context for the existing container detail page.

    Built from the container's own tracking, so a container with no shipment still
    gets a map, a status and an ETA rather than three blanks. ``workspace`` lets the
    detail view hand over the workspace it has already built.

    ``map_positions`` are LOC-4's canonical markers for this one box — where MCR has
    accepted it to be, and where it is going. They travel to the template as well as
    to the GeoJSON endpoint, because the page has to be able to say "no current
    plottable position, destination Oceanterminalen" in words when the map cannot
    draw it. A destination shown as though it were a position is the specific lie
    this page must not tell.
    """
    if workspace is None:
        workspace = get_container_workspaces(team, [container]).get(container.pk)
    visibility = get_container_visibility(team=team, container=container, workspace=workspace)
    positions, coverage = build_map_positions(team, [visibility])
    return {
        "mapbox": get_mapbox_config(),
        "map_mode": "container",
        "map_data_url": reverse("visibility:container_map_data", args=[container.pk]),
        "visibility": visibility,
        "map_positions": ContainerMapPositions(positions),
        "map_coverage": coverage,
        "eta_history": (
            get_shipment_eta_history(team=team, shipment=visibility.shipment) if visibility.shipment else []
        ),
    }
