"""
Small, safe wrapper around the Xero Accounting API used by the Allocation Removal feature.

Only `requests` (already a dependency) is used. The existing fetch code in app.py is intentionally left untouched.

* 401  -> refresh the token once (through the app's own, locked refresh) and retry once
* 429  -> wait for Retry-After and retry (Xero did NOT process a rate-limited call, so this is safe for DELETE too);
          a daily limit is not retried
* timeouts / connection errors / 5xx -> raised with ambiguous=True: the caller must re-check the real state
  before any retry of a write
* every other 4xx -> a readable message taken from Xero's own validation text
Error messages never contain tokens, headers or request bodies.
"""
import time

import requests

import token_store

BASE_URL = "https://api.xero.com/api.xro/2.0"
_sleep = time.sleep  # patched in tests


class XeroApiError(Exception):
    def __init__(self, status, code, message, ambiguous=False, transient=False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.ambiguous = ambiguous
        self.transient = transient


def _extract_message(resp):
    try:
        data = resp.json()
    except ValueError:
        data = None
    msg = None
    if isinstance(data, dict):
        errors = (data.get("Elements") or [{}])[0].get("ValidationErrors") or []
        msg = "; ".join(e.get("Message", "") for e in errors if e.get("Message")) or None
        msg = msg or data.get("Message") or data.get("Detail") or data.get("Title")
    return str(msg or "Xero returned an error.")[:300]


def map_http_error(resp):
    s = resp.status_code
    if s == 401:
        return XeroApiError(401, "XERO_UNAUTHORIZED", "Xero rejected the access token. Please reconnect.")
    if s == 403:
        return XeroApiError(403, "INSUFFICIENT_SCOPE",
                            "Xero denied access (403). Check the app scopes (accounting.transactions) and the "
                            "user's permissions in this organisation.")
    if s == 404:
        return XeroApiError(404, "XERO_NOT_FOUND", _extract_message(resp))
    if s == 429:
        return XeroApiError(429, "RATE_LIMITED", "Xero rate limit reached. Wait a minute and try again.")
    if s >= 500:
        return XeroApiError(502, "XERO_SERVER_ERROR", "Xero returned a server error.", ambiguous=True, transient=True)
    return XeroApiError(400, "XERO_VALIDATION", _extract_message(resp))


class XeroClient:
    def __init__(self, get_valid_token, force_refresh, timeout=20, max_rate_retries=3, max_retry_wait=65):
        self._get_valid_token = get_valid_token
        self._force_refresh = force_refresh
        self.timeout = timeout
        self.max_rate_retries = max_rate_retries
        self.max_retry_wait = max_retry_wait

    def _access_token(self, rejected=None):
        try:
            token = self._force_refresh(rejected) if rejected else self._get_valid_token()
            return token["access_token"]
        except token_store.TokenStoreError as exc:
            raise XeroApiError(500, "TOKEN_STORE_ERROR", str(exc)) from None
        except Exception as exc:  # no token / refresh failed. Do not echo upstream text.
            code = "NOT_CONNECTED" if "No Xero token" in str(exc) else "TOKEN_REFRESH_FAILED"
            raise XeroApiError(401, code, "The Xero connection is not valid. Use Reset Connection, then Connect to Xero again.") from None

    def request(self, method, path, tenant_id, params=None):
        rejected = None
        rate_retries = 0
        while True:
            access_token = self._access_token(rejected)
            try:
                resp = requests.request(
                    method, BASE_URL + path, params=params, timeout=self.timeout,
                    headers={"Authorization": f"Bearer {access_token}", "xero-tenant-id": tenant_id,
                             "Accept": "application/json"},
                )
            except requests.exceptions.Timeout:
                raise XeroApiError(504, "NETWORK_TIMEOUT", "The request to Xero timed out.", ambiguous=True, transient=True) from None
            except requests.exceptions.RequestException:
                raise XeroApiError(502, "NETWORK_ERROR", "Could not reach Xero.", ambiguous=True, transient=True) from None

            status = resp.status_code
            if 200 <= status < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            if status == 401 and rejected is None:
                rejected = access_token
                continue
            if status == 429:
                daily = str(resp.headers.get("X-Rate-Limit-Problem", "")).lower() == "day"
                if not daily and rate_retries < self.max_rate_retries:
                    rate_retries += 1
                    try:
                        wait = float(resp.headers.get("Retry-After", 5))
                    except (TypeError, ValueError):
                        wait = 5
                    _sleep(min(wait, self.max_retry_wait))
                    continue
                raise XeroApiError(
                    429, "RATE_LIMIT_DAILY" if daily else "RATE_LIMITED",
                    "Xero daily API limit reached for this organisation." if daily
                    else "Xero rate limit reached. Wait a minute and try again.")
            raise map_http_error(resp)
