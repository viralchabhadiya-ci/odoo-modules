import base64
import logging
import time
from datetime import timedelta

import requests

from odoo import fields
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)

HOSTS = {False: "https://api.ebay.com", True: "https://api.sandbox.ebay.com"}
TIMEOUT = 30
RETRIES = 2
SCOPES = "sell.fulfillment and sell.inventory"


def _message(response):
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        return (response.text or "")[:200]
    return "; ".join(filter(None, (e.get("longMessage") or e.get("message") for e in errors if isinstance(e, dict)))) or (
        response.text or ""
    )[:200]


class EbayClient:
    """eBay REST client for one store: OAuth refresh token -> 2 hour access token -> JSON calls."""

    def __init__(self, store):
        self.store = store
        self.env = store.env
        secret = store.sudo()
        if not (secret.api_key and secret.api_secret and secret.ebay_refresh_token):
            raise ChannelError(self.env._("Enter the App ID, Cert ID and Refresh token of your eBay application."))
        self.host = HOSTS[bool(store.ebay_sandbox)]

    def _token(self, force=False):
        store = self.store.sudo()
        soon = fields.Datetime.now() + timedelta(minutes=5)
        if not force and store.access_token and store.token_expiry and store.token_expiry > soon:
            return store.access_token
        basic = base64.b64encode(f"{store.api_key}:{store.api_secret}".encode()).decode()
        try:
            response = requests.post(
                f"{self.host}/identity/v1/oauth2/token",
                data={"grant_type": "refresh_token", "refresh_token": store.ebay_refresh_token},
                headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ChannelError(self.env._("Could not reach eBay: %s", exc)) from exc
        if response.status_code != 200:
            try:
                error = response.json().get("error")
            except ValueError:
                error = None
            if error == "invalid_client":
                text = self.env._(
                    "eBay does not accept this App ID / Cert ID. Copy the production keys (or switch on Sandbox if "
                    "they are sandbox keys) from developer.ebay.com > Application Keys."
                )
            else:
                text = self.env._(
                    "eBay refused the Refresh token. It stops working after 18 months or when the seller changes "
                    "the password. Sign in the seller again to get a new one with the scopes %s.",
                    SCOPES,
                )
            raise ChannelAuthError(text)
        payload = response.json()
        store.write(
            {
                "access_token": payload["access_token"],
                "token_expiry": fields.Datetime.now() + timedelta(seconds=int(payload.get("expires_in", 7200))),
            }
        )
        return payload["access_token"]

    def request(self, method, path, params=None, json=None):
        """Call the API and return the JSON body ({} when there is none)."""
        refreshed = False
        attempt = 0
        while True:
            try:
                response = requests.request(
                    method,
                    f"{self.host}{path}",
                    params=params,
                    json=json,
                    headers={
                        "Authorization": f"Bearer {self._token()}",
                        "Accept": "application/json",
                        "Content-Language": "en-US",
                    },
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise ChannelError(self.env._("Could not reach eBay: %s", exc)) from exc
            code = response.status_code
            if code == 401 and not refreshed:
                refreshed = True
                self._token(force=True)
                continue
            if code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                attempt += 1
                time.sleep(attempt * 2)
                continue
            break
        self._check(response)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            raise ChannelError(self.env._("eBay sent an answer that could not be read."))

    def _check(self, response):
        code = response.status_code
        if code < 400:
            return
        text = _message(response)
        if code == 401:
            raise ChannelAuthError(
                self.env._("eBay did not accept the connection. Create a new Refresh token and paste it in the store.")
            )
        if code == 403:
            raise ChannelError(
                self.env._(
                    "eBay denied this request (%(text)s). The Refresh token needs the scopes %(scopes)s: sign the "
                    "seller in again with them.",
                    text=text,
                    scopes=SCOPES,
                )
            )
        if code == 429:
            raise ChannelError(self.env._("eBay is limiting how often this can be requested. Odoo will try again."))
        raise ChannelError(self.env._("eBay answered HTTP %(code)s: %(message)s", code=code, message=text))

    def paginate(self, path, key, params=None, limit=100):
        """Yield `key` rows page by page using limit/offset."""
        offset = 0
        while True:
            body = self.request("GET", path, {**(params or {}), "limit": limit, "offset": offset})
            rows = body.get(key) or []
            yield from rows
            offset += limit
            if not rows or offset >= int(body.get("total") or 0):
                return
