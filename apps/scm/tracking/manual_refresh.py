"""Refreshing one container's carrier tracking on demand, from the UI.

This is a thin orchestration over parts that already exist — it adds no transport,
no parser and no second write path. All it does is answer the two questions the
scheduled poller never has to ask, and then hand over:

Which carrier?
    A scheduled sync starts from a subscription, which already names its provider.
    A person pressing "Refresh tracking" starts from a container, which does not.
    :func:`resolve_carrier_code_for_container` works through the evidence in order
    of how much it is worth — an existing subscription, the shipment's carrier, the
    ISO 6346 owner prefix, and finally the team's single configured carrier — and
    returns "" rather than picking between candidates. Asking the wrong carrier
    produces an answer indistinguishable from "this container does not exist".

Is it worth asking?
    Without an active carrier integration for the team there is nothing to call, so
    the refresh says so instead of creating a subscription and a sync run that can
    only ever be SKIPPED.

Everything after that is :func:`apps.scm.tracking.sync.sync_tracking_subscription`:
fetch, store the raw response, parse, persist idempotently, update the shipment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .models import TrackingSubscription, TrackingSyncRun
from .sync import sync_tracking_subscription

logger = logging.getLogger(__name__)

# Message levels, matching django.contrib.messages' function names. Kept as plain
# strings so this module stays independent of the messages framework.
SUCCESS = "success"
INFO = "info"
WARNING = "warning"
ERROR = "error"


@dataclass(frozen=True)
class RefreshResult:
    """What one manual refresh achieved, ready to show to the person who asked."""

    level: str
    message: str
    carrier_code: str = ""
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
    from apps.scm.integrations.carriers.registry import (
        resolve_carrier_code,
        suggest_carrier_for_owner_code,
    )

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

    # 3. The ISO 6346 owner prefix. Only a hint — boxes are leased and interchanged —
    #    but a better hint than nothing when no carrier has been recorded. The full
    #    container ID is passed because the prefix is owner code + category identifier.
    code = suggest_carrier_for_owner_code(container.container_id)
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
            message=_(
                "Could not tell which carrier to ask about %(reference)s. Set the carrier on its "
                "shipment, or start tracking it from the tracking workspace."
            )
            % {"reference": reference},
        )

    try:
        definition = get_carrier_definition(carrier_code)
    except UnknownCarrierError:
        return RefreshResult(
            level=ERROR,
            carrier_code=carrier_code,
            message=_("'%(carrier)s' is not a carrier this system can call.") % {"carrier": carrier_code},
        )

    # Nothing to call without an integration: say so rather than recording a sync
    # run that could only be SKIPPED.
    if get_carrier_integration(team, carrier_code) is None:
        return RefreshResult(
            level=ERROR,
            carrier_code=carrier_code,
            message=_("%(carrier)s is not connected for this team yet, so there is nothing to fetch.")
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
            carrier_code=carrier_code,
            message=_("Could not start tracking %(reference)s.") % {"reference": reference},
        )

    sync_run = sync_tracking_subscription(subscription)
    if sync_run is None:
        return RefreshResult(
            level=INFO,
            carrier_code=carrier_code,
            message=_("A tracking sync for %(reference)s is already running.") % {"reference": reference},
        )
    return _describe(sync_run, carrier_name=definition.name, carrier_code=carrier_code, reference=reference)


def _describe(sync_run: TrackingSyncRun, *, carrier_name: str, carrier_code: str, reference: str) -> RefreshResult:
    """Turn a finished sync run into something worth reading."""
    statuses = TrackingSyncRun.Status
    common = {
        "carrier_code": carrier_code,
        "events_created": sync_run.events_created,
        "events_updated": sync_run.events_updated,
        "sync_run": sync_run,
    }

    if sync_run.status == statuses.SKIPPED:
        return RefreshResult(
            level=WARNING,
            message=_("%(carrier)s was not called: %(reason)s")
            % {"carrier": carrier_name, "reason": sync_run.error_message or sync_run.get_error_type_display()},
            **common,
        )

    if sync_run.status == statuses.FAILED:
        return RefreshResult(
            level=ERROR,
            message=_("%(carrier)s could not be reached: %(reason)s")
            % {"carrier": carrier_name, "reason": sync_run.error_message or sync_run.get_error_type_display()},
            **common,
        )

    total = sync_run.events_created + sync_run.events_updated
    if not total:
        # The carrier answered and has nothing for this reference — a real answer.
        return RefreshResult(
            level=INFO,
            message=_("%(carrier)s has no tracking data for %(reference)s yet.")
            % {"carrier": carrier_name, "reference": reference},
            **common,
        )

    message = _("%(carrier)s returned %(total)s event(s) for %(reference)s: %(created)s new, %(updated)s updated.") % {
        "carrier": carrier_name,
        "total": total,
        "reference": reference,
        "created": sync_run.events_created,
        "updated": sync_run.events_updated,
    }
    if sync_run.status == statuses.PARTIAL_SUCCESS:
        return RefreshResult(level=WARNING, message=f"{message} {sync_run.error_message}".strip(), **common)
    return RefreshResult(level=SUCCESS, message=message, **common)
