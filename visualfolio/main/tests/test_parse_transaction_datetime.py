"""Unit tests for _parse_transaction_datetime in the GoCardless mapping module."""

import zoneinfo
from datetime import date, datetime, time, timezone

from django.test import SimpleTestCase

from main.services.stateful.gocardless.mapping import _parse_transaction_datetime

UTC = zoneinfo.ZoneInfo("UTC")
ROME = zoneinfo.ZoneInfo("Europe/Rome")           # UTC+1 winter / UTC+2 summer
TOKYO = zoneinfo.ZoneInfo("Asia/Tokyo")            # UTC+9
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")   # UTC-5 winter / UTC-4 summer

MIDDAY = time(12, 0)


class TestIso8601WithTimezone(SimpleTestCase):
    """Full ISO 8601 strings that include timezone info."""

    def test_utc_z_suffix(self):
        result = _parse_transaction_datetime("2024-02-27T15:05:06.123Z", "t", ROME)
        self.assertEqual(result, datetime(2024, 2, 27, 15, 5, 6, 123000, tzinfo=UTC))

    def test_positive_offset(self):
        result = _parse_transaction_datetime("2024-02-27T15:05:06+02:00", "t", UTC)
        self.assertEqual(result, datetime(2024, 2, 27, 13, 5, 6, tzinfo=UTC))

    def test_negative_offset(self):
        result = _parse_transaction_datetime("2024-07-15T10:30:00-05:00", "t", TOKYO)
        self.assertEqual(result, datetime(2024, 7, 15, 15, 30, 0, tzinfo=UTC))

    def test_user_tz_ignored_when_offset_present(self):
        r1 = _parse_transaction_datetime("2024-01-01T12:00:00+00:00", "t", ROME)
        r2 = _parse_transaction_datetime("2024-01-01T12:00:00+00:00", "t", TOKYO)
        self.assertEqual(r1, r2)


class TestNaiveDatetime(SimpleTestCase):
    """ISO 8601 datetime without timezone -- should assume user_tz."""

    def test_assumes_rome(self):
        result = _parse_transaction_datetime("2024-02-27T15:05:06", "t", ROME)
        # Rome UTC+1 in Feb -> 14:05:06 UTC
        self.assertEqual(result, datetime(2024, 2, 27, 14, 5, 6, tzinfo=UTC))

    def test_assumes_tokyo(self):
        result = _parse_transaction_datetime("2024-02-27T15:05:06", "t", TOKYO)
        # Tokyo UTC+9 -> 06:05:06 UTC
        self.assertEqual(result, datetime(2024, 2, 27, 6, 5, 6, tzinfo=UTC))

    def test_assumes_utc(self):
        result = _parse_transaction_datetime("2024-02-27T15:05:06", "t", UTC)
        self.assertEqual(result, datetime(2024, 2, 27, 15, 5, 6, tzinfo=UTC))

    def test_with_microseconds(self):
        result = _parse_transaction_datetime("2024-02-27T15:05:06.123456", "t", UTC)
        self.assertEqual(result, datetime(2024, 2, 27, 15, 5, 6, 123456, tzinfo=UTC))


class TestDateOnly(SimpleTestCase):
    """Date-only strings -- should produce midday in user_tz, converted to UTC."""

    def test_rome_winter(self):
        result = _parse_transaction_datetime("2024-02-27", "t", ROME)
        # Midday Rome (CET, UTC+1 in Feb) -> 11:00 UTC
        self.assertEqual(result, datetime(2024, 2, 27, 11, 0, 0, tzinfo=UTC))

    def test_tokyo(self):
        result = _parse_transaction_datetime("2024-02-27", "t", TOKYO)
        # Midday Tokyo (UTC+9) -> 03:00 UTC
        self.assertEqual(result, datetime(2024, 2, 27, 3, 0, 0, tzinfo=UTC))

    def test_utc(self):
        result = _parse_transaction_datetime("2024-02-27", "t", UTC)
        self.assertEqual(result, datetime(2024, 2, 27, 12, 0, 0, tzinfo=UTC))

    def test_new_york_winter(self):
        result = _parse_transaction_datetime("2024-02-27", "t", NEW_YORK)
        # Midday EST (UTC-5) -> 17:00 UTC
        self.assertEqual(result, datetime(2024, 2, 27, 17, 0, 0, tzinfo=UTC))

    def test_new_york_summer_dst(self):
        result = _parse_transaction_datetime("2024-07-15", "t", NEW_YORK)
        # Midday EDT (UTC-4) -> 16:00 UTC
        self.assertEqual(result, datetime(2024, 7, 15, 16, 0, 0, tzinfo=UTC))

    def test_date_does_not_cross_day_boundary(self):
        """Midday prevents the UTC date from drifting to a different calendar day."""
        result = _parse_transaction_datetime("2024-02-27", "t", TOKYO)
        self.assertEqual(result.date(), date(2024, 2, 27))


