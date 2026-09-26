"""Which timezone a local time at a canonical location is in, and reading one.

A depot reports "2026-09-17 16:00:51" with no offset because it means its own wall
clock. That instant belongs to the place, not to whoever pastes the report, so a
local time recorded against a location is read in the location's zone:

.. code-block:: text

    ContainerLocation.timezone
        → the nearest parent location's timezone   (a terminal keeps its port's clock)
        → the active timezone                       (the user's profile, else settings.TIME_ZONE)

There is no team level: teams carry no timezone, and inventing one here would be a
second place to configure what the location already says.

An unrecognised zone name on a location is skipped rather than trusted, and the
resolution says which level answered, so a caller can tell an operator when the
answer fell through to their own profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone

from .location_hierarchy import ancestor_chain

if TYPE_CHECKING:
    from apps.teams.models import Team

    from .models import ContainerLocation

FROM_LOCATION = "location"
FROM_PARENT_LOCATION = "parent_location"
FROM_ACTIVE = "active"


@dataclass(frozen=True)
class LocationTimezone:
    zone: tzinfo
    name: str
    source: str
    # The location whose timezone answered, when one did.
    location: ContainerLocation | None = None

    @property
    def is_from_location(self) -> bool:
        return self.source != FROM_ACTIVE


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name.strip()) if name and name.strip() else None
    except ZoneInfoNotFoundError, ValueError:
        return None


def resolve_location_timezone(team: Team, location: ContainerLocation) -> LocationTimezone:
    """The timezone a local time recorded at *location* is in. See the module docstring."""
    if (zone := _zone(location.timezone)) is not None:
        return LocationTimezone(zone=zone, name=str(zone), source=FROM_LOCATION, location=location)
    for parent in reversed(ancestor_chain(team, location)):  # Nearest first.
        if (zone := _zone(parent.timezone)) is not None:
            return LocationTimezone(zone=zone, name=str(zone), source=FROM_PARENT_LOCATION, location=parent)
    active = timezone.get_current_timezone()
    return LocationTimezone(zone=active, name=timezone.get_current_timezone_name(), source=FROM_ACTIVE)


@dataclass(frozen=True)
class LocalTime:
    """A local wall-clock time made into an instant, and whether the clock was unclear."""

    instant: datetime
    # The wall-clock time happened twice (autumn DST change); the first, summer-time
    # occurrence was taken.
    is_ambiguous: bool = False
    # The wall-clock time never happened (spring DST change); read with the offset in
    # force just before the change.
    is_nonexistent: bool = False


def localize(value: datetime, zone: tzinfo) -> LocalTime:
    """Make *value* aware in *zone*. An aware *value* keeps its own offset.

    Uses ``timezone.make_aware``, which takes ``fold=0`` for zoneinfo zones: the
    earlier reading of a repeated time, and the pre-transition offset for a skipped one.
    Both cases are flagged so they can be shown rather than silently decided.
    """
    if timezone.is_aware(value):
        return LocalTime(instant=value)
    instant = timezone.make_aware(value, zone)
    other = value.replace(tzinfo=zone, fold=1)
    if instant.utcoffset() == other.utcoffset():
        return LocalTime(instant=instant)
    round_trip = instant.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != value:
        return LocalTime(instant=instant, is_nonexistent=True)
    return LocalTime(instant=instant, is_ambiguous=True)
