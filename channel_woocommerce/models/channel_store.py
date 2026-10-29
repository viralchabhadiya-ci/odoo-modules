import base64
import hashlib
import hmac
import json
import secrets

from odoo import _, api, fields, models
from odoo.addons.channel_connector_base.models.channel_error import ChannelError

from .woo_client import WooClient, normalize_site

WEBHOOK_TOPICS = ("order.created", "order.updated")
CANCELLED = ("cancelled", "failed", "trash")
PAID = ("processing", "completed", "refunded")


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _address(node):
    if not node:
        return None
    name = " ".join(filter(None, [node.get("first_name"), node.get("last_name")])).strip()
    return {
        "name": name or None,
        "company": node.get("company") or None,
        "street": node.get("address_1"),
        "street2": node.get("address_2"),
        "city": node.get("city"),
        "zip": node.get("postcode"),
        "state_code": node.get("state"),
        "country_code": node.get("country"),
        "phone": node.get("phone"),
    }


def _line_taxes(taxes, rates):
    """Woo lists tax rate ids per line; the rates themselves live on the order."""
    return [
        {"rate": rates.get(str(t.get("id")), 0.0)}
        for t in taxes or []
        if _float(t.get("total")) or _float(t.get("subtotal"))
    ]


def normalize_order(order, refunds=None):
    """WooCommerce order -> the connector-neutral order dict used by the base module.

    Prices are taken before tax (`subtotal`), so `taxes_included` is always False."""
    rates = {str(t["rate_id"]): _float(t.get("rate_percent")) for t in order.get("tax_lines") or []}
    lines = []
    for line in order.get("line_items") or []:
        qty = _float(line.get("quantity")) or 1.0
        product_id, variation_id = line.get("product_id"), line.get("variation_id")
        lines.append(
            {
                "external_id": str(line["id"]),
                "name": line.get("name"),
                "sku": line.get("sku"),
                "variant_id": str(variation_id or product_id or "") or None,
                "inventory_ref": f"{product_id}/{variation_id}" if variation_id else str(product_id or ""),
                "qty": qty,
                "price": _float(line.get("subtotal")) / qty,
                "discounted_price": _float(line.get("total")) / qty,
                "taxes": _line_taxes(line.get("taxes"), rates),
            }
        )
    billing = order.get("billing") or {}
    status = order.get("status")
    return {
        "external_id": str(order["id"]),
        "name": f"#{order.get('number') or order['id']}",
        "date": order.get("date_created_gmt"),
        "updated_at": order.get("date_modified_gmt"),
        "cancelled": status in CANCELLED,
        "currency": order.get("currency"),
        "email": billing.get("email"),
        "phone": billing.get("phone"),
        "note": order.get("customer_note"),
        "customer_id": str(order["customer_id"]) if order.get("customer_id") else None,
        "billing": _address(billing),
        "shipping": _address(order.get("shipping")),
        "taxes_included": False,
        "gateways": [g for g in (order.get("payment_method_title"), order.get("payment_method")) if g],
        "paid": bool(order.get("date_paid_gmt")) or status in PAID,
        "fulfilled": status == "completed",
        "lines": lines,
        "shipping_lines": [
            {
                "title": s.get("method_title"),
                "price": _float(s.get("total")),
                "taxes": _line_taxes(s.get("taxes"), rates),
            }
            for s in order.get("shipping_lines") or []
        ],
        "refunds": [normalize_refund(r) for r in refunds or []],
    }


def normalize_refund(refund):
    lines = []
    for item in refund.get("line_items") or []:
        meta = {m.get("key"): m.get("value") for m in item.get("meta_data") or []}
        lines.append(
            {
                "line_id": str(meta.get("_refunded_item_id") or ""),
                "sku": item.get("sku"),
                "qty": abs(_float(item.get("quantity"))),
            }
        )
    return {"external_id": str(refund["id"]), "amount": abs(_float(refund.get("total"))), "lines": lines}


def normalize_product(product, variations=None):
    """WooCommerce product (+ its variations when variable) -> neutral product dict."""
    image = ((product.get("images") or [{}])[0]).get("src")
    base = {
        "external_id": str(product["id"]),
        "title": product.get("name"),
        "description": product.get("description") or product.get("short_description"),
        "product_type": ((product.get("categories") or [{}])[0]).get("name"),
        "updated_at": product.get("date_modified_gmt"),
        "image_url": image,
    }
    if not variations:
        return {
            **base,
            "options": [],
            "variants": [
                {
                    "external_id": str(product["id"]),
                    "inventory_ref": str(product["id"]),
                    "sku": product.get("sku"),
                    "price": _float(product.get("price") or product.get("regular_price")),
                    "options": [],
                }
            ],
        }
    names = [a["name"] for a in sorted(product.get("attributes") or [], key=lambda a: a.get("position", 0)) if a.get("variation")]
    variants = []
    for variation in variations:
        chosen = {a["name"]: a.get("option") for a in variation.get("attributes") or []}
        variants.append(
            {
                "external_id": str(variation["id"]),
                "inventory_ref": f"{product['id']}/{variation['id']}",
                "sku": variation.get("sku"),
                "price": _float(variation.get("price") or variation.get("regular_price")),
                "options": [chosen.get(name) or "Any" for name in names],
            }
        )
    return {**base, "options": names, "variants": variants}


