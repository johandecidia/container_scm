"""Tests for Business Central OAuth2 client-credentials authentication.

All HTTP is mocked — no live Business Central account or network access.
"""

from unittest import mock

import requests
from django.test import SimpleTestCase

from apps.scm.integrations.business_systems.business_central.auth import BusinessCentralAuth
from apps.scm.integrations.business_systems.business_central.exceptions import (
    BusinessCentralAuthenticationError,
    BusinessCentralConfigurationError,
    BusinessCentralConnectionError,
)

_AUTH_KWARGS = {"tenant_id": "tenant-123", "client_id": "client-abc", "client_secret": "shh-secret"}


def _response(status_code=200, json_data=None, raise_json=False):
    resp = mock.Mock()
    resp.status_code = status_code
    if raise_json:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_data or {}
    return resp


class BusinessCentralAuthTest(SimpleTestCase):
    def _auth(self, **overrides):
        return BusinessCentralAuth(**{**_AUTH_KWARGS, **overrides})

    def test_missing_tenant_raises_configuration_error(self):
        with self.assertRaises(BusinessCentralConfigurationError):
            BusinessCentralAuth(tenant_id="", client_id="a", client_secret="b")

    def test_missing_secret_raises_configuration_error(self):
        with self.assertRaises(BusinessCentralConfigurationError):
            BusinessCentralAuth(tenant_id="t", client_id="a", client_secret="")

    def test_token_endpoint_built_from_tenant(self):
        auth = self._auth()
        self.assertEqual(
            auth.token_endpoint,
            "https://login.microsoftonline.com/tenant-123/oauth2/v2.0/token",
        )

    def test_successful_token_acquisition(self):
        auth = self._auth()
        with mock.patch(
            "requests.post", return_value=_response(200, {"access_token": "tok-1", "expires_in": 3600})
        ) as post:
            token = auth.get_access_token()
        self.assertEqual(token, "tok-1")
        post.assert_called_once()
        # client-credentials grant with the BC scope
        _, kwargs = post.call_args
        self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")
        self.assertIn("businesscentral", kwargs["data"]["scope"])

    def test_token_is_reused_before_expiry(self):
        auth = self._auth()
        with mock.patch(
            "requests.post", return_value=_response(200, {"access_token": "tok-1", "expires_in": 3600})
        ) as post:
            first = auth.get_access_token()
            second = auth.get_access_token()
        self.assertEqual(first, second)
        post.assert_called_once()

    def test_token_renewed_after_expiry(self):
        auth = self._auth()
        responses = [
            _response(200, {"access_token": "tok-1", "expires_in": 3600}),
            _response(200, {"access_token": "tok-2", "expires_in": 3600}),
        ]
        with mock.patch("requests.post", side_effect=responses) as post:
            first = auth.get_access_token()
            auth._expires_at = 0.0  # force expiry
            second = auth.get_access_token()
        self.assertEqual(first, "tok-1")
        self.assertEqual(second, "tok-2")
        self.assertEqual(post.call_count, 2)

    def test_invalidate_forces_new_token(self):
        auth = self._auth()
        responses = [
            _response(200, {"access_token": "tok-1", "expires_in": 3600}),
            _response(200, {"access_token": "tok-2", "expires_in": 3600}),
        ]
        with mock.patch("requests.post", side_effect=responses) as post:
            auth.get_access_token()
            auth.invalidate_token()
            auth.get_access_token()
        self.assertEqual(post.call_count, 2)

    def test_bad_credentials_raise_authentication_error(self):
        auth = self._auth()
        with (
            mock.patch("requests.post", return_value=_response(401, {"error": "invalid_client"})),
            self.assertRaises(BusinessCentralAuthenticationError),
        ):
            auth.get_access_token()

    def test_timeout_raises_connection_error(self):
        auth = self._auth()
        with (
            mock.patch("requests.post", side_effect=requests.Timeout()),
            self.assertRaises(BusinessCentralConnectionError),
        ):
            auth.get_access_token()

    def test_missing_token_in_response_raises(self):
        auth = self._auth()
        with (
            mock.patch("requests.post", return_value=_response(200, {"expires_in": 3600})),
            self.assertRaises(BusinessCentralAuthenticationError),
        ):
            auth.get_access_token()

    def test_invalid_json_raises_authentication_error(self):
        auth = self._auth()
        with (
            mock.patch("requests.post", return_value=_response(200, raise_json=True)),
            self.assertRaises(BusinessCentralAuthenticationError),
        ):
            auth.get_access_token()

    def test_token_never_logged(self):
        auth = self._auth()
        with (
            mock.patch(
                "requests.post", return_value=_response(200, {"access_token": "super-secret-token", "expires_in": 3600})
            ),
            self.assertLogs("apps.scm.integrations.business_systems.business_central.auth", level="INFO") as cm,
        ):
            auth.get_access_token()
        joined = "\n".join(cm.output)
        self.assertNotIn("super-secret-token", joined)
