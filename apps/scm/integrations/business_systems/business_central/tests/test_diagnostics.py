"""Parsing Business Central permission errors into safe, admin-facing diagnostics."""

from django.test import SimpleTestCase

from apps.scm.integrations.business_systems.business_central.diagnostics import (
    failure_diagnostic,
    format_diagnostic,
    parse_permission_error,
)
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralAuthenticationError,
    BusinessCentralPermissionError,
    BusinessCentralResponseError,
)

CORRELATION_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
CURRENT_MESSAGE = (
    "Sorry, the current permissions prevented the action. "
    "(TableData 5496 Purchase Order Entity Buffer Read: Base Application)\r\n\r\n"
    f"To view details about your permissions, see the Effective Permissions page.  CorrelationId:  {CORRELATION_ID}."
)
LEGACY_MESSAGE = (
    "You do not have the following permissions on TableData Purchase Header: Read\r\n\r\n"
    "To view details about your permissions, see the Effective Permissions page."
)


class ParsePermissionErrorTest(SimpleTestCase):
    def test_current_format(self):
        self.assertEqual(
            parse_permission_error(CURRENT_MESSAGE),
            {
                "object_type": "TableData",
                "object_id": "5496",
                "object_name": "Purchase Order Entity Buffer",
                "permission": "Read",
                "app": "Base Application",
                "correlation_id": CORRELATION_ID,
            },
        )

    def test_current_format_page_execute_without_app(self):
        detail = parse_permission_error(
            "Sorry, the current permissions prevented the action. (Page 30066 APIV2 - Purchase Orders Execute)"
        )
        self.assertEqual(detail["object_type"], "Page")
        self.assertEqual(detail["object_id"], "30066")
        self.assertEqual(detail["object_name"], "APIV2 - Purchase Orders")
        self.assertEqual(detail["permission"], "Execute")
        self.assertNotIn("app", detail)

    def test_legacy_format(self):
        self.assertEqual(
            parse_permission_error(LEGACY_MESSAGE),
            {"object_type": "TableData", "object_name": "Purchase Header", "permission": "Read"},
        )

    def test_api_page_permission_check(self):
        detail = parse_permission_error("You do not have permissions to read Purchase Orders.")
        self.assertEqual(detail, {"object_name": "Purchase Orders", "permission": "Read"})

    def test_unrecognised_message_yields_nothing(self):
        self.assertEqual(parse_permission_error("Something went wrong on the server."), {})
        self.assertEqual(parse_permission_error(""), {})
        self.assertEqual(parse_permission_error(None), {})

    def test_unsafe_values_are_never_extracted(self):
        message = (
            "Bearer eyJhbGciOi.secret-token (TableData 18 <script>alert(1)</script> Read: App) "
            "client_secret=super-secret-value"
        )
        detail = parse_permission_error(message)
        self.assertEqual(detail, {})

    def test_unsafe_app_name_is_dropped(self):
        detail = parse_permission_error("(TableData 18 Customer Read: <img src=x>)")
        self.assertEqual(detail["object_name"], "Customer")
        self.assertNotIn("app", detail)

    def test_non_guid_correlation_id_ignored(self):
        self.assertNotIn("correlation_id", parse_permission_error("CorrelationId: not-a-guid"))


class FailureDiagnosticTest(SimpleTestCase):
    def _permission_error(self, permission):
        return BusinessCentralPermissionError(
            "denied",
            status_code=403,
            error_code="Internal_ServerError",
            endpoint="companies(abc)/purchaseOrders",
            permission=permission,
        )

    def test_permission_error_diagnostic_and_format(self):
        exc = self._permission_error(parse_permission_error(CURRENT_MESSAGE))
        diagnostic = failure_diagnostic(exc)
        self.assertEqual(diagnostic["endpoint"], "companies(abc)/purchaseOrders")
        self.assertEqual(diagnostic["status_code"], 403)
        self.assertEqual(diagnostic["object_id"], "5496")
        self.assertEqual(
            format_diagnostic(diagnostic),
            "Failed request: GET companies(abc)/purchaseOrders (HTTP 403). "
            'Missing permission: TableData 5496 "Purchase Order Entity Buffer": Read (Base Application). '
            f"CorrelationId: {CORRELATION_ID}.",
        )

    def test_permission_error_without_detail_says_so(self):
        text = format_diagnostic(failure_diagnostic(self._permission_error({})))
        self.assertIn("GET companies(abc)/purchaseOrders (HTTP 403)", text)
        self.assertIn("did not name the denied object", text)

    def test_other_response_error_names_request_only(self):
        exc = BusinessCentralResponseError("bad", status_code=400, endpoint="companies")
        self.assertEqual(format_diagnostic(failure_diagnostic(exc)), "Failed request: GET companies (HTTP 400).")

    def test_errors_without_a_request_have_no_diagnostic(self):
        self.assertEqual(failure_diagnostic(BusinessCentralAuthenticationError("token")), {})
        self.assertEqual(failure_diagnostic(BusinessCentralResponseError("bad")), {})
        self.assertEqual(format_diagnostic({}), "")
