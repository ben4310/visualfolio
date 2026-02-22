from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime

from main.models import Account, Asset, PendingConnection, Transaction


@dataclass(frozen=True)
class ConnectedAccountInfo:
    provider_account_id: str
    currency: str | None = None


@dataclass(frozen=True)
class ConnectionResult:
    provider_connection_id: str
    institution_id: str
    institution_name: str
    institution_country: str
    access_expires_at: datetime | None
    transaction_total_days: int
    connected_accounts: list[ConnectedAccountInfo] = field(default_factory=list)


@dataclass(frozen=True)
class RateLimitSnapshot:
    account_success_limit: int | None = None
    account_success_remaining: int | None = None
    account_success_reset_seconds: int | None = None


class BankingProvider(ABC):
    @property
    @abstractmethod
    def provider_type(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def is_enabled(self) -> bool:
        raise NotImplementedError

    @property
    def disabled_reason(self) -> str | None:
        """Human-readable explanation of why this provider is unavailable, or None if enabled."""
        return None

    @abstractmethod
    def list_institutions(self, country_code: str) -> list[dict]:
        raise NotImplementedError

    @abstractmethod
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
        raise NotImplementedError

    @abstractmethod
    def complete_auth(
        self,
        pending: PendingConnection,
        callback_params: dict,
    ) -> ConnectionResult:
        raise NotImplementedError

    @abstractmethod
    def get_balance(
        self,
        provider_account_id: str,
        expected_currency: str,
    ) -> tuple[float, RateLimitSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def get_transactions(
        self,
        provider_account_id: str,
        date_from: date,
        date_to: date,
        account: Account,
        expected_asset: Asset,
    ) -> tuple[list[Transaction], RateLimitSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def resolve_currency(self, provider_account_id: str) -> tuple[str, RateLimitSnapshot]:
        raise NotImplementedError
