"""What has to be fixed in the master data before the map can improve.

LOC-4 ended with a map that refuses to draw what it cannot honestly place, and a
coverage line that says how much that is. This is the other half of that sentence:
the finite list of master-data edits that would make the refusals stop.

Three kinds of gap, and each has exactly one safe action:

``CoordinateGap``
    A canonical location MCR believes in and has never recorded a latitude for. The
    place is real; the map cannot draw it. The action is the location form.
``AMBIGUOUS`` evidence
    A provider named a place and the evidence fitted several canonical locations
    equally well, so :func:`~apps.scm.containers.location_resolver.resolve_location`
    refused to choose. The action is an alias that breaks the tie.
``UNRESOLVED`` evidence
    A provider named a place no canonical location claims. The action is an alias —
    or, where the place genuinely is not in the network yet, a new location, which
    nothing here will create.

**This module owns no facts and writes nothing.** Every number is read off
something that already decided it: the coordinate gap off ``ContainerLocation``, the
plottable counts off LOC-4's :class:`~apps.scm.visibility.map_positions.MapCoverage`,
the resolution statuses off the columns ingestion wrote, and the candidate list off
the resolver itself. There is no second definition of "plottable" here and no second
resolver — the map remains the authority for what a position means, and
``location_resolver`` for what a place name means. The alias write lives in
``containers``, which owns the master data.

**Evidence is aggregated into tasks, not listed as rows.** A carrier that has said
``GOTHENBURG`` four hundred times is one decision, and four hundred rows of it would
bury the nineteen other decisions underneath. Groups are keyed on the identifying
evidence — provider, reported name, reported code, and the status that resulted —
and never on coordinates, which vary event to event and are not an identity.

**Only active locations.** ``resolve_location`` considers active locations only, so
that set is the resolver's whole universe and the only one an alias can point into.
A deactivated location still holding inventory is a different problem — stale state
on a retired place — which is why the blocked-position counts here can add up to
less than the map's own ``containers_missing_coordinates``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, cast

from django.db.models import Count, Max, Q

from apps.scm.containers.choices import LocationResolutionStatus
from apps.scm.containers.location_identity import normalize_location_name
from apps.scm.containers.models import Container, ContainerLocation, LocationAlias
from apps.scm.shipments.models import Shipment
from apps.scm.tracking.models import TrackingEvent

from .map_positions import MapCoverage, build_map_positions
from .selectors import ACTIVE_SHIPMENT_STATUSES, list_visibility_objects

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from apps.teams.models import Team

# A location the map cannot draw. Either column being NULL is enough: half a
# coordinate pair is not a point, and the model deliberately allows it because LOC-1
# refused to invent the other half. Stated once and reused from both ends of the
# foreign keys that point at a location.
MISSING_COORDINATES = Q(latitude__isnull=True) | Q(longitude__isnull=True)
MISSING_COORDINATES_ON_CURRENT_LOCATION = Q(current_location__latitude__isnull=True) | Q(
    current_location__longitude__isnull=True
)
MISSING_COORDINATES_ON_DESTINATION = Q(destination_location__latitude__isnull=True) | Q(
    destination_location__longitude__isnull=True
)

# Evidence an operator can actually act on: the provider named the place, or sent a
# code for it. Deliberately narrower than
# :data:`apps.scm.tracking.positions.HAS_A_PLACE`, which also counts an event located
# only by coordinates — there is no alias to record for a bare latitude. Used for the
# counts *and* for the queue, so the summary and the list below it describe the same
# evidence.
NAMED_LOCATION_EVIDENCE = Q(location_name__gt="") | Q(location_unlocode__gt="")

# Statuses that leave a place unclaimed. Both are work; they differ in why.
UNMATCHED_STATUSES = (LocationResolutionStatus.AMBIGUOUS, LocationResolutionStatus.UNRESOLVED)

# Display caps. A queue is a screen of work, not an export. The totals are reported
# separately so the page can say what it is not showing.
COORDINATE_GAP_LIMIT = 50
EVIDENCE_LIMIT = 25


@dataclass(frozen=True)
class CoordinateGap:
    """One canonical location with no coordinates, and what that currently costs.

    The three counts are three different answers, kept apart on purpose. Containers
    *are* here; shipments are *coming* here; blocked positions are what the map
    refused to draw because of it — and the third is not the sum of the first two,
    since a container the map cannot place may have no accepted physical position at
    all.
    """

    location: ContainerLocation
    container_count: int = 0
    shipment_count: int = 0
    blocked_positions: int = 0

    @property
    def name(self) -> str:
        """The place inside its parent, as the rest of the product names it."""
        return self.location.full_name

    @property
    def unlocode(self) -> str:
        return self.location.unlocode

    @property
    def type_label(self) -> str:
        return self.location.get_location_type_display()

    @property
    def has_impact(self) -> bool:
        """True when something is currently worse off for this gap.

        A location with no impact is still listed: it is master data that will be
        wrong the first time a box arrives there, and a queue exists to make that a
        five-minute job now rather than a surprise later.
        """
        return bool(self.container_count or self.shipment_count or self.blocked_positions)

    @property
    def impact_key(self) -> tuple:
        """Worst first, then alphabetically so the order is stable between loads."""
        return (-self.blocked_positions, -self.container_count, -self.shipment_count, self.location.name)


@dataclass(frozen=True)
class LocationEvidence:
    """Repeated provider evidence about one place, as a single decision.

    ``candidates`` is the resolver's own tie, not this module's guess: for an
    ambiguous group the resolver is asked again and its refusal carried through
    verbatim, so the queue shows exactly the places it could not choose between. An
    unresolved group has no candidates by definition — nothing matched.

    ``alias_location`` is a hint about work already done, not a resolution. An alias
    recorded for this source and name means the decision has been taken; the
    historic events still say what they said, because nothing here rewrites them.

    Frozen because it is an interpretation of the evidence as it was read. A caller
    able to edit one could describe a decision nobody took.
    """

    provider_code: str = ""
    provider_name: str = ""
    raw_name: str = ""
    raw_unlocode: str = ""
    status: str = LocationResolutionStatus.UNRESOLVED
    event_count: int = 0
    container_count: int = 0
    last_seen_at: datetime | None = None
    candidates: list[ContainerLocation] = field(default_factory=list)
    alias_location: ContainerLocation | None = None

    @property
    def is_ambiguous(self) -> bool:
        return self.status == LocationResolutionStatus.AMBIGUOUS

    @property
    def label(self) -> str:
        """What the provider called the place, falling back to the code it sent."""
        return self.raw_name or self.raw_unlocode

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)

    @property
    def is_aliased(self) -> bool:
        return self.alias_location is not None

    @property
    def can_record_alias(self) -> bool:
        """True when an alias would actually resolve this evidence.

        An alias matches on the reported *name*, which is what the tracking pipeline
        puts in a :class:`~apps.scm.containers.location_resolver.LocationQuery`.
        Evidence carrying only a UN/LOCODE cannot be fixed that way: the code has to
        go on a canonical location, or the locations sharing it have to be arranged
        into the hierarchy they really form. Offering an alias box for it would
        record a row no lookup would ever read.
        """
        return bool(self.raw_name) and not self.is_aliased


@dataclass(frozen=True)
class LocationQualitySummary:
    """The cheap headline: how complete the master data is, in five numbers.

    Two queries and no fleet read, so a page that only wants to point at this queue
    — the Locations list does — can afford to say how much work is in it.
    """

    locations_with_coordinates: int = 0
    locations_missing_coordinates: int = 0
    evidence_resolved: int = 0
    evidence_ambiguous: int = 0
    evidence_unresolved: int = 0

    @property
    def total_locations(self) -> int:
        return self.locations_with_coordinates + self.locations_missing_coordinates

    @property
    def total_evidence(self) -> int:
        return self.evidence_resolved + self.evidence_ambiguous + self.evidence_unresolved

    @property
    def unmatched_evidence(self) -> int:
        return self.evidence_ambiguous + self.evidence_unresolved

    @property
    def has_gaps(self) -> bool:
        return bool(self.locations_missing_coordinates or self.unmatched_evidence)


@dataclass(frozen=True)
class LocationDataQuality:
    """Everything the Location Data Quality page renders.

    ``coverage`` is LOC-4's own :class:`MapCoverage`, carried rather than restated.
    The container numbers on this page and the coverage line under the Control
    Tower's map are the same numbers from the same read model, which is the whole
    reason this page can claim to explain that one.
    """

    summary: LocationQualitySummary = field(default_factory=LocationQualitySummary)
    coverage: MapCoverage = field(default_factory=MapCoverage)
    coordinate_gaps: list[CoordinateGap] = field(default_factory=list)
    ambiguous_evidence: list[LocationEvidence] = field(default_factory=list)
    unresolved_evidence: list[LocationEvidence] = field(default_factory=list)
    # Groups found before the display caps, so the page can say what it is hiding.
    ambiguous_total: int = 0
    unresolved_total: int = 0

    @property
    def has_work(self) -> bool:
        return bool(self.coordinate_gaps or self.ambiguous_evidence or self.unresolved_evidence)

    @property
    def gaps_hidden(self) -> int:
        return max(self.summary.locations_missing_coordinates - len(self.coordinate_gaps), 0)

    @property
    def ambiguous_hidden(self) -> int:
        return max(self.ambiguous_total - len(self.ambiguous_evidence), 0)

    @property
    def unresolved_hidden(self) -> int:
        return max(self.unresolved_total - len(self.unresolved_evidence), 0)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def get_location_quality_summary(team: Team) -> LocationQualitySummary:
    """The headline counts, in two aggregate queries.

    Separated from the full page so a pointer to this queue costs two queries rather
    than a fleet-wide map read. Nothing here needs the containers.
    """
    locations = ContainerLocation.objects.filter(team=team, is_active=True).aggregate(
        total=Count("pk"),
        missing=Count("pk", filter=MISSING_COORDINATES),
    )
    missing = locations["missing"] or 0

    by_status = {
        row["location_resolution_status"]: row["total"]
        for row in cast(
            "Iterable[dict]",
            TrackingEvent.objects.filter(NAMED_LOCATION_EVIDENCE, team=team)
            .values("location_resolution_status")
            .annotate(total=Count("pk")),
        )
    }
    return LocationQualitySummary(
        locations_with_coordinates=(locations["total"] or 0) - missing,
        locations_missing_coordinates=missing,
        evidence_resolved=by_status.get(LocationResolutionStatus.RESOLVED, 0),
        evidence_ambiguous=by_status.get(LocationResolutionStatus.AMBIGUOUS, 0),
        evidence_unresolved=by_status.get(LocationResolutionStatus.UNRESOLVED, 0),
    )


def get_location_data_quality(team: Team) -> LocationDataQuality:
    """Build the whole queue for *team*.

    Composed from reads that do not grow with the volume of evidence: LOC-4's map
    read once (the Control Tower's own cost plus two), three aggregates for the
    coordinate gap and its impact, two for the summary, one over the tracking
    evidence and one for the aliases already recorded. The resolver is then asked
    once per *displayed ambiguous group* — a capped number, and the one place its
    answer is the finding rather than something already stored.
    """
    coverage, blocked = _map_read(team)
    evidence = _evidence_groups(team)
    ambiguous = [row for row in evidence if row.is_ambiguous]
    unresolved = [row for row in evidence if not row.is_ambiguous]

    return LocationDataQuality(
        summary=get_location_quality_summary(team),
        coverage=coverage,
        coordinate_gaps=_coordinate_gaps(team, blocked=blocked),
        ambiguous_evidence=_with_candidates(team, ambiguous[:EVIDENCE_LIMIT]),
        unresolved_evidence=unresolved[:EVIDENCE_LIMIT],
        ambiguous_total=len(ambiguous),
        unresolved_total=len(unresolved),
    )


def has_unmatched_evidence(team: Team, *, source: str, raw_name: str) -> bool:
    """True when *team* really has unmatched evidence naming *raw_name* from *source*.

    The gate on the alias action. An alias is a decision about evidence, so the
    endpoint recording one has to establish that the evidence exists and belongs to
    this team — otherwise the queue's own POST is a general-purpose "record an alias
    for any string I like" endpoint, which is not what it says it is.

    ``location_name`` is stored verbatim, which is the point of keeping the
    carrier's own wording, so there is no normalised column to compare against.
    ``iexact`` covers the case and padding that actually differ between a payload
    and an operator's retyping of it; the diacritic folding
    ``normalize_location_name`` also does is left to the resolver, where it belongs.
    """
    name = normalize_location_name(raw_name)
    if not source or not name:
        return False
    return TrackingEvent.objects.filter(
        team=team,
        provider__code=source,
        location_resolution_status__in=UNMATCHED_STATUSES,
        location_name__iexact=name,
    ).exists()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _map_read(team: Team) -> tuple[MapCoverage, Counter]:
    """LOC-4's coverage for the whole team, and what each location blocks.

    One map read serves both. Building a cheaper count here would be a second
    definition of "plottable", and the two would eventually disagree in front of an
    operator trying to work out why the map is empty.

    The blockage counts every position class, not only the current ones: a
    destination marker that cannot be drawn is the same missing latitude, and fixing
    the location fixes both. ``MapCoverage`` counts containers rather than positions,
    which is why this is derived from the positions themselves.
    """
    positions, coverage = build_map_positions(team, list_visibility_objects(team))
    blocked = Counter(
        position.location.pk for position in positions if position.location is not None and not position.has_coordinates
    )
    return coverage, blocked


def _coordinate_gaps(team: Team, *, blocked: Counter) -> list[CoordinateGap]:
    """The active locations with no coordinates, worst first.

    Three aggregate queries whatever the size of the network: the locations, the
    containers standing on them, and the active shipments routed to them. Nothing
    follows a foreign key per row.
    """
    locations = list(
        ContainerLocation.objects.filter(team=team, is_active=True)
        .filter(MISSING_COORDINATES)
        .select_related("parent_location")
    )
    if not locations:
        return []

    containers = _counts_by_location(
        Container.objects.filter(team=team, current_location__isnull=False)
        .filter(MISSING_COORDINATES_ON_CURRENT_LOCATION)
        .values("current_location")
        .annotate(total=Count("pk")),
        key="current_location",
    )
    shipments = _counts_by_location(
        Shipment.objects.filter(
            team=team,
            status__in=ACTIVE_SHIPMENT_STATUSES,
            destination_location__isnull=False,
        )
        .filter(MISSING_COORDINATES_ON_DESTINATION)
        .values("destination_location")
        .annotate(total=Count("pk")),
        key="destination_location",
    )

    gaps = [
        CoordinateGap(
            location=location,
            container_count=containers.get(location.pk, 0),
            shipment_count=shipments.get(location.pk, 0),
            blocked_positions=blocked.get(location.pk, 0),
        )
        for location in locations
    ]
    gaps.sort(key=lambda gap: gap.impact_key)
    return gaps[:COORDINATE_GAP_LIMIT]


def _counts_by_location(queryset, *, key: str) -> dict[int, int]:
    return {row[key]: row["total"] for row in cast("Iterable[dict]", queryset)}


def _evidence_groups(team: Team) -> list[LocationEvidence]:
    """Aggregate every unmatched place into one row per decision.

    One query. Grouped by provider, reported name, reported code and the resulting
    status — enough to make the row a task, and nothing more: coordinates vary event
    to event, and folding them into the key would split one decision into as many
    rows as the carrier had fixes for the place.

    ``last_seen_at`` is the newest event time in the group, which is how an operator
    tells a live naming problem from one a provider stopped sending months ago.
    """
    rows = cast(
        "Iterable[dict]",
        TrackingEvent.objects.filter(
            NAMED_LOCATION_EVIDENCE,
            team=team,
            location_resolution_status__in=UNMATCHED_STATUSES,
        )
        .values(
            "provider__code",
            "provider__name",
            "location_name",
            "location_unlocode",
            "location_resolution_status",
        )
        .annotate(
            event_count=Count("pk"),
            container_count=Count("container_id", distinct=True),
            last_seen_at=Max("event_datetime"),
        )
        .order_by("-event_count", "location_name", "location_unlocode"),
    )
    groups = [
        LocationEvidence(
            provider_code=row["provider__code"] or "",
            provider_name=row["provider__name"] or "",
            raw_name=row["location_name"],
            raw_unlocode=row["location_unlocode"],
            status=row["location_resolution_status"],
            event_count=row["event_count"],
            container_count=row["container_count"],
            last_seen_at=row["last_seen_at"],
        )
        for row in rows
    ]
    return _with_recorded_aliases(team, groups)


def _with_recorded_aliases(team: Team, groups: list[LocationEvidence]) -> list[LocationEvidence]:
    """Mark the groups an alias has already been recorded for. One query.

    Batched rather than resolved per row: this is a display hint about master data,
    not a resolution, and asking the resolver once per row to produce it would make
    the page's cost grow with the length of the queue.

    Only an alias pointing at an *active* location counts, because only those are in
    the resolver's universe. An alias to a deactivated place will not resolve, and
    saying the work was done would be wrong.
    """
    sources = {group.provider_code for group in groups if group.provider_code}
    names = {normalize_location_name(group.raw_name) for group in groups} - {""}
    if not sources or not names:
        return groups

    recorded = {
        (alias.source, alias.normalized_name): alias.location
        for alias in LocationAlias.objects.filter(
            team=team, source__in=sources, normalized_name__in=names
        ).select_related("location")
        if alias.location.is_active
    }
    return [
        replace(group, alias_location=recorded.get((group.provider_code, normalize_location_name(group.raw_name))))
        for group in groups
    ]


def _with_candidates(team: Team, groups: list[LocationEvidence]) -> list[LocationEvidence]:
    """Attach the resolver's own candidate list to each ambiguous group.

    The resolver is asked again rather than a candidate list being stored on the
    event, because the tie is a function of the *current* master data: two terminals
    that could not be told apart last month may be one hierarchy today, and a stored
    list would still show the old ambiguity.

    Only the name and the code are handed over. The coordinates of some particular
    event are deliberately left out — a coordinate is not an identity, and a group is
    one decision about a *named* place rather than about a fix.

    Asked once per displayed group, which the caller has already capped. An
    unresolved group is never asked: nothing matched it, so there is nothing to show.
    """
    from apps.scm.containers.location_resolver import LocationQuery, resolve_location

    return [
        replace(
            group,
            candidates=list(
                resolve_location(
                    team,
                    LocationQuery(source=group.provider_code, name=group.raw_name, unlocode=group.raw_unlocode),
                ).candidates
            ),
        )
        for group in groups
    ]
