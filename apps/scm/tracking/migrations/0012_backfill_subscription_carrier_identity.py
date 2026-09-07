"""Give existing subscriptions the carrier identity they always implied.

Until now a subscription recorded only its provider, and for a direct carrier watch
that single code carried both facts: Maersk supplies the data *and* Maersk is moving
the box. Separating the two leaves those rows with a blank ``carrier_code`` that is
strictly less than what was already known, so this fills it in — the carrier is the
provider, and the evidence is that the carrier returned tracking events, which is the
only way a subscription is ever created.

Two rules keep the backfill honest.

*Only where the provider really is a carrier.* An aggregator watch — Traqo, Vizion —
is left with a blank carrier. ``carrier_code = traqo`` would be false, and it is the
precise falsehood this whole change exists to remove; a later refresh resolves those
properly rather than inheriting a guess made here.

*Nothing already recorded is touched.* Only blank ``carrier_code`` rows are written,
so re-running is a no-op and a carrier a person has since corrected survives.

The carrier codes are frozen below rather than read from the registry. A historical
migration must produce the same result in a year's time, and a registry this imported
would keep changing under it.
"""

from django.db import migrations

# The registry's provider codes as of this migration. A provider whose code is in this
# set was, by construction, created from a carrier — see carriers/registry.py.
CARRIER_PROVIDER_CODES = (
    "maersk",
    "msc",
    "cma_cgm",
    "cosco",
    "hapag_lloyd",
    "one",
    "evergreen",
    "hmm",
    "yang_ming",
    "zim",
)

DIRECT_API = "direct_api"


def backfill(apps, schema_editor):
    TrackingSubscription = apps.get_model("scm_tracking", "TrackingSubscription")

    rows = TrackingSubscription.objects.filter(
        carrier_code="",
        provider__code__in=CARRIER_PROVIDER_CODES,
    ).select_related("provider")

    updated = []
    for subscription in rows:
        subscription.carrier_code = subscription.provider.code
        # The provider row's name is the carrier's name for these codes; it is what the
        # UI has been showing as the carrier all along.
        subscription.carrier_name = subscription.provider.name or subscription.provider.code
        subscription.carrier_source = DIRECT_API
        updated.append(subscription)

    if updated:
        TrackingSubscription.objects.bulk_update(updated, ["carrier_code", "carrier_name", "carrier_source"])


def unfill(apps, schema_editor):
    """Clear only what the forward migration wrote, leaving anything else alone."""
    TrackingSubscription = apps.get_model("scm_tracking", "TrackingSubscription")
    TrackingSubscription.objects.filter(
        carrier_source=DIRECT_API,
        provider__code__in=CARRIER_PROVIDER_CODES,
    ).update(carrier_code="", carrier_name="", carrier_source="")


class Migration(migrations.Migration):
    dependencies = [
        ("scm_tracking", "0011_trackingsubscription_carrier_code_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, unfill),
    ]
