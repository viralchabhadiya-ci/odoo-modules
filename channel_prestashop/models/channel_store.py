from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from odoo import _, api, fields, models
from odoo.addons.channel_connector_base.models.channel_error import ChannelError

from .ps_client import PrestaClient, normalize_site, text, to_xml

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
CANCELLING_TEMPLATES = ("order_canceled", "payment_error")
DEFAULT_SHIPPED_STATE = "4"
CONFIG_KEYS = "[PS_TIMEZONE|PS_LANG_DEFAULT|PS_SHOP_NAME]"


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _rate(excl, incl):
    return round((incl / excl - 1) * 100, 2) if excl > 0 and incl > excl else 0.0


def _taxes(rate):
    return [{"rate": rate}] if rate else []


def _tz(name):
    try:
        return ZoneInfo(name or "UTC")
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def to_utc(value, tzname):
    """PrestaShop stores dates in the shop's time zone, without an offset."""
    if not value or value.startswith("0000"):
        return None
    local = datetime.strptime(value[:19], DATE_FORMAT).replace(tzinfo=_tz(tzname))
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_local(value, tzname):
    """Naive UTC datetime -> the shop's wall clock, as PrestaShop filters want it."""
    return value.replace(tzinfo=timezone.utc).astimezone(_tz(tzname)).strftime(DATE_FORMAT)


def _address(address, lookups):
    if not address:
        return None
    name = " ".join(filter(None, [address.get("firstname"), address.get("lastname")])).strip()
    return {
        "name": name or None,
        "company": address.get("company") or None,
        "street": address.get("address1"),
        "street2": address.get("address2") or None,
        "city": address.get("city"),
        "zip": address.get("postcode"),
        "state_code": lookups["states"].get(str(address.get("id_state"))),
        "country_code": lookups["countries"].get(str(address.get("id_country"))),
        "phone": address.get("phone_mobile") or address.get("phone") or None,
    }


def normalize_slip(slip):
    """Credit slip -> neutral refund."""
    details = (slip.get("associations") or {}).get("order_slip_details") or []
    return {
        "external_id": str(slip["id"]),
        "amount": _float(slip.get("total_products_tax_incl")) + _float(slip.get("total_shipping_tax_incl")),
        "lines": [
            {"line_id": str(d.get("id_order_detail")), "qty": _float(d.get("product_quantity"))} for d in details
        ],
    }


