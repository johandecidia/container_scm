"""Reading a Vizion reference — and its ACI outcome — as carrier-resolution *evidence*.

Container SCM already has a way to find out which carrier knows a container:
``integrations/carriers/carrier_discovery.py`` builds candidates from what somebody
chose, from the ISO 6346 owner prefix, and from the team's connected carriers, then
probes them and lets the one that answers with data win. Traqo's ``/carriers/lookup`` is
a fourth signal into that model. Vizion's ACI is a fifth. Nothing here imports or
modifies that module.

Which is deliberate, and it is the same reasoning the Traqo lookup was built on: if a
third party owned carrier identity, Container SCM's answer to "who is moving this box"
would be an opinion nothing could disagree with. So the reading below produces a
resolved carrier, a state and a reason, and stops.

Two things make Vizion's evidence stronger than a lookup, and one makes it dearer.

*It is an answer, not a guess.* ``auto_carrier_completed`` means a supported carrier
returned recent shipment data for this box. That is not a probabilistic match on a
number's shape — it is a carrier system saying "yes, this is mine". So there is no
confidence band to read: the state **is** the confidence, and inventing a HIGH/MEDIUM/LOW
scale for it would add a number Vizion never stated.

*It distinguishes "not yet" from "no".* ``auto_carrier_not_found`` retries daily for up
to seven days; ``auto_carrier_failed`` stops. Collapsing the two into "failed" would
throw away a pending answer.

*It costs a reference.* Unlike Traqo's lookup, ACI is not free — creating the reference
is what starts tracking, and a reference is the billable unit. So identification and
tracking arrive together whether or not the caller wanted both. That is a property of
the provider, reported rather than hidden, and it is the single most important input to
any future routing decision: resolving through Vizion and then tracking elsewhere means
paying for a reference and not reading it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Vizion's last_update_status values, as documented. Kept as constants rather than an
# enum on a model: these are a provider's vocabulary, not Container SCM's, and nothing
# persists them as a field.
STATUS_AUTO_CARRIER_COMPLETED = "auto_carrier_completed"
STATUS_AUTO_CARRIER_NOT_FOUND = "auto_carrier_not_found"
STATUS_AUTO_CARRIER_FAILED = "auto_carrier_failed"
STATUS_DATA_RECEIVED = "data_received"
STATUS_NO_DATA = "no_data"
STATUS_DUPLICATE_PAYLOAD = "duplicate_payload"
STATUS_INVALID_CONTAINER = "invalid_container"
STATUS_EXTRACTION_FAILED = "extraction_failed"
STATUS_INCOMPLETE_PROCESSING = "incomplete_processing"

# How Container SCM reads an ACI outcome. Four states, because three would have to merge
# two that mean different things to a caller.
ACI_IDENTIFIED = "IDENTIFIED"  # a carrier was found and attached
ACI_PENDING = "PENDING"  # Vizion is still looking, or has not answered yet
ACI_NOT_FOUND = "NOT_FOUND"  # no supported carrier had data; Vizion keeps retrying
ACI_FAILED = "FAILED"  # identification errored, or the identifier was rejected

_ACI_STATE_BY_STATUS = {
    STATUS_AUTO_CARRIER_COMPLETED: ACI_IDENTIFIED,
    STATUS_AUTO_CARRIER_NOT_FOUND: ACI_NOT_FOUND,
    STATUS_AUTO_CARRIER_FAILED: ACI_FAILED,
    STATUS_INVALID_CONTAINER: ACI_FAILED,
    STATUS_EXTRACTION_FAILED: ACI_FAILED,
    # A reference that is already producing tracking data has, by definition, a carrier
    # attached — Vizion cannot build a payload without one.
    STATUS_DATA_RECEIVED: ACI_IDENTIFIED,
    STATUS_DUPLICATE_PAYLOAD: ACI_IDENTIFIED,
    STATUS_NO_DATA: ACI_PENDING,
    STATUS_INCOMPLETE_PROCESSING: ACI_PENDING,
}


def _text(source: dict, *keys: str) -> str:
    """Return the first non-empty value among ``keys``, stripped."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, (dict, list)):
            continue
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


