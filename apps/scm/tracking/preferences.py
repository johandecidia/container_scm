"""Who somebody *chose* to track a container through, whether to start at all, and
what may be chosen.

Three decisions already exist and this is not a fourth:
:mod:`apps.scm.integrations.carriers.carrier_resolution` says who is carrying the
box, :mod:`.provider_routing` says who to ask about it, and :mod:`.activation`
does the asking. This module owns only the stored preferences those read — a team
default, a per-container override and a team policy on new containers — and the
rule about what is a legal value.

Three scopes, three shapes, for the same reason in each case: the smallest
persistence that can express the choice.

``TeamTrackingSettings.default_provider_code``
    One row per team naming the aggregator tier. Traqo today.

``TeamTrackingSettings.auto_start_tracking_for_new_containers``
    One boolean on the same row: does a container this team creates start being
    tracked immediately. Off by default, because tracking costs provider requests
    and an aggregator shipment slot per box.

``Container.tracking_provider_override``
    One column on the container. Blank is the normal state and means "the team's
    standard routing decides" — which is the behaviour that has always existed.

**A preference is not a subscription.** Nothing here creates, cancels or polls a
watch. Setting an override records an intent; making it real is
:func:`apps.scm.tracking.activation.activate_tracking_route`, through the same
``get_or_create`` natural key every other source uses, so an override can never
produce a second watch on the same provider or a second polling job.

**Validation is here, not in the view.** "Is Maersk a legal tracking source for this
container" has one answer: the carrier must be the one moving the box, the team must
have that integration active with credentials, and the adapter must be able to
answer by container number. A view that re-derived any part of that would eventually
offer an option routing then refuses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from apps.scm.integrations.traqo import PROVIDER_CODE as TRAQO_PROVIDER_CODE
from apps.scm.integrations.traqo import PROVIDER_NAME as TRAQO_PROVIDER_NAME

from .models import TeamTrackingSettings

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

    from apps.scm.containers.models import Container
    from apps.teams.models import Team

logger = logging.getLogger(__name__)

# The stored value that means "no override — use the team's standard routing". Blank
# rather than a magic string, so an unset column and an explicit "standard" are the
# same state and cannot drift apart.
TEAM_DEFAULT = ""


class InvalidTrackingProvider(ValueError):
    """A provider was chosen that cannot track this container. Carries a reason to show."""


@dataclass(frozen=True)
class ProviderOption:
    """One choice on the container's "Tracking via" selector."""

    value: str
    # Display text, so a lazy translation is a legal value: the option is built here
    # and rendered in a request, and forcing it to ``str`` now would resolve it in
    # whatever language the builder happened to run in.
    label: StrOrPromise
    # Why this one is currently selected, or "" — used only for the help line.
    detail: StrOrPromise = ""
    is_selected: bool = False

    @property
    def is_team_default(self) -> bool:
        return self.value == TEAM_DEFAULT


# ---------------------------------------------------------------------------
# Team default
# ---------------------------------------------------------------------------


def get_team_tracking_settings(team: Team) -> TeamTrackingSettings:
    """The team's tracking settings row, created with the shipped default if absent.

    Created on read rather than on team creation: a team that existed before this
    model must answer the same way as one created after it, and the default is a
    value rather than a decision.
    """
    settings, _created = TeamTrackingSettings.objects.get_or_create(team=team)
    return settings


def get_team_default_provider(team: Team) -> str:
    """The provider code this team falls back to when no carrier can be called directly."""
    return get_team_tracking_settings(team).default_provider_code or TRAQO_PROVIDER_CODE


def get_team_default_provider_name(team: Team) -> str:
    """The team default's display name, for the "Standard (Traqo)" label."""
    code = get_team_default_provider(team)
    return TRAQO_PROVIDER_NAME if code == TRAQO_PROVIDER_CODE else code