def normalize_order(order, lookups, slips=None):
    """PrestaShop order -> the connector-neutral order dict used by the base module.

    Prices are sent without tax; the rate is the ratio of the tax-included to the tax-excluded unit price.
    `lookups` holds customers, addresses, countries, states, currencies, carriers, order states and the shop time zone."""
    tzname = lookups.get("timezone")
    lines = []
    for row in (order.get("associations") or {}).get("order_rows") or []:
        qty = _float(row.get("product_quantity")) or 1.0
        excl, incl = _float(row.get("unit_price_tax_excl")), _float(row.get("unit_price_tax_incl"))
        original = _float(row.get("original_product_price"))
        product, combo = str(row.get("product_id")), str(row.get("product_attribute_id") or 0)
        lines.append(
            {
                "external_id": str(row["id"]),
                "name": row.get("product_name"),
                "sku": row.get("product_reference") or None,
                "barcode": row.get("product_ean13") or None,
                "variant_id": combo if combo != "0" else f"p{product}",
                "inventory_ref": f"{product}/{combo}",
                "qty": qty,
                "price": max(original, excl),
                "discounted_price": excl,
                "taxes": _taxes(_rate(excl, incl)),
            }
        )
    extras = []
    shipping = _float(order.get("total_shipping_tax_excl"))
    if shipping:
        carrier = lookups["carriers"].get(str(order.get("id_carrier")))
        extras.append(
            {
                "title": carrier or _("Shipping"),
                "price": shipping,
                "taxes": _taxes(_float(order.get("carrier_tax_rate")) or _rate(shipping, _float(order.get("total_shipping_tax_incl")))),
            }
        )
    wrapping = _float(order.get("total_wrapping_tax_excl"))
    if wrapping:
        extras.append(
            {
                "title": _("Gift wrapping"),
                "price": wrapping,
                "taxes": _taxes(_rate(wrapping, _float(order.get("total_wrapping_tax_incl")))),
            }
        )
    discount = _float(order.get("total_discounts_tax_excl"))
    discounts = (
        [
            {
                "title": _("Discount"),
                "price": discount,
                "taxes": _taxes(_rate(discount, _float(order.get("total_discounts_tax_incl")))),
            }
        ]
        if discount
        else []
    )
    state = lookups["order_states"].get(str(order.get("current_state")), {})
    customer = lookups["customers"].get(str(order.get("id_customer")), {})
    invoice = lookups["addresses"].get(str(order.get("id_address_invoice")))
    delivery = lookups["addresses"].get(str(order.get("id_address_delivery")))
    billing = _address(invoice or delivery, lookups)
    name = order.get("reference") or str(order["id"])
    return {
        "external_id": str(order["id"]),
        "name": name,
        "date": to_utc(order.get("date_add"), tzname),
        "updated_at": to_utc(order.get("date_upd"), tzname),
        "cancelled": bool(state.get("cancelled")),
        "currency": lookups["currencies"].get(str(order.get("id_currency"))),
        "email": customer.get("email"),
        "phone": billing and billing.get("phone"),
        "customer_id": None if customer.get("is_guest") == "1" else (str(order["id_customer"]) if order.get("id_customer") else None),
        "billing": billing,
        "shipping": _address(delivery, lookups),
        "taxes_included": False,
        "gateways": [g for g in (order.get("payment"), order.get("module")) if g],
        "paid": order.get("valid") == "1" or bool(state.get("paid")),
        "fulfilled": bool(state.get("shipped")),
        "lines": lines,
        "shipping_lines": extras,
        "discount_lines": discounts,
        "refunds": [normalize_slip(s) for s in slips or []],
    }


def normalize_product(product, combinations, options, language=None):
    """Product + its combinations -> neutral product dict. `options` maps option value id -> (group id, group name, value)."""
    base = {
        "external_id": str(product["id"]),
        "title": text(product.get("name"), language),
        "description": text(product.get("description"), language) or text(product.get("description_short"), language),
        "updated_at": product.get("date_upd"),
        "image_url": product.get("image_url"),
    }
    price = _float(product.get("price"))
    if not combinations:
        return {
            **base,
            "options": [],
            "variants": [
                {
                    "external_id": f"p{product['id']}",
                    "inventory_ref": f"{product['id']}/0",
                    "sku": product.get("reference") or None,
                    "barcode": product.get("ean13") or None,
                    "price": price,
                    "options": [],
                }
            ],
        }
    groups = {}
    chosen = {}
    for combination in combinations:
        values = {}
        for ref in (combination.get("associations") or {}).get("product_option_values") or []:
            group_id, group_name, value = options.get(str(ref.get("id")), (None, None, None))
            if group_id is not None:
                groups[group_id] = group_name
                values[group_id] = value
        chosen[combination["id"]] = values
    group_ids = sorted(groups)
    variants = []
    for combination in combinations:
        variants.append(
            {
                "external_id": str(combination["id"]),
                "inventory_ref": f"{product['id']}/{combination['id']}",
                "sku": combination.get("reference") or None,
                "barcode": combination.get("ean13") or None,
                "price": price + _float(combination.get("price")),
                "options": [chosen[combination["id"]].get(g) or "Any" for g in group_ids],
            }
        )
    return {**base, "options": [groups[g] for g in group_ids], "variants": variants}


