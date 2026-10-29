from odoo import _, api, fields, models

from .ebay_client import EbayClient

ORDERS = "/sell/fulfillment/v1/order"
INVENTORY = "/sell/inventory/v1"
STOCK_CHUNK = 25
CARRIER_CODES = {
    "ups": "UPS",
    "usps": "USPS",
    "fedex": "FedEx",
    "dhl": "DHL",
    "dpd": "DPD",
    "royal mail": "RoyalMail",
    "hermes": "Hermes",
}


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _amount(node):
    return _float((node or {}).get("value"))


def _address(contact, name=None, company=None):
    if not contact:
        return None
    address = contact.get("contactAddress") or {}
    return {
        "name": name or contact.get("fullName"),
        "company": company or contact.get("companyName") or None,
        "street": address.get("addressLine1"),
        "street2": address.get("addressLine2") or None,
        "city": address.get("city"),
        "zip": address.get("postalCode"),
        "state_code": address.get("stateOrProvince"),
        "country_code": address.get("countryCode"),
        "phone": (contact.get("primaryPhone") or {}).get("phoneNumber"),
    }


def _rate(tax, net):
    return round(tax / net * 100, 2) if tax > 0 and net > 0 else 0.0


def _refunds(order):
    """Refunds from the payment summary; by line when eBay refunded whole lines, else by amount."""
    by_id = {}
    for line in order.get("lineItems") or []:
        total = _amount(line.get("total"))
        for refund in line.get("refunds") or []:
            by_id.setdefault(refund.get("refundId"), []).append(
                (line, _amount(refund.get("amount")), _amount(refund.get("amount")) >= total * 0.99 > 0)
            )
    result = []
    for refund in (order.get("paymentSummary") or {}).get("refunds") or []:
        if refund.get("refundStatus") not in (None, "REFUNDED"):
            continue
        parts = by_id.get(refund.get("refundId")) or []
        whole = parts and all(full for _line, _amount_, full in parts)
        result.append(
            {
                "external_id": str(refund.get("refundId") or refund.get("refundReferenceId")),
                "amount": _amount(refund.get("amount")),
                "lines": [{"line_id": line["lineItemId"], "qty": _float(line.get("quantity"))} for line, _a, _f in parts]
                if whole
                else [],
            }
        )
    return result


def normalize_order(order):
    """eBay Fulfillment order -> the connector-neutral order dict used by the base module.

    eBay shows VAT inside the price in some countries and adds sales tax on top in others. The order totals tell which:
    if total = items + delivery + discounts + tax the prices are tax-excluded, otherwise tax is inside them.
    Tax that eBay collects and remits itself never reaches the seller's books, so it is left out."""
    pricing = order.get("pricingSummary") or {}
    subtotal, delivery = _amount(pricing.get("priceSubtotal")), _amount(pricing.get("deliveryCost"))
    price_discount, delivery_discount = _amount(pricing.get("priceDiscount")), _amount(pricing.get("deliveryDiscount"))
    adjustment, fee, tax = _amount(pricing.get("adjustment")), _amount(pricing.get("fee")), _amount(pricing.get("tax"))
    total = _amount(pricing.get("total"))
    without_tax = subtotal - abs(price_discount) + delivery - abs(delivery_discount) + adjustment + fee
    collected_by_ebay = bool(order.get("ebayCollectAndRemitTax"))
    seller_tax = 0.0 if collected_by_ebay else tax
    included = bool(seller_tax) and abs(total - without_tax) < 0.02
    lines = []
    line_tax_total = 0.0
    for item in order.get("lineItems") or []:
        qty = _float(item.get("quantity")) or 1.0
        cost = _amount(item.get("lineItemCost"))
        after = _amount(item.get("discountedLineItemCost")) or cost
        line_tax = 0.0 if collected_by_ebay else sum(_amount(t.get("amount")) for t in item.get("taxes") or [])
        line_tax_total += line_tax
        rate = _rate(line_tax, after - line_tax if included else after)
        sku = item.get("sku") or None
        legacy = item.get("legacyItemId")
        lines.append(
            {
                "external_id": item["lineItemId"],
                "name": item.get("title"),
                "sku": sku,
                "variant_id": sku or (f"L{legacy}-{item.get('legacyVariationId') or 0}" if legacy else None),
                "inventory_ref": sku,
                "qty": qty,
                "price": cost / qty,
                "discounted_price": after / qty,
                "taxes": [{"rate": rate}] if rate else [],
            }
        )
    shipping_rate = lines[0]["taxes"][0]["rate"] if lines and lines[0]["taxes"] else 0.0
    steps = ((order.get("fulfillmentStartInstructions") or [{}])[0]).get("shippingStep") or {}
    ship_to = steps.get("shipTo") or {}
    buyer = order.get("buyer") or {}
    registration = buyer.get("buyerRegistrationAddress")
    payments = [p.get("paymentMethod") for p in (order.get("paymentSummary") or {}).get("payments") or []]
    shipping_lines = (
        [
            {
                "title": steps.get("shippingServiceCode") or "Shipping",
                "price": delivery,
                "taxes": [{"rate": shipping_rate}] if shipping_rate else [],
            }
        ]
        if delivery
        else []
    )
    # line prices already carry the item promotions; only what is left over is an order level discount
    residual = abs(price_discount) - sum(l["price"] * l["qty"] - l["discounted_price"] * l["qty"] for l in lines)
    discounts = [
        {"title": title, "price": value, "taxes": [{"rate": shipping_rate}] if shipping_rate else []}
        for title, value in (("Discount", residual), ("Shipping discount", abs(delivery_discount)))
        if value > 0.01
    ]
    status = order.get("orderPaymentStatus")
    return {
        "external_id": order["orderId"],
        "name": order["orderId"],
        "date": order.get("creationDate"),
        "updated_at": order.get("lastModifiedDate"),
        "cancelled": (order.get("cancelStatus") or {}).get("cancelState") == "CANCELED",
        "currency": (pricing.get("total") or {}).get("currency"),
        "email": ship_to.get("email") or (registration or {}).get("email"),
        "phone": (ship_to.get("primaryPhone") or {}).get("phoneNumber"),
        "note": order.get("buyerCheckoutNotes"),
        "customer_id": buyer.get("username"),
        "billing": _address(registration or ship_to, name=(registration or ship_to).get("fullName") or buyer.get("username")),
        "shipping": _address(ship_to),
        "taxes_included": included,
        "gateways": ["eBay", *[p for p in payments if p]],
        "paid": status in ("PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"),
        "fulfilled": order.get("orderFulfillmentStatus") == "FULFILLED",
        "lines": lines,
        "shipping_lines": shipping_lines,
        "discount_lines": discounts,
        "refunds": _refunds(order),
    }


