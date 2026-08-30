"""Traqo Ocean — an external ocean tracking aggregator, not a carrier.

Traqo sits beside the direct carrier adapters rather than among them: it answers
about containers moving with Maersk, CMA CGM, COSCO and others, and the real
carrier stays identifiable through the ``sealine`` we ask with. That is why this
package is a sibling of ``carriers/`` and is deliberately absent from the carrier
registry — a Traqo entry there would put it into carrier discovery sweeps and the
team's carrier integration screens as though it moved boxes itself.

What it feeds is the ordinary tracking domain::

    TraqoClient.get_container()      → the original response envelope
    map_traqo_container_payload()    → NormalisedTrackingEvent DTOs
    apps.scm.tracking.ingestion      → TrackingEvent rows

See ``README.md`` in this package for what the sandbox does and does not prove.
"""

PROVIDER_CODE = "traqo"
PROVIDER_NAME = "Traqo Ocean"
