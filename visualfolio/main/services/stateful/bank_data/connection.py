import logging
import threading

from main.models import Account, AccountAsset, Asset, PendingConnection
from main.services.stateful.bank_data.providers import get_provider
from main.services.stateful.bank_data.sync import (
    SyncAlreadyInProgressError,
    TransactionSyncService,
)

logger = logging.getLogger(__name__)


class ConnectionError(Exception):
    """Raised when the bank connection flow encounters an error."""


class ConnectionService:
    def list_institutions(self, provider_type: str, country_code: str) -> list[dict]:
        provider = get_provider(provider_type)
        return provider.list_institutions(country_code)

    def initiate_connection(
        self,
        *,
        provider_type: str,
        user,
        institution_id: str,
        institution_name: str,
        institution_country: str,
        redirect_url: str,
        callback_url: str,
        transaction_total_days: int | None = None,
        custom_name: str | None = None,
        color: str | None = None,
    ) -> PendingConnection:
        provider = get_provider(provider_type)
        if not provider.is_enabled:
            raise ConnectionError(f"Provider '{provider_type}' is not configured")
        return provider.initiate_auth(
            user=user,
            institution_id=institution_id,
            institution_name=institution_name,
            institution_country=institution_country,
            redirect_url=redirect_url,
            callback_url=callback_url,
            transaction_total_days=transaction_total_days,
            custom_name=custom_name,
            color=color,
        )

    def complete_connection(
        self,
        *,
        provider_type: str,
        pending: PendingConnection,
        callback_params: dict,
    ) -> list[Account]:
        provider = get_provider(provider_type)
        result = provider.complete_auth(pending, callback_params)

        # A valid provider connection must resolve to at least one provider account.
        # For Enable Banking this commonly indicates missing account linking in the
        # developer portal.
        if not result.connected_accounts:
            raise ConnectionError(
                f"No accounts were returned by provider '{provider_type}'."
            )

        existing_account = Account.objects.filter(
            provider_connection_id=result.provider_connection_id,
            user=pending.user,
        ).first()

        if existing_account:
            account = existing_account
            account.access_expires_at = result.access_expires_at
            account.transaction_total_days = result.transaction_total_days
            account.save(update_fields=["access_expires_at", "transaction_total_days"])
        else:
            account_name = pending.provider_data.get("custom_name") or result.institution_name
            account_color = pending.provider_data.get("color") or "#4b8dd3"
            account = Account.objects.create(
                user=pending.user,
                name=account_name,
                color=account_color,
                country=result.institution_country,
                account_type=provider_type,
                provider_connection_id=result.provider_connection_id,
                institution_id=result.institution_id,
                transaction_total_days=result.transaction_total_days,
                access_expires_at=result.access_expires_at,
            )

        for connected in result.connected_accounts:
            asset = None
            if connected.currency:
                asset = Asset.objects.filter(code=connected.currency).first()
            AccountAsset.objects.update_or_create(
                account=account,
                provider_account_id=connected.provider_account_id,
                defaults={"asset": asset},
            )

        pending.status = "completed"
        pending.save(update_fields=["status"])

        has_idle = AccountAsset.objects.filter(account=account, sync_status="idle").exists()
        if has_idle:
            AccountAsset.objects.filter(
                account=account,
                sync_status="idle",
            ).update(sync_status="pending_sync", last_sync_error=None)
            self._trigger_background_sync(account)

        return [account]

    @staticmethod
    def _trigger_background_sync(account: Account) -> None:
        def _run():
            try:
                TransactionSyncService().sync_account(account)
                logger.info("Background sync completed for account %s", account.pk)
            except SyncAlreadyInProgressError:
                logger.info("Background sync already in progress for account %s", account.pk)
            except Exception:
                logger.exception("Background sync failed for account %s", account.pk)

        threading.Thread(target=_run, daemon=True).start()
