import logging
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from django.core.exceptions import ValidationError
from django.db import transaction as db_transaction

from main.models import Account, AccountAsset, Asset, BalanceHistory, Transaction
from main.services.stateful.bank_data.providers import get_provider
from main.services.stateful.bank_data.providers.base import (
    BankingProvider,
    RateLimitSnapshot,
)
from main.services.stateful.bank_data.providers.gocardless.mapping import (
    CurrencyMismatchError,
    TransactionMappingError,
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
    """Raised when a sync is already in progress for this AccountAsset."""


@dataclass(frozen=True)
class SyncResult:
    success: bool
    transactions_added: int = 0
    balance_days_computed: int = 0
    error: str = ""


class TransactionSyncService:
    """Orchestrates transaction syncing for synced accounts."""

    def __init__(self):
        self.calc = BalanceCalculationService()

    def _provider_for_account(self, account: Account) -> BankingProvider:
        if account.account_type == "manual":
            raise SyncError(f"Account {account.pk} is not a synced account")
        return get_provider(account.account_type)

    def sync_account(self, account: Account) -> SyncResult:
        if not account.is_synced:
            raise SyncError(f"Account {account.pk} is not a synced account")

        account_assets = AccountAsset.objects.filter(account=account).select_related("asset")
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

    def _sync_account_asset(self, account_asset: AccountAsset) -> SyncResult:
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
        try:
            return zoneinfo.ZoneInfo(account.user.user_timezone)
        except (KeyError, zoneinfo.ZoneInfoNotFoundError, AttributeError):
            return zoneinfo.ZoneInfo("UTC")

    def _route_sync(self, account_asset: AccountAsset) -> SyncResult:
        account = account_asset.account
        provider = self._provider_for_account(account)
        today = datetime.now(timezone.utc).date()
        earliest_available_transactions_date = today - timedelta(days=account.transaction_total_days)
        user_tz = self._resolve_user_tz(account)

        asset = account_asset.asset
        if asset:
            has_history = BalanceHistory.objects.filter(
                account=account, asset=asset,
            ).exists()
        else:
            has_history = False

        if not has_history:
            return self._scratch_sync(
                account_asset,
                earliest_available_transactions_date,
                today,
                user_tz,
            )
        return self._incremental_sync(
            account_asset,
            earliest_available_transactions_date,
            today,
            user_tz,
        )

    def _resolve_asset_from_provider(
        self,
        account_asset: AccountAsset,
        provider: BankingProvider,
    ) -> tuple[Asset, RateLimitSnapshot]:
        if not account_asset.provider_account_id:
            raise TransactionMappingError(
                f"AccountAsset {account_asset.pk} has no provider_account_id"
            )
        currency, rate_limits = provider.resolve_currency(account_asset.provider_account_id)
        asset = Asset.objects.filter(code=currency).first()
        if asset is None:
            raise TransactionMappingError(
                f"Currency {currency} returned by provider is not supported."
            )
        account_asset.asset = asset
        account_asset.save(update_fields=["asset"])
        return asset, rate_limits

    def _resolve_asset_if_needed(
        self,
        account_asset: AccountAsset,
        provider: BankingProvider,
    ) -> tuple[Asset, RateLimitSnapshot | None]:
        if account_asset.asset is not None:
            return account_asset.asset, None
        return self._resolve_asset_from_provider(account_asset, provider)

    def _scratch_sync(
        self,
        account_asset: AccountAsset,
        earliest_available_transactions_date: date,
        today: date,
        user_tz: zoneinfo.ZoneInfo,
    ) -> SyncResult:
        account = account_asset.account
        provider = self._provider_for_account(account)
        asset, rate_limits = self._resolve_asset_if_needed(account_asset, provider)
        if rate_limits is not None:
            self._update_rate_limits(account_asset, rate_limits)

        try:
            balance, transactions = self._fetch_and_filter(
                account_asset,
                provider,
                asset,
                earliest_available_transactions_date,
                today,
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
                f"Balance calculation failed: negative balance encountered. Detail: {exc}"
            ) from exc

    def _incremental_sync(
        self,
        account_asset: AccountAsset,
        earliest_available_transactions_date: date,
        today: date,
        user_tz: zoneinfo.ZoneInfo,
        stitch_tolerance: float = 0.01,
    ) -> SyncResult:
        account = account_asset.account
        provider = self._provider_for_account(account)
        asset, rate_limits = self._resolve_asset_if_needed(account_asset, provider)
        if rate_limits is not None:
            self._update_rate_limits(account_asset, rate_limits)

        last_stored_balance_date = (
            BalanceHistory.objects.filter(account=account, asset=asset)
            .order_by("-date")
            .values_list("date", flat=True)
            .first()
        )
        last_complete_stored_balance_date = last_stored_balance_date - timedelta(days=1)
        earliest_backcomputable_balance_date = earliest_available_transactions_date - timedelta(days=1)

        if last_complete_stored_balance_date < earliest_backcomputable_balance_date:
            raise FetchExceedsBankHistoryLimit(
                f"Account '{account.name}': insufficient overlap for stitch-point validation."
            )

        try:
            balance, transactions = self._fetch_and_filter(
                account_asset,
                provider,
                asset,
                earliest_available_transactions_date,
                today,
            )
            history = self.calc.generate_balance_history_backward(
                transactions, balance, earliest_available_transactions_date, today,
            )
        except ValidationError as exc:
            raise BalanceReconciliationError(
                f"Balance calculation failed. Detail: {exc}"
            ) from exc

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
                break

        if successful_stitch_date is None:
            raise BalanceReconciliationError("Stitch-point balance mismatch (incremental sync).")

        self._atomic_apply_incremental(
            account, asset, transactions, history, successful_stitch_date,
        )
        return SyncResult(
            success=True,
            transactions_added=len(transactions),
            balance_days_computed=len(history),
        )

    def _update_rate_limits(
        self,
        account_asset: AccountAsset,
        snapshot: RateLimitSnapshot,
    ) -> None:
        updated_fields = []
        if snapshot.account_success_limit is not None:
            account_asset.account_success_limit = snapshot.account_success_limit
            updated_fields.append("account_success_limit")
        if snapshot.account_success_remaining is not None:
            account_asset.account_success_remaining = snapshot.account_success_remaining
            updated_fields.append("account_success_remaining")
        if snapshot.account_success_reset_seconds is not None:
            account_asset.account_success_reset = datetime.now(timezone.utc) + timedelta(
                seconds=snapshot.account_success_reset_seconds
            )
            updated_fields.append("account_success_reset")

        if updated_fields:
            account_asset.save(update_fields=updated_fields)

    def _fetch_and_filter(
        self,
        account_asset: AccountAsset,
        provider: BankingProvider,
        asset: Asset,
        date_from: date,
        date_to: date,
    ) -> tuple[float, list[Transaction]]:
        account = account_asset.account
        provider_account_id = account_asset.provider_account_id
        if not provider_account_id:
            raise TransactionMappingError(
                f"AccountAsset {account_asset.pk} has no provider_account_id"
            )

        balance, balance_rate_limits = provider.get_balance(provider_account_id, asset.code)
        self._update_rate_limits(account_asset, balance_rate_limits)

        transactions, txn_rate_limits = provider.get_transactions(
            provider_account_id=provider_account_id,
            date_from=date_from,
            date_to=date_to,
            account=account,
            expected_asset=asset,
        )
        self._update_rate_limits(account_asset, txn_rate_limits)
        return balance, transactions

    @staticmethod
    def _atomic_apply_scratch(
        account: Account,
        asset: Asset,
        transactions: list[Transaction],
        history: dict[date, float],
    ) -> None:
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

            transactions_to_write = [
                txn for txn in transactions if txn.datetime.date() >= rewrite_from
            ]
            Transaction.objects.bulk_create(transactions_to_write)

            BalanceHistory.objects.bulk_create([
                BalanceHistory(account=account, asset=asset, date=d, balance=b)
                for d, b in history.items()
                if d >= rewrite_from
            ])
