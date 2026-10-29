import logging
import re
import time
import uuid
from datetime import timedelta

import requests

from odoo import fields
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)

TIMEOUT = 30
MAX_THROTTLE_RETRIES = 3


def normalize_shop(url):
    """'https://My-Shop.myshopify.com/admin' -> 'my-shop.myshopify.com'."""
    shop = re.sub(r"^https?://", "", (url or "").strip().lower()).split("/")[0]
    if shop and "." not in shop:
        shop += ".myshopify.com"
    return shop


class ShopifyClient:
    """Thin Admin GraphQL client. One instance per store and request burst."""

    def __init__(self, store):
        self.store = store
        self.env = store.env
        self.shop = normalize_shop(store.shop_url)
        if not self.shop:
            raise ChannelError(self.env._("Enter the shop address, e.g. my-shop.myshopify.com."))
        version = (
            store.env["ir.config_parameter"].sudo().get_param("channel_shopify.api_version")
            or "2026-07"
        )
        self.base = f"https://{self.shop}/admin"
        self.url = f"{self.base}/api/{version}/graphql.json"

    # -- authentication -----------------------------------------------------------

    def _token(self):
        store = self.store.sudo()
        if store.access_token and not store.token_expiry:
            return store.access_token  # static admin API token of a custom app
        soon = fields.Datetime.now() + timedelta(minutes=5)
        if store.access_token and store.token_expiry and store.token_expiry > soon:
            return store.access_token
        if not (store.api_key and store.api_secret):
            raise ChannelError(
                self.env._("Enter the Client ID and Client secret of your Shopify app (or an Admin API access token).")
            )
        try:
            response = requests.post(
                f"{self.base}/oauth/access_token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": store.api_key,
                    "client_secret": store.api_secret,
                },
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ChannelError(self.env._("Could not reach %(shop)s: %(error)s", shop=self.shop, error=exc)) from exc
        if response.status_code != 200:
            raise ChannelAuthError(
                self.env._(
                    "Shopify refused the Client ID / Client secret (%(code)s). Check them in the Dev Dashboard "
                    "and make sure the app is installed on this shop and belongs to the same organization.",
                    code=response.status_code,
                )
            )
        payload = response.json()
        store.write(
            {
                "access_token": payload["access_token"],
                "token_expiry": fields.Datetime.now() + timedelta(seconds=int(payload.get("expires_in", 86399))),
            }
        )
        return payload["access_token"]

    # -- requests -----------------------------------------------------------------

    def graphql(self, query, variables=None):
        """Run a query and return `data`. Raises ChannelError with a fixable message."""
        for attempt in range(MAX_THROTTLE_RETRIES + 1):
            try:
                response = requests.post(
                    self.url,
                    json={"query": query, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": self._token()},
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise ChannelError(self.env._("Could not reach %(shop)s: %(error)s", shop=self.shop, error=exc)) from exc
            if response.status_code == 401:
                if self.store.sudo().token_expiry:  # fetched token: force a refresh next time
                    self.store.sudo().token_expiry = fields.Datetime.now()
                raise ChannelAuthError(self.env._("Shopify did not accept the credentials. Test the connection again."))
            if response.status_code == 429 and attempt < MAX_THROTTLE_RETRIES:
                time.sleep(float(response.headers.get("Retry-After", 2)))
                continue
            if response.status_code >= 400:
                raise ChannelError(
                    self.env._("Shopify answered HTTP %(code)s: %(body)s", code=response.status_code, body=response.text[:300])
                )
            body = response.json()
            errors = body.get("errors")
            if errors:
                codes = {(e.get("extensions") or {}).get("code") for e in errors if isinstance(e, dict)}
                if "THROTTLED" in codes and attempt < MAX_THROTTLE_RETRIES:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ShopifyGraphQLError(errors, self.env)
            return body.get("data") or {}
        raise ChannelError(self.env._("Shopify is busy (rate limit). The job will be retried automatically."))

    def paginate(self, query, connection, variables=None, first=25):
        """Yield every node of `data[connection]`, following cursors.

        Shrinks the page size when Shopify refuses the query as too expensive."""
        variables = dict(variables or {})
        after = None
        while True:
            try:
                data = self.graphql(query, {**variables, "first": first, "after": after})
            except ShopifyGraphQLError as exc:
                if exc.code == "MAX_COST_EXCEEDED" and first > 1:
                    first = max(1, first // 2)
                    continue
                raise
            page = data[connection]
            yield from page["nodes"]
            if not page["pageInfo"]["hasNextPage"]:
                return
            after = page["pageInfo"]["endCursor"]

    def mutate(self, query, variables, result_key):
        """Run a mutation and raise on userErrors."""
        data = self.graphql(query, {**variables, "key": str(uuid.uuid4())})
        errors = (data.get(result_key) or {}).get("userErrors") or []
        if errors:
            raise ChannelError(
                self.env._("Shopify refused the change: %s", "; ".join(e.get("message", "") for e in errors))
            )
        return data[result_key]

    def mutate_webhook(self, topic, url):
        query = """
        mutation($topic: WebhookSubscriptionTopic!, $url: URL!) {
          webhookSubscriptionCreate(topic: $topic, webhookSubscription: { callbackUrl: $url, format: JSON }) {
            userErrors { field message }
          }
        }"""
        data = self.graphql(query, {"topic": topic, "url": url})
        errors = data["webhookSubscriptionCreate"]["userErrors"]
        if errors:
            raise ChannelError(self.env._("Shopify refused the webhook: %s", "; ".join(e["message"] for e in errors)))


class ShopifyGraphQLError(ChannelError):
    """GraphQL level error, with the fix when we know it."""

    def __init__(self, errors, env):
        self.errors = errors
        first = errors[0] if errors else {}
        self.code = (first.get("extensions") or {}).get("code")
        message = first.get("message", "")
        if self.code == "ACCESS_DENIED" or "access denied" in message.lower():
            message = env._(
                "Your Shopify app is missing a permission: %s. Add the scope in the Dev Dashboard, release a new "
                "version, then test the connection again.",
                message,
            )
        else:
            message = env._("Shopify error: %s", message)
        super().__init__(message)
