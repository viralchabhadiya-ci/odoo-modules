import base64
import copy
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.channel_connector_base.models.channel_error import ChannelAuthError, ChannelError
from odoo.tests import tagged

from ..models.channel_store import normalize_order, normalize_product, normalize_refund
from ..models.woo_client import WooClient, normalize_site

REQUEST = "odoo.addons.channel_woocommerce.models.woo_client.WooClient.request"
HTTP = "odoo.addons.channel_woocommerce.models.woo_client.requests.request"

ORDER = {
    "id": 5521,
    "number": "5521",
    "status": "processing",
    "currency": "USD",
    "date_created_gmt": "2026-10-29T10:00:00",
    "date_modified_gmt": "2026-10-29T10:05:00",
    "date_paid_gmt": "2026-10-29T10:01:00",
    "customer_id": 0,
    "customer_note": "Ring the bell",
    "payment_method": "stripe",
    "payment_method_title": "Credit card (Stripe)",
    "billing": {
        "first_name": "Omar", "last_name": "Saleh", "company": "", "address_1": "5 Souk St", "address_2": "",
        "city": "Dubai", "state": "", "postcode": "00000", "country": "AE", "email": "Omar@Example.com", "phone": "+971501111111",
    },
    "shipping": {
        "first_name": "Omar", "last_name": "Saleh", "company": "", "address_1": "12 Beach Rd", "address_2": "",
        "city": "Dubai", "state": "", "postcode": "00000", "country": "AE", "phone": "",
    },
    "line_items": [
        {"id": 31, "name": "Hoodie - L", "product_id": 70, "variation_id": 71, "quantity": 2, "sku": "HOOD-L",
         "subtotal": "80.00", "total": "72.00", "taxes": [{"id": 9, "total": "3.60", "subtotal": "4.00"}]},
        {"id": 32, "name": "Cap", "product_id": 80, "variation_id": 0, "quantity": 1, "sku": "CAP-1",
         "subtotal": "10.00", "total": "10.00", "taxes": [{"id": 9, "total": "0.50", "subtotal": "0.50"}]},
    ],
    "shipping_lines": [{"method_title": "Flat rate", "total": "5.00", "taxes": []}],
    "tax_lines": [{"rate_id": 9, "rate_code": "VAT-1", "label": "VAT", "rate_percent": 5}],
    "refunds": [],
}


