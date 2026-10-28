"""The purchase order list as a table: display, sorting, filtering and paging.

Four orders on one team, chosen so every column has something to sort and filter:
a long supplier name, a fractional quantity, two orders that tie on Remaining, an
order with no lines, and one of each source. A second team owns an order that
matches the same text filters, so isolation is checked by every filter, not once.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.scm.procurement.models import PurchaseOrder, PurchaseOrderLine
from apps.scm.procurement.selectors import annotate_purchase_order_totals
from apps.scm.procurement.services import calculate_purchase_order_fulfillment
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
LONG_SUPPLIER = "Grand View Containers International Holdings Aktiebolag"


def _po(team, number, supplier, status, source, order_date, lines=()):
    po = PurchaseOrder.objects.create(
        team=team,
        external_id=f"ext-{team.slug}-{number}",
        po_number=number,
        supplier_no="SUP",
        supplier_name=supplier,
        status=status,
        source_system=source,
        order_date=order_date,
    )
    for index, (ordered, shipped, received) in enumerate(lines):
        PurchaseOrderLine.objects.create(
            team=team,
            purchase_order=po,
            external_id=f"{po.external_id}-{index}",
            line_no=str(10000 + index),
            item_no=f"ITEM-{index}",
            ordered_qty=Decimal(ordered),
            shipped_qty=Decimal(shipped),
            received_qty=Decimal(received),
        )
    return po


@override_settings(STORAGES=_TEST_STORAGES)
class PurchaseOrderListTableTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Table Team", slug="po-table-team")
        cls.user = CustomUser.objects.create_user(username="po-table@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.a = _po(cls.team, "100046", LONG_SUPPLIER, "open", "business_central", date(2026, 10, 1), [("5", "2", "0")])
        cls.b = _po(
            cls.team,
            "100047",
            "Alpha Supply",
            "partially_received",
            "manual",
            date(2026, 9, 1),
            [("10", "10", "4"), ("2.5", "2.5", "0")],
        )
        cls.c = _po(
            cls.team,
            "200001",
            "beta containers",
            "fully_received",
            "business_central",
            date(2026, 8, 1),
            [("3", "3", "3")],
        )
        cls.d = _po(cls.team, "PO-NOLINES", "Zeta Trading", "closed", "document_import", date(2026, 7, 1))

        other = Team.objects.create(name="Other Table Team", slug="po-table-other")
        _po(other, "100046-OTHER", "Grand View Other", "open", "manual", date(2026, 10, 2), [("9", "0", "0")])

    def setUp(self):
        self.client.force_login(self.user)

    def get(self, **params):
        return self.client.get(reverse("procurement:purchase_order_list"), params)

    def numbers(self, **params) -> list[str]:
        return [po.po_number for po in self.get(**params).context["po_rows"]]

    # -- display -----------------------------------------------------------

    def test_po_number_and_bc_badge_sit_on_one_line(self):
        html = self.get().content.decode()
        detail = reverse("procurement:purchase_order_detail", args=[self.a.pk])
        self.assertRegex(
            html,
            rf'(?s)<td class="whitespace-nowrap">\s*<span class="inline-flex items-center gap-2">\s*'
            rf'<a [^>]*href="{detail}">100046</a>\s*<span class="badge badge-xs badge-ghost[^"]*" '
            r'title="Managed by Business Central">BC</span>',
        )

    def test_status_badges_carry_the_models_label_and_its_colour(self):
        html = self.get().content.decode()
        for label, colour in (
            ("Open", "badge-info"),
            ("Partially Received", "badge-warning"),
            ("Fully Received", "badge-success"),
            ("Closed", "badge-neutral"),
        ):
            with self.subTest(label=label):
                self.assertRegex(
                    html,
                    rf'(?s)<span class="badge badge-sm badge-soft whitespace-nowrap\s+{colour}">\s*{label}\s*</span>',
                )

    def test_a_long_supplier_is_truncated_on_screen_and_whole_in_the_markup(self):
        html = self.get().content.decode()
        self.assertIn(f'<span class="block truncate" title="{LONG_SUPPLIER}">{LONG_SUPPLIER}</span>', html)

    def test_quantities_drop_empty_decimals_and_keep_real_ones(self):
        html = self.get().content.decode()
        row = html[html.index(f'id="po-row-{self.b.pk}"') :]
        row = row[: row.index("</tr>")]
        cells = re.findall(r'<td class="text-right tabular-nums">([^<]*)</td>', row)
        self.assertEqual(cells, ["12.500", "12.500", "4", "8.500"])
        self.assertNotIn("5.000", html)

    def test_the_list_totals_match_the_fulfillment_service(self):
        """The annotation is a second place the arithmetic lives; this holds them together."""
        annotated = {po.pk: po for po in annotate_purchase_order_totals(PurchaseOrder.objects.filter(team=self.team))}
        for po in (self.a, self.b, self.c, self.d):
            with self.subTest(po=po.po_number):
                service = calculate_purchase_order_fulfillment(po)
                row = annotated[po.pk]
                self.assertEqual(
                    (row.ordered_total, row.shipped_total, row.received_total, row.remaining_total),
                    (service["ordered_qty"], service["shipped_qty"], service["received_qty"], service["remaining_qty"]),
                )

    def test_the_page_does_not_run_a_query_per_row(self):
        def count():
            with CaptureQueriesContext(connection) as queries:
                self.get()
            return len(queries)

        before = count()
        for index in range(5):
            _po(self.team, f"EXTRA-{index}", "Extra", "open", "manual", date(2026, 1, 1), [("1", "0", "0")])
        self.assertEqual(count(), before)

    # -- sorting -----------------------------------------------------------

    def test_default_order_is_newest_order_first(self):
        self.assertEqual(self.numbers(), ["100046", "100047", "200001", "PO-NOLINES"])

    def test_supplier_sorts_both_ways_ignoring_case(self):
        self.assertEqual(self.numbers(sort="supplier"), ["100047", "200001", "100046", "PO-NOLINES"])
        self.assertEqual(self.numbers(sort="supplier", direction="desc"), ["PO-NOLINES", "100046", "200001", "100047"])

    def test_status_sorts_in_workflow_order(self):
        self.assertEqual(self.numbers(sort="status"), ["100046", "100047", "200001", "PO-NOLINES"])

    def test_quantities_sort_on_the_summed_totals_with_ties_in_default_order(self):
        self.assertEqual(self.numbers(sort="remaining"), ["200001", "PO-NOLINES", "100046", "100047"])
        self.assertEqual(self.numbers(sort="ordered", direction="desc"), ["100047", "100046", "200001", "PO-NOLINES"])

    def test_an_unknown_sort_or_direction_falls_back_safely(self):
        for params in ({"sort": "pk"}, {"sort": "team__name"}, {"sort": "-po_number"}, {"sort": "supplier_name"}):
            with self.subTest(params=params):
                response = self.get(**params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([po.po_number for po in response.context["po_rows"]], self.numbers())
        self.assertEqual(self.numbers(sort="supplier", direction="sideways"), self.numbers(sort="supplier"))

    def test_a_header_cycles_ascending_descending_default_and_keeps_filters(self):
        columns = self.get(status="open").context["columns"]
        self.assertEqual(columns["supplier"]["sort_url"], "?status=open&sort=supplier&direction=asc")
        columns = self.get(status="open", sort="supplier").context["columns"]
        self.assertEqual(columns["supplier"]["direction"], "asc")
        self.assertEqual(columns["supplier"]["sort_url"], "?status=open&sort=supplier&direction=desc")
        columns = self.get(status="open", sort="supplier", direction="desc").context["columns"]
        self.assertEqual(columns["supplier"]["sort_url"], "?status=open")

    def test_the_sorted_column_announces_its_direction(self):
        html = self.get(sort="remaining", direction="desc").content.decode()
        self.assertIn('aria-sort="descending"', html)
        self.assertIn("fa-arrow-down", html)

    @mock.patch("apps.scm.procurement.views.PURCHASE_ORDERS_PER_PAGE", 2)
    def test_pages_split_a_sorted_list_without_repeating_or_losing_rows(self):
        first = self.numbers(sort="remaining", page=1)
        second = self.numbers(sort="remaining", page=2)
        self.assertEqual(first + second, ["200001", "PO-NOLINES", "100046", "100047"])

    # -- filtering ---------------------------------------------------------

    def test_po_number_search(self):
        self.assertEqual(self.numbers(po="1000"), ["100046", "100047"])

    def test_supplier_search_is_case_insensitive_and_team_scoped(self):
        self.assertEqual(self.numbers(supplier="GRAND"), ["100046"])

    def test_status_is_a_multiselect(self):
        response = self.client.get(reverse("procurement:purchase_order_list") + "?status=open&status=fully_received")
        self.assertEqual([po.po_number for po in response.context["po_rows"]], ["100046", "200001"])

    def test_source_filter(self):
        self.assertEqual(self.numbers(source="manual"), ["100047"])
        self.assertEqual(self.numbers(source="business_central"), ["100046", "200001"])

    def test_numeric_ranges(self):
        self.assertEqual(self.numbers(ordered_min="4"), ["100046", "100047"])
        self.assertEqual(self.numbers(remaining_max="0"), ["200001", "PO-NOLINES"])
        self.assertEqual(self.numbers(remaining_min="1", remaining_max="6"), ["100046"])
        self.assertEqual(self.numbers(shipped_min="12.5", shipped_max="12.5"), ["100047"])

    def test_filters_combine(self):
        self.assertEqual(self.numbers(supplier="a", status="partially_received", ordered_min="10"), ["100047"])

    def test_invalid_filter_values_narrow_nothing(self):
        everything = self.numbers()
        for params in (
            {"status": "cancelled"},
            {"source": "sap"},
            {"ordered_min": "abc"},
            {"remaining_max": "NaN"},
            {"received_min": "Infinity"},
        ):
            with self.subTest(params=params):
                response = self.get(**params)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([po.po_number for po in response.context["po_rows"]], everything)

    def test_a_filter_that_matches_nothing_keeps_the_table_and_says_so(self):
        html = self.get(supplier="nobody").content.decode()
        self.assertIn("No purchase orders match these filters.", html)
        self.assertIn('popovertarget="table-filter-supplier"', html)

    # -- chips and URL state ----------------------------------------------

    def test_each_chip_removes_only_its_own_filter(self):
        response = self.get(supplier="a", status="partially_received", remaining_min="1", sort="supplier")
        chips = {chip.label: chip.remove_url for chip in response.context["chips"]}
        self.assertEqual(
            chips,
            {
                "Supplier: a": "?status=partially_received&remaining_min=1&sort=supplier",
                "Status: Partially Received": "?supplier=a&remaining_min=1&sort=supplier",
                "Remaining ≥ 1": "?supplier=a&status=partially_received&sort=supplier",
            },
        )

    def test_clear_all_drops_every_filter_and_keeps_the_sort(self):
        response = self.get(supplier="a", status="open", sort="remaining", direction="desc", page="2")
        self.assertEqual(response.context["clear_all_url"], "?sort=remaining&direction=desc")

    def test_a_column_filter_form_carries_the_other_state_and_drops_the_page(self):
        response = self.get(supplier="a", status="open", sort="supplier", page="2")
        self.assertEqual(response.context["columns"]["status"]["hidden"], [("supplier", "a"), ("sort", "supplier")])
        self.assertEqual(response.context["columns"]["status"]["clear_url"], "?supplier=a&sort=supplier")

    # -- pagination --------------------------------------------------------

    @mock.patch("apps.scm.procurement.views.PURCHASE_ORDERS_PER_PAGE", 1)
    def test_pages_count_the_filtered_list_and_keep_its_state(self):
        response = self.get(status="open", source="business_central", sort="supplier")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)
        response = self.client.get(
            reverse("procurement:purchase_order_list") + "?status=open&status=fully_received&sort=supplier&page=2"
        )
        self.assertEqual(response.context["page_obj"].paginator.count, 2)
        self.assertEqual([po.po_number for po in response.context["po_rows"]], ["100046"])
        html = response.content.decode()
        self.assertIn("2–2 of 2 purchase orders", html)
        self.assertIn('hx-get="?status=open&amp;status=fully_received&amp;sort=supplier&amp;page=1"', html)

    # -- security and HTMX ------------------------------------------------

    def test_no_filter_reaches_another_team(self):
        for params in ({}, {"po": "100046"}, {"supplier": "grand"}, {"status": "open"}, {"ordered_min": "9"}):
            with self.subTest(params=params):
                self.assertNotIn("100046-OTHER", self.numbers(**params))

    def test_business_central_orders_still_offer_no_delete(self):
        html = self.get().content.decode()
        self.assertNotIn(reverse("procurement:purchase_order_delete", args=[self.a.pk]), html)
        self.assertNotIn(reverse("procurement:purchase_order_delete", args=[self.c.pk]), html)
        self.assertIn(reverse("procurement:purchase_order_delete", args=[self.b.pk]), html)

    def test_an_htmx_request_gets_the_table_alone(self):
        url = reverse("procurement:purchase_order_list")
        response = self.client.get(url, {"sort": "supplier"}, headers={"hx-request": "true"})
        self.assertNotContains(response, "Purchase order visibility from Business Central.")
        self.assertContains(response, 'id="po-row-')
        restore = self.client.get(
            url, {"sort": "supplier"}, headers={"hx-request": "true", "hx-history-restore-request": "true"}
        )
        self.assertContains(restore, "Purchase order visibility from Business Central.")

    def test_blank_values_do_not_travel_into_later_links(self):
        """A range form sends its empty bound; no link built afterwards carries it."""
        response = self.get(remaining_min="1", remaining_max="", supplier="")
        columns, chips = response.context["columns"], response.context["chips"]
        self.assertEqual(columns["supplier"]["sort_url"], "?remaining_min=1&sort=supplier&direction=asc")
        self.assertEqual(columns["status"]["hidden"], [("remaining_min", "1")])
        self.assertEqual([chip.remove_url for chip in chips], ["?"])
