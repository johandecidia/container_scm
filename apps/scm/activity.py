"""The shape of an activity entry, shared by every SCM workspace that has one.

A workspace's Activity tab answers "what has this platform done with, and learned
about, this object" — as distinct from what happened to the physical thing, which
is the Journey tab's subject. Containers, purchase orders and anything that follows
answer it from different records, but they answer it in the same shape, and
``scm/components/activity_list.html`` renders that shape.

This module holds the contract and nothing else. It has no queries and knows about
no models: each app derives its own entries from records that already exist for
their own reasons, and none of them writes an activity table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise


@dataclass(frozen=True)
class ActivityEntry:
    """One thing that happened to an object's record.

    ``kind`` selects the icon and nothing else — it is presentation, not a taxonomy
    anything depends on. ``actor`` is a person where we know of one and a system
    where we do not, and the two are never conflated.
    """

    occurred_at: datetime
    kind: str
    title: StrOrPromise
    detail: str = ""
    actor: str = ""
    url: str = ""
