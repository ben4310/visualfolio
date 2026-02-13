import logging
import time
from datetime import datetime, timedelta, timezone

import requests
from django.conf import settings

from main.models import GoCardlessToken

logger = logging.getLogger(__name__)

BASE_URL = settings.GOCARDLESS_BASE_URL
REQUEST_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
BACKOFF_FACTOR = 2  # seconds: 2, 4, 8
RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


class GoCardlessAPIError(Exception):
    """Raised when the GoCardless API returns a non-retryable error."""

    def __init__(self, status_code: int, summary: str, detail: str = ""):
        self.status_code = status_code
        self.summary = summary
        self.detail = detail
        super().__init__(f"[{status_code}] {summary}: {detail}")


class GoCardlessRateLimitError(GoCardlessAPIError):
    """Raised when a rate limit (429) is hit."""

    def __init__(self, retry_after: int, summary: str, detail: str):
        self.retry_after = retry_after
        super().__init__(429, summary, detail)


class GoCardlessClient:
    """
    HTTP client for the GoCardless Bank Account Data API.
    Handles token lifecycle, retry with backoff, and rate-limit awareness.
    """

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    # ── Token management ────────────────────────────────────────────

    def _ensure_access_token(self) -> str:
        """Return a valid access token, refreshing or re-creating as needed."""
        token = GoCardlessToken.load()
        now = self._now()

        # Access token still valid
        if token.access_token and token.access_expires_at > now + TOKEN_REFRESH_MARGIN:
            return token.access_token

        # Try refresh if refresh token is still valid
        if token.refresh_token and token.refresh_expires_at > now + TOKEN_REFRESH_MARGIN:
            return self._refresh_access_token(token)

        # Both expired -- create new token pair from secrets
        return self._create_token_pair(token)

    def _create_token_pair(self, token: GoCardlessToken) -> str:
        """POST /api/v2/token/new/ with secret_id and secret_key."""
        resp = requests.post(
            f"{BASE_URL}/api/v2/token/new/",
            json={
                "secret_id": settings.GOCARDLESS_SECRET_ID,
                "secret_key": settings.GOCARDLESS_SECRET_KEY,
            },
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            raise GoCardlessAPIError(
                resp.status_code,
                body.get("summary", "Token creation failed"),
                body.get("detail", resp.text),
            )

        data = resp.json()
        now = self._now()
        token.access_token = data["access"]
        token.access_expires_at = now + timedelta(seconds=data["access_expires"])
        token.refresh_token = data["refresh"]
        token.refresh_expires_at = now + timedelta(seconds=data["refresh_expires"])
        token.save()
        logger.info("GoCardless token pair created (access expires in %ss)", data["access_expires"])
        return token.access_token

    def _refresh_access_token(self, token: GoCardlessToken) -> str:
        """POST /api/v2/token/refresh/ with the stored refresh token."""
        resp = requests.post(
            f"{BASE_URL}/api/v2/token/refresh/",
            json={"refresh": token.refresh_token},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            logger.warning("Refresh failed (status %s), falling back to full token creation", resp.status_code)
            return self._create_token_pair(token)

        data = resp.json()
        now = self._now()
        token.access_token = data["access"]
        token.access_expires_at = now + timedelta(seconds=data["access_expires"])
        token.save()
        logger.info("GoCardless access token refreshed (expires in %ss)", data["access_expires"])
        return token.access_token

    # ── HTTP helpers ────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> tuple[dict, dict]:
        """
        Perform an authenticated API request with retry and rate-limit handling.
        Returns tuple of (parsed JSON response body, response headers dict).
        """
        access_token = self._ensure_access_token()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        url = f"{BASE_URL}{path}"
        kwargs.setdefault("timeout", REQUEST_TIMEOUT)

        last_exception = None
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.request(method, url, headers=headers, **kwargs)
            except requests.exceptions.Timeout:
                last_exception = GoCardlessAPIError(0, "Request timed out", url)
                logger.warning("Request timed out (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, url)
                time.sleep(BACKOFF_FACTOR ** attempt)
                continue
            except requests.exceptions.ConnectionError as exc:
                last_exception = GoCardlessAPIError(0, "Connection error", str(exc))
                logger.warning("Connection error (attempt %d/%d): %s", attempt + 1, MAX_RETRIES, exc)
                time.sleep(BACKOFF_FACTOR ** attempt)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("HTTP_X_RATELIMIT_ACCOUNT_SUCCESS_RESET", 60))
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                raise GoCardlessRateLimitError(
                    retry_after,
                    body.get("summary", "Rate limit exceeded"),
                    body.get("detail", ""),
                )

            if resp.status_code in RETRYABLE_STATUS_CODES:
                last_exception = GoCardlessAPIError(resp.status_code, "Server error", resp.text)
                logger.warning(
                    "Retryable error %d (attempt %d/%d): %s",
                    resp.status_code, attempt + 1, MAX_RETRIES, url,
                )
                time.sleep(BACKOFF_FACTOR ** attempt)
                continue

            if resp.status_code >= 400:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                raise GoCardlessAPIError(
                    resp.status_code,
                    body.get("summary", f"HTTP {resp.status_code}"),
                    body.get("detail", resp.text),
                )

            return resp.json(), dict(resp.headers)

        raise last_exception  # type: ignore[misc]

    def _parse_account_rate_limit_headers(self, headers: dict) -> dict:
        """
        Extract per-account rate limit info from response headers.
        These headers only appear for successful account data requests (details/transactions/balances).
        General rate limits are app-wide and not tracked.
        
        Note: requests library uses standard HTTP header format (case-insensitive).
        GoCardless sends: X-RateLimit-Account-Success-Limit etc.
        """
        # Try multiple possible header name formats (requests CaseInsensitiveDict handles most)
        def get_header(name_variants):
            for name in name_variants:
                val = headers.get(name)
                if val is not None:
                    return val
            return None
        
        return {
            'account_success_limit': get_header([
                'X-RateLimit-Account-Success-Limit',
                'X-Ratelimit-Account-Success-Limit',
                'x-ratelimit-account-success-limit',
            ]),
            'account_success_remaining': get_header([
                'X-RateLimit-Account-Success-Remaining',
                'X-Ratelimit-Account-Success-Remaining',
                'x-ratelimit-account-success-remaining',
            ]),
            'account_success_reset': get_header([
                'X-RateLimit-Account-Success-Reset',
                'X-Ratelimit-Account-Success-Reset',
                'x-ratelimit-account-success-reset',
            ]),
        }

    def _get(self, path: str, **kwargs) -> tuple[dict, dict]:
        return self._request("GET", path, **kwargs)

    def _post(self, path: str, **kwargs) -> tuple[dict, dict]:
        return self._request("POST", path, **kwargs)

    # ── Public API methods ──────────────────────────────────────────

    def get_institutions(self, country_code: str) -> list[dict]:
        """
        GET /api/v2/institutions/?country={code}
        Returns list of institution dicts with keys: id, name, bic,
        transaction_total_days, countries, logo, max_access_valid_for_days, etc.
        """
        body, _ = self._get(f"/api/v2/institutions/?country={country_code}")
        return body

    def create_agreement(
        self, institution_id: str, max_historical_days: int, access_valid_for_days: int
    ) -> dict:
        """POST /api/v2/agreements/enduser/"""
        body, _ = self._post(
            "/api/v2/agreements/enduser/",
            json={
                "institution_id": institution_id,
                "max_historical_days": max_historical_days,
                "access_valid_for_days": access_valid_for_days,
                "access_scope": ["balances", "details", "transactions"],
            },
        )
        return body

    def create_requisition(
        self,
        institution_id: str,
        redirect_url: str,
        reference: str,
        agreement_id: str | None = None,
        user_language: str = "EN",
    ) -> dict:
        """POST /api/v2/requisitions/"""
        payload: dict = {
            "institution_id": institution_id,
            "redirect": redirect_url,
            "reference": reference,
            "user_language": user_language,
        }
        if agreement_id:
            payload["agreement"] = agreement_id
        body, _ = self._post("/api/v2/requisitions/", json=payload)
        return body

    def get_requisition(self, requisition_id: str) -> dict:
        """GET /api/v2/requisitions/{id}/"""
        body, _ = self._get(f"/api/v2/requisitions/{requisition_id}/")
        return body

    def get_account_balances(self, account_id: str) -> tuple[dict, dict]:
        """
        GET /api/v2/accounts/{id}/balances/
        Returns (balance data, response headers with rate limits)
        """
        return self._get(f"/api/v2/accounts/{account_id}/balances/")

    def get_account_transactions(
        self, account_id: str, date_from: str, date_to: str
    ) -> tuple[dict, dict]:
        """
        GET /api/v2/accounts/{id}/transactions/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
        Returns ({"transactions": {"booked": [...], "pending": [...]}}, response headers with rate limits)
        """
        return self._get(
            f"/api/v2/accounts/{account_id}/transactions/"
            f"?date_from={date_from}&date_to={date_to}"
        )

    def get_account_details(self, account_id: str) -> tuple[dict, dict]:
        """
        GET /api/v2/accounts/{id}/details/
        Returns (account details, response headers with rate limits)
        """
        return self._get(f"/api/v2/accounts/{account_id}/details/")

    def get_account_metadata(self, account_id: str) -> dict:
        """GET /api/v2/accounts/{id}/ (account status, institution_id, etc.)"""
        body, _ = self._get(f"/api/v2/accounts/{account_id}/")
        return body
