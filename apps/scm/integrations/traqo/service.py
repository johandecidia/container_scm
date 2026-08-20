"""Ingesting a Traqo container response through the existing tracking pipeline.

Nothing here writes to the database itself. Every write is an existing tracking
service, in the same order and with the same semantics a discovered carrier source
gets in :func:`apps.scm.tracking.manual_refresh.store_discovered_carrier_source`:

    get_or_create_tracking_provider   the provider row, shared with every other source
    get_or_create_container_subscription   the watch, on the same natural key
    create_sync_run                   the attempt, so it appears in sync history
    store_verified_carrier_result     raw payload first, then normalised events
    apply_sync_outcome                close the run and move the subscription

Which means Traqo events land in TrackingEvent through the same fingerprinting and
upsert as Maersk's, and the timeline, position and ETA derivations read them without
knowing where they came from.

Fetch happens before any of it. A Traqo outage, a rejected key or an account problem
therefore leaves the container exactly as it was — no subscription, no state change,
and above all no effect on tracking events any other source already produced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

from . import PROVIDER_CODE, PROVIDER_NAME
from .client import PRODUCTION_BASE_URL, TraqoClient
from .eta import read_traqo_eta_observation
from .mapper import map_traqo_container_payload

if TYPE_CHECKING:
    from apps.scm.tracking.models import TrackingSubscription, TrackingSyncRun

logger = logging.getLogger(__name__)


@dataclass
class TraqoIngestResult:
    """What one Traqo ingest achieved."""

    container_number: str
    sealine: str
    sandbox: bool
    events_mapped: int = 0
    events_created: int = 0
    events_updated: int = 0
    events_failed: int = 0
    raw_payloads_created: int = 0
    subscription: TrackingSubscription | None = None
    sync_run: TrackingSyncRun | None = None
    payload: dict = field(default_factory=dict)
    eta_observation_recorded: bool = False

    @property
    def events_seen(self) -> int:
        return self.events_created + self.events_updated


def get_traqo_provider():
    """Return the Traqo TrackingProvider, creating it once if needed.

    Uses the same ``get_or_create`` helper every carrier source goes through, so there
    is one provider bootstrap in the codebase rather than a Traqo-shaped copy of it.
    The documented base URL is filled in on first creation only; a value someone has
    since edited is left alone.
    """
    provider = get_or_create_tracking_provider(carrier_code=PROVIDER_CODE, carrier_name=PROVIDER_NAME)
    if provider is not None and not provider.base_url:
        provider.base_url = PRODUCTION_BASE_URL
        provider.save(update_fields=["base_url", "updated_at"])
    return provider


def fetch_traqo_container(*, container_number: str, sealine: str, sandbox: bool = True, client=None) -> dict:
    """Fetch one container's Traqo response envelope.

    Separate from persistence so a caller can look at what Traqo said without writing
    anything — which is what the POC command's dry run does.
    """
    client = client or TraqoClient.from_settings(sandbox=sandbox)
    return client.get_container(container_number, sealine)


def ingest_traqo_container(
    *,
    team,
    container,
    sealine: str,
    sandbox: bool = True,
    client=None,
) -> TraqoIngestResult:
    """Fetch a container from Traqo and persist the result through tracking ingestion.

    The subscription is Traqo's own: an existing carrier subscription for the same
    container is neither replaced nor touched, so a container can be watched through
    Maersk Direct and through Traqo at once and the two can be compared.

    Raises the client's typed carrier errors — nothing is written when the fetch fails.
    """
    from apps.scm.tracking.eta_observations import record_provider_eta_observation
    from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
    from apps.scm.tracking.services import create_sync_run
    from apps.scm.tracking.sync import apply_sync_outcome, store_verified_carrier_result

    container_number = container.container_id
    payload = fetch_traqo_container(
        container_number=container_number,
        sealine=sealine,
        sandbox=sandbox,
        client=client,
    )
    events = map_traqo_container_payload(payload, container_number=container_number)

    # Ensures the provider row carries Traqo's base URL before the subscription helper
    # get_or_creates the same row by code.
    get_traqo_provider()
    subscription = get_or_create_container_subscription(
        team=team,
        container=container,
        carrier_code=PROVIDER_CODE,
        carrier_name=PROVIDER_NAME,
    )
    if subscription is None:  # pragma: no cover — only if the provider code is blank
        raise RuntimeError("Could not resolve a Traqo tracking subscription.")

    sync_run = create_sync_run(team=team, subscription=subscription, provider=subscription.provider)
    outcome = store_verified_carrier_result(subscription, raw_payload=payload, events=events)
    apply_sync_outcome(subscription, sync_run, outcome)

    # After the events, because whether this forecast is worth recording depends on what
    # they say: a box the events have already brought home has no arrival left to
    # forecast, however Traqo still describes it.
    observation = read_traqo_eta_observation(payload, observed_at=timezone.now())
    eta_row = (
        record_provider_eta_observation(
            team=team,
            observation=observation,
            shipment=subscription.shipment,
            container=container,
        )
        if observation is not None
        else None
    )

    logger.info(
        "Traqo ingest for %s (%s, %s): %d mapped, %d created, %d updated, ETA observation %s.",
        container_number,
        sealine,
        "sandbox" if sandbox else "live",
        len(events),
        outcome.events_created,
        outcome.events_updated,
        "recorded" if eta_row else "not recorded",
    )

    return TraqoIngestResult(
        container_number=container_number,
        sealine=sealine,
        sandbox=sandbox,
        events_mapped=len(events),
        events_created=outcome.events_created,
        events_updated=outcome.events_updated,
        events_failed=outcome.events_failed,
        raw_payloads_created=outcome.raw_payloads_created,
        subscription=subscription,
        sync_run=sync_run,
        payload=payload,
        eta_observation_recorded=eta_row is not None,
    )
