from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt as pyjwt
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 30


class EnableBankingAPIError(Exception):
    def __init__(self, status_code: int, message: str, detail: str = ""):
        self.status_code = status_code
        self.message = message
        self.detail = detail
        super().__init__(f"[{status_code}] {message}: {detail}")


class EnableBankingClient:
    def __init__(self):
        self.base_url = settings.ENABLE_BANKING_BASE_URL.rstrip("/")

    def _build_jwt(self) -> str:
        iat = int(datetime.now(timezone.utc).timestamp())
        payload = {
            "iss": "enablebanking.com",
            "aud": "api.enablebanking.com",
            "iat": iat,
            "exp": iat + 3600,
        }
        private_key = base64.b64decode(settings.ENABLE_BANKING_KEY_CONTENT)
        return pyjwt.encode(
            payload,
            private_key,
            algorithm="RS256",
            headers={"kid": settings.ENABLE_BANKING_APPLICATION_ID},
        )

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._build_jwt()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        resp = requests.request(
            method,
            url,
            headers=self._headers(),
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except Exception:
                data = {}
            raise EnableBankingAPIError(
                status_code=resp.status_code,
                message=data.get("code", f"HTTP {resp.status_code}"),
                detail=data.get("message", resp.text),
            )
        return resp.json()

    def get_aspsps(self, country: str) -> list[dict]:
        query = urlencode({"country": country})
        data = self._request("GET", f"/aspsps?{query}")
        return data.get("aspsps", [])

    def start_auth(self, payload: dict) -> dict:
        return self._request("POST", "/auth", json=payload)

    def create_session(self, code: str) -> dict:
        return self._request("POST", "/sessions", json={"code": code})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/sessions/{session_id}")

    def get_balances(self, account_uid: str) -> dict:
        return self._request("GET", f"/accounts/{account_uid}/balances")

    def get_transactions(self, account_uid: str, date_from: str, date_to: str) -> dict:
        params = {"date_from": date_from, "date_to": date_to}
        all_transactions: list[dict] = []
        continuation_key: str | None = None
        while True:
            if continuation_key:
                params["continuation_key"] = continuation_key
            query = urlencode(params)
            data = self._request("GET", f"/accounts/{account_uid}/transactions?{query}")
            all_transactions.extend(data.get("transactions", []))
            continuation_key = data.get("continuation_key")
            if not continuation_key:
                break
        return {"transactions": all_transactions}
