from __future__ import annotations

import math
from datetime import date

from main.utils.market_data import get_prices_daily


class MarketCurrencyConversionError(Exception):
    """Raised when market-data currency conversion cannot be computed."""


def _get_eod_price_eur(instrument: str, conversion_date: date) -> float:
    try:
        price = float(get_prices_daily(instrument, "EUR", conversion_date).iloc[-1])
    except Exception as exc:  # pragma: no cover - error handling path
        raise MarketCurrencyConversionError(
            f"Missing market data for '{instrument}/EUR' on {conversion_date}."
        ) from exc

    if not math.isfinite(price) or price <= 0:
        raise MarketCurrencyConversionError(
            f"Invalid market data for '{instrument}/EUR' on {conversion_date}: {price}."
        )
    return price


def convert_amount_with_eod_market_data(
    amount: float,
    source_currency: str,
    target_currency: str,
    conversion_date: date,
    txn_id: str,
) -> float:
    """
    Convert an amount between two currencies using end-of-day prices quoted in EUR.

    The conversion uses same-day market close as an approximation of intraday rates.
    """
    if source_currency == target_currency:
        return amount

    try:
        from_price_eur = _get_eod_price_eur(source_currency, conversion_date)
        to_price_eur = _get_eod_price_eur(target_currency, conversion_date)
        converted_amount = amount * (from_price_eur / to_price_eur)
    except MarketCurrencyConversionError as exc:
        raise MarketCurrencyConversionError(
            "Could not convert transaction "
            f"'{txn_id}' from {source_currency} to {target_currency} on {conversion_date}: {exc}"
        ) from exc

    if not math.isfinite(converted_amount):
        raise MarketCurrencyConversionError(
            f"Computed non-finite converted amount for transaction '{txn_id}'."
        )

    return converted_amount