@dataclass(frozen=True)
class VizionReference:
    """One Vizion reference, and what it says about who is carrying the box.

    Evidence for carrier resolution. Deliberately not a decision, and deliberately not a
    statement about which provider should *track* the container — those are different
    questions and this dataclass answers only the first.
    """

    container_number: str
    reference_id: str = ""
    carrier_scac: str = ""
    carrier_code: str = ""
    carrier_name: str = ""
    auto_carrier: bool | None = None
    active: bool | None = None
    last_update_status: str = ""
    aci_state: str = ACI_PENDING
    retry_count: int | None = None
    created_at: str = ""
    last_update_attempted_at: str = ""
    deactivate_reason: str = ""
    # The envelope verbatim, so a field this reader did not recognise is not lost and
    # the parse can be revisited against real evidence.
    raw: dict = field(default_factory=dict)

    @property
    def identified(self) -> bool:
        """True when Vizion has attached a carrier to this reference."""
        return self.aci_state == ACI_IDENTIFIED and bool(self.carrier_scac or self.carrier_code)

    @property
    def used_aci(self) -> bool:
        """True when Vizion says this reference's carrier came from ACI.

        Distinct from ``identified``: a reference created *with* a carrier code is
        identified from the start and used no ACI at all. The POC needs to tell those
        apart, because only the second proves anything about resolving an unknown box.
        """
        return self.auto_carrier is True

    @property
    def carrier_identifier(self) -> str:
        """The carrier Vizion named, preferring its own stable code over the SCAC.

        Vizion documents ``scac`` as deprecated in favour of ``carrier_code``, because a
        SCAC can change under a carrier — Yang Ming's moved from YMLU to YMJA in 2023
        while Vizion's code stayed YMLU. Where both are present the code wins.
        """
        return self.carrier_code or self.carrier_scac

    def as_dict(self) -> dict:
        return {
            "container_number": self.container_number,
            "reference_id": self.reference_id,
            "carrier_scac": self.carrier_scac,
            "carrier_code": self.carrier_code,
            "carrier_name": self.carrier_name,
            "carrier_identifier": self.carrier_identifier,
            "auto_carrier": self.auto_carrier,
            "used_aci": self.used_aci,
            "active": self.active,
            "last_update_status": self.last_update_status,
            "aci_state": self.aci_state,
            "identified": self.identified,
            "retry_count": self.retry_count,
            "created_at": self.created_at,
            "last_update_attempted_at": self.last_update_attempted_at,
            "deactivate_reason": self.deactivate_reason,
        }


def read_aci_state(last_update_status: str) -> str:
    """Classify a Vizion ``last_update_status`` into an ACI state.

    An empty status means Vizion has not attempted an update yet, which is PENDING and
    not a failure — a reference created seconds ago is in exactly that position. A status
    this reader does not recognise is also PENDING rather than being rounded to the
    nearest familiar outcome, because reporting an unknown state as FAILED would
    deactivate a POC run over a vocabulary change.
    """
    status = (last_update_status or "").strip().lower()
    if not status:
        return ACI_PENDING
    state = _ACI_STATE_BY_STATUS.get(status)
    if state is None:
        logger.info("Vizion: unrecognised last_update_status %r — treating as pending.", last_update_status)
        return ACI_PENDING
    return state


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def read_reference(payload: dict, *, container_number: str = "") -> VizionReference:
    """Read a reference envelope — from create or from get — into carrier evidence.

    Accepts both shapes Vizion returns: ``POST /references`` wraps the object in
    ``{"message": ..., "reference": {...}}`` while ``GET /references/{id}`` returns it
    bare. Unwrapping here rather than at the two call sites is what keeps the ACI reading
    identical whether it came from the create or from a later poll.

    Tolerant by design: an unreadable response yields a reference that reports nothing
    identified, never an exception, because the observation itself is the thing being
    collected.
    """
    if not isinstance(payload, dict):
        return VizionReference(container_number=(container_number or "").strip().upper())

    reference = payload.get("reference") if isinstance(payload.get("reference"), dict) else payload
    carrier = reference.get("carrier") if isinstance(reference.get("carrier"), dict) else {}

    status = _text(reference, "last_update_status")
    active = reference.get("active")
    auto_carrier = reference.get("auto_carrier")

    return VizionReference(
        container_number=(container_number or _text(reference, "container_id")).strip().upper(),
        reference_id=_text(reference, "id", "reference_id"),
        carrier_scac=_text(reference, "carrier_scac", "scac").upper(),
        # The nested carrier object is Vizion's own record of the line; the flat
        # carrier_code is the same value denormalised onto the reference.
        carrier_code=(_text(reference, "carrier_code") or _text(carrier, "code")).upper(),
        carrier_name=_text(carrier, "name") or _text(reference, "carrier_name"),
        auto_carrier=auto_carrier if isinstance(auto_carrier, bool) else None,
        active=active if isinstance(active, bool) else None,
        last_update_status=status,
        aci_state=read_aci_state(status),
        retry_count=_int_or_none(reference.get("retry_count")),
        created_at=_text(reference, "created_at"),
        last_update_attempted_at=_text(reference, "last_update_attempted_at"),
        deactivate_reason=_text(reference, "deactivate_reason"),
        raw=payload,
    )
