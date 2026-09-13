"""Settings entry point.

The individual sections live in their own modules — `member_views`,
`tracking_views`, `container_views` — because they read three unrelated parts of
the domain and sharing a file would only share the decorator.
"""

from django.shortcuts import redirect

from apps.scm.decorators import scm_team_admin_required


@scm_team_admin_required
def settings_home(request):
    """`/scm/settings/` has no content of its own; Members is the first section."""
    return redirect("team_settings:members")
