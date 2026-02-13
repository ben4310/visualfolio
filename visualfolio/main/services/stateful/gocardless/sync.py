"""
Transaction sync engine implementing the fetch flowchart.

Paths:
  - Scratch: first sync for an account (no balance history exists)
  - Incremental: subsequent syncs (balance history exists)

Retry strategy (incremental):
  - Attempt 0: normal incremental with refetch_start = last_balance_date - 1
  - Attempt 1: same parameters
  - Attempt 2: reset refetch_start = earliest_available_date (full-range incremental)
  - If all fail: raise BalanceReconciliationError

Retry strategy (scratch):
  - Attempt 0: normal
  - Attempt 1: retry once
  - If both fail: raise BalanceCalculationError
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from main.models import Account, Asset, BalanceHistory, Transaction

from .client import GoCardlessClient
from .mapping import (
    CurrencyMismatchError,
    TransactionMappingError,
    extract_current_balance,
    map_booked_transactions,
)
from main.services.stateless.calculation import BalanceCalculationService

logger = logging.getLogger(__name__)


class SyncError(Exception):
    """Base class for sync errors."""


class FetchExceedsBankHistoryLimit(SyncError):
    """Raised when stored data is too old to bridge with available API history."""


class BalanceReconciliationError(SyncError):
    """Raised when the incremental stitch-point balance does not match."""


class BalanceCalculationError(SyncError):
    """Raised when balance back-calculation encounters a negative balance."""


class SyncAlreadyInProgressError(SyncError):
    """Raised when a sync is already in progress for this account."""


@dataclass(frozen=True)
class SyncResult:
    success: bool
    transactions_added: int = 0
    balance_days_computed: int = 0
    error: str = ""


class TransactionSyncService:
    """Orchestrates transaction syncing for a single synced account."""

    def __init__(self, client: GoCardlessClient | None = None):
        self.client = client or GoCardlessClient()
        self.calc = BalanceCalculationService()

    # ── Public entry point ──────────────────────────────────────────

    def sync_account(self, account: Account) -> SyncResult:
        """
        Main entry point. Determines scratch vs incremental path.
        Updates account.sync_status and account.sync_error.
        
        Uses atomic locking to prevent concurrent syncs for the same account.
        Raises SyncAlreadyInProgressError if another sync is already running.
        """
        if not account.is_synced:
            raise SyncError(f"Account {account.pk} is not a synced account")

        # Atomically lock the account row and check/update status BEFORE any API calls
        with db_transaction.atomic():
            locked_account = Account.objects.select_for_update().get(pk=account.pk)
            
            if locked_account.sync_status == "syncing":
                raise SyncAlreadyInProgressError(
                    f"Account {account.pk} is already being synced"
                )
            
            # Atomically set status to syncing while locked
            locked_account.sync_status = "syncing"
            locked_account.sync_error = None
            locked_account.save(update_fields=["sync_status", "sync_error"])
        
        # Lock is released here, but status is already set to "syncing"
        # Any concurrent call will see "syncing" status and fail above

        try:
            result = self._sync(account)
            account.sync_status = "success"
            account.sync_error = None
            account.last_successful_sync = datetime.now(timezone.utc)
            account.save(update_fields=["sync_status", "sync_error", "last_successful_sync"])
            return result
        except Exception as exc:
            account.sync_status = "error"
            account.sync_error = str(exc)
            account.save(update_fields=["sync_status", "sync_error"])
            raise

    def _sync(self, account: Account) -> SyncResult:
        """Route to scratch or incremental for each linked asset."""
        from main.models import AccountAsset
        
        today = date.today()
        earliest_available = today - timedelta(days=account.transaction_total_days)

        # Get all assets linked to this account via AccountAsset
        account_assets = AccountAsset.objects.filter(account=account).select_related('asset')
        
        if not account_assets.exists():
            raise TransactionMappingError(
                f"No AccountAsset entries found for account {account.name}. "
                f"The account may not be properly linked."
            )

        total_result = SyncResult(success=True, transactions_added=0, balance_days_computed=0)
        
        # Sync each asset independently
        for account_asset in account_assets:
            asset = account_asset.asset
            logger.info("Syncing %s - %s", account.name, asset.code)
            
            has_history = BalanceHistory.objects.filter(account=account, asset=asset).exists()

            if not has_history:
                result = self._scratch_sync(account, asset, account_asset, earliest_available, today)
            else:
                result = self._incremental_sync(account, asset, account_asset, earliest_available, today)
            
            total_result.transactions_added += result.transactions_added
            total_result.balance_days_computed += result.balance_days_computed
            
        return total_result

    # ── Scratch sync ────────────────────────────────────────────────

    def _scratch_sync(
        self, account: Account, asset: Asset, account_asset, earliest_available: date, today: date,
    ) -> SyncResult:
        """First-ever sync: fetch everything from earliest_available to today."""
        max_retries = 1

        for attempt in range(max_retries + 1):
            try:
                balance, transactions = self._fetch_and_filter(
                    account, asset, account_asset, earliest_available, today,
                )
                # Calculate balance history from (earliest_available - 1) to today
                history_start = earliest_available - timedelta(days=1)
                history = self.calc.generate_balance_history_backward(
                    transactions, balance, history_start, today,
                )
                self._atomic_apply_scratch(account, asset, transactions, history)
                return SyncResult(
                    success=True,
                    transactions_added=len(transactions),
                    balance_days_computed=len(history),
                )
            except ValidationError as exc:
                logger.warning(
                    "Scratch sync attempt %d/%d failed (negative balance): %s",
                    attempt + 1, max_retries + 1, exc,
                )
                if attempt >= max_retries:
                    raise BalanceCalculationError(
                        f"Balance calculation failed: negative balance encountered. "
                        f"This account may not support syncing. Detail: {exc}"
                    ) from exc

        # Unreachable, but satisfies type checker
        raise BalanceCalculationError("Scratch sync exhausted retries")

    # ── Incremental sync ────────────────────────────────────────────

    def _incremental_sync(
        self, account: Account, asset: Asset, account_asset, earliest_available: date, today: date,
    ) -> SyncResult:
        """Subsequent sync: fetch from overlap point, validate stitch, apply delta."""
        max_retries = 2

        last_balance_date = (
            BalanceHistory.objects.filter(account=account, asset=asset)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        refetch_start = last_balance_date - timedelta(days=1)

        if refetch_start < earliest_available:
            raise FetchExceedsBankHistoryLimit(
                f"Account '{account.name}': last synced data is from "
                f"{last_balance_date}, but the bank only provides history from "
                f"{earliest_available}. The gap cannot be bridged."
            )

        for attempt in range(max_retries + 1):
            effective_start = refetch_start

            # Attempt 2: reset to full available range
            if attempt == 2:
                effective_start = earliest_available
                logger.info(
                    "Incremental sync attempt 2: resetting refetch_start to %s",
                    effective_start,
                )

            try:
                balance, transactions = self._fetch_and_filter(
                    account, asset, account_asset, effective_start, today,
                )
                history_start = effective_start - timedelta(days=1)
                history = self.calc.generate_balance_history_backward(
                    transactions, balance, history_start, today,
                )
            except ValidationError as exc:
                logger.warning(
                    "Incremental sync attempt %d/%d: balance calculation failed: %s",
                    attempt + 1, max_retries + 1, exc,
                )
                if attempt >= max_retries:
                    raise BalanceReconciliationError(
                        f"Balance calculation failed after {max_retries + 1} attempts. "
                        f"Retry later, or if the error persists, delete and reconnect "
                        f"the account. Detail: {exc}"
                    ) from exc
                continue

            # Stitch-point validation
            stitch_date = effective_start - timedelta(days=1)
            computed_stitch_balance = history.get(stitch_date)
            stored_stitch = (
                BalanceHistory.objects.filter(
                    account=account, asset=asset, date=stitch_date,
                )
                .values_list("balance", flat=True)
                .first()
            )

            if stored_stitch is None or computed_stitch_balance is None:
                mismatch = True
            else:
                # Compare with tolerance for floating-point arithmetic
                mismatch = abs(computed_stitch_balance - stored_stitch) > 0.01

            if mismatch:
                logger.warning(
                    "Incremental sync attempt %d/%d: stitch-point mismatch at %s "
                    "(computed=%s, stored=%s)",
                    attempt + 1, max_retries + 1, stitch_date,
                    computed_stitch_balance, stored_stitch,
                )
                if attempt >= max_retries:
                    raise BalanceReconciliationError(
                        f"Stitch-point balance mismatch at {stitch_date}: "
                        f"computed {computed_stitch_balance}, stored {stored_stitch}. "
                        f"Retry later, or if the error persists, delete and reconnect "
                        f"the account."
                    )
                continue

            # All checks passed -- apply atomically
            self._atomic_apply_incremental(
                account, asset, transactions, history, effective_start,
            )
            return SyncResult(
                success=True,
                transactions_added=len(transactions),
                balance_days_computed=len(history),
            )

        raise BalanceReconciliationError("Incremental sync exhausted retries")

    # ── Shared helpers ──────────────────────────────────────────────

    def _update_rate_limits(self, account: Account, headers: dict) -> None:
        """
        Parse rate limit headers and update the account's rate limit fields.
        Only updates if the headers contain rate limit data.
        """
        # Debug: log all headers that contain 'ratelimit' (case-insensitive)
        rate_headers = {k: v for k, v in headers.items() if 'ratelimit' in k.lower()}
        if rate_headers:
            logger.debug("Rate limit headers received: %s", rate_headers)
        else:
            logger.debug("No rate limit headers found in response. All headers: %s", list(headers.keys()))
        
        rate_limit_data = self.client._parse_account_rate_limit_headers(headers)
        
        limit = rate_limit_data.get('account_success_limit')
        remaining = rate_limit_data.get('account_success_remaining')
        reset_seconds = rate_limit_data.get('account_success_reset')
        
        # Only update if we have the data (headers present for successful account data requests)
        if limit is not None:
            account.account_success_limit = int(limit)
        if remaining is not None:
            account.account_success_remaining = int(remaining)
        if reset_seconds is not None:
            # Convert seconds-from-now to absolute datetime
            account.account_success_reset = datetime.now(timezone.utc) + timedelta(seconds=int(reset_seconds))
        
        account.save(update_fields=['account_success_limit', 'account_success_remaining', 'account_success_reset'])
        
        logger.debug(
            "Rate limits updated for account %s: %s/%s remaining, resets at %s",
            account.name, account.account_success_remaining, account.account_success_limit,
            account.account_success_reset,
        )

    def _fetch_and_filter(
        self, account: Account, asset: Asset, account_asset, date_from: date, date_to: date,
    ) -> tuple[float, list[Transaction]]:
        """
        Fetch balance and transactions from the API.
        The two calls are made immediately one after the other to minimize drift.
        Returns (current_balance, filtered_transaction_list).
        Also updates account rate limit fields from response headers.
        """
        gc_account_id = account_asset.gocardless_account_id
        
        if not gc_account_id:
            raise TransactionMappingError(
                f"AccountAsset {account_asset.pk} has no gocardless_account_id"
            )

        # Balance first (snapshot), then transactions
        balances_resp, balance_headers = self.client.get_account_balances(gc_account_id)
        current_balance = extract_current_balance(balances_resp, asset.code)

        txn_resp, txn_headers = self.client.get_account_transactions(
            gc_account_id,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )

        transactions = map_booked_transactions(
            txn_resp, account, asset, date_from, date_to,
        )

        # Update rate limits from most recent call (transactions endpoint)
        self._update_rate_limits(account, txn_headers)

        logger.info(
            "Fetched %d transactions for account %s (%s to %s), balance=%.2f %s",
            len(transactions), account.name,
            date_from, date_to, current_balance, asset.code,
        )
        return current_balance, transactions

    # ── Atomic DB operations ────────────────────────────────────────

    @staticmethod
    def _atomic_apply_scratch(
        account: Account,
        asset: Asset,
        transactions: list[Transaction],
        history: dict[date, float],
    ) -> None:
        """Atomic: write transactions and balance history from scratch."""
        with db_transaction.atomic():
            # Wipe any stale data (shouldn't exist, but be safe)
            Transaction.objects.filter(account=account).delete()
            BalanceHistory.objects.filter(account=account, asset=asset).delete()

            Transaction.objects.bulk_create(transactions)
            BalanceHistory.objects.bulk_create([
                BalanceHistory(account=account, asset=asset, date=d, balance=b)
                for d, b in history.items()
            ])

    @staticmethod
    def _atomic_apply_incremental(
        account: Account,
        asset: Asset,
        transactions: list[Transaction],
        history: dict[date, float],
        refetch_start: date,
    ) -> None:
        """Atomic: delete overlapping data from refetch_start onward, write new."""
        stitch_date = refetch_start - timedelta(days=1)
        with db_transaction.atomic():
            Transaction.objects.filter(
                account=account,
                datetime__date__gte=refetch_start,
            ).delete()
            BalanceHistory.objects.filter(
                account=account,
                asset=asset,
                date__gte=stitch_date,
            ).delete()

            Transaction.objects.bulk_create(transactions)
            BalanceHistory.objects.bulk_create([
                BalanceHistory(account=account, asset=asset, date=d, balance=b)
                for d, b in history.items()
            ])
