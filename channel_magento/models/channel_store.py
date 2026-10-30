from urllib.parse import quote

from odoo import _, api, fields, models
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError

from .magento_client import MagentoClient, normalize_site

STOCK_CHUNK = 100
LEGACY_LOCATION = "default"
CATALOG_TYPES = "simple,configurable"


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def sku_path(sku):
    """A SKU as a URL path part; Magento wants a slash double-encoded."""
    return quote(sku, safe="").replace("%2F", "%252F")


def _rate(tax, net):
    return round(tax / net * 100, 2) if tax > 0 and net > 0 else 0.0


def _address(address):
    if not address:
        return None
    street = [s for s in address.get("street") or [] if s]
    name = " ".join(filter(None, [address.get("firstname"), address.get("lastname")])).strip()
    return {
        "name": name or None,
        "company": address.get("company") or None,
        "street": street[0] if street else None,
        "street2": " ".join(street[1:]) or None,
        "city": address.get("city"),
        "zip": address.get("postcode"),
        "state_code": address.get("region_code") or address.get("region"),
        "country_code": address.get("country_id"),
        "phone": address.get("telephone"),
    }


def normalize_credit_memo(memo, valid_lines=None):
    lines = [
        {"line_id": str(i["order_item_id"]), "qty": _float(i.get("qty"))}
        for i in memo.get("items") or []
        if _float(i.get("qty")) > 0 and (valid_lines is None or str(i["order_item_id"]) in valid_lines)
    ]
    return {
        "external_id": str(memo.get("increment_id") or memo["entity_id"]),
        "amount": _float(memo.get("grand_total")),
        "lines": lines,
    }


def normalize_order(order, memos=None):
    """Magento order -> the connector-neutral order dict used by the base module.

    Magento keeps unit prices and tax percentages apart, so prices go out tax-excluded and the rate is taken
    as it is. Configurable products come as a parent row (money) and a child row (the real SKU)."""
    items = order.get("items") or []
    children = {i["parent_item_id"]: i for i in items if i.get("parent_item_id")}
    lines = []
    for item in items:
        if item.get("parent_item_id"):
            continue
        ordered = _float(item.get("qty_ordered")) or 1.0
        qty = ordered - _float(item.get("qty_canceled"))
        if qty <= 0:
            continue
        real = children.get(item["item_id"]) or item
        unit = _float(item.get("price")) or _float(item.get("row_total")) / ordered
        rate = _float(item.get("tax_percent"))
        lines.append(
            {
                "external_id": str(item["item_id"]),
                "name": item.get("name"),
                "sku": real.get("sku"),
                "variant_id": str(real.get("product_id")) if real.get("product_id") else None,
                "inventory_ref": real.get("sku"),
                "qty": qty,
                "price": unit,
                "discounted_price": unit - _float(item.get("discount_amount")) / ordered,
                "taxes": [{"rate": rate}] if rate else [],
            }
        )
    shipping = _float(order.get("shipping_amount"))
    shipping_rate = _rate(_float(order.get("shipping_tax_amount")), shipping)
    shipping_taxes = [{"rate": shipping_rate}] if shipping_rate else []
    shipping_discount = _float(order.get("shipping_discount_amount"))
    assignment = ((order.get("extension_attributes") or {}).get("shipping_assignments") or [{}])[0]
    delivery = (assignment.get("shipping") or {}).get("address")
    billing = order.get("billing_address")
    payment = order.get("payment") or {}
    extra = payment.get("additional_information") or []
    guest = bool(order.get("customer_is_guest")) or not order.get("customer_id")
    state = order.get("state")
    top_level = {str(i["item_id"]) for i in items if not i.get("parent_item_id")}
    return {
        "external_id": str(order["entity_id"]),
        "name": order.get("increment_id") or str(order["entity_id"]),
        "date": order.get("created_at"),
        "updated_at": order.get("updated_at"),
        "cancelled": state == "canceled",
        "currency": order.get("order_currency_code"),
        "email": order.get("customer_email"),
        "phone": (billing or {}).get("telephone"),
        "customer_id": None if guest else str(order["customer_id"]),
        "billing": _address(billing) or _address(delivery),
        "shipping": _address(delivery) or _address(billing),
        "taxes_included": False,
        "gateways": [g for g in [payment.get("method"), *[e for e in extra if isinstance(e, str)]] if g],
        "paid": _float(order.get("total_invoiced")) > 0,
        "fulfilled": state == "complete",
        "lines": lines,
        "shipping_lines": [
            {"title": order.get("shipping_description") or "Shipping", "price": shipping, "taxes": shipping_taxes}
        ]
        if shipping
        else [],
        "discount_lines": [{"title": "Shipping discount", "price": shipping_discount, "taxes": shipping_taxes}]
        if shipping_discount
        else [],
        "refunds": [normalize_credit_memo(m, top_level) for m in memos or []],
    }


