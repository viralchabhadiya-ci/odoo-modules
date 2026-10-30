import logging
import time
from urllib.parse import quote

import requests

from odoo.addons.marketing_connector_base.models.marketing_error import MarketingAuthError, MarketingError

_logger = logging.getLogger(__name__)

API = "https://api.brevo.com/v3"
TIMEOUT = 60
RETRIES = 2


class BrevoNotFound(MarketingError):
    """The contact or list does not exist (any more)."""


def contact_path(email):
    return f"contacts/{quote(email.strip().lower(), safe='')}"


class BrevoClient:
    """Brevo API v3: the key travels in the `api-key` header."""

    def __init__(self, account):
        self.env = account.env
        key = (account.sudo().api_key or "").strip()
        if not key:
            raise MarketingError(self.env._("Paste the Brevo API key first."))
        self.headers = {"api-key": key, "accept": "application/json"}

    def request(self, method, path, params=None, json=None):
        for attempt in range(RETRIES + 1):
            try:
                response = requests.request(
                    method, f"{API}/{path.lstrip('/')}", params=params, json=json, headers=self.headers, timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                raise MarketingError(self.env._("Could not reach Brevo: %s", exc)) from exc
            if response.status_code in (429, 502, 503, 504) and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            break
        self._check(response)
        return response.json() if response.content else {}

    def _check(self, response):
        code = response.status_code
        if code < 400:
            return
        try:
            message = response.json().get("message") or response.text[:200]
        except (ValueError, AttributeError):
            message = response.text[:200]
        if code == 401:
            raise MarketingAuthError(
                self.env._(
                    "Brevo rejected the API key (%s). Create a new one under SMTP & API > API keys. If you limit access by "
                    "IP address under Security > Authorised IPs, add the address of this Odoo server.",
                    message,
                )
            )
        if code == 403:
            raise MarketingError(self.env._("Brevo refused this: %s", message))
        if code == 404:
            raise BrevoNotFound(self.env._("Brevo could not find it: %s", message))
        if code == 429:
            raise MarketingError(self.env._("Brevo says there are too many requests. Odoo tries again in 15 minutes."))
        raise MarketingError(self.env._("Brevo answered %(code)s: %(message)s", code=code, message=message))

    def paginate(self, path, key, params=None, page=50):
        """Brevo caps the page size per endpoint (50 lists, 100 campaigns, 500 contacts)."""
        offset = 0
        while True:
            rows = (self.request("GET", path, {**(params or {}), "limit": page, "offset": offset}) or {}).get(key) or []
            yield from rows
            if len(rows) < page:
                return
            offset += page
