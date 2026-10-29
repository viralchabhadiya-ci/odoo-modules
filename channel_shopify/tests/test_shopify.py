import base64
import copy
import hashlib
import hmac
import json
from unittest.mock import patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError
from odoo.tests import tagged

from ..models.channel_store import normalize_order, normalize_product
from ..models.shopify_client import ShopifyClient, normalize_shop

CLIENT = "odoo.addons.channel_shopify.models.shopify_client.ShopifyClient.graphql"

ORDER_NODE = {
    "id": "gid://shopify/Order/1001",
    "legacyResourceId": "1001",
    "name": "#1001",
    "createdAt": "2026-10-29T10:00:00Z",
    "updatedAt": "2026-10-29T10:05:00Z",
    "cancelledAt": None,
    "note": "Leave at the door",
    "email": "Ayesha@Example.com",
    "phone": "+971500000000",
    "taxesIncluded": False,
    "presentmentCurrencyCode": "USD",
    "displayFinancialStatus": "PAID",
    "displayFulfillmentStatus": "UNFULFILLED",
    "paymentGatewayNames": ["Stripe"],
    "customer": {"legacyResourceId": "777"},
    "billingAddress": {
        "name": "Ayesha Khan", "company": None, "address1": "1 Marina Walk", "address2": None,
        "city": "Dubai", "zip": "00000", "provinceCode": None, "countryCodeV2": "AE", "phone": None,
    },
    "shippingAddress": {
        "name": "Ayesha Khan", "company": None, "address1": "9 Palm Road", "address2": None,
        "city": "Dubai", "zip": "00000", "provinceCode": None, "countryCodeV2": "AE", "phone": None,
    },
    "lineItems": {"nodes": [
        {
            "id": "gid://shopify/LineItem/11", "name": "T-Shirt - Red / M", "sku": "TS-RED-M", "quantity": 2,
            "variant": {"legacyResourceId": "501", "barcode": None, "inventoryItem": {"legacyResourceId": "901"}},
            "originalUnitPriceSet": {"presentmentMoney": {"amount": "20.00"}},
            "discountedUnitPriceSet": {"presentmentMoney": {"amount": "18.00"}},
            "taxLines": [{"rate": 0.05, "title": "VAT"}],
        },
        {
            "id": "gid://shopify/LineItem/12", "name": "Mug", "sku": "MUG-1", "quantity": 1,
            "variant": {"legacyResourceId": "502", "barcode": None, "inventoryItem": {"legacyResourceId": "902"}},
            "originalUnitPriceSet": {"presentmentMoney": {"amount": "10.00"}},
            "discountedUnitPriceSet": {"presentmentMoney": {"amount": "10.00"}},
            "taxLines": [{"rate": 0.05, "title": "VAT"}],
        },
    ]},
    "shippingLines": {"nodes": [
        {"title": "Standard", "discountedPriceSet": {"presentmentMoney": {"amount": "5.00"}}, "taxLines": []},
    ]},
    "refunds": [],
}


