"""Unit tests for DST-related invariants across input, storage and display.

Covers:
- Browser offset strings parsed correctly in both winter and summer DST states.
- UTC-stored datetimes display in the correct local time for their own date's
  DST rules (not "today's" DST status).
- Past transactions never shift when DST changes.
- Edge cases at the exact spring-forward / fall-back boundary.
- Middleware activates a DST-aware IANA timezone, not a fixed offset.
- The future-date validator compares UTC instants, not wall-clock values.
"""

import zoneinfo
from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import MagicMock, patch

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from main.middleware import TimezoneMiddleware
from main.models import Transaction

UTC = zoneinfo.ZoneInfo("UTC")
ROME = zoneinfo.ZoneInfo("Europe/Rome")       # CET +1 winter / CEST +2 summer
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")  # EST -5 winter / EDT -4 summer

# ── Europe/Rome 2026 DST transitions ──
# Spring forward: last Sunday of March = 2026-03-29 at 02:00 CET → 03:00 CEST
# Fall back:      last Sunday of October = 2026-10-25 at 03:00 CEST → 02:00 CET


# ---------------------------------------------------------------------------
# 1. Input: browser offset → correct UTC instant
# ---------------------------------------------------------------------------
class TestInputOffsetParsing(SimpleTestCase):
    """parse_datetime must honour the offset in the string, yielding the
    correct UTC instant.  Same wall-clock "12:00" in winter CET (+01:00) and
    summer CEST (+02:00) must produce *different* UTC instants."""

    def test_winter_cet_offset(self):
        dt = parse_datetime("2026-01-15T12:00:00+01:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=1))
        utc = dt.astimezone(dt_tz.utc)
        self.assertEqual(utc.hour, 11)

    def test_summer_cest_offset(self):
        dt = parse_datetime("2026-07-15T12:00:00+02:00")
        self.assertEqual(dt.utcoffset(), timedelta(hours=2))
        utc = dt.astimezone(dt_tz.utc)
        self.assertEqual(utc.hour, 10)

    def test_same_wall_clock_different_utc(self):
        winter = parse_datetime("2026-01-15T12:00:00+01:00").astimezone(dt_tz.utc)
        summer = parse_datetime("2026-07-15T12:00:00+02:00").astimezone(dt_tz.utc)
        self.assertNotEqual(winter.hour, summer.hour)
        self.assertEqual(winter.hour, 11)
        self.assertEqual(summer.hour, 10)

    def test_negative_offset_edt(self):
        dt = parse_datetime("2026-07-15T12:00:00-04:00")
        utc = dt.astimezone(dt_tz.utc)
        self.assertEqual(utc.hour, 16)

    def test_negative_offset_est(self):
        dt = parse_datetime("2026-01-15T12:00:00-05:00")
        utc = dt.astimezone(dt_tz.utc)
        self.assertEqual(utc.hour, 17)


# ---------------------------------------------------------------------------
# 2. Display: stored UTC → correct local time with DST awareness
# ---------------------------------------------------------------------------
class TestDisplayDSTAwareness(SimpleTestCase):
    """timezone.localtime must use the DST rules of the *transaction's own
    date*, not the current date.  A January instant should show CET (+1);
    a July instant should show CEST (+2) — same IANA timezone, different offsets."""

    def test_winter_utc_to_rome_cet(self):
        stored = datetime(2026, 1, 15, 11, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 12)
        self.assertEqual(local.utcoffset(), timedelta(hours=1))

    def test_summer_utc_to_rome_cest(self):
        stored = datetime(2026, 7, 15, 10, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 12)
        self.assertEqual(local.utcoffset(), timedelta(hours=2))

    def test_winter_utc_to_ny_est(self):
        stored = datetime(2026, 1, 15, 17, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, NEW_YORK)
        self.assertEqual(local.hour, 12)
        self.assertEqual(local.utcoffset(), timedelta(hours=-5))

    def test_summer_utc_to_ny_edt(self):
        stored = datetime(2026, 7, 15, 16, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, NEW_YORK)
        self.assertEqual(local.hour, 12)
        self.assertEqual(local.utcoffset(), timedelta(hours=-4))


# ---------------------------------------------------------------------------
# 3. Stability: past transactions do NOT shift
# ---------------------------------------------------------------------------
class TestPastTransactionsDoNotShift(SimpleTestCase):
    """A fixed UTC instant must *always* map to the same local representation.
    These assertions are deterministic: they hold regardless of whether the
    test suite runs in January or July."""

    def test_january_transaction_always_shows_noon_rome(self):
        stored = datetime(2026, 1, 15, 11, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 12)
        self.assertEqual(local.minute, 0)
        self.assertEqual(local.day, 15)

    def test_july_transaction_always_shows_noon_rome(self):
        stored = datetime(2026, 7, 15, 10, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 12)
        self.assertEqual(local.minute, 0)
        self.assertEqual(local.day, 15)

    def test_roundtrip_winter_input_to_display(self):
        """Simulate the full lifecycle: browser sends +01:00 string → stored
        as UTC → displayed back in Rome → same wall-clock."""
        browser_str = "2026-01-15T14:30:00+01:00"
        parsed = parse_datetime(browser_str)
        stored_utc = parsed.astimezone(dt_tz.utc)
        displayed = timezone.localtime(stored_utc, ROME)
        self.assertEqual(displayed.hour, 14)
        self.assertEqual(displayed.minute, 30)

    def test_roundtrip_summer_input_to_display(self):
        browser_str = "2026-07-15T14:30:00+02:00"
        parsed = parse_datetime(browser_str)
        stored_utc = parsed.astimezone(dt_tz.utc)
        displayed = timezone.localtime(stored_utc, ROME)
        self.assertEqual(displayed.hour, 14)
        self.assertEqual(displayed.minute, 30)


# ---------------------------------------------------------------------------
# 4. DST transition boundary
# ---------------------------------------------------------------------------
class TestDSTTransitionBoundary(SimpleTestCase):
    """Edge cases at the exact spring-forward and fall-back moments
    for Europe/Rome in 2026."""

    # ── Spring forward: 2026-03-29 02:00 CET → 03:00 CEST ──

    def test_just_before_spring_forward(self):
        """01:59 CET (UTC+1) = 00:59 UTC → still displayed as CET."""
        stored = datetime(2026, 3, 29, 0, 59, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 1)
        self.assertEqual(local.minute, 59)
        self.assertEqual(local.utcoffset(), timedelta(hours=1))

    def test_just_after_spring_forward(self):
        """03:00 CEST (UTC+2) = 01:00 UTC → displayed as CEST."""
        stored = datetime(2026, 3, 29, 1, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 3)
        self.assertEqual(local.minute, 0)
        self.assertEqual(local.utcoffset(), timedelta(hours=2))

    def test_nonexistent_wall_clock_skipped(self):
        """02:30 local on spring-forward day doesn't exist.  The UTC instant
        01:30 = 03:30 CEST, not 02:30 CET — proving the gap is handled."""
        stored = datetime(2026, 3, 29, 1, 30, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 3)
        self.assertEqual(local.minute, 30)
        self.assertEqual(local.utcoffset(), timedelta(hours=2))

    # ── Fall back: 2026-10-25 03:00 CEST → 02:00 CET ──

    def test_just_before_fall_back(self):
        """02:59 CEST (UTC+2) = 00:59 UTC → still CEST."""
        stored = datetime(2026, 10, 25, 0, 59, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 2)
        self.assertEqual(local.minute, 59)
        self.assertEqual(local.utcoffset(), timedelta(hours=2))

    def test_just_after_fall_back(self):
        """02:00 CET (UTC+1) = 01:00 UTC → CET after clocks fall back."""
        stored = datetime(2026, 10, 25, 1, 0, 0, tzinfo=dt_tz.utc)
        local = timezone.localtime(stored, ROME)
        self.assertEqual(local.hour, 2)
        self.assertEqual(local.minute, 0)
        self.assertEqual(local.utcoffset(), timedelta(hours=1))


# ---------------------------------------------------------------------------
# 5. Middleware activates a DST-aware (IANA) timezone, not a fixed offset
# ---------------------------------------------------------------------------
class TestMiddlewareDSTAwareActivation(SimpleTestCase):
    """After the middleware activates "Europe/Rome", converting a winter and
    a summer datetime must yield different UTC offsets.  This would fail if
    the middleware used a fixed-offset timezone like UTC+1."""

    def setUp(self):
        self.captured_tz = None

        def capture(request):
            self.captured_tz = timezone.get_current_timezone()
            return MagicMock()

        self.middleware = TimezoneMiddleware(capture)

    def _make_request(self, user_timezone):
        req = MagicMock()
        req.user.is_authenticated = True
        req.user.user_timezone = user_timezone
        return req

    def test_rome_winter_and_summer_offsets_differ(self):
        self.middleware(self._make_request("Europe/Rome"))
        tz = self.captured_tz

        winter_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt_tz.utc)
        summer_dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt_tz.utc)

        winter_local = timezone.localtime(winter_dt, tz)
        summer_local = timezone.localtime(summer_dt, tz)

        self.assertEqual(winter_local.utcoffset(), timedelta(hours=1))
        self.assertEqual(summer_local.utcoffset(), timedelta(hours=2))

    def test_new_york_winter_and_summer_offsets_differ(self):
        self.middleware(self._make_request("America/New_York"))
        tz = self.captured_tz

        winter_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=dt_tz.utc)
        summer_dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt_tz.utc)

        winter_local = timezone.localtime(winter_dt, tz)
        summer_local = timezone.localtime(summer_dt, tz)

        self.assertEqual(winter_local.utcoffset(), timedelta(hours=-5))
        self.assertEqual(summer_local.utcoffset(), timedelta(hours=-4))


