"""
Management command to check and sync user accounts based on their settings.

This command is run hourly by APScheduler. It checks each user's sync settings
and triggers syncs for users whose sync schedule has been reached.
"""
import logging
import zoneinfo
from datetime import datetime, timedelta, timezone
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from main.models import Account
from main.services.stateful.gocardless.sync import (
    TransactionSyncService,
    SyncAlreadyInProgressError,
)

logger = logging.getLogger(__name__)
User = get_user_model()


def _user_local_hour(user, utc_now: datetime) -> int:
    """Return the current hour in the user's configured timezone."""
    try:
        user_tz = zoneinfo.ZoneInfo(user.user_timezone)
    except (KeyError, zoneinfo.ZoneInfoNotFoundError):
        user_tz = zoneinfo.ZoneInfo("UTC")
    return utc_now.astimezone(user_tz).hour


class Command(BaseCommand):
    help = 'Check and sync accounts for users based on their sync settings'

    def handle(self, *args, **options):
        start_time = datetime.now(timezone.utc)
        
        logger.info("Running scheduled sync check at %s (UTC)", start_time)
        
        users_to_sync = self.get_users_to_sync(start_time)
        
        if not users_to_sync:
            logger.info("No users need syncing at this time")
            return
        
        logger.info("Found %d user(s) to sync", len(users_to_sync))
        
        for user in users_to_sync:
            try:
                self.sync_user_accounts(user, start_time)
            except Exception as exc:
                logger.exception(
                    "Unexpected error during scheduled sync for user %s (tolerated, will retry next cycle): %s",
                    user.username, exc
                )
    
    def get_users_to_sync(self, now):
        """
        Get users who need syncing based on their settings.
        
        Returns users where:
        - sync_frequency is not 'manual'
        - If never synced or last sync was yesterday/earlier: only sync at preferred hour (first sync of day)
        - If last sync was today: sync if enough time has passed (subsequent syncs)
        
        The preferred hour is compared against the user's local timezone.
        """
        users_to_sync = []
        
        for user in User.objects.exclude(sync_frequency='manual'):
            current_user_hour = _user_local_hour(user, now)

            is_first_sync_of_day = False
            if user.last_auto_sync is None:
                is_first_sync_of_day = True
            else:
                last_sync_date = user.last_auto_sync.date()
                today = now.date()
                if last_sync_date < today:
                    is_first_sync_of_day = True
            
            if is_first_sync_of_day:
                if user.sync_preferred_hour != current_user_hour:
                    continue
            
            if not self.should_sync_now(user, now):
                continue
            
            users_to_sync.append(user)
        
        return users_to_sync
    
    def should_sync_now(self, user, now):
        """
        Determine if enough time has passed since last auto sync.
        """
        if user.last_auto_sync is None:
            return True
        
        # Calculate time since last sync
        time_since_last = (now - user.last_auto_sync).total_seconds()
        
        # Get required interval based on frequency setting
        frequency_map = {
            '6h': 6 * 3600,
            '12h': 12 * 3600,
            '24h': 24 * 3600,
        }
        
        required_seconds = frequency_map.get(user.sync_frequency, float('inf'))
        
        return time_since_last >= required_seconds
    
    def sync_user_accounts(self, user, now):
        """
        Sync all synced accounts for a user iteratively.
        """
        start = datetime.now(timezone.utc)
        logger.info("Starting sync for user %s (%s)", user.username, user.email)
        
        synced_accounts = Account.objects.filter(
            user=user,
            requisition_id__isnull=False,
        ).select_related()
        
        if not synced_accounts.exists():
            logger.info("User %s has no synced accounts", user.username)
            user.last_auto_sync = now
            user.save(update_fields=['last_auto_sync'])
            return
        
        syncer = TransactionSyncService()
        success_count = 0
        skip_count = 0
        error_count = 0
        
        for account in synced_accounts:
            try:
                logger.info("Syncing account %s for user %s", account.name, user.username)
                syncer.sync_account(account)
                success_count += 1
            except SyncAlreadyInProgressError:
                logger.info("Sync already in progress for account %s", account.name)
                skip_count += 1
            except Exception as exc:
                logger.warning(
                    "Scheduled sync encountered error for account %s (tolerated, will retry next cycle): %s",
                    account.name, exc
                )
                error_count += 1
        
        user.last_auto_sync = now
        user.save(update_fields=['last_auto_sync'])
        
        duration = (datetime.now(timezone.utc) - start).total_seconds()
        logger.info(
            "Completed sync for user %s: %d successful, %d skipped, %d errors (duration: %.2fs)",
            user.username, success_count, skip_count, error_count, duration
        )
