"""What the filter dropdowns offer.

Two rules, and they pull against each other. Offer only what is present, so a
choice can never return an empty list for nothing; but never drop the value that is
currently applied, or the control lies about the state of the page.
"""

from __future__ import annotations

from .issues import ISSUE_LABELS, QueueItem


def issue_choices(items: list[QueueItem], active: str = "") -> list[tuple[str, str]]:
    """The issue types present in *items*, in ISSUE_LABELS order, plus *active*.

    Only what is in the queue: a filter offering "Rolled" when nothing has rolled
    invites a click that can only return an empty list. An unrecognised code is not
    invented into a choice — it still filters, to nothing.
    """
    present = {issue_type for item in items for issue_type in item.issue_types}
    if active in ISSUE_LABELS:
        present.add(active)
    return [(issue_type, str(label)) for issue_type, label in ISSUE_LABELS.items() if issue_type in present]


def text_choices(present: set[str], active: str = "") -> list[str]:
    """Sorted free-text choices — carriers, destinations — with *active* guaranteed.

    A filter can arrive by URL naming something the queue has none of: the Control
    Tower's Delayed card links to ``?issue=delay`` whether or not anything is
    delayed, and a filtered link can be bookmarked and reopened after the state
    behind it cleared. Offering only what is present would drop that value from the
    dropdown, leaving the control reading "Any carrier" while the list was in fact
    filtered to nothing — and the empty state advising a filter be cleared that
    nothing on the page showed. The filter still applies either way; this is about
    the page telling the truth about it.
    """
    choices = sorted(present)
    if active and active not in choices:
        choices = sorted({*choices, active})
    return choices
