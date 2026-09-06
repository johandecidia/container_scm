"""The one place that turns an external location into a canonical one.

.. code-block:: text

    what a provider reported          what MCR believes
    ------------------------          -----------------
    source, code, name,       ──▶     ContainerLocation
    unlocode, lat/lon,                    + how we got there
    country, city

Every carrier adapter produces a location as free text and coordinates. Exactly one
module is allowed to decide what that text means, and this is it. A place-name rule
living in the Traqo client would be invisible to Maersk, would be re-invented
slightly differently for CMA CGM, and would make "why did this resolve to
Oceanterminalen" a question with nine possible answers.

So the resolver takes a :class:`LocationQuery` — a plain description of an external
location, with no provider's response schema anywhere in it — and returns a
:class:`LocationResolution` that always says how it decided.

Refusing to answer is a feature
-------------------------------

The expensive failure in this domain is not an unresolved location; it is a
*confidently wrong* one. A shipment credited to Oceanterminalen because a carrier
said "Gothenburg" puts boxes on the wrong depot's arrivals list, and nothing in the
data will contradict it.

So there is no fuzzy matching here — no edit distance, no scoring, no "closest
match". Every rule is an exact comparison over a canonicalised value. Where the
evidence fits several canonical locations equally well, the answer is
``AMBIGUOUS``, which names the problem an operator can fix (record an alias) rather
than hiding it behind a guess.

The one place plurality is *not* ambiguity is a hierarchy. Several locations sharing
``SEGOT`` where one contains the others is not a tie — it is a port with terminals
inside it — and the code resolves to the port. See :func:`_narrow_to_outermost`.

Precision follows the evidence, in both directions
--------------------------------------------------

A hierarchy makes two mistakes possible, and they are opposites.

*Overstating.* ``SEGOT`` is a claim about Göteborg; it says nothing about which berth.
Resolving it to Oceanterminalen would turn port-level evidence into terminal-level
certainty, and a shipment would be credited to a terminal no carrier named.

*Understating.* A carrier that names ``Oceanterminalen`` *and* sends ``SEGOT`` has
identified the terminal. Answering "Göteborg" because the code was checked first
throws away the more specific of two pieces of agreeing evidence, and adding a coarse
code to a query would make the answer worse.

So the rules are ordered by how narrow the *evidence* is, not by which column it came
from, and :func:`_by_name_within_unlocode` sits between the aliases and the bare code:
a name that identifies exactly one place inside what the code names resolves to that
place. Where the name and the code cannot be reconciled at all — the name is one of
our locations, the code is one of our locations, and neither contains the other — the
answer is ``AMBIGUOUS``, for the same reason two contradicting aliases are: letting
one of them win would make the result depend on this module's ordering rather than on
the master data.

Nothing here writes
-------------------

The resolver never creates a ``ContainerLocation`` and never creates an alias.
Canonical locations are master data an operator owns; a read of a carrier response
that quietly minted one would mean the location list grew every time a carrier
changed its spelling, and no one would be able to say which rows were deliberate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import Q

from .choices import LocationResolutionMethod, LocationResolutionStatus
from .location_hierarchy import contained_ids
from .location_identity import (
    distance_km,
    normalize_alias_source,
    normalize_country_code,
    normalize_external_code,
    normalize_location_name,
    normalize_unlocode,
)
from .models import ContainerLocation, LocationAlias

if TYPE_CHECKING:
    from apps.teams.models import Team

# How far apart two points may be and still be treated as the same place, when
# nothing better than coordinates is available. Deliberately small: terminals inside
# one port sit a couple of kilometres apart, and a radius that swallows several of
# them turns a fallback into a coin toss. Widening it does not make the resolver
# guess — it makes it report AMBIGUOUS more often, which is the intended failure.
_DEFAULT_COORDINATE_RADIUS_KM = 5.0


def coordinate_radius_km() -> float:
    """The configured coordinate-match radius, in kilometres."""
    return float(getattr(settings, "SCM_LOCATION_COORDINATE_RADIUS_KM", _DEFAULT_COORDINATE_RADIUS_KM))


@dataclass(frozen=True)
class LocationQuery:
    """An external location, described in terms no provider owns.

    Built by whatever is holding a provider's data — the tracking ingestion path
    builds one from a ``NormalisedTrackingEvent`` — so the resolver never imports a
    carrier schema and a new provider needs no change here.

    Every field is optional because no provider sends them all. A query carrying
    only a name is normal; so is one carrying only coordinates.
    """

    source: str = ""
    external_code: str = ""
    name: str = ""
    unlocode: str = ""
    # Decimal from a carrier event, whose coordinates match the model's columns;
    # float from anything hand-built. `distance_km` floats whatever it is given, so
    # both are accepted rather than one being converted at the boundary and losing
    # the precision the other kept.
    latitude: Decimal | float | None = None
    longitude: Decimal | float | None = None
    country_code: str = ""
    city: str = ""

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to resolve against.

        A carrier event with no location at all is ordinary — a booking
        confirmation names no place — and must cost no queries.
        """
        return not any(
            (
                normalize_external_code(self.external_code),
                normalize_location_name(self.name),
                normalize_unlocode(self.unlocode),
                self.latitude is not None and self.longitude is not None,
            )
        )


