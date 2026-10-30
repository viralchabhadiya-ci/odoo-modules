import copy
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError
from odoo.tests import tagged

from ..models.amazon_client import AmazonClient
from ..models.channel_store import normalize_listing, normalize_order

REQUEST = "odoo.addons.channel_amazon.models.amazon_client.AmazonClient.request"
HTTP = "odoo.addons.channel_amazon.models.amazon_client.requests.request"
LWA = "odoo.addons.channel_amazon.models.amazon_client.requests.post"
SLEEP = "odoo.addons.channel_amazon.models.amazon_client.time.sleep"


def money(amount, currency="EUR"):
    return {"amount": str(amount), "currencyCode": currency}


ORDER = {
    "orderId": "028-1234567-7654321",
    "createdTime": "2026-10-29T10:00:00Z",
    "lastUpdatedTime": "2026-10-29T10:05:00Z",
    "salesChannel": {"marketplaceId": "A1PA6795UKMFR9", "marketplaceName": "Amazon.de"},
    "buyer": {"buyerName": "Lena Vogel", "buyerEmail": "abc123@marketplace.amazon.de", "buyerCompanyName": None},
    "recipient": {
        "deliveryAddress": {
            "name": "Lena Vogel", "addressLine1": "Hauptstr. 5", "addressLine2": "Hinterhaus", "city": "Berlin",
            "stateOrRegion": "Berlin", "postalCode": "10115", "countryCode": "DE", "phone": "+4930123",
        }
    },
    "proceeds": {"grandTotal": money("61.20")},
    "payment": {"paymentExecutions": [{"paymentMethod": "CARD", "paymentAmount": money("61.20")}]},
    "fulfillment": {"fulfillmentStatus": "UNSHIPPED", "fulfilledBy": "MERCHANT"},
    "orderItems": [
        {
            "orderItemId": "12345678901234",
            "quantityOrdered": 2,
            "product": {"asin": "B000TEST01", "title": "Steel bottle 500ml", "sellerSku": "BOT-500"},
            "proceeds": {
                "proceedsTotal": money("49.20"),
                "breakdowns": [
                    {"type": "ITEM", "subtotal": money("40.00")},
                    {"type": "DISCOUNT", "subtotal": money("-4.00")},
                    {"type": "SHIPPING", "subtotal": money("5.00")},
                    {
                        "type": "TAX",
                        "subtotal": money("8.20"),
                        "detailedBreakdowns": [
                            {"subtype": "ITEM", "value": money("7.20")},
                            {"subtype": "SHIPPING", "value": money("1.00")},
                        ],
                    },
                ],
            },
        },
        {
            "orderItemId": "12345678901235",
            "quantityOrdered": 1,
            "product": {"asin": "B000TEST02", "title": "Lid", "sellerSku": "LID-1"},
            "proceeds": {
                "proceedsTotal": money("12.00"),
                "breakdowns": [
                    {"type": "ITEM", "subtotal": money("10.00")},
                    {"type": "TAX", "subtotal": money("2.00"), "detailedBreakdowns": [{"subtype": "ITEM", "value": money("2.00")}]},
                ],
            },
        },
    ],
}

LISTING = {
    "sku": "BOT-500",
    "summaries": [
        {
            "marketplaceId": "A1PA6795UKMFR9", "asin": "B000TEST01", "productType": "WATER_BOTTLE",
            "itemName": "Steel bottle 500ml", "lastUpdatedDate": "2026-10-20T08:00:00Z",
            "mainImage": {"link": "https://img.example/bottle.jpg"},
        }
    ],
    "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT", "quantity": 9}],
    "offers": [{"marketplaceId": "A1PA6795UKMFR9", "offerType": "B2C", "price": money("20.00")}],
}

