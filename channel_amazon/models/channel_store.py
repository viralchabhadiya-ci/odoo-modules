from odoo import _, api, fields, models
from odoo.addons.channel_connector_base.models.channel_error import ChannelError

from .amazon_client import AmazonClient

REGIONS = [
    ("eu", "Europe, UAE, India, Saudi Arabia, Turkey, Egypt, South Africa"),
    ("na", "North America and Brazil"),
    ("fe", "Far East: Japan, Singapore, Australia"),
]
OPEN_STATUSES = "UNSHIPPED,PARTIALLY_SHIPPED,SHIPPED,CANCELLED,UNFULFILLABLE"
CANCELLED = ("CANCELLED", "UNFULFILLABLE")
ORDER_DATA = "BUYER,RECIPIENT,PROCEEDS,FULFILLMENT,PAYMENT,CANCELLATION"
MERCHANT_CHANNEL = "DEFAULT"
# Carrier codes Amazon documents; anything else is sent as "Other" with the carrier's name
CARRIER_CODES = {"ups": "UPS", "usps": "USPS", "fedex": "FedEx", "dhl": "DHL"}
EXTRA_CHARGES = (
    ("SHIPPING", "Shipping"),
    ("GIFT_WRAP", "Gift wrap"),
    ("COD_FEE", "Cash on delivery fee"),
)


