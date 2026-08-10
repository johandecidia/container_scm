"""Tests for container intake: single create, paste import and CSV import.

The three paths share one service, so these tests check that a number is treated
identically whichever way it arrives, and that team scoping holds for all of them.
"""

import io

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from apps.scm.containers.forms import ContainerPasteForm, QuickContainerForm
from apps.scm.containers.intake import (
    bulk_create_containers,
    create_or_get_container,
    entries_from_csv,
    entries_from_text,
    preview_containers,
    split_container_numbers,
)
from apps.scm.containers.models import Container, EquipmentType
from apps.scm.containers.utils import calculate_check_digit
from apps.teams.models import Team
from apps.teams.roles import ROLE_MEMBER
from apps.users.models import CustomUser

_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _number(owner: str, serial: str, category: str = "U") -> str:
    return f"{owner}{category}{serial}{calculate_check_digit(owner, category, serial)}"


# Three valid numbers and one whose check digit is deliberately wrong.
VALID_A = _number("TRD", "925896")
VALID_B = _number("MSC", "123456")
VALID_C = _number("CMA", "765432")
INVALID_CHECK_DIGIT = VALID_C[:-1] + str((int(VALID_C[-1]) + 1) % 10)


def _et() -> EquipmentType:
    return EquipmentType.objects.get_or_create(
        iso_code="20GP",
        defaults={"category": "GP", "length_ft": 20, "high_cube": False, "description": "20' GP"},
    )[0]


def _csv(text: str) -> SimpleUploadedFile:
    return SimpleUploadedFile("containers.csv", text.encode("utf-8"), content_type="text/csv")


class SplitContainerNumbersTest(TestCase):
    def test_newline_separated(self):
        self.assertEqual(split_container_numbers(f"{VALID_A}\n{VALID_B}"), [VALID_A, VALID_B])

    def test_comma_and_semicolon_separated(self):
        self.assertEqual(split_container_numbers(f"{VALID_A}, {VALID_B};{VALID_C}"), [VALID_A, VALID_B, VALID_C])

    def test_tab_separated_excel_paste(self):
        self.assertEqual(split_container_numbers(f"{VALID_A}\t{VALID_B}\t"), [VALID_A, VALID_B])

    def test_lowercase_and_spaces_normalised(self):
        self.assertEqual(split_container_numbers(f"  {VALID_A.lower()}  "), [VALID_A])

    def test_duplicates_collapse_and_order_is_kept(self):
        self.assertEqual(split_container_numbers(f"{VALID_B}\n{VALID_A}\n{VALID_B}"), [VALID_B, VALID_A])

    def test_empty_text_gives_no_numbers(self):
        self.assertEqual(split_container_numbers("  \n\n"), [])


class QuickContainerFormTest(TestCase):
    def test_lowercase_input_is_accepted(self):
        form = QuickContainerForm({"container_number": VALID_A.lower()})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["container_number"], VALID_A)

    def test_surrounding_and_inner_spaces_are_accepted(self):
        form = QuickContainerForm({"container_number": f"  {VALID_A[:4]} {VALID_A[4:]}  "})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["container_number"], VALID_A)

    def test_invalid_check_digit_is_rejected(self):
        form = QuickContainerForm({"container_number": INVALID_CHECK_DIGIT})
        self.assertFalse(form.is_valid())
        self.assertIn("check digit", str(form.errors["container_number"]))

    def test_malformed_number_is_rejected(self):
        form = QuickContainerForm({"container_number": "NOT-A-CONTAINER"})
        self.assertFalse(form.is_valid())

    def test_carrier_is_optional(self):
        form = QuickContainerForm({"container_number": VALID_A})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["carrier"], "")

    def test_carrier_choices_come_from_the_registry(self):
        codes = {code for code, _label in QuickContainerForm().fields["carrier"].choices}
        self.assertIn("maersk", codes)
        self.assertIn("", codes)


class ContainerPasteFormTest(TestCase):
    def test_blank_list_is_rejected(self):
        form = ContainerPasteForm({"numbers": "   \n  "})
        self.assertFalse(form.is_valid())

    def test_too_many_numbers_is_rejected(self):
        form = ContainerPasteForm({"numbers": "\n".join(_number("ABC", f"{i:06d}") for i in range(501))})
        self.assertFalse(form.is_valid())


class CreateOrGetContainerTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Intake", slug="intake-team")
        cls.other_team = Team.objects.create(name="Other", slug="intake-other-team")
        cls.user = CustomUser.objects.create_user(username="intake@example.com", password="pass")

    def setUp(self):
        _et()

    def test_creates_container_with_parsed_parts(self):
        container, created = create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        self.assertTrue(created)
        self.assertEqual(container.owner_code, "TRD")
        self.assertEqual(container.category_id, "U")
        self.assertEqual(container.serial_number, "925896")
        self.assertEqual(container.check_digit, int(VALID_A[-1]))
        self.assertEqual(container.container_id, VALID_A)

    def test_defaults_are_applied_without_asking(self):
        container, _created = create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        self.assertEqual(container.equipment_type, _et())
        self.assertEqual(container.status, "AVAILABLE")
        self.assertEqual(container.condition, "GOOD")
        self.assertEqual(container.created_by, self.user)

    def test_lowercase_input_creates_the_same_container(self):
        create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        _container, created = create_or_get_container(team=self.team, user=self.user, number=VALID_A.lower())
        self.assertFalse(created)
        self.assertEqual(Container.objects.filter(team=self.team).count(), 1)

    def test_duplicate_returns_existing_without_creating(self):
        first, _ = create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        second, created = create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        self.assertFalse(created)
        self.assertEqual(first.pk, second.pk)

    def test_invalid_check_digit_raises(self):
        with self.assertRaises(ValidationError):
            create_or_get_container(team=self.team, user=self.user, number=INVALID_CHECK_DIGIT)
        self.assertFalse(Container.objects.filter(team=self.team).exists())

    def test_same_number_in_two_teams_creates_two_containers(self):
        create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        _container, created = create_or_get_container(team=self.other_team, user=self.user, number=VALID_A)
        self.assertTrue(created)
        self.assertEqual(Container.objects.filter(team=self.team).count(), 1)
        self.assertEqual(Container.objects.filter(team=self.other_team).count(), 1)

    def test_no_equipment_types_configured_raises(self):
        EquipmentType.objects.all().delete()
        with self.assertRaises(ValidationError):
            create_or_get_container(team=self.team, user=self.user, number=VALID_A)

    def test_chosen_carrier_is_recorded_as_the_carrier_to_ask(self):
        from apps.scm.containers.models import PlannedContainer

        container, _ = create_or_get_container(team=self.team, user=self.user, number=VALID_A, carrier="maersk")
        planned = PlannedContainer.objects.get(team=self.team, container_number=VALID_A)
        self.assertEqual(planned.carrier, "maersk")
        self.assertEqual(planned.container_id, container.pk)

    def test_choosing_a_carrier_does_not_make_it_a_tracking_source(self):
        """Typing "Maersk" into a form has not made Maersk answer about this box."""
        from apps.scm.tracking.models import TrackingSubscription

        container, _ = create_or_get_container(team=self.team, user=self.user, number=VALID_A, carrier="maersk")
        self.assertFalse(TrackingSubscription.objects.filter(team=self.team, container=container).exists())

    def test_owner_prefix_alone_never_links_a_carrier(self):
        from apps.scm.containers.models import PlannedContainer

        # TRDU is an owner code, not a carrier — nothing may be inferred from it.
        create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        self.assertFalse(PlannedContainer.objects.filter(team=self.team, container_number=VALID_A).exists())

    def test_unknown_carrier_is_ignored_rather_than_guessed(self):
        from apps.scm.containers.models import PlannedContainer

        create_or_get_container(team=self.team, user=self.user, number=VALID_A, carrier="not-a-carrier")
        self.assertFalse(PlannedContainer.objects.filter(team=self.team, container_number=VALID_A).exists())


class PreviewContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Preview", slug="preview-team")
        cls.other_team = Team.objects.create(name="Preview other", slug="preview-other-team")
        cls.user = CustomUser.objects.create_user(username="preview@example.com", password="pass")

    def setUp(self):
        _et()

    def test_classifies_new_existing_and_invalid(self):
        create_or_get_container(team=self.team, user=self.user, number=VALID_B)
        preview = preview_containers(
            team=self.team,
            entries=entries_from_text(f"{VALID_A}\n{VALID_B}\n{INVALID_CHECK_DIGIT}"),
        )
        self.assertEqual([row.state for row in preview.rows], ["new", "exists", "invalid"])
        self.assertEqual((preview.new_count, preview.existing_count, preview.invalid_count), (1, 1, 1))

    def test_duplicates_in_input_are_counted_once(self):
        preview = preview_containers(team=self.team, entries=entries_from_text(f"{VALID_A}\n{VALID_A.lower()}"))
        self.assertEqual(preview.total, 1)
        self.assertEqual(preview.new_count, 1)

    def test_another_teams_container_is_still_new_here(self):
        create_or_get_container(team=self.other_team, user=self.user, number=VALID_A)
        preview = preview_containers(team=self.team, entries=entries_from_text(VALID_A))
        self.assertEqual(preview.new_count, 1)


class BulkCreateContainersTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Bulk", slug="bulk-team")
        cls.other_team = Team.objects.create(name="Bulk other", slug="bulk-other-team")
        cls.user = CustomUser.objects.create_user(username="bulk@example.com", password="pass")

    def setUp(self):
        _et()

    def test_mixed_input_creates_only_valid_new_containers(self):
        create_or_get_container(team=self.team, user=self.user, number=VALID_B)
        result = bulk_create_containers(
            team=self.team,
            user=self.user,
            entries=entries_from_text(f"{VALID_A}\t{VALID_B}\t{INVALID_CHECK_DIGIT}"),
        )
        self.assertEqual(result.created, [VALID_A])
        self.assertEqual(result.existed, [VALID_B])
        self.assertEqual([row.number for row in result.invalid], [INVALID_CHECK_DIGIT])
        self.assertEqual(Container.objects.filter(team=self.team).count(), 2)

    def test_duplicates_in_input_create_one_container(self):
        result = bulk_create_containers(
            team=self.team, user=self.user, entries=entries_from_text(f"{VALID_A};{VALID_A}")
        )
        self.assertEqual(result.created_count, 1)
        self.assertEqual(Container.objects.filter(team=self.team).count(), 1)

    def test_invalid_rows_do_not_stop_the_import(self):
        result = bulk_create_containers(
            team=self.team,
            user=self.user,
            entries=entries_from_text(f"GARBAGE\n{VALID_A}\nBAD-NUMBER\n{VALID_C}"),
        )
        self.assertEqual(sorted(result.created), sorted([VALID_A, VALID_C]))
        self.assertEqual(result.invalid_count, 2)

    def test_import_is_scoped_to_the_importing_team(self):
        bulk_create_containers(team=self.team, user=self.user, entries=entries_from_text(f"{VALID_A}\n{VALID_B}"))
        self.assertEqual(Container.objects.filter(team=self.team).count(), 2)
        self.assertEqual(Container.objects.filter(team=self.other_team).count(), 0)

    def test_carrier_applies_to_every_container_in_the_list(self):
        from apps.scm.containers.models import PlannedContainer

        bulk_create_containers(
            team=self.team,
            user=self.user,
            entries=entries_from_text(f"{VALID_A}\n{VALID_B}", carrier="maersk"),
        )
        self.assertEqual(PlannedContainer.objects.filter(team=self.team, carrier="maersk").count(), 2)


class EntriesFromCsvTest(TestCase):
    def test_container_number_column_only(self):
        entries = entries_from_csv(_csv(f"container_number\n{VALID_A}\n{VALID_B}\n"))
        self.assertEqual(entries, [(VALID_A, ""), (VALID_B, "")])

    def test_optional_carrier_column(self):
        entries = entries_from_csv(_csv(f"container_number,carrier\n{VALID_A},maersk\n{VALID_B},\n"))
        self.assertEqual(entries, [(VALID_A, "maersk"), (VALID_B, "")])

    def test_header_case_and_bom_are_tolerated(self):
        entries = entries_from_csv(_csv(f"﻿Container_Number\n{VALID_A.lower()}\n"))
        self.assertEqual(entries, [(VALID_A, "")])

    def test_missing_container_number_column_raises(self):
        with self.assertRaises(ValidationError):
            entries_from_csv(_csv(f"box_id\n{VALID_A}\n"))

    def test_empty_file_raises(self):
        with self.assertRaises(ValidationError):
            entries_from_csv(_csv("container_number\n"))

    def test_blank_rows_are_ignored(self):
        entries = entries_from_csv(_csv(f"container_number\n{VALID_A}\n\n"))
        self.assertEqual(entries, [(VALID_A, "")])