def _attribute(product, code):
    for entry in product.get("custom_attributes") or []:
        if entry.get("attribute_code") == code:
            return entry.get("value")
    return None


def normalize_product(product, site, children=None, option_labels=None):
    """Simple product, or configurable product with its children, -> neutral product dict.

    `option_labels` is [(attribute label, attribute code, {value index: label})] in display order."""
    gallery = product.get("media_gallery_entries") or []
    image = _attribute(product, "image") or (gallery[0].get("file") if gallery else None)
    base = {
        "external_id": str(product["id"]),
        "title": product.get("name"),
        "description": _attribute(product, "description"),
        "updated_at": product.get("updated_at"),
        "image_url": f"{site}/media/catalog/product{image}" if image and image != "no_selection" else None,
    }
    if not children:
        return {
            **base,
            "options": [],
            "variants": [
                {
                    "external_id": str(product["id"]),
                    "inventory_ref": product["sku"],
                    "sku": product["sku"],
                    "price": _float(product.get("price")),
                    "options": [],
                }
            ],
        }
    labels = option_labels or []
    variants = []
    for child in children:
        values = []
        for _label, code, mapping in labels:
            values.append(mapping.get(str(_attribute(child, code))) or "Any")
        variants.append(
            {
                "external_id": str(child["id"]),
                "inventory_ref": child["sku"],
                "sku": child["sku"],
                "price": _float(child.get("price")) or _float(product.get("price")),
                "options": values,
            }
        )
    return {**base, "options": [label for label, _code, _mapping in labels], "variants": variants}


