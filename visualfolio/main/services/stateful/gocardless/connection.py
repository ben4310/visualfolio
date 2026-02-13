"""
Bank connection lifecycle: institution discovery, requisition creation, and account linking.
"""

import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model

from main.models import Account, Requisition

from .client import GoCardlessClient
from .sync import TransactionSyncService

logger = logging.getLogger(__name__)

User = get_user_model()


class ConnectionError(Exception):
    """Raised when the bank connection flow encounters an error."""


class ConnectionService:
    """Manages the GoCardless bank account linking lifecycle."""

    def __init__(self, client: GoCardlessClient | None = None):
        self.client = client or GoCardlessClient()

    def list_institutions(self, country_code: str) -> list[dict]:
        """
        Return available institutions for a country.
        Each dict contains: id, name, bic, transaction_total_days, logo, etc.
        """
        return self.client.get_institutions(country_code)

    def initiate_connection(
        self,
        user,
        institution_id: str,
        institution_name: str,
        institution_country: str,
        transaction_total_days: int,
        redirect_url: str,
    ) -> Requisition:
        """
        Create a GoCardless agreement and requisition, then return the Requisition
        with the link the user must visit to authenticate with their bank.
        """
        # Request the maximum history the institution supports
        max_historical_days = min(transaction_total_days, 730)
        # GoCardless default is 90 days access; use that
        access_valid_for_days = 90

        agreement = self.client.create_agreement(
            institution_id=institution_id,
            max_historical_days=max_historical_days,
            access_valid_for_days=access_valid_for_days,
        )

        reference = str(uuid.uuid4())
        requisition_resp = self.client.create_requisition(
            institution_id=institution_id,
            redirect_url=redirect_url,
            reference=reference,
            agreement_id=agreement["id"],
        )

        requisition = Requisition.objects.create(
            id=requisition_resp["id"],
            user=user,
            institution_id=institution_id,
            institution_name=institution_name,
            institution_country=institution_country,
            status=requisition_resp.get("status", "CR"),
            link=requisition_resp["link"],
            agreement_id=agreement["id"],
            transaction_total_days=transaction_total_days,
            access_valid_for_days=access_valid_for_days,
        )

        logger.info(
            "Requisition %s created for user %s, institution %s",
            requisition.pk, user.pk, institution_name,
        )
        return requisition

    def complete_connection(self, requisition_id: str) -> list[Account]:
        """
        Called after the user returns from bank authentication.
        Creates one Account per requisition, with AccountAsset entries for each
        currency/asset returned by the bank. Triggers initial sync in background.
        """
        from main.models import AccountAsset, Asset
        
        requisition = Requisition.objects.get(pk=requisition_id)
        logger.info("complete_connection: Fetching requisition %s status", requisition.pk)
        req_data = self.client.get_requisition(str(requisition.pk))

        requisition.status = req_data.get("status", requisition.status)
        requisition.save(update_fields=["status"])
        logger.info("complete_connection: Requisition %s status is %s", requisition.pk, requisition.status)

        if requisition.status != "LN":
            logger.warning("complete_connection: Requisition %s not linked (status: %s)", requisition.pk, requisition.status)
            raise ConnectionError(
                f"Requisition {requisition.pk} is not linked (status: {requisition.status}). "
                f"The user may not have completed authentication."
            )

        gc_account_ids = req_data.get("accounts", [])
        logger.info("complete_connection: Requisition %s returned %d account(s)", requisition.pk, len(gc_account_ids))
        if not gc_account_ids:
            logger.error("complete_connection: Requisition %s linked but no accounts returned", requisition.pk)
            raise ConnectionError(
                f"Requisition {requisition.pk} is linked but returned no accounts."
            )

        now = datetime.now(timezone.utc)
        access_expires = now + timedelta(days=requisition.access_valid_for_days)
        
        # Check if this requisition is already linked
        existing_account = Account.objects.filter(gocardless_code=str(requisition.pk)).first()
        
        if existing_account:
            # Update existing account
            existing_account.access_expires_at = access_expires
            existing_account.save(update_fields=["access_expires_at"])
            account = existing_account
            logger.info("Requisition %s already linked to account %s, updated access expiry", requisition.pk, account.pk)
        else:
            # Create new account for this requisition
            account = Account.objects.create(
                user=requisition.user,
                name=requisition.institution_name,
                country=requisition.institution_country,
                gocardless_code=str(requisition.pk),
                gocardless_institution_id=requisition.institution_id,
                transaction_total_days=requisition.transaction_total_days,
                access_expires_at=access_expires,
                sync_status="idle",
            )
            logger.info("Created account %s for requisition %s", account.pk, requisition.pk)

        # Create/update AccountAsset entries for each GoCardless account
        for gc_account_id in gc_account_ids:
            # Fetch account details to determine currency
            account_details, _ = self.client.get_account_details(gc_account_id)
            account_currency = account_details.get("account", {}).get("currency", "")
            
            if not account_currency:
                logger.warning("GoCardless account %s has no currency, skipping", gc_account_id)
                continue
                
            try:
                asset = Asset.objects.get(code=account_currency)
            except Asset.DoesNotExist:
                logger.warning("Currency %s not found in Asset table, skipping GoCardless account %s", 
                             account_currency, gc_account_id)
                continue
            
            # Create or update AccountAsset
            account_asset, created = AccountAsset.objects.get_or_create(
                account=account,
                asset=asset,
                defaults={'gocardless_account_id': gc_account_id}
            )
            
            if not created and account_asset.gocardless_account_id != gc_account_id:
                account_asset.gocardless_account_id = gc_account_id
                account_asset.save(update_fields=['gocardless_account_id'])
                logger.info("Updated AccountAsset %s with new GoCardless ID", account_asset.pk)
            elif created:
                logger.info("Created AccountAsset %s for %s - %s (GC ID: %s)", 
                          account_asset.pk, account.name, asset.code, gc_account_id)

        # Trigger initial sync in background
        if account.sync_status == "idle":
            self._trigger_background_sync(account)

        return [account]

    @staticmethod
    def _trigger_background_sync(account: Account) -> None:
        """Run the initial sync in a background thread."""
        from main.services.stateful.gocardless.sync import SyncAlreadyInProgressError
        
        def _run():
            try:
                syncer = TransactionSyncService()
                syncer.sync_account(account)
                logger.info("Background sync completed for account %s", account.pk)
            except SyncAlreadyInProgressError:
                # Another sync is already running - this is expected in race conditions
                logger.info("Background sync already in progress for account %s", account.pk)
            except Exception:
                logger.exception("Background sync failed for account %s", account.pk)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()


