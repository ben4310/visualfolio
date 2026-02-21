"""
Transaction sync engine implementing the fetch flowchart.

Paths:
  - Scratch: first sync for an AccountAsset (no balance history exists)
  - Incremental: subsequent syncs (balance history exists)

Sync status, errors, and rate limits are tracked per AccountAsset since each
corresponds to a distinct GoCardless account with its own API quotas.
"""

import json
import logging
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from main.models import Account, AccountAsset, Asset, BalanceHistory, Transaction

from .client import GoCardlessClient, GoCardlessAPIError
from .mapping import (
    CurrencyMismatchError,
    TransactionMappingError,
    extract_current_balance,
    map_booked_transactions,
)
from main.services.stateless.calculation import BalanceCalculationService

logger = logging.getLogger(__name__)


def _log_raw_api_responses(raw_balances: dict | None, raw_transactions: dict | None) -> None:
    """Log full raw JSON responses from balances and transactions endpoints (for mismatch debugging)."""
    if raw_balances is not None:
        logger.warning(
            "Incremental sync mismatch: raw balances endpoint response:\n%s",
            json.dumps(raw_balances, indent=2, default=str),
        )
    if raw_transactions is not None:
        logger.warning(
            "Incremental sync mismatch: raw transactions endpoint response:\n%s",
            json.dumps(raw_transactions, indent=2, default=str),
        )


class SyncError(Exception):
    """Base class for sync errors."""


class FetchExceedsBankHistoryLimit(SyncError):
    """Raised when stored data is too old to bridge with available API history."""


class BalanceReconciliationError(SyncError):
    """Raised when the incremental stitch-point balance does not match."""


class BalanceCalculationError(SyncError):
    """Raised when balance back-calculation encounters a negative balance."""


class SyncAlreadyInProgressError(SyncError):
    """Raised when a sync is already in progress for this AccountAsset."""


@dataclass(frozen=True)
class SyncResult:
    success: bool
    transactions_added: int = 0
    balance_days_computed: int = 0
    error: str = ""


def _format_incremental_mismatch_diagnostic(
    *,
    account: Account,
    asset: Asset,
    attempt: int,
    max_retries: int,
    effective_start: date,
    refetch_start: date,
    today: date,
    stitch_date: date,
    computed_stitch_balance: float | None,
    stored_stitch: float | None,
    api_snapshot_balance: float,
    transactions: list[Transaction],
    history: dict[date, float],
) -> str:
    """Build a full diagnostic string for incremental stitch mismatch (for toast notification)."""
    lines = [
        "Stitch-point balance mismatch (incremental sync).",
        "",
        "--- Context ---",
        f"Account: {account.name} (pk={account.pk})",
        f"Asset: {asset.code} (pk={asset.pk})",
        f"Attempt: {attempt + 1}/{max_retries + 1}",
        f"Date range: effective_start={effective_start}, refetch_start={refetch_start}, today={today}",
        f"Stitch date: {stitch_date}",
        f"API snapshot balance (used as end balance): {api_snapshot_balance}",
        f"Computed stitch balance: {computed_stitch_balance}",
        f"Stored stitch balance: {stored_stitch}",
        f"Difference: {(computed_stitch_balance or 0) - (stored_stitch or 0)}",
        "",
        "--- Computed balance history (date -> balance) ---",
    ]
    for d in sorted(history.keys()):
        lines.append(f"  {d.isoformat()}: {history[d]}")
    lines.extend([
        "",
        f"--- All transactions in this fetch ({len(transactions)} total) ---",
    ])
    for i, t in enumerate(transactions):
        dt_str = t.datetime.isoformat() if t.datetime else ""
        lines.append(
            f"  [{i}] date={dt_str} entity={t.entity!r} amount={t.amount} asset={t.asset_id}"
        )
    lines.extend([
        "",
        "Retry later, or if the error persists, delete and reconnect the account.",
    ])
    return "\n".join(lines)


