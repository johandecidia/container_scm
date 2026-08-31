"""The operational work queues: what needs attention, and what is arriving.

Both queues are compositions over :func:`~apps.scm.visibility.selectors.list_visibility_objects`.
Nothing here decides that something is wrong, that a date has moved, or that a box
is late: the exception engine and the delay engine remain the only things that
decide those, and this package turns their findings into rows, bands, groups and a
sort order.

**Why there is no severity.** The domain has no SLA, no cargo value and no impact
model, so ranking a customs hold "high" against port congestion "medium" would be
a judgment the platform has never made and cannot defend. What the code *can*
distinguish is where a finding came from, and that turns out to be the useful
split:

* ``EXCEPTION`` — a carrier reported an event: a hold, a rollover, congestion.
* ``DELAY`` — a date moved, or an arrival is missing. The delay engine's verdict.
* ``TRACKING`` — no carrier event for days. Derived from the *absence* of data,
  which is a different kind of claim from the two above and belongs below them.

That ordering is the existing attention queue's rule — exceptions before delays —
with the tracking gap separated out from the exceptions it was previously mixed
into. See :attr:`~apps.scm.visibility.selectors.VisibilityOverview.needs_attention`.

**No workflow state.** There is no acknowledge, assign, resolve, snooze or owner,
because none of those exist in the database. A queue is a view of current supply
chain state, so an item leaves it when the state changes and not before.

The package is laid out along the seam the two queues already had:

* :mod:`.issues` — the shared vocabulary. What an issue is, what to call it, and
  which band it belongs to. Both queues read it; neither owns it.
* :mod:`.choices` — what the filter dropdowns offer.
* :mod:`.exceptions` — the exceptions queue.
* :mod:`.arrivals` — the arrivals queue.

Everything the views and templates use is re-exported here, so the import path is
``from .work_queues import get_exception_queue`` as it was when this was one module.
"""

from __future__ import annotations

from .arrivals import (
    ARRIVAL_WINDOWS,
    DEFAULT_ARRIVAL_WINDOW,
    ArrivalGroup,
    ArrivalQueue,
    ArrivalQueueFilters,
    get_arrival_queue,
    parse_arrival_queue_filters,
)
from .exceptions import (
    ExceptionQueue,
    ExceptionQueueFilters,
    get_exception_queue,
    parse_exception_queue_filters,
)
from .issues import DELAY_ISSUE, ISSUE_LABELS, IssueBand, QueueIssue, QueueItem

__all__ = [
    "ARRIVAL_WINDOWS",
    "DEFAULT_ARRIVAL_WINDOW",
    "DELAY_ISSUE",
    "ISSUE_LABELS",
    "ArrivalGroup",
    "ArrivalQueue",
    "ArrivalQueueFilters",
    "ExceptionQueue",
    "ExceptionQueueFilters",
    "IssueBand",
    "QueueIssue",
    "QueueItem",
    "get_arrival_queue",
    "get_exception_queue",
    "parse_arrival_queue_filters",
    "parse_exception_queue_filters",
]
