import logging
import time

import requests

from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)

TIMEOUT = 30
RETRIES = 2


def normalize_site(url):
    """'shop.example.com/' -> 'https://shop.example.com'."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


class WooClient:
    """Minimal WooCommerce REST v3 client (consumer key and secret over HTTPS)."""

    def __init__(self, store):
        self.store = store
        self.env = store.env
        self.site = normalize_site(store.shop_url)
        if not self.site:
            raise ChannelError(self.env._("Enter the website address, e.g. https://shop.example.com."))
        if not self.site.startswith("https://"):
            raise ChannelError(
                self.env._(
                    "WooCommerce only accepts consumer keys over https. Install an SSL certificate on the shop and use https://."
                )
            )
        secret = store.sudo()
        if not (secret.api_key and secret.api_secret):
            raise ChannelError(
                self.env._("Enter the Consumer key and Consumer secret (WooCommerce > Settings > Advanced > REST API).")
            )
        self.auth = (secret.api_key, secret.api_secret)
        self.api = f"{self.site}/wp-json/wc/v3"

    def request(self, method, path, params=None, json=None, full=False):
        """Call the API. `full=True` returns (data, response) so callers can read pagination headers."""
        url = path if path.startswith("http") else f"{self.api}/{path.lstrip('/')}"
        for attempt in range(RETRIES + 1):
            try:
                response = requests.request(
                    method, url, params=params, json=json, auth=self.auth, timeout=TIMEOUT
                )
            except requests.RequestException as exc:
                raise ChannelError(
                    self.env._("Could not reach %(site)s: %(error)s", site=self.site, error=exc)
                ) from exc
            if response.status_code in (429, 502, 503, 504) and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            break
        self._check(response)
        try:
            data = response.json()
        except ValueError:
            raise ChannelError(
                self.env._(
                    "%s did not answer like WooCommerce. A security plugin or firewall may be blocking the REST API.",
                    self.site,
                )
            )
        return (data, response) if full else data

    def _check(self, response):
        code = response.status_code
        if code < 400:
            return
        if code == 401:
            raise ChannelAuthError(
                self.env._(
                    "WooCommerce rejected the Consumer key / secret. Create a new key with Read/Write permission "
                    "under WooCommerce > Settings > Advanced > REST API."
                )
            )
        if code == 403:
            raise ChannelError(
                self.env._(
                    "The WooCommerce key does not have permission for this. Give it Read/Write access and try again."
                )
            )
        if code == 404:
            raise ChannelError(
                self.env._(
                    "The WooCommerce REST API was not found on %s. In WordPress go to Settings > Permalinks and pick "
                    "anything except 'Plain', then test again.",
                    self.site,
                )
            )
        try:
            message = response.json().get("message", "")
        except ValueError:
            message = response.text[:200]
        raise ChannelError(
            self.env._("WooCommerce answered HTTP %(code)s: %(message)s", code=code, message=message)
        )

    def paginate(self, path, params=None, per_page=50):
        page = 1
        while True:
            data, response = self.request(
                "GET", path, params={**(params or {}), "per_page": per_page, "page": page}, full=True
            )
            yield from data
            if page >= int(response.headers.get("X-WP-TotalPages", 1)):
                return
            page += 1
