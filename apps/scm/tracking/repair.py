"""Repairing subscription state that a since-fixed bug left behind.

A subscription's ``tracking_status`` is meant to say what the provider is currently
telling us. Until the carrier poller learned to leave non-carrier providers alone, it
did the opposite for them: every cycle it looked Traqo up in the carrier registry, found
nothing, and recorded ``NOT_CONFIGURED`` — "we never got to ask" — over the TRACKING
status a successful Traqo ingestion had written. The provider was working; the poller
was simply not the thing that drives it.

The poller no longer queues those providers, so the bug cannot recur. What it cannot do
is undo what it already wrote, because a status is not derived on read — it is a stored
answer, and the wrong answer stays until something corrects it.

This module corrects it, under three constraints that decide its whole shape:

*The rule is not restated.* :func:`~apps.scm.tracking.sync.tracking_status_from_run`
reads the subscription's own most recent successful run through the same decision
function a live sync uses. A second copy of "what does success mean" here would be a
second thing to keep in step.

*No sync is fabricated.* Nothing is fetched, no ``TrackingSyncRun`` is created, and
``last_synced_at`` is not touched — so the repaired subscription still says it was last
heard from when it actually was. A repair that moved that clock forward would destroy
the freshness evidence the experiment is collecting.

*Evidence is required.* A subscription with no successful run on record is left alone.
``NOT_CONFIGURED`` may well be the truth for it, and this module cannot tell the
difference between a status that is stale and one that is merely unwelcome.

Not a migration. Migrations run once, everywhere, unconditionally; this is a targeted
correction of known-wrong rows that is safe to run twice and reports what it did.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .models import TrackingSubscription, TrackingSyncRun
from .sources import is_polled_by_carrier_sync, non_carrier_provider_codes

logger = logging.getLogger(__name__)

# Statuses that are worth re-deriving. A subscription the poller marked NOT_CONFIGURED
# is the bug's signature; anything else is a real answer from a real attempt and is left
# exactly as it is, whether or not it is flattering.
_REPAIRABLE_STATUSES = (TrackingSubscription.TrackingStatus.NOT_CONFIGURED,)

_SUCCESSFUL_RUN_STATUSES = (TrackingSyncRun.Status.SUCCESS, TrackingSyncRun.Status.PARTIAL_SUCCESS)


@dataclass(frozen=True)
class StatusRepair:
    """What a repair attempt found, and what it changed — if anything."""

    subscription_id: int
    provider_code: str
    reference: str
    before: str
    after: str
    reason: str
    changed: bool

    def as_dict(self) -> dict:
        return {
            "subscription_id": self.subscription_id,
            "provider_code": self.provider_code,
            "reference": self.reference,
            "before": self.before,
            "after": self.after,
            "reason": self.reason,
            "changed": self.changed,
        }


def _unchanged(subscription: TrackingSubscription, reason: str) -> StatusRepair:
    return StatusRepair(
        subscription_id=subscription.pk,
        provider_code=subscription.provider.code,
        reference=subscription.tracking_reference,
        before=subscription.tracking_status,
        after=subscription.tracking_status,
        reason=reason,
        changed=False,
    )


def repair_non_carrier_tracking_status(subscription: TrackingSubscription, *, commit: bool = True) -> StatusRepair:
    """Re-derive one non-carrier subscription's tracking status from its own history.

    Returns a :class:`StatusRepair` in every case, including the cases where nothing is
    changed — the caller reporting on a cleanup needs to know *why* a row was left
    alone as much as it needs to know which rows moved.

    ``commit=False`` computes the verdict without writing, so a caller can show what
    would happen first.
    """
    if is_polled_by_carrier_sync(subscription.provider.code):
        # A carrier's NOT_CONFIGURED is a genuine report about a genuine gap: no
        # credentials, no adapter, or an integration nobody has set up. Clearing it
        # would hide work that needs doing.
        return _unchanged(subscription, "provider is driven by the carrier poller — status left as reported")

    if subscription.tracking_status not in _REPAIRABLE_STATUSES:
        return _unchanged(subscription, "status is not one the old poller could have written")

    run = (
        TrackingSyncRun.objects.filter(subscription=subscription, status__in=_SUCCESSFUL_RUN_STATUSES)
        .order_by("-started_at", "-created_at")
        .first()
    )
    if run is None:
        return _unchanged(subscription, "no successful sync run on record — nothing proves the status is wrong")

    from .sync import tracking_status_from_run

    after = tracking_status_from_run(subscription, run)
    if after == subscription.tracking_status:
        return _unchanged(subscription, "the last successful run implies this same status")

    before = subscription.tracking_status
    reason = (
        f"last successful run #{run.pk} at {run.started_at:%Y-%m-%d %H:%M} stored "
        f"{run.events_created} new and {run.events_updated} updated event(s); "
        f"the carrier poller has since overwritten that with {before}"
    )
    if not commit:
        return StatusRepair(
            subscription_id=subscription.pk,
            provider_code=subscription.provider.code,
            reference=subscription.tracking_reference,
            before=before,
            after=after,
            reason=reason,
            changed=False,
        )

    subscription.tracking_status = after
    # The stale poller message described a problem that never existed for this
    # provider, and the container panel shows it verbatim as the reason tracking is
    # broken. It goes with the status it belongs to; last_synced_at, next_sync_at and
    # the failure counter are deliberately untouched.
    subscription.last_error_message = ""
    subscription.save(update_fields=["tracking_status", "last_error_message", "updated_at"])

    logger.info(
        "Repaired tracking status for subscription %s (%s %s): %s -> %s.",
        subscription.pk,
        subscription.provider.code,
        subscription.tracking_reference,
        before,
        after,
    )
    return StatusRepair(
        subscription_id=subscription.pk,
        provider_code=subscription.provider.code,
        reference=subscription.tracking_reference,
        before=before,
        after=after,
        reason=reason,
        changed=True,
    )


def repair_non_carrier_tracking_statuses(*, team=None, commit: bool = True) -> list[StatusRepair]:
    """Repair every non-carrier subscription whose status the old poller could have broken.

    Scoped to the providers the carrier poller does not drive, so a carrier subscription
    is never a candidate — the queryset, not just the per-row guard, keeps them out.
    """
    subscriptions = TrackingSubscription.objects.filter(
        provider__code__in=non_carrier_provider_codes(),
        tracking_status__in=_REPAIRABLE_STATUSES,
    ).select_related("provider", "team")
    if team is not None:
        subscriptions = subscriptions.filter(team=team)

    return [
        repair_non_carrier_tracking_status(subscription, commit=commit) for subscription in subscriptions.order_by("pk")
    ]
