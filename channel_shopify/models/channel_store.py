import base64
import hashlib
import hmac
import json

from odoo import _, api, fields, models
from odoo.addons.channel_connector_base.models.channel_error import ChannelError

from .shopify_client import ShopifyClient, normalize_shop

SHOP_QUERY = """
query { shop { name currencyCode } locations(first: 50) { nodes { id legacyResourceId name isActive } } }
"""

MONEY = "presentmentMoney { amount }"
ADDRESS = "name company address1 address2 city zip provinceCode countryCodeV2 phone"

ORDER_FIELDS = f"""
  id legacyResourceId name createdAt updatedAt cancelledAt note email phone taxesIncluded
  presentmentCurrencyCode displayFinancialStatus displayFulfillmentStatus paymentGatewayNames
  customer {{ legacyResourceId }}
  billingAddress {{ {ADDRESS} }}
  shippingAddress {{ {ADDRESS} }}
  lineItems(first: 50) {{ nodes {{
    id name sku quantity
    variant {{ legacyResourceId barcode inventoryItem {{ legacyResourceId }} }}
    originalUnitPriceSet {{ {MONEY} }}
    discountedUnitPriceSet {{ {MONEY} }}
    taxLines {{ rate title }}
  }} }}
  shippingLines(first: 5) {{ nodes {{ title discountedPriceSet {{ {MONEY} }} taxLines {{ rate title }} }} }}
  refunds {{
    id legacyResourceId
    totalRefundedSet {{ {MONEY} }}
    refundLineItems(first: 30) {{ nodes {{ quantity lineItem {{ id sku }} }} }}
  }}
"""

ORDERS_QUERY = f"""
query($first: Int!, $after: String, $query: String) {{
  orders(first: $first, after: $after, query: $query, sortKey: UPDATED_AT) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ {ORDER_FIELDS} }}
  }}
}}"""

ORDER_QUERY = f"query($id: ID!) {{ order(id: $id) {{ {ORDER_FIELDS} }} }}"

PRODUCTS_QUERY = """
query($first: Int!, $after: String) {
  products(first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id legacyResourceId title descriptionHtml productType updatedAt
      featuredMedia { preview { image { url } } }
      options { name position }
      variants(first: 100) { nodes {
        id legacyResourceId sku barcode price
        inventoryItem { legacyResourceId }
        selectedOptions { name value }
      } }
    }
  }
}"""

STOCK_MUTATION = """
mutation($input: InventorySetQuantitiesInput!, $key: String!) {
  inventorySetQuantities(input: $input) @idempotent(key: $key) {
    userErrors { field message }
  }
}"""

FULFILLMENT_ORDERS_QUERY = """
query($id: ID!) { order(id: $id) { fulfillmentOrders(first: 10) { nodes {
  id status
  lineItems(first: 50) { nodes { id remainingQuantity lineItem { id sku } } }
} } } }"""

FULFILLMENT_MUTATION = """
mutation($fulfillment: FulfillmentInput!, $key: String!) {
  fulfillmentCreate(fulfillment: $fulfillment) @idempotent(key: $key) {
    userErrors { field message }
  }
}"""

WEBHOOK_TOPICS = {
    "orders/create": "ORDERS_CREATE",
    "orders/updated": "ORDERS_UPDATED",
    "orders/cancelled": "ORDERS_CANCELLED",
    "refunds/create": "REFUNDS_CREATE",
}


def _amount(money_set):
    return float(((money_set or {}).get("presentmentMoney") or {}).get("amount") or 0.0)


def _address(node):
    if not node:
        return None
    return {
        "name": node.get("name"),
        "company": node.get("company"),
        "street": node.get("address1"),
        "street2": node.get("address2"),
        "city": node.get("city"),
        "zip": node.get("zip"),
        "state_code": node.get("provinceCode"),
        "country_code": node.get("countryCodeV2"),
        "phone": node.get("phone"),
    }


def _taxes(tax_lines):
    return [{"rate": float(t.get("rate") or 0) * 100, "title": t.get("title")} for t in tax_lines or []]