@tagged("post_install", "-at_install")
class TestShopify(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref("channel_connector_base.group_channel_manager")
        cls.company = cls.env.company
        cls.usd = cls.env.ref("base.USD")
        cls.usd.active = True
        cls.env["account.tax"].create(
            {"name": "VAT 5%", "amount": 5.0, "amount_type": "percent", "type_tax_use": "sale", "company_id": cls.company.id}
        )
        cls.bank = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.store = cls.env["channel.store"].create(
            {
                "name": "Test shop",
                "channel_type": "shopify",
                "shop_url": "test-shop",
                "api_key": "id",
                "api_secret": "secret",
                "state": "connected",
                "warehouse_id": cls.warehouse.id,
                "import_orders_from": "2020-01-01 00:00:00",
                "workflow_ids": [(0, 0, {"name": "Paid", "payment_journal_id": cls.bank.id})],
            }
        )
        cls.tshirt = cls.env["product.product"].create(
            {"name": "T-Shirt Red M", "default_code": "TS-RED-M", "is_storable": True, "type": "consu"}
        )
        cls.mug = cls.env["product.product"].create(
            {"name": "Mug", "default_code": "MUG-1", "is_storable": True, "type": "consu"}
        )

    def _order(self, **changes):
        node = copy.deepcopy(ORDER_NODE)
        node.update(changes)
        return normalize_order(node)

    # -- helpers ---------------------------------------------------------------------

    def test_shop_address_is_normalised(self):
        self.assertEqual(normalize_shop("https://My-Shop.myshopify.com/admin/"), "my-shop.myshopify.com")
        self.assertEqual(normalize_shop("my-shop"), "my-shop.myshopify.com")

    def test_normalize_order_scales_tax_rate(self):
        data = self._order()
        self.assertEqual(data["lines"][0]["taxes"][0]["rate"], 5.0)
        self.assertTrue(data["paid"])
        self.assertEqual(data["gateways"], ["Stripe"])

    # -- connection ------------------------------------------------------------------

    def test_connection_creates_locations_once(self):
        reply = {
            "shop": {"name": "Kumbh Living"},
            "locations": {"nodes": [
                {"id": "gid://shopify/Location/1", "legacyResourceId": "1", "name": "Dubai", "isActive": True},
                {"id": "gid://shopify/Location/2", "legacyResourceId": "2", "name": "Closed", "isActive": False},
                {"id": "gid://shopify/Location/3", "legacyResourceId": "3", "name": "Sharjah", "isActive": True},
            ]},
        }
        with patch(CLIENT, return_value=reply):
            self.assertEqual(self.store._test_connection(), "Kumbh Living")
            self.store._test_connection()
        self.assertEqual(self.store.shop_url, "test-shop.myshopify.com")
        self.assertEqual(len(self.store.location_ids), 2)
        # only one location gets the warehouse, otherwise stock would be counted twice
        self.assertEqual(len(self.store.location_ids.filtered("warehouse_id")), 1)

    def test_bad_credentials_give_a_readable_error(self):
        store = self.store.copy({"name": "No creds", "api_key": False, "api_secret": False, "access_token": False})
        with self.assertRaisesRegex(ChannelError, "Client ID"):
            ShopifyClient(store)._token()

    # -- orders ----------------------------------------------------------------------

    def test_order_import_runs_the_workflow(self):
        order = self.store._process_order_data(self._order())
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.channel_order_name, "#1001")
        self.assertEqual(order.partner_id.email.lower(), "ayesha@example.com")
        self.assertNotEqual(order.partner_shipping_id, order.partner_id)
        product_lines = order.order_line.filtered("product_id").filtered(lambda l: l.product_id in (self.tshirt | self.mug))
        self.assertEqual(len(product_lines), 2)
        shirt = product_lines.filtered(lambda l: l.product_id == self.tshirt)
        self.assertAlmostEqual(shirt.discount, 10.0)
        self.assertEqual(shirt.tax_ids.amount, 5.0)
        self.assertTrue(order.order_line.filtered(lambda l: l.product_id == self.store.shipping_product_id))
        invoice = order.invoice_ids
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.payment_state, "paid")

    def test_order_import_is_idempotent(self):
        first = self.store._process_order_data(self._order())
        second = self.store._process_order_data(self._order(updatedAt="2026-10-29T11:00:00Z"))
        self.assertEqual(first, second)
        self.assertEqual(self.env["sale.order"].search_count([("channel_store_id", "=", self.store.id)]), 1)

    def test_unpaid_order_stays_a_quotation(self):
        order = self.store._process_order_data(self._order(displayFinancialStatus="PENDING", legacyResourceId="1002", name="#1002"))
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.invoice_ids)

    def test_unknown_product_without_auto_create_fails_clearly(self):
        self.store.create_missing_products = False
        node = copy.deepcopy(ORDER_NODE)
        node["lineItems"]["nodes"][1]["sku"] = "NOPE-1"
        with self.assertRaisesRegex(ChannelError, "NOPE-1"):
            self.store._process_order_data(normalize_order(node))

    def test_unknown_product_is_created_when_allowed(self):
        node = copy.deepcopy(ORDER_NODE)
        node["lineItems"]["nodes"][1]["sku"] = "NEW-1"
        order = self.store._process_order_data(normalize_order(node))
        self.assertTrue(self.env["product.product"].search([("default_code", "=", "NEW-1")]))
        self.assertTrue(order)

    def test_cancellation_from_shopify_cancels_open_order(self):
        self.store.workflow_ids.confirm_order = False
        order = self.store._process_order_data(self._order(displayFinancialStatus="PENDING"))
        self.store._process_order_data(self._order(cancelledAt="2026-10-29T12:00:00Z", displayFinancialStatus="PENDING"))
        self.assertEqual(order.state, "cancel")

    def test_refund_creates_credit_note_for_refunded_lines(self):
        self.store._process_order_data(self._order())
        refund = {
            "id": "gid://shopify/Refund/9", "legacyResourceId": "9",
            "totalRefundedSet": {"presentmentMoney": {"amount": "18.90"}},
            "refundLineItems": {"nodes": [{"quantity": 1, "lineItem": {"id": "gid://shopify/LineItem/11", "sku": "TS-RED-M"}}]},
        }
        data = self._order(refunds=[refund])
        self.store._process_order_data(data)
        self.store._process_order_data(data)  # twice: still one credit note
        notes = self.env["account.move"].search([("channel_refund_id", "=", "9")])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.move_type, "out_refund")
        self.assertEqual(notes.invoice_line_ids.product_id, self.tshirt)
        self.assertEqual(notes.invoice_line_ids.quantity, 1)
        self.assertEqual(notes.state, "draft")

    def test_fetch_orders_queues_one_job_each(self):
        reply = {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [copy.deepcopy(ORDER_NODE)]}}
        with patch(CLIENT, return_value=reply):
            self.assertEqual(self.store._import_orders(), 1)
            self.assertEqual(self.store._import_orders(), 0)  # same updatedAt: deduplicated
        job = self.store.job_ids
        self.assertEqual(job.job_type, "import_order")
        job._run()
        self.assertEqual(job.state, "done")
        self.assertEqual(job.res_model, "sale.order")

    # -- jobs ------------------------------------------------------------------------

    def test_failed_job_keeps_a_plain_message_and_retries_later(self):
        self.store.create_missing_products = False
        node = copy.deepcopy(ORDER_NODE)
        node["lineItems"]["nodes"][0]["sku"] = "GHOST"
        job = self.store._queue_job("import_order", "Import #1001", {"order": normalize_order(node)})
        job._run()
        self.assertEqual(job.state, "pending")
        self.assertEqual(job.retry_count, 1)
        self.assertIn("GHOST", job.error)
        job.max_retries = 1
        job.action_retry()
        self.assertEqual(job.state, "failed")

    # -- products & stock ----------------------------------------------------------------

    def test_product_import_builds_variants_and_maps_them(self):
        node = {
            "id": "gid://shopify/Product/1", "legacyResourceId": "1", "title": "Hoodie",
            "descriptionHtml": "<p>Warm</p>", "productType": "Apparel", "updatedAt": "2026-10-01T00:00:00Z",
            "featuredMedia": None,
            "options": [{"name": "Size", "position": 1}],
            "variants": {"nodes": [
                {"id": "g/1", "legacyResourceId": "11", "sku": "HOOD-S", "barcode": None, "price": "40.00",
                 "inventoryItem": {"legacyResourceId": "91"}, "selectedOptions": [{"name": "Size", "value": "S"}]},
                {"id": "g/2", "legacyResourceId": "12", "sku": "HOOD-L", "barcode": None, "price": "40.00",
                 "inventoryItem": {"legacyResourceId": "92"}, "selectedOptions": [{"name": "Size", "value": "L"}]},
            ]},
        }
        template = self.store._process_product_data(normalize_product(node))
        self.assertEqual(len(template.product_variant_ids), 2)
        small = self.store._get_mapped("product.product", "11")
        self.assertEqual(small.default_code, "HOOD-S")
        self.assertEqual(self.store._get_mapped("product.template", "1"), template)
        # importing again changes nothing
        self.assertEqual(self.store._process_product_data(normalize_product(node)), template)
        self.assertEqual(len(template.product_variant_ids), 2)

    def test_stock_is_pushed_only_when_it_changes(self):
        self.env["stock.quant"]._update_available_quantity(self.tshirt, self.warehouse.lot_stock_id, 7)
        self.store._set_mapping(self.tshirt, "501", "901")
        self.env["channel.location"].create(
            {"store_id": self.store.id, "name": "Dubai", "external_id": "1", "warehouse_id": self.warehouse.id}
        )
        self.store.stock_safety = 2
        calls = []
        with patch(CLIENT, side_effect=lambda q, v=None: calls.append(v) or {"inventorySetQuantities": {"userErrors": []}}):
            self.assertEqual(self.store._push_stock(), 1)
            self.env["channel.job"]._run_due_jobs()
            self.assertEqual(self.store._push_stock(), 0)
        quantity = calls[0]["input"]["quantities"][0]
        self.assertEqual(quantity["quantity"], 5)
        self.assertEqual(quantity["inventoryItemId"], "gid://shopify/InventoryItem/901")
        self.assertIsNone(quantity["changeFromQuantity"])

    def test_stock_user_errors_become_job_errors(self):
        self.store._set_mapping(self.tshirt, "501", "901")
        self.env["channel.location"].create(
            {"store_id": self.store.id, "name": "Dubai", "external_id": "1", "warehouse_id": self.warehouse.id}
        )
        self.env["stock.quant"]._update_available_quantity(self.tshirt, self.warehouse.lot_stock_id, 3)
        reply = {"inventorySetQuantities": {"userErrors": [{"field": ["x"], "message": "Location not stocked"}]}}
        with patch(CLIENT, return_value=reply):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
        self.assertIn("Location not stocked", self.store.job_ids.error)

    # -- shipments -----------------------------------------------------------------------

    def test_validated_delivery_queues_and_sends_fulfillment(self):
        self.store.workflow_ids.validate_delivery = False
        order = self.store._process_order_data(self._order())
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
        job = self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment")
        self.assertEqual(len(job), 1)
        fulfillment_orders = {"order": {"fulfillmentOrders": {"nodes": [{
            "id": "gid://shopify/FulfillmentOrder/5", "status": "OPEN",
            "lineItems": {"nodes": [
                {"id": "gid://shopify/FulfillmentOrderLineItem/1", "remainingQuantity": 2, "lineItem": {"id": "x", "sku": "TS-RED-M"}},
                {"id": "gid://shopify/FulfillmentOrderLineItem/2", "remainingQuantity": 1, "lineItem": {"id": "y", "sku": "MUG-1"}},
            ]},
        }]}}}
        sent = []

        def fake(query, variables=None):
            if "fulfillmentOrders" in query:
                return fulfillment_orders
            sent.append(variables["fulfillment"])
            return {"fulfillmentCreate": {"userErrors": []}}

        with patch(CLIENT, side_effect=fake):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        items = sent[0]["lineItemsByFulfillmentOrder"][0]["fulfillmentOrderLineItems"]
        self.assertEqual({i["quantity"] for i in items}, {2, 1})

    def test_already_fulfilled_on_shopify_is_explained(self):
        order = self.store._process_order_data(self._order())
        picking = order.picking_ids
        with patch(CLIENT, return_value={"order": {"fulfillmentOrders": {"nodes": []}}}):
            with self.assertRaisesRegex(ChannelError, "already be fulfilled"):
                self.store._push_fulfillment(picking)

    # -- webhooks -------------------------------------------------------------------------

    def test_webhook_signature(self):
        body = json.dumps({"admin_graphql_api_id": "gid://shopify/Order/1001", "name": "#1001", "updated_at": "x"}).encode()
        good = base64.b64encode(hmac.new(b"secret", body, hashlib.sha256).digest()).decode()
        self.assertTrue(self.store._verify_webhook(body, {"X-Shopify-Hmac-Sha256": good}))
        self.assertFalse(self.store._verify_webhook(body, {"X-Shopify-Hmac-Sha256": "bad"}))
        self.assertFalse(self.store._verify_webhook(body, {}))
        parsed = self.store._parse_webhook(body, {"X-Shopify-Topic": "orders/create"})
        self.assertEqual(parsed[0][0], "import_order")
        self.assertEqual(parsed[0][2], {"external_id": "gid://shopify/Order/1001"})
        self.assertEqual(self.store._parse_webhook(body, {"X-Shopify-Topic": "customers/update"}), [])

    # -- setup wizard -----------------------------------------------------------------------

    def test_setup_wizard_two_steps(self):
        reply = {
            "shop": {"name": "Kumbh Living"},
            "locations": {"nodes": [{"id": "g/1", "legacyResourceId": "1", "name": "Dubai", "isActive": True}]},
        }
        wizard = self.env["channel.setup.wizard"].create(
            {
                "channel_type": "shopify", "name": "Wizard shop", "shop_url": "https://wizard-shop.myshopify.com",
                "api_key": "id", "api_secret": "secret", "company_id": self.company.id,
            }
        )
        with patch(CLIENT, return_value=reply):
            wizard.action_connect()
        store = wizard.store_id
        self.assertEqual((wizard.step, store.state), ("settings", "connected"))
        self.assertEqual(store.shop_url, "wizard-shop.myshopify.com")
        with patch(CLIENT, return_value={"products": {"pageInfo": {"hasNextPage": False}, "nodes": []}, "orders": {"pageInfo": {"hasNextPage": False}, "nodes": []}}):
            wizard.action_finish()
        self.assertEqual(store.workflow_ids.name, "Paid orders")
        self.assertEqual(store.location_ids.warehouse_id, store.warehouse_id)

    def test_setup_wizard_bad_credentials_leave_nothing_behind(self):
        wizard = self.env["channel.setup.wizard"].create(
            {"channel_type": "shopify", "name": "Bad", "shop_url": "bad", "company_id": self.company.id}
        )
        before = self.env["channel.store"].search_count([])
        with self.assertRaises(ChannelError), self.cr.savepoint():
            wizard.action_connect()
        self.assertEqual(self.env["channel.store"].search_count([]), before)

    # -- scheduled runs -------------------------------------------------------------------

    def test_network_outage_does_not_disconnect_the_store(self):
        with patch(CLIENT, side_effect=ChannelError("Could not reach shop")):
            self.store._safe_run("import_orders", self.store._import_orders)
            self.store._safe_run("import_orders", self.store._import_orders)
        self.assertEqual(self.store.state, "connected")
        jobs = self.store.job_ids
        self.assertEqual(len(jobs), 1, "one job per problem, not one per cron run")
        self.assertEqual(jobs.state, "failed")

    def test_rejected_credentials_flag_the_store_and_recovery_closes_the_job(self):
        with patch(CLIENT, side_effect=ChannelAuthError("Shopify did not accept the credentials")):
            self.store._safe_run("import_orders", self.store._import_orders)
        self.assertEqual(self.store.state, "error")
        self.store.state = "connected"
        empty = {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}
        with patch(CLIENT, return_value=empty):
            self.store._safe_run("import_orders", self.store._import_orders)
        self.assertEqual(self.store.job_ids.state, "done")
