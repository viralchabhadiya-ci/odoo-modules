import hashlib
import logging
import time

import requests

from odoo.addons.marketing_connector_base.models.marketing_error import MarketingAuthError, MarketingError

_logger = logging.getLogger(__name__)

TIMEOUT = 60
RETRIES = 2
PAGE = 500


def subscriber_hash(email):
    """Mailchimp addresses a member by the MD5 of the lower-case address."""
    return hashlib.md5(email.strip().lower().encode()).hexdigest()  # noqa: S324 - an identifier, not security


class MailchimpNotFound(MarketingError):
    """The list or member does not exist (any more)."""


class MailchimpClient:
    """Mailchimp Marketing API v3: basic auth with any user name and the API key as password."""

    def __init__(self, account):
        self.env = account.env
        key = (account.sudo().api_key or "").strip()
        if not key:
            raise MarketingError(self.env._("Paste the Mailchimp API key first."))
        if "-" not in key:
            raise MarketingError(
                self.env._("This does not look like a Mailchimp API key. It ends with a dash and a code such as -us21.")
            )
        self.base = f"https://{key.rsplit('-', 1)[1]}.api.mailchimp.com/3.0"
        self.auth = ("odoo", key)

    def request(self, method, path, params=None, json=None):
        for attempt in range(RETRIES + 1):
            try:
                response = requests.request(
                    method, f"{self.base}/{path.lstrip('/')}", params=params, json=json, auth=self.auth, timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                raise MarketingError(self.env._("Could not reach Mailchimp: %s", exc)) from exc
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
            body = response.json()
        except ValueError:
            body = {}
        detail = body.get("detail") or response.text[:200]
        if code == 401:
            raise MarketingAuthError(
                self.env._(
                    "Mailchimp rejected the API key. Create a new one under Profile > Extras > API keys "
                    "(it must belong to a user who can manage audiences)."
                )
            )
        if code == 403:
            raise MarketingError(self.env._("Mailchimp refused this: %s", detail))
        if code == 404:
            raise MailchimpNotFound(self.env._("Mailchimp could not find it: %s", detail))
        if code == 429:
            raise MarketingError(
                self.env._("Mailchimp says there are too many requests. Odoo tries again in 15 minutes.")
            )
        raise MarketingError(self.env._("Mailchimp answered %(code)s: %(detail)s", code=code, detail=detail))

    def paginate(self, path, key, params=None):
        offset = 0
        while True:
            rows = (self.request("GET", path, {**(params or {}), "count": PAGE, "offset": offset}) or {}).get(key) or []
            yield from rows
            if len(rows) < PAGE:
                return
            offset += PAGE