class ChannelStore(models.Model):
    _inherit = "channel.store"

    channel_type = fields.Selection(selection_add=[("magento", "Magento 2")], ondelete={"magento": "cascade"})
    magento_store_code = fields.Char(
        string="Store view code",
        default="default",
        help="The code of the store view in the REST address /rest/<code>/V1. 'default' fits most shops.",
    )
    magento_msi = fields.Boolean(string="Multi-Source Inventory", copy=False)

    def _magento(self):
        return MagentoClient(self)

    @api.model
    def _credentials_help(self, channel_type):
        if channel_type != "magento":
            return super()._credentials_help(channel_type)
        return _(
            "<p>In the Magento admin open <b>System &gt; Extensions &gt; Integrations</b>, add an integration, open the "
            "<b>API</b> tab and give it access to <b>Sales</b> (orders, shipments, credit memos), <b>Catalog</b>, "
            "<b>Stores</b> and <b>Inventory</b>. Save, <b>Activate</b> it and paste the <b>Access Token</b> here "
            "together with the shop address.</p>"
        )

    # -- connection ---------------------------------------------------------------

    def _magento_test_connection(self):
        self.ensure_one()
        client = self._magento()
        self.shop_url = normalize_site(self.shop_url)
        configs = client.request("GET", "store/storeConfigs")
        client.request(
            "GET", "orders", {"searchCriteria[pageSize]": 1}
        )  # fails with a readable error if the token lacks Sales access
        sources = self._magento_sources(client)
        self.magento_msi = bool(sources)
        if sources:
            self._sync_locations(
                [{"external_id": s["source_code"], "name": s.get("name") or s["source_code"]} for s in sources]
            )
        else:
            self._sync_locations([{"external_id": LEGACY_LOCATION, "name": _("Magento stock")}])
        config = configs[0] if isinstance(configs, list) and configs else {}
        return f"{self.shop_url} ({config.get('base_currency_code', '')})".strip(" ()")

    def _magento_sources(self, client):
        """Multi-Source Inventory sources, or [] when the shop runs the single stock."""
        try:
            body = client.request("GET", "inventory/sources")
        except ChannelAuthError:
            raise
        except ChannelError:
            return []
        return [s for s in body.get("items") or [] if s.get("enabled", True)]

    # -- orders -------------------------------------------------------------------

    def _magento_fetch_orders(self, since):
        self.ensure_one()
        client = self._magento()
        stamp = fields.Datetime.to_datetime(since).strftime("%Y-%m-%d %H:%M:%S")
        orders = {
            str(o["entity_id"]): o for o in client.search("orders", [("updated_at", stamp, "gteq")], "updated_at")
        }
        # a credit memo does not always touch the order, so look for new memos as well
        new_memos = list(client.search("creditmemos", [("updated_at", stamp, "gteq")], "updated_at"))
        missing = {str(m["order_id"]) for m in new_memos} - set(orders)
        for order_id in missing:
            orders[order_id] = client.request("GET", f"orders/{order_id}")
        return self._magento_normalize(client, list(orders.values()))

    def _magento_normalize(self, client, orders):
        memos = {}
        ids = [str(o["entity_id"]) for o in orders]
        for start in range(0, len(ids), 50):
            for memo in client.search("creditmemos", [("order_id", ",".join(ids[start : start + 50]), "in")]):
                memos.setdefault(str(memo["order_id"]), []).append(memo)
        return [normalize_order(o, memos.get(str(o["entity_id"]))) for o in orders]

    def _magento_fetch_order(self, external_id):
        client = self._magento()
        order = client.request("GET", f"orders/{external_id}")
        return self._magento_normalize(client, [order])[0]

    # -- products -----------------------------------------------------------------

    def _magento_fetch_products(self):
        self.ensure_one()
        client = self._magento()
        site = normalize_site(self.shop_url)
        products = [p for p in client.search("products", [("type_id", CATALOG_TYPES, "in")]) if p.get("status") != 2]
        child_ids = {
            str(link)
            for p in products
            for link in (p.get("extension_attributes") or {}).get("configurable_product_links") or []
        }
        attributes = {}
        for product in products:
            if str(product["id"]) in child_ids:
                continue  # listed as a variant of its configurable parent
            if product.get("type_id") != "configurable":
                yield normalize_product(product, site)
                continue
            children = client.request("GET", f"configurable-products/{sku_path(product['sku'])}/children")
            options = (product.get("extension_attributes") or {}).get("configurable_product_options") or []
            labels = []
            for option in sorted(options, key=lambda o: o.get("position", 0)):
                attribute_id = str(option["attribute_id"])
                if attribute_id not in attributes:
                    attributes[attribute_id] = self._magento_attribute(client, attribute_id)
                code, mapping = attributes[attribute_id]
                labels.append((option.get("label") or code, code, mapping))
            yield normalize_product(product, site, children, labels)

    def _magento_attribute(self, client, attribute_id):
        body = client.request(
            "GET",
            "products/attributes",
            {
                "searchCriteria[filter_groups][0][filters][0][field]": "attribute_id",
                "searchCriteria[filter_groups][0][filters][0][value]": attribute_id,
            },
        )
        attribute = (body.get("items") or [{}])[0]
        return attribute.get("attribute_code"), {
            str(o.get("value")): o.get("label") for o in attribute.get("options") or []
        }

    # -- stock --------------------------------------------------------------------

    def _magento_push_stock_levels(self, items):
        client = self._magento()
        refused = []
        if not self.magento_msi:
            for item in items:
                try:
                    client.request(
                        "PUT",
                        f"products/{sku_path(item['inventory_ref'])}/stockItems/1",
                        json={"stockItem": {"qty": item["qty"], "is_in_stock": item["qty"] > 0}},
                    )
                except ChannelAuthError:
                    raise
                except ChannelError:
                    refused.append(item["mapping_id"])
            return refused
        for start in range(0, len(items), STOCK_CHUNK):
            chunk = items[start : start + STOCK_CHUNK]
            try:
                client.request(
                    "POST",
                    "inventory/source-items",
                    json={"sourceItems": [self._magento_source_item(i) for i in chunk]},
                )
            except ChannelAuthError:
                raise
            except ChannelError:
                # one unknown SKU fails the whole call: find out which ones are at fault
                for item in chunk:
                    try:
                        client.request(
                            "POST", "inventory/source-items", json={"sourceItems": [self._magento_source_item(item)]}
                        )
                    except ChannelAuthError:
                        raise
                    except ChannelError:
                        refused.append(item["mapping_id"])
        return refused

    @staticmethod
    def _magento_source_item(item):
        return {
            "sku": item["inventory_ref"],
            "source_code": item["location"],
            "quantity": item["qty"],
            "status": 1 if item["qty"] > 0 else 0,
        }

    # -- shipments ----------------------------------------------------------------

    def _magento_push_fulfillment(self, picking):
        """Create a Magento shipment for this delivery; Magento completes the order after the last one."""
        order = picking.sale_id
        quantities = {}
        for move in picking.move_ids.filtered(lambda m: m.state == "done"):
            line_id = move.sale_line_id.channel_line_id
            if line_id:
                quantities[line_id] = quantities.get(line_id, 0) + int(move.quantity)
        if not quantities:
            return
        body = {
            "items": [{"order_item_id": int(line), "qty": qty} for line, qty in quantities.items()],
            "notify": True,
        }
        if picking.carrier_tracking_ref:
            body["tracks"] = [
                {
                    "track_number": picking.carrier_tracking_ref,
                    "title": picking.carrier_id.name or "Carrier",
                    "carrier_code": "custom",
                }
            ]
        self._magento().request("POST", f"order/{order.channel_order_id}/ship", json=body)