class ChannelStore(models.Model):
    _inherit = "channel.store"

    channel_type = fields.Selection(selection_add=[("woocommerce", "WooCommerce")], ondelete={"woocommerce": "cascade"})

    def _woo(self):
        return WooClient(self)

    @api.model
    def _credentials_help(self, channel_type):
        if channel_type != "woocommerce":
            return super()._credentials_help(channel_type)
        return _(
            "<p>In WordPress open <b>WooCommerce &gt; Settings &gt; Advanced &gt; REST API</b>, click "
            "<b>Add key</b>, choose <b>Read/Write</b> permission and paste the Consumer key and Consumer secret here. "
            "The shop must use https, and <b>Settings &gt; Permalinks</b> must not be set to 'Plain'.</p>"
        )

    # -- connection ---------------------------------------------------------------

    def _woocommerce_test_connection(self):
        self.ensure_one()
        client = self._woo()
        status = client.request("GET", "system_status")  # fails with a readable error on bad keys
        self.shop_url = normalize_site(self.shop_url)
        # WooCommerce has one stock pool: a single pseudo location keeps the stock logic shared
        self._sync_locations([{"external_id": "0", "name": _("WooCommerce stock")}])
        settings = status.get("settings") or {}
        name = (status.get("environment") or {}).get("site_url") or self.shop_url
        return f"{name} ({settings.get('currency', '')})".strip(" ()")

    # -- webhooks -----------------------------------------------------------------

    def _woocommerce_register_webhooks(self):
        self.ensure_one()
        if not self.webhook_url.startswith("https://"):
            raise ChannelError(
                _("WooCommerce delivers webhooks to https addresses only. Orders will still be imported on a schedule.")
            )
        client = self._woo()
        if not self.sudo().webhook_secret:
            self.sudo().webhook_secret = secrets.token_urlsafe(24)
        known = {(w["topic"], w["delivery_url"]) for w in client.paginate("webhooks")}
        for topic in WEBHOOK_TOPICS:
            if (topic, self.webhook_url) in known:
                continue
            client.request(
                "POST",
                "webhooks",
                json={
                    "name": f"Odoo: {topic}",
                    "topic": topic,
                    "delivery_url": self.webhook_url,
                    "secret": self.sudo().webhook_secret,
                    "status": "active",
                },
            )

    def _woocommerce_verify_webhook(self, body, headers):
        secret = (self.sudo().webhook_secret or "").encode()
        received = headers.get("X-WC-Webhook-Signature", "")
        if not secret or not received:
            return False
        digest = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(digest, received)

    def _woocommerce_parse_webhook(self, body, headers):
        topic = headers.get("X-WC-Webhook-Topic", "")
        if topic not in WEBHOOK_TOPICS:
            return []  # includes the ping WooCommerce sends when a webhook is created
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            return []
        order_id = str(payload["id"])
        return [
            (
                "import_order",
                _("Import order #%s", payload.get("number") or order_id),
                {"external_id": order_id},
                f"order:{order_id}:{payload.get('date_modified_gmt')}",
            )
        ]

    # -- orders & products -----------------------------------------------------------

    def _woocommerce_fetch_orders(self, since):
        self.ensure_one()
        client = self._woo()
        params = {
            "modified_after": fields.Datetime.to_datetime(since).strftime("%Y-%m-%dT%H:%M:%S"),
            "dates_are_gmt": "true",
            "orderby": "modified",
            "order": "asc",
            "status": "any",
        }
        for order in client.paginate("orders", params):
            yield normalize_order(order, self._fetch_refunds(client, order))

    def _woocommerce_fetch_order(self, external_id):
        client = self._woo()
        order = client.request("GET", f"orders/{external_id}")
        return normalize_order(order, self._fetch_refunds(client, order))

    @staticmethod
    def _fetch_refunds(client, order):
        if not order.get("refunds"):
            return []
        return client.request("GET", f"orders/{order['id']}/refunds")

    def _woocommerce_fetch_products(self):
        client = self._woo()
        for product in client.paginate("products", {"status": "any"}):
            if product.get("status") == "trash":
                continue
            variations = (
                list(client.paginate(f"products/{product['id']}/variations", per_page=100))
                if product.get("type") == "variable"
                else None
            )
            yield normalize_product(product, variations)

    # -- stock ------------------------------------------------------------------------

    def _woocommerce_push_stock_levels(self, items):
        client = self._woo()
        simple, by_parent = [], {}
        for item in items:
            payload = {"manage_stock": True, "stock_quantity": item["qty"]}
            parent, _sep, variation = item["inventory_ref"].partition("/")
            if variation:
                by_parent.setdefault(parent, []).append({"id": int(variation), **payload})
            else:
                simple.append({"id": int(parent), **payload})
        failures = []
        if simple:
            failures += self._batch_failures(client.request("POST", "products/batch", json={"update": simple}))
        for parent, variations in by_parent.items():
            failures += self._batch_failures(
                client.request("POST", f"products/{parent}/variations/batch", json={"update": variations})
            )
        if failures:
            raise ChannelError(_("WooCommerce refused some stock updates: %s", "; ".join(failures)))

    @staticmethod
    def _batch_failures(response):
        return [
            f"#{row.get('id')}: {row['error'].get('message')}"
            for row in response.get("update") or []
            if row.get("error")
        ]

    # -- shipments --------------------------------------------------------------------

    def _woocommerce_push_fulfillment(self, picking):
        """WooCommerce has no shipment object: add the tracking as a customer note and complete the order."""
        order = picking.sale_id
        client = self._woo()
        tracking = getattr(picking, "carrier_tracking_ref", False)
        if tracking:
            carrier = picking.carrier_id.name if getattr(picking, "carrier_id", False) else ""
            note = _("Your order has shipped. %(carrier)s tracking number: %(tracking)s", carrier=carrier, tracking=tracking)
            client.request("POST", f"orders/{order.channel_order_id}/notes", json={"note": note.strip(), "customer_note": True})
        outgoing = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        if all(p.state in ("done", "cancel") for p in outgoing):
            client.request("PUT", f"orders/{order.channel_order_id}", json={"status": "completed"})