@tagged("post_install", "-at_install")
class TestWooCommerce(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.group_ids |= cls.env.ref("channel_connector_base.group_channel_manager")
        cls.company = cls.env.company
        cls.env.ref("base.USD").active = True
        cls.env["account.tax"].create(
            {"name": "VAT 5%", "amount": 5.0, "amount_type": "percent", "type_tax_use": "sale", "company_id": cls.company.id}
        )
        bank = cls.env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", cls.company.id)], limit=1)
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.store = cls.env["channel.store"].create(
            {
                "name": "Woo shop",
                "channel_type": "woocommerce",
                "shop_url": "https://shop.example.com",
                "api_key": "ck_x",
                "api_secret": "cs_x",
                "webhook_secret": "whsec",
                "state": "connected",
                "warehouse_id": cls.warehouse.id,
                "import_orders_from": "2020-01-01 00:00:00",
                "workflow_ids": [(0, 0, {"name": "Paid", "payment_journal_id": bank.id})],
            }
        )
        cls.hoodie = cls.env["product.product"].create(
            {"name": "Hoodie L", "default_code": "HOOD-L", "is_storable": True, "type": "consu"}
        )
        cls.cap = cls.env["product.product"].create(
            {"name": "Cap", "default_code": "CAP-1", "is_storable": True, "type": "consu"}
        )

    def _order(self, **changes):
        return {**copy.deepcopy(ORDER), **changes}

    # -- helpers ------------------------------------------------------------------------

    def test_site_is_normalised_and_http_refused(self):
        self.assertEqual(normalize_site("shop.example.com/"), "https://shop.example.com")
        store = self.store.copy({"name": "Plain http", "shop_url": "http://shop.example.com"})
        with self.assertRaisesRegex(ChannelError, "https"):
            WooClient(store)

    def test_normalize_order_uses_pre_tax_prices_and_rates_from_the_order(self):
        data = normalize_order(ORDER)
        hoodie = data["lines"][0]
        self.assertEqual(hoodie["price"], 40.0)
        self.assertEqual(hoodie["discounted_price"], 36.0)
        self.assertEqual(hoodie["taxes"], [{"rate": 5.0}])
        self.assertEqual(hoodie["variant_id"], "71")
        self.assertEqual(hoodie["inventory_ref"], "70/71")
        self.assertEqual(data["lines"][1]["variant_id"], "80")
        self.assertEqual(data["lines"][1]["inventory_ref"], "80")
        self.assertIsNone(data["customer_id"])
        self.assertTrue(data["paid"])
        self.assertIn("stripe", data["gateways"])
        self.assertEqual(data["billing"]["name"], "Omar Saleh")

    # -- connection errors are readable -----------------------------------------------------

    def _http(self, status, body=None):
        response = MagicMock(status_code=status, headers={})
        response.json.return_value = body if body is not None else {}
        response.text = json.dumps(body or {})
        return response

    def test_wrong_keys_say_what_to_do(self):
        with patch(HTTP, return_value=self._http(401)):
            with self.assertRaises(ChannelAuthError) as caught:
                WooClient(self.store).request("GET", "system_status")
        self.assertIn("Read/Write", str(caught.exception))

    def test_missing_rest_api_points_to_permalinks(self):
        with patch(HTTP, return_value=self._http(404)):
            with self.assertRaisesRegex(ChannelError, "Permalinks"):
                WooClient(self.store).request("GET", "system_status")

    def test_connection_creates_one_pseudo_location(self):
        status = {"environment": {"site_url": "https://shop.example.com"}, "settings": {"currency": "AED"}}
        with patch(REQUEST, return_value=status):
            result = self.store._test_connection()
            self.store._test_connection()
        self.assertIn("AED", result)
        self.assertEqual(len(self.store.location_ids), 1)
        self.assertEqual(self.store.location_ids.warehouse_id, self.warehouse)

    # -- orders --------------------------------------------------------------------------------

    def test_order_import_creates_invoice_and_payment(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        self.assertEqual(order.channel_order_name, "#5521")
        self.assertEqual(order.state, "sale")
        hoodie_line = order.order_line.filtered(lambda line: line.product_id == self.hoodie)
        self.assertAlmostEqual(hoodie_line.price_unit, 40.0)
        self.assertAlmostEqual(hoodie_line.discount, 10.0)
        self.assertEqual(hoodie_line.tax_ids.amount, 5.0)
        self.assertEqual(order.partner_id.email.lower(), "omar@example.com")
        self.assertNotEqual(order.partner_shipping_id, order.partner_id)
        self.assertEqual(order.invoice_ids.payment_state, "paid")
        self.assertEqual(order.client_order_ref, "#5521")

    def test_order_import_is_idempotent_and_cod_stays_quotation(self):
        cod = self._order(status="on-hold", date_paid_gmt=None, id=5522, number="5522", payment_method="cod")
        first = self.store._process_order_data(normalize_order(cod))
        again = self.store._process_order_data(normalize_order({**cod, "date_modified_gmt": "2026-10-30T00:00:00"}))
        self.assertEqual(first, again)
        self.assertEqual(first.state, "draft")

    def test_cancelled_orders_are_not_imported_but_cancel_existing_ones(self):
        self.store.workflow_ids.confirm_order = False
        order = self.store._process_order_data(normalize_order(self._order(status="pending", date_paid_gmt=None)))
        self.store._process_order_data(normalize_order(self._order(status="cancelled", date_paid_gmt=None)))
        self.assertEqual(order.state, "cancel")
        ghost = normalize_order(self._order(status="cancelled", id=9999, number="9999"))
        self.assertFalse(self.store._process_order_data(ghost))

    def test_refund_lines_become_a_credit_note_for_that_quantity(self):
        refund = {
            "id": 88, "total": "-40.00",
            "line_items": [
                {"sku": "HOOD-L", "quantity": -1, "meta_data": [{"key": "_refunded_item_id", "value": "31"}]}
            ],
        }
        data = normalize_order(self._order(refunds=[{"id": 88, "total": "-40.00"}]), [refund])
        order = self.store._process_order_data(data)
        self.store._process_order_data(data)
        notes = self.env["account.move"].search([("channel_refund_id", "=", "88")])
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes.invoice_line_ids.product_id, self.hoodie)
        self.assertEqual(notes.invoice_line_ids.quantity, 1)
        self.assertIn("Credit note", order.message_ids[0].body)

    def test_amount_only_refund_uses_the_adjustment_product(self):
        refund = normalize_refund({"id": 89, "total": "-12.50", "line_items": []})
        self.assertEqual(refund["lines"], [])
        self.store._process_order_data(normalize_order(self._order(refunds=[{"id": 89}]), [{"id": 89, "total": "-12.50", "line_items": []}]))
        note = self.env["account.move"].search([("channel_refund_id", "=", "89")])
        self.assertEqual(note.invoice_line_ids.product_id, self.store.adjustment_product_id)
        self.assertAlmostEqual(note.invoice_line_ids.price_unit, 12.5)

    def test_scheduled_import_asks_for_modified_orders_and_loads_refunds(self):
        calls = []

        def fake(method, path, params=None, json=None, full=False):
            calls.append((path, params))
            if path == "orders":
                return [self._order(refunds=[{"id": 88}])], MagicMock(headers={"X-WP-TotalPages": "1"})
            return [{"id": 88, "total": "-1.00", "line_items": []}]

        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._import_orders(), 1)
        self.assertEqual(calls[0][1]["dates_are_gmt"], "true")
        self.assertIn("modified_after", calls[0][1])
        self.assertEqual(calls[1][0], "orders/5521/refunds")

    # -- products -------------------------------------------------------------------------------

    def test_variable_product_import_maps_variations(self):
        product = {
            "id": 70, "name": "Hoodie", "type": "variable", "description": "<p>Warm</p>", "sku": "",
            "images": [], "categories": [{"name": "Apparel"}],
            "attributes": [{"name": "Size", "position": 0, "variation": True, "options": ["S", "L"]}],
        }
        variations = [
            {"id": 71, "sku": "HOOD-L2", "price": "40", "attributes": [{"name": "Size", "option": "L"}]},
            {"id": 72, "sku": "HOOD-S", "price": "40", "attributes": [{"name": "Size", "option": "S"}]},
        ]
        template = self.store._process_product_data(normalize_product(product, variations))
        self.assertEqual(len(template.product_variant_ids), 2)
        self.assertEqual(self.store._get_mapped("product.product", "72").default_code, "HOOD-S")
        self.assertEqual(
            self.env["channel.mapping"].search([("store_id", "=", self.store.id), ("external_id", "=", "72")]).external_ref,
            "70/72",
        )

    def test_simple_product_import(self):
        product = {"id": 90, "name": "Poster", "type": "simple", "sku": "POST-1", "price": "9.5", "images": [], "categories": []}
        template = self.store._process_product_data(normalize_product(product))
        self.assertEqual(template.default_code, "POST-1")
        self.assertEqual(self.store._get_mapped("product.product", "90"), template.product_variant_id)

    # -- stock ------------------------------------------------------------------------------------

    def test_stock_goes_out_in_batches_per_product_type(self):
        self.env["stock.quant"]._update_available_quantity(self.hoodie, self.warehouse.lot_stock_id, 6)
        self.env["stock.quant"]._update_available_quantity(self.cap, self.warehouse.lot_stock_id, 4)
        self.store._set_mapping(self.hoodie, "71", "70/71")
        self.store._set_mapping(self.cap, "80", "80")
        self.store._sync_locations([{"external_id": "0", "name": "Stock"}])
        sent = []

        def fake(method, path, params=None, json=None, full=False):
            sent.append((path, json))
            return {"update": [{"id": row["id"]} for row in json["update"]]}

        with patch(REQUEST, side_effect=fake):
            self.assertEqual(self.store._push_stock(), 2)
            self.env["channel.job"]._run_due_jobs()
        paths = {path: body["update"] for path, body in sent}
        self.assertEqual(paths["products/batch"], [{"id": 80, "manage_stock": True, "stock_quantity": 4}])
        self.assertEqual(
            paths["products/70/variations/batch"], [{"id": 71, "manage_stock": True, "stock_quantity": 6}]
        )

    def test_stock_errors_from_woocommerce_are_reported(self):
        self.env["stock.quant"]._update_available_quantity(self.cap, self.warehouse.lot_stock_id, 4)
        self.store._set_mapping(self.cap, "80", "80")
        self.store._sync_locations([{"external_id": "0", "name": "Stock"}])
        reply = {"update": [{"id": 80, "error": {"code": "invalid_id", "message": "Invalid ID"}}]}
        with patch(REQUEST, return_value=reply):
            self.store._push_stock()
            self.env["channel.job"]._run_due_jobs()
        self.assertIn("Invalid ID", self.store.job_ids.error)

    # -- shipments ----------------------------------------------------------------------------------

    def test_delivery_adds_tracking_note_and_completes_the_order(self):
        order = self.store._process_order_data(normalize_order(ORDER))
        picking = order.picking_ids
        for move in picking.move_ids:
            move.quantity = move.product_uom_qty
        picking.carrier_tracking_ref = "TRK123"
        picking.with_context(skip_sms=True, skip_backorder=True).button_validate()
        job = self.store.job_ids.filtered(lambda j: j.job_type == "push_fulfillment")
        calls = []
        with patch(REQUEST, side_effect=lambda method, path, params=None, json=None, full=False: calls.append((method, path, json)) or {}):
            job._run()
        self.assertEqual(job.state, "done", job.error)
        self.assertEqual(calls[0][1], "orders/5521/notes")
        self.assertIn("TRK123", calls[0][2]["note"])
        self.assertEqual(calls[1], ("PUT", "orders/5521", {"status": "completed"}))

    # -- webhooks --------------------------------------------------------------------------------------

    def test_webhook_signature_and_ping(self):
        body = json.dumps({"id": 5521, "number": "5521", "date_modified_gmt": "x"}).encode()
        good = base64.b64encode(hmac.new(b"whsec", body, hashlib.sha256).digest()).decode()
        self.assertTrue(self.store._verify_webhook(body, {"X-WC-Webhook-Signature": good}))
        self.assertFalse(self.store._verify_webhook(body, {"X-WC-Webhook-Signature": "nope"}))
        queued = self.store._parse_webhook(body, {"X-WC-Webhook-Topic": "order.updated"})
        self.assertEqual(queued[0][2], {"external_id": "5521"})
        self.assertEqual(self.store._parse_webhook(b"webhook_id=3", {}), [])

    def test_registering_webhooks_needs_https_and_is_not_duplicated(self):
        self.env["ir.config_parameter"].set_param("web.base.url", "http://odoo.local")
        self.store.invalidate_recordset(["webhook_url"])
        with self.assertRaisesRegex(ChannelError, "https"):
            self.store._register_webhooks()

        self.env["ir.config_parameter"].set_param("web.base.url", "https://odoo.example.com")
        self.store.invalidate_recordset(["webhook_url"])
        url = self.store.webhook_url
        created = []

        def fake(method, path, params=None, json=None, full=False):
            if method == "GET":
                return [{"topic": "order.created", "delivery_url": url}], MagicMock(headers={})
            created.append(json["topic"])
            return {}

        with patch(REQUEST, side_effect=fake):
            self.store._register_webhooks()
        self.assertEqual(created, ["order.updated"])
