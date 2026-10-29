import logging
import time
from datetime import timedelta

import requests

from odoo import fields
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)

LWA_URL = "https://api.amazon.com/auth/o2/token"
HOSTS = {
    "na": "https://sellingpartnerapi-na.amazon.com",
    "eu": "https://sellingpartnerapi-eu.amazon.com",
    "fe": "https://sellingpartnerapi-fe.amazon.com",
}
USER_AGENT = "OdooChannelConnector/19.0 (Language=Python)"
TIMEOUT = 30
RETRIES = 2


def _message(response):
    """Amazon's own explanation of an error, when it sent one."""
    try:
        errors = response.json().get("errors") or []
    except ValueError:
        return (response.text or "")[:200]
    return "; ".join(filter(None, (e.get("message") for e in errors if isinstance(e, dict)))) or (
        response.text or ""
    )[:200]


class AmazonClient:
    """Selling Partner API client for one store: Login with Amazon token + JSON calls.

    No AWS IAM user or request signing is needed any more; the LWA access token is enough."""

    def __init__(self, store):
        self.store = store
        self.env = store.env
        secret = store.sudo()
        if not (secret.api_key and secret.api_secret and secret.amazon_refresh_token):
            raise ChannelError(
                self.env._("Enter the Client ID, Client secret and Refresh token of your Amazon app.")
            )
        self.host = HOSTS.get(store.amazon_region or "eu", HOSTS["eu"])

    # -- authentication -----------------------------------------------------------

    def _token(self, force=False):
        store = self.store.sudo()
        soon = fields.Datetime.now() + timedelta(minutes=5)
        if not force and store.access_token and store.token_expiry and store.token_expiry > soon:
            return store.access_token
        try:
            response = requests.post(
                LWA_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": store.amazon_refresh_token,
                    "client_id": store.api_key,
                    "client_secret": store.api_secret,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ChannelError(self.env._("Could not reach Amazon login: %s", exc)) from exc
        if response.status_code != 200:
            try:
                error = response.json().get("error")
            except ValueError:
                error = None
            if error == "invalid_client":
                text = self.env._(
                    "Amazon does not know this Client ID / Client secret. Copy them again from the app in "
                    "Seller Central > Apps and Services > Develop Apps."
                )
            else:
                text = self.env._(
                    "Amazon refused the Refresh token. Open your app in Seller Central > Apps and Services > "
                    "Develop Apps, choose Authorize app and paste the new token."
                )
            raise ChannelAuthError(text)
        payload = response.json()
        store.write(
            {
                "access_token": payload["access_token"],
                "token_expiry": fields.Datetime.now() + timedelta(seconds=int(payload.get("expires_in", 3600))),
            }
        )
        return payload["access_token"]

    # -- requests -----------------------------------------------------------------

    def request(self, method, path, params=None, json=None, patient=True):
        """Call the API and return the JSON body ({} for 204).

        `patient=False` skips the wait-and-retry on throttling, for calls Amazon allows only
        a few times an hour (orders): the next scheduled run simply continues."""
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
                        "x-amz-access-token": self._token(),
                        "user-agent": USER_AGENT,
                        "accept": "application/json",
                    },
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise ChannelError(self.env._("Could not reach Amazon: %s", exc)) from exc
            code = response.status_code
            if code in (401, 403) and not refreshed:
                # an expired access token looks like a refusal: get a fresh one once
                refreshed = True
                self._token(force=True)
                continue
            if code == 429 and patient and attempt < RETRIES:
                attempt += 1
                time.sleep(attempt * 2)
                continue
            if code >= 500 and attempt < RETRIES:
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
            raise ChannelError(self.env._("Amazon sent an answer that could not be read."))

    def _check(self, response):
        code = response.status_code
        if code < 400:
            return
        text = _message(response)
        if code == 401:
            raise ChannelAuthError(
                self.env._(
                    "Amazon did not accept the connection. Authorize the app again in Seller Central and paste the "
                    "new Refresh token."
                )
            )
        if code == 403:
            raise ChannelError(
                self.env._(
                    "Amazon denied this request (%s). The app is probably missing a role: tick Inventory and Order "
                    "Tracking, Product Listing and Direct-to-Consumer Shipping (Restricted) on the app, then "
                    "authorize it again.",
                    text,
                )
            )
        if code == 429:
            raise ChannelError(
                self.env._("Amazon is limiting how often this can be requested. Odoo will try again automatically.")
            )
        raise ChannelError(self.env._("Amazon answered HTTP %(code)s: %(message)s", code=code, message=text))

    # -- paging -------------------------------------------------------------------

    def paginate_orders(self, params):
        """Yield orders of /orders/2026-01-01/orders page by page."""
        params = dict(params)
        while True:
            body = self.request("GET", "/orders/2026-01-01/orders", params=params, patient=False)
            yield from body.get("orders") or []
            token = (body.get("pagination") or {}).get("nextToken")
            if not token:
                return
            params["paginationToken"] = token

    def paginate_listings(self, seller_id, params):
        """Yield listing items of the seller, 20 per page."""
        params = dict(params)
        while True:
            body = self.request("GET", f"/listings/2021-08-01/items/{seller_id}", params=params)
            yield from body.get("items") or []
            token = (body.get("pagination") or {}).get("nextToken")
            if not token:
                return
            params["pageToken"] = token
