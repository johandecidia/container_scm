"""Vizion — a container visibility aggregator, and a carrier *resolver*.

Vizion sits beside the direct carrier adapters for the same reason Traqo does: it
answers about containers moving with ONE, Maersk, MSC and two dozen others, and it is
not a carrier itself. So this package is a sibling of ``carriers/`` and is deliberately
absent from the carrier registry — an entry there would put Vizion into carrier
discovery sweeps and the team's carrier integration screens as though it moved boxes.

What makes it different from Traqo, and the reason this POC exists, is one capability:

    Auto Carrier Identification (ACI)
        Create a reference with a container number and **no carrier**, and Vizion works
        out which line is carrying it.

That is *carrier resolution*, and it is a separate question from *tracking provider
selection*. Container SCM must be able to end up in any of these states::

    Carrier: ONE      Resolved by: Vizion         Tracked by: Traqo
    Carrier: Maersk   Resolved by: Container SCM  Tracked by: Maersk Direct
    Carrier: ONE      Resolved by: Vizion         Tracked by: Vizion

Nothing in this package couples the two. :func:`~.service.resolve_carrier_via_aci`
returns evidence about who is carrying the box; :func:`~.service.ingest_vizion_container`
stores tracking data. Neither calls the other, and neither writes a routing decision.

What it feeds is the ordinary tracking domain::

    VizionClient.create_reference()   → the reference envelope (ACI happens here)
    VizionClient.get_reference()      → ACI outcome, via last_update_status
    VizionClient.list_updates()       → the update envelopes
    map_vizion_updates()              → NormalisedTrackingEvent DTOs
    apps.scm.tracking.ingestion       → TrackingEvent rows

See ``README.md`` in this package for the capability matrix, the canonical gaps and
what the POC does and does not prove.
"""

PROVIDER_CODE = "vizion"
PROVIDER_NAME = "Vizion"
