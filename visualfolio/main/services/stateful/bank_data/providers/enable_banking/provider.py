from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone

from django.conf import settings
from django.utils.dateparse import parse_datetime

from main.models import Account, Asset, PendingConnection
from main.services.stateful.bank_data.providers.base import (
    BankingProvider,
    ConnectedAccountInfo,
    ConnectionResult,
    RateLimitSnapshot,
)
from main.services.stateful.bank_data.providers.enable_banking.client import (
    EnableBankingClient,
)
from main.services.stateful.bank_data.providers.enable_banking.mapping import (
    extract_current_balance,
    map_transactions,
)

DEFAULT_TRANSACTION_TOTAL_DAYS = 90


class EnableBankingProvider(BankingProvider):
    @property
    def provider_type(self) -> str:
        return "enable_banking"

    @property
    def is_enabled(self) -> bool:
        return bool(
            settings.ENABLE_BANKING_KEY_CONTENT and settings.ENABLE_BANKING_APPLICATION_ID
        )

    @property
    def disabled_reason(self) -> str | None:
        if self.is_enabled:
            return None
        return "Enable Banking credentials are not configured (ENABLE_BANKING_APPLICATION_ID / ENABLE_BANKING_KEY_CONTENT)."

    def __init__(self, client: EnableBankingClient | None = None):
        self.client = client or EnableBankingClient()

    def list_institutions(self, country_code: str) -> list[dict]:
        aspsps = self.client.get_aspsps(country_code)
        return [
            {
                "id": f"{aspsp['country']}::{aspsp['name']}",
                "name": aspsp["name"],
                "country": aspsp["country"],
                "logo": aspsp.get("logo"),
                "transaction_total_days": None,
            }
            for aspsp in aspsps
        ]

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
        # institution_id format for EB is "{country_code}::{name}" (e.g. "IT::Fineco Bank").
        # The API requires the 2-letter ISO code, not the display country name.
        aspsp_country_code = institution_id.split("::")[0]
        pending_id = uuid.uuid4()
        auth_response = self.client.start_auth(
            {
                "access": {
                    "valid_until": (
                        datetime.now(timezone.utc) + timedelta(days=10)
                    ).isoformat()
                },
                "aspsp": {"name": institution_name, "country": aspsp_country_code},
                "state": str(pending_id),
                "redirect_url": callback_url,
                "psu_type": "personal",
            }
        )
        return PendingConnection.objects.create(
            id=pending_id,
            user=user,
            provider=self.provider_type,
            institution_id=institution_id,
            institution_name=institution_name,
            institution_country=institution_country,
            status="pending",
            redirect_url=auth_response["url"],
            provider_data={
                "authorization_id": auth_response.get("authorization_id"),
            },
        )

    def complete_auth(
        self,
        pending: PendingConnection,
        callback_params: dict,
    ) -> ConnectionResult:
        code = callback_params.get("code")
        if not code:
            raise ValueError("Missing authorization code in callback")

        session = self.client.create_session(code)
        session_id = session["session_id"]
        session_data = self.client.get_session(session_id)
        valid_until = parse_datetime((session_data.get("access") or {}).get("valid_until", ""))

        connected_accounts = [
            ConnectedAccountInfo(
                provider_account_id=acc["uid"],
                currency=acc.get("currency"),
            )
            for acc in session.get("accounts", [])
        ]
        return ConnectionResult(
            provider_connection_id=session_id,
            institution_id=pending.institution_id,
            institution_name=pending.institution_name,
            institution_country=pending.institution_country,
            access_expires_at=valid_until,
            transaction_total_days=DEFAULT_TRANSACTION_TOTAL_DAYS,
            connected_accounts=connected_accounts,
        )

    def get_balance(
        self,
        provider_account_id: str,
        expected_currency: str,
    ) -> tuple[float, RateLimitSnapshot]:
        data = self.client.get_balances(provider_account_id)
        return extract_current_balance(data, expected_currency), RateLimitSnapshot()

    def get_transactions(
        self,
        provider_account_id: str,
        date_from: date,
        date_to: date,
        account: Account,
        expected_asset: Asset,
    ) -> tuple[list, RateLimitSnapshot]:
        data = self.client.get_transactions(
            provider_account_id,
            date_from.isoformat(),
            date_to.isoformat(),
        )
        return (
            map_transactions(
                data,
                account=account,
                expected_asset=expected_asset,
                date_from=date_from,
                date_to=date_to,
            ),
            RateLimitSnapshot(),
        )

    def resolve_currency(self, provider_account_id: str) -> tuple[str, RateLimitSnapshot]:
        data = self.client.get_balances(provider_account_id)
        balances = data.get("balances", [])
        if not balances:
            raise ValueError("No balances returned by provider")
        currency = balances[0].get("balance_amount", {}).get("currency")
        if not currency:
            raise ValueError("Could not resolve account currency from provider balance data")
        return currency, RateLimitSnapshot()
