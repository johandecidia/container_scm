"""Container discovery service.

Coordinates running carrier discovery for all planned containers on a team.
Uses a list of CarrierDiscoveryProvider implementations to search carriers.
"""

from __future__ import annotations

import logging

from django.utils import timezone as django_timezone

from apps.scm.integrations.carriers.base import CarrierDiscoveryProvider
from apps.scm.integrations.carriers.schemas import ContainerDiscoveryResult
from apps.teams.models import Team

from .models import ContainerDiscoveryEvent, ContainerPool
from .selectors import get_planned_containers
from .services import mark_container_detected

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dummy provider (used until real carrier clients are implemented)
# ---------------------------------------------------------------------------


class DummyCarrierDiscoveryProvider(CarrierDiscoveryProvider):
    """Test provider that detects containers whose numbers start with 'MCUU'.

    Used for development and testing. Replace with real carrier implementations.
    """

    provider_code = "dummy"
    provider_name = "Dummy Carrier"

    def discover_container(self, container_number: str) -> ContainerDiscoveryResult | None:
        if container_number.upper().startswith("MCUU"):
            return ContainerDiscoveryResult(
                container_number=container_number.upper(),
                carrier_code=self.provider_code,
                carrier_name=self.provider_name,
                current_status="IN_TRANSIT",
                raw_payload={"source": "dummy", "container_number": container_number},
            )
        return None


# ---------------------------------------------------------------------------
# Default provider registry
# ---------------------------------------------------------------------------

DEFAULT_PROVIDERS: list[CarrierDiscoveryProvider] = [
    DummyCarrierDiscoveryProvider(),
]


# ---------------------------------------------------------------------------
# Discovery runner
# ---------------------------------------------------------------------------


def run_container_discovery(
    team: Team,
    providers: list[CarrierDiscoveryProvider] | None = None,
) -> dict:
    """Run container discovery for all PLANNED containers on the team.

    For each planned container, each provider is queried in order. If a provider
    returns a result, the container is marked DETECTED and a CONTAINER_DETECTED
    event is saved. If all providers fail with an exception, a SEARCH_FAILED event
    is saved.

    Returns a summary dict with counts of detected, failed, and skipped containers.
    """
    if providers is None:
        providers = DEFAULT_PROVIDERS

    planned = list(get_planned_containers(team=team))
    summary = {"planned": len(planned), "detected": 0, "failed": 0}

    for pool_entry in planned:
        _run_discovery_for_entry(team=team, pool_entry=pool_entry, providers=providers, summary=summary)

    logger.info(
        "Discovery run complete for team %s — planned=%d detected=%d failed=%d",
        team.slug,
        summary["planned"],
        summary["detected"],
        summary["failed"],
    )
    return summary


def _run_discovery_for_entry(
    team: Team,
    pool_entry: ContainerPool,
    providers: list[CarrierDiscoveryProvider],
    summary: dict,
) -> None:
    container_number = pool_entry.container_number
    result: ContainerDiscoveryResult | None = None
    error: Exception | None = None

    for provider in providers:
        try:
            result = provider.discover_container(container_number)
            if result is not None:
                break
        except Exception as exc:
            logger.exception(
                "Provider %s failed for container %s: %s",
                provider.provider_code,
                container_number,
                exc,
            )
            error = exc

    if result is not None:
        _handle_detection(team=team, pool_entry=pool_entry, result=result)
        summary["detected"] += 1
    elif error is not None:
        _handle_failure(team=team, pool_entry=pool_entry, error=error)
        summary["failed"] += 1


def _handle_detection(
    team: Team,
    pool_entry: ContainerPool,
    result: ContainerDiscoveryResult,
) -> None:
    now = django_timezone.now()
    ContainerDiscoveryEvent.objects.create(
        team=team,
        container_pool=pool_entry,
        container_number=result.container_number,
        carrier_code=result.carrier_code,
        carrier_name=result.carrier_name,
        event_type=ContainerDiscoveryEvent.EventType.CONTAINER_DETECTED,
        detected_at=now,
        payload=result.raw_payload,
    )
    mark_container_detected(pool_entry)


def _handle_failure(
    team: Team,
    pool_entry: ContainerPool,
    error: Exception,
) -> None:
    ContainerDiscoveryEvent.objects.create(
        team=team,
        container_pool=pool_entry,
        container_number=pool_entry.container_number,
        event_type=ContainerDiscoveryEvent.EventType.SEARCH_FAILED,
        payload={"error": str(error)},
    )