def normalize_order(node):
    """Shopify GraphQL order -> the connector-neutral order dict used by the base module."""
    status = node.get("displayFinancialStatus")
    lines = []
    for line in node["lineItems"]["nodes"]:
        variant = line.get("variant") or {}
        price = _amount(line.get("originalUnitPriceSet"))
        lines.append(
            {
                "external_id": line["id"].rsplit("/", 1)[-1],
                "name": line.get("name"),
                "sku": line.get("sku"),
                "barcode": variant.get("barcode"),
                "variant_id": variant.get("legacyResourceId"),
                "inventory_ref": (variant.get("inventoryItem") or {}).get("legacyResourceId"),
                "qty": line.get("quantity"),
                "price": price,
                "discounted_price": _amount(line.get("discountedUnitPriceSet")),
                "taxes": _taxes(line.get("taxLines")),
            }
        )
    return {
        "external_id": node["legacyResourceId"],
        "graphql_id": node["id"],
        "name": node["name"],
        "date": node["createdAt"],
        "updated_at": node["updatedAt"],
        "cancelled": bool(node.get("cancelledAt")),
        "currency": node.get("presentmentCurrencyCode"),
        "email": node.get("email"),
        "phone": node.get("phone"),
        "note": node.get("note"),
        "customer_id": (node.get("customer") or {}).get("legacyResourceId"),
        "billing": _address(node.get("billingAddress")),
        "shipping": _address(node.get("shippingAddress")),
        "taxes_included": bool(node.get("taxesIncluded")),
        "gateways": node.get("paymentGatewayNames") or [],
        "paid": status in ("PAID", "PARTIALLY_REFUNDED", "REFUNDED"),
        "fulfilled": node.get("displayFulfillmentStatus") == "FULFILLED",
        "lines": lines,
        "shipping_lines": [
            {
                "title": s.get("title"),
                "price": _amount(s.get("discountedPriceSet")),
                "taxes": _taxes(s.get("taxLines")),
            }
            for s in node["shippingLines"]["nodes"]
        ],
        "refunds": [
            {
                "external_id": r["legacyResourceId"],
                "amount": _amount(r.get("totalRefundedSet")),
                "lines": [
                    {
                        "line_id": (item.get("lineItem") or {}).get("id", "").rsplit("/", 1)[-1],
                        "sku": (item.get("lineItem") or {}).get("sku"),
                        "qty": item["quantity"],
                    }
                    for item in r["refundLineItems"]["nodes"]
                ],
            }
            for r in node.get("refunds") or []
        ],
    }


