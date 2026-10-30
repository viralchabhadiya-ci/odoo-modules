import copy
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError
from odoo.tests import tagged

from ..models.channel_store import normalize_inventory_item, normalize_order
from ..models.ebay_client import EbayClient

REQUEST = "odoo.addons.channel_ebay.models.ebay_client.EbayClient.request"
HTTP = "odoo.addons.channel_ebay.models.ebay_client.requests.request"
OAUTH = "odoo.addons.channel_ebay.models.ebay_client.requests.post"
SLEEP = "odoo.addons.channel_ebay.models.ebay_client.time.sleep"


def amount(value, currency="GBP"):
    return {"value": str(value), "currency": currency}


# a UK style order: VAT is inside the prices
ORDER = {
    "orderId": "12-34567-89012",
    "creationDate": "2026-10-29T10:00:00.000Z",
    "lastModifiedDate": "2026-10-29T10:05:00.000Z",
    "sellerId": "bottlehouse",
    "buyer": {
        "username": "jane_buyer",
        "buyerRegistrationAddress": {
            "fullName": "Jane Buyer", "email": "jane@members.ebay.com",
            "contactAddress": {"addressLine1": "1 High St", "city": "Leeds", "postalCode": "LS1 1AA", "countryCode": "GB"},
            "primaryPhone": {"phoneNumber": "0113"},
        },
    },
    "buyerCheckoutNotes": "Gift please",
    "cancelStatus": {"cancelState": "NONE_REQUESTED", "cancelRequests": []},
    "orderFulfillmentStatus": "NOT_STARTED",
    "orderPaymentStatus": "PAID",
    "paymentSummary": {"payments": [{"paymentMethod": "EBAY", "paymentStatus": "PAID"}], "refunds": []},
    "pricingSummary": {
        "priceSubtotal": amount("52.00"), "priceDiscount": amount("-4.00"), "deliveryCost": amount("6.00"),
        "tax": amount("8.00"), "total": amount("54.00"),
    },
    "fulfillmentStartInstructions": [
        {
            "shippingStep": {
                "shippingServiceCode": "UK_RoyalMailSecondClassStandard",
                "shipTo": {
                    "fullName": "Jane Buyer", "email": "jane@members.ebay.com", "primaryPhone": {"phoneNumber": "07700"},
                    "contactAddress": {"addressLine1": "9 Low Rd", "addressLine2": "Flat 2", "city": "York", "stateOrProvince": "",
                                       "postalCode": "YO1 1AA", "countryCode": "GB"},
                },
            }
        }
    ],
    "lineItems": [
        {"lineItemId": "L1", "sku": "MUG-1", "title": "Steel mug", "quantity": 2, "lineItemCost": amount("40.00"),
         "discountedLineItemCost": amount("36.00"), "taxes": [{"amount": amount("6.00")}], "total": amount("36.00")},
        {"lineItemId": "L2", "legacyItemId": "1234567", "title": "Old listing", "quantity": 1, "lineItemCost": amount("12.00"),
         "taxes": [{"amount": amount("2.00")}], "total": amount("12.00")},
    ],
}

INVENTORY = {
    "total": 3,
    "inventoryItems": [
        {"sku": "MUG-1", "product": {"title": "Steel mug", "description": "<p>Hot</p>", "imageUrls": ["https://i.ebayimg.com/1.jpg"], "ean": ["4006381333931"]}},
        {"sku": "LID-1", "product": {"title": "Lid"}},
        {"sku": "GONE", "product": {"title": "Gone"}},
    ],
}


