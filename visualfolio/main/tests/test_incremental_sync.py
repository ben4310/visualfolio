"""
Tests for incremental sync logic in TransactionSyncService.

Covers:
  - Gap detection (FetchExceedsBankHistoryLimit)
  - Walk-back stitch validation (recent-first, fallback, exhaustion)
  - Atomic apply boundaries (only rewrites data after stitch point)
"""

import uuid
import zoneinfo
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from django.test import TestCase

from main.models import (
    Account, AccountAsset, Asset, AssetClass, BalanceHistory, CustomUser, Transaction,
)
from main.services.stateful.gocardless.sync import (
    BalanceReconciliationError,
    FetchExceedsBankHistoryLimit,
    TransactionSyncService,
)


def _d(offset: int, anchor: date | None = None) -> date:
    """Shorthand: date relative to anchor (default: 2025-06-15)."""
    return (anchor or date(2025, 6, 15)) + timedelta(days=offset)


def _dt(d: date, hour: int = 12) -> datetime:
    """Shorthand: timezone-aware datetime at given hour on given date."""
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=timezone.utc)


class IncrementalSyncTestBase(TestCase):
    """Shared setup: user, account, asset, account_asset, and helpers."""

    @classmethod
    def setUpTestData(cls):
        cls.asset_class, _ = AssetClass.objects.get_or_create(
            name="Currency", defaults={"color": "#000000", "volatility_index": 0.0},
        )
        cls.asset, _ = Asset.objects.get_or_create(
            code="EUR", defaults={"name": "Euro", "asset_class": cls.asset_class, "is_liquid": True},
        )

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="syncuser", password="pw")
        self.account = Account.objects.create(
            user=self.user,
            name="TestBank",
            country="IT",
            requisition_id=uuid.uuid4(),
            transaction_total_days=90,
        )
        self.account_asset = AccountAsset.objects.create(
            account=self.account,
            asset=self.asset,
            gocardless_account_id="gc-test-123",
        )
        self.service = TransactionSyncService(client=MagicMock())
        self.user_tz = zoneinfo.ZoneInfo("UTC")

    def _seed_balance_history(self, start: date, end: date, balance: float):
        """Create contiguous BalanceHistory from start to end (inclusive)."""
        entries = []
        d = start
        while d <= end:
            entries.append(BalanceHistory(
                account=self.account, asset=self.asset, date=d, balance=balance,
            ))
            d += timedelta(days=1)
        BalanceHistory.objects.bulk_create(entries)

    def _seed_transactions(self, txn_specs: list[tuple[date, float]]):
        """Create transactions from (date, amount) pairs."""
        for txn_date, amount in txn_specs:
            Transaction.objects_extended.create(
                account=self.account,
                asset=self.asset,
                entity="Test",
                amount=amount,
                datetime=_dt(txn_date),
            )

    def _mock_fetch(self, api_balance: float, transactions: list[Transaction]):
        """Patch _fetch_and_filter to return given balance and transactions."""
        return patch.object(
            self.service, "_fetch_and_filter",
            return_value=(api_balance, transactions, {}, {}),
        )

    def _mock_fetch_with_txn_specs(self, api_balance: float, txn_specs: list[tuple[date, float]]):
        """Patch _fetch_and_filter with Transaction objects built from (date, amount) specs."""
        txns = [
            Transaction(
                account=self.account,
                asset=self.asset,
                entity="API",
                amount=amount,
                datetime=_dt(txn_date),
            )
            for txn_date, amount in txn_specs
        ]
        return self._mock_fetch(api_balance, txns)


# ─── Gap detection ───────────────────────────────────────────────────────────


