"""Hand-entry writes for the purchase order data SCM owns.

Business Central is master for the orders it sources, lines included, so every
function here refuses a BC-owned record before touching it. The rule lives in this
module rather than only in the views that happen to call it: editing a BC line in
SCM is not a change, it is a change that the next sync silently reverts, and a
writer reaching these functions from the admin or a shell must hit the same wall as
one coming from a form.

Kept out of ``services.py`` on purpose — that module is the sync and fulfillment
engine, and these are the hand-entry writes. Creating and deleting a manual *order*
predates this module and still lives there as ``create_purchase_order`` and
``delete_purchase_order``; both already carry the same BC refusal.
"""

from __future__ import annotations

import uuid
from typing import Any

from django.core.exceptions import PermissionDenied
from django.db import transaction

from apps.teams.models import Team

from .models import PurchaseOrder, PurchaseOrderLine


def _refuse_business_central(purchase_order: PurchaseOrder) -> None:
    if purchase_order.is_business_central:
        raise PermissionDenied(f"Purchase order {purchase_order.po_number} is managed by Business Central.")


def create_purchase_order_line(*, team: Team, purchase_order: PurchaseOrder, **fields: Any) -> PurchaseOrderLine:
    """Add a hand-entered line to an SCM-owned purchase order.

    ``external_id`` is generated rather than asked for. It exists to key the source
    system's own identifier and a manual line has none, but the column is part of the
    order's uniqueness constraint, so it gets a value nothing will collide with.

    Raises:
        PermissionDenied: if the order is owned by Business Central.
    """
    _refuse_business_central(purchase_order)
    with transaction.atomic():
        return PurchaseOrderLine.objects.create(
            team=team,
            purchase_order=purchase_order,
            external_id=f"manual-{uuid.uuid4().hex}",
            **fields,
        )


def update_purchase_order_line(*, line: PurchaseOrderLine, **fields: Any) -> PurchaseOrderLine:
    """Update an SCM-owned purchase order line in place.

    Raises:
        PermissionDenied: if the parent order is owned by Business Central.
    """
    _refuse_business_central(line.purchase_order)
    with transaction.atomic():
        for attr, value in fields.items():
            setattr(line, attr, value)
        line.save(update_fields=[*fields.keys(), "updated_at"])
    return line


def delete_purchase_order_line(*, line: PurchaseOrderLine) -> None:
    """Delete an SCM-owned purchase order line and the links hanging off it.

    The line's container links go with it, which is the right cascade: a link means
    "this line acquired that box" and the line is being removed. Neither the
    containers nor any other line is touched. Delivery lines cascade the same way,
    as they already did.

    Raises:
        PermissionDenied: if the parent order is owned by Business Central.
    """
    _refuse_business_central(line.purchase_order)
    with transaction.atomic():
        line.delete()


def update_purchase_order(*, purchase_order: PurchaseOrder, **fields: Any) -> PurchaseOrder:
    """Update an SCM-owned purchase order header.

    Raises:
        PermissionDenied: if the order is owned by Business Central.
    """
    _refuse_business_central(purchase_order)
    with transaction.atomic():
        for attr, value in fields.items():
            setattr(purchase_order, attr, value)
        purchase_order.save(update_fields=[*fields.keys(), "updated_at"])
    return purchase_order