PARTICIPATIONS = {
    "payload": [
        {
            "marketplace": {"id": "A1PA6795UKMFR9", "name": "Amazon.de", "domainName": "www.amazon.de", "countryCode": "DE"},
            "participation": {"isParticipating": True},
            "storeName": "Bottle House",
        },
        {
            "marketplace": {"id": "A1805IZSGTT6HS", "name": "Amazon.nl", "domainName": "www.amazon.nl", "countryCode": "NL"},
            "participation": {"isParticipating": False},
        },
    ]
}


@tagged("post_install", "-at_install")
class TestAmazon(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref("channel_connector_base.group_channel_manager")
        cls.company = cls.env.company
        cls.env.ref("base.EUR").active = True
        cls.env["account.tax"].create(
            {"name": "VAT 20%", "amount": 20.0, "amount_type": "percent", "type_tax_use": "sale", "company_id": cls.company.id}
        )
        bank = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.fba_warehouse = cls.env["stock.warehouse"].create({"name": "Amazon FBA", "code": "FBA"})
        cls.store = cls.env["channel.store"].create(
            {
                "name": "Amazon DE",
                "channel_type": "amazon",
                "api_key": "amzn1.application-oa2-client.x",
                "api_secret": "secret",
                "amazon_refresh_token": "Atzr|refresh",
                "amazon_seller_id": "A1SELLER",
                "amazon_region": "eu",
                "amazon_marketplace_ids": "A1PA6795UKMFR9",
                "state": "connected",
                "warehouse_id": cls.warehouse.id,
                "fulfilled_warehouse_id": cls.fba_warehouse.id,
                "import_orders_from": "2020-01-01 00:00:00",
                "workflow_ids": [(0, 0, {"name": "Paid", "payment_journal_id": bank.id})],
            }
        )
        cls.bottle = cls.env["product.product"].create(
            {"name": "Bottle", "default_code": "BOT-500", "is_storable": True, "type": "consu"}
        )
        cls.lid = cls.env["product.product"].create(
            {"name": "Lid", "default_code": "LID-1", "is_storable": True, "type": "consu"}
        )

    def _order(self, **changes):
        return {**copy.deepcopy(ORDER), **changes}

    # -- order mapping ------------------------------------------------------------------

    def test_normalize_order_takes_prices_without_tax_and_works_out_the_rates(self):
        data = normalize_order(ORDER)
        bottle, lid = data["lines"]
        self.assertEqual(bottle["external_id"], "12345678901234")
        self.assertEqual(bottle["price"], 20.0)
        self.assertEqual(bottle["discounted_price"], 18.0)
        self.assertEqual(bottle["taxes"], [{"rate": 20.0}])
        self.assertEqual(lid["taxes"], [{"rate": 20.0}])
        self.assertEqual(data["shipping_lines"], [{"title": "Shipping", "price": 5.0, "taxes": [{"rate": 20.0}]}])
        self.assertFalse(data["taxes_included"])
        self.assertEqual(data["currency"], "EUR")
        self.assertEqual(data["marketplace_id"], "A1PA6795UKMFR9")
        self.assertEqual(data["shipping"]["street2"], "Hinterhaus")
        self.assertEqual(data["billing"]["name"], "Lena Vogel")
        self.assertTrue(data["paid"])
        self.assertFalse(data["channel_fulfilled"])
        self.assertTrue(bottle["push_stock"])

    def test_item_without_a_tax_breakdown_is_passed_on_as_it_is(self):
        order = self._order()
        order["orderItems"][1]["proceeds"]["breakdowns"] = [{"type": "ITEM", "subtotal": money("12.00")}]
        lid = normalize_order(order)["lines"][1]
        self.assertEqual(lid["price"], 12.0)
        self.assertEqual(lid["taxes"], [])

    def test_unit_price_is_used_when_proceeds_are_not_included(self):
        order = self._order()
        order["orderItems"][1]["proceeds"] = {}
        order["orderItems"][1]["product"]["price"] = {"unitPrice": money("11.00")}
        self.assertEqual(normalize_order(order)["lines"][1]["price"], 11.0)

    def test_cash_on_delivery_is_not_paid_and_cancelled_items_are_left_out(self):
        order = self._order(payment={"paymentExecutions": [{"paymentMethod": "COD"}]})
        order["orderItems"][1]["cancellation"] = {"cancellationExecution": {"cancelledBy": "MERCHANT"}}
        data = normalize_order(order)
        self.assertFalse(data["paid"])
        self.assertEqual(len(data["lines"]), 1)
        order["orderItems"][0]["cancellation"] = {"cancellationExecution": {"cancelledBy": "AMAZON"}}
        self.assertTrue(normalize_order(order)["cancelled"])
        self.assertTrue(normalize_order(self._order(fulfillment={"fulfillmentStatus": "CANCELLED"}))["cancelled"])

    def test_buyer_company_becomes_the_invoice_company(self):
        order = self._order(buyer={"buyerName": "Lena Vogel", "buyerEmail": "x@y", "buyerCompanyName": "Vogel GmbH"})
        self.assertEqual(normalize_order(order)["billing"]["company"], "Vogel GmbH")

    # -- importing orders ---------------------------------------------------------------

    def test_order_import_creates_invoice_payment_and_remembers_the_marketplace(self):
        order = self.store._process_order_data(self.store._amazon_order(ORDER))
        self.assertEqual(order.channel_order_id, "028-1234567-7654321")
        self.assertEqual(order.amazon_marketplace_id, "A1PA6795UKMFR9")
        self.assertEqual(order.state, "sale")
        bottle_line = order.order_line.filtered(lambda line: line.product_id == self.bottle)
        self.assertAlmostEqual(bottle_line.price_unit, 20.0)
        self.assertAlmostEqual(bottle_line.discount, 10.0)
        self.assertEqual(bottle_line.tax_ids.amount, 20.0)
        self.assertEqual(bottle_line.channel_line_id, "12345678901234")
        self.assertEqual(order.partner_id.country_id.code, "DE")
        self.assertEqual(order.invoice_ids.payment_state, "paid")
        self.assertAlmostEqual(order.amount_total, 61.2)
        again = self.store._process_order_data(self.store._amazon_order({**ORDER, "lastUpdatedTime": "2026-10-30T00:00:00Z"}))
        self.assertEqual(order, again)

    def test_tax_rate_snaps_to_the_odoo_tax_it_adds_up_to(self):
        self.assertEqual(self.store._closest_tax_rate(19.98), 20.0)
        self.assertEqual(self.store._closest_tax_rate(7.0), 7.0)  # no Odoo tax is close: keep Amazon's figure

    def test_orders_amazon_ships_itself_use_the_fba_warehouse_and_need_no_shipment(self):
        order = self._order(fulfillment={"fulfillmentStatus": "SHIPPED", "fulfilledBy": "AMAZON"})
        odoo_order = self.store._process_order_data(self.store._amazon_order(order))
        self.assertEqual(odoo_order.warehouse_id, self.fba_warehouse)
        self.assertEqual(odoo_order.picking_ids.mapped("state"), ["done"])
        self.assertFalse(self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment"))
        # Amazon holds that stock: Odoo must never overwrite it
        mapping = self.store.mapping_ids.filtered(lambda m: m.res_model == "product.product" and m.res_id == self.bottle.id)
        self.assertFalse(mapping.push_stock)

    def test_cancelled_order_cancels_the_odoo_order(self):
        self.store.workflow_ids.unlink()  # a quotation: nothing invoiced yet
        order = self.store._process_order_data(self.store._amazon_order(ORDER))
        self.store._process_order_data(self.store._amazon_order(self._order(fulfillment={"fulfillmentStatus": "CANCELLED"})))
        self.assertEqual(order.state, "cancel")

    def test_scheduled_import_asks_for_changed_orders_and_follows_the_pages(self):
        pages = [
            {"orders": [ORDER], "pagination": {"nextToken": "TOKEN-2"}},
            {"orders": [{**ORDER, "orderId": "028-0000000-0000001"}]},
        ]
        calls = []

        def fake(method, path, params=None, json=None, patient=True):
            calls.append((path, dict(params)))
            return pages[len(calls) - 1]

        self.store.import_orders_from = "2026-10-01 00:00:00"
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._import_orders(), 2)
        first, second = calls
        self.assertEqual(first[0], "/orders/2026-01-01/orders")
        self.assertEqual(first[1]["lastUpdatedAfter"], "2026-10-01T00:00:00Z")
        self.assertEqual(first[1]["marketplaceIds"], "A1PA6795UKMFR9")
        self.assertIn("RECIPIENT", first[1]["includedData"])
        self.assertNotIn("PENDING", first[1]["fulfillmentStatuses"])
        self.assertEqual(second[1]["paginationToken"], "TOKEN-2")

    def test_import_needs_a_marketplace(self):
        self.store.amazon_marketplace_ids = False
        with self.assertRaisesRegex(ChannelError, "Test the connection"):
            list(self.store._fetch_orders(fields.Datetime.now()))

    def test_single_order_fetch_for_retries(self):
        with patch(REQUEST, return_value={"order": ORDER}) as request:
            data = self.store._fetch_order("028-1234567-7654321")
        self.assertEqual(data["external_id"], "028-1234567-7654321")
        self.assertEqual(request.call_args.args[:2], ("GET", "/orders/2026-01-01/orders/028-1234567-7654321"))

    # -- connection ---------------------------------------------------------------------

    def test_connection_reads_marketplaces_and_creates_one_location(self):
        self.store.amazon_marketplace_ids = False
        with patch(REQUEST, return_value=PARTICIPATIONS):
            name = self.store._test_connection()
            self.store._test_connection()
        self.assertEqual(name, "Bottle House")
        self.assertEqual(self.store.amazon_marketplace_ids, "A1PA6795UKMFR9")
        self.assertEqual(self.store.amazon_marketplace_names, "Amazon.de")
        self.assertEqual(self.store.shop_url, "www.amazon.de")
        self.assertEqual(len(self.store.location_ids), 1)
        self.assertEqual(self.store.location_ids.warehouse_id, self.warehouse)

    def test_connection_keeps_the_marketplaces_the_user_chose(self):
        self.store.amazon_marketplace_ids = "A1805IZSGTT6HS"
        with patch(REQUEST, return_value=PARTICIPATIONS):
            self.store._test_connection()
        self.assertEqual(self.store.amazon_marketplace_ids, "A1805IZSGTT6HS")

    def test_connection_explains_missing_seller_id_and_wrong_region(self):
        with patch(REQUEST, return_value={"payload": []}):
            with self.assertRaisesRegex(ChannelError, "region"):
                self.store._test_connection()
        self.store.amazon_seller_id = False
        with self.assertRaisesRegex(ChannelError, "Seller ID"):
            self.store._test_connection()

    def test_setup_wizard_passes_the_amazon_credentials_on(self):
        wizard = self.env["channel.setup.wizard"].create(
            {
                "name": "New", "channel_type": "amazon", "api_key": "id", "api_secret": "sec",
                "amazon_region": "na", "amazon_seller_id": " A1X ", "amazon_refresh_token": " Atzr|x ",
            }
        )
        vals = wizard._prepare_store_vals()
        self.assertEqual((vals["amazon_region"], vals["amazon_seller_id"], vals["amazon_refresh_token"]), ("na", "A1X", "Atzr|x"))

    def test_amazon_has_no_webhooks(self):
        self.assertFalse(self.store.supports_webhooks)

    # -- listings -----------------------------------------------------------------------

    def test_listing_becomes_a_product_with_its_sku_and_price(self):
        data = normalize_listing(LISTING)
        self.assertEqual(data["title"], "Steel bottle 500ml")
        self.assertEqual(data["product_type"], "Water Bottle")
        self.assertEqual(data["variants"][0]["price"], 20.0)
        self.assertTrue(data["variants"][0]["push_stock"])

    def test_listing_stored_by_amazon_never_receives_odoo_stock(self):
        fba = {**LISTING, "fulfillmentAvailability": [{"fulfillmentChannelCode": "AMAZON_EU", "quantity": 40}]}
        self.assertFalse(normalize_listing(fba)["variants"][0]["push_stock"])
        both = {**LISTING, "fulfillmentAvailability": [{"fulfillmentChannelCode": "DEFAULT"}, {"fulfillmentChannelCode": "AMAZON_EU"}]}
        self.assertTrue(normalize_listing(both)["variants"][0]["push_stock"])

    def test_product_import_links_by_sku_and_pages_through_listings(self):
        fba = {**copy.deepcopy(LISTING), "sku": "LID-1", "fulfillmentAvailability": [{"fulfillmentChannelCode": "AMAZON_EU"}]}
        pages = [
            {"items": [LISTING, LISTING], "pagination": {"nextToken": "P2"}},
            {"items": [fba]},
        ]
        calls = []

        def fake(method, path, params=None, json=None, patient=True):
            calls.append((path, dict(params)))
            return pages[len(calls) - 1]

        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._import_products(), 2)  # the duplicate SKU is read once
            self.env["channel.job"]._run_due_jobs()
        self.assertEqual(calls[0][0], "/listings/2021-08-01/items/A1SELLER")
        self.assertEqual(calls[1][1]["pageToken"], "P2")
        mapping = {m.res_id: m for m in self.store.mapping_ids.filtered(lambda m: m.res_model == "product.product")}
        self.assertEqual(mapping[self.bottle.id].external_id, "BOT-500")
        self.assertTrue(mapping[self.bottle.id].push_stock)
        self.assertFalse(mapping[self.lid.id].push_stock)

    # -- stock --------------------------------------------------------------------------

    def _stock_setup(self):
        self.env["stock.quant"]._update_available_quantity(self.bottle, self.warehouse.lot_stock_id, 7)
        self.env["stock.quant"]._update_available_quantity(self.lid, self.warehouse.lot_stock_id, 3)
        self.store._set_mapping(self.bottle, "BOT-500", "BOT-500")
        self.store._set_mapping(self.lid, "LID-1", "LID-1")
        self.store._sync_locations([{"external_id": "DEFAULT", "name": "Stock"}])

    def test_stock_is_patched_per_sku_as_merchant_fulfilled_quantity(self):
        self._stock_setup()
        sent = {}

        def fake(method, path, params=None, json=None, patient=True):
            sent[path] = (method, params, json)
            return {"status": "ACCEPTED"}

        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._push_stock(), 2)
            self.env["channel.job"]._run_due_jobs()
        method, params, body = sent["/listings/2021-08-01/items/A1SELLER/BOT-500"]
        self.assertEqual(method, "PATCH")
        self.assertEqual(params, {"marketplaceIds": "A1PA6795UKMFR9"})
        self.assertEqual(
            body["patches"],
            [{"op": "replace", "path": "/attributes/fulfillment_availability", "value": [{"fulfillment_channel_code": "DEFAULT", "quantity": 7}]}],
        )
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._push_stock(), 0)  # nothing changed

    def test_refused_sku_is_reported_but_the_others_are_remembered(self):
        self._stock_setup()

        def fake(method, path, params=None, json=None, patient=True):
            return {"status": "INVALID" if path.endswith("/LID-1") else "ACCEPTED"}

        with patch(REQUEST, side_effect=fake):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
            problem = self.store.job_ids.filtered(lambda j: j.state == "failed")
            self.assertEqual(len(problem), 1)
            self.assertIn("Lid", problem.error)
            self.assertEqual(self.store._push_stock(), 1)  # only the refused SKU is offered again
            self.env["channel.job"]._run_due_jobs()
            self.assertEqual(len(self.store.job_ids.filtered(lambda j: j.state == "failed")), 1)  # same problem, one job
        with patch(REQUEST, return_value={"status": "ACCEPTED"}):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
        self.assertFalse(self.store.job_ids.filtered(lambda j: j.state == "failed"))

    # -- shipments ----------------------------------------------------------------------

    def _ship(self, order, tracking="1Z999", carrier=None):
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.carrier_tracking_ref = tracking
        if carrier:
            picking.carrier_id = carrier
        picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
        return picking, self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment")

    def test_delivery_confirms_the_shipment_with_tracking_and_order_items(self):
        order = self.store._process_order_data(self.store._amazon_order(ORDER))
        carrier = self.env["delivery.carrier"].create(
            {"name": "UPS", "delivery_type": "fixed", "product_id": self.env.ref("channel_connector_base.product_channel_shipping").id}
        )
        picking, job = self._ship(order, carrier=carrier)
        calls = []
        with patch(REQUEST, side_effect=lambda method, path, params=None, json=None, patient=True: calls.append((method, path, json)) or {}):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", "/orders/v0/orders/028-1234567-7654321/shipmentConfirmation"))
        self.assertEqual(body["marketplaceId"], "A1PA6795UKMFR9")
        package = body["packageDetail"]
        self.assertEqual(package["carrierCode"], "UPS")
        self.assertNotIn("carrierName", package)
        self.assertEqual(package["trackingNumber"], "1Z999")
        self.assertEqual(package["packageReferenceId"], str(picking.id))
        self.assertEqual(
            sorted((i["orderItemId"], i["quantity"]) for i in package["orderItems"]),
            [("12345678901234", 2), ("12345678901235", 1)],
        )

    def test_unknown_carrier_is_sent_as_other_with_its_name(self):
        order = self.store._process_order_data(self.store._amazon_order(ORDER))
        carrier = self.env["delivery.carrier"].create(
            {"name": "Local Courier", "delivery_type": "fixed", "product_id": self.env.ref("channel_connector_base.product_channel_shipping").id}
        )
        _picking, job = self._ship(order, carrier=carrier)
        calls = []
        with patch(REQUEST, side_effect=lambda method, path, params=None, json=None, patient=True: calls.append(json) or {}):
            job._run()
        self.assertEqual(calls[0]["packageDetail"]["carrierCode"], "Other")
        self.assertEqual(calls[0]["packageDetail"]["carrierName"], "Local Courier")

    def test_shipment_without_tracking_tells_what_to_fill_in(self):
        order = self.store._process_order_data(self.store._amazon_order(ORDER))
        _picking, job = self._ship(order, tracking=False)
        with patch(REQUEST, return_value={}):
            job._run()
        self.assertEqual(job.state, "pending")
        self.assertIn("tracking", job.error)

    # -- API client ---------------------------------------------------------------------

    def _http(self, status, body=None, content=True):
        response = MagicMock(status_code=status, headers={})
        response.json.return_value = body if body is not None else {}
        response.text = str(body or "")
        response.content = b"{}" if content else b""
        return response

    def _lwa(self, token="tok", expires=3600):
        return self._http(200, {"access_token": token, "expires_in": expires})

    def test_credentials_are_required(self):
        store = self.store.copy({"name": "Empty", "amazon_refresh_token": False})
        with self.assertRaisesRegex(ChannelError, "Refresh token"):
            AmazonClient(store)

    def test_access_token_is_fetched_once_and_cached(self):
        with patch(LWA, return_value=self._lwa()) as lwa, patch(HTTP, return_value=self._http(200, {"ok": 1})) as http:
            client = AmazonClient(self.store)
            client.request("GET", "/sellers/v1/marketplaceParticipations")
            client.request("GET", "/sellers/v1/marketplaceParticipations")
        self.assertEqual(lwa.call_count, 1)
        self.assertEqual(lwa.call_args.kwargs["data"]["grant_type"], "refresh_token")
        headers = http.call_args.kwargs["headers"]
        self.assertEqual(headers["x-amz-access-token"], "tok")
        self.assertIn("Language=Python", headers["user-agent"])
        self.assertEqual(http.call_args.args[1], "https://sellingpartnerapi-eu.amazon.com/sellers/v1/marketplaceParticipations")

    def test_region_selects_the_host(self):
        self.store.amazon_region = "fe"
        self.assertEqual(AmazonClient(self.store).host, "https://sellingpartnerapi-fe.amazon.com")

    def test_rejected_refresh_token_and_client_say_what_to_do(self):
        with patch(LWA, return_value=self._http(400, {"error": "invalid_grant"})):
            with self.assertRaises(ChannelAuthError) as caught:
                AmazonClient(self.store)._token(force=True)
        self.assertIn("Authorize app", str(caught.exception))
        with patch(LWA, return_value=self._http(401, {"error": "invalid_client"})):
            with self.assertRaisesRegex(ChannelAuthError, "Client ID"):
                AmazonClient(self.store)._token(force=True)

    def test_expired_token_is_replaced_once_before_giving_up(self):
        self.store.write({"access_token": "old", "token_expiry": fields.Datetime.now() + timedelta(hours=1)})
        answers = [self._http(403, {"errors": [{"message": "token expired"}]}), self._http(200, {"fine": True})]
        with patch(LWA, return_value=self._lwa("new")), patch(HTTP, side_effect=answers) as http:
            self.assertEqual(AmazonClient(self.store).request("GET", "/x"), {"fine": True})
        self.assertEqual(http.call_args.kwargs["headers"]["x-amz-access-token"], "new")

    def test_denied_request_points_to_the_app_roles(self):
        denied = self._http(403, {"errors": [{"code": "Unauthorized", "message": "Access to requested resource is denied."}]})
        with patch(LWA, return_value=self._lwa()), patch(HTTP, return_value=denied):
            with self.assertRaises(ChannelError) as caught:
                AmazonClient(self.store).request("GET", "/x")
        self.assertNotIsInstance(caught.exception, ChannelAuthError)  # a missing role must not disconnect the store
        self.assertIn("Direct-to-Consumer Shipping", str(caught.exception))
        self.assertIn("denied", str(caught.exception))

    def test_throttling_waits_for_patient_calls_but_not_for_orders(self):
        slow = self._http(429)
        with patch(LWA, return_value=self._lwa()), patch(SLEEP), patch(HTTP, side_effect=[slow, self._http(200, {"ok": 1})]):
            self.assertEqual(AmazonClient(self.store).request("GET", "/x"), {"ok": 1})
        with patch(LWA, return_value=self._lwa()), patch(SLEEP) as sleep, patch(HTTP, return_value=slow) as http:
            with self.assertRaisesRegex(ChannelError, "limiting"):
                AmazonClient(self.store).request("GET", "/orders/2026-01-01/orders", patient=False)
        self.assertEqual(http.call_count, 1)
        sleep.assert_not_called()

    def test_server_errors_are_retried_and_empty_answers_are_fine(self):
        with patch(LWA, return_value=self._lwa()), patch(SLEEP), patch(HTTP, side_effect=[self._http(503), self._http(204, content=False)]):
            self.assertEqual(AmazonClient(self.store).request("POST", "/x"), {})

    def test_sync_failure_with_a_lost_connection_keeps_the_store_connected(self):
        with patch(LWA, return_value=self._lwa()), patch(HTTP, return_value=self._http(403, {"errors": [{"message": "no"}]})):
            self.store._safe_run("import_orders", self.store._import_orders)
        self.assertEqual(self.store.state, "connected")
        self.assertEqual(self.store.job_failed_count, 1)
