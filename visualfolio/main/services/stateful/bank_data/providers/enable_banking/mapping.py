from __future__ import annotations

import zoneinfo
from datetime import date

from main.models import Account, Asset, Transaction
from main.services.stateful.bank_data.providers.gocardless.mapping import (
    TransactionMappingError,
    _parse_transaction_datetime,
)


def extract_current_balance(balances_response: dict, expected_currency: str) -> float:
    balances = balances_response.get("balances", [])
    if not balances:
        raise TransactionMappingError("No balances returned by API")

    preference = ["ITAV", "ITBD", "CLAV"]
    selected = None
    for balance_type in preference:
        selected = next((b for b in balances if b.get("balance_type") == balance_type), None)
        if selected:
            break
    if selected is None:
        selected = balances[0]

    amount_obj = selected.get("balance_amount", {})
    currency = amount_obj.get("currency", "")
    amount = float(amount_obj.get("amount", 0))
    if currency != expected_currency:
        raise TransactionMappingError(
            f"Expected balance currency '{expected_currency}', got '{currency}'"
        )
    return amount


def map_transactions(
    transactions_response: dict,
    account: Account,
    expected_asset: Asset,
    date_from: date,
    date_to: date,
    user_tz: zoneinfo.ZoneInfo | None = None,
) -> list[Transaction]:
    if user_tz is None:
        user_tz = zoneinfo.ZoneInfo("UTC")
    mapped: list[Transaction] = []
    for raw_txn in transactions_response.get("transactions", []):
        amount_obj = raw_txn.get("transaction_amount", {})
        currency = amount_obj.get("currency", "")
        if currency != expected_asset.code:
            raise TransactionMappingError(
                f"Expected transaction currency '{expected_asset.code}', got '{currency}'"
            )

        amount = float(amount_obj.get("amount", 0))
        indicator = raw_txn.get("credit_debit_indicator", "")
        if indicator == "DBIT":
            amount = -abs(amount)
        elif indicator == "CRDT":
            amount = abs(amount)

        if amount == 0:
            continue

        booking_date_str = (
            raw_txn.get("booking_date")
            or raw_txn.get("value_date")
            or raw_txn.get("transaction_date")
        )
        txn_datetime = _parse_transaction_datetime(
            booking_date_str,
            raw_txn.get("transaction_id") or "unknown",
            user_tz,
        )
        txn_date = txn_datetime.date()
        if txn_date < date_from or txn_date > date_to:
            continue

        creditor_name = (raw_txn.get("creditor") or {}).get("name")
        debtor_name = (raw_txn.get("debtor") or {}).get("name")
        remittance_list = raw_txn.get("remittance_information") or []
        remittance = remittance_list[0] if remittance_list else None
        entity = creditor_name or debtor_name or remittance or "Unknown"
        category = (raw_txn.get("merchant_category_code") or "")[:31] or None

        mapped.append(
            Transaction(
                account=account,
                entity=entity[:255],
                amount=amount,
                datetime=txn_datetime,
                asset=expected_asset,
                category=category,
            )
        )
    return mapped
