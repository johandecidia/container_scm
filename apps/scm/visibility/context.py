"""Map context for pages that belong to other SCM apps.

The shipment and container detail pages are not being replaced — they are being
extended with a map card. These builders keep everything that card needs in one
place, so neither of those views grows its own idea of how a map is configured and
neither has to import a Mapbox detail.
"""

from __future__ import annotations

from django.urls import reverse

from apps.scm.containers.workspace import get_container_workspaces

from .mapbox import get_mapbox_config
from .selectors import get_container_visibility, get_shipment_eta_history, get_shipment_visibility


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
    """
    if workspace is None:
        workspace = get_container_workspaces(team, [container]).get(container.pk)
    visibility = get_container_visibility(team=team, container=container, workspace=workspace)
    return {
        "mapbox": get_mapbox_config(),
        "map_mode": "container",
        "map_data_url": reverse("visibility:container_map_data", args=[container.pk]),
        "visibility": visibility,
        "eta_history": (
            get_shipment_eta_history(team=team, shipment=visibility.shipment) if visibility.shipment else []
        ),
    }