@dataclass(frozen=True)
class LocationResolution:
    """What the resolver concluded, and how.

    Structured rather than a nullable location because "no location" and "several
    locations" are different findings that need different work, and a bare ``None``
    cannot tell them apart. ``candidates`` carries the tie for an ambiguous result,
    so the UI can show an operator exactly which places need separating.

    There is no confidence score. The method already says why the answer is
    believable, and a number between an explicit alias and a coordinate match would
    be invented precision.
    """

    status: str = LocationResolutionStatus.UNRESOLVED
    method: str = LocationResolutionMethod.NONE
    location: ContainerLocation | None = None
    candidates: list[ContainerLocation] = field(default_factory=list)

    @property
    def is_resolved(self) -> bool:
        return self.status == LocationResolutionStatus.RESOLVED and self.location is not None

    @property
    def is_ambiguous(self) -> bool:
        return self.status == LocationResolutionStatus.AMBIGUOUS

    @property
    def location_id(self) -> int | None:
        return self.location.pk if self.location is not None else None


UNRESOLVED = LocationResolution()


def resolve_location(team: Team, query: LocationQuery) -> LocationResolution:
    """Resolve *query* to one of *team*'s canonical locations.

    Rules are tried in order and the first one that produces an answer wins. Each is
    strictly narrower evidence than the one below it:

    1. ``ALIAS`` — an alias this source recorded for this *name*. Somebody decided
       this, so it beats everything derived.
    2. ``EXTERNAL_CODE`` — an alias this source recorded for this *code*.
    3. ``NAME`` within ``UNLOCODE`` — a name identifying exactly one canonical
       location inside what the code names. Two agreeing pieces of evidence, and the
       name is the narrower of them.
    4. ``UNLOCODE`` — canonical locations carrying the code, narrowed to the
       outermost one when they form a hierarchy.
    5. ``COORDINATES`` — exactly one canonical location within the configured
       radius.
    6. ``NAME`` — canonical locations whose own normalised name matches, optionally
       narrowed by country and city.
    7. ``UNRESOLVED``.

    Steps 1 and 2 are both explicit aliases and are checked against each other: if a
    source's name alias and its code alias point at *different* canonical locations,
    that is a contradiction in the master data, and the result is ``AMBIGUOUS``
    rather than whichever of the two happens to be tried first. Silently preferring
    one would make the answer depend on this function's ordering instead of on what
    an operator actually recorded.

    Concretely, for Göteborg (``SEGOT``, ``PORT``) containing Oceanterminalen
    (``SEGOT``, ``TERMINAL``):

    .. code-block:: text

        unlocode=SEGOT                        ──▶  Göteborg      (step 4, outermost)
        name=Oceanterminalen, unlocode=SEGOT  ──▶  Oceanterminalen (step 3)
        name=Oceanterminalen                  ──▶  Oceanterminalen (step 6)
        name=Göteborg, unlocode=SEGOT         ──▶  Göteborg      (step 3)

    and where the two places share the code but *no* containment is recorded, a bare
    ``SEGOT`` is ``AMBIGUOUS`` — which is the row LOC-5's queue turns into "review the
    hierarchy".

    Only ``is_active`` locations are considered as answers. Deactivating a location
    says "stop routing new evidence here", and it is the aliases and codes that get
    reused when a place is replaced. Containment itself is read regardless of active
    state: retiring a terminal does not move the yard inside it out of the port.

    Never writes. An unresolvable location is a fact about the master data, not an
    invitation to create some.
    """
    if query.is_empty:
        return UNRESOLVED

    for rule in (_by_alias_and_code, _by_name_within_unlocode, _by_unlocode, _by_coordinates, _by_name):
        resolution = rule(team, query)
        if resolution is not None:
            return resolution
    return UNRESOLVED


