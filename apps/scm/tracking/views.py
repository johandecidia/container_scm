# Tracking views — request handling, response rendering, form handling only.
# Business logic belongs in services.py; queries belong in selectors.py.
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

from apps.scm.decorators import scm_login_required

from .forms import TrackingSubscriptionForm
from .models import TrackingSubscription
from .selectors import (
    get_team_tracking_subscriptions,
    get_tracking_events_for_shipment,
    get_tracking_sync_runs_for_subscription,
)
from .services import (
    cancel_tracking_subscription,
    create_tracking_subscription,
    pause_tracking_subscription,
    resume_tracking_subscription,
)
from .timeline import get_tracking_timeline_items_for_shipment


@scm_login_required
def tracking_list(request):
    """List all tracking subscriptions for the current team."""
    team = request.default_team
    subscriptions = get_team_tracking_subscriptions(team=team)
    context = {
        "subscriptions": subscriptions,
        "status_choices": TrackingSubscription.Status.choices,
        "team_slug": team.slug,
    }
    if request.htmx:
        return render(request, "scm/tracking/partials/_tracking_subscriptions_table.html", context)
    return render(request, "scm/tracking/pages/tracking_list.html", context)


@scm_login_required
def tracking_detail(request, pk):
    """Detail view for a tracking subscription including recent events and sync history."""
    team = request.default_team
    subscription = get_object_or_404(TrackingSubscription, pk=pk, team=team)
    events = (
        get_tracking_events_for_shipment(team=team, shipment=subscription.shipment) if subscription.shipment else []
    )
    sync_runs = get_tracking_sync_runs_for_subscription(team=team, subscription=subscription)
    context = {
        "subscription": subscription,
        "events": events,
        "sync_runs": sync_runs,
        "team_slug": team.slug,
    }
    return render(request, "scm/tracking/pages/tracking_detail.html", context)


@scm_login_required
def tracking_timeline_partial(request, pk):
    """HTMX partial: return tracking events for a subscription's shipment as a timeline."""
    team = request.default_team
    subscription = get_object_or_404(TrackingSubscription, pk=pk, team=team)
    items = (
        get_tracking_timeline_items_for_shipment(team=team, shipment=subscription.shipment)
        if subscription.shipment
        else []
    )
    return render(
        request,
        "scm/tracking/partials/_tracking_timeline.html",
        {"subscription": subscription, "timeline_items": items, "team_slug": team.slug},
    )


@scm_login_required
def start_tracking(request):
    """Create a new tracking subscription."""
    team = request.default_team
    if request.method == "POST":
        form = TrackingSubscriptionForm(request.POST)
        if form.is_valid():
            subscription = create_tracking_subscription(
                team=team,
                provider=form.cleaned_data["provider"],
                tracking_reference=form.cleaned_data["tracking_reference"],
                reference_type=form.cleaned_data["reference_type"],
            )
            if request.htmx:
                subscriptions = get_team_tracking_subscriptions(team=team)
                return render(
                    request,
                    "scm/tracking/partials/_tracking_subscriptions_table.html",
                    {
                        "subscriptions": subscriptions,
                        "status_choices": TrackingSubscription.Status.choices,
                        "team_slug": team.slug,
                    },
                )
            messages.success(request, _("Tracking subscription created."))
            return redirect("tracking:detail", pk=subscription.pk)
        if request.htmx:
            return render(
                request,
                "scm/tracking/partials/_tracking_subscription_form.html",
                {"form": form, "modal_title": _("Start Tracking"), "form_action": request.path, "team_slug": team.slug},
            )
    else:
        form = TrackingSubscriptionForm()

    context = {
        "form": form,
        "modal_title": _("Start Tracking"),
        "form_action": request.path,
        "team_slug": team.slug,
    }
    return render(request, "scm/tracking/partials/_tracking_subscription_form.html", context)


@scm_login_required
def pause_tracking(request, pk):
    """Pause an active tracking subscription."""
    team = request.default_team
    subscription = get_object_or_404(TrackingSubscription, pk=pk, team=team)
    if request.method == "POST":
        pause_tracking_subscription(subscription)
        if request.htmx:
            return render(
                request,
                "scm/tracking/partials/_tracking_status_badge.html",
                {"subscription": subscription, "team_slug": team.slug},
            )
        messages.success(request, _("Tracking paused."))
        return redirect("tracking:detail", pk=pk)
    return redirect("tracking:detail", pk=pk)


@scm_login_required
def resume_tracking(request, pk):
    """Resume a paused tracking subscription."""
    team = request.default_team
    subscription = get_object_or_404(TrackingSubscription, pk=pk, team=team)
    if request.method == "POST":
        resume_tracking_subscription(subscription)
        if request.htmx:
            return render(
                request,
                "scm/tracking/partials/_tracking_status_badge.html",
                {"subscription": subscription, "team_slug": team.slug},
            )
        messages.success(request, _("Tracking resumed."))
        return redirect("tracking:detail", pk=pk)
    return redirect("tracking:detail", pk=pk)


@scm_login_required
def manual_sync_tracking(request, pk):
    """Trigger a manual sync for a tracking subscription."""
    team = request.default_team
    subscription = get_object_or_404(TrackingSubscription, pk=pk, team=team)
    if request.method == "POST":
        from .tasks import sync_single_tracking_subscription

        sync_single_tracking_subscription.delay(subscription.pk)
        messages.success(request, _("Sync queued."))
        if request.htmx:
            return render(
                request,
                "scm/tracking/partials/_tracking_status_badge.html",
                {"subscription": subscription, "team_slug": team.slug},
            )
        return redirect("tracking:detail", pk=pk)
    return redirect("tracking:detail", pk=pk)


@scm_login_required
def cancel_tracking(request, pk):
    """Cancel a tracking subscription."""
    team = request.default_team
    subscription = get_object_or_404(TrackingSubscription, pk=pk, team=team)
    if request.method == "POST":
        cancel_tracking_subscription(subscription)
        if request.htmx:
            subscriptions = get_team_tracking_subscriptions(team=team)
            return render(
                request,
                "scm/tracking/partials/_tracking_subscriptions_table.html",
                {
                    "subscriptions": subscriptions,
                    "status_choices": TrackingSubscription.Status.choices,
                    "team_slug": team.slug,
                },
            )
        messages.success(request, _("Tracking subscription cancelled."))
        return redirect("tracking:list")
    return redirect("tracking:detail", pk=pk)
