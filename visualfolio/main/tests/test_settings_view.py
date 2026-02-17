"""Unit tests for timezone handling in SettingsView."""

from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class SettingsViewTimezoneTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="tzuser", password="pass")
        self.client.login(username="tzuser", password="pass")

    def test_saves_valid_timezone(self):
        self.client.post("/settings/", {"user_timezone": "Europe/Rome"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.user_timezone, "Europe/Rome")

    def test_saves_utc(self):
        self.user.user_timezone = "Asia/Tokyo"
        self.user.save()
        self.client.post("/settings/", {"user_timezone": "UTC"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.user_timezone, "UTC")

    def test_ignores_invalid_timezone(self):
        self.client.post("/settings/", {"user_timezone": "Invalid/Zone"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.user_timezone, "UTC")  # default unchanged

    def test_ignores_empty_timezone(self):
        self.user.user_timezone = "Europe/Rome"
        self.user.save()
        self.client.post("/settings/", {"user_timezone": ""})
        self.user.refresh_from_db()
        self.assertEqual(self.user.user_timezone, "Europe/Rome")

    def test_ignores_missing_timezone_field(self):
        self.user.user_timezone = "Asia/Tokyo"
        self.user.save()
        self.client.post("/settings/", {})
        self.user.refresh_from_db()
        self.assertEqual(self.user.user_timezone, "Asia/Tokyo")

    def test_get_returns_200(self):
        response = self.client.get("/settings/")
        self.assertEqual(response.status_code, 200)

    def test_get_contains_timezone_select(self):
        response = self.client.get("/settings/")
        self.assertContains(response, 'name="user_timezone"')
        self.assertContains(response, "Europe/Rome")


class UserTimezoneModelDefault(TestCase):

    def test_default_is_utc(self):
        user = User.objects.create_user(username="fresh", password="pass")
        self.assertEqual(user.user_timezone, "UTC")
