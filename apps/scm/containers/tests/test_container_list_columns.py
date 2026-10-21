"""The container list's Carrier, Source and Supplier columns.

Carrier is who moves the box, Source is whose API the tracking comes from, and the two
are different names for an aggregator watch — Hapag-Lloyd through Traqo Ocean. Supplier
is who the box was bought from, read through its ContainerAcquisition's purchase order.

The row is rendered from two places — the list and the Start/Stop response — and both
must say the same thing about the same container.
"""

import re
from datetime import timedelta

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.scm.procurement.models import ContainerAcquisition, PurchaseOrder, PurchaseOrderLine
from apps.scm.tracking.models import TrackingProvider, TrackingSubscription
from apps.teams.models import Team
from apps.teams.roles import ROLE_ADMIN
from apps.users.models import CustomUser

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "list-columns"}}
_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

LONG_SUPPLIER = "Container Providers Int. Aps CPI USD JYBADKKK"


def _container(team, owner="PSL", serial="291364") -> Container:
    equipment_type = EquipmentType.objects.get_or_create(
        iso_code="22G1",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]
    return Container.objects.create(
        team=team,
        owner_code=owner,
        category_id="U",
        serial_number=serial,
        check_digit=calculate_check_digit(owner, "U", serial),
        equipment_type=equipment_type,
    )


def _traqo() -> TrackingProvider:
    provider, _ = TrackingProvider.objects.update_or_create(code="traqo", defaults={"name": "Traqo Ocean"})
    return provider


def _watch(team, container, *, carrier_code="", carrier_name="", provider=None) -> TrackingSubscription:
    return TrackingSubscription.objects.create(
        team=team,
        container=container,
        provider=provider or _traqo(),
        tracking_reference=container.container_id,
        carrier_code=carrier_code,
        carrier_name=carrier_name,
    )


def _acquire(team, container, supplier_name: str) -> ContainerAcquisition:
    order = PurchaseOrder.objects.create(
        team=team,
        external_id=f"po-{container.pk}",
        po_number=f"PO{container.pk}",
        supplier_no="S1",
        supplier_name=supplier_name,
    )
    line = PurchaseOrderLine.objects.create(
        team=team, purchase_order=order, external_id=f"pol-{container.pk}", line_no="10000", item_no="CONT22G1"
    )
    return ContainerAcquisition.objects.create(team=team, purchase_order_line=line, container=container)


def _cells(html: str, container: Container) -> list[str]:
    """The text of each cell in this container's row, whitespace collapsed."""
    match = re.search(rf'<tr\s+id="container-row-{container.pk}".*?</tr>', html, re.S)
    assert match is not None, f"no row for container {container.pk}"
    row = match.group(0)
    return [" ".join(re.sub(r"<[^>]+>", " ", cell).split()) for cell in re.findall(r"<td.*?</td>", row, re.S)]


# Column positions in the row, matching container_table.html's header.
CARRIER, SOURCE, SUPPLIER = 3, 4, 9