def _float(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(node):
    return _float((node or {}).get("amount"))


def _breakdown_total(breakdowns, kind):
    return sum(_money(b.get("subtotal")) for b in breakdowns if b.get("type") == kind)


def _tax_total(breakdowns, subtype):
    """Tax Amazon collected on one part of an item (ITEM, SHIPPING, GIFT_WRAP...)."""
    total = 0.0
    for breakdown in breakdowns:
        if breakdown.get("type") != "TAX":
            continue
        details = breakdown.get("detailedBreakdowns")
        if details:
            total += sum(_money(d.get("value")) for d in details if d.get("subtype") == subtype)
        elif subtype == "ITEM":
            total += _money(breakdown.get("subtotal"))
    return total


def _rate(tax, net):
    return round(tax / net * 100, 2) if tax > 0 and net > 0 else 0.0


def _address(address, name=None, company=None):
    if not address:
        return None
    return {
        "name": name or address.get("name"),
        "company": company or address.get("companyName") or None,
        "street": address.get("addressLine1"),
        "street2": " ".join(filter(None, [address.get("addressLine2"), address.get("addressLine3")])) or None,
        "city": address.get("city"),
        "zip": address.get("postalCode"),
        "state_code": address.get("stateOrRegion"),
        "country_code": address.get("countryCode"),
        "phone": address.get("phone"),
    }


def normalize_order(order):
    """Orders API v2026-01-01 order -> the connector-neutral order dict used by the base module.

    Amazon says an item's ITEM amount excludes tax when the tax is listed as its own breakdown.
    Prices are therefore sent without tax and the rate is worked out from the tax breakdown;
    when Amazon lists no tax the amount is passed on as it is."""
    fulfillment = order.get("fulfillment") or {}
    status = fulfillment.get("fulfillmentStatus")
    by_amazon = fulfillment.get("fulfilledBy") == "AMAZON"
    lines, extras = [], {kind: [0.0, 0.0] for kind, _title in EXTRA_CHARGES}
    skipped = 0
    for item in order.get("orderItems") or []:
        if (item.get("cancellation") or {}).get("cancellationExecution"):
            skipped += 1
            continue
        product = item.get("product") or {}
        qty = _float(item.get("quantityOrdered")) or 1.0
        breakdowns = (item.get("proceeds") or {}).get("breakdowns") or []
        total = _breakdown_total(breakdowns, "ITEM") or _money((product.get("price") or {}).get("unitPrice")) * qty
        discount = abs(_breakdown_total(breakdowns, "DISCOUNT"))
        rate = _rate(_tax_total(breakdowns, "ITEM"), total - discount)
        sku = product.get("sellerSku")
        lines.append(
            {
                "external_id": item.get("orderItemId"),
                "name": product.get("title"),
                "sku": sku,
                "variant_id": sku,
                "inventory_ref": sku,
                "qty": qty,
                "price": total / qty,
                "discounted_price": (total - discount) / qty,
                "taxes": [{"rate": rate}] if rate else [],
                # stock held by Amazon must never be overwritten with Odoo's quantity
                "push_stock": not by_amazon,
            }
        )
        for kind, _title in EXTRA_CHARGES:
            extras[kind][0] += _breakdown_total(breakdowns, kind)
            extras[kind][1] += _tax_total(breakdowns, kind)
    buyer = (order.get("buyer") or {})
    recipient = ((order.get("recipient") or {}).get("deliveryAddress")) or {}
    buyer_name = buyer.get("buyerName") or recipient.get("name")
    payments = (order.get("payment") or {}).get("paymentExecutions") or []
    methods = [p["paymentMethod"] for p in payments if p.get("paymentMethod")]
    shipping_address = _address(recipient)
    return {
        "external_id": order["orderId"],
        "name": order["orderId"],
        "marketplace_id": (order.get("salesChannel") or {}).get("marketplaceId"),
        "date": order.get("createdTime"),
        "updated_at": order.get("lastUpdatedTime"),
        "cancelled": status in CANCELLED or bool(skipped and not lines),
        "currency": ((order.get("proceeds") or {}).get("grandTotal") or {}).get("currencyCode"),
        "email": buyer.get("buyerEmail"),
        "phone": recipient.get("phone"),
        "billing": _address(recipient, name=buyer_name, company=buyer.get("buyerCompanyName")),
        "shipping": shipping_address,
        "taxes_included": False,
        "gateways": ["Amazon", *methods],
        "paid": "COD" not in {m.upper() for m in methods},
        "fulfilled": status == "SHIPPED",
        "channel_fulfilled": by_amazon,
        "lines": lines,
        "shipping_lines": [
            {
                "title": title,
                "price": extras[kind][0],
                "taxes": [{"rate": _rate(extras[kind][1], extras[kind][0])}]
                if _rate(extras[kind][1], extras[kind][0])
                else [],
            }
            for kind, title in EXTRA_CHARGES
            if extras[kind][0]
        ],
        "refunds": [],
    }


def normalize_listing(item):
    """Listings Items entry -> neutral product dict (one product, one variant: the SKU)."""
    summary = (item.get("summaries") or [{}])[0]
    codes = {a.get("fulfillmentChannelCode") for a in item.get("fulfillmentAvailability") or []}
    # a SKU that Amazon stores and ships (FBA) must not receive Odoo's stock
    amazon_only = bool(codes) and MERCHANT_CHANNEL not in codes
    offer = next((o for o in item.get("offers") or [] if o.get("offerType") in (None, "B2C")), {})
    sku = item["sku"]
    product_type = summary.get("productType")
    return {
        "external_id": sku,
        "title": summary.get("itemName") or sku,
        "product_type": product_type.replace("_", " ").title() if product_type else None,
        "updated_at": summary.get("lastUpdatedDate"),
        "image_url": (summary.get("mainImage") or {}).get("link"),
        "options": [],
        "variants": [
            {
                "external_id": sku,
                "inventory_ref": sku,
                "sku": sku,
                "price": _money(offer.get("price")),
                "options": [],
                "push_stock": not amazon_only,
            }
        ],
    }


class ChannelStore(models.Model):
    _inherit = "channel.store"

    channel_type = fields.Selection(selection_add=[("amazon", "Amazon")], ondelete={"amazon": "cascade"})
    amazon_region = fields.Selection(REGIONS, string="Amazon region", default="eu")
    amazon_seller_id = fields.Char(string="Seller ID")
    amazon_refresh_token = fields.Char(
        string="Refresh token", groups="channel_connector_base.group_channel_manager", copy=False
    )
    amazon_marketplace_ids = fields.Char(
        string="Marketplace IDs",
        help="Comma separated. Filled in when you test the connection; remove the ones you do not want to sync.",
    )
    amazon_marketplace_names = fields.Char(string="Marketplaces", readonly=True, copy=False)

    def _amazon(self):
        return AmazonClient(self)

    def _amazon_marketplaces(self):
        return [m.strip() for m in (self.amazon_marketplace_ids or "").split(",") if m.strip()]

    @api.model
    def _credentials_help(self, channel_type):
        if channel_type != "amazon":
            return super()._credentials_help(channel_type)
        return _(
            "<p>In <b>Seller Central &gt; Apps and Services &gt; Develop Apps</b> register a private developer "
            "profile and add an app (no AWS account is needed). Tick the roles <b>Inventory and Order Tracking</b>, "
            "<b>Product Listing</b> and <b>Direct-to-Consumer Shipping (Restricted)</b>. Copy the app's "
            "<b>Client ID</b> and <b>Client secret</b>, then choose <b>Authorize app</b> to get the "
            "<b>Refresh token</b>. Your <b>Seller ID</b> is under Account Info &gt; Your Merchant Token.</p>"
        )

    # -- connection ---------------------------------------------------------------

    def _amazon_test_connection(self):
        self.ensure_one()
        if not self.amazon_seller_id:
            raise ChannelError(_("Enter your Seller ID (Seller Central > Account Info > Your Merchant Token)."))
        body = self._amazon().request("GET", "/sellers/v1/marketplaceParticipations")
        active = [
            row
            for row in body.get("payload") or []
            if (row.get("participation") or {}).get("isParticipating")
        ]
        if not active:
            raise ChannelError(
                _("This Amazon account does not sell in any marketplace of the chosen region. Check the region.")
            )
        values = {
            "amazon_marketplace_names": ", ".join(
                (row.get("marketplace") or {}).get("name") or "?" for row in active
            ),
            "shop_url": (active[0].get("marketplace") or {}).get("domainName") or self.shop_url,
        }
        if not self.amazon_marketplace_ids:
            values["amazon_marketplace_ids"] = ",".join(
                (row.get("marketplace") or {}).get("id") for row in active if (row.get("marketplace") or {}).get("id")
            )
        self.write(values)
        # merchant-fulfilled stock is one pool per SKU, so one location is enough
        self._sync_locations([{"external_id": MERCHANT_CHANNEL, "name": _("Amazon merchant-fulfilled stock")}])
        return active[0].get("storeName") or self.name

    # -- orders -------------------------------------------------------------------------

    def _amazon_fetch_orders(self, since):
        self.ensure_one()
        if not self._amazon_marketplaces():
            raise ChannelError(_("Test the connection first: no Amazon marketplace is selected yet."))
        params = {
            "lastUpdatedAfter": fields.Datetime.to_datetime(since).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fulfillmentStatuses": OPEN_STATUSES,
            "marketplaceIds": ",".join(self._amazon_marketplaces()),
            "includedData": ORDER_DATA,
            "maxResultsPerPage": 100,
        }
        for order in self._amazon().paginate_orders(params):
            yield self._amazon_order(order)

    def _amazon_fetch_order(self, external_id):
        body = self._amazon().request(
            "GET", f"/orders/2026-01-01/orders/{external_id}", params={"includedData": ORDER_DATA}
        )
        return self._amazon_order(body.get("order") or body)

    def _amazon_order(self, order):
        data = normalize_order(order)
        for node in [*data["lines"], *data["shipping_lines"]]:
            node["taxes"] = [{"rate": self._amazon_snap_rate(t["rate"])} for t in node["taxes"]]
        return data

    def _amazon_snap_rate(self, rate):
        """Amazon only gives tax amounts. Use the Odoo sale tax the amounts add up to when there is one."""
        taxes = self.env["account.tax"].search(
            [
                ("type_tax_use", "=", "sale"),
                ("amount_type", "=", "percent"),
                ("company_id", "=", self.company_id.id),
            ]
        )
        closest = min(taxes.mapped("amount"), key=lambda amount: abs(amount - rate), default=None)
        if closest is not None and abs(closest - rate) <= 0.3:
            return closest
        return round(rate, 1)

    def _amazon_order_vals(self, data):
        return {"amazon_marketplace_id": data.get("marketplace_id")}

    # -- listings -----------------------------------------------------------------------

    def _amazon_fetch_products(self):
        self.ensure_one()
        params = {
            "marketplaceIds": ",".join(self._amazon_marketplaces()),
            "includedData": "summaries,fulfillmentAvailability,offers",
            "pageSize": 20,
        }
        seen = set()
        for item in self._amazon().paginate_listings(self.amazon_seller_id, params):
            if item.get("sku") and item["sku"] not in seen:  # the same SKU is listed once per marketplace
                seen.add(item["sku"])
                yield normalize_listing(item)

    # -- stock --------------------------------------------------------------------------

    def _amazon_push_stock_levels(self, items):
        client = self._amazon()
        refused = []
        marketplaces = ",".join(self._amazon_marketplaces())
        for item in items:
            body = client.request(
                "PATCH",
                f"/listings/2021-08-01/items/{self.amazon_seller_id}/{item['inventory_ref']}",
                params={"marketplaceIds": marketplaces},
                json={
                    "productType": "PRODUCT",
                    "patches": [
                        {
                            "op": "replace",
                            "path": "/attributes/fulfillment_availability",
                            "value": [{"fulfillment_channel_code": MERCHANT_CHANNEL, "quantity": item["qty"]}],
                        }
                    ],
                },
            )
            if body.get("status") == "INVALID":
                refused.append(item["mapping_id"])
        return refused

    # -- shipments ----------------------------------------------------------------------

    def _amazon_push_fulfillment(self, picking):
        """Confirm the shipment of one delivery as one Amazon package."""
        order = picking.sale_id
        quantities = {}
        for move in picking.move_ids.filtered(lambda m: m.state == "done"):
            line_id = move.sale_line_id.channel_line_id
            if line_id:
                quantities[line_id] = quantities.get(line_id, 0) + int(move.quantity)
        if not quantities:
            return
        tracking = picking.carrier_tracking_ref
        if not tracking:
            raise ChannelError(
                _(
                    "Amazon needs a tracking number to confirm the shipment. Fill in the tracking reference on "
                    "delivery %s and retry this job.",
                    picking.name,
                )
            )
        carrier = picking.carrier_id.name or ""
        code = CARRIER_CODES.get(carrier.strip().lower(), "Other")
        marketplace = order.amazon_marketplace_id or (self._amazon_marketplaces() or [False])[0]
        shipped = picking.date_done or fields.Datetime.now()
        package = {
            "packageReferenceId": str(picking.id),
            "carrierCode": code,
            "trackingNumber": tracking,
            "shipDate": shipped.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "orderItems": [{"orderItemId": line, "quantity": qty} for line, qty in quantities.items()],
        }
        if code == "Other":
            package["carrierName"] = carrier or _("Other")
        self._amazon().request(
            "POST",
            f"/orders/v0/orders/{order.channel_order_id}/shipmentConfirmation",
            json={"marketplaceId": marketplace, "packageDetail": package},
        )