# ---------------------------------------------------------------------------
# Rules. Each returns None to mean "this rule has nothing to say", which passes
# the question down; a LocationResolution — resolved or ambiguous — stops the chain.
# ---------------------------------------------------------------------------


def _by_alias_and_code(team: Team, query: LocationQuery) -> LocationResolution | None:
    """Steps 1 and 2: what this source has explicitly been told a place is."""
    source = normalize_alias_source(query.source)
    if not source:
        return None

    name = normalize_location_name(query.name)
    code = normalize_external_code(query.external_code)
    if not name and not code:
        return None

    # One query for both lookups: an alias row carries the code and the name, so
    # asking for either and sorting them out here costs one round trip, not two.
    aliases = list(
        LocationAlias.objects.filter(team=team, source=source)
        .filter(_alias_filter(name, code))
        .select_related("location")
    )
    by_name = next((alias for alias in aliases if name and alias.normalized_name == name), None)
    by_code = next((alias for alias in aliases if code and alias.external_code == code), None)

    if by_name is not None and by_code is not None and by_name.location_id != by_code.location_id:
        # The master data disagrees with itself. Report the tie instead of letting
        # the order of these two lines decide it.
        return LocationResolution(
            status=LocationResolutionStatus.AMBIGUOUS,
            method=LocationResolutionMethod.ALIAS,
            candidates=_active([by_name.location, by_code.location]),
        )

    if by_name is not None and by_name.location.is_active:
        return LocationResolution(
            status=LocationResolutionStatus.RESOLVED,
            method=LocationResolutionMethod.ALIAS,
            location=by_name.location,
        )
    if by_code is not None and by_code.location.is_active:
        return LocationResolution(
            status=LocationResolutionStatus.RESOLVED,
            method=LocationResolutionMethod.EXTERNAL_CODE,
            location=by_code.location,
        )
    return None


def _alias_filter(name: str, code: str) -> Q:
    """A Q matching an alias by either identifier, whichever the query supplied."""
    matches = Q(pk__in=[])
    if name:
        matches |= Q(normalized_name=name)
    if code:
        matches |= Q(external_code=code)
    return matches


