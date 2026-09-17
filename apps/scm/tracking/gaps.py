"""Recognising the part of a journey nothing has explained.

A tracking gap is not "we have not heard from the carrier lately". Carriers go
quiet for days in the ordinary course of a voyage, and a platform that called that
a gap would cry wolf on every healthy shipment until nobody read the warnings.

A gap here requires a *contradiction*: two sources that cannot both be describing
the same journey unless something happened in between that nobody reported.

    last carrier observation      Born, NL          12 Aug
    our own physical record       Gothenburg, SE    16 Aug
    carrier events explaining the move              none

The box cannot be in Born and then in Gothenburg without moving, so the move is
real and unreported. That is a gap, and it is worth showing.

What is deliberately *not* a gap:

*Silence.* No event for a week says nothing about whether the box moved.

*The same place, later.* A depot receipt at the terminal the carrier discharged to
is the carrier's story continuing, not a hole in it.

*A move a carrier already accounts for.* If any carrier observed the container at
the destination, the move is explained — by whichever carrier reported it, not
necessarily the one that reported the origin.

*A forecast.* An estimated arrival at Gothenburg is what a carrier expects, not
something it saw. It lowers the confidence of a gap, because at least somebody
expected the box to go there, but it cannot close one.

Nothing is stored. A gap is computed from the journey every time it is asked for,
so it disappears the moment a new event explains the segment — which is the point:
the gap is a statement about what we know now, not a record to be reconciled.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from .journey import ContainerJourney, JourneyPoint, JourneySource

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

# Place-name tokens shorter than this carry no identifying information ("de", "of")
# and would match places that have nothing to do with each other.
_MIN_TOKEN_LENGTH = 3

# Words that appear in place names without narrowing them down. Kept small and
# language-neutral on purpose — this list exists to avoid matching "Port of Born"
# to "Port of Gothenburg" on the word "port", not to parse addresses.
_GENERIC_PLACE_TOKENS = frozenset(
    {
        "port",
        "terminal",
        "terminalen",
        "depot",
        "depo",
        "container",
        "containers",
        "harbour",
        "harbor",
        "hamn",
        "yard",
        "warehouse",
        "of",
        "the",
        "and",
        "und",
        "van",
        "der",
        "den",
        "las",
        "los",
    }
)


class GapReason(TextChoices):
    """Why a segment of the journey is unexplained."""

    UNEXPLAINED_PHYSICAL_MOVE = (
        "unexplained_physical_move",
        _("The container was observed beyond the last place any carrier reported it."),
    )


class GapConfidence(TextChoices):
    """How sure we are that a segment is genuinely untracked.

    Both values mean the move happened and nothing observed it. They differ in
    whether anybody was even expecting it: a carrier that forecast the destination
    knew the box was headed there and simply never confirmed it, which is a weaker
    signal that a tracking source is missing than a destination no carrier has ever
    mentioned.
    """

    HIGH = "high", _("High")
    MEDIUM = "medium", _("Medium")


@dataclass(frozen=True)
class TrackingGap:
    """A segment of the journey that no tracking source accounts for."""

    from_point: JourneyPoint
    to_point: JourneyPoint
    reason: str = GapReason.UNEXPLAINED_PHYSICAL_MOVE
    confidence: str = GapConfidence.HIGH

    # -- where it starts ---------------------------------------------------

    @property
    def from_location(self) -> str:
        return self.from_point.place_label

    @property
    def from_unlocode(self) -> str:
        return self.from_point.location_unlocode

    @property
    def from_datetime(self) -> datetime | None:
        return self.from_point.occurred_at

    @property
    def from_source(self) -> JourneySource:
        return self.from_point.source

    # -- where it ends -----------------------------------------------------

    @property
    def to_location(self) -> str:
        return self.to_point.place_label

    @property
    def to_unlocode(self) -> str:
        return self.to_point.location_unlocode

    @property
    def to_datetime(self) -> datetime | None:
        return self.to_point.occurred_at

    @property
    def to_source(self) -> JourneySource:
        return self.to_point.source

    # -- how it reads ------------------------------------------------------

    @property
    def reason_label(self) -> str:
        return str(GapReason(self.reason).label)

    @property
    def confidence_label(self) -> str:
        return str(GapConfidence(self.confidence).label)

    @property
    def is_strong(self) -> bool:
        """True when no source has so much as forecast the destination.

        Used to decide whether looking for another carrier is worth the call.
        """
        return self.confidence == GapConfidence.HIGH

    @property
    def segment(self) -> str:
        return f"{self.from_location} → {self.to_location}"

    @property
    def description(self) -> StrOrPromise:
        return _("No tracking source currently explains the move from %(origin)s to %(destination)s.") % {
            "origin": self.from_location or _("its last reported place"),
            "destination": self.to_location or _("where it was observed"),
        }


def detect_tracking_gap(journey: ContainerJourney) -> TrackingGap | None:
    """Return the journey's unexplained segment, or None when there is not one.

    One gap, not a list: the case worth acting on is the current frontier — the box
    is somewhere no carrier followed it to. Earlier segments of a journey that has
    since been picked up again are history, and a list of them would be a report
    about our own data quality rather than about this container.
    """
    destination = journey.physical_observation
    if destination is None or destination.occurred_at is None or not destination.has_a_place:
        # Without a dated, placed observation of our own there is no second opinion
        # to contradict the carrier with, and silence alone is never a gap.
        return None

    origin = journey.latest_located_actual_carrier_point
    if origin is None or origin.occurred_at is None:
        # Nothing has ever been observed by a carrier. That is an untracked
        # container, not a journey with a hole in it.
        return None

    if destination.occurred_at <= origin.occurred_at:
        # The carrier has seen the box at least as recently as we have, so our
        # record is not evidence of anything it missed.
        return None

    if places_match(origin, destination):
        # Same place, later — the carrier's story continuing, not a gap in it.
        return None

    if _explained_by_a_carrier(journey, destination, after=origin.occurred_at):
        return None

    return TrackingGap(
        from_point=origin,
        to_point=destination,
        reason=GapReason.UNEXPLAINED_PHYSICAL_MOVE,
        confidence=(GapConfidence.MEDIUM if _forecast_mentions(journey, destination) else GapConfidence.HIGH),
    )


def places_match(first: JourneyPoint, second: JourneyPoint) -> bool:
    """True when two points are plausibly the same place.

    Deliberately generous, and only used to *withhold* a gap. UN/LOCODE settles it
    when both sides have one; otherwise a shared distinctive word does, so a depot
    recorded as "Oceanterminalen, Göteborg" is not reported as a different place
    from a carrier's "Göteborg".

    The limit is language: a carrier's "Gothenburg" shares no word with "Göteborg",
    so a physical location without a UN/LOCODE can read as a different place than
    the carrier's own name for it. Recording the UN/LOCODE on the location — its
    ``external_reference`` — makes the comparison exact.
    """
    first_code = first.location_unlocode.strip().upper()
    second_code = second.location_unlocode.strip().upper()
    if first_code and second_code:
        return first_code == second_code

    first_tokens = _place_tokens(first)
    second_tokens = _place_tokens(second)
    if not first_tokens or not second_tokens:
        # One side names nowhere. Not a match, but the caller has already required
        # both ends to have a place before it gets here.
        return False
    return bool(first_tokens & second_tokens)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _explained_by_a_carrier(journey: ContainerJourney, destination: JourneyPoint, *, after: datetime) -> bool:
    """True when some carrier observed the container at the destination.

    Any carrier: a second provider that picked the box up and delivered it to
    Gothenburg explains the move even though the first provider never mentioned it —
    which is the whole reason a container may have several tracking sources.

    Observations only. A forecast for the destination is not an observation of it,
    and letting one close a gap would hide exactly the case this exists to surface.
    """
    for point in journey.carrier_points:
        if not point.is_actual or point.occurred_at is None:
            continue
        if point.occurred_at < after:
            continue
        if places_match(point, destination):
            return True
    return False


def _forecast_mentions(journey: ContainerJourney, destination: JourneyPoint) -> bool:
    """True when a carrier at least expected the container at the destination."""
    return any(
        point.is_estimated and places_match(point, destination) for point in journey.carrier_points if point.has_a_place
    )


def _place_tokens(point: JourneyPoint) -> set[str]:
    """The distinctive words in a point's place, for a conservative name match.

    Accents are folded away so "Göteborg" and "Goteborg" are one word, and only the
    place's own names are read — never an event description, which could share a
    word with a place it has nothing to do with.
    """
    text = " ".join(part for part in (point.location_name, *point.location_aliases) if part)
    decomposed = unicodedata.normalize("NFKD", text)
    folded = "".join(character for character in decomposed if not unicodedata.combining(character)).casefold()
    stripped = "".join(character if character.isalnum() else " " for character in folded)
    return {
        token for token in stripped.split() if len(token) >= _MIN_TOKEN_LENGTH and token not in _GENERIC_PLACE_TOKENS
    }
