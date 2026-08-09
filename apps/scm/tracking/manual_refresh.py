"""Refreshing one container's carrier tracking on demand, from the UI.

This is a thin orchestration over parts that already exist — it adds no transport,
no parser and no second write path. All it does is answer the two questions the
scheduled poller never has to ask, and then hand over:

Which carrier?
    A scheduled sync starts from a subscription, which already names its provider.
    A person pressing "Refresh tracking" starts from a container, which does not.
    :func:`resolve_carrier_code_for_container` works through the evidence in order
    of how much it is worth — an existing subscription, the shipment's carrier, a
    carrier recorded explicitly for this container number, and finally the team's
    single configured carrier — and returns "" rather than picking between
    candidates. The ISO 6346 owner prefix is deliberately *not* evidence: it names
    who owns the box, not who is moving it, and asking the wrong carrier produces
    an answer indistinguishable from "this container does not exist".

Is it worth asking?
    Without an active carrier integration for the team there is nothing to call, so
    the refresh says so instead of creating a subscription and a sync run that can
    only ever be SKIPPED.

Everything after that is :func:`apps.scm.tracking.sync.sync_tracking_subscription`:
fetch, store the raw response, parse, persist idempotently, update the shipment.
The call runs inside :func:`interactive_carrier_requests`, so a slow carrier cannot
hold the web worker for minutes.

What the user is told is decided here too, from the sync run's status and error
type alone. Carrier error text can carry a response body or an echoed credential,
so it is logged and never rendered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from apps.scm.integrations.carriers.http import interactive_carrier_requests

from .models import TrackingSubscription, TrackingSyncRun
from .sync import sync_tracking_subscription

logger = logging.getLogger(__name__)

# Message levels, matching django.contrib.messages' function names. Kept as plain
# strings so this module stays independent of the messages framework.
SUCCESS = "success"
INFO = "info"
WARNING = "warning"
ERROR = "error"

# What happened, as a value the panel can branch on without parsing the message.
UPDATED = "updated"
NO_DATA = "no_data"
NOT_CONFIGURED = "not_configured"
CARRIER_UNKNOWN = "carrier_unknown"
UNAVAILABLE = "unavailable"
IN_PROGRESS = "in_progress"


@dataclass(frozen=True)
class RefreshResult:
    """What one manual refresh achieved, ready to show to the person who asked."""

    level: str
    message: str
    state: str = UPDATED
    carrier_code: str = ""
    carrier_name: str = ""
    events_created: int = 0
    events_updated: int = 0
    sync_run: TrackingSyncRun | None = None

    @property
    def events_seen(self) -> int:
        return self.events_created + self.events_updated


def resolve_carrier_code_for_container(team, container) -> str:
    """Return the carrier code to ask about ``container``, or "" when unknown.

    Never guesses between candidates: an empty string means the caller must tell
    the user what to configure, not that no carrier exists.
    """
    from apps.scm.integrations.carriers.registry import resolve_carrier_code

    # 1. An existing subscription already records who we ask about this container.
    subscription = (
        TrackingSubscription.objects.filter(team=team, container=container)
        .exclude(status=TrackingSubscription.Status.CANCELLED)
        .select_related("provider")
        .order_by("-created_at")
        .first()
    )
    if subscription is not None and subscription.provider_id:
        code = resolve_carrier_code(subscription.provider.code)
        if code:
            return code

    # 2. The carrier named on the shipment the container is travelling on.
    from apps.scm.shipments.models import ShipmentContainer

    link = (
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
        .first()
    )
    if link is not None and link.shipment.carrier:
        code = resolve_carrier_code(link.shipment.carrier)
        if code:
            return code

    # 3. A carrier recorded explicitly for this container number when it was planned.
    #    Someone chose it; that outranks anything the system could infer.
    from apps.scm.containers.models import PlannedContainer

    planned = (
        PlannedContainer.objects.filter(team=team, container_number=container.container_id)
        .exclude(carrier="")
        .order_by("-created_at")
        .first()
    )
    if planned is not None:
        code = resolve_carrier_code(planned.carrier)
        if code:
            return code

    # 4. The team's single configured carrier. With exactly one there is nothing to
    #    choose between; with none or several this stays silent.
    from apps.scm.integrations.models import Integration

    configured = list(
        Integration.objects.filter(
            team=team,
            provider_family=Integration.ProviderFamily.CARRIER,
            is_active=True,
        )
        .values_list("provider_code", flat=True)
        .distinct()[:2]
    )
    if len(configured) == 1:
        return resolve_carrier_code(configured[0]) or ""
    return ""


def get_or_create_container_subscription(*, team, container, carrier_code: str, carrier_name: str = ""):
    """Return the container-number subscription for this container and carrier.

    Matches what container discovery creates, on the same natural key, so pressing
    Refresh on a container that discovery later finds does not produce two watches.
    """
    from apps.scm.integrations.carriers.auto_link import get_or_create_tracking_provider
    from apps.scm.shipments.models import ShipmentContainer

    provider = get_or_create_tracking_provider(carrier_code=carrier_code, carrier_name=carrier_name or carrier_code)
    if provider is None:
        return None

    link = (
        ShipmentContainer.objects.filter(container=container, shipment__team=team)
        .select_related("shipment")
        .order_by("-created_at")
        .first()
    )
    subscription, created = TrackingSubscription.objects.get_or_create(
        team=team,
        provider=provider,
        container=container,
        reference_type=TrackingSubscription.ReferenceType.CONTAINER_NUMBER,
        defaults={
            "shipment": link.shipment if link else None,
            "tracking_reference": container.container_id,
            "status": TrackingSubscription.Status.ACTIVE,
        },
    )
    if created:
        logger.info(
            "Created TrackingSubscription for container %s / provider %s from a manual refresh.",
            container.container_id,
            provider.code,
        )
    return subscription


def refresh_container_tracking(*, team, container) -> RefreshResult:
    """Fetch this container's tracking from its carrier now, and report the result.

    Runs in the caller's thread so the person who pressed the button sees the real
    outcome rather than "queued". No carrier failure escapes as an exception — the
    sync engine classifies every one of them — so the result is always something the
    UI can show.
    """
    from apps.scm.integrations.carriers.factory import get_carrier_integration
    from apps.scm.integrations.carriers.registry import UnknownCarrierError, get_carrier_definition

    reference = container.container_id
    carrier_code = resolve_carrier_code_for_container(team, container)
    if not carrier_code:
        return RefreshResult(
            level=ERROR,
            state=CARRIER_UNKNOWN,
            message=_("Carrier could not be determined. Assign a carrier through the shipment or tracking setup."),
        )

    try:
        definition = get_carrier_definition(carrier_code)
    except UnknownCarrierError:
        return RefreshResult(
            level=ERROR,
            state=NOT_CONFIGURED,
            carrier_code=carrier_code,
            carrier_name=carrier_code,
            message=_("'%(carrier)s' is not a carrier this system can call.") % {"carrier": carrier_code},
        )

    # Nothing to call without an integration: say so rather than recording a sync
    # run that could only be SKIPPED.
    if get_carrier_integration(team, carrier_code) is None:
        return RefreshResult(
            level=ERROR,
            state=NOT_CONFIGURED,
            carrier_code=carrier_code,
            carrier_name=definition.name,
            message=_("Tracking is not configured for this container. %(carrier)s is not connected for this team yet.")
            % {"carrier": definition.name},
        )

    subscription = get_or_create_container_subscription(
        team=team,
        container=container,
        carrier_code=carrier_code,
        carrier_name=definition.name,
    )
    if subscription is None:
        return RefreshResult(
            level=ERROR,
            state=NOT_CONFIGURED,
            carrier_code=carrier_code,
            carrier_name=definition.name,
            message=_("Could not start tracking %(reference)s.") % {"reference": reference},
        )

    # Bound the call: someone is waiting for this response.
    with interactive_carrier_requests():
        sync_run = sync_tracking_subscription(subscription)

    if sync_run is None:
        return RefreshResult(
            level=INFO,
            state=IN_PROGRESS,
            carrier_code=carrier_code,
            carrier_name=definition.name,
            message=_("A tracking refresh for this container is already running."),
        )
    return _describe(sync_run, carrier_name=definition.name, carrier_code=carrier_code, reference=reference)


def _describe(sync_run: TrackingSyncRun, *, carrier_name: str, carrier_code: str, reference: str) -> RefreshResult:
    """Turn a finished sync run into something worth reading.

    The wording comes from the run's status and error type only. ``error_message``
    can carry a carrier response body — it belongs in the log, not on the page.
    """
    statuses = TrackingSyncRun.Status
    common = {
        "carrier_code": carrier_code,
        "carrier_name": carrier_name,
        "events_created": sync_run.events_created,
        "events_updated": sync_run.events_updated,
        "sync_run": sync_run,
    }

    if sync_run.status in (statuses.SKIPPED, statuses.FAILED):
        _log_technical_failure(sync_run, carrier_code=carrier_code, reference=reference)

    if sync_run.status == statuses.SKIPPED:
        return RefreshResult(
            level=WARNING,
            state=NOT_CONFIGURED,
            message=_("Tracking is not configured for this container."),
            **common,
        )

    if sync_run.status == statuses.FAILED:
        return RefreshResult(
            level=ERROR,
            state=_failure_state(sync_run.error_type),
            message=_failure_message(sync_run.error_type, carrier_name),
            **common,
        )

    total = sync_run.events_created + sync_run.events_updated
    if not total:
        # The carrier answered and has nothing for this reference — a real answer.
        return RefreshResult(
            level=INFO,
            state=NO_DATA,
            message=_("No tracking data found for this container."),
            **common,
        )

    message = _("Tracking updated — %(total)s events received · %(created)s new · %(updated)s unchanged") % {
        "total": total,
        "created": sync_run.events_created,
        "updated": sync_run.events_updated,
    }
    if sync_run.status == statuses.PARTIAL_SUCCESS:
        return RefreshResult(
            level=WARNING,
            state=UPDATED,
            message=_("%(summary)s. Some events could not be stored.") % {"summary": message},
            **common,
        )
    return RefreshResult(level=SUCCESS, state=UPDATED, message=message, **common)


def _failure_state(error_type: str) -> str:
    """A configuration problem and an outage need different advice, so keep them apart."""
    errors = TrackingSyncRun.ErrorType
    if error_type in (errors.NOT_CONFIGURED, errors.NOT_IMPLEMENTED, errors.UNSUPPORTED_REFERENCE):
        return NOT_CONFIGURED
    return UNAVAILABLE


def _failure_message(error_type: str, carrier_name: str):
    errors = TrackingSyncRun.ErrorType
    if _failure_state(error_type) == NOT_CONFIGURED:
        return _("Tracking is not configured for this container.")
    if error_type == errors.RATE_LIMIT:
        return _("%(carrier)s is rate limiting us right now. Try again in a few minutes.") % {"carrier": carrier_name}
    # Authentication failures are deliberately not spelled out to the user: the fix
    # is an admin task, and the distinction only helps someone probing the setup.
    return _("%(carrier)s tracking is temporarily unavailable.") % {"carrier": carrier_name}


def _log_technical_failure(sync_run: TrackingSyncRun, *, carrier_code: str, reference: str) -> None:
    """Keep the detail the user is not shown, where support can find it."""
    logger.warning(
        "Manual tracking refresh %s for %s/%s: error_type=%s detail=%s",
        sync_run.status,
        carrier_code,
        reference,
        sync_run.error_type or "none",
        sync_run.error_message or "(none)",
    )
