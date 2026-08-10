"""Tests for specific carrier capabilities as declared in the registry."""

import unittest

from apps.scm.integrations.carriers.registry import carrier_supports, get_carrier_definition


class MaerskCapabilitiesTest(unittest.TestCase):
    def test_maersk_supports_webhooks(self):
        defn = get_carrier_definition("maersk")
        self.assertTrue(defn.capabilities.supports_webhooks)

    def test_maersk_supports_dcsa(self):
        defn = get_carrier_definition("maersk")
        self.assertTrue(defn.capabilities.supports_dcsa)

    def test_maersk_supports_pull(self):
        defn = get_carrier_definition("maersk")
        self.assertTrue(defn.capabilities.supports_pull)

    def test_maersk_does_not_require_an_account_number(self):
        """The public Track & Trace endpoint answers on the consumer key alone."""
        defn = get_carrier_definition("maersk")
        self.assertFalse(defn.capabilities.requires_account_number)

    def test_registry_and_client_agree_on_the_account_number(self):
        from apps.scm.integrations.carriers.maersk.client import MaerskClient

        defn = get_carrier_definition("maersk")
        self.assertEqual(
            defn.capabilities.requires_account_number,
            MaerskClient.capabilities.requires_account_number,
        )


class HapagLloydCapabilitiesTest(unittest.TestCase):
    def test_hapag_lloyd_supports_webhooks(self):
        defn = get_carrier_definition("hapag_lloyd")
        self.assertTrue(defn.capabilities.supports_webhooks)

    def test_hapag_lloyd_supports_dcsa(self):
        defn = get_carrier_definition("hapag_lloyd")
        self.assertTrue(defn.capabilities.supports_dcsa)


class CmaCgmCapabilitiesTest(unittest.TestCase):
    def test_cma_cgm_supports_dcsa(self):
        defn = get_carrier_definition("cma_cgm")
        self.assertTrue(defn.capabilities.supports_dcsa)

    def test_cma_cgm_supports_webhooks(self):
        defn = get_carrier_definition("cma_cgm")
        self.assertTrue(defn.capabilities.supports_webhooks)


class EvergreenCapabilitiesTest(unittest.TestCase):
    def test_evergreen_does_not_support_pull(self):
        defn = get_carrier_definition("evergreen")
        self.assertFalse(defn.capabilities.supports_pull)

    def test_evergreen_does_not_support_dcsa(self):
        defn = get_carrier_definition("evergreen")
        self.assertFalse(defn.capabilities.supports_dcsa)


class YangMingCapabilitiesTest(unittest.TestCase):
    def test_yang_ming_supports_tracking_by_purchase_order(self):
        defn = get_carrier_definition("yang_ming")
        self.assertTrue(defn.capabilities.supports_tracking_by_purchase_order)


class ZimCapabilitiesTest(unittest.TestCase):
    def test_zim_does_not_support_pull(self):
        defn = get_carrier_definition("zim")
        self.assertFalse(defn.capabilities.supports_pull)

    def test_zim_does_not_support_dcsa(self):
        defn = get_carrier_definition("zim")
        self.assertFalse(defn.capabilities.supports_dcsa)


class CarrierSupportsHelperTest(unittest.TestCase):
    def test_carrier_supports_maersk_webhooks_returns_true(self):
        self.assertTrue(carrier_supports("maersk", "supports_webhooks"))

    def test_carrier_supports_evergreen_webhooks_returns_false(self):
        self.assertFalse(carrier_supports("evergreen", "supports_webhooks"))