def set_team_default_provider(team: Team, provider_code: str) -> TeamTrackingSettings:
    """Set the team's default tracking provider.

    Only a provider the scheduled sync can actually fetch on its own is allowed.
    Vizion is the reason that check is not "is it an aggregator": it can track, and
    polling it would spend a billable reference per cycle — see
    :mod:`apps.scm.tracking.sources`.
    """
    from .sources import get_non_carrier_source

    code = (provider_code or "").strip().lower()
    source = get_non_carrier_source(code)
    if source is None or not source.supports_scheduled_tracking:
        raise InvalidTrackingProvider(
            _("{provider} cannot be a team default tracking provider.").format(provider=provider_code or "—")
        )

    settings = get_team_tracking_settings(team)
    if settings.default_provider_code != code:
        settings.default_provider_code = code
        settings.save(update_fields=["default_provider_code", "updated_at"])
        logger.info("Team %s default tracking provider set to %s.", team.pk, code)
    return settings


# ---------------------------------------------------------------------------
# Automatic tracking for new containers
# ---------------------------------------------------------------------------


def get_team_auto_start_tracking(team: Team) -> bool:
    """Whether a container this team creates should start being tracked at once.

    The default is off and that is a cost decision rather than caution: starting
    tracking spends a provider request per container and, through an aggregator, a
    shipment slot — so a team pasting six hundred container numbers must have chosen
    that rather than found out afterwards. A team that wants it says so once here,
    and an import can still override it for a single run.

    What "start tracking" then *means* is not decided here. It is
    :func:`apps.scm.tracking.lifecycle.start_container_tracking`, the same function the
    Start button calls, so there is no separate import tracking behaviour to diverge.
    """
    return get_team_tracking_settings(team).auto_start_tracking_for_new_containers


def set_team_auto_start_tracking(team: Team, enabled: bool) -> TeamTrackingSettings:
    """Turn automatic tracking of newly created containers on or off for this team.

    Applies to containers created *after* the change and to nothing already in the
    fleet: it is a policy about creation, not a batch operation. Switching it on does
    not sweep the existing containers into tracking, and switching it off does not stop
    anything that is already tracked — that is Stop's job, per container, which is
    where somebody can see what it costs.
    """
    settings = get_team_tracking_settings(team)
    value = bool(enabled)
    if settings.auto_start_tracking_for_new_containers != value:
        settings.auto_start_tracking_for_new_containers = value
        settings.save(update_fields=["auto_start_tracking_for_new_containers", "updated_at"])
        logger.info("Team %s automatic tracking for new containers set to %s.", team.pk, value)
    return settings


# ---------------------------------------------------------------------------
# Automatic stop, once a container is received
# ---------------------------------------------------------------------------


def get_team_stop_tracking_on_receive(team: Team) -> bool:
    """Whether a container this team receives should stop being tracked straight after.

    Off by default. What "stop" then means is
    :func:`apps.scm.tracking.lifecycle.stop_container_tracking` — the same function the
    Stop button calls — so a receive has no tracking behaviour of its own.
    """
    return get_team_tracking_settings(team).stop_tracking_on_receive


def set_team_stop_tracking_on_receive(team: Team, enabled: bool) -> TeamTrackingSettings:
    """Turn automatic stop-on-receive on or off for this team.

    Applies to receives recorded after the change. Switching it on does not stop
    containers received earlier; that stays a per-container Stop.
    """
    settings = get_team_tracking_settings(team)
    value = bool(enabled)
    if settings.stop_tracking_on_receive != value:
        settings.stop_tracking_on_receive = value
        settings.save(update_fields=["stop_tracking_on_receive", "updated_at"])
        logger.info("Team %s stop tracking on receive set to %s.", team.pk, value)
    return settings


# ---------------------------------------------------------------------------
# Container override
# ---------------------------------------------------------------------------


def get_container_provider_override(container: Container | None) -> str:
    """The provider code chosen for this container, or ``TEAM_DEFAULT``."""
    if container is None:
        return TEAM_DEFAULT
    return (container.tracking_provider_override or TEAM_DEFAULT).strip().lower()