def _by_name_within_unlocode(team: Team, query: LocationQuery) -> LocationResolution | None:
    """Step 3: a name that says *which* place inside the one the code names.

    Only fires when the query carries both identifiers, which is the case a hierarchy
    makes interesting:

    .. code-block:: text

        Göteborg           SEGOT      name=Oceanterminalen  ──▶  Oceanterminalen
          └─ Oceanterminalen  SEGOT   unlocode=SEGOT

    The code establishes the port; the name establishes the place inside it. Both
    agree, and the answer is the narrower of the two — otherwise a carrier that told
    us the terminal would be recorded as having told us only the city, and the extra
    precision it sent would be discarded because the code happened to be checked
    first.

    "Inside" means what :mod:`.location_hierarchy` says it means: the named location
    either carries the code itself or sits beneath something that does. A name whose
    place has no such relation to the code is *not* narrowed into it — that is the
    inference this module exists to refuse.

    Three outcomes:

    *One consistent place* — resolved to it, by ``NAME``, because the name is what
    discriminated.

    *Several consistent places* — no answer here. Two locations of the same name
    inside one port is a naming problem, and the code still answers correctly at the
    level it names, so the question passes down to step 4.

    *No consistent place, while the name does match locations of ours* — the two
    identifiers describe unrelated places, and neither is allowed to win. ``AMBIGUOUS``
    over both sets, so LOC-5's queue shows exactly which places need separating or
    relating. Deciding this by rule order would be the silent, confidently-wrong
    answer: a carrier's ``SEGOT`` would quietly overrule the name of a place it has
    nothing to do with.
    """
    unlocode = normalize_unlocode(query.unlocode)
    if not unlocode or not normalize_location_name(query.name):
        return None

    named = _narrow_to_outermost(_by_name_candidates(team, query), team=team)
    if not named:
        return None

    coded = _by_unlocode_candidates(team, unlocode)
    if not coded:
        # The code names nothing of ours, so there is nothing to be inside of. The
        # name rule further down answers this on its own terms.
        return None

    coded_ids = {location.pk for location in coded}
    inside = contained_ids(named, coded_ids, team=team)
    consistent = [location for location in named if location.pk in coded_ids or location.pk in inside]

    if len(consistent) == 1:
        return LocationResolution(
            status=LocationResolutionStatus.RESOLVED,
            method=LocationResolutionMethod.NAME,
            location=consistent[0],
        )
    if consistent:
        return None
    return LocationResolution(
        status=LocationResolutionStatus.AMBIGUOUS,
        method=LocationResolutionMethod.UNLOCODE,
        candidates=[*_narrow_to_outermost(coded, team=team), *named],
    )


def _by_unlocode(team: Team, query: LocationQuery) -> LocationResolution | None:
    """Step 4: the UN/LOCODE register's answer, at the level it actually names.

    A code names a port, not a berth. Where several of this team's locations carry
    it and one contains the others, the code resolves to the container — Göteborg,
    not Oceanterminalen — because that is the only claim the code supports. Where
    the locations carrying it are unrelated, the code cannot choose between them.
    """
    unlocode = normalize_unlocode(query.unlocode)
    if not unlocode:
        return None

    return _decide(team, _by_unlocode_candidates(team, unlocode), LocationResolutionMethod.UNLOCODE)


def _by_unlocode_candidates(team: Team, unlocode: str) -> list[ContainerLocation]:
    """This team's active locations carrying *unlocode*, in one query."""
    return list(ContainerLocation.objects.filter(team=team, is_active=True, unlocode=unlocode))


def _by_coordinates(team: Team, query: LocationQuery) -> LocationResolution | None:
    """Step 5: one canonical location close enough to be the same place.

    A fallback, not an identity system. Distance is computed in application code
    over the locations that have coordinates at all — usually a small handful, since
    coordinates are optional and mostly unset — so no PostGIS and no bounding-box
    arithmetic in SQL.

    A hierarchy is *not* collapsed here the way it is for a UN/LOCODE: a point five
    kilometres from both a port and one of its terminals is not evidence about which,
    and the code says so rather than defaulting to the larger place.
    """
    if query.latitude is None or query.longitude is None:
        return None

    radius = coordinate_radius_km()
    within = []
    for location in ContainerLocation.objects.filter(
        team=team, is_active=True, latitude__isnull=False, longitude__isnull=False
    ):
        distance = distance_km(query.latitude, query.longitude, location.latitude, location.longitude)
        if distance is not None and distance <= radius:
            within.append(location)

    if not within:
        return None
    if len(within) > 1:
        return LocationResolution(
            status=LocationResolutionStatus.AMBIGUOUS,
            method=LocationResolutionMethod.COORDINATES,
            candidates=within,
        )
    return LocationResolution(
        status=LocationResolutionStatus.RESOLVED,
        method=LocationResolutionMethod.COORDINATES,
        location=within[0],
    )


