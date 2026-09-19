"""Provider account status, for whoever runs the installation rather than a team.

One page, one question: how much of our agreement with each tracking aggregator is
left. Traqo's plan, its allowance, what this billing cycle has cost and what remains.

**Superuser only, and that is a data-classification decision rather than a UI one.**
Every number here describes the *installation's* commercial relationship with a
provider. There is one Traqo account for the whole deployment — its credential is an
environment setting, not an ``Integration`` row, precisely because no team owns it — so
its plan and quota are no more a customer's business than our hosting bill is. A team
administrator decides which of *their* containers to track; how much provider capacity
that leaves is ours.

So this is not a hidden card on a shared page. It is a separate route behind
``is_superuser``, and the numbers reach no other response in the system: the tracking
layer never persists them (see ``carriers/exceptions.py`` on ``safe_message``), and the
one place a provider states them — an HTTP 402 body — is logged and never rendered. A
team administrator who hits a spent allowance is told that tracking could not be
started and to contact an administrator, which is the whole of what they need.

The same two decorators the platform dashboard already uses
(``apps/dashboard/views.py``), so there is one idea of "platform admin" in the codebase
rather than a second one invented here.

Read-only and advisory. Nothing on this page gates a Start: the provider's own
activation response is what decides, because a cached reading cannot survive two
concurrent starts against one remaining slot.
"""

from __future__ import annotations

import logging

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import render

from .sources import get_provider_usage, usage_reporting_provider_codes

logger = logging.getLogger(__name__)

PROVIDER_STATUS_TEMPLATE = "scm/tracking/pages/provider_status.html"


@user_passes_test(lambda user: user.is_superuser, login_url="/404")
@staff_member_required
def provider_status(request):
    """Show each tracking provider's account allowance for the current billing cycle.

    Fetches live, because a stale number is worse than none for the one question this
    answers. Each provider is asked independently and none of them can break the page:
    the capability returns a state — figures, "not configured here", or "asked and could
    not answer" — rather than raising.
    """
    providers = [
        fetch for fetch in (get_provider_usage(code) for code in usage_reporting_provider_codes()) if fetch is not None
    ]
    return render(
        request,
        PROVIDER_STATUS_TEMPLATE,
        {
            "provider_fetches": providers,
            "active_tab": "provider-status",
        },
    )
