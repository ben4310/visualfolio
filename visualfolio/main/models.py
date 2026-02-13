from django.db import models
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator, ValidationError
from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone


from .constants import COUNTRY_CHOICES


'''
<< Static models >>
'''

class AssetClass(models.Model):
    name = models.CharField(max_length=63, primary_key=True)
    color = models.CharField(max_length=7)
    volatility_index = models.FloatField()


class Asset(models.Model):
    code = models.CharField(max_length=15, primary_key=True)
    name = models.CharField(max_length=127)
    symbol = models.CharField(max_length=7, null=True, blank=True)
    asset_class = models.ForeignKey(AssetClass, on_delete=models.PROTECT)
    is_liquid = models.BooleanField()


'''
<< User models >>
'''

class CustomUser(AbstractUser):
    last_update = models.DateTimeField(null=True, blank=True)
    last_full_refresh = models.DateTimeField(null=True, blank=True)


class Account(models.Model):
    SYNC_STATUS_CHOICES = [
        ("idle", "Idle"),
        ("syncing", "Syncing"),
        ("success", "Success"),
        ("error", "Error"),
    ]

    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    country = models.CharField(max_length=255, choices=COUNTRY_CHOICES)
    gocardless_code = models.UUIDField(blank=True, null=True, unique=True)  # Requisition ID for new accounts, Account ID for legacy
    color = models.CharField(max_length=7, default="#4b8dd3")

    # GoCardless-specific fields (populated at link time, null for manual accounts)
    gocardless_institution_id = models.CharField(max_length=64, blank=True, null=True)
    transaction_total_days = models.PositiveIntegerField(blank=True, null=True)
    last_successful_sync = models.DateTimeField(blank=True, null=True)
    access_expires_at = models.DateTimeField(blank=True, null=True)

    # Sync status tracking
    sync_status = models.CharField(max_length=10, choices=SYNC_STATUS_CHOICES, default="idle")
    sync_error = models.TextField(blank=True, null=True)

    # Per-account rate limits (GoCardless account-success limits for data fetching)
    account_success_limit = models.PositiveIntegerField(blank=True, null=True)
    account_success_remaining = models.PositiveIntegerField(blank=True, null=True)
    account_success_reset = models.DateTimeField(blank=True, null=True)

    @property
    def is_synced(self):
        return self.gocardless_code is not None

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['name', 'user'],
                name='unique_account_name_per_user',
            )
        ]


class AccountAsset(models.Model):
    """
    Links an Account to an Asset, storing external provider metadata.
    
    For GoCardless synced accounts: stores the GoCardless account ID (their
    internal identifier for each account-currency combination).
    
    For manual accounts: exists only to track which assets have been established
    on the account (no gocardless_account_id).
    """
    id = models.AutoField(primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='account_assets')
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    gocardless_account_id = models.CharField(max_length=64, blank=True, null=True)
    
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'asset'],
                name='unique_account_asset_pair',
            )
        ]
    
    def __str__(self):
        return f"{self.account.name} - {self.asset.code}"


class TransactionManager(models.Manager):
    """Default manager that excludes trade sides."""
    def get_queryset(self):
        return super().get_queryset().filter(is_trade_side=False)


class AllTransactionsManager(models.Manager):
    """Manager that includes all transactions (including trade sides)."""
    pass


class Transaction(models.Model):
    def validate_transaction_amount(value):
        if value == 0:
            raise ValidationError("Transaction amount can't be zero.")

    def validate_not_future(value):
        if value > timezone.now():
            raise ValidationError("Transaction date cannot be in the future.")
    
    id = models.AutoField(primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    entity = models.CharField(max_length=255, blank=True)
    amount = models.FloatField(validators=[validate_transaction_amount])
    datetime = models.DateTimeField(validators=[validate_not_future])
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    category = models.CharField(max_length=31, null=True, blank=True)
    is_trade_side = models.BooleanField(default=False, db_index=True)
    paired_transaction = models.ForeignKey(
        'self', on_delete=models.CASCADE, null=True, blank=True, related_name='trade_pair'
    )

    objects = TransactionManager()  # Default: excludes trade sides
    objects_extended = AllTransactionsManager()  # Includes trade sides

    def clean(self):
        super().clean()
        if not self.is_trade_side and not self.entity:
            raise ValidationError({"entity": "From/to is required for non-trade transactions."})
        if not self.is_trade_side and self.asset and self.asset.asset_class_id != "Fiat Currencies":
            raise ValidationError({"asset": "Transactions can only use fiat currency assets."})

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)
    

