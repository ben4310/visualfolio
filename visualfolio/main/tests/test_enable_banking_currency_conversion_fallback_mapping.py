from datetime import date
from unittest.mock import patch

import pandas as pd
from django.test import TestCase

from main.models import Account, Asset, AssetClass, CustomUser
from main.services.stateful.bank_data.providers.enable_banking.mapping import (
    TransactionMappingError,
    map_transactions,
)


class EnableBankingCurrencyConversionFallbackMappingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(username="ebcurrency", password="pw")
        cls.asset_class, _ = AssetClass.objects.get_or_create(
            name="Fiat Currencies",
            defaults={"color": "#000000", "volatility_index": 0.0},
        )
        cls.eur, _ = Asset.objects.get_or_create(
            code="EUR",
            defaults={"name": "Euro", "asset_class": cls.asset_class, "is_liquid": True},
        )
        cls.account = Account.objects.create(
            user=cls.user,
            name="EB Account",
            country="IT",
            account_type="enable_banking",
        )

    def _map_one(self, txn_payload: dict):
        data = {"transactions": [txn_payload]}
        txns = map_transactions(
            data,
            account=self.account,
            expected_asset=self.eur,
            date_from=date(2025, 1, 1),
            date_to=date(2025, 1, 31),
        )
        self.assertEqual(len(txns), 1)
        return txns[0]

    @patch("main.services.stateful.bank_data.currency_conversion_fallback.get_prices_daily")
    def test_uses_bank_conversion_when_present(self, mocked_prices):
        mocked_prices.side_effect = AssertionError("Market fallback should not run")
        mapped = self._map_one(
            {
                "transaction_id": "eb-bank-priority",
                "transaction_amount": {"amount": "10", "currency": "USD"},
                "credit_debit_indicator": "CRDT",
                "currency_exchange": {
                    "target_currency": "EUR",
                    "exchange_rate": "1.1",
                },
                "booking_date": "2025-01-15",
                "creditor": {"name": "Vendor"},
            }
        )
        self.assertEqual(mapped.amount, 11.0)

    @patch("main.services.stateful.bank_data.currency_conversion_fallback.get_prices_daily")
    def test_uses_market_fallback_when_bank_conversion_missing(self, mocked_prices):
        mocked_prices.side_effect = [
            pd.Series([0.8], index=pd.Index([date(2025, 1, 15)], name="date")),
            pd.Series([1.0], index=pd.Index([date(2025, 1, 15)], name="date")),
        ]
        mapped = self._map_one(
            {
                "transaction_id": "eb-market-fallback",
                "transaction_amount": {"amount": "10", "currency": "USD"},
                "credit_debit_indicator": "CRDT",
                "booking_date": "2025-01-15",
                "creditor": {"name": "Vendor"},
            }
        )
        self.assertEqual(mapped.amount, 8.0)

    @patch("main.services.stateful.bank_data.currency_conversion_fallback.get_prices_daily")
    def test_raises_mapping_error_when_market_data_missing(self, mocked_prices):
        mocked_prices.side_effect = Exception("no market data")
        data = {
            "transactions": [
                {
                    "transaction_id": "eb-no-rate",
                    "transaction_amount": {"amount": "10", "currency": "USD"},
                    "credit_debit_indicator": "CRDT",
                    "booking_date": "2025-01-15",
                }
            ]
        }
        with self.assertRaises(TransactionMappingError):
            map_transactions(
                data,
                account=self.account,
                expected_asset=self.eur,
                date_from=date(2025, 1, 1),
                date_to=date(2025, 1, 31),
            )
