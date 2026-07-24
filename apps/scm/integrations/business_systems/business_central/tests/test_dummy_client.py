"""Tests for the Business Central dummy client."""

from django.test import SimpleTestCase

from apps.scm.integrations.business_systems.business_central.client import BusinessCentralClient
from apps.scm.integrations.business_systems.business_central.exceptions import BusinessCentralConfigurationError


class DummyClientPurchaseOrdersTest(SimpleTestCase):
    def setUp(self):
        self.client = BusinessCentralClient(use_dummy=True)

    def test_fetch_purchase_orders_returns_list(self):
        result = self.client.fetch_purchase_orders()
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_fetch_purchase_orders_first_po(self):
        result = self.client.fetch_purchase_orders()
        po = result[0]
        self.assertEqual(po["id"], "bc-po-id-001")
        self.assertEqual(po["number"], "PO100245")
        self.assertEqual(po["vendorNumber"], "L00141")
        self.assertEqual(po["vendorName"], "Shanghai Containers Ltd")
        self.assertEqual(po["status"], "Open")
        self.assertEqual(po["currencyCode"], "USD")

    def test_fetch_purchase_orders_second_po(self):
        result = self.client.fetch_purchase_orders()
        po = result[1]
        self.assertEqual(po["number"], "PO100246")
        self.assertEqual(po["status"], "Released")

    def test_fetch_purchase_order_lines_po100245(self):
        lines = self.client.fetch_purchase_order_lines("PO100245")
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["id"], "bc-line-id-001")
        self.assertEqual(line["itemNumber"], "40HC-NEW")
        self.assertEqual(line["quantity"], 25)
        self.assertEqual(line["directUnitCost"], 1940.0)

    def test_fetch_purchase_order_lines_po100246(self):
        lines = self.client.fetch_purchase_order_lines("PO100246")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["itemNumber"], "40RF-NEW")
        self.assertEqual(lines[1]["itemNumber"], "40RF-PTI")

    def test_live_client_without_integration_raises_configuration_error(self):
        with self.assertRaises(BusinessCentralConfigurationError):
            BusinessCentralClient(use_dummy=False)
