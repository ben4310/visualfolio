import datetime 
from collections import OrderedDict
from django.core.exceptions import ValidationError


class BalanceCalculationService:
    def generate_balance_history_backward(self, transactions, current_balance, start_date, end_date):
        """Backward calculation from a known current balance.

        Args:
            transactions: Complete set of all transactions that took place within [start_date, end_date].*
            current_balance: Balance at end of end_date.
            start_date: Start of transaction period.
            end_date: End of transaction period.

            *= Not as in all days having transactions, but as having the guarantee that in the
            start_date -> end_date timespan all transactions that took place are included in the transactions set.
            The set might even be completely empty if no transactions took place.

        Returns:
            OrderedDict of date -> balance. Important: the balance at the end of the day before start_date 
            is also included as this value is computable with the known data. This means the returned date range is
            one day longer than the input date range.

        Raises:
            ValidationError: If any intermediate balance goes negative.
        """
        transactions_by_date = {}
        for txn in transactions:
            transaction_date = txn.datetime.date()
            transactions_by_date.setdefault(transaction_date, []).append(txn)

        reversed_date_list = []
        current_date = end_date
        opening_date = start_date - datetime.timedelta(days=1)
        while current_date >= opening_date:
            reversed_date_list.append(current_date)
            current_date -= datetime.timedelta(days=1)

        balance_history = OrderedDict()
        balance = current_balance

        for current_date in reversed_date_list:
            
            balance_history[current_date] = balance
            
            if current_date in transactions_by_date:
                for txn in transactions_by_date[current_date]:
                    amount = txn.amount
                    balance -= amount
                    if balance < 0:
                        raise ValidationError(
                            f"Negative balance for {txn.account} - {txn.asset} on {current_date}"
                        )

        return balance_history
    