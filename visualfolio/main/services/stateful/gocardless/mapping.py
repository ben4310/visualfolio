"""
Mapping from GoCardless Bank Account Data API responses
to internal Transaction model instances.

Date parsing is intentionally tolerant: banks vary widely in how they format
dates (date-only, full ISO 8601, with/without timezone). When timezone info
is absent, the user's configured timezone is assumed; when only a date is
available the time is set to 12:00 (midday) to minimise date-boundary errors.
"""

import logging
import zoneinfo
from datetime import date, datetime, time, timezone

import dateutil.parser

from main.models import Account, Asset, Transaction

logger = logging.getLogger(__name__)

MIDDAY = time(12, 0)


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
    Prefers available balances, then booked, to match transaction inclusion.
    Raises CurrencyMismatchError if the balance currency doesn't match.
    """
    balances = balances_response.get("balances", [])
    if not balances:
        raise TransactionMappingError("No balances returned by API")

    # Preference order for balance types: available first, then booked
    preference = ["interimAvailable", "interimBooked", "closingBooked", "expected"]

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


def _parse_transaction_datetime(
    raw_date_str: str | None,
    txn_id: str,
    user_tz: zoneinfo.ZoneInfo,
) -> datetime:
    """
    Parse a date or datetime string from the bank API into a UTC-aware datetime.

    Strategy (ordered by priority):
      1. Full ISO 8601 with timezone  -> use as-is, convert to UTC.
      2. Full ISO 8601 without tz     -> assume *user_tz*, convert to UTC.
      3. Date-only (YYYY-MM-DD etc.)  -> set time to midday in *user_tz*, convert to UTC.
      4. Any other parseable format   -> dateutil fallback, same tz rules.
      5. None / unparseable           -> midday today in *user_tz*, convert to UTC.
    """
    if not raw_date_str:
        logger.warning(
            "Transaction '%s': no date provided, defaulting to midday today",
            txn_id,
        )
        today = datetime.now(timezone.utc).astimezone(user_tz).date()
        return datetime.combine(today, MIDDAY, tzinfo=user_tz).astimezone(timezone.utc)

    raw_date_str = raw_date_str.strip()

    # Try date-only first (YYYY-MM-DD) -- must come before datetime.fromisoformat
    # because Python 3.11+ accepts bare dates as datetimes (returning midnight).
    try:
        d = date.fromisoformat(raw_date_str)
        return datetime.combine(d, MIDDAY, tzinfo=user_tz).astimezone(timezone.utc)
    except ValueError:
        pass

    # Full ISO 8601 datetime (handles 2024-02-27T15:05:06.123Z, 2024-02-27T15:05:06+01:00, etc.)
    try:
        dt = datetime.fromisoformat(raw_date_str)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        # Naive datetime: assume user timezone
        return dt.replace(tzinfo=user_tz).astimezone(timezone.utc)
    except ValueError:
        pass

    # Fallback: dateutil tolerant parser for non-standard formats
    try:
        dt = dateutil.parser.parse(raw_date_str)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
        # If dateutil parsed only a date (time is midnight and no time indicators in string),
        # treat as date-only and use midday
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and ":" not in raw_date_str:
            return datetime.combine(dt.date(), MIDDAY, tzinfo=user_tz).astimezone(timezone.utc)
        return dt.replace(tzinfo=user_tz).astimezone(timezone.utc)
    except (ValueError, dateutil.parser.ParserError):
        pass

    logger.warning(
        "Transaction '%s': unparseable date '%s', defaulting to midday today",
        txn_id, raw_date_str,
    )
    today = datetime.now(timezone.utc).astimezone(user_tz).date()
    return datetime.combine(today, MIDDAY, tzinfo=user_tz).astimezone(timezone.utc)


def map_booked_transactions(
    transactions_response: dict,
    account: Account,
    expected_asset: Asset,
    date_from: date,
    date_to: date,
    user_tz: zoneinfo.ZoneInfo | None = None,
) -> list[Transaction]:
    """
    Map transactions from the GoCardless API response to unsaved Transaction instances.
    Includes both booked and pending transactions.
    Only includes transactions within [date_from, date_to].
    Raises CurrencyMismatchError on currency mismatches.
    Silently skips zero-amount transactions.

    Args:
        user_tz: The account owner's timezone. Used as fallback when bank-provided
                 dates lack timezone info. Defaults to UTC if not provided.
    """
    if user_tz is None:
        user_tz = zoneinfo.ZoneInfo("UTC")
    booked = transactions_response.get("transactions", {}).get("booked", [])
    pending = transactions_response.get("transactions", {}).get("pending", [])
    all_transactions = booked + pending
    mapped: list[Transaction] = []

    for raw_txn in all_transactions:
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

        # --- Date/Datetime ---
        booking_date_str = (
            raw_txn.get("bookingDateTime")
            or raw_txn.get("bookingDate")
            or raw_txn.get("valueDateTime")
            or raw_txn.get("valueDate")
        )
        txn_datetime = _parse_transaction_datetime(booking_date_str, txn_id, user_tz)
        booking_date = txn_datetime.date()

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
