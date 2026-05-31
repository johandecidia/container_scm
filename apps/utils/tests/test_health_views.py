"""Tests for health check endpoints."""

import json

from django.test import Client, TestCase, override_settings


class HealthViewTest(TestCase):
    def setUp(self):
        self.client = Client()

    def test_health_returns_200(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)

    def test_health_returns_json(self):
        response = self.client.get("/health/")
        data = json.loads(response.content)
        self.assertEqual(data, {"status": "ok"})

    def test_health_db_returns_200(self):
        response = self.client.get("/health/db/")
        self.assertEqual(response.status_code, 200)

    def test_health_db_returns_json(self):
        response = self.client.get("/health/db/")
        data = json.loads(response.content)
        self.assertEqual(data, {"database": "ok"})

    def test_health_no_auth_required(self):
        """Health endpoints must be accessible without authentication."""
        anon = Client()
        self.assertEqual(anon.get("/health/").status_code, 200)
        self.assertEqual(anon.get("/health/db/").status_code, 200)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class HealthRedisViewTest(TestCase):
    """Redis health check uses the default cache; override to LocMem for unit tests."""

    def test_health_redis_returns_200(self):
        response = self.client.get("/health/redis/")
        self.assertEqual(response.status_code, 200)

    def test_health_redis_returns_json(self):
        response = self.client.get("/health/redis/")
        data = json.loads(response.content)
        self.assertEqual(data, {"redis": "ok"})
