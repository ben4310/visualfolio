"""
Strict mapping from GoCardless Bank Account Data API responses
to internal Transaction model instances.
"""

import logging
from datetime import date, datetime, timezone

from main.models import Account, Asset, Transaction

logger = logging.getLogger(__name__)


class CurrencyMismatchError(Exception):
    """Raised when a transaction's currency does not match the expected account currency."""

    def __init__(self, account: Account, expected: str, actual: str, txn_id: str, bank_fault: bool = False):
        self.expected = expected
        self.actual = actual
        self.bank_fault = bank_fault
        
        if bank_fault:
            msg = (
                f"Account '{account.name}' (ID {account.pk}): "
                f"Bank returned transaction '{txn_id}' in {actual} without providing currency "
                f"conversion to account currency {expected}. This is a limitation of the bank's "
                f"PSD2 API implementation - the 'currencyExchange' field is optional under PSD2, "
                f"and this bank chose not to provide it. Contact your bank to request proper "
                f"multi-currency support, or use Visualfolio's market rate conversion (coming soon)."
            )
        else:
            msg = (
                f"Account '{account.name}' (ID {account.pk}): "
                f"expected currency '{expected}' but transaction '{txn_id}' has currency '{actual}'"
            )
        super().__init__(msg)


class TransactionMappingError(Exception):
    """Raised when a transaction cannot be mapped due to missing required fields."""


def extract_current_balance(balances_response: dict, expected_currency: str) -> float:
    """
    Extract the current balance from a GoCardless balances API response.
    Prefers 'interimBooked' or 'interimAvailable', falls back to any available balance.
    Raises CurrencyMismatchError if the balance currency doesn't match.
    """
    balances = balances_response.get("balances", [])
    if not balances:
        raise TransactionMappingError("No balances returned by API")

    # Preference order for balance types
    preference = ["interimBooked", "interimAvailable", "closingBooked", "expected"]

    selected = None
    for pref in preference:
        for bal in balances:
            if bal.get("balanceType") == pref:
                selected = bal
                break
        if selected:
            break

    # Fall back to first available balance
    if selected is None:
        selected = balances[0]

    amount_obj = selected.get("balanceAmount", {})
    currency = amount_obj.get("currency", "")
    amount = float(amount_obj.get("amount", 0))

    if currency != expected_currency:
        raise CurrencyMismatchError(
            account=None,  # type: ignore[arg-type]
            expected=expected_currency,
            actual=currency,
            txn_id="balance",
        )

    return amount


def map_booked_transactions(
    transactions_response: dict,
    account: Account,
    expected_asset: Asset,
    date_from: date,
    date_to: date,
) -> list[Transaction]:
    """
    Map booked transactions from the GoCardless API response to unsaved Transaction instances.
    Only includes transactions within [date_from, date_to].
    Raises CurrencyMismatchError on currency mismatches.
    Silently skips zero-amount transactions.
    """
    booked = transactions_response.get("transactions", {}).get("booked", [])
    mapped: list[Transaction] = []

    for raw_txn in booked:
        txn_id = (
            raw_txn.get("transactionId")
            or raw_txn.get("internalTransactionId")
            or "unknown"
        )

        # --- Amount and currency ---
        amount_obj = raw_txn.get("transactionAmount", {})
        currency = amount_obj.get("currency", "")
        try:
            amount = float(amount_obj.get("amount", 0))
        except (ValueError, TypeError):
            raise TransactionMappingError(
                f"Transaction '{txn_id}': invalid amount '{amount_obj.get('amount')}'"
            )

        # Handle currency mismatch: check for currencyExchange field
        if currency != expected_asset.code:
            currency_exchange = raw_txn.get("currencyExchange")
            
            # Try to extract converted amount from currencyExchange
            # PSD2 allows this to be either a dict or a list
            converted_amount = None
            target_currency = None
            
            if isinstance(currency_exchange, dict):
                # Single exchange: check if it converts to our expected currency
                target_currency = currency_exchange.get("targetCurrency") or currency_exchange.get("sourceCurrency")
                if target_currency == expected_asset.code:
                    try:
                        converted_amount = float(currency_exchange.get("exchangeRate", 0)) * amount
                        # Some banks provide the converted amount directly
                        if "targetAmount" in currency_exchange:
                            converted_amount = float(currency_exchange["targetAmount"])
                    except (ValueError, TypeError, KeyError):
                        pass
            
            elif isinstance(currency_exchange, list) and currency_exchange:
                # Multiple exchanges: find the one that converts to our expected currency
                for exchange in currency_exchange:
                    target_currency = exchange.get("targetCurrency") or exchange.get("sourceCurrency")
                    if target_currency == expected_asset.code:
                        try:
                            converted_amount = float(exchange.get("exchangeRate", 0)) * amount
                            if "targetAmount" in exchange:
                                converted_amount = float(exchange["targetAmount"])
                            break
                        except (ValueError, TypeError, KeyError):
                            continue
            
            # If we found a conversion, use it
            if converted_amount is not None and target_currency == expected_asset.code:
                logger.info(
                    "Transaction '%s': converted %s %s to %s %s using bank-provided exchange rate",
                    txn_id, amount, currency, converted_amount, expected_asset.code
                )
                amount = converted_amount
                # Update currency to match
                currency = expected_asset.code
            else:
                # No conversion available - this is the bank's fault
                raise CurrencyMismatchError(
                    account=account,
                    expected=expected_asset.code,
                    actual=currency,
                    txn_id=txn_id,
                    bank_fault=True,  # Bank didn't provide optional currencyExchange field
                )

        # Skip zero-amount transactions
        if amount == 0:
            logger.debug("Skipping zero-amount transaction '%s'", txn_id)
            continue

        # --- Date ---
        booking_date_str = raw_txn.get("bookingDate") or raw_txn.get("valueDate")
        if not booking_date_str:
            raise TransactionMappingError(
                f"Transaction '{txn_id}': no bookingDate or valueDate"
            )
        try:
            booking_date = date.fromisoformat(booking_date_str)
        except ValueError:
            raise TransactionMappingError(
                f"Transaction '{txn_id}': invalid date '{booking_date_str}'"
            )

        # Filter to requested range
        if booking_date < date_from or booking_date > date_to:
            continue

        # --- Entity ---
        entity = (
            raw_txn.get("creditorName")
            or raw_txn.get("debtorName")
            or raw_txn.get("remittanceInformationUnstructured")
            or "Unknown"
        )
        # Truncate to model field max_length
        entity = entity[:255]

        # --- Category ---
        category = raw_txn.get("merchantCategoryCode") or None
        if category:
            category = category[:31]

        # --- Build unsaved Transaction ---
        txn_datetime = datetime(
            booking_date.year, booking_date.month, booking_date.day,
            tzinfo=timezone.utc,
        )
        mapped.append(
            Transaction(
                account=account,
                entity=entity,
                amount=amount,
                datetime=txn_datetime,
                asset=expected_asset,
                category=category,
            )
        )

    return mapped