class TransactionSyncService:
    """Orchestrates transaction syncing for synced accounts."""

    def __init__(self, client: GoCardlessClient | None = None):
        self.client = client or GoCardlessClient()
        self.calc = BalanceCalculationService()

    # ── Public entry point ──────────────────────────────────────────

    def sync_account(self, account: Account) -> SyncResult:
        """
        Sync all AccountAssets for a synced account.
        Each AccountAsset is locked and synced independently.
        """
        if not account.is_synced:
            raise SyncError(f"Account {account.pk} is not a synced account")

        account_assets = AccountAsset.objects.filter(
            account=account,
        ).select_related("asset")

        if not account_assets.exists():
            raise TransactionMappingError(
                f"No AccountAsset entries found for account {account.name}. "
                f"The account may not be properly linked."
            )

        total_transactions = 0
        total_balance_days = 0

        for account_asset in account_assets:
            try:
                result = self._sync_account_asset(account_asset)
                total_transactions += result.transactions_added
                total_balance_days += result.balance_days_computed
            except SyncAlreadyInProgressError:
                logger.info(
                    "AccountAsset %s already syncing, skipping", account_asset.pk
                )
            except Exception as exc:
                logger.exception(
                    "Failed to sync AccountAsset %s: %s", account_asset.pk, exc
                )

        return SyncResult(
            success=True,
            transactions_added=total_transactions,
            balance_days_computed=total_balance_days,
        )

    # ── Per-AccountAsset sync lifecycle ─────────────────────────────

    def _sync_account_asset(self, account_asset: AccountAsset) -> SyncResult:
        """
        Lock, sync, and update status for a single AccountAsset.
        Raises SyncAlreadyInProgressError if already syncing.
        """
        with db_transaction.atomic():
            locked = AccountAsset.objects.select_for_update().get(pk=account_asset.pk)
            if locked.sync_status == "syncing":
                raise SyncAlreadyInProgressError(
                    f"AccountAsset {account_asset.pk} is already being synced"
                )
            locked.sync_status = "syncing"
            locked.save(update_fields=["sync_status"])

        try:
            result = self._route_sync(account_asset)
            account_asset.refresh_from_db()
            account_asset.sync_status = "idle"
            account_asset.last_successful_sync = datetime.now(timezone.utc)
            account_asset.last_sync_error = None
            account_asset.save(
                update_fields=["sync_status", "last_successful_sync", "last_sync_error"]
            )
            return result
        except Exception as exc:
            account_asset.refresh_from_db()
            account_asset.sync_status = "idle"
            account_asset.last_sync_error = str(exc)
            account_asset.save(update_fields=["sync_status", "last_sync_error"])
            raise

    @staticmethod
    def _resolve_user_tz(account: Account) -> zoneinfo.ZoneInfo:
        """Resolve the account owner's timezone as a ZoneInfo instance."""
        try:
            return zoneinfo.ZoneInfo(account.user.user_timezone)
        except (KeyError, zoneinfo.ZoneInfoNotFoundError, AttributeError):
            return zoneinfo.ZoneInfo("UTC")

    def _route_sync(self, account_asset: AccountAsset) -> SyncResult:
        """Determine scratch vs incremental path for a single AccountAsset."""
        account = account_asset.account
        today = datetime.now(timezone.utc).date()
        earliest_available_transactions_date = today - timedelta(days=account.transaction_total_days)
        user_tz = self._resolve_user_tz(account)

        asset = account_asset.asset
        if asset:
            logger.info("Syncing %s - %s", account.name, asset.code)
            has_history = BalanceHistory.objects.filter(
                account=account, asset=asset,
            ).exists()
        else:
            logger.info("Syncing %s - asset pending resolution", account.name)
            has_history = False

        if not has_history:
            return self._scratch_sync(account_asset, earliest_available_transactions_date, today, user_tz)
        return self._incremental_sync(account_asset, earliest_available_transactions_date, today, user_tz)

    # ── Asset resolution helper ─────────────────────────────────────

    def _resolve_asset_from_balance(
        self, account_asset: AccountAsset, balance_data: dict
    ) -> Asset:
        """
        Resolve and persist asset from balance API response.
        Returns the resolved Asset instance.
        """
        balances = balance_data.get("balances", [])

        if not balances:
            raise TransactionMappingError(
                f"No balances returned for GoCardless account {account_asset.gocardless_account_id}. "
                f"Cannot determine currency."
            )

        currency = balances[0].get("balanceAmount", {}).get("currency", "")

        if not currency:
            raise TransactionMappingError(
                f"Balance response for GoCardless account {account_asset.gocardless_account_id} "
                f"contains no currency. Cannot determine asset."
            )

        try:
            asset = Asset.objects.get(code=currency)
            account_asset.asset = asset
            account_asset.save(update_fields=["asset"])
            logger.info(
                "Resolved AccountAsset %s to asset %s from balance currency",
                account_asset.pk, asset.code,
            )
            return asset
        except Asset.DoesNotExist:
            raise TransactionMappingError(
                f"Currency {currency} returned by GoCardless is not supported. "
                f"Supported currencies: EUR, USD, CHF."
            )

    def _resolve_asset_if_needed(
        self, account_asset: AccountAsset,
    ) -> tuple[Asset, dict | None, dict | None]:
        """
        If the AccountAsset has no asset, resolve it from the balance endpoint.
        Returns (asset, prefetched_balance_data, prefetched_balance_headers).
        """
        if account_asset.asset is not None:
            return account_asset.asset, None, None

        if not account_asset.gocardless_account_id:
            raise TransactionMappingError(
                f"AccountAsset {account_asset.pk} has no asset and no GoCardless account ID. "
                f"Cannot resolve currency."
            )

        logger.info(
            "AccountAsset %s has no asset, resolving from balance endpoint (reusing fetch)",
            account_asset.pk,
        )

        try:
            prefetched_data, prefetched_headers = self.client.get_account_balances(
                account_asset.gocardless_account_id,
            )
            asset = self._resolve_asset_from_balance(account_asset, prefetched_data)
            return asset, prefetched_data, prefetched_headers
        except GoCardlessAPIError as exc:
            self._update_rate_limits(account_asset, exc.headers)
            logger.error(
                "Failed to resolve asset for AccountAsset %s: %s",
                account_asset.pk, str(exc),
            )
            raise TransactionMappingError(
                f"Failed to resolve currency for AccountAsset {account_asset.pk}: {str(exc)}"
            ) from exc
        except TransactionMappingError:
            raise
        except Exception as e:
            logger.error(
                "Failed to resolve asset for AccountAsset %s: %s",
                account_asset.pk, str(e),
            )
            raise TransactionMappingError(
                f"Failed to resolve currency for AccountAsset {account_asset.pk}: {str(e)}"
            )

    # ── Scratch sync ────────────────────────────────────────────────

    def _scratch_sync(
        self,
        account_asset: AccountAsset,
        earliest_available_transactions_date: date,
        today: date,
        user_tz: zoneinfo.ZoneInfo,
    ) -> SyncResult:
        """First-ever sync: fetch everything from earliest_available_transactions_date to today."""

        # The asset of the AccountAsset is assessed at linking time, but due to OpenBanking implementation
        # inconsistencies, for some banks this is not possible. In those cases we try resolve the asset here,
        # from the balance endpoint.
        account = account_asset.account
        asset, prefetched_data, prefetched_headers = self._resolve_asset_if_needed(
            account_asset,
        )

        try:
            balance, transactions, _, _ = self._fetch_and_filter(
                account_asset, asset, earliest_available_transactions_date, today,
                prefetched_balance_data=prefetched_data,
                prefetched_balance_headers=prefetched_headers,
                user_tz=user_tz,
            )
            history = self.calc.generate_balance_history_backward(
                transactions, balance, earliest_available_transactions_date, today,
            )
            self._atomic_apply_scratch(account, asset, transactions, history)
            return SyncResult(
                success=True,
                transactions_added=len(transactions),
                balance_days_computed=len(history),
            )
        except ValidationError as exc:
            raise BalanceCalculationError(
                f"Balance calculation failed: negative balance encountered. "
                f"This account may not support syncing. Detail: {exc}"
            ) from exc

    # ── Incremental sync ────────────────────────────────────────────

    def _incremental_sync(
        self,
        account_asset: AccountAsset,
        earliest_available_transactions_date: date,
        today: date,
        user_tz: zoneinfo.ZoneInfo,
        stitch_tolerance: float = 0.01,
    ) -> SyncResult:
        """Subsequent sync: fetch from earliest_available_transactions_date, validate stitch, apply delta."""
        account = account_asset.account
        asset, prefetched_data, prefetched_headers = self._resolve_asset_if_needed(
            account_asset,
        )

        last_stored_balance_date = (
            BalanceHistory.objects.filter(account=account, asset=asset)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )

        # The last balance entry for each account-asset is not guaranteed to be the end-of-day balance (read more on model).
        # The last balance guaranteed to be the complete balance at end-of-day is the one before the last.
        last_complete_stored_balance_date = last_stored_balance_date - timedelta(days=1)

        # Fetching transactions from date A to date B, and having the guarantee that all transactions on day A are included,
        # means that we can derive the end-of-day balances back to day A - 1.
        earliest_backcomputable_balance_date = earliest_available_transactions_date - timedelta(days=1)

        # For a valid stitch, we need at least one day of overlap between the last complete balance
        # and the earliest backcomputable balance (not just adjacent).
        if last_complete_stored_balance_date < earliest_backcomputable_balance_date:
            raise FetchExceedsBankHistoryLimit(
                f"Account '{account.name}': last complete balance data is from "
                f"{last_complete_stored_balance_date}, but with the available transaction history "
                f"(from {earliest_available_transactions_date}), we can only compute balances back to "
                f"{earliest_backcomputable_balance_date}. At least one day of overlap is required for validation."
            )

        try:
            balance, transactions, raw_balances_resp, raw_txn_resp = self._fetch_and_filter(
                account_asset, asset, earliest_available_transactions_date, today,
                prefetched_balance_data=prefetched_data,
                prefetched_balance_headers=prefetched_headers,
                user_tz=user_tz,
            )
            history = self.calc.generate_balance_history_backward(
                transactions, balance, earliest_available_transactions_date, today,
            )
        except ValidationError as exc:
            _log_raw_api_responses(raw_balances_resp, raw_txn_resp)
            raise BalanceReconciliationError(
                f"Balance calculation failed. "
                f"Retry later, or if the error persists, delete and reconnect "
                f"the account. Detail: {exc}"
            ) from exc

        # Stitch-point validation: try from most recent date backward
        # Start at the last complete balance date and walk backward to earliest_backcomputable_balance_date
        successful_stitch_date = None
        for candidate_date in range(
            (last_complete_stored_balance_date - earliest_backcomputable_balance_date).days + 1
        ):
            stitch_date = last_complete_stored_balance_date - timedelta(days=candidate_date)
            computed_stitch_balance = history.get(stitch_date)
            stored_stitch = (
                BalanceHistory.objects.filter(
                    account=account, asset=asset, date=stitch_date,
                )
                .values_list("balance", flat=True)
                .first()
            )

            if stored_stitch is None or computed_stitch_balance is None:
                continue

            if abs(computed_stitch_balance - stored_stitch) <= stitch_tolerance:
                successful_stitch_date = stitch_date
                logger.info(
                    "Stitch successful at %s for account %s (%s)",
                    stitch_date, account.name, asset.code,
                )
                break

        if successful_stitch_date is None:
            _log_raw_api_responses(raw_balances_resp, raw_txn_resp)
            detail = _format_incremental_mismatch_diagnostic(
                account=account,
                asset=asset,
                attempt=0,
                max_retries=0,
                effective_start=earliest_available_transactions_date,
                refetch_start=earliest_available_transactions_date,
                today=today,
                stitch_date=earliest_backcomputable_balance_date,
                computed_stitch_balance=history.get(earliest_backcomputable_balance_date),
                stored_stitch=BalanceHistory.objects.filter(
                    account=account, asset=asset, date=earliest_backcomputable_balance_date,
                ).values_list("balance", flat=True).first(),
                api_snapshot_balance=balance,
                transactions=transactions,
                history=history,
            )
            raise BalanceReconciliationError(detail)

        self._atomic_apply_incremental(
            account, asset, transactions, history, successful_stitch_date,
        )
        return SyncResult(
            success=True,
            transactions_added=len(transactions),
            balance_days_computed=len(history),
        )

    # ── Shared helpers ──────────────────────────────────────────────

    def _update_rate_limits(self, account_asset: AccountAsset, headers: dict) -> None:
        """
        Parse rate limit headers and update the AccountAsset's rate limit fields.
        Only updates if the headers contain rate limit data.
        """
        rate_headers = {k: v for k, v in headers.items() if 'ratelimit' in k.lower()}
        if rate_headers:
            logger.debug("Rate limit headers received: %s", rate_headers)
        else:
            logger.debug("No rate limit headers found in response. All headers: %s", list(headers.keys()))

        rate_limit_data = self.client._parse_account_rate_limit_headers(headers)

        limit = rate_limit_data.get("account_success_limit")
        remaining = rate_limit_data.get("account_success_remaining")
        reset_seconds = rate_limit_data.get("account_success_reset")

        if limit is not None:
            account_asset.account_success_limit = int(limit)
        if remaining is not None:
            account_asset.account_success_remaining = int(remaining)
        if reset_seconds is not None:
            account_asset.account_success_reset = datetime.now(timezone.utc) + timedelta(
                seconds=int(reset_seconds)
            )

        account_asset.save(
            update_fields=[
                "account_success_limit",
                "account_success_remaining",
                "account_success_reset",
            ]
        )

        logger.debug(
            "Rate limits updated for %s: %s/%s remaining, resets at %s",
            account_asset,
            account_asset.account_success_remaining,
            account_asset.account_success_limit,
            account_asset.account_success_reset,
        )

    def _fetch_and_filter(
        self,
        account_asset: AccountAsset,
        asset: Asset,
        date_from: date,
        date_to: date,
        prefetched_balance_data: dict | None = None,
        prefetched_balance_headers: dict | None = None,
        user_tz: zoneinfo.ZoneInfo | None = None,
    ) -> tuple[float, list[Transaction], dict, dict]:
        """
        Fetch balance and transactions from the API.
        Returns (current_balance, filtered_transaction_list, raw_balances_response, raw_transactions_response).
        Also updates AccountAsset rate limit fields from response headers.

        If prefetched_balance_data and prefetched_balance_headers are provided,
        skips the balance API call and reuses the provided data (for asset resolution).
        """
        account = account_asset.account
        gc_account_id = account_asset.gocardless_account_id

        if not gc_account_id:
            raise TransactionMappingError(
                f"AccountAsset {account_asset.pk} has no gocardless_account_id"
            )

        if prefetched_balance_data is not None and prefetched_balance_headers is not None:
            logger.debug(
                "Reusing prefetched balance data for %s (asset resolution)",
                account.name,
            )
            balances_resp = prefetched_balance_data
            balance_headers = prefetched_balance_headers
        else:
            try:
                balances_resp, balance_headers = self.client.get_account_balances(gc_account_id)
            except GoCardlessAPIError as exc:
                self._update_rate_limits(account_asset, exc.headers)
                raise

        self._update_rate_limits(account_asset, balance_headers)

        current_balance = extract_current_balance(balances_resp, asset.code)

        try:
            txn_resp, txn_headers = self.client.get_account_transactions(
                gc_account_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
            )
        except GoCardlessAPIError as exc:
            self._update_rate_limits(account_asset, exc.headers)
            raise

        logger.debug(
            "Raw transactions response for %s (%s to %s):\n%s",
            account.name, date_from, date_to,
            json.dumps(txn_resp, indent=2, default=str),
        )

        transactions = map_booked_transactions(
            txn_resp, account, asset, date_from, date_to,
            user_tz=user_tz,
        )

        self._update_rate_limits(account_asset, txn_headers)

        logger.info(
            "Fetched %d transactions for account %s (%s to %s), balance=%.2f %s",
            len(transactions), account.name,
            date_from, date_to, current_balance, asset.code,
        )
        return current_balance, transactions, balances_resp, txn_resp

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
            Transaction.objects.filter(account=account, asset=asset).delete()
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
        stitch_date: date,
    ) -> None:
        """
        Atomic: delete data from the day after stitch_date onward, write new.
        
        The stitch_date itself is kept (it was validated), and we only rewrite from the next day.
        Transactions ON stitch_date are part of the validated data as they would influence the back-computation of the 
        balance at end of (stitch_date - 1), not the balance at end of stitch_date.
        """
        rewrite_from = stitch_date + timedelta(days=1)
        with db_transaction.atomic():
            Transaction.objects.filter(
                account=account,
                datetime__date__gte=rewrite_from,
            ).delete()
            BalanceHistory.objects.filter(
                account=account,
                asset=asset,
                date__gte=rewrite_from,
            ).delete()

            # Only write transactions from rewrite_from onward
            transactions_to_write = [
                txn for txn in transactions if txn.datetime.date() >= rewrite_from
            ]
            Transaction.objects.bulk_create(transactions_to_write)
            
            # Only write balance history from rewrite_from onward
            BalanceHistory.objects.bulk_create([
                BalanceHistory(account=account, asset=asset, date=d, balance=b)
                for d, b in history.items()
                if d >= rewrite_from
            ])
