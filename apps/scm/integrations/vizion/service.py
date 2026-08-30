"""The two Vizion operations, kept apart on purpose.

    resolve_carrier_via_aci()   which carrier is moving this box?
    ingest_vizion_container()   store what Vizion knows about its journey

These are the POC's central architectural claim in code form. *Carrier resolution* and
*tracking provider selection* are different questions, and a future state may legitimately
be "carrier ONE, resolved by Vizion, tracked by Traqo". So neither function calls the
other, neither returns the other's answer, and nothing here writes a routing decision or
consults one. A caller that wants both does both, and says so.

Nothing in this module writes to the database itself. Every write is an existing tracking
service, in the same order and with the same semantics Traqo's ingest uses:

    get_or_create_tracking_provider        the provider row, shared with every source
    get_or_create_container_subscription   the watch, on the same natural key
    create_sync_run                        the attempt, so it appears in sync history
    store_verified_carrier_result          raw payload first, then normalised events
    apply_sync_outcome                     close the run and move the subscription

Which means Vizion events land in TrackingEvent through the same fingerprinting and
upsert as Maersk's, and the timeline, position and ETA derivations read them without
knowing where they came from.

Fetch happens before any of it. A Vizion outage, a rejected key or an account problem
therefore leaves the container exactly as it was — no subscription, no state change, and
above all no effect on tracking events any other source already produced.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils import timezone

from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider

from . import PROVIDER_CODE, PROVIDER_NAME
from .client import PRODUCTION_BASE_URL, VizionClient
from .eta import read_vizion_eta_observation
from .mapper import map_vizion_updates, read_latest_payload
from .schemas import ACI_PENDING, VizionReference, read_reference

if TYPE_CHECKING:
    from apps.scm.tracking.models import TrackingSubscription, TrackingSyncRun

logger = logging.getLogger(__name__)

# How long a POC run is willing to wait for ACI to settle. Vizion identifies a carrier
# asynchronously — the create returns immediately and the answer arrives on its own
# schedule — so a single read straight after the create will usually still say PENDING.
DEFAULT_ACI_POLL_ATTEMPTS = 6
DEFAULT_ACI_POLL_INTERVAL_SECONDS = 10


@dataclass
class VizionAciResult:
    """What one Auto Carrier Identification attempt established.

    ``reference`` is the evidence; ``polls`` and ``waited_seconds`` are how long it took
    to get it, which is a property of the provider the POC needs to report rather than
    average away.
    """

    container_number: str
    reference: VizionReference
    demo: bool = False
    polls: int = 0
    waited_seconds: float = 0.0
    create_payload: dict = field(default_factory=dict)
    reference_payload: dict = field(default_factory=dict)

    @property
    def identified(self) -> bool:
        return self.reference.identified

    @property
    def reference_id(self) -> str:
        return self.reference.reference_id


@dataclass
class VizionIngestResult:
    """What one Vizion ingest achieved."""

    container_number: str
    reference_id: str
    demo: bool = False
    updates_fetched: int = 0
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


def get_vizion_provider():
    """Return the Vizion TrackingProvider, creating it once if needed.

    Uses the same ``get_or_create`` helper every carrier source goes through, so there is
    one provider bootstrap in the codebase rather than a Vizion-shaped copy of it. The
    documented base URL is filled in on first creation only; a value someone has since
    edited is left alone.
    """
    provider = get_or_create_tracking_provider(carrier_code=PROVIDER_CODE, carrier_name=PROVIDER_NAME)
    if provider is not None and not provider.base_url:
        provider.base_url = PRODUCTION_BASE_URL
        provider.save(update_fields=["base_url", "updated_at"])
    return provider


# ---------------------------------------------------------------------------
# Carrier resolution
# ---------------------------------------------------------------------------


def resolve_carrier_via_aci(
    *,
    container_number: str,
    demo: bool = False,
    client=None,
    poll_attempts: int = DEFAULT_ACI_POLL_ATTEMPTS,
    poll_interval_seconds: float = DEFAULT_ACI_POLL_INTERVAL_SECONDS,
    sleep=time.sleep,
) -> VizionAciResult:
    """Ask Vizion which carrier is moving a container, given only its number.

    **No carrier hint is sent, and none can be.** The request body is the container
    number alone — that is what invokes ACI — so an owner-prefix guess or a previously
    known carrier cannot leak into the answer even by accident. This is the function the
    acceptance cases call.

    Identification is asynchronous: ``POST /references`` returns before Vizion has
    searched, so the reference is polled until it reports an outcome or the attempts run
    out. Running out is reported as PENDING, not as a failure — Vizion retries
    ``auto_carrier_not_found`` daily for up to seven days, and a POC that waited sixty
    seconds has learned nothing about the seventh day.

    Writes nothing to Container SCM. It does create a reference **at Vizion**, which is
    that provider's billable unit — unlike Traqo's free carrier lookup, resolution and
    tracking are the same purchase here. The caller is told so rather than shielded from
    it, because it is the central cost input to any later routing decision.

    Returns a :class:`VizionAciResult`.
    """
    number = (container_number or "").strip().upper()
    client = client or VizionClient.from_settings(demo=demo)

    create_payload = client.create_reference(number)
    reference = read_reference(create_payload, container_number=number)

    started = time.monotonic()
    polls = 0
    reference_payload: dict = {}

    if reference.reference_id:
        for attempt in range(max(0, poll_attempts)):
            if reference.aci_state != ACI_PENDING:
                break
            if attempt:
                sleep(poll_interval_seconds)
            reference_payload = client.get_reference(reference.reference_id)
            reference = read_reference(reference_payload, container_number=number)
            polls += 1

    logger.info(
        "Vizion ACI for %s: state=%s carrier=%s after %d poll(s).",
        number,
        reference.aci_state,
        reference.carrier_identifier or "none",
        polls,
    )

    return VizionAciResult(
        container_number=number,
        reference=reference,
        demo=demo,
        polls=polls,
        waited_seconds=round(time.monotonic() - started, 1),
        create_payload=create_payload,
        reference_payload=reference_payload,
    )


# ---------------------------------------------------------------------------
# Tracking retrieval
# ---------------------------------------------------------------------------


def fetch_vizion_updates(*, reference_id: str, demo: bool = False, client=None) -> list[dict]:
    """Fetch one reference's update envelopes.

    Separate from persistence so a caller can look at what Vizion said without writing
    anything — which is what the POC command's dry run does.
    """
    client = client or VizionClient.from_settings(demo=demo)
    return client.list_updates(reference_id)


def build_raw_payload(*, reference: VizionReference | None, updates: list[dict]) -> dict:
    """Return the object stored in TrackingRawPayload for one Vizion fetch.

    The reference and the updates are kept together because neither is interpretable
    alone: the updates carry the milestones, and the reference carries which carrier ACI
    attached and whether it is still active. Storing them as one object is also what lets
    ``reparse_tracking_payloads`` re-read a Vizion response later without refetching —
    see :mod:`apps.scm.tracking.sources`.
    """
    return {
        "reference": reference.as_dict() if reference is not None else {},
        "updates": updates,
    }


def ingest_vizion_container(
    *,
    team,
    container,
    reference_id: str,
    demo: bool = False,
    client=None,
    updates: list[dict] | None = None,
    reference: VizionReference | None = None,
) -> VizionIngestResult:
    """Fetch a reference's updates and persist them through tracking ingestion.

    The subscription is Vizion's own: an existing carrier or Traqo subscription for the
    same container is neither replaced nor touched, so a container can be watched through
    Maersk Direct, Traqo and Vizion at once and the three can be compared. That is the
    whole point of the benchmark, and it is only possible because nothing here decides
    which provider owns the container.

    ``updates`` and ``reference`` may be supplied by a caller that has already fetched
    them — the POC command resolves the carrier and then ingests without asking twice.

    Raises the client's typed carrier errors — nothing is written when the fetch fails.
    """
    from apps.scm.tracking.eta_observations import record_provider_eta_observation
    from apps.scm.tracking.manual_refresh import get_or_create_container_subscription
    from apps.scm.tracking.services import create_sync_run
    from apps.scm.tracking.sync import apply_sync_outcome, store_verified_carrier_result

    container_number = container.container_id
    if updates is None:
        updates = fetch_vizion_updates(reference_id=reference_id, demo=demo, client=client)

    events = map_vizion_updates(updates, container_number=container_number)
    raw_payload = build_raw_payload(reference=reference, updates=updates)

    # Ensures the provider row carries Vizion's base URL before the subscription helper
    # get_or_creates the same row by code.
    get_vizion_provider()
    subscription = get_or_create_container_subscription(
        team=team,
        container=container,
        carrier_code=PROVIDER_CODE,
        carrier_name=PROVIDER_NAME,
    )
    if subscription is None:  # pragma: no cover — only if the provider code is blank
        raise RuntimeError("Could not resolve a Vizion tracking subscription.")

    sync_run = create_sync_run(team=team, subscription=subscription, provider=subscription.provider)
    outcome = store_verified_carrier_result(subscription, raw_payload=raw_payload, events=events)
    apply_sync_outcome(subscription, sync_run, outcome)

    # After the events, because whether this forecast is worth recording depends on what
    # they say: a box the events have already brought home has no arrival left to
    # forecast, however Vizion still describes it.
    observation = read_vizion_eta_observation(read_latest_payload(updates), observed_at=timezone.now())
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
        "Vizion ingest for %s (%s): %d update(s), %d mapped, %d created, %d updated, ETA observation %s.",
        container_number,
        "demo" if demo else "production",
        len(updates),
        len(events),
        outcome.events_created,
        outcome.events_updated,
        "recorded" if eta_row else "not recorded",
    )

    return VizionIngestResult(
        container_number=container_number,
        reference_id=reference_id,
        demo=demo,
        updates_fetched=len(updates),
        events_mapped=len(events),
        events_created=outcome.events_created,
        events_updated=outcome.events_updated,
        events_failed=outcome.events_failed,
        raw_payloads_created=outcome.raw_payloads_created,
        subscription=subscription,
        sync_run=sync_run,
        payload=raw_payload,
        eta_observation_recorded=eta_row is not None,
    )
