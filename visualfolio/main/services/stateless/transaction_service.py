"""
Service for managing transactions and their balance history updates.

This service centralizes the logic for creating and deleting transactions,
ensuring consistent balance history updates across both regular transactions
and trade operations.
"""
import datetime
from django.db import transaction
from django.core.exceptions import ValidationError
from django.db.models import F

from main.models import Transaction, BalanceHistory


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
            # If transaction is before earliest BH, backfill with flat balance
            if txn_date < earliest_bh.date:
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
            
            # Get latest BH for forward-fill check
            latest_bh = (
                BalanceHistory.objects
                .filter(account=account, asset=asset)
                .order_by("-date")
                .first()
            )
            
            # If transaction is after latest BH, forward-fill with last balance
            if txn_date > latest_bh.date:
                fill_entries = []
                d = latest_bh.date + datetime.timedelta(days=1)
                while d <= txn_date:
                    fill_entries.append(
                        BalanceHistory(account=account, asset=asset, date=d, balance=latest_bh.balance)
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
            
            # Propagate amount forward through balance history
            BalanceHistory.objects.filter(
                account=account, asset=asset, date__gte=txn_date,
            ).update(balance=F("balance") + amount)
            
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