def normalize_product(node):
    image = (((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get("url")
    return {
        "external_id": node["legacyResourceId"],
        "title": node["title"],
        "description": node.get("descriptionHtml"),
        "product_type": node.get("productType"),
        "updated_at": node.get("updatedAt"),
        "image_url": image,
        "options": [o["name"] for o in sorted(node.get("options") or [], key=lambda o: o["position"])]
        if len(node["variants"]["nodes"]) > 1
        else [],
        "variants": [
            {
                "external_id": v["legacyResourceId"],
                "inventory_ref": (v.get("inventoryItem") or {}).get("legacyResourceId"),
                "sku": v.get("sku"),
                "barcode": v.get("barcode"),
                "price": float(v.get("price") or 0),
                "options": [o["value"] for o in v.get("selectedOptions") or []],
            }
            for v in node["variants"]["nodes"]
        ],
    }


class ChannelStore(models.Model):
    _inherit = "channel.store"

    channel_type = fields.Selection(selection_add=[("shopify", "Shopify")], ondelete={"shopify": "cascade"})

    def _shopify(self):
        return ShopifyClient(self)

    @api.model
    def _credentials_help(self, channel_type):
        if channel_type != "shopify":
            return super()._credentials_help(channel_type)
        return _(
            "<p>In the Shopify <b>Dev Dashboard</b> create an app, give it the scopes "
            "<code>read_orders, read_all_orders, read_products, read_customers, read_inventory, write_inventory, "
            "read_locations, read_merchant_managed_fulfillment_orders, write_merchant_managed_fulfillment_orders</code>, "
            "install it on your shop, then copy the <b>Client ID</b> and <b>Client secret</b>. "
            "Using an older custom app? Paste its Admin API access token instead.</p>"
        )

    # -- connection ---------------------------------------------------------------

    def _shopify_test_connection(self):
        self.ensure_one()
        data = self._shopify().graphql(SHOP_QUERY)
        self.shop_url = normalize_shop(self.shop_url)
        self._sync_locations(
            [
                {"external_id": str(n["legacyResourceId"]), "name": n["name"]}
                for n in data["locations"]["nodes"]
                if n.get("isActive")
            ]
        )
        return data["shop"]["name"]

    # -- webhooks -----------------------------------------------------------------

    def _shopify_register_webhooks(self):
        self.ensure_one()
        client = self._shopify()
        if not self.webhook_url.startswith("https://"):
            raise ChannelError(
                _("Shopify only delivers webhooks to https addresses. Orders will still be imported on a schedule.")
            )
        for topic in WEBHOOK_TOPICS.values():
            try:
                client.mutate_webhook(topic, self.webhook_url)
            except ChannelError as exc:
                if "already" in str(exc).lower():
                    continue
                raise

    def _shopify_verify_webhook(self, body, headers):
        secret = (self.sudo().api_secret or "").encode()
        received = headers.get("X-Shopify-Hmac-Sha256", "")
        if not secret or not received:
            return False
        digest = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest()).decode()
        return hmac.compare_digest(digest, received)

    def _shopify_parse_webhook(self, body, headers):
        topic = headers.get("X-Shopify-Topic", "")
        payload = json.loads(body or b"{}")
        if topic in ("orders/create", "orders/updated", "orders/cancelled"):
            order_id = payload.get("admin_graphql_api_id")
            name = payload.get("name") or payload.get("id")
            updated = payload.get("updated_at")
        elif topic == "refunds/create":
            order_id = f"gid://shopify/Order/{payload.get('order_id')}"
            name = _("refund on order %s", payload.get("order_id"))
            updated = payload.get("created_at")
        else:
            return []
        return [
            (
                "import_order",
                _("Import order %s", name),
                {"external_id": order_id},
                f"order:{order_id}:{updated}",
            )
        ]

    # -- orders & products ----------------------------------------------------------

    def _shopify_fetch_orders(self, since):
        self.ensure_one()
        since_iso = fields.Datetime.to_datetime(since).strftime("%Y-%m-%dT%H:%M:%SZ")
        for node in self._shopify().paginate(
            ORDERS_QUERY, "orders", {"query": f"updated_at:>='{since_iso}'"}, first=10
        ):
            yield normalize_order(node)

    def _shopify_fetch_order(self, external_id):
        node = self._shopify().graphql(ORDER_QUERY, {"id": external_id})["order"]
        return normalize_order(node) if node else None

    def _shopify_fetch_products(self):
        for node in self._shopify().paginate(PRODUCTS_QUERY, "products", first=25):
            yield normalize_product(node)

    # -- stock ----------------------------------------------------------------------

    def _shopify_push_stock_levels(self, items):
        client = self._shopify()
        quantities = [
            {
                "inventoryItemId": f"gid://shopify/InventoryItem/{item['inventory_ref']}",
                "locationId": f"gid://shopify/Location/{item['location']}",
                "quantity": item["qty"],
                "changeFromQuantity": None,  # None = overwrite whatever Shopify holds
            }
            for item in items
        ]
        client.mutate(
            STOCK_MUTATION,
            {"input": {"name": "available", "reason": "correction", "quantities": quantities}},
            "inventorySetQuantities",
        )

    # -- shipments ------------------------------------------------------------------

    def _shopify_push_fulfillment(self, picking):
        order = picking.sale_id
        client = self._shopify()
        graphql_id = f"gid://shopify/Order/{order.channel_order_id}"
        fulfillment_orders = client.graphql(FULFILLMENT_ORDERS_QUERY, {"id": graphql_id})["order"][
            "fulfillmentOrders"
        ]["nodes"]
        shipped = {}
        for move in picking.move_line_ids:
            sku = move.product_id.default_code
            shipped[sku] = shipped.get(sku, 0) + move.quantity
        by_order = []
        for fulfillment_order in fulfillment_orders:
            if fulfillment_order["status"] not in ("OPEN", "IN_PROGRESS"):
                continue
            items = []
            for item in fulfillment_order["lineItems"]["nodes"]:
                sku = item["lineItem"].get("sku")
                qty = min(int(shipped.get(sku, 0)), item["remainingQuantity"])
                if qty > 0:
                    shipped[sku] -= qty
                    items.append({"id": item["id"], "quantity": qty})
            if items:
                by_order.append({"fulfillmentOrderId": fulfillment_order["id"], "fulfillmentOrderLineItems": items})
        if not by_order:
            raise ChannelError(
                _(
                    "Nothing left to ship on Shopify for order %s: it may already be fulfilled there. "
                    "Use 'Mark as done' on this job if that is expected.",
                    order.channel_order_name,
                )
            )
        fulfillment = {"lineItemsByFulfillmentOrder": by_order, "notifyCustomer": True}
        tracking = getattr(picking, "carrier_tracking_ref", False)
        if tracking:
            fulfillment["trackingInfo"] = {
                "number": tracking,
                "company": picking.carrier_id.name if getattr(picking, "carrier_id", False) else None,
            }
        client.mutate(FULFILLMENT_MUTATION, {"fulfillment": fulfillment}, "fulfillmentCreate")
