"""Unit tests for TimezoneMiddleware."""

import zoneinfo
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from main.middleware import TimezoneMiddleware


def _make_request(*, authenticated: bool, user_timezone: str = "UTC"):
    """Build a mock request with an optionally authenticated user."""
    request = MagicMock()
    request.user.is_authenticated = authenticated
    if authenticated:
        request.user.user_timezone = user_timezone
    return request


class TimezoneMiddlewareActivationTests(SimpleTestCase):
    """Verify that the middleware activates the correct timezone."""

    def setUp(self):
        self.activated_tz = None

        def capture_response(request):
            self.activated_tz = timezone.get_current_timezone()
            return MagicMock()

        self.middleware = TimezoneMiddleware(capture_response)

    def test_activates_user_timezone_rome(self):
        request = _make_request(authenticated=True, user_timezone="Europe/Rome")
        self.middleware(request)
        self.assertEqual(str(self.activated_tz), "Europe/Rome")

    def test_activates_user_timezone_tokyo(self):
        request = _make_request(authenticated=True, user_timezone="Asia/Tokyo")
        self.middleware(request)
        self.assertEqual(str(self.activated_tz), "Asia/Tokyo")

    def test_default_utc(self):
        request = _make_request(authenticated=True, user_timezone="UTC")
        self.middleware(request)
        self.assertEqual(str(self.activated_tz), "UTC")

    def test_invalid_timezone_falls_back_to_utc(self):
        request = _make_request(authenticated=True, user_timezone="Invalid/Zone")
        self.middleware(request)
        self.assertEqual(str(self.activated_tz), "UTC")

    def test_anonymous_deactivates(self):
        request = _make_request(authenticated=False)
        self.middleware(request)
        # After deactivate(), get_current_timezone() returns settings.TIME_ZONE
        self.assertEqual(str(self.activated_tz), "UTC")

    def test_sequential_requests_do_not_leak(self):
        req_rome = _make_request(authenticated=True, user_timezone="Europe/Rome")
        self.middleware(req_rome)
        self.assertEqual(str(self.activated_tz), "Europe/Rome")

        req_anon = _make_request(authenticated=False)
        self.middleware(req_anon)
        self.assertEqual(str(self.activated_tz), "UTC")
