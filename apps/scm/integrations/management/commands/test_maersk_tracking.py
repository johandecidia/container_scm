"""Make one live Maersk Track & Trace call and report what came back.

A Maersk-fixed alias for ``test_carrier_tracking``, which holds the logic for every
carrier. Read-only, and the consumer key is never printed.

Usage:
    python manage.py test_maersk_tracking TRDU9258963 --team <team-slug>
"""

from apps.scm.integrations.carriers.maersk.client import PROVIDER_CODE

from .test_carrier_tracking import Command as CarrierTrackingCommand


class Command(CarrierTrackingCommand):
    help = "Make one live Maersk Track & Trace call for a container number (read-only)."
    provider_code = PROVIDER_CODE
