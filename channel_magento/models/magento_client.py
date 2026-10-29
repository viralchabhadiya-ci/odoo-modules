import logging
import re
import time

import requests

from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

_logger = logging.getLogger(__name__)

TIMEOUT = 30
RETRIES = 2
PAGE = 100


def normalize_site(url):
    """'shop.example.com/' -> 'https://shop.example.com' (a /rest or /index.php suffix is dropped)."""
    url = (url or "").strip().rstrip("/")
    url = re.sub(r"/(index\.php/)?rest(/.*)?$", "", url)
    url = re.sub(r"/index\.php$", "", url)
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def criteria(filters=None, page=1, page_size=PAGE, sort="entity_id"):
    """searchCriteria query parameters. `filters` is a list of (field, value, condition); each one is ANDed."""
    params = {
        "searchCriteria[pageSize]": page_size,
        "searchCriteria[currentPage]": page,
        "searchCriteria[sortOrders][0][field]": sort,
        "searchCriteria[sortOrders][0][direction]": "ASC",
    }
    for index, (field, value, condition) in enumerate(filters or []):
        base = f"searchCriteria[filter_groups][{index}][filters][0]"
        params[f"{base}[field]"] = field
        params[f"{base}[value]"] = value
        params[f"{base}[condition_type]"] = condition
    return params


def _message(response):
    try:
        body = response.json()
    except ValueError:
        return (response.text or "")[:200]
    message = body.get("message") or ""
    for index, value in enumerate(body.get("parameters") or [], 1):
        if isinstance(value, (str, int, float)):
            message = message.replace(f"%{index}", str(value))
        elif isinstance(value, dict):
            message = message.replace(f"%{index}", ", ".join(str(v) for v in value.values()))
    return message or (response.text or "")[:200]


class MagentoClient:
    """Magento 2 / Adobe Commerce REST client: an integration access token as bearer."""

    def __init__(self, store):
        self.store = store
        self.env = store.env
        self.site = normalize_site(store.shop_url)
        if not self.site:
            raise ChannelError(self.env._("Enter the shop address, e.g. https://shop.example.com."))
        token = store.sudo().access_token
        if not token:
            raise ChannelError(
                self.env._("Enter the Integration access token (Magento admin > System > Extensions > Integrations).")
            )
        self.token = token
        code = (store.magento_store_code or "default").strip()
        self.api = f"{self.site}/rest/{code}/V1"

    def request(self, method, path, params=None, json=None):
        url = f"{self.api}/{path.lstrip('/')}"
        for attempt in range(RETRIES + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
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
        self._check(response)
        if not response.content.strip():
            return {}
        try:
            return response.json()
        except ValueError:
            raise ChannelError(
                self.env._(
                    "%s did not answer like Magento. Check the address; if the shop has no URL rewrites, use "
                    "https://shop.example.com/index.php as the address.",
                    self.site,
                )
            )

    def _check(self, response):
        code = response.status_code
        if code < 400:
            return
        text = _message(response)
        if code == 401:
            if "authorized to access" in text:
                raise ChannelError(
                    self.env._(
                        "The integration is not allowed to do this (%s). Open it under System > Extensions > "
                        "Integrations > Edit > API and give it access to Sales, Catalog and Stores resources.",
                        text,
                    )
                )
            raise ChannelAuthError(
                self.env._(
                    "Magento rejected the access token. Check the Integration is activated and paste the Access "
                    "Token (not the Consumer key)."
                )
            )
        if code == 404 and "Request does not match" in text:
            raise ChannelError(
                self.env._(
                    "Magento does not know this REST address on %s. Check the store view code; without URL rewrites "
                    "the shop address must end with /index.php.",
                    self.site,
                )
            )
        raise ChannelError(self.env._("Magento answered HTTP %(code)s: %(message)s", code=code, message=text))

    def search(self, path, filters=None, sort="entity_id"):
        """All items of a searchCriteria list, page by page (stops at a short page)."""
        page = 1
        while True:
            body = self.request("GET", path, criteria(filters, page, PAGE, sort))
            items = body.get("items") or []
            yield from items
            if len(items) < PAGE:
                return
            page += 1