@override_settings(CACHES=_LOCMEM, STORAGES=_TEST_STORAGES)
class ContainerListColumnsTest(TestCase):
    def setUp(self):
        self.team = Team.objects.create(name="Columns", slug="list-columns")
        self.admin = CustomUser.objects.create(username="admin@list-columns.test", email="admin@list-columns.test")
        self.team.members.add(self.admin, through_defaults={"role": ROLE_ADMIN})
        self.container = _container(self.team)
        self.client = Client()
        self.client.force_login(self.admin)

    def list_cells(self) -> list[str]:
        response = self.client.get(reverse("containers:list"))
        self.assertEqual(response.status_code, 200)
        return _cells(response.content.decode(), self.container)

    def test_header_has_source_after_carrier_and_supplier_instead_of_manufacturer(self):
        response = self.client.get(reverse("containers:list"))

        headers = re.findall(r"<th>(.*?)</th>", response.content.decode())
        self.assertEqual(headers[CARRIER : SOURCE + 1], ["Carrier", "Source"])
        self.assertEqual(headers[SUPPLIER], "Supplier")
        self.assertNotIn("Manufacturer", headers)

    def test_traqo_watch_under_hapag_lloyd_shows_each_in_its_own_column(self):
        _watch(self.team, self.container, carrier_code="hapag_lloyd", carrier_name="Hapag-Lloyd")

        cells = self.list_cells()

        self.assertEqual(cells[CARRIER], "Hapag-Lloyd")
        self.assertEqual(cells[SOURCE], "Traqo Ocean")

    def test_carrier_recorded_by_code_alone_reads_as_the_registry_name(self):
        _watch(self.team, self.container, carrier_code="hapag_lloyd")

        self.assertEqual(self.list_cells()[CARRIER], "Hapag-Lloyd")

    def test_unknown_carrier_is_never_shown_as_traqo(self):
        _watch(self.team, self.container)

        cells = self.list_cells()

        self.assertEqual(cells[CARRIER], "—")
        self.assertEqual(cells[SOURCE], "Traqo Ocean")

    def test_untracked_container_shows_dashes(self):
        cells = self.list_cells()

        self.assertEqual(cells[CARRIER], "—")
        self.assertEqual(cells[SOURCE], "—")
        self.assertEqual(cells[SUPPLIER], "—")

    def test_supplier_comes_from_the_acquisition_purchase_order(self):
        _acquire(self.team, self.container, "Acme Boxes")
        self.container.manufacturer = "CIMC"
        self.container.save(update_fields=["manufacturer"])

        cells = self.list_cells()

        self.assertEqual(cells[SUPPLIER], "Acme Boxes")
        self.assertNotIn("CIMC", cells)

    def test_long_supplier_is_truncated_visually_but_whole_in_the_html(self):
        _acquire(self.team, self.container, LONG_SUPPLIER)

        response = self.client.get(reverse("containers:list"))

        self.assertContains(
            response,
            f'<span class="block max-w-[180px] truncate" title="{LONG_SUPPLIER}">{LONG_SUPPLIER}</span>',
            html=True,
        )

    def test_htmx_row_after_stop_matches_the_list(self):
        # An older, finished Traqo leg under Hapag-Lloyd, and a newer direct Maersk watch.
        # Stopping cancels the Maersk one, so the row must fall back to the Traqo leg —
        # and say exactly what a fresh load of the list says.
        completed = _watch(self.team, self.container, carrier_code="hapag_lloyd", carrier_name="Hapag-Lloyd")
        completed.status = TrackingSubscription.Status.COMPLETED
        completed.save(update_fields=["status"])
        TrackingSubscription.objects.filter(pk=completed.pk).update(created_at=timezone.now() - timedelta(days=30))
        maersk, _ = TrackingProvider.objects.update_or_create(code="maersk", defaults={"name": "Maersk"})
        _watch(self.team, self.container, carrier_code="maersk", carrier_name="Maersk", provider=maersk)
        _acquire(self.team, self.container, LONG_SUPPLIER)
        before = self.list_cells()
        self.assertEqual((before[CARRIER], before[SOURCE]), ("Maersk", "Maersk"))

        response = self.client.post(
            reverse("containers:stop_tracking", args=[self.container.pk]),
            {"origin": "row"},
            headers={"hx-request": "true"},
        )

        self.assertEqual(response.status_code, 200)
        row = _cells(response.content.decode(), self.container)
        self.assertEqual(row[CARRIER], "Hapag-Lloyd")
        self.assertEqual(row[SOURCE], "Traqo Ocean")
        self.assertEqual(row[SUPPLIER], LONG_SUPPLIER)
        self.assertEqual(row, self.list_cells())

    def test_another_teams_watch_and_acquisition_are_not_shown(self):
        other = Team.objects.create(name="Other", slug="list-columns-other")
        # Rows pointing at this team's container but owned by another team — corrupt data
        # the list must still refuse to read across.
        _watch(other, self.container, carrier_code="hapag_lloyd", carrier_name="Hapag-Lloyd")
        order = PurchaseOrder.objects.create(
            team=other, external_id="x", po_number="POX", supplier_no="S", supplier_name="Other Supplier"
        )
        line = PurchaseOrderLine.objects.create(
            team=other, purchase_order=order, external_id="xl", line_no="1", item_no="I"
        )
        ContainerAcquisition.objects.bulk_create(
            [ContainerAcquisition(team=self.team, purchase_order_line=line, container=self.container)]
        )

        cells = self.list_cells()

        self.assertEqual(cells[CARRIER], "—")
        self.assertEqual(cells[SOURCE], "—")
        self.assertEqual(cells[SUPPLIER], "—")

    def test_list_queries_do_not_grow_with_rows(self):
        def count() -> int:
            with CaptureQueriesContext(connection) as ctx:
                self.client.get(reverse("containers:list"))
            return len(ctx.captured_queries)

        _watch(self.team, self.container, carrier_code="hapag_lloyd", carrier_name="Hapag-Lloyd")
        _acquire(self.team, self.container, "Acme")
        baseline = count()
        for serial in ("100001", "100002", "100003"):
            container = _container(self.team, owner="HLX", serial=serial)
            _watch(self.team, container, carrier_code="hapag_lloyd")
            _acquire(self.team, container, f"Supplier {serial}")

        self.assertEqual(count(), baseline)
