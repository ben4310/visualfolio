from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from django.conf import settings

from main.models import Account, Asset, PendingConnection
from main.services.stateful.bank_data.providers.base import (
    BankingProvider,
    ConnectedAccountInfo,
    ConnectionResult,
    RateLimitSnapshot,
)
from main.services.stateful.bank_data.providers.gocardless.client import (
    GoCardlessAPIError,
    GoCardlessClient,
)
from main.services.stateful.bank_data.providers.gocardless.mapping import (
    extract_current_balance,
    map_booked_transactions,
)


class GoCardlessProvider(BankingProvider):
    @property
    def provider_type(self) -> str:
        return "gocardless"

    @property
    def is_enabled(self) -> bool:
        return settings.GOCARDLESS_ENABLED

    @property
    def disabled_reason(self) -> str | None:
        if self.is_enabled:
            return None
        return "GoCardless credentials are not configured (GOCARDLESS_SECRET_ID / GOCARDLESS_SECRET_KEY)."

    def __init__(self, client: GoCardlessClient | None = None):
        self.client = client or GoCardlessClient()

    def list_institutions(self, country_code: str) -> list[dict]:
        return self.client.get_institutions(country_code)

    def initiate_auth(
        self,
        *,
        user,
        institution_id: str,
        institution_name: str,
        institution_country: str,
        redirect_url: str,
        callback_url: str,
        transaction_total_days: int | None = None,
    ) -> PendingConnection:
        max_history_days = min(transaction_total_days or 90, 730)
        access_valid_for_days = 90
        agreement = self.client.create_agreement(
            institution_id=institution_id,
            max_historical_days=max_history_days,
            access_valid_for_days=access_valid_for_days,
        )
        requisition = self.client.create_requisition(
            institution_id=institution_id,
            redirect_url=callback_url,
            reference=str(uuid.uuid4()),
            agreement_id=agreement["id"],
        )
        return PendingConnection.objects.create(
            id=requisition["id"],
            user=user,
            provider=self.provider_type,
            institution_id=institution_id,
            institution_name=institution_name,
            institution_country=institution_country,
            status="pending",
            redirect_url=requisition["link"],
            provider_data={
                "agreement_id": agreement["id"],
                "transaction_total_days": transaction_total_days or 90,
                "access_valid_for_days": access_valid_for_days,
            },
        )

    def complete_auth(
        self,
        pending: PendingConnection,
        callback_params: dict,
    ) -> ConnectionResult:
        req_data = self.client.get_requisition(str(pending.pk))
        status = req_data.get("status", "")
        if status != "LN":
            raise ValueError(f"Requisition {pending.pk} is not linked (status: {status})")
        gc_account_ids = req_data.get("accounts", [])
        if not gc_account_ids:
            raise ValueError(f"Requisition {pending.pk} is linked but returned no accounts")
        access_valid_for_days = int(pending.provider_data.get("access_valid_for_days", 90))
        connection_result = ConnectionResult(
            provider_connection_id=str(pending.pk),
            institution_id=pending.institution_id,
            institution_name=pending.institution_name,
            institution_country=pending.institution_country,
            access_expires_at=datetime.now(timezone.utc) + timedelta(days=access_valid_for_days),
            transaction_total_days=int(pending.provider_data.get("transaction_total_days", 90)),
            connected_accounts=[],
        )
        connected = []
        for account_id in gc_account_ids:
            account_details, _ = self.client.get_account_details(account_id)
            connected.append(
                ConnectedAccountInfo(
                    provider_account_id=account_id,
                    currency=account_details.get("account", {}).get("currency") or None,
                )
            )
        return ConnectionResult(
            provider_connection_id=connection_result.provider_connection_id,
            institution_id=connection_result.institution_id,
            institution_name=connection_result.institution_name,
            institution_country=connection_result.institution_country,
            access_expires_at=connection_result.access_expires_at,
            transaction_total_days=connection_result.transaction_total_days,
            connected_accounts=connected,
        )

    def _rate_limits(self, headers: dict) -> RateLimitSnapshot:
        parsed = self.client._parse_account_rate_limit_headers(headers)
        limit = parsed.get("account_success_limit")
        remaining = parsed.get("account_success_remaining")
        reset = parsed.get("account_success_reset")
        return RateLimitSnapshot(
            account_success_limit=int(limit) if limit is not None else None,
            account_success_remaining=int(remaining) if remaining is not None else None,
            account_success_reset_seconds=int(reset) if reset is not None else None,
        )

    def get_balance(
        self,
        provider_account_id: str,
        expected_currency: str,
    ) -> tuple[float, RateLimitSnapshot]:
        data, headers = self.client.get_account_balances(provider_account_id)
        return extract_current_balance(data, expected_currency), self._rate_limits(headers)

    def get_transactions(
        self,
        provider_account_id: str,
        date_from: date,
        date_to: date,
        account: Account,
        expected_asset: Asset,
    ):
        data, headers = self.client.get_account_transactions(
            provider_account_id,
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )
        txns = map_booked_transactions(
            data,
            account=account,
            expected_asset=expected_asset,
            date_from=date_from,
            date_to=date_to,
        )
        return txns, self._rate_limits(headers)

    def resolve_currency(self, provider_account_id: str) -> tuple[str, RateLimitSnapshot]:
        data, headers = self.client.get_account_balances(provider_account_id)
        balances = data.get("balances", [])
        if not balances:
            raise ValueError("No balances returned by provider")
        currency = balances[0].get("balanceAmount", {}).get("currency")
        if not currency:
            raise ValueError("Could not resolve account currency from provider balance data")
        return currency, self._rate_limits(headers)
