"""Unit tests for _user_local_hour used in scheduled sync."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from main.management.commands.check_and_sync_accounts import _user_local_hour


def _mock_user(user_timezone: str = "UTC") -> MagicMock:
    user = MagicMock()
    user.user_timezone = user_timezone
    return user


class UserLocalHourTests(SimpleTestCase):

    def test_utc_user_matches_utc_hour(self):
        utc_now = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("UTC"), utc_now), 14)

    def test_rome_winter_utc_plus_1(self):
        # 14:00 UTC -> 15:00 CET
        utc_now = datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("Europe/Rome"), utc_now), 15)

    def test_rome_summer_utc_plus_2(self):
        # 14:00 UTC -> 16:00 CEST
        utc_now = datetime(2024, 7, 15, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("Europe/Rome"), utc_now), 16)

    def test_new_york_winter_utc_minus_5(self):
        # 14:00 UTC -> 09:00 EST
        utc_now = datetime(2024, 1, 15, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("America/New_York"), utc_now), 9)

    def test_new_york_summer_utc_minus_4(self):
        # 14:00 UTC -> 10:00 EDT
        utc_now = datetime(2024, 7, 15, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("America/New_York"), utc_now), 10)

    def test_tokyo_utc_plus_9(self):
        # 14:00 UTC -> 23:00 JST
        utc_now = datetime(2024, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("Asia/Tokyo"), utc_now), 23)

    def test_midnight_boundary_tokyo(self):
        # 15:00 UTC -> 00:00 JST (next day)
        utc_now = datetime(2024, 6, 15, 15, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("Asia/Tokyo"), utc_now), 0)

    def test_invalid_timezone_falls_back_to_utc(self):
        utc_now = datetime(2024, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("Invalid/Zone"), utc_now), 14)

    def test_day_boundary_crossing(self):
        """A user in UTC-5 at 03:00 UTC should see 22:00 (previous day)."""
        utc_now = datetime(2024, 1, 15, 3, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_user_local_hour(_mock_user("America/New_York"), utc_now), 22)