class ChannelStore(models.Model):
    _inherit = "channel.store"

    channel_type = fields.Selection(
        selection_add=[("prestashop", "PrestaShop")], ondelete={"prestashop": "cascade"}
    )
    prestashop_timezone = fields.Char(string="Shop time zone", help="Read from PrestaShop, e.g. Europe/Paris.")
    prestashop_language = fields.Integer(string="Shop language id", copy=False)

    def _presta(self):
        return PrestaClient(self)

    @api.model
    def _credentials_help(self, channel_type):
        if channel_type != "prestashop":
            return super()._credentials_help(channel_type)
        return _(
            "<p>In the PrestaShop back office open <b>Advanced Parameters &gt; Webservice</b>, switch the webservice "
            "on and click <b>Add new webservice key</b>. Generate a key and give it <b>View</b> on orders, order_details, "
            "order_slips, order_states, customers, addresses, countries, states, currencies, carriers, products, "
            "combinations, product_options, product_option_values, stock_availables, configurations, and "
            "<b>Modify</b> on stock_availables and order_carriers and <b>Add</b> on order_histories (for stock and "
            "shipments). Paste the key and the shop address (without /api).</p>"
        )

    # -- connection ---------------------------------------------------------------

    def _prestashop_test_connection(self):
        self.ensure_one()
        client = self._presta()
        self.shop_url = normalize_site(self.shop_url)
        client.request("GET", "orders", {"limit": "1", "display": "[id]"})  # fails with a readable error on a bad key
        settings = {}
        try:
            rows = client.rows(client.request("GET", "configurations", {"display": "full", "filter[name]": CONFIG_KEYS}), "configurations")
            settings = {row["name"]: row.get("value") for row in rows}
        except ChannelError:
            pass  # the key may not read configurations: dates are then taken as UTC
        self.write(
            {
                "prestashop_timezone": settings.get("PS_TIMEZONE") or self.prestashop_timezone or "UTC",
                "prestashop_language": int(settings.get("PS_LANG_DEFAULT") or 0) or self.prestashop_language,
            }
        )
        # PrestaShop has one stock pool per shop, so one location is enough
        self._sync_locations([{"external_id": "0", "name": _("PrestaShop stock")}])
        return settings.get("PS_SHOP_NAME") or self.shop_url

    # -- lookups -------------------------------------------------------------------

    def _prestashop_order_states(self, client):
        states = {}
        for row in client.list("order_states"):
            template = text(row.get("template"), self.prestashop_language or None)
            states[str(row["id"])] = {
                "cancelled": template in CANCELLING_TEMPLATES,
                "paid": row.get("paid") == "1",
                "shipped": row.get("shipped") == "1",
                "delivered": row.get("delivery") == "1",
                "deleted": row.get("deleted") == "1",
            }
        return states

    def _prestashop_lookups(self, client, orders):
        """Everything the orders refer to, fetched in a few bulk requests instead of one per order."""
        customers = {str(c["id"]): c for c in client.get_many("customers", [o.get("id_customer") for o in orders])}
        address_ids = [o.get("id_address_invoice") for o in orders] + [o.get("id_address_delivery") for o in orders]
        addresses = {str(a["id"]): a for a in client.get_many("addresses", address_ids)}
        return {
            "timezone": self.prestashop_timezone,
            "customers": customers,
            "addresses": addresses,
            "countries": {str(c["id"]): c.get("iso_code") for c in client.list("countries", {"display": "[id,iso_code]"})},
            "states": {str(s["id"]): s.get("iso_code") for s in client.list("states", {"display": "[id,iso_code]"})},
            "currencies": {str(c["id"]): c.get("iso_code") for c in client.list("currencies", {"display": "[id,iso_code]"})},
            "carriers": {str(c["id"]): c.get("name") for c in client.list("carriers", {"display": "[id,name]"})},
            "order_states": self._prestashop_order_states(client),
        }

    # -- orders -------------------------------------------------------------------

    def _prestashop_fetch_orders(self, since):
        self.ensure_one()
        client = self._presta()
        stamp = to_local(fields.Datetime.to_datetime(since), self.prestashop_timezone)
        changed = {"filter[date_upd]": f">[{stamp}]", "date": "1", "sort": "[date_upd_ASC]"}
        orders = {str(o["id"]): o for o in client.list("orders", changed)}
        # a credit slip does not always touch the order, so look for new slips too
        new_slips = list(client.list("order_slips", {"filter[date_upd]": f">[{stamp}]", "date": "1"}))
        missing = {str(s["id_order"]) for s in new_slips} - set(orders)
        for order in client.get_many("orders", missing):
            orders[str(order["id"])] = order
        return self._prestashop_normalize(client, list(orders.values()))

    def _prestashop_normalize(self, client, orders):
        if not orders:
            return []
        lookups = self._prestashop_lookups(client, orders)
        slips = {}
        for slip in client.get_many("order_slips", [o["id"] for o in orders], key="id_order"):
            slips.setdefault(str(slip["id_order"]), []).append(slip)
        return [normalize_order(o, lookups, slips.get(str(o["id"]))) for o in orders]

    def _prestashop_fetch_order(self, external_id):
        client = self._presta()
        orders = list(client.get_many("orders", [external_id]))
        return self._prestashop_normalize(client, orders)[0] if orders else None

    # -- products -----------------------------------------------------------------

    def _prestashop_fetch_products(self):
        self.ensure_one()
        client = self._presta()
        language = {"language": self.prestashop_language} if self.prestashop_language else {}
        options = {}
        groups = {str(g["id"]): text(g.get("name"), self.prestashop_language or None) for g in client.list("product_options", language)}
        for value in client.list("product_option_values", language):
            group = str(value.get("id_attribute_group"))
            options[str(value["id"])] = (int(group), groups.get(group, group), text(value.get("name"), self.prestashop_language or None))
        combinations = {}
        for combination in client.list("combinations"):
            combinations.setdefault(str(combination["id_product"]), []).append(combination)
        for product in client.list("products", language):
            if product.get("active") == "0":
                continue
            product["image_url"] = self._prestashop_image_url(product)
            yield normalize_product(product, combinations.get(str(product["id"])), options, self.prestashop_language or None)

    def _prestashop_image_url(self, product):
        image = str(product.get("id_default_image") or "")
        if not image or image == "0":
            return None
        # the original picture sits in a public folder made of the id digits: 123 -> img/p/1/2/3/123.jpg
        return f"{normalize_site(self.shop_url)}/img/p/{'/'.join(image)}/{image}.jpg"

    # -- stock --------------------------------------------------------------------

    def _prestashop_push_stock_levels(self, items):
        client = self._presta()
        products = {item["inventory_ref"].partition("/")[0] for item in items}
        records = {}
        for record in client.get_many("stock_availables", products, key="id_product"):
            records[(str(record["id_product"]), str(record["id_product_attribute"]))] = record
        refused = []
        for item in items:
            product, _sep, combo = item["inventory_ref"].partition("/")
            record = records.get((product, combo))
            if not record or record.get("depends_on_stock") == "1":
                refused.append(item["mapping_id"])  # gone from the shop, or PrestaShop counts the stock itself
                continue
            values = {k: v for k, v in record.items() if k != "associations" and not isinstance(v, (list, dict))}
            values["quantity"] = int(item["qty"])
            client.request("PUT", f"stock_availables/{record['id']}", xml=to_xml("stock_available", values))
        return refused

    # -- shipments ----------------------------------------------------------------

    def _prestashop_push_fulfillment(self, picking):
        """Put the tracking number on the order and, once every delivery is done, move it to the Shipped state."""
        order = picking.sale_id
        client = self._presta()
        tracking = picking.carrier_tracking_ref
        carriers = list(client.list("order_carriers", {"filter[id_order]": f"[{order.channel_order_id}]"}))
        if tracking and carriers:
            record = {k: v for k, v in carriers[0].items() if not isinstance(v, (list, dict))}
            record["tracking_number"] = tracking
            client.request("PUT", f"order_carriers/{record['id']}", xml=to_xml("order_carrier", record))
        outgoing = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        if not all(p.state in ("done", "cancel") for p in outgoing):
            return
        states = self._prestashop_order_states(client)
        current = next(iter(client.get_many("orders", [order.channel_order_id])), {}).get("current_state")
        if states.get(str(current), {}).get("shipped"):
            return  # already shipped (or beyond) on the shop
        shipped = sorted(
            (int(i) for i, s in states.items() if s["shipped"] and not s["delivered"] and not s["deleted"])
        )
        client.request(
            "POST",
            "order_histories",
            xml=to_xml(
                "order_history",
                {"id_order": order.channel_order_id, "id_order_state": shipped[0] if shipped else DEFAULT_SHIPPED_STATE},
            ),
        )