def _by_name(team: Team, query: LocationQuery) -> LocationResolution | None:
    """Step 6: this team's own name for the place, matched exactly once normalised.

    Case, whitespace and diacritics are already folded, so "GOTHENBURG",
    " Gothenburg " and "Göteborg" all arrive here as one string. Nothing beyond
    that: an English exonym or a "GOTHENBURG, SE" suffix is a mapping somebody has
    to record, and this rule will not infer it.

    Country and city narrow the candidates when the query carries them and the
    stored rows do too, which is what stops a warehouse named "Central" in Sweden
    answering for one in Vietnam.
    """
    if not normalize_location_name(query.name):
        return None
    return _decide(team, _by_name_candidates(team, query), LocationResolutionMethod.NAME)


def _by_name_candidates(team: Team, query: LocationQuery) -> list[ContainerLocation]:
    """The locations whose own name matches the query's, narrowed by country and city.

    Shared with step 3, so "which of our places is called this" has one answer
    wherever the question is asked. Hierarchy narrowing is deliberately *not* done
    here: step 3 needs the un-narrowed set to compare against the code, and applies
    it itself.
    """
    name = normalize_location_name(query.name)
    if not name:
        return []
    candidates = list(ContainerLocation.objects.filter(team=team, is_active=True, normalized_name=name))
    if len(candidates) > 1:
        candidates = _narrow_by_place(candidates, query)
    return candidates


# ---------------------------------------------------------------------------
# Shared narrowing
# ---------------------------------------------------------------------------


def _decide(team: Team, candidates: list[ContainerLocation], method: str) -> LocationResolution | None:
    """Turn a candidate set into a resolution, collapsing hierarchies first."""
    if not candidates:
        return None

    outermost = _narrow_to_outermost(candidates, team=team)
    if len(outermost) == 1:
        return LocationResolution(
            status=LocationResolutionStatus.RESOLVED,
            method=method,
            location=outermost[0],
        )
    return LocationResolution(
        status=LocationResolutionStatus.AMBIGUOUS,
        method=method,
        candidates=outermost,
    )


def _narrow_to_outermost(candidates: list[ContainerLocation], *, team: Team) -> list[ContainerLocation]:
    """Drop any candidate that sits inside another candidate.

    Göteborg (PORT, SEGOT) and Oceanterminalen (DEPOT, SEGOT, parent Göteborg) both
    match ``SEGOT``, but they are not competing answers — the second is part of the
    first, and the code names the first. Dropping the descendant leaves one answer.

    Two *unrelated* locations sharing a code survive this untouched and go on to be
    reported as ambiguous, which is the honest result — and the one LOC-5 offers to
    fix by recording the containment.

    The containment itself is :func:`~apps.scm.containers.location_hierarchy.contained_ids`,
    which walks the whole set a level at a time instead of following each candidate's
    ``parent_location`` in turn. For the structure this exists for — a port whose
    terminals carry its code — the parents are already among the candidates and the
    answer costs no queries at all.
    """
    if len(candidates) < 2:
        return candidates

    ids = {location.pk for location in candidates}
    inside = contained_ids(candidates, ids, team=team)
    return [location for location in candidates if location.pk not in inside]


def _narrow_by_place(candidates: list[ContainerLocation], query: LocationQuery) -> list[ContainerLocation]:
    """Keep only the candidates whose country and city agree with the query.

    Applied one field at a time and only where it actually discriminates: a filter
    that would empty the set is dropped, because a location with no recorded country
    is not evidence that it is in a *different* country.
    """
    country = normalize_country_code(query.country_code)
    if country:
        narrowed = [location for location in candidates if location.country_code == country]
        if narrowed:
            candidates = narrowed

    city = normalize_location_name(query.city)
    if city:
        narrowed = [location for location in candidates if normalize_location_name(location.city) == city]
        if narrowed:
            candidates = narrowed

    return candidates


def _active(locations: list[ContainerLocation]) -> list[ContainerLocation]:
    return [location for location in locations if location.is_active]