def get_allowed_provider_codes(team: Team, container: Container) -> set[str]:
    """Every provider that may be chosen for this container, excluding the team default.

    Traqo whenever it is configured for live calls, plus the direct carriers that are
    *valid for this container's carrier*: one carrier moves the box, so at most one
    direct provider can be a legal source for it. Offering the team's other
    integrations would invite a choice that routing has to refuse.
    """
    from apps.scm.integrations.carriers.registry import UnknownCarrierError, get_carrier_definition
    from apps.scm.integrations.traqo.discovery import is_traqo_configured
    from apps.scm.team_settings.tracking_selectors import get_usable_carrier_codes

    allowed: set[str] = set()
    if is_traqo_configured():
        allowed.add(TRAQO_PROVIDER_CODE)

    carrier_code = get_container_carrier_code(team, container)
    if carrier_code and carrier_code in get_usable_carrier_codes(team):
        try:
            definition = get_carrier_definition(carrier_code)
        except UnknownCarrierError:
            definition = None
        if definition is not None and definition.capabilities.supports_tracking_by_container:
            allowed.add(carrier_code)
    return allowed


def get_container_carrier_code(team: Team, container: Container) -> str:
    """The carrier believed to be moving this container, from evidence already held.

    Reuses ``get_trusted_carrier_for_container`` — a verified source, then the planned
    container, then the shipment. No provider is called: deciding which options to
    offer must not cost a request, and a carrier nobody has established yet simply
    means no direct provider is offered.
    """
    from apps.scm.integrations.carriers.carrier_resolution import get_trusted_carrier_for_container

    carrier_code, _name, _source = get_trusted_carrier_for_container(team, container)
    return carrier_code


def get_provider_options(team: Team, container: Container) -> list[ProviderOption]:
    """The "Tracking via" choices for this container, in the order they are offered.

    The team default first, because it is the answer for almost every container, then
    Traqo explicitly, then the container's own carrier when it can be called directly.
    """
    from apps.scm.integrations.carriers.registry import UnknownCarrierError, get_carrier_definition

    current = get_container_provider_override(container)
    allowed = get_allowed_provider_codes(team, container)

    options = [
        ProviderOption(
            value=TEAM_DEFAULT,
            label=_("Standard ({provider})").format(provider=get_team_default_provider_name(team)),
            detail=_("Uses a direct carrier integration when one can answer, otherwise the team default."),
            is_selected=current == TEAM_DEFAULT,
        )
    ]
    if TRAQO_PROVIDER_CODE in allowed:
        options.append(
            ProviderOption(
                value=TRAQO_PROVIDER_CODE,
                label=TRAQO_PROVIDER_NAME,
                detail=_("Always ask Traqo about this container, even if its carrier could be called directly."),
                is_selected=current == TRAQO_PROVIDER_CODE,
            )
        )
    for code in sorted(allowed - {TRAQO_PROVIDER_CODE}):
        try:
            name = get_carrier_definition(code).name
        except UnknownCarrierError:  # pragma: no cover — allowed codes are registered
            name = code
        options.append(
            ProviderOption(
                value=code,
                label=name,
                detail=_("Ask this carrier's own API directly."),
                is_selected=current == code,
            )
        )

    # A stored override whose provider has since been deactivated or whose carrier has
    # changed is still shown, selected, and marked — silently presenting it as
    # "Standard" would hide a setting that is actively breaking this container's
    # tracking, which is the opposite of surfacing the error.
    if current != TEAM_DEFAULT and current not in allowed:
        options.append(
            ProviderOption(
                value=current,
                label=_("{provider} (unavailable)").format(provider=current),
                detail=_("This provider is no longer a valid tracking source for this container."),
                is_selected=True,
            )
        )
    return options


def set_container_provider_override(*, team: Team, container: Container, provider_code: str) -> str:
    """Record which provider this container should be tracked through.

    ``provider_code`` of ``""`` clears the override and returns the container to the
    team's standard routing. Anything else must be in
    :func:`get_allowed_provider_codes` — the check is here so the option list and the
    write cannot disagree.

    Returns the stored value. Raises :class:`InvalidTrackingProvider` with a message
    fit to show when the choice is not a legal source for this container.
    """
    code = (provider_code or "").strip().lower()
    if code and code not in get_allowed_provider_codes(team, container):
        raise InvalidTrackingProvider(
            _("{provider} is not an available tracking source for this container.").format(provider=provider_code)
        )

    if container.tracking_provider_override != code:
        container.tracking_provider_override = code
        container.save(update_fields=["tracking_provider_override", "updated_at"])
        logger.info(
            "Container %s tracking provider override set to %s.",
            container.container_id,
            code or "team default",
        )
    return code