class BalanceHistory(models.Model):
    id = models.AutoField(primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT)
    date = models.DateField()
    balance = models.FloatField(validators=[MinValueValidator(0.0)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['account', 'asset', 'date'], name='unique_account_asset_date')
        ]
        ordering = ['account', 'asset', 'date']

    @classmethod
    def get_valid_range_dates(cls, user):
        """
        Returns a tuple (start_date, end_date) representing the latest continuous period
        where all account-asset combinations have records.
        Returns (None, None) if no valid range exists.
        """
        # Get count of ALL account-asset combinations for the user
        n_all_combinations = cls.objects.filter(
            account__user=user
        ).values('account', 'asset').distinct().count()
        
        if not n_all_combinations:
            return None, None
        
        # Get all dates where every combination has a record, ordered by date ascending
        complete_dates = cls.objects.filter(
            account__user=user
        ).values('date').annotate(
            combination_count=Count('id', distinct=True, filter=models.Q(account__user=user))
        ).filter(
            combination_count=n_all_combinations
        ).order_by('date').values_list('date', flat=True)
        
        complete_dates = list(complete_dates)

        if not complete_dates:
            return None, None
        
        # Set the end of the valid range to the latest complete date
        valid_range_end = complete_dates[-1]
        valid_range_start = valid_range_end
        
        # Find the first discontinuity going backwards
        for i in range(len(complete_dates) - 1, 0, -1):
            if (complete_dates[i] - complete_dates[i - 1]).days > 1:  # Gap found
                break
            # Keep moving start date back as long as dates are continuous
            valid_range_start = complete_dates[i - 1]
        
        return valid_range_start, valid_range_end

    @classmethod
    def get_valid_range_records(cls, user):
        """
        Returns BalanceHistory records within the valid date range for a user.
        A valid range is defined as the latest continuous period where all account-asset combinations have records.
        """
        valid_range_start, valid_range_end = cls.get_valid_range_dates(user)
        
        if valid_range_start is None:
            return cls.objects.none()
            
        return cls.objects.filter(
            account__user=user,
            date__gte=valid_range_start,
            date__lte=valid_range_end
        )

    @classmethod
    def get_latest_valid_balances(cls, user):
        """
        Returns the latest BalanceHistory records within the valid date range for a user.
        Returns only one record per account-asset combination, representing the most recent balance.
        """
        valid_range_start, valid_range_end = cls.get_valid_range_dates(user)
        
        if valid_range_start is None:
            return cls.objects.none()
        
        return cls.objects.filter(
            account__user=user,
            date=valid_range_end
        )


'''
<< GoCardless integration models >>
'''

class GoCardlessToken(models.Model):
    """
    Singleton model storing the app-level GoCardless API tokens.
    Only one row should exist; use GoCardlessToken.load() to access it.
    """
    access_token = models.TextField()
    access_expires_at = models.DateTimeField()
    refresh_token = models.TextField()
    refresh_expires_at = models.DateTimeField()

    class Meta:
        verbose_name = "GoCardless Token"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "access_token": "",
                "access_expires_at": "2000-01-01T00:00:00Z",
                "refresh_token": "",
                "refresh_expires_at": "2000-01-01T00:00:00Z",
            },
        )
        return obj


class Requisition(models.Model):
    """Tracks a GoCardless bank connection flow from creation through completion."""
    STATUS_CHOICES = [
        ("CR", "Created"),
        ("GC", "Giving Consent"),
        ("UA", "Undergoing Authentication"),
        ("RJ", "Rejected"),
        ("SA", "Selecting Accounts"),
        ("GA", "Granting Access"),
        ("LN", "Linked"),
        ("EX", "Expired"),
    ]

    id = models.UUIDField(primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    institution_id = models.CharField(max_length=64)
    institution_name = models.CharField(max_length=255)
    institution_country = models.CharField(max_length=255)
    status = models.CharField(max_length=2, choices=STATUS_CHOICES, default="CR")
    link = models.URLField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)
    agreement_id = models.UUIDField(blank=True, null=True)
    transaction_total_days = models.PositiveIntegerField()
    access_valid_for_days = models.PositiveIntegerField()