class TestFetchExceedsBankHistoryLimit(IncrementalSyncTestBase):
    """
    The gap check ensures that last_complete_stored_balance_date >= earliest_backcomputable_balance_date.

    Given transaction_total_days=90:
      - earliest_available_transactions_date = today - 90
      - earliest_backcomputable_balance_date = today - 91
      - last_complete_stored_balance_date = last_stored_balance_date - 1

    The check is: last_complete_stored_balance_date < earliest_backcomputable_balance_date => raise
    """

    def test_raises_when_gap_too_large(self):
        """
        Stored data ends so far in the past that there's no overlap.
        last_stored_balance at (today - 93), so last_complete at (today - 94).
        earliest_backcomputable at (today - 91).
        -94 < -91 => raise.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=90)

        self._seed_balance_history(
            start=today - timedelta(days=180),
            end=today - timedelta(days=93),
            balance=1000.0,
        )

        with self.assertRaises(FetchExceedsBankHistoryLimit):
            self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )

    def test_raises_at_exact_boundary(self):
        """
        last_stored_balance at (today - 91), so last_complete at (today - 92).
        earliest_backcomputable at (today - 91).
        -92 < -91 => raise.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=90)

        self._seed_balance_history(
            start=today - timedelta(days=180),
            end=today - timedelta(days=91),
            balance=1000.0,
        )

        with self.assertRaises(FetchExceedsBankHistoryLimit):
            self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )

    def test_passes_at_minimum_overlap(self):
        """
        last_stored_balance at (today - 90), so last_complete at (today - 91).
        earliest_backcomputable at (today - 91).
        -91 == -91, NOT < => passes gap check (proceeds to fetch).
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=90)

        self._seed_balance_history(
            start=today - timedelta(days=180),
            end=today - timedelta(days=90),
            balance=1000.0,
        )

        # It will pass the gap check and try to fetch. Mock the fetch to
        # return consistent data so we can verify it doesn't raise FetchExceedsBankHistoryLimit.
        with self._mock_fetch_with_txn_specs(1000.0, []):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )
            self.assertTrue(result.success)

    def test_passes_with_ample_overlap(self):
        """
        Normal case: last sync was yesterday. Plenty of overlap.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=90)

        self._seed_balance_history(
            start=today - timedelta(days=100),
            end=today - timedelta(days=1),
            balance=1000.0,
        )

        with self._mock_fetch_with_txn_specs(1000.0, []):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )
            self.assertTrue(result.success)


# ─── Walk-back stitch validation ─────────────────────────────────────────────


