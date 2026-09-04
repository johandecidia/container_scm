"""What an issue is, what to call it, and which band it belongs to.

The vocabulary both queues read and neither owns. See the package docstring for why
bands exist and severity does not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from ..read_models import VisibilityObject

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

# The delay engine's finding, given an issue type so it can sit in one list beside
# the exception engine's. It is not an exception code and the engine never emits it.
DELAY_ISSUE = "delay"


class IssueBand(TextChoices):
    """How much of a claim an issue is, by where the finding came from.

    Not a severity. See the package docstring: this is the one ranking the domain
    can actually justify.
    """

    EXCEPTION = "exception", _("Exception")
    DELAY = "delay", _("Delay")
    TRACKING = "tracking", _("Tracking")


# Every issue type a queue can show, with the words to show it in. Keys are the
# exception engine's own codes plus DELAY_ISSUE — this table maps them, it does not
# add to them, and a code the engine stops emitting simply stops appearing.
ISSUE_LABELS: dict[str, StrOrPromise] = {
    "customs_hold": _("Customs hold"),
    "rolled": _("Rolled"),
    "port_congestion": _("Port congestion"),
    DELAY_ISSUE: _("Delayed"),
    "missing_event": _("Tracking stale"),
}

_BAND_BY_ISSUE: dict[str, str] = {
    "customs_hold": IssueBand.EXCEPTION,
    "rolled": IssueBand.EXCEPTION,
    "port_congestion": IssueBand.EXCEPTION,
    DELAY_ISSUE: IssueBand.DELAY,
    "missing_event": IssueBand.TRACKING,
}

BAND_ORDER: dict[str, int] = {
    IssueBand.EXCEPTION: 0,
    IssueBand.DELAY: 1,
    IssueBand.TRACKING: 2,
}

# Far enough ahead that an item with no ETA sorts last without needing a branch.
_NO_ETA = date.max


@dataclass(frozen=True)
class QueueIssue:
    """One thing wrong with one object, in words, with where it came from."""

    issue_type: str
    detail: str

    @property
    def label(self) -> str:
        return str(ISSUE_LABELS.get(self.issue_type, self.issue_type.replace("_", " ").title()))

    @property
    def band(self) -> str:
        # An exception code this table has not been told about is still an
        # exception: the carrier reported something. Better to show it in the top
        # band than to hide a finding because the label table is out of date.
        return _BAND_BY_ISSUE.get(self.issue_type, IssueBand.EXCEPTION)

    @property
    def band_label(self) -> str:
        return str(IssueBand(self.band).label)


@dataclass
class QueueItem:
    """One row of a work queue: an object, and why it is in the queue.

    An object appears once however many things are wrong with it. ``primary_issue``
    is the one the row leads with — the highest band — and the rest stay visible as
    ``other_issues`` rather than being dropped, because a box that is both held and
    stale is one thing to work with two facts about it.
    """

    object: VisibilityObject
    issues: list[QueueIssue] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.object.key

    @property
    def primary_issue(self) -> QueueIssue | None:
        return self.issues[0] if self.issues else None

    @property
    def other_issues(self) -> list[QueueIssue]:
        return self.issues[1:]

    @property
    def issue_types(self) -> list[str]:
        return [issue.issue_type for issue in self.issues]

    @property
    def band(self) -> str:
        primary = self.primary_issue
        return primary.band if primary else IssueBand.TRACKING

    @property
    def sort_key(self) -> tuple:
        """Band first, then the soonest arrival, then the label.

        Soonest ETA second because that is what makes a queue actionable: a held box
        arriving on Thursday has to be worked before one arriving in three weeks.
        Items with no ETA sort last within their band rather than first.
        """
        return (
            BAND_ORDER.get(self.band, len(BAND_ORDER)),
            self.object.current_eta or _NO_ETA,
            self.object.label,
        )


def build_queue_item(obj: VisibilityObject) -> QueueItem:
    """Turn one object's exception and delay findings into a queue row.

    Both engines are read, neither is re-implemented. The delay is only listed when
    the delay engine says so, and its wording is the engine's own reason.
    """
    issues = [QueueIssue(issue_type=issue.exception_type, detail=issue.detail) for issue in obj.exception_issues]
    if obj.is_delayed:
        issues.append(QueueIssue(issue_type=DELAY_ISSUE, detail=_delay_detail(obj)))
    issues.sort(key=lambda issue: BAND_ORDER.get(issue.band, len(BAND_ORDER)))
    return QueueItem(object=obj, issues=issues)


def _delay_detail(obj: VisibilityObject) -> str:
    """The delay engine's verdict, with the drift it measured where there is one."""
    reason = obj.delay_reason
    if obj.delay_days > 0 and reason:
        return f"{reason} · +{obj.delay_days}d"
    return reason