@override_settings(STORAGES=_TEST_STORAGES)
class IntakeViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.team = Team.objects.create(name="Intake views", slug="intake-views-team")
        cls.user = CustomUser.objects.create_user(username="intake-views@example.com", password="pass")
        cls.team.members.add(cls.user, through_defaults={"role": ROLE_MEMBER})
        cls.other_team = Team.objects.create(name="Intake views other", slug="intake-views-other")
        cls.other_user = CustomUser.objects.create_user(username="intake-other@example.com", password="pass")
        cls.other_team.members.add(cls.other_user, through_defaults={"role": ROLE_MEMBER})

    def setUp(self):
        _et()
        self.client = Client()
        self.client.force_login(self.user)

    # -- single ------------------------------------------------------------

    def test_add_container_opens_the_modal_on_the_single_tab(self):
        response = self.client.get(reverse("containers:create"), HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_modal.html")
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_single.html")
        self.assertContains(response, "Container number")

    def test_valid_single_submit_creates_and_shows_success(self):
        response = self.client.post(
            reverse("containers:create"), data={"container_number": VALID_A.lower()}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_created.html")
        self.assertContains(response, "created")
        self.assertTrue(Container.objects.filter(team=self.team, serial_number="925896").exists())

    def test_invalid_single_submit_re_renders_the_form_with_errors(self):
        response = self.client.post(
            reverse("containers:create"), data={"container_number": INVALID_CHECK_DIGIT}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_single.html")
        self.assertTemplateNotUsed(response, "scm/containers/partials/container_table.html")
        self.assertContains(response, "check digit")
        self.assertFalse(Container.objects.filter(team=self.team).exists())

    def test_duplicate_single_submit_reports_it_without_creating_twice(self):
        create_or_get_container(team=self.team, user=self.user, number=VALID_A)
        response = self.client.post(
            reverse("containers:create"), data={"container_number": VALID_A}, HTTP_HX_REQUEST="true"
        )
        self.assertContains(response, "already exists")
        self.assertEqual(Container.objects.filter(team=self.team).count(), 1)

    def test_number_check_reports_valid_parts(self):
        response = self.client.post(reverse("containers:number_check"), data={"container_number": VALID_A})
        self.assertContains(response, "Valid container number")
        self.assertContains(response, "925896")

    def test_number_check_reports_invalid(self):
        response = self.client.post(reverse("containers:number_check"), data={"container_number": INVALID_CHECK_DIGIT})
        self.assertContains(response, "Invalid container number")

    def test_number_check_says_nothing_for_empty_input(self):
        response = self.client.post(reverse("containers:number_check"), data={"container_number": ""})
        self.assertNotContains(response, "Invalid container number")

    # -- paste -------------------------------------------------------------

    def test_paste_tab_opens(self):
        response = self.client.get(reverse("containers:import_paste"), HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_paste.html")

    def test_paste_shows_a_preview_before_importing(self):
        create_or_get_container(team=self.team, user=self.user, number=VALID_B)
        response = self.client.post(
            reverse("containers:import_paste"),
            data={"numbers": f"{VALID_A}\n{VALID_B}\n{INVALID_CHECK_DIGIT}"},
            HTTP_HX_REQUEST="true",
        )
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_preview.html")
        preview = response.context["preview"]
        self.assertEqual((preview.new_count, preview.existing_count, preview.invalid_count), (1, 1, 1))
        # Nothing is written by a preview.
        self.assertEqual(Container.objects.filter(team=self.team).count(), 1)

    def test_empty_paste_shows_a_form_error(self):
        response = self.client.post(reverse("containers:import_paste"), data={"numbers": "  "}, HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_paste.html")
        self.assertContains(response, "This field is required.")

    def test_paste_of_separators_only_shows_a_form_error(self):
        response = self.client.post(reverse("containers:import_paste"), data={"numbers": ",,;"}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "at least one container number")

    def test_confirm_imports_the_previewed_numbers(self):
        preview_response = self.client.post(
            reverse("containers:import_paste"),
            data={"numbers": f"{VALID_A},{VALID_C},{INVALID_CHECK_DIGIT}"},
            HTTP_HX_REQUEST="true",
        )
        payload = preview_response.context["payload"]
        response = self.client.post(
            reverse("containers:import_confirm"), data={"entries": payload, "tab": "paste"}, HTTP_HX_REQUEST="true"
        )
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_result.html")
        result = response.context["result"]
        self.assertEqual((result.created_count, result.existed_count, result.invalid_count), (2, 0, 1))
        self.assertEqual(Container.objects.filter(team=self.team).count(), 2)

    def test_confirm_with_an_unreadable_payload_imports_nothing(self):
        response = self.client.post(
            reverse("containers:import_confirm"), data={"entries": "not json", "tab": "paste"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Container.objects.filter(team=self.team).exists())

    def test_confirm_is_scoped_to_the_importing_team(self):
        preview_response = self.client.post(
            reverse("containers:import_paste"), data={"numbers": VALID_A}, HTTP_HX_REQUEST="true"
        )
        payload = preview_response.context["payload"]
        self.client.post(reverse("containers:import_confirm"), data={"entries": payload}, HTTP_HX_REQUEST="true")
        self.assertEqual(Container.objects.filter(team=self.team).count(), 1)
        self.assertEqual(Container.objects.filter(team=self.other_team).count(), 0)

    # -- CSV ---------------------------------------------------------------

    def test_csv_tab_opens(self):
        response = self.client.get(reverse("containers:import_csv"), HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_csv.html")

    def test_csv_upload_shows_the_same_preview(self):
        response = self.client.post(
            reverse("containers:import_csv"),
            data={"file": _csv(f"container_number,carrier\n{VALID_A},maersk\n{INVALID_CHECK_DIGIT},\n")},
            HTTP_HX_REQUEST="true",
        )
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_preview.html")
        preview = response.context["preview"]
        self.assertEqual((preview.new_count, preview.invalid_count), (1, 1))
        self.assertEqual(preview.rows[0].carrier, "maersk")

    def test_csv_without_container_number_column_shows_an_error(self):
        response = self.client.post(
            reverse("containers:import_csv"), data={"file": _csv("box\nTRDU9258963\n")}, HTTP_HX_REQUEST="true"
        )
        self.assertTemplateUsed(response, "scm/containers/partials/container_intake_csv.html")
        self.assertContains(response, "container_number")

    def test_non_csv_upload_is_rejected(self):
        upload = SimpleUploadedFile("containers.txt", b"container_number\n", content_type="text/plain")
        response = self.client.post(reverse("containers:import_csv"), data={"file": upload}, HTTP_HX_REQUEST="true")
        self.assertContains(response, "Upload a .csv file.")

    def test_csv_import_creates_containers_for_this_team_only(self):
        preview_response = self.client.post(
            reverse("containers:import_csv"),
            data={"file": _csv(f"container_number\n{VALID_A}\n{VALID_A.lower()}\n{VALID_B}\n")},
            HTTP_HX_REQUEST="true",
        )
        payload = preview_response.context["payload"]
        response = self.client.post(
            reverse("containers:import_confirm"), data={"entries": payload, "tab": "csv"}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.context["result"].created_count, 2)
        self.assertEqual(Container.objects.filter(team=self.team).count(), 2)
        self.assertEqual(Container.objects.filter(team=self.other_team).count(), 0)

    def test_another_team_does_not_see_this_teams_containers_in_a_preview(self):
        bulk_create_containers(team=self.team, user=self.user, entries=entries_from_text(VALID_A))
        other_client = Client()
        other_client.force_login(self.other_user)
        response = other_client.post(
            reverse("containers:import_paste"), data={"numbers": VALID_A}, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.context["preview"].new_count, 1)

    # -- access ------------------------------------------------------------

    def test_intake_urls_require_login(self):
        anonymous = Client()
        for url in [
            reverse("containers:create"),
            reverse("containers:import_paste"),
            reverse("containers:import_csv"),
        ]:
            response = anonymous.get(url)
            self.assertEqual(response.status_code, 302, url)

    def test_confirm_rejects_get(self):
        self.assertEqual(self.client.get(reverse("containers:import_confirm")).status_code, 405)


class CsvReaderReuseTest(TestCase):
    """Container CSV intake must read files with the import app's reader, not its own."""

    def test_uses_the_import_apps_csv_parser(self):
        from apps.scm.imports.parsers import parse_csv_rows

        rows = parse_csv_rows(io.BytesIO(f"container_number\n{VALID_A}\n".encode()))
        self.assertEqual(rows, [{"container_number": VALID_A}])
