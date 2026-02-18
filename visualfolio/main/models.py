import zoneinfo

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
    SYNC_FREQUENCY_CHOICES = [
        ("manual", "Manual only"),
        ("6h", "Every 6 hours"),
        ("12h", "Every 12 hours"),
        ("24h", "Every 24 hours"),
    ]
    
    THEME_CHOICES = [
        ("system", "System"),
        ("light", "Light"),
        ("dark", "Dark"),
    ]

    TIMEZONE_CHOICES = sorted(
        [(tz, tz) for tz in zoneinfo.available_timezones()],
        key=lambda x: x[0],
    )
    
    last_update = models.DateTimeField(null=True, blank=True)
    last_full_refresh = models.DateTimeField(null=True, blank=True)
    
    # Scheduled sync settings
    sync_frequency = models.CharField(
        max_length=10, 
        choices=SYNC_FREQUENCY_CHOICES, 
        default="24h"
    )
    sync_preferred_hour = models.PositiveSmallIntegerField(
        default=5,
        validators=[MinValueValidator(0)],
        help_text="Preferred hour of day (0-23) for automatic sync"
    )
    last_auto_sync = models.DateTimeField(null=True, blank=True)
    
    # Theme preference
    theme = models.CharField(
        max_length=10,
        choices=THEME_CHOICES,
        default="system"
    )

    # Timezone preference
    user_timezone = models.CharField(
        max_length=63,
        choices=TIMEZONE_CHOICES,
        default="UTC",
        help_text="IANA timezone for date/time display and input",
    )


