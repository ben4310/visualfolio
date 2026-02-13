"""
Service for managing transactions and their balance history updates.

This service centralizes the logic for creating and deleting transactions,
ensuring consistent balance history updates across both regular transactions
and trade operations.
"""
import datetime
from django.db import transaction
from django.core.exceptions import ValidationError
from django.conf import settings
from django.db.models import F

from main.models import Transaction, BalanceHistory
from .calculation import BalanceCalculationService


class TransactionService:
    """Centralizes transaction creation and deletion logic with balance history updates."""
    
    @staticmethod
    def create_transaction_with_balance_update(
        account,
        entity,
        amount,
        datetime_obj,
        asset,
        category=None,
        influences_balance=True,
        is_trade_side=False,
        paired_transaction=None
    ):
        """
        Creates a transaction and updates BalanceHistory accordingly.
        
        Args:
            account: Account model instance
            entity: Transaction entity name (string)
            amount: Transaction amount (float, positive or negative)
            datetime_obj: Transaction datetime (timezone-aware datetime object)
            asset: Asset model instance
            category: Optional category (string)
            influences_balance: If True, applies delta forward (new transaction).
                               If False, recalculates backward (retroactive).
            is_trade_side: If True, marks as part of a trade
            paired_transaction: Optional paired transaction for trades
        
        Returns:
            Transaction: The created transaction
        
        Raises:
            ValidationError: If operation would cause negative balance or if no BH exists
        """
        txn_date = datetime_obj.date()
        
        # Verify BH exists for this (account, asset)
        earliest_bh = (
            BalanceHistory.objects
            .filter(account=account, asset=asset)
            .order_by("date")
            .first()
        )
        if not earliest_bh:
            raise ValidationError(
                f"Asset '{asset.code}' doesn't exist in account '{account.name}'. Please add the asset in the Accounts tab and set its balance first."
            )
        
        with transaction.atomic():
            # Delta mode only: if transaction is before earliest BH, backfill with flat balance
            # (Retroactive mode handles this automatically via backward recalculation)
            if influences_balance and txn_date < earliest_bh.date:
                txns_on_earliest = Transaction.objects_extended.filter(
                    account=account, asset=asset,
                    datetime__date=earliest_bh.date,
                )
                opening_balance = earliest_bh.balance - sum(t.amount for t in txns_on_earliest)
                fill_entries = []
                d = txn_date
                while d < earliest_bh.date:
                    fill_entries.append(
                        BalanceHistory(account=account, asset=asset, date=d, balance=opening_balance)
                    )
                    d += datetime.timedelta(days=1)
                BalanceHistory.objects.bulk_create(fill_entries)
            
            # Create the transaction
            created_txn = Transaction.objects_extended.create(
                account=account,
                entity=entity,
                amount=amount,
                datetime=datetime_obj,
                asset=asset,
                category=category,
                is_trade_side=is_trade_side,
                paired_transaction=paired_transaction,
            )
            
            if influences_balance:
                # Delta mode: propagate amount forward through BH
                BalanceHistory.objects.filter(
                    account=account, asset=asset, date__gte=txn_date,
                ).update(balance=F("balance") + amount)
            else:
                # Retroactive mode: keep current balance, recalculate history backward
                latest_bh = (
                    BalanceHistory.objects
                    .filter(account=account, asset=asset)
                    .order_by("-date")
                    .first()
                )
                all_txns = Transaction.objects_extended.filter(account=account, asset=asset)
                svc = BalanceCalculationService()
                # Extend back to full timespan
                today = datetime.date.today()
                start = today - datetime.timedelta(days=settings.BALANCE_HISTORY_TIMESPAN_DAYS)
                new_history = svc.generate_balance_history_backward(
                    transactions=all_txns,
                    current_balance=latest_bh.balance,
                    start_date=start,
                    end_date=latest_bh.date,
                )
                BalanceHistory.objects.filter(account=account, asset=asset).delete()
                BalanceHistory.objects.bulk_create([
                    BalanceHistory(account=account, asset=asset, date=d, balance=b)
                    for d, b in new_history.items()
                ])
            
            # Validate: no negative balances
            if BalanceHistory.objects.filter(account=account, asset=asset, balance__lt=0).exists():
                raise ValidationError("Transaction would cause a negative balance")
        
        return created_txn
    
    @staticmethod
    def delete_transaction_with_balance_update(txn):
        """
        Deletes a transaction and reverses its balance history impact.
        
        Args:
            txn: Transaction model instance to delete
        
        Raises:
            ValidationError: If deletion would cause negative balance
        """
        account = txn.account
        asset = txn.asset
        amount = txn.amount
        txn_date = txn.datetime.date()
        
        with transaction.atomic():
            # Reverse the delta: subtract the transaction's amount from BH entries
            BalanceHistory.objects.filter(
                account=account, asset=asset, date__gte=txn_date,
            ).update(balance=F("balance") - amount)
            
            # Validate: no negative balances
            if BalanceHistory.objects.filter(account=account, asset=asset, balance__lt=0).exists():
                raise ValidationError(
                    "Cannot delete transaction: would cause negative balance. "
                    "Delete transactions in reverse chronological order or adjust the balance first."
                )
            
            txn.delete()
