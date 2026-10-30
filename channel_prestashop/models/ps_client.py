import logging
import time
from xml.sax.saxutils import escape

import requests

from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)

TIMEOUT = 30
RETRIES = 2
PAGE = 100
CHUNK = 50  # ids per `filter[id]=[a|b|c]` request, keeps the URL short


def normalize_site(url):
    """'shop.example.com/' -> 'https://shop.example.com' (a /api suffix is dropped)."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/api"):
        url = url[:-4]
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def to_xml(resource, values):
    """PrestaShop reads XML only: {"quantity": 5} -> <prestashop><stock_available>...</stock_available></prestashop>."""
    inner = "".join(f"<{key}>{escape(str(value if value is not None else ''))}</{key}>" for key, value in values.items())
    return f"<?xml version='1.0' encoding='UTF-8'?><prestashop><{resource}>{inner}</{resource}></prestashop>"


def text(value, language=None):
    """Multilingual fields come as a plain string or as [{"id": "1", "value": "..."}]."""
    if isinstance(value, list):
        for entry in value:
            if language is None or str(entry.get("id")) == str(language):
                return entry.get("value") or ""
        return (value[0].get("value") if value else "") or ""
    return value or ""


class PrestaClient:
    """PrestaShop Webservice client: the API key is the basic-auth user name, answers are JSON."""

    def __init__(self, store):
        self.store = store
        self.env = store.env
        self.site = normalize_site(store.shop_url)
        if not self.site:
            raise ChannelError(self.env._("Enter the shop address, e.g. https://shop.example.com."))
        key = store.sudo().api_key
        if not key:
            raise ChannelError(
                self.env._("Enter the Webservice key (PrestaShop back office > Advanced Parameters > Webservice).")
            )
        self.auth = (key, "")
        self.api = f"{self.site}/api"

    def request(self, method, resource, params=None, xml=None):
        url = f"{self.api}/{resource.lstrip('/')}"
        query = {"output_format": "JSON", **(params or {})}
        headers = {"Output-Format": "JSON"}
        if xml is not None:
            headers["Content-Type"] = "text/xml; charset=utf-8"
        for attempt in range(RETRIES + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    params=query,
                    data=xml.encode() if xml is not None else None,
                    headers=headers,
                    auth=self.auth,
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise ChannelError(
                    self.env._("Could not reach %(site)s: %(error)s", site=self.site, error=exc)
                ) from exc
            if response.status_code in (429, 502, 503, 504) and attempt < RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            break
        self._check(response, resource)
        if not response.content.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            raise ChannelError(
                self.env._(
                    "%s did not answer like PrestaShop. Check the address (no /api at the end) and that no firewall "
                    "blocks the webservice.",
                    self.site,
                )
            )

    def _check(self, response, resource):
        code = response.status_code
        if code < 400:
            return
        if code == 401:
            raise ChannelAuthError(
                self.env._(
                    "PrestaShop rejected the Webservice key. Check it under Advanced Parameters > Webservice and make "
                    "sure the webservice is switched on."
                )
            )
        if code in (403, 405):
            raise ChannelError(
                self.env._(
                    "The Webservice key may not use '%s'. In Advanced Parameters > Webservice tick View (and Add / "
                    "Modify for stock, order_carriers and order_histories) for this resource.",
                    resource,
                )
            )
        if code == 404:
            raise ChannelError(
                self.env._(
                    "The PrestaShop webservice was not found on %s. Switch it on under Advanced Parameters > "
                    "Webservice and check the shop address.",
                    self.site,
                )
            )
        try:
            body = response.json()
            message = (body.get("errors") or [{}])[0].get("message") or response.text[:200]
        except (ValueError, AttributeError, IndexError):
            message = response.text[:200]
        raise ChannelError(
            self.env._("PrestaShop answered HTTP %(code)s: %(message)s", code=code, message=message)
        )

    # -- reading --------------------------------------------------------------------

    @staticmethod
    def rows(body, resource):
        """A list answer is {"orders": [...]}, or [] when nothing matches."""
        if isinstance(body, dict):
            return body.get(resource) or []
        return []

    def list(self, resource, params=None):
        """All rows of a resource, page by page (full records)."""
        params = {"display": "full", **(params or {})}
        offset = 0
        while True:
            rows = self.rows(
                self.request("GET", resource, {**params, "limit": f"{offset},{PAGE}"}), resource
            )
            yield from rows
            if len(rows) < PAGE:
                return
            offset += PAGE

    def get_many(self, resource, ids, key="id"):
        """Full records whose `key` is in `ids`."""
        ids = sorted({str(i) for i in ids if i and str(i) != "0"})
        for start in range(0, len(ids), CHUNK):
            chunk = "|".join(ids[start : start + CHUNK])
            yield from self.list(resource, {f"filter[{key}]": f"[{chunk}]"})