class TestStitchWalkBack(IncrementalSyncTestBase):
    """
    The stitch validation walks backward from last_complete_stored_balance_date
    to earliest_backcomputable_balance_date, looking for the first date where
    stored and computed balances match.
    """

    def test_stitches_at_most_recent_date(self):
        """
        When last_complete_stored_balance_date matches, the stitch succeeds
        immediately at the most recent possible date.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=10)

        # Stored: balance 500 from day -15 to day -1 (day -1 = last_stored, day -2 = last_complete)
        self._seed_balance_history(
            start=today - timedelta(days=15),
            end=today - timedelta(days=1),
            balance=500.0,
        )

        # API returns 500 with no transactions => all computed balances are 500.
        # Stitch at last_complete (today - 2): computed 500 == stored 500 => match.
        with self._mock_fetch_with_txn_specs(500.0, []):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )
            self.assertTrue(result.success)

        # Verify that old data before stitch is preserved.
        # last_complete is today - 2. rewrite_from = today - 1. Old data at today - 3 should be intact.
        bh = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=today - timedelta(days=3),
        )
        self.assertEqual(bh.balance, 500.0)

    def test_falls_back_to_earlier_date(self):
        """
        If most recent date doesn't match, walk-back finds an earlier one.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=10)

        # Stored: 500 everywhere except last_complete_stored where stored is 999.
        self._seed_balance_history(
            start=today - timedelta(days=15),
            end=today - timedelta(days=1),
            balance=500.0,
        )
        last_complete = today - timedelta(days=2)
        BalanceHistory.objects.filter(
            account=self.account, asset=self.asset, date=last_complete,
        ).update(balance=999.0)

        # API returns 500 with no txns => all computed balances are 500.
        # Stitch at last_complete (today - 2): computed 500 != stored 999 => skip.
        # Stitch at today - 3: computed 500 == stored 500 => match.
        with self._mock_fetch_with_txn_specs(500.0, []):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )
            self.assertTrue(result.success)

    def test_exhaustion_raises_reconciliation_error(self):
        """
        If no date in the walk-back range matches, raise BalanceReconciliationError.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=10)

        # Stored: balance 999 everywhere
        self._seed_balance_history(
            start=today - timedelta(days=15),
            end=today - timedelta(days=1),
            balance=999.0,
        )

        # API returns 500 with no txns => all computed balances are 500.
        # No stored balance matches 500 => exhaustion.
        with self._mock_fetch_with_txn_specs(500.0, []):
            with self.assertRaises(BalanceReconciliationError):
                self.service._incremental_sync(
                    self.account_asset, earliest, today, self.user_tz,
                )

    def test_stitch_tolerance(self):
        """
        A difference within the tolerance (0.01) is accepted.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=10)

        self._seed_balance_history(
            start=today - timedelta(days=15),
            end=today - timedelta(days=1),
            balance=500.005,
        )

        # API returns 500.0 => computed is 500.0, stored is 500.005.
        # Difference = 0.005 <= 0.01 tolerance => match.
        with self._mock_fetch_with_txn_specs(500.0, []):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )
            self.assertTrue(result.success)

    def test_stitch_skips_none_balances(self):
        """
        If a date has no stored BalanceHistory, the walk-back skips it gracefully.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=5)

        # Stored: only 3 days exist, with a gap in the middle
        self._seed_balance_history(
            start=today - timedelta(days=10),
            end=today - timedelta(days=1),
            balance=500.0,
        )
        # Delete the balance at the last_complete date so the walk-back must skip it
        last_complete = today - timedelta(days=2)
        BalanceHistory.objects.filter(
            account=self.account, asset=self.asset, date=last_complete,
        ).delete()

        with self._mock_fetch_with_txn_specs(500.0, []):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )
            self.assertTrue(result.success)


# ─── Atomic apply boundaries ─────────────────────────────────────────────────


class TestAtomicApplyIncremental(IncrementalSyncTestBase):
    """
    _atomic_apply_incremental should:
      - Preserve data at and before stitch_date
      - Delete and rewrite data from (stitch_date + 1) onward
      - Only write new transactions from (stitch_date + 1) onward
    """

    def test_preserves_data_at_stitch_date(self):
        """Balance and transactions at/before stitch_date are untouched."""
        stitch = date(2025, 6, 10)

        self._seed_balance_history(
            start=date(2025, 6, 5), end=date(2025, 6, 14), balance=100.0,
        )
        self._seed_transactions([(date(2025, 6, 10), 50.0)])

        new_history = OrderedDict([
            (date(2025, 6, 9), 900.0),   # earlier than stitch - should be ignored
            (date(2025, 6, 10), 800.0),   # at stitch - should be ignored
            (date(2025, 6, 11), 200.0),   # after stitch - should be written
            (date(2025, 6, 12), 300.0),
        ])
        new_txns = [
            Transaction(
                account=self.account, asset=self.asset, entity="Old",
                amount=10.0, datetime=_dt(date(2025, 6, 10)),
            ),
            Transaction(
                account=self.account, asset=self.asset, entity="New",
                amount=20.0, datetime=_dt(date(2025, 6, 11)),
            ),
        ]

        TransactionSyncService._atomic_apply_incremental(
            self.account, self.asset, new_txns, new_history, stitch,
        )

        # Balance at stitch preserved (original 100, not the 800 from new_history)
        bh_stitch = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=stitch,
        )
        self.assertEqual(bh_stitch.balance, 100.0)

        # Balance before stitch preserved
        bh_before = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=date(2025, 6, 9),
        )
        self.assertEqual(bh_before.balance, 100.0)

        # Transaction on stitch day preserved (the seed one)
        stitch_txns = list(
            Transaction.objects_extended.filter(
                account=self.account, asset=self.asset,
                datetime__date=stitch,
            )
        )
        self.assertEqual(len(stitch_txns), 1)
        self.assertEqual(stitch_txns[0].amount, 50.0)

    def test_rewrites_data_after_stitch_date(self):
        """Data from (stitch_date + 1) onward is replaced with new values."""
        stitch = date(2025, 6, 10)

        # Seed old data
        self._seed_balance_history(
            start=date(2025, 6, 5), end=date(2025, 6, 14), balance=100.0,
        )
        self._seed_transactions([
            (date(2025, 6, 11), 10.0),
            (date(2025, 6, 12), 20.0),
        ])

        new_history = OrderedDict([
            (date(2025, 6, 11), 200.0),
            (date(2025, 6, 12), 300.0),
            (date(2025, 6, 13), 400.0),
        ])
        new_txns = [
            Transaction(
                account=self.account, asset=self.asset, entity="Fresh",
                amount=99.0, datetime=_dt(date(2025, 6, 11)),
            ),
        ]

        TransactionSyncService._atomic_apply_incremental(
            self.account, self.asset, new_txns, new_history, stitch,
        )

        # New balances written
        bh_11 = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=date(2025, 6, 11),
        )
        self.assertEqual(bh_11.balance, 200.0)

        bh_13 = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=date(2025, 6, 13),
        )
        self.assertEqual(bh_13.balance, 400.0)

        # Old balance at day 14 is deleted
        self.assertFalse(
            BalanceHistory.objects.filter(
                account=self.account, asset=self.asset, date=date(2025, 6, 14),
            ).exists()
        )

        # New transactions written, old ones after stitch deleted
        post_stitch_txns = list(
            Transaction.objects_extended.filter(
                account=self.account, asset=self.asset,
                datetime__date__gt=stitch,
            )
        )
        self.assertEqual(len(post_stitch_txns), 1)
        self.assertEqual(post_stitch_txns[0].entity, "Fresh")
        self.assertEqual(post_stitch_txns[0].amount, 99.0)


# ─── End-to-end incremental with transactions ────────────────────────────────


class TestIncrementalSyncEndToEnd(IncrementalSyncTestBase):
    """
    End-to-end incremental sync with actual balance calculation,
    verifying that the backward calculation produces correct stitch values.
    """

    def test_consistent_data_stitches_and_updates(self):
        """
        Simulate a scratch sync followed by an incremental sync with a new transaction.
        The stitch should succeed and the new data should be applied correctly.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=10)

        # "Previous sync" result: balance 1000, one txn of +200 on day -5.
        # Backward from 1000: days -5 to today are 1000, days before -5 are 800.
        self._seed_balance_history(
            start=earliest - timedelta(days=1),
            end=today - timedelta(days=6),
            balance=800.0,
        )
        # Days -5 to -1 have balance 1000 (after the +200 txn)
        d = today - timedelta(days=5)
        while d <= today - timedelta(days=1):
            BalanceHistory.objects.create(
                account=self.account, asset=self.asset, date=d, balance=1000.0,
            )
            d += timedelta(days=1)

        # "New sync": API balance is 1050 (a +50 txn happened on day -2).
        # Transactions: the original +200 on day -5 and a new +50 on day -2.
        txn_specs = [
            (today - timedelta(days=5), 200.0),
            (today - timedelta(days=2), 50.0),
        ]

        with self._mock_fetch_with_txn_specs(1050.0, txn_specs):
            result = self.service._incremental_sync(
                self.account_asset, earliest, today, self.user_tz,
            )

        self.assertTrue(result.success)

        # Verify: balance at today should be 1050
        bh_today = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=today,
        )
        self.assertEqual(bh_today.balance, 1050.0)

        # Verify: balance at day -3 should be 1000 (before the +50 on day -2)
        bh_pre_new_txn = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=today - timedelta(days=3),
        )
        self.assertEqual(bh_pre_new_txn.balance, 1000.0)

        # Verify: balance at day -6 should be 800 (before the +200 on day -5)
        bh_pre_old_txn = BalanceHistory.objects.get(
            account=self.account, asset=self.asset, date=today - timedelta(days=6),
        )
        self.assertEqual(bh_pre_old_txn.balance, 800.0)

    def test_stitch_fails_when_stored_history_is_wrong(self):
        """
        If previous stored data is fundamentally inconsistent with the API,
        no stitch date will match and the sync should fail.
        """
        today = date(2025, 6, 15)
        earliest = today - timedelta(days=5)

        # Stored history says balance was always 9999
        self._seed_balance_history(
            start=earliest - timedelta(days=1),
            end=today - timedelta(days=1),
            balance=9999.0,
        )

        # API says balance is 100 with no transactions
        with self._mock_fetch_with_txn_specs(100.0, []):
            with self.assertRaises(BalanceReconciliationError):
                self.service._incremental_sync(
                    self.account_asset, earliest, today, self.user_tz,
                )
