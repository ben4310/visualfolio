from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.utils import timezone
from datetime import timedelta, date
from decimal import Decimal
import random

from main.models import AssetClass, Asset, Account, AccountAsset, Transaction, BalanceHistory

User = get_user_model()


class Command(BaseCommand):
    help = 'Load mock data for user test1'

    def handle(self, *args, **options):
        self.stdout.write('Loading static data (AssetClasses and Assets)...')
        self.load_static_data()
        
        self.stdout.write('Creating user test1...')
        user = self.create_test_user()
        
        self.stdout.write('Creating mock accounts...')
        accounts = self.create_accounts(user)
        
        self.stdout.write('Creating account-asset links...')
        self.create_account_assets(accounts)
        
        self.stdout.write('Creating mock transactions...')
        self.create_transactions(accounts)
        
        self.stdout.write('Creating mock trades...')
        self.create_trades(accounts)
        
        self.stdout.write('Creating mock balance history...')
        self.create_balance_history(accounts)
        
        self.stdout.write(self.style.SUCCESS('✓ Mock data loaded successfully!'))
        self.stdout.write(f'  User: test1 (password: 123)')
        self.stdout.write(f'  Accounts: {len(accounts)}')

    def load_static_data(self):
        """Load AssetClasses and Assets from static data"""
        # AssetClasses
        asset_classes = [
            {"name": "Fiat Currencies", "color": "#1AA1F4", "volatility_index": 1},
            {"name": "Publicly Traded Funds", "color": "#F46D1A", "volatility_index": 2},
            {"name": "Public Equity", "color": "#256AF4", "volatility_index": 3},
            {"name": "Decentralized Digital Assets", "color": "#F4AF25", "volatility_index": 4},
        ]
        for ac in asset_classes:
            AssetClass.objects.get_or_create(name=ac["name"], defaults=ac)
        
        # Assets
        assets = [
            {"code": "EUR", "name": "Euro", "symbol": "€", "asset_class": "Fiat Currencies", "is_liquid": True},
            {"code": "USD", "name": "United States dollar", "symbol": "$", "asset_class": "Fiat Currencies", "is_liquid": True},
            {"code": "SPY.US", "name": "SPDR S&P 500 ETF Trust", "asset_class": "Publicly Traded Funds", "is_liquid": False},
            {"code": "AAPL.US", "name": "Apple Inc", "asset_class": "Public Equity", "is_liquid": False},
            {"code": "NVDA.US", "name": "Nvidia Corp", "asset_class": "Public Equity", "is_liquid": False},
            {"code": "GOOG.US", "name": "Alphabet Inc - Class C", "asset_class": "Public Equity", "is_liquid": False},
            {"code": "BTC", "name": "Bitcoin", "asset_class": "Decentralized Digital Assets", "is_liquid": False},
            {"code": "ETH", "name": "Ethereum", "asset_class": "Decentralized Digital Assets", "is_liquid": False},
        ]
        for asset_data in assets:
            asset_class = AssetClass.objects.get(name=asset_data.pop("asset_class"))
            Asset.objects.get_or_create(code=asset_data["code"], defaults={**asset_data, "asset_class": asset_class})

    def create_test_user(self):
        """Create or get test1 user"""
        user, created = User.objects.get_or_create(
            username='test1',
            defaults={
                'email': 'test1@example.com',
                'first_name': 'Test',
                'last_name': 'User'
            }
        )
        if created:
            user.set_password('123')
            user.save()
            self.stdout.write('  Created new user test1')
        else:
            self.stdout.write('  User test1 already exists')
        return user

    def create_accounts(self, user):
        """Create mock accounts"""
        accounts_data = [
            {"name": "Chase Bank", "country": "United States", "color": "#0066CC"},
            {"name": "Interactive Brokers", "country": "United States", "color": "#FF6B00"},
            {"name": "Coinbase", "country": "United States", "color": "#0052FF"},
        ]
        accounts = []
        for acc_data in accounts_data:
            account, _ = Account.objects.get_or_create(
                user=user,
                name=acc_data["name"],
                defaults=acc_data
            )
            accounts.append(account)
        return accounts
    
    def create_account_assets(self, accounts):
        """Create AccountAsset links for mock data"""
        eur = Asset.objects.get(code="EUR")
        usd = Asset.objects.get(code="USD")
        spy = Asset.objects.get(code="SPY.US")
        aapl = Asset.objects.get(code="AAPL.US")
        btc = Asset.objects.get(code="BTC")
        
        # Chase Bank: EUR only (for salary/expenses)
        AccountAsset.objects.get_or_create(account=accounts[0], asset=eur)
        
        # Interactive Brokers: EUR, USD, SPY, AAPL (multi-currency broker)
        for asset in [eur, usd, spy, aapl]:
            AccountAsset.objects.get_or_create(account=accounts[1], asset=asset)
        
        # Coinbase: EUR, BTC (crypto exchange)
        for asset in [eur, btc]:
            AccountAsset.objects.get_or_create(account=accounts[2], asset=asset)

    def create_transactions(self, accounts):
        """Create mock transactions"""
        eur = Asset.objects.get(code="EUR")
        
        # Salary transactions
        for month in range(6):
            dt = timezone.now() - timedelta(days=30 * month)
            Transaction.objects.get_or_create(
                account=accounts[0],  # Chase Bank
                entity="Acme Corp",
                amount=3500.0,
                datetime=dt,
                asset=eur,
                category="Salary"
            )
        
        # Expense transactions
        expenses = [
            ("Amazon", -150.0), ("Grocery Store", -250.0), 
            ("Utilities", -120.0), ("Netflix", -15.0)
        ]
        for i, (entity, amount) in enumerate(expenses):
            for month in range(4):
                dt = timezone.now() - timedelta(days=30 * month + i * 5)
                Transaction.objects.get_or_create(
                    account=accounts[0],
                    entity=entity,
                    amount=amount,
                    datetime=dt,
                    asset=eur,
                    category="Expense"
                )

    def create_trades(self, accounts):
        """Create mock trades as paired transactions"""
        eur = Asset.objects.get(code="EUR")
        usd = Asset.objects.get(code="USD")
        spy = Asset.objects.get(code="SPY.US")
        aapl = Asset.objects.get(code="AAPL.US")
        btc = Asset.objects.get(code="BTC")

        trades_data = [
            # Buy SPY with EUR: +5 SPY, -2500 EUR
            (accounts[1], spy, 5.0, eur, -2500.0, timezone.now() - timedelta(days=150)),
            # Buy AAPL with USD: +10 AAPL, -1800 USD
            (accounts[1], aapl, 10.0, usd, -1800.0, timezone.now() - timedelta(days=120)),
            # Buy BTC with EUR: +0.1 BTC, -5000 EUR
            (accounts[2], btc, 0.1, eur, -5000.0, timezone.now() - timedelta(days=90)),
        ]

        for account, asset, amount, counter_asset, counter_amount, dt in trades_data:
            # Check if trade already exists (both sides must exist with same timestamp)
            existing_txn1 = Transaction.objects_extended.filter(
                account=account,
                entity=f"{asset.code} Buy",
                datetime=dt,
                asset=asset,
                is_trade_side=True,
            ).first()
            
            if not existing_txn1:
                # Create first side of trade
                txn1 = Transaction.objects_extended.create(
                    account=account,
                    entity=f"{asset.code} Buy",
                    amount=amount,
                    datetime=dt,
                    asset=asset,
                    is_trade_side=True,
                )
                
                # Create second side of trade, linked to first
                txn2 = Transaction.objects_extended.create(
                    account=account,
                    entity=f"{asset.code} Buy",
                    amount=counter_amount,
                    datetime=dt,
                    asset=counter_asset,
                    is_trade_side=True,
                    paired_transaction=txn1,
                )
                
                # Link first transaction back to second
                txn1.paired_transaction = txn2
                txn1.save()

    def create_balance_history(self, accounts):
        """Create mock balance history for the last 90 days"""
        eur = Asset.objects.get(code="EUR")
        usd = Asset.objects.get(code="USD")
        spy = Asset.objects.get(code="SPY.US")
        aapl = Asset.objects.get(code="AAPL.US")
        btc = Asset.objects.get(code="BTC")
        
        # Define initial balances
        balances_config = [
            (accounts[0], eur, 15000.0),  # Chase Bank - EUR
            (accounts[1], spy, 5.0),      # Interactive Brokers - SPY
            (accounts[1], aapl, 10.0),    # Interactive Brokers - AAPL
            (accounts[2], btc, 0.1),      # Coinbase - BTC
        ]
        
        # Generate daily balances for the last 90 days
        start_date = timezone.localdate() - timedelta(days=90)
        for day in range(91):
            current_date = start_date + timedelta(days=day)
            
            for account, asset, initial_balance in balances_config:
                # Add some random variation to make it realistic
                if asset.code == "EUR":
                    # Checking account - slight variations
                    balance = initial_balance + random.uniform(-500, 500) + (day * 10)
                elif asset.code in ["SPY.US", "AAPL.US"]:
                    # Stocks - volatile growth
                    variation = initial_balance * (random.uniform(-0.02, 0.03))
                    growth = initial_balance * (day * 0.001)
                    balance = initial_balance + variation + growth
                elif asset.code == "BTC":
                    # Crypto - very volatile
                    variation = initial_balance * (random.uniform(-0.05, 0.05))
                    growth = initial_balance * (day * 0.002)
                    balance = initial_balance + variation + growth
                else:
                    balance = initial_balance
                
                balance = max(0, balance)  # No negative balances
                
                BalanceHistory.objects.get_or_create(
                    account=account,
                    asset=asset,
                    date=current_date,
                    defaults={'balance': balance}
                )
