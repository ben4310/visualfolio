from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from main.models import Account, AccountAsset, Asset, CustomUser


class AccountAssetRateLimitReadModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = CustomUser.objects.create_user(username="rateuser", password="pw")
        cls.asset = Asset.objects.get(code="EUR")
        cls.account = Account.objects.create(
            user=cls.user,
            name="Rate Test Account",
            country="IT",
            account_type="gocardless",
        )

    def test_effective_remaining_uses_limit_when_reset_is_in_past(self):
        aa = AccountAsset.objects.create(
            account=self.account,
            asset=self.asset,
            account_success_limit=100,
            account_success_remaining=0,
            account_success_reset=timezone.now() - timedelta(minutes=1),
        )

        self.assertEqual(aa.account_success_remaining_effective, 100)

    def test_effective_remaining_keeps_remaining_when_reset_is_in_future(self):
        aa = AccountAsset.objects.create(
            account=self.account,
            asset=self.asset,
            account_success_limit=100,
            account_success_remaining=7,
            account_success_reset=timezone.now() + timedelta(minutes=1),
        )

        self.assertEqual(aa.account_success_remaining_effective, 7)

    def test_effective_remaining_keeps_remaining_when_limit_is_null(self):
        aa = AccountAsset.objects.create(
            account=self.account,
            asset=self.asset,
            account_success_limit=None,
            account_success_remaining=3,
            account_success_reset=timezone.now() - timedelta(minutes=1),
        )

        self.assertEqual(aa.account_success_remaining_effective, 3)

    def test_reset_display_is_none_when_reset_is_in_past(self):
        aa = AccountAsset.objects.create(
            account=self.account,
            asset=self.asset,
            account_success_limit=100,
            account_success_remaining=0,
            account_success_reset=timezone.now() - timedelta(minutes=1),
        )

        self.assertIsNone(aa.account_success_reset_display)

    def test_reset_display_returns_reset_when_reset_is_in_future(self):
        reset_at = timezone.now() + timedelta(minutes=1)
        aa = AccountAsset.objects.create(
            account=self.account,
            asset=self.asset,
            account_success_limit=100,
            account_success_remaining=0,
            account_success_reset=reset_at,
        )

        self.assertEqual(aa.account_success_reset_display, reset_at)