class TestDateutilFallback(SimpleTestCase):
    """Non-standard date formats handled by the dateutil fallback."""

    def test_dd_mm_yyyy_slash(self):
        result = _parse_transaction_datetime("27/02/2024", "t", ROME)
        # Date-only via dateutil -> midday Rome (CET) -> 11:00 UTC
        self.assertEqual(result, datetime(2024, 2, 27, 11, 0, 0, tzinfo=UTC))

    def test_month_name_format(self):
        result = _parse_transaction_datetime("Feb 27, 2024", "t", UTC)
        self.assertEqual(result, datetime(2024, 2, 27, 12, 0, 0, tzinfo=UTC))

    def test_dateutil_with_time_treats_as_full_datetime(self):
        result = _parse_transaction_datetime("27 Feb 2024 15:30", "t", ROME)
        # Has `:` -> treated as full naive datetime, assume Rome (UTC+1)
        self.assertEqual(result, datetime(2024, 2, 27, 14, 30, 0, tzinfo=UTC))


def _is_utc(dt: datetime) -> bool:
    """Check that a datetime is UTC-aware (offset == 0) regardless of tzinfo type."""
    return dt.tzinfo is not None and dt.utcoffset().total_seconds() == 0


class TestNoneAndUnparseable(SimpleTestCase):
    """Edge cases: None, empty, garbage strings."""

    def test_none_returns_utc_midday_today(self):
        result = _parse_transaction_datetime(None, "t", UTC)
        self.assertTrue(_is_utc(result))
        self.assertEqual(result.time(), MIDDAY)
        self.assertEqual(result.date(), datetime.now(timezone.utc).date())

    def test_empty_string_returns_utc_aware(self):
        result = _parse_transaction_datetime("", "t", UTC)
        self.assertTrue(_is_utc(result))

    def test_whitespace_returns_utc_aware(self):
        result = _parse_transaction_datetime("   ", "t", UTC)
        self.assertTrue(_is_utc(result))

    def test_garbage_returns_midday_today(self):
        result = _parse_transaction_datetime("not-a-date-at-all", "t", ROME)
        self.assertTrue(_is_utc(result))
        # Midday in Rome today, converted to UTC
        today_rome = datetime.now(timezone.utc).astimezone(ROME).date()
        expected = datetime.combine(today_rome, MIDDAY, tzinfo=ROME).astimezone(timezone.utc)
        self.assertEqual(result, expected)

    def test_none_with_nondefault_tz(self):
        result = _parse_transaction_datetime(None, "t", TOKYO)
        self.assertTrue(_is_utc(result))
        # Verify the midday is in Tokyo, not UTC
        today_tokyo = datetime.now(timezone.utc).astimezone(TOKYO).date()
        expected = datetime.combine(today_tokyo, MIDDAY, tzinfo=TOKYO).astimezone(timezone.utc)
        self.assertEqual(result, expected)


class TestResultInvariants(SimpleTestCase):
    """Every return value must be a UTC-aware datetime regardless of input."""

    CASES = [
        "2024-02-27T15:05:06.123Z",
        "2024-02-27T15:05:06+02:00",
        "2024-02-27T15:05:06",
        "2024-02-27",
        "27/02/2024",
        "",
        None,
    ]

    def test_all_results_are_utc_aware(self):
        for raw in self.CASES:
            with self.subTest(raw=raw):
                result = _parse_transaction_datetime(raw, "inv", ROME)
                self.assertIsNotNone(result.tzinfo, f"Naive result for {raw!r}")
                self.assertEqual(
                    result.utcoffset().total_seconds(), 0,
                    f"Non-UTC offset for {raw!r}",
                )
