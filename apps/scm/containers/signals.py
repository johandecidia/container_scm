"""One signal: a new team starts with a usable set of container conditions.

Conditions stopped being an enum and became per-team rows, which means a brand new
team would otherwise have an empty condition list and no way to grade a container
until somebody visited Settings. Seeding them on team creation is what keeps "add a
container" working on day one.

A signal rather than a call in ``create_default_team_for_user``, because a team is
also created from the team-creation form, from the admin, and in tests, and the
conditions have to exist in all four cases. It is deliberately the only thing this
app hooks onto ``Team``.

Only on ``created``. :func:`~apps.scm.containers.conditions.ensure_default_conditions`
is idempotent, so running it on every save would be harmless but would put five
queries behind every team edit — including the billing bookkeeping in
``apps.teams.signals``, which saves the team on each membership change.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.teams.models import Team


@receiver(post_save, sender=Team, dispatch_uid="scm_containers.seed_default_conditions")
def seed_default_conditions(sender, instance, created, **kwargs):
    if not created:
        return
    # Imported here rather than at module scope: this module is loaded from
    # AppConfig.ready(), which runs before the app registry is guaranteed to have
    # finished importing every model.
    from .conditions import ensure_default_conditions

    ensure_default_conditions(instance)