class Account(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    country = models.CharField(max_length=255, choices=COUNTRY_CHOICES)
    requisition_id = models.UUIDField(blank=True, null=True, unique=True)
    color = models.CharField(max_length=7, default="#4b8dd3")

    # GoCardless requisition-level fields (populated at link time, null for manual accounts)
    gocardless_institution_id = models.CharField(max_length=64, blank=True, null=True)
    transaction_total_days = models.PositiveIntegerField(blank=True, null=True)
    access_expires_at = models.DateTimeField(blank=True, null=True)

    @property
    def is_synced(self):
        return self.requisition_id is not None

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
    internal identifier for each account-currency combination). The asset may
    be NULL initially if the currency cannot be determined at link time; it
    will be resolved during the first sync. Sync operational status and rate
    limits are tracked per AccountAsset since each corresponds to a distinct
    GoCardless account with its own API quotas.
    
    For manual accounts: exists only to track which assets have been established
    on the account (no gocardless_account_id).
    """

    SYNC_STATUS_CHOICES = [
        ("idle", "Idle"),
        ("syncing", "Syncing"),
    ]

    id = models.AutoField(primary_key=True)
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name='account_assets')
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, null=True, blank=True)
    gocardless_account_id = models.CharField(max_length=64, blank=True, null=True)

    # Sync operational status tracking (per GoCardless account)
    sync_status = models.CharField(max_length=10, choices=SYNC_STATUS_CHOICES, default="idle")
    last_successful_sync = models.DateTimeField(blank=True, null=True)
    last_sync_error = models.TextField(blank=True, null=True)

    # Per-account rate limits (GoCardless account-success limits for data fetching)
    account_success_limit = models.PositiveIntegerField(blank=True, null=True)
    account_success_remaining = models.PositiveIntegerField(blank=True, null=True)
    account_success_reset = models.DateTimeField(blank=True, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['account', 'asset'],
                name='unique_account_asset_pair',
            ),
            models.UniqueConstraint(
                fields=['gocardless_account_id'],
                condition=models.Q(gocardless_account_id__isnull=False),
                name='unique_gocardless_account_id',
            ),
        ]
    
    def __str__(self):
        asset_code = self.asset.code if self.asset else "pending"
        return f"{self.account.name} - {asset_code}"


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
            raise ValidationError({"entity": "From/to is required for transactions."})
        if not self.is_trade_side and self.asset and self.asset.asset_class_id != "Fiat Currencies":
            raise ValidationError({"asset": "Transactions are currently only supported for FIAT currency assets."})

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

    @staticmethod
    def to_values(instances, *fields):
        """
        Convert a list of model instances to a list of dicts, matching the format
        produced by QuerySet.values().

        Supports Django-style double-underscore FK traversal:
          - Plain field names (e.g. "balance", "date") return the field value.
          - FK field names without traversal (e.g. "account", "asset") return the FK id.
          - Traversed FK paths (e.g. "account__name", "asset__asset_class__color")
            follow the relation chain.
        """
        result = []
        for instance in instances:
            row = {}
            for field in fields:
                if "__" in field:
                    parts = field.split("__")
                    obj = instance
                    for part in parts:
                        obj = getattr(obj, part)
                    row[field] = obj
                else:
                    row[field] = getattr(instance, f"{field}_id", getattr(instance, field))
            result.append(row)
        return result

    @classmethod
    def get_valid_range_dates_with_forward_fill(cls, user):
        """
        Like get_valid_range_dates(), but extends to today if all account-asset combinations
        are merely timing-lagged (all have their last balance on the same date < today).

        This allows views to show data up to today even if syncs haven't run yet, without
        requiring physical DB writes that would break sync integrity.
        """
        valid_range_start, valid_range_end = cls.get_valid_range_dates(user)

        if valid_range_end is None:
            return None, None

        today = timezone.localdate()
        if valid_range_end >= today:
            return valid_range_start, valid_range_end

        # Check if all account-asset combinations have their latest balance on valid_range_end
        # (meaning they're all timing-lagged together, not just some missing data)
        n_all_combinations = cls.objects.filter(
            account__user=user
        ).values('account', 'asset').distinct().count()

        combinations_at_end = cls.objects.filter(
            account__user=user,
            date=valid_range_end
        ).values('account', 'asset').distinct().count()

        if combinations_at_end == n_all_combinations:
            return valid_range_start, today
        else:
            return valid_range_start, valid_range_end

    @classmethod
    def get_valid_range_records_with_forward_fill(cls, user):
        """
        Returns BalanceHistory instances within the valid date range, with virtual
        forward-fill to today if appropriate.

        Always returns list[BalanceHistory] with FKs preloaded. Virtual records are
        unsaved model instances (never persisted to DB).
        """
        import datetime as dt

        original_start, original_end = cls.get_valid_range_dates(user)
        extended_start, extended_end = cls.get_valid_range_dates_with_forward_fill(user)

        if extended_start is None:
            return []

        records = list(
            cls.objects.filter(
                account__user=user,
                date__gte=extended_start,
                date__lte=original_end,
            ).select_related("account", "asset__asset_class")
        )

        if extended_end <= original_end:
            return records

        last_balances = {
            (r.account_id, r.asset_id): r
            for r in records if r.date == original_end
        }

        d = original_end + dt.timedelta(days=1)
        while d <= extended_end:
            for template in last_balances.values():
                records.append(cls(
                    account=template.account,
                    asset=template.asset,
                    date=d,
                    balance=template.balance,
                ))
            d += dt.timedelta(days=1)

        return records

    @classmethod
    def get_latest_valid_balances_with_forward_fill(cls, user):
        """
        Returns the latest BalanceHistory instances with forward-fill to today.
        One record per account-asset combination at the extended end date.

        Always returns list[BalanceHistory] with FKs preloaded.
        """
        valid_range_start, valid_range_end = cls.get_valid_range_dates_with_forward_fill(user)

        if valid_range_start is None:
            return []

        physical_end = cls.get_valid_range_dates(user)[1]

        physical_latest = list(
            cls.objects.filter(
                account__user=user,
                date=physical_end,
            ).select_related("account", "asset__asset_class")
        )

        if valid_range_end == physical_end:
            return physical_latest

        return [
            cls(
                account=record.account,
                asset=record.asset,
                date=valid_range_end,
                balance=record.balance,
            )
            for record in physical_latest
        ]


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