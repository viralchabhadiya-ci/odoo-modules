import logging
import time

import requests

from .quickbooks_error import QuickBooksAuthError, QuickBooksError

_logger = logging.getLogger(__name__)

AUTH_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"
SCOPE = "com.intuit.quickbooks.accounting"
API = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com/v3/company",
    "production": "https://quickbooks.api.intuit.com/v3/company",
}
MINOR_VERSION = "75"
PAGE = 1000
TIMEOUT = 60
RETRIES = 3


def quote(value):
    """A value inside a QuickBooks query string."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def oauth_post(client_id, client_secret, data):
    """Intuit's token endpoint (code exchange and refresh). Returns the token answer."""
    try:
        response = requests.post(
            TOKEN_URL,
            data=data,
            auth=(client_id, client_secret),
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise QuickBooksError(f"Intuit could not be reached: {exc}") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200:
        reason = body.get("error_description") or body.get("error") or f"HTTP {response.status_code}"
        if body.get("error") in ("invalid_grant", "invalid_client") or response.status_code in (400, 401):
            raise QuickBooksAuthError(f"Intuit refused the sign-in: {reason}")
        raise QuickBooksError(f"Intuit could not sign in: {reason}")
    return body


class QuickBooksClient:
    """QuickBooks Online accounting API for one company (realm). Raises QuickBooksError with Intuit's own message."""

    def __init__(self, environment, realm_id, token, refresh=None):
        self.base = f"{API[environment]}/{realm_id}"
        self.realm_id = realm_id
        self.token = token
        self.refresh = refresh  # callable returning a fresh access token, tried once after a 401

    def request(self, method, path, params=None, json=None):
        params = {"minorversion": MINOR_VERSION, **(params or {})}
        refreshed = False
        for attempt in range(RETRIES):
            try:
                response = requests.request(
                    method,
                    f"{self.base}/{path}",
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise QuickBooksError(f"QuickBooks could not be reached: {exc}") from exc
            if response.status_code == 401:
                if self.refresh and not refreshed:
                    refreshed = True
                    self.token = self.refresh()
                    continue
                raise QuickBooksAuthError("QuickBooks no longer accepts the connection. Connect again.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < RETRIES - 1:
                    time.sleep(min(int(response.headers.get("Retry-After") or 2 * (attempt + 1)), 30))
                    continue
            return self._answer(response)
        raise QuickBooksError("QuickBooks did not answer.")  # pragma: no cover

    @staticmethod
    def _answer(response):
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400 or body.get("Fault"):
            fault = body.get("Fault") or {}
            error = (fault.get("Error") or [{}])[0]
            message = error.get("Detail") or error.get("Message") or f"HTTP {response.status_code}"
            if error.get("Message") and error.get("Detail") and error["Message"] not in message:
                message = f"{error['Message']}: {message}"
            code = error.get("code")
            if fault.get("type") == "AUTHENTICATION":
                raise QuickBooksAuthError(message, code)
            details = [f"QuickBooks error {code}" if code else None, response.headers.get("intuit_tid")]
            suffix = ", ".join(d for d in details if d)
            raise QuickBooksError(f"{message} ({suffix})" if suffix else message, code)
        return body

    def query(self, sql):
        return self.request("GET", "query", {"query": sql}).get("QueryResponse", {})

    def query_all(self, entity, where=""):
        """Every record of an entity that matches the where clause, a page at a time."""
        rows, start = [], 1
        while True:
            sql = f"select * from {entity}{' where ' + where if where else ''} startposition {start} maxresults {PAGE}"
            page = self.query(sql).get(entity, [])
            rows += page
            if len(page) < PAGE:
                return rows
            start += PAGE

    def read(self, entity, remote_id):
        return self.request("GET", f"{entity.lower()}/{remote_id}").get(entity, {})

    def save(self, entity, payload, **params):
        return self.request("POST", entity.lower(), params, payload).get(entity, {})

    def delete(self, entity, payload):
        return self.request("POST", entity.lower(), {"operation": "delete"}, payload).get(entity, {})

    def void(self, entity, payload):
        return self.request("POST", entity.lower(), {"operation": "void"}, payload).get(entity, {})

    def company_info(self):
        return self.request("GET", f"companyinfo/{self.realm_id}").get("CompanyInfo", {})
