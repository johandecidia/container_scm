"""What ``ContainerLocation.parent_location`` means, and every traversal of it.

.. code-block:: text

    Göteborg                     PORT      SEGOT
      └─ Oceanterminalen         TERMINAL  SEGOT
           └─ MCR Yard           DEPOT

The contract
------------

A parent relationship says exactly one thing:

    the child is *contained within*, or belongs operationally within, the parent.

Nothing more. It is not a naming rule, not a routing rule and not a claim about
geography. A terminal inside a port, a yard inside a terminal, an area inside a
depot and a gate inside a depot are all the same relation, which is why the model
does not restrict it by :class:`~apps.scm.containers.choices.LocationType`: MCR's
network is not only ports, and a hierarchy that could only express
``terminal → port`` would be re-invented the first time a warehouse got a yard.

**It is recorded, never inferred.** Similar names, nearby coordinates, a shared
country and a shared UN/LOCODE are all evidence that two places *might* be related,
and none of them is containment. Nothing in this module or anywhere else derives a
parent; an operator sets it, and :mod:`apps.scm.visibility.location_quality` can only
point out where setting one would help.

**It is separate from an alias.** An alias maps a *provider's* name for a place onto
one canonical location; a parent maps one canonical location onto another. Neither
implies the other, and neither is derived from the other — see
:class:`~apps.scm.containers.models.LocationAlias`.

Invariants
----------

* one parent at most, so the structure is a forest of trees rather than a graph;
* acyclic, checked over the whole chain rather than one link (``A → B → C → A`` is
  rejected at the point the third link is written);
* within one team, because a hierarchy is master data a tenant owns;
* bounded depth, so a cycle written straight to the database by an importer cannot
  make a read spin.

Only ``clean()`` enforces them, which is why every writer — the form, the admin, the
services, a shell session — goes through ``full_clean``.

Why the traversals live here
----------------------------

Four places need to know what is inside what: the resolver narrowing candidates that
share a UN/LOCODE, the arrivals queue expanding a port into its terminals, the
Location Workspace drawing the path, and the location form deciding which parents may
be offered. Each writing its own walk is how a port comes to contain a terminal on one
page and not on another.

All of them are level-by-level and bounded. There is no recursive CTE and no tree
library: the adjacency list is two or three levels deep in practice, the bound is
:data:`MAX_DEPTH`, and a walk over already-loaded rows costs no queries at all in the
common case because a candidate's ``parent_location_id`` is already in memory.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.utils.translation import gettext_lazy as _

from .models import ContainerLocation

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from apps.teams.models import Team

# How many levels of containment any walk here will follow. A port inside a port
# inside a port is already past anything the domain describes, so the bound is not a
# limit on legitimate structure — it is what stops corrupt data (a cycle written
# straight to the database) from turning a read into an infinite loop.
MAX_DEPTH = 10


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_parent(location: ContainerLocation) -> None:
    """Raise :class:`ValidationError` unless *location*'s parent is a legal one.

    Called from :meth:`ContainerLocation.clean`, so it holds for every writer rather
    than for the one form somebody remembered to add a check to.

    Four refusals:

    *Itself.* A place is not inside itself, and the row would be invisible to every
    query that starts at a root.

    *A cycle, at any distance.* ``A → B`` and ``B → A`` are each individually
    harmless; together they detach both from every root. The same is true of
    ``A → B → C → A``, which is why the whole chain above the proposed parent is
    walked rather than just the one link being written.

    *Another team's location.* A hierarchy is master data a tenant owns. A foreign
    parent would let one team's edit change what another team's resolver decides.

    *Depth beyond* :data:`MAX_DEPTH`. Reached only by data no rule here produced;
    refusing it keeps every read below bounded.

    An **inactive** parent is deliberately *not* refused. Deactivating a place says
    "stop routing new evidence here"; it does not move the things inside it somewhere
    else, and rejecting the relationship would mean an unrelated edit to a child
    started failing the day somebody retired its parent. The location form declines
    to *offer* an inactive parent, which is where that belongs — see
    :func:`parent_options`.
    """
    parent_id = location.parent_location_id
    if parent_id is None:
        return

    if location.pk is not None and parent_id == location.pk:
        raise ValidationError({"parent_location": _("A location cannot be its own parent.")})

    parent = location.parent_location
    if parent is not None and location.team_id and parent.team_id != location.team_id:
        raise ValidationError({"parent_location": _("The parent location must belong to the same team.")})

    seen = {location.pk} if location.pk is not None else set()
    current = parent
    for _step in range(MAX_DEPTH):
        if current is None:
            return
        if current.pk in seen:
            raise ValidationError({"parent_location": _("That would make the location hierarchy circular.")})
        seen.add(current.pk)
        current = current.parent_location
    raise ValidationError({"parent_location": _("The location hierarchy is nested too deeply.")})


# ---------------------------------------------------------------------------
# Reading the structure
# ---------------------------------------------------------------------------


def ancestor_chain(team: Team, location: ContainerLocation) -> list[ContainerLocation]:
    """Every location above *location*, outermost first.

    ``[Göteborg, Oceanterminalen]`` for MCR Yard, which is the path a breadcrumb
    reads. One query per level and at most :data:`MAX_DEPTH` of them — for the two
    and three level structures the domain actually has, one or two.

    Team-scoped at every step. Cross-team parents are rejected on write, so this is
    belt and braces against data that predates the rule rather than an expected path;
    what it guarantees is that no page can render another tenant's place as context
    for one of ours.
    """
    chain: list[ContainerLocation] = []
    seen: set[int] = {location.pk}
    parent_id = location.parent_location_id
    for _step in range(MAX_DEPTH):
        if parent_id is None or parent_id in seen:
            break
        parent = ContainerLocation.objects.filter(team=team, pk=parent_id).first()
        if parent is None:
            break
        chain.append(parent)
        seen.add(parent.pk)
        parent_id = parent.parent_location_id
    chain.reverse()
    return chain


def descendant_ids(team: Team, location: ContainerLocation) -> list[int]:
    """*location*'s id together with every location beneath it.

    What "expected at Göteborg" has to mean: a shipment bound for Oceanterminalen is
    arriving at the port that contains it, and a port whose terminals were invisible
    to it would under-report its own arrivals. This is not inference — the containment
    is a relation MCR recorded itself.

    Walked level by level: one query per level of the subtree, not one per location,
    so a port with forty terminals costs the same as a port with two.
    """
    ids = [location.pk]
    frontier = [location.pk]
    for _level in range(MAX_DEPTH):
        children = list(
            ContainerLocation.objects.filter(team=team, parent_location_id__in=frontier)
            .exclude(pk__in=ids)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        ids.extend(children)
        frontier = children
    return ids


def contained_ids(locations: Iterable[ContainerLocation], container_ids: set[int], *, team: Team) -> set[int]:
    """Which of *locations* sit inside one of *container_ids*.

    The primitive the resolver narrows with: given the locations that all carry
    ``SEGOT``, which of them are inside another one of them. Returns the *inner* ids,
    so what is left is the set nothing else in the group contains.

    Bulk rather than per location. The first level is free — a loaded row already has
    its ``parent_location_id`` — and each level after that is one query for the whole
    set. In the case this exists for, a port with its terminals, every candidate's
    parent is already in ``container_ids`` and the answer costs no queries at all.

    Compare with the old shape of this: following ``location.parent_location`` per
    candidate meant a query per level *per candidate*, which is the N+1 a queue
    rendering twenty-five groups would multiply by twenty-five.
    """
    if not container_ids:
        return set()

    inside: set[int] = set()
    # location id -> the id of the ancestor currently being examined.
    pending = {
        location.pk: location.parent_location_id
        for location in locations
        if location.parent_location_id is not None and location.pk is not None
    }
    for _level in range(MAX_DEPTH):
        if not pending:
            break
        inside.update(pk for pk, ancestor in pending.items() if ancestor in container_ids)
        pending = {pk: ancestor for pk, ancestor in pending.items() if ancestor not in container_ids}
        if not pending:
            break
        parents = dict(
            ContainerLocation.objects.filter(team=team, pk__in=set(pending.values())).values_list(
                "pk", "parent_location_id"
            )
        )
        pending = {
            pk: parents[ancestor]
            for pk, ancestor in pending.items()
            if parents.get(ancestor) is not None and parents[ancestor] != pk
        }
    return inside


def is_contained_in(location: ContainerLocation, container_ids: set[int], *, team: Team) -> bool:
    """True when *location* sits inside one of *container_ids*."""
    return location.pk in contained_ids([location], container_ids, team=team)


def children_with_counts(team: Team, location: ContainerLocation) -> QuerySet[ContainerLocation]:
    """The locations immediately inside *location*, with their inventory counts.

    One query, annotated rather than followed per row: a port with forty terminals
    must not cost forty counts. Inactive children are included — a retired terminal
    inside a live port is still part of the structure, and hiding it would make the
    port look like it has fewer places in it than it does.
    """
    return (
        ContainerLocation.objects.filter(team=team, parent_location=location)
        .annotate(container_count=Count("containers"))
        .order_by("name")
    )


# ---------------------------------------------------------------------------
# Choosing a parent
# ---------------------------------------------------------------------------


def parent_options(team: Team, location: ContainerLocation | None = None) -> QuerySet[ContainerLocation]:
    """The locations that may legally be offered as *location*'s parent.

    Scoped on the queryset rather than checked afterwards, so an illegal parent is
    neither shown in the selector nor accepted when posted:

    * this team's locations only;
    * not *location* itself;
    * not anything inside *location*, which would be a cycle — the form refusing it
      up front is better than ``clean()`` explaining it after a round trip;
    * active locations only, because an inactive place is not somewhere new evidence
      should be routed and offering it invites exactly that.

    With one exception, and it matters: whatever is *already* recorded as the parent
    stays in the list even when it has since been deactivated. A ``ModelChoiceField``
    whose queryset excludes its own value renders as unset and saves as unset, so
    leaving it out would silently delete a relationship somebody meant to keep.
    """
    queryset = ContainerLocation.objects.filter(team=team)
    keep_current = Q(pk__in=[])
    if location is not None and location.pk is not None:
        queryset = queryset.exclude(pk__in=descendant_ids(team, location))
        if location.parent_location_id is not None:
            keep_current = Q(pk=location.parent_location_id)
    return queryset.filter(Q(is_active=True) | keep_current).select_related("parent_location").order_by("name")