def normalize_inventory_item(item):
    product = item.get("product") or {}
    sku = item["sku"]
    return {
        "external_id": sku,
        "title": product.get("title") or sku,
        "description": product.get("description"),
        "updated_at": None,
        "image_url": (product.get("imageUrls") or [None])[0],
        "options": [],
        "variants": [
            {
                "external_id": sku,
                "inventory_ref": sku,
                "sku": sku,
                "barcode": (product.get("ean") or product.get("upc") or [None])[0],
                "price": 0.0,
                "options": [],
            }
        ],
    }


class ChannelStore(models.Model):
    _inherit = "channel.store"

    channel_type = fields.Selection(selection_add=[("ebay", "eBay")], ondelete={"ebay": "cascade"})
    ebay_refresh_token = fields.Char(
        string="Refresh token", groups="channel_connector_base.group_channel_manager", copy=False
    )
    ebay_sandbox = fields.Boolean(string="Sandbox", help="Use the eBay sandbox instead of the live site.")

    def _ebay(self):
        return EbayClient(self)

    @api.model
    def _credentials_help(self, channel_type):
        if channel_type != "ebay":
            return super()._credentials_help(channel_type)
        return _(
            "<p>On <b>developer.ebay.com</b> create a keyset (<b>Application Keys</b>) and copy the <b>App ID</b> and "
            "<b>Cert ID</b>. Then use <b>User Tokens</b> to sign in as the seller with the scopes <b>sell.fulfillment</b> "
            "and <b>sell.inventory</b> and copy the <b>Refresh token</b> (valid for 18 months).</p>"
        )

    # -- connection ---------------------------------------------------------------

    def _ebay_test_connection(self):
        self.ensure_one()
        body = self._ebay().request("GET", ORDERS, {"limit": 1})  # fails with a readable error on bad credentials
        # eBay sells stock from the seller's inventory: one pool, so one location
        self._sync_locations([{"external_id": "0", "name": _("eBay inventory")}])
        seller = next((o.get("sellerId") for o in body.get("orders") or [] if o.get("sellerId")), None)
        return seller or self.name

    # -- orders -------------------------------------------------------------------------

    def _ebay_fetch_orders(self, since):
        self.ensure_one()
        stamp = fields.Datetime.to_datetime(since).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        for order in self._ebay().paginate(ORDERS, "orders", {"filter": f"lastmodifieddate:[{stamp}]"}, limit=200):
            yield normalize_order(order)

    def _ebay_fetch_order(self, external_id):
        return normalize_order(self._ebay().request("GET", f"{ORDERS}/{external_id}"))

    # -- inventory ----------------------------------------------------------------------

    def _ebay_fetch_products(self):
        self.ensure_one()
        for item in self._ebay().paginate(f"{INVENTORY}/inventory_item", "inventoryItems"):
            if item.get("sku"):
                yield normalize_inventory_item(item)

    def _ebay_push_stock_levels(self, items):
        client = self._ebay()
        refused = []
        for start in range(0, len(items), STOCK_CHUNK):
            chunk = items[start : start + STOCK_CHUNK]
            body = client.request(
                "POST",
                f"{INVENTORY}/bulk_update_price_quantity",
                json={
                    "requests": [
                        {"sku": item["inventory_ref"], "shipToLocationAvailability": {"quantity": int(item["qty"])}}
                        for item in chunk
                    ]
                },
            )
            failed = {r.get("sku") for r in body.get("responses") or [] if r.get("statusCode", 200) >= 300}
            refused += [item["mapping_id"] for item in chunk if item["inventory_ref"] in failed]
        return refused

    # -- shipments ----------------------------------------------------------------------

    def _ebay_push_fulfillment(self, picking):
        """One shipping fulfillment per delivery, with the tracking number when there is one."""
        order = picking.sale_id
        quantities = {}
        for move in picking.move_ids.filtered(lambda m: m.state == "done"):
            line_id = move.sale_line_id.channel_line_id
            if line_id:
                quantities[line_id] = quantities.get(line_id, 0) + int(move.quantity)
        if not quantities:
            return
        shipped = picking.date_done or fields.Datetime.now()
        body = {
            "lineItems": [{"lineItemId": line, "quantity": qty} for line, qty in quantities.items()],
            "shippedDate": shipped.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
        if picking.carrier_tracking_ref:
            carrier = (picking.carrier_id.name or "").strip()
            body["trackingNumber"] = picking.carrier_tracking_ref
            body["shippingCarrierCode"] = CARRIER_CODES.get(carrier.lower(), "Other")
        self._ebay().request("POST", f"{ORDERS}/{order.channel_order_id}/shipping_fulfillment", json=body)