@tagged("post_install", "-at_install")
class TestEbay(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref("channel_connector_base.group_channel_manager")
        cls.company = cls.env.company
        cls.env.ref("base.GBP").active = True
        cls.env["account.tax"].create(
            {"name": "VAT 20%", "amount": 20.0, "amount_type": "percent", "type_tax_use": "sale", "company_id": cls.company.id}
        )
        bank = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.store = cls.env["channel.store"].create(
            {
                "name": "eBay UK",
                "channel_type": "ebay",
                "api_key": "AppId",
                "api_secret": "CertId",
                "ebay_refresh_token": "v^1.1#refresh",
                "state": "connected",
                "warehouse_id": cls.warehouse.id,
                "import_orders_from": "2020-01-01 00:00:00",
                "workflow_ids": [(0, 0, {"name": "Paid", "payment_journal_id": bank.id})],
            }
        )
        cls.mug = cls.env["product.product"].create(
            {"name": "Mug", "default_code": "MUG-1", "is_storable": True, "type": "consu"}
        )
        cls.lid = cls.env["product.product"].create(
            {"name": "Lid", "default_code": "LID-1", "is_storable": True, "type": "consu"}
        )

    def _order(self, **changes):
        return {**copy.deepcopy(ORDER), **changes}

    # -- order mapping ------------------------------------------------------------------

    def test_vat_inside_the_price_is_detected_from_the_order_totals(self):
        data = normalize_order(ORDER)
        mug, old = data["lines"]
        self.assertTrue(data["taxes_included"])
        self.assertEqual((mug["price"], mug["discounted_price"]), (20.0, 18.0))
        self.assertEqual(mug["taxes"], [{"rate": 20.0}])
        self.assertEqual(old["taxes"], [{"rate": 20.0}])
        self.assertEqual((mug["variant_id"], mug["inventory_ref"]), ("MUG-1", "MUG-1"))
        self.assertEqual((old["variant_id"], old["inventory_ref"], old["sku"]), ("L1234567-0", None, None))
        self.assertEqual(data["shipping_lines"], [{"title": "UK_RoyalMailSecondClassStandard", "price": 6.0, "taxes": [{"rate": 20.0}]}])
        self.assertEqual(data["discount_lines"], [])  # the 4.00 is already in the line prices
        self.assertEqual(data["currency"], "GBP")
        self.assertEqual(data["customer_id"], "jane_buyer")
        self.assertEqual(data["email"], "jane@members.ebay.com")
        self.assertEqual(data["shipping"]["street2"], "Flat 2")
        self.assertEqual(data["billing"]["street"], "1 High St")
        self.assertEqual(data["note"], "Gift please")
        self.assertTrue(data["paid"])
        self.assertFalse(data["cancelled"])
        self.assertFalse(data["fulfilled"])

    def test_sales_tax_on_top_of_the_price_is_detected_too(self):
        order = self._order()
        order["pricingSummary"]["total"] = amount("62.00")  # 54 + 8 tax
        order["lineItems"][0]["taxes"] = [{"amount": amount("3.60")}]
        order["lineItems"][1]["taxes"] = [{"amount": amount("2.40")}]
        data = normalize_order(order)
        self.assertFalse(data["taxes_included"])
        self.assertEqual(data["lines"][0]["taxes"], [{"rate": 10.0}])  # 3.60 on 36.00
        self.assertEqual(data["lines"][1]["taxes"], [{"rate": 20.0}])

    def test_tax_ebay_collects_and_remits_never_reaches_the_books(self):
        order = self._order(ebayCollectAndRemitTax=True)
        order["pricingSummary"]["total"] = amount("62.00")
        data = normalize_order(order)
        self.assertFalse(data["taxes_included"])
        self.assertEqual([l["taxes"] for l in data["lines"]], [[], []])
        self.assertEqual(data["shipping_lines"][0]["taxes"], [])

    def test_order_level_discounts_are_booked_once(self):
        order = self._order()
        order["pricingSummary"]["priceDiscount"] = amount("-9.00")  # 4.00 in the lines, 5.00 on the order
        order["pricingSummary"]["deliveryDiscount"] = amount("-1.00")
        order["pricingSummary"]["total"] = amount("49.00")
        titles = {d["title"]: d["price"] for d in normalize_order(order)["discount_lines"]}
        self.assertEqual(titles, {"Discount": 5.0, "Shipping discount": 1.0})

    def test_states_cancelled_unpaid_and_fulfilled(self):
        self.assertTrue(normalize_order(self._order(cancelStatus={"cancelState": "CANCELED"}))["cancelled"])
        self.assertFalse(normalize_order(self._order(cancelStatus={"cancelState": "CANCEL_REQUESTED"}))["cancelled"])
        self.assertFalse(normalize_order(self._order(orderPaymentStatus="PENDING"))["paid"])
        self.assertTrue(normalize_order(self._order(orderFulfillmentStatus="FULFILLED"))["fulfilled"])

    def test_refunds_by_whole_line_or_by_amount(self):
        order = self._order()
        order["paymentSummary"]["refunds"] = [
            {"refundId": "R1", "refundStatus": "REFUNDED", "amount": amount("12.00")},
            {"refundId": "R2", "refundStatus": "REFUNDED", "amount": amount("5.00")},
            {"refundId": "R3", "refundStatus": "PENDING", "amount": amount("1.00")},
        ]
        order["lineItems"][1]["refunds"] = [{"refundId": "R1", "amount": amount("12.00")}]
        order["lineItems"][0]["refunds"] = [{"refundId": "R2", "amount": amount("5.00")}]
        r1, r2 = normalize_order(order)["refunds"]
        self.assertEqual(r1, {"external_id": "R1", "amount": 12.0, "lines": [{"line_id": "L2", "qty": 1.0}]})
        self.assertEqual(r2["lines"], [])  # a part of a line: only the amount is known

    # -- importing ------------------------------------------------------------------------

    def test_order_import_matches_the_total_eBay_charged(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        self.assertEqual(order.channel_order_name, "12-34567-89012")
        self.assertEqual(order.state, "sale")
        self.assertAlmostEqual(order.amount_total, 54.0)
        self.assertEqual(order.partner_id.country_id.code, "GB")
        self.assertEqual(order.invoice_ids.payment_state, "paid")
        self.assertIn("Old listing", order.order_line.mapped("name"))
        self.assertEqual(self.store._get_mapped("res.partner", "jane_buyer"), order.partner_id)
        again = self.store._process_order_data(normalize_order({**ORDER, "lastModifiedDate": "2026-10-30T00:00:00.000Z"}))
        self.assertEqual(order, again)

    def test_refund_by_amount_becomes_an_adjustment_credit_note(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        self.store._process_refund(order, {"external_id": "R2", "amount": 5.0, "lines": []})
        credit = self.env["account.move"].search([("channel_refund_id", "=", "R2")])
        self.assertAlmostEqual(credit.amount_total, 5.0)

    def test_scheduled_import_filters_on_last_modified_and_follows_the_pages(self):
        pages = [{"total": 300, "orders": [ORDER, ORDER]}, {"total": 300, "orders": [{**ORDER, "orderId": "12-34567-89013"}]}]
        calls = []

        def fake(method, path, params=None, json=None):
            calls.append((path, dict(params)))
            return pages[len(calls) - 1]

        self.store.import_orders_from = "2026-10-01 00:00:00"
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._import_orders(), 2)  # the repeated order is queued once
        self.assertEqual(calls[0][0], "/sell/fulfillment/v1/order")
        self.assertEqual(calls[0][1]["filter"], "lastmodifieddate:[2026-10-01T00:00:00.000Z]")
        self.assertEqual((calls[0][1]["limit"], calls[0][1]["offset"]), (200, 0))
        self.assertEqual(calls[1][1]["offset"], 200)

    def test_single_order_fetch_for_retries(self):
        with patch(REQUEST, return_value=ORDER) as request:
            data = self.store._fetch_order("12-34567-89012")
        self.assertEqual(data["external_id"], "12-34567-89012")
        self.assertEqual(request.call_args.args[:2], ("GET", "/sell/fulfillment/v1/order/12-34567-89012"))

    def test_connection_names_the_seller_and_creates_one_location(self):
        with patch(REQUEST, return_value={"orders": [ORDER]}):
            self.assertEqual(self.store._test_connection(), "bottlehouse")
            self.store._test_connection()
        self.assertEqual(len(self.store.location_ids), 1)
        with patch(REQUEST, return_value={"orders": []}):
            self.assertEqual(self.store._test_connection(), "eBay UK")

    def test_ebay_has_no_webhooks(self):
        self.assertFalse(self.store.supports_webhooks)

    def test_setup_wizard_passes_the_ebay_credentials_on(self):
        wizard = self.env["channel.setup.wizard"].create(
            {"name": "New", "channel_type": "ebay", "api_key": "a", "api_secret": "b", "ebay_refresh_token": " tok ", "ebay_sandbox": True}
        )
        vals = wizard._prepare_store_vals()
        self.assertEqual((vals["ebay_refresh_token"], vals["ebay_sandbox"]), ("tok", True))

    # -- inventory ------------------------------------------------------------------------

    def test_inventory_items_become_products_linked_by_sku(self):
        data = normalize_inventory_item(INVENTORY["inventoryItems"][0])
        self.assertEqual((data["external_id"], data["title"], data["variants"][0]["barcode"]), ("MUG-1", "Steel mug", "4006381333931"))
        with patch(REQUEST, return_value=INVENTORY), patch.object(type(self.store), "_download_image", return_value=False):
            self.assertEqual(self.store._import_products(), 3)
            self.env["channel.job"]._run_due_jobs()
        mapped = {m.external_id: m.res_id for m in self.store.mapping_ids.filtered(lambda m: m.res_model == "product.product")}
        self.assertEqual((mapped["MUG-1"], mapped["LID-1"]), (self.mug.id, self.lid.id))

    def _stock_setup(self):
        for product, qty in ((self.mug, 7), (self.lid, 3)):
            self.env["stock.quant"]._update_available_quantity(product, self.warehouse.lot_stock_id, qty)
        self.store._set_mapping(self.mug, "MUG-1", "MUG-1")
        self.store._set_mapping(self.lid, "LID-1", "LID-1")
        self.store._sync_locations([{"external_id": "0", "name": "Stock"}])

    def test_stock_goes_out_in_bulk_and_refused_skus_are_named(self):
        self._stock_setup()
        sent = []

        def fake(method, path, params=None, json=None):
            sent.append((method, path, json))
            return {"responses": [{"sku": "MUG-1", "statusCode": 200}, {"sku": "LID-1", "statusCode": 404}]}

        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._push_stock(), 2)
            self.env["channel.job"]._run_due_jobs()
        method, path, body = sent[0]
        self.assertEqual((method, path), ("POST", "/sell/inventory/v1/bulk_update_price_quantity"))
        self.assertEqual(
            sorted((r["sku"], r["shipToLocationAvailability"]["quantity"]) for r in body["requests"]),
            [("LID-1", 3), ("MUG-1", 7)],
        )
        self.assertIn("Lid", self.store.job_ids.filtered(lambda j: j.state == "failed").error)
        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._push_stock(), 1)  # only the refused SKU again

    # -- shipments ------------------------------------------------------------------------

    def _ship(self, order, tracking="TRK9", carrier=None):
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.carrier_tracking_ref = tracking
        if carrier:
            picking.carrier_id = self.env["delivery.carrier"].create(
                {"name": carrier, "delivery_type": "fixed", "product_id": self.env.ref("channel_connector_base.product_channel_shipping").id}
            )
        picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
        return self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment")

    def test_delivery_creates_a_shipping_fulfillment_with_tracking(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        job = self._ship(order, carrier="Royal Mail")
        calls = []
        with patch(REQUEST, side_effect=lambda method, path, params=None, json=None: calls.append((method, path, json)) or {}):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", "/sell/fulfillment/v1/order/12-34567-89012/shipping_fulfillment"))
        self.assertEqual(sorted((i["lineItemId"], i["quantity"]) for i in body["lineItems"]), [("L1", 2), ("L2", 1)])
        self.assertEqual((body["trackingNumber"], body["shippingCarrierCode"]), ("TRK9", "RoyalMail"))
        self.assertTrue(body["shippedDate"].endswith("Z"))

    def test_shipment_without_tracking_is_still_confirmed(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        job = self._ship(order, tracking=False)
        calls = []
        with patch(REQUEST, side_effect=lambda method, path, params=None, json=None: calls.append(json) or {}):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        self.assertNotIn("trackingNumber", calls[0])
        self.assertNotIn("shippingCarrierCode", calls[0])

    # -- API client -----------------------------------------------------------------------

    def _http(self, status, body=None, content=True):
        response = MagicMock(status_code=status, headers={})
        response.json.return_value = body if body is not None else {}
        response.text = str(body or "")
        response.content = b"{}" if content else b""
        return response

    def _token(self, token="tok"):
        return self._http(200, {"access_token": token, "expires_in": 7200})

    def test_credentials_are_required(self):
        store = self.store.copy({"name": "Empty", "ebay_refresh_token": False})
        with self.assertRaisesRegex(ChannelError, "Refresh token"):
            EbayClient(store)

    def test_refresh_token_is_exchanged_once_with_basic_auth_and_cached(self):
        with patch(OAUTH, return_value=self._token()) as oauth, patch(HTTP, return_value=self._http(200, {"ok": 1})) as http:
            client = EbayClient(self.store)
            client.request("GET", "/sell/fulfillment/v1/order")
            client.request("GET", "/sell/fulfillment/v1/order")
        self.assertEqual(oauth.call_count, 1)
        self.assertEqual(oauth.call_args.args[0], "https://api.ebay.com/identity/v1/oauth2/token")
        self.assertEqual(oauth.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.assertEqual(oauth.call_args.kwargs["headers"]["Authorization"], "Basic QXBwSWQ6Q2VydElk")  # AppId:CertId
        self.assertEqual(http.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    def test_sandbox_uses_the_sandbox_host(self):
        self.store.ebay_sandbox = True
        self.assertEqual(EbayClient(self.store).host, "https://api.sandbox.ebay.com")

    def test_rejected_keys_and_tokens_say_what_to_do(self):
        with patch(OAUTH, return_value=self._http(401, {"error": "invalid_client"})):
            with self.assertRaisesRegex(ChannelAuthError, "App ID"):
                EbayClient(self.store)._token(force=True)
        with patch(OAUTH, return_value=self._http(400, {"error": "invalid_grant"})):
            with self.assertRaisesRegex(ChannelAuthError, "18 months"):
                EbayClient(self.store)._token(force=True)

    def test_expired_access_token_is_replaced_once(self):
        self.store.write({"access_token": "old", "token_expiry": fields.Datetime.now() + timedelta(hours=1)})
        answers = [self._http(401, {"errors": [{"message": "expired"}]}), self._http(200, {"fine": True})]
        with patch(OAUTH, return_value=self._token("new")), patch(HTTP, side_effect=answers) as http:
            self.assertEqual(EbayClient(self.store).request("GET", "/x"), {"fine": True})
        self.assertEqual(http.call_args.kwargs["headers"]["Authorization"], "Bearer new")

    def test_missing_scope_is_not_a_lost_connection(self):
        denied = self._http(403, {"errors": [{"longMessage": "Insufficient permissions"}]})
        with patch(OAUTH, return_value=self._token()), patch(HTTP, return_value=denied):
            with self.assertRaises(ChannelError) as caught:
                EbayClient(self.store).request("GET", "/x")
        self.assertNotIsInstance(caught.exception, ChannelAuthError)
        self.assertIn("sell.fulfillment", str(caught.exception))
        self.assertIn("Insufficient permissions", str(caught.exception))

    def test_throttling_and_server_errors_are_retried_and_empty_answers_are_fine(self):
        answers = [self._http(429), self._http(503), self._http(201, content=False)]
        with patch(OAUTH, return_value=self._token()), patch(SLEEP), patch(HTTP, side_effect=answers):
            self.assertEqual(EbayClient(self.store).request("POST", "/x"), {})
