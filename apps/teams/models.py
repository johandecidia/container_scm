import logging
import uuid

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from waffle import get_setting
from waffle.models import CACHE_EMPTY, AbstractUserFlag
from waffle.utils import get_cache, keyfmt

from apps.subscriptions.models import SubscriptionModelBase
from apps.utils.models import BaseModel
from apps.web.meta import absolute_url

from . import roles
from .context import EmptyTeamContextException, get_current_team


class Team(SubscriptionModelBase, BaseModel):
    """
    A Team, with members.
    """

    name = models.CharField(max_length=100)
    slug = models.SlugField(unique=True)
    members = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="teams", through="teams.Membership")

    # your team customizations go here.

    def __str__(self):
        return self.name

    @property
    def email(self):
        return self.membership_set.filter(role=roles.ROLE_ADMIN).first().user.email

    def get_quantity(self) -> int:
        # by default do per-seat billing based on the number of team members
        return self.members.count()

    @property
    def sorted_memberships(self):
        return self.membership_set.order_by("user__email")

    def pending_invitations(self):
        return self.invitations.filter(is_accepted=False)

    @property
    def dashboard_url(self) -> str:
        return reverse("web_team:home", args=[self.slug])


class Membership(BaseModel):
    """
    A user's team membership
    """

    team = models.ForeignKey(Team, on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    role = models.CharField(max_length=100, choices=roles.ROLE_CHOICES)
    # your additional membership fields go here.

    def __str__(self):
        return f"{self.user}: {self.team}"

    def is_admin(self) -> bool:
        return self.role == roles.ROLE_ADMIN

    class Meta:
        # Ensure a user can only be associated with a team once.
        unique_together = ("team", "user")


class Invitation(BaseModel):
    """
    An invitation for new team members.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="invitations")
    email = models.EmailField()
    role = models.CharField(max_length=100, choices=roles.ROLE_CHOICES, default=roles.ROLE_MEMBER)
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="sent_invitations")
    is_accepted = models.BooleanField(default=False)
    accepted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="accepted_invitations", null=True, blank=True
    )

    def get_url(self) -> str:
        return absolute_url(reverse("teams:accept_invitation", args=[self.id]))


class TeamScopedManager(models.Manager):
    """Model manager that will automatically filter the queryset using the team from the global
    team context. If no team is set in the context, it will return an empty queryset unless
    the `STRICT_TEAM_CONTEXT` setting is `True` in which case it will raise an exception.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        team = get_current_team()
        if team is None:
            if getattr(settings, "STRICT_TEAM_CONTEXT", False):
                raise EmptyTeamContextException("Team missing from context")
            else:
                logging.warning("Team not available in filtered context. Use `set_current_team()`.")
            return queryset.none()
        return queryset.filter(team=team)


class BaseTeamModel(BaseModel):
    """
    Abstract model for objects that are part of a team.

    See `teams_example` app for usage.
    """

    team = models.ForeignKey(Team, verbose_name=_("Team"), on_delete=models.CASCADE)

    # Default unfiltered manager
    # Having the default manager unfiltered is useful for the Django Admin and anywhere
    # else where the queryset is not easily customizable.
    # See https://docs.djangoproject.com/en/stable/topics/db/managers/#default-managers
    objects = models.Manager()

    # pre-filtered to the current team
    for_team = TeamScopedManager()

    class Meta:
        abstract = True


class Flag(AbstractUserFlag):
    """Custom Waffle flag to support usage with teams.

    See https://waffle.readthedocs.io/en/stable/types/flag.html#custom-flag-models"""

    FLAG_TEAMS_CACHE_KEY = "FLAG_TEAMS_CACHE_KEY"
    FLAG_TEAMS_CACHE_KEY_DEFAULT = "flag:%s:teams"

    teams = models.ManyToManyField(
        Team,
        blank=True,
        help_text=_("Activate this flag for these teams."),
    )

    def get_flush_keys(self, flush_keys=None):
        flush_keys = super().get_flush_keys(flush_keys)
        teams_cache_key = get_setting(Flag.FLAG_TEAMS_CACHE_KEY, Flag.FLAG_TEAMS_CACHE_KEY_DEFAULT)
        flush_keys.append(keyfmt(teams_cache_key, self.name))
        return flush_keys

    def is_active(self, request, read_only=False):
        is_active = super().is_active(request, read_only)
        if is_active:
            return is_active

        if not self.pk:
            # flag not created
            return False

        team = request.team
        if team:
            team_ids = self._get_team_ids()
            return team.pk in team_ids

    def _get_team_ids(self):
        cache = get_cache()
        cache_key = keyfmt(get_setting(Flag.FLAG_TEAMS_CACHE_KEY, Flag.FLAG_TEAMS_CACHE_KEY_DEFAULT), self.name)
        cached = cache.get(cache_key)
        if cached == CACHE_EMPTY:
            return set()
        if cached:
            return cached

        team_ids = set(self.teams.all().values_list("pk", flat=True))
        cache.add(cache_key, team_ids or CACHE_EMPTY)
        return team_ids