# ---------------------------------------------------------------------------
# 6. Future-date validator with offset-aware datetimes
# ---------------------------------------------------------------------------
class TestFutureValidatorWithOffsets(SimpleTestCase):
    """Transaction.validate_not_future compares against timezone.now() (UTC).
    It must compare actual UTC instants, not raw wall-clock values."""

    @patch("main.models.timezone.now")
    def test_past_utc_instant_passes(self, mock_now):
        mock_now.return_value = datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt_tz.utc)
        past = datetime(2026, 7, 15, 11, 59, 0, tzinfo=dt_tz.utc)
        Transaction.validate_not_future(past)

    @patch("main.models.timezone.now")
    def test_future_utc_instant_raises(self, mock_now):
        mock_now.return_value = datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt_tz.utc)
        future = datetime(2026, 7, 15, 12, 1, 0, tzinfo=dt_tz.utc)
        with self.assertRaises(ValidationError):
            Transaction.validate_not_future(future)

    @patch("main.models.timezone.now")
    def test_offset_aware_past_instant_passes(self, mock_now):
        """A wall-clock value that *looks* like it's in the future (22:37+01:00)
        but whose UTC instant (21:37Z) is in the past relative to now (21:38Z)."""
        mock_now.return_value = datetime(2026, 2, 17, 21, 38, 0, tzinfo=dt_tz.utc)
        value = parse_datetime("2026-02-17T22:37:00+01:00")
        Transaction.validate_not_future(value)

    @patch("main.models.timezone.now")
    def test_offset_aware_future_instant_raises(self, mock_now):
        """Wall-clock looks small (10:00-05:00) but UTC instant (15:00Z) is
        after now (14:59Z)."""
        mock_now.return_value = datetime(2026, 7, 15, 14, 59, 0, tzinfo=dt_tz.utc)
        value = parse_datetime("2026-07-15T10:00:00-05:00")
        with self.assertRaises(ValidationError):
            Transaction.validate_not_future(value)

    @patch("main.models.timezone.now")
    def test_dst_boundary_winter_offset_not_misread(self, mock_now):
        """CET offset +01:00: "23:30+01:00" = 22:30 UTC, now is 22:31 UTC → passes."""
        mock_now.return_value = datetime(2026, 1, 15, 22, 31, 0, tzinfo=dt_tz.utc)
        value = parse_datetime("2026-01-15T23:30:00+01:00")
        Transaction.validate_not_future(value)

    @patch("main.models.timezone.now")
    def test_dst_boundary_summer_offset_not_misread(self, mock_now):
        """CEST offset +02:00: "23:30+02:00" = 21:30 UTC, now is 21:31 UTC → passes."""
        mock_now.return_value = datetime(2026, 7, 15, 21, 31, 0, tzinfo=dt_tz.utc)
        value = parse_datetime("2026-07-15T23:30:00+02:00")
        Transaction.validate_not_future(value)